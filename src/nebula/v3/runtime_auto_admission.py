"""Project admission for chats and queued project work, and cold API recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import and_, func, or_, select

from .database import (
    DeploymentHostRow,
    DeploymentOperationRow,
    DeploymentReceiptRow,
    EntityRow,
    MemoryBudgetPolicyRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeProcessRow,
    RuntimeRecoveryRow,
)
from .diagnostics import record_caught_exception
from .domain import Engagement
from .runtime_coordination import CoordinationConflict, project_worker_commands
from .setup import bootstrap_scratch_project

_RECOVERY_ACTOR = "runtime-auto-recovery"
_RECOVERY_WINDOW_MS = 10 * 60_000
_RECOVERY_LIMIT = 3
_RECOVERY_RETRY_MS = 30_000


# A refused automatic recovery step proved no effect; it is asked again after
# this long, so a transient refusal never blocks recovery for good.
REFUSED_RECOVERY_RETRY_MS = 30_000


class RuntimeAutoAdmission:
    def __init__(self, supervisor, *, default_version: str):
        self.supervisor = supervisor
        self.default_version = default_version
        self.task = None
        self._pending: dict[str, str] = {}
        self._blocked: dict[str, tuple[int, int | None]] = {}
        self._retirement_cursor: str | None = None
        self._legacy_chat_cursor: tuple[str, int, str] | None = None
        self._legacy_chat_scan_complete = False
        self._legacy_chat_vault = None
        self._legacy_chat_retry: dict[str, tuple[str, str, dict, int]] = {}
        self._legacy_chat_next_scan_at = 0.0
        self._recovery_blocked: dict[str, tuple[str, int, int]] = {}
        self._recovery_cursor: str | None = None
        self._serial = asyncio.Lock()

    async def start(self):
        if self.task is not None:
            return
        await bootstrap_scratch_project(self.supervisor.store)
        # diagnostic-expected: the owned loop records operation failures and is cancelled and joined on close.
        self.task = asyncio.create_task(self._run(), name="nebula-first-admission")

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _run(self):
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                record_caught_exception(
                    "runtime",
                    "runtime.first_admission.waiting",
                    "New project work remains queued while admission is unavailable.",
                    error,
                    stage="admission",
                )
            await asyncio.sleep(0.5)

    async def reconcile(self):
        async with self._serial:
            supervisor = self.supervisor
            await self._recover_legacy_chat_starts()
            async with supervisor.coordinator.database.session() as session:
                host = await session.get(DeploymentHostRow, "host")
                if host is None or host.shutdown_requested:
                    return
                policy = await session.get(MemoryBudgetPolicyRow, "host")
                now_ms = await supervisor.coordinator.coordination._clock_ms(session)
                signature = (host.revision, policy.revision if policy else None)
                version = host.target_version or self.default_version
                promotion = await session.scalar(
                    select(DeploymentReceiptRow)
                    .where(
                        DeploymentReceiptRow.action == "api.promoted",
                    )
                    .order_by(
                        DeploymentReceiptRow.result["revision"]
                        .as_integer()
                        .desc()
                        .nullslast(),
                        DeploymentReceiptRow.occurred_at_ms.desc(),
                        DeploymentReceiptRow.id.desc(),
                    )
                    .limit(1)
                )
                api_version = (
                    promotion.result.get("version", self.default_version)
                    if promotion
                    else self.default_version
                )
                open_chat = (
                    select(EntityRow.id)
                    .where(
                        EntityRow.kind == "chat_sessions",
                        EntityRow.engagement_id == ProjectOwnershipRow.project_id,
                        EntityRow.payload["metadata"]["archived_at"]
                        .as_string()
                        .is_(None),
                    )
                    .exists()
                )
                untracked_open_projects = list(
                    await session.scalars(
                        select(EntityRow.engagement_id)
                        .where(
                            EntityRow.kind == "chat_sessions",
                            EntityRow.engagement_id.is_not(None),
                            EntityRow.payload["metadata"]["archived_at"]
                            .as_string()
                            .is_(None),
                            ~select(ProjectOwnershipRow.project_id)
                            .where(
                                ProjectOwnershipRow.project_id
                                == EntityRow.engagement_id
                            )
                            .exists(),
                        )
                        .distinct()
                        .limit(16)
                    )
                )
                # Some Core-owned paths create a chat session without first
                # passing through RuntimeRouting. Give only that session's
                # exact project a fencing counter so the normal open-chat
                # demand query below can admit it.
                for project_id in untracked_open_projects:
                    await supervisor.coordinator.coordination._ensure_project(
                        session, project_id
                    )
                pending_chat_start = (
                    select(RuntimeCommandRow.id)
                    .where(
                        RuntimeCommandRow.project_id == ProjectOwnershipRow.project_id,
                        RuntimeCommandRow.chat_start.is_(True),
                        RuntimeCommandRow.state.in_(("queued", "running", "uncertain")),
                    )
                    .exists()
                )
                legacy_chat_start = (
                    select(RuntimeCommandRow.id)
                    .where(
                        RuntimeCommandRow.project_id == ProjectOwnershipRow.project_id,
                        RuntimeCommandRow.name.in_(
                            ("chat.accept/v1", "chat.resume/v1")
                        ),
                        RuntimeCommandRow.state.in_(("queued", "running", "uncertain")),
                    )
                    .exists()
                )
                # A rename, archive, delete or any other project-routed write
                # waits for the project's worker even without an open chat.
                queued_work = project_worker_commands(ProjectOwnershipRow.project_id)
                # Retiring a worker while its project command runs would only
                # record a blocker every pass; wait for the command to finish.
                active_work = project_worker_commands(
                    ProjectOwnershipRow.project_id, ("queued", "running")
                )
                demands = list(
                    await session.execute(
                        select(ProjectOwnershipRow, queued_work)
                        # A verified quiescence clears the owner while retaining
                        # its fencing history. Reopening may claim the next fence;
                        # expired or uncertain owners still have a runtime ID.
                        .where(
                            ProjectOwnershipRow.runtime_id.is_(None),
                            # A held project refuses the grant; preparing a
                            # worker for it would only leave one unassigned.
                            ProjectOwnershipRow.retirement_requested.is_(False),
                            or_(
                                open_chat,
                                pending_chat_start,
                                legacy_chat_start,
                                queued_work,
                            ),
                        )
                        .limit(16)
                    )
                )
                idle_query = select(
                    ProjectOwnershipRow.project_id,
                    ProjectOwnershipRow.runtime_id,
                ).where(
                    ProjectOwnershipRow.runtime_id.is_not(None),
                    ProjectOwnershipRow.lease_expires_at_ms > now_ms,
                    ~open_chat,
                    ~pending_chat_start,
                    ~legacy_chat_start,
                    ~active_work,
                )
                if self._retirement_cursor is not None:
                    idle_query = idle_query.where(
                        ProjectOwnershipRow.project_id > self._retirement_cursor
                    )
                idle_owners = list(
                    await session.execute(
                        idle_query.order_by(ProjectOwnershipRow.project_id).limit(8)
                    )
                )
                if not idle_owners and self._retirement_cursor is not None:
                    self._retirement_cursor = None
                    idle_owners = list(
                        await session.execute(
                            select(
                                ProjectOwnershipRow.project_id,
                                ProjectOwnershipRow.runtime_id,
                            )
                            .where(
                                ProjectOwnershipRow.runtime_id.is_not(None),
                                ProjectOwnershipRow.lease_expires_at_ms > now_ms,
                                ~open_chat,
                                ~pending_chat_start,
                                ~legacy_chat_start,
                                ~active_work,
                            )
                            .order_by(ProjectOwnershipRow.project_id)
                            .limit(8)
                        )
                    )
                self._retirement_cursor = idle_owners[-1][0] if idle_owners else None
                api_id = host.api_runtime_id
            if api_id is None and self._blocked.get("api") != signature:
                try:
                    worker = await supervisor.prepare_api(api_version)
                    current = await supervisor.coordinator.status()
                    await supervisor.promote_api(
                        worker.runtime_id, expected_revision=current.revision
                    )
                    self._blocked.pop("api", None)
                except Exception as error:
                    self._blocked["api"] = signature
                    record_caught_exception(
                        "runtime",
                        "runtime.api_admission.waiting",
                        "Host recovery remains available while API admission is held.",
                        error,
                        stage="admission",
                    )
                return
            await self._recover_expired()
            runtime_host = getattr(supervisor, "host", None)
            if runtime_host is not None:
                reconcile_retirements = getattr(
                    runtime_host, "reconcile_idle_project_retirements", None
                )
                if reconcile_retirements is not None:
                    await reconcile_retirements()
                retire_idle = getattr(runtime_host, "retire_idle_project", None)
                if retire_idle is not None:
                    for project_id, _runtime_id in idle_owners:
                        try:
                            await retire_idle(project_id)
                        except Exception as error:
                            record_caught_exception(
                                "runtime",
                                "runtime.project_retirement.waiting",
                                "An execution worker remains assigned until the project has no chat or work blockers.",
                                error,
                                stage="admission",
                                metadata={
                                    "entity_id": project_id,
                                    "kind": "project",
                                },
                            )
            for demand, has_project_work in demands:
                if self._blocked.get(demand.project_id) == signature:
                    continue
                try:
                    project = await supervisor.store.get(Engagement, demand.project_id)
                    if project.status == "archived" and not has_project_work:
                        # An archived project's chats keep no worker, but an
                        # explicit request for it (unarchive or delete) does.
                        continue
                    runtime_id = self._pending.get(project.id)
                    if runtime_id is not None:
                        async with supervisor.coordinator.database.session() as session:
                            pending = await session.get(RuntimeProcessRow, runtime_id)
                        if pending is None or pending.state not in (
                            "reserved",
                            "starting",
                            "ready",
                        ):
                            # A quiesced, aborted or retired process never
                            # takes this project; prepare a fresh one.
                            self._pending.pop(project.id, None)
                            runtime_id = None
                    if runtime_id is None:
                        # Unknown unassigned processes may be manually prepared
                        # targets or a lost spawn response. Never steal/recreate
                        # them; require explicit reconciliation instead.
                        async with supervisor.coordinator.database.session() as session:
                            reserved = list(
                                await session.scalars(
                                    select(RuntimeProcessRow.runtime_id).where(
                                        RuntimeProcessRow.role == "execution",
                                        RuntimeProcessRow.state.in_(
                                            ("reserved", "starting", "ready")
                                        ),
                                        ~RuntimeProcessRow.runtime_id.in_(
                                            select(
                                                ProjectOwnershipRow.runtime_id
                                            ).where(
                                                ProjectOwnershipRow.runtime_id.is_not(
                                                    None
                                                )
                                            )
                                        ),
                                    )
                                )
                            )
                        if reserved:
                            continue
                        worker = await supervisor.prepare_execution(version)
                        runtime_id = worker.runtime_id
                        self._pending[project.id] = runtime_id
                    await supervisor.attach_execution(project.id, runtime_id)
                    self._pending.pop(project.id, None)
                    self._blocked.pop(project.id, None)
                except Exception as error:
                    self._blocked[demand.project_id] = signature
                    record_caught_exception(
                        "runtime",
                        "runtime.project_admission.waiting",
                        "This project's first work remains queued until admission succeeds.",
                        error,
                        stage="admission",
                    )

    @staticmethod
    def _recovery_operation_id(owner: dict, stage: str) -> str:
        digest = hashlib.sha256(
            json.dumps([owner, stage], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return "auto-recovery:" + digest

    async def _has_unassigned_execution(self) -> bool:
        async with self.supervisor.coordinator.database.session() as session:
            return (
                await session.scalar(
                    select(RuntimeProcessRow.runtime_id)
                    .where(
                        RuntimeProcessRow.role == "execution",
                        RuntimeProcessRow.state.in_(("reserved", "starting", "ready")),
                        ~RuntimeProcessRow.runtime_id.in_(
                            select(ProjectOwnershipRow.runtime_id).where(
                                ProjectOwnershipRow.runtime_id.is_not(None)
                            )
                        ),
                    )
                    .limit(1)
                )
                is not None
            )

    async def _recovery_operation(self, owner: dict, action: str, parameters: dict):
        management = self.supervisor.management
        operation_id = self._recovery_operation_id(owner, action)
        ledger = management.ledger()
        async with self.supervisor.coordinator.database.session() as session:
            prior = await session.get(DeploymentOperationRow, operation_id)
        retry_refused = False
        if prior is not None and prior.state == "failed":
            # A refused attempt proved it had no effect, so the same
            # deterministic operation is asked again, but not on every tick.
            async with self.supervisor.coordinator.database.session() as session:
                now_ms = await self.supervisor.coordinator.coordination._clock_ms(
                    session
                )
            if now_ms - prior.updated_at_ms < REFUSED_RECOVERY_RETRY_MS:
                detail = (prior.result or {}).get("detail", "refused")
                raise CoordinationConflict(
                    f"Automatic recovery operation {operation_id} was refused "
                    f"({detail}); it is asked again shortly"
                )
            retry_refused = True
        if prior is not None and not retry_refused:
            # Saved parameters and results are authoritative, including an
            # unknown spawn outcome after a host restart.
            receipt = await ledger.status(operation_id, actor_id=_RECOVERY_ACTOR)
        else:
            if action == "prepare_execution" and await self._has_unassigned_execution():
                raise CoordinationConflict(
                    "An unassigned execution process needs explicit reconciliation before automatic recovery"
                )

            async def dispatch(name, values):
                return await management.dispatch(
                    name, values, operation_id, _RECOVERY_ACTOR
                )

            receipt = await ledger.execute(
                operation_id=operation_id,
                actor_id=_RECOVERY_ACTOR,
                action=action,
                parameters=parameters,
                dispatch=dispatch,
                retry_rejected=True,
            )
        if receipt["state"] != "succeeded":
            raise CoordinationConflict(
                f"Automatic recovery operation {operation_id} is {receipt['state']}; "
                "read its saved deployment result before reconciliation"
            )
        return receipt["result"]

    async def _recover_legacy_chat_starts(self):
        """Promote queued legacy HTTP requests after authenticating their intent."""
        host = getattr(self.supervisor, "host", None)
        data_root = getattr(host, "data_root", None)
        if data_root is None:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._legacy_chat_scan_complete and not self._legacy_chat_retry:
            if now < self._legacy_chat_next_scan_at:
                return
            # New API workers persist chat_start on acceptance. This bounded
            # rescan also catches a legacy ingress row committed after startup.
            self._legacy_chat_cursor = None
            self._legacy_chat_scan_complete = False
        from .runtime_chat_acceptance import HTTP_INVOKE_COMMAND
        from .runtime_delivery import HttpIntent, RuntimeDelivery
        from .runtime_vault import RuntimeVault

        vault_root = Path(data_root) / "runtime" / "payloads"
        if not (vault_root / "key").is_file():
            return
        if self._legacy_chat_vault is None:
            self._legacy_chat_vault = RuntimeVault(vault_root)
        vault = self._legacy_chat_vault
        candidate_by_id: dict[str, tuple[str, str, dict, int]] = {}
        scan_exhausted = False
        async with self.supervisor.coordinator.database.session() as session:
            if self._legacy_chat_retry:
                retry_rows = await session.scalars(
                    select(RuntimeCommandRow).where(
                        RuntimeCommandRow.id.in_(list(self._legacy_chat_retry))
                    )
                )
                current_retries = {row.id: row for row in retry_rows}
                for command_id, prior in list(self._legacy_chat_retry.items()):
                    row = current_retries.get(command_id)
                    if (
                        row is None
                        or row.name != HTTP_INVOKE_COMMAND
                        or row.state != "queued"
                        or row.chat_start
                        or (row.project_id, row.actor_id, row.payload)
                        != (prior[0], prior[1], prior[2])
                    ):
                        self._legacy_chat_retry.pop(command_id, None)
                        continue
                    candidate_by_id[command_id] = (
                        row.project_id,
                        row.actor_id,
                        row.payload,
                        row.sequence,
                    )

            query = select(
                RuntimeCommandRow.id,
                RuntimeCommandRow.project_id,
                RuntimeCommandRow.actor_id,
                RuntimeCommandRow.payload,
                RuntimeCommandRow.sequence,
            ).where(
                RuntimeCommandRow.name == HTTP_INVOKE_COMMAND,
                RuntimeCommandRow.state == "queued",
                RuntimeCommandRow.chat_start.is_(False),
            )
            if self._legacy_chat_cursor is not None:
                project_id, sequence, command_id = self._legacy_chat_cursor
                query = query.where(
                    or_(
                        RuntimeCommandRow.project_id > project_id,
                        and_(
                            RuntimeCommandRow.project_id == project_id,
                            RuntimeCommandRow.sequence > sequence,
                        ),
                        and_(
                            RuntimeCommandRow.project_id == project_id,
                            RuntimeCommandRow.sequence == sequence,
                            RuntimeCommandRow.id > command_id,
                        ),
                    )
                )
            candidates = list(
                await session.execute(
                    query.order_by(
                        RuntimeCommandRow.project_id,
                        RuntimeCommandRow.sequence,
                        RuntimeCommandRow.id,
                    ).limit(16)
                )
            )
            if candidates:
                self._legacy_chat_cursor = (
                    candidates[-1].project_id,
                    candidates[-1].sequence,
                    candidates[-1].id,
                )
            else:
                scan_exhausted = True
            for row in candidates:
                candidate_by_id[row.id] = (
                    row.project_id,
                    row.actor_id,
                    row.payload,
                    row.sequence,
                )

        for command_id, (project_id, actor_id, payload, _sequence) in list(
            candidate_by_id.items()
        ):
            try:
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"format", "request_reference"}
                    or payload.get("format") != 1
                    or not isinstance(payload.get("request_reference"), str)
                ):
                    continue
                raw = await asyncio.to_thread(
                    vault.get,
                    payload["request_reference"],
                    purpose=RuntimeDelivery._intent_purpose(actor_id),
                )
                intent = HttpIntent.model_validate_json(raw)
                if (
                    not RuntimeDelivery.is_chat_start_intent(intent)
                    or intent.path != "/api/v1/chat/goal-conversations"
                ):
                    self._legacy_chat_retry.pop(command_id, None)
                    continue
                request_body = json.loads(intent.body())
                if request_body.get("engagement_id") != project_id:
                    self._legacy_chat_retry.pop(command_id, None)
                    continue
                marked = await self.supervisor.coordinator.coordination.mark_queued_chat_start(
                    project_id=project_id,
                    command_id=command_id,
                    actor_id=actor_id,
                    payload=payload,
                )
                if marked:
                    self._legacy_chat_retry.pop(command_id, None)
                else:
                    self._legacy_chat_retry[command_id] = (
                        project_id,
                        actor_id,
                        payload,
                        _sequence,
                    )
            except Exception as error:
                self._legacy_chat_retry[command_id] = (
                    project_id,
                    actor_id,
                    payload,
                    _sequence,
                )
                record_caught_exception(
                    "runtime",
                    "runtime.legacy_chat_start.unavailable",
                    "A legacy queued command remains held because its chat intent "
                    "could not be verified.",
                    error,
                    stage="admission",
                    metadata={"entity_id": command_id, "kind": "runtime-command"},
                )
        if scan_exhausted and not self._legacy_chat_retry:
            self._legacy_chat_scan_complete = True
            self._legacy_chat_next_scan_at = loop.time() + 30.0

    async def _recover_expired(self):
        supervisor = self.supervisor
        async with supervisor.coordinator.database.session() as session:
            host = await session.get(DeploymentHostRow, "host")
            if host is None or host.shutdown_requested:
                return
            target_version = host.target_version
            now = await supervisor.coordinator.coordination._clock_ms(session)
            open_chat = (
                select(EntityRow.id)
                .where(
                    EntityRow.kind == "chat_sessions",
                    EntityRow.engagement_id == ProjectOwnershipRow.project_id,
                    EntityRow.payload["metadata"]["archived_at"].as_string().is_(None),
                )
                .exists()
            )
            pending_chat_start = (
                select(RuntimeCommandRow.id)
                .where(
                    RuntimeCommandRow.project_id == ProjectOwnershipRow.project_id,
                    RuntimeCommandRow.chat_start.is_(True),
                    RuntimeCommandRow.state.in_(("queued", "running", "uncertain")),
                )
                .exists()
            )
            legacy_chat_start = (
                select(RuntimeCommandRow.id)
                .where(
                    RuntimeCommandRow.project_id == ProjectOwnershipRow.project_id,
                    RuntimeCommandRow.name.in_(("chat.accept/v1", "chat.resume/v1")),
                    RuntimeCommandRow.state.in_(("queued", "running", "uncertain")),
                )
                .exists()
            )
            query = select(ProjectOwnershipRow).where(
                ProjectOwnershipRow.runtime_id.is_not(None),
                ProjectOwnershipRow.lease_expires_at_ms <= now,
                or_(open_chat, pending_chat_start, legacy_chat_start),
            )
            if self._recovery_cursor is not None:
                query = query.where(
                    ProjectOwnershipRow.project_id > self._recovery_cursor
                )
            owners = list(
                await session.scalars(
                    query.order_by(ProjectOwnershipRow.project_id).limit(16)
                )
            )
        # This cursor only schedules bounded scans; operation receipts remain
        # the authority across controller or host restarts.
        self._recovery_cursor = owners[-1].project_id if owners else None
        if not owners:
            return
        # Older embedders without a process host cannot prove OS absence.
        if (
            getattr(supervisor, "host", None) is None
            or getattr(supervisor, "management", None) is None
        ):
            return
        observed = {
            worker.runtime_id: worker
            for worker in await supervisor.host.rediscover()
            if worker.process_state == "gone" and worker.identity is not None
        }
        for owner in owners:
            worker = observed.get(owner.runtime_id)
            if worker is None:
                continue
            blocked = self._recovery_blocked.get(owner.project_id)
            if blocked and blocked[:2] == (owner.runtime_id, owner.fencing_token):
                if blocked[2] > now:
                    continue
            try:
                project = await supervisor.store.get(Engagement, owner.project_id)
                if project.status == "archived":
                    continue
                expected_owner = {
                    "project_id": owner.project_id,
                    "runtime_id": owner.runtime_id,
                    "fencing_token": owner.fencing_token,
                }
                async with supervisor.coordinator.database.session() as session:
                    existing = await session.scalar(
                        select(RuntimeRecoveryRow.operation_id)
                        .where(
                            RuntimeRecoveryRow.project_id == owner.project_id,
                            RuntimeRecoveryRow.source_runtime_id == owner.runtime_id,
                            RuntimeRecoveryRow.source_fencing_token
                            == owner.fencing_token,
                            RuntimeRecoveryRow.phase.in_(
                                ("held", "prepared", "verified")
                            ),
                        )
                        .limit(1)
                    )
                    attempts = await session.scalar(
                        select(func.count())
                        .select_from(RuntimeRecoveryRow)
                        .where(
                            RuntimeRecoveryRow.project_id == owner.project_id,
                            RuntimeRecoveryRow.created_at_ms
                            >= now - _RECOVERY_WINDOW_MS,
                        )
                    )
                if existing is not None:
                    continue
                if attempts >= _RECOVERY_LIMIT:
                    raise CoordinationConflict(
                        "Automatic recovery is held after three recoveries in ten minutes; inspect this project's runtime failures"
                    )
                await self._recovery_operation(
                    expected_owner,
                    "reconcile_project",
                    {"expected_owner": expected_owner},
                )
                prepared = await self._recovery_operation(
                    expected_owner,
                    "prepare_execution",
                    {"version": target_version or worker.identity.version},
                )
                target_runtime_id = prepared.get("runtime_id")
                if not isinstance(target_runtime_id, str) or not target_runtime_id:
                    raise CoordinationConflict(
                        "The saved recovery preparation has no confirmed runtime identity"
                    )
                async with supervisor.coordinator.database.session() as session:
                    handoff = await session.get(ProjectHandoffRow, owner.project_id)
                    revision = handoff.revision if handoff else None
                await self._recovery_operation(
                    expected_owner,
                    "recover_project",
                    {
                        "project_id": owner.project_id,
                        "target_runtime_id": target_runtime_id,
                        "expected_owner": expected_owner,
                        "expected_handoff_revision": revision,
                    },
                )
                self._recovery_blocked.pop(owner.project_id, None)
            except Exception as error:
                self._recovery_blocked[owner.project_id] = (
                    owner.runtime_id,
                    owner.fencing_token,
                    now + _RECOVERY_RETRY_MS,
                )
                record_caught_exception(
                    "runtime",
                    "runtime.project_recovery.waiting",
                    "This project remains held until its expired runtime can be safely recovered.",
                    error,
                    stage="recovery",
                )
