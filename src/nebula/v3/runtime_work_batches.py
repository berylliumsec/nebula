"""Host-owned, atomic SQL Work batches with explicit project write fences."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from .api_crud import _resolve_engagement_workspace_path
from .async_utils import finish_cleanup
from .database import (
    DeploymentHostRow,
    EntityRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeDeliveryRow,
    RuntimeProcessRow,
    RuntimeReleaseRow,
    RuntimeWorkBatchProjectRow,
)
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT, ReleaseManifest
from .diagnostics import record_caught_exception
from .domain import Engagement, WorkItem, WorkUpdate
from .runtime_coordination import (
    CommandReceipt,
    CoordinationConflict,
    LeaseLost,
    ProjectLease,
)
from .runtime_delivery import MAX_FRAME_BYTES, HttpIntent, _json
from .runtime_global_api import GlobalSqlStore
from .runtime_ipc import RuntimeIpcError
from .setup import create_engagement_with_default_scope
from .storage import ConflictError, NotFoundError, StoreTransaction
from .work import (
    WorkCheckIn,
    WorkCreate,
    WorkImportBatch,
    WorkImportItemResult,
    WorkImportProjectResult,
    WorkImportResult,
    WorkProjectBatchUpdate,
    WorkService,
)

WORK_BATCH_FEATURE = "project.work-batch/v1"
WORK_BATCH_COMMAND = "global.work-batch/v1"
WORK_BATCH_BARRIER = "work.batch.barrier/v1"
WORK_BATCH_CAPABILITY = "host.work-batches.capability/v1"
_ACTIVE = ("queued", "running", "uncertain")


class _Waiting(Exception):
    """No final SQL effects committed; authoritative readiness may change."""


class _InvalidBatch(Exception):
    """Accepted input cannot construct a domain record; no SQL effects commit."""


@dataclass(frozen=True)
class _Fence:
    project_id: str
    runtime_id: str | None
    token: int


def _import_id(source, external_id):
    identity = json.dumps([source, external_id], separators=(",", ":"))
    return str(uuid5(NAMESPACE_URL, f"nebula:work-import-project:{identity}"))


def _ids(body):
    return sorted(
        set(
            body.project_ids
            if isinstance(body, WorkProjectBatchUpdate)
            else [
                project.engagement_id or _import_id(body.source, project.external_id)
                for project in body.projects
            ]
        )
    )


def _parse(intent):
    try:
        value = json.loads(intent.body())
        model = WorkImportBatch if intent.method == "POST" else WorkProjectBatchUpdate
        body = model.model_validate(value)
    except (ValueError, RecursionError):
        raise HTTPException(
            422, "The Work batch does not match its bounded request schema"
        ) from None
    if any(not identity or len(identity) > 200 for identity in _ids(body)):
        raise HTTPException(422, "Work project IDs require 1 to 200 characters")
    return body


class _ProjectTransaction(StoreTransaction):
    def __init__(self, session, project_id):
        super().__init__(session)
        self.project_id = project_id

    async def add(self, entity):
        if (
            not isinstance(entity, (WorkItem, WorkUpdate))
            or entity.engagement_id != self.project_id
        ):
            raise LeaseLost("A Work batch cannot write another project's records")
        return await super().add(entity)

    async def update(self, model, entity_id, changes, *, expected_revision=None):
        row = await self.session.get(EntityRow, entity_id)
        if (
            model not in (WorkItem, WorkUpdate)
            or row is None
            or row.engagement_id != self.project_id
        ):
            raise LeaseLost("A Work batch cannot update another project's records")
        return await super().update(
            model, entity_id, changes, expected_revision=expected_revision
        )


class _ProjectStore(GlobalSqlStore):
    """The import's Work service cannot borrow the coordinator's global scope."""

    def __init__(self, database, project_id):
        super().__init__(database)
        self.project_id = project_id

    async def get(self, model, entity_id):
        entity = await super().get(model, entity_id)
        owner = (
            entity.id
            if isinstance(entity, Engagement)
            else getattr(entity, "engagement_id", None)
        )
        if owner != self.project_id:
            raise LeaseLost("A Work batch cannot read another project's Work records")
        return entity

    @asynccontextmanager
    async def transaction(self, *, immediate=False):
        session = self.database.task_session()
        if session is None:
            raise RuntimeError(
                "Project Work writes require the verified batch transaction"
            )
        yield _ProjectTransaction(session, self.project_id)


class _MetadataTransaction(StoreTransaction):
    def __init__(self, session, projects):
        super().__init__(session)
        self.projects = projects

    async def update(self, model, entity_id, changes, *, expected_revision=None):
        if (
            model is not Engagement
            or entity_id not in self.projects
            or set(changes) - {"parent_engagement_id", "work_enabled"}
            or expected_revision is None
        ):
            raise LeaseLost("The Work batch only owns its declared metadata edits")
        return await super().update(
            model, entity_id, changes, expected_revision=expected_revision
        )


class _MetadataStore(GlobalSqlStore):
    def __init__(self, database, projects):
        super().__init__(database)
        self.projects = projects

    @asynccontextmanager
    async def transaction(self, *, immediate=False):
        session = self.database.task_session()
        if session is None:
            raise RuntimeError("Metadata edits require the verified batch transaction")
        yield _MetadataTransaction(session, self.projects)


class RuntimeWorkBatches:
    def __init__(
        self,
        delivery,
        *,
        host=None,
        availability: Callable[[], Awaitable[dict]] | None = None,
    ):
        self.delivery, self.coordination, self.host = (
            delivery,
            delivery.coordination,
            host,
        )
        self.store = GlobalSqlStore(self.coordination.database)
        self.availability = availability
        self.task = None
        self.closed = False
        self._stop = asyncio.Event()
        self._serial = asyncio.Lock()
        self._cursor = 0

    @staticmethod
    def supports(scope):
        return (scope.get("method"), scope.get("path")) in {
            ("POST", "/api/v1/work/import"),
            ("PATCH", "/api/v1/work/projects"),
        }

    def capability(self):
        ready = bool(
            self.host is not None
            and self.host.lock.descriptor is not None
            and not self.host.closed
            and not self.closed
            and self.task is not None
            and not self.task.done()
        )
        return {
            "protocol": "nebula.work-batches/v1",
            "feature": WORK_BATCH_FEATURE,
            "ready": ready,
        }

    async def _available(self):
        if self.host is not None:
            value = self.capability()
        elif self.availability is not None:
            try:
                value = await self.availability()
            except (RuntimeIpcError, OSError, TimeoutError) as error:
                record_caught_exception(
                    "runtime",
                    "work.batch_capability_unavailable",
                    "Work batch coordination is unavailable; existing project work continues.",
                    error,
                    stage="admission",
                )
                value = None
        else:
            value = None
        if value != {
            "protocol": "nebula.work-batches/v1",
            "feature": WORK_BATCH_FEATURE,
            "ready": True,
        }:
            raise HTTPException(
                503,
                "Reconnect to a Core host supporting coordinated Work batches before importing or editing multiple projects",
            )

    async def _locks(self, session):
        if session.bind.dialect.name == "postgresql":
            # Scoped execution connections acquire this lock at BEGIN. Use the
            # same order before the global sequence allocator to avoid a cycle.
            await session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtext('nebula:execution-sql:v1'))"
                )
            )
            await session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('nebula:global-sql:v1'))"
                )
            )

    async def _existing(self, session, actor, key, digest):
        row = await session.scalar(
            select(RuntimeCommandRow).where(
                RuntimeCommandRow.actor_id == actor,
                RuntimeCommandRow.idempotency_key == key,
            )
        )
        if row is not None:
            if row.name != WORK_BATCH_COMMAND or row.payload_hash != digest:
                raise CoordinationConflict(
                    "Idempotency-Key was used for different request details"
                )
            return CommandReceipt(
                row.id, row.project_id, row.sequence, row.accepted_at_ms
            )

    async def accept(self, intent, *, actor_id, idempotency_key):
        try:
            return await self._accept(
                intent, actor_id=actor_id, idempotency_key=idempotency_key
            )
        except IntegrityError as error:
            # diagnostic-expected: a competing ordinary project submission can
            # win the same actor/key; the whole new acceptance rolled back.
            raise CoordinationConflict(
                "Idempotency-Key conflicts with another accepted request"
            ) from error

    async def _accept(self, intent, *, actor_id, idempotency_key):
        if not self.supports({"method": intent.method, "path": intent.path}):
            raise HTTPException(404, "This route is not a coordinated Work batch")
        reference = await asyncio.to_thread(
            self.delivery.vault.put,
            _json(intent.model_dump(mode="json")),
            purpose=self.delivery._intent_purpose(actor_id),
        )
        original = {"format": 1, "request_reference": reference}
        digest = sha256(
            _json(
                {
                    "project_id": GLOBAL_EXECUTION_PROJECT,
                    "name": WORK_BATCH_COMMAND,
                    "payload": original,
                    "expected_revision": None,
                }
            )
        ).hexdigest()
        async with self.coordination.database.session() as session:
            existing = await self._existing(session, actor_id, idempotency_key, digest)
        if existing is not None:
            return existing
        await self._available()
        failure = None
        body = None
        normalized = None
        try:
            body = _parse(intent)
            if isinstance(body, WorkImportBatch):
                if any(
                    project.workspace_path is not None
                    and not project.workspace_path.strip()
                    for project in body.projects
                ):
                    raise HTTPException(
                        422, "project workspace folder must not be blank"
                    )
                body = body.model_copy(
                    update={
                        "projects": [
                            project.model_copy(
                                update={
                                    "workspace_path": _resolve_engagement_workspace_path(
                                        project.workspace_path
                                    )
                                }
                            )
                            if project.workspace_path is not None
                            else project
                            for project in body.projects
                        ]
                    }
                )
            normalized = await asyncio.to_thread(
                self.delivery.vault.put,
                _json(body.model_dump(mode="json")),
                purpose="work-batch-plan:" + actor_id,
            )
        except HTTPException as error:
            # diagnostic-expected: invalid saved input gets its ordinary durable
            # error response, without creating project participants or effects.
            failure = (error.status_code, str(error.detail))
        async with self.coordination._write() as session:
            await self._locks(session)
            existing = await self._existing(session, actor_id, idempotency_key, digest)
            if existing is not None:
                return existing
            host = await session.get(
                DeploymentHostRow, "host", with_for_update={"read": True}
            )
            if host is not None and host.shutdown_requested:
                raise CoordinationConflict(
                    "Core shutdown is in progress; new Work batches are not accepted"
                )
            if failure is None:
                try:
                    await self._validate(body)
                except (NotFoundError, ConflictError) as error:
                    # diagnostic-expected: every reference is checked before any
                    # project write and failures are saved under the same key.
                    failure = (
                        404 if isinstance(error, NotFoundError) else 409,
                        str(error),
                    )
            sequence = (
                await session.scalar(
                    select(func.max(RuntimeCommandRow.sequence)).where(
                        RuntimeCommandRow.project_id == GLOBAL_EXECUTION_PROJECT
                    )
                )
                or 0
            ) + 1
            now = await self.coordination._clock_ms(session)
            command = RuntimeCommandRow(
                id=str(uuid4()),
                project_id=GLOBAL_EXECUTION_PROJECT,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                sequence=sequence,
                name=WORK_BATCH_COMMAND,
                lane="work",
                payload={**original, "normalized_reference": normalized},
                payload_hash=digest,
                accepted_at_ms=now,
                state="queued",
            )
            session.add(command)
            await session.flush()
            if failure is not None:
                await self._frames(session, command, failure[0], {"detail": failure[1]})
            else:
                for project_id in _ids(body):
                    session.add(
                        RuntimeWorkBatchProjectRow(
                            batch_id=command.id, project_id=project_id, state="waiting"
                        )
                    )
            return CommandReceipt(command.id, command.project_id, sequence, now)

    async def _validate(self, body):
        """Validate the complete graph before any project or item write."""
        proposed = {}
        if isinstance(body, WorkProjectBatchUpdate):
            for identity in body.project_ids:
                project = await self.store.get(Engagement, identity)
                proposed[identity] = (
                    body.parent_engagement_id or project.parent_engagement_id
                )
        else:
            for entry in body.projects:
                identity = entry.engagement_id or _import_id(
                    body.source, entry.external_id
                )
                try:
                    project = await self.store.get(Engagement, identity)
                except NotFoundError:
                    if entry.engagement_id is not None:
                        raise
                    proposed[identity] = entry.parent_engagement_id
                else:
                    if entry.engagement_id is None and project.metadata.get(
                        "work_import"
                    ) != {"source": body.source, "external_id": entry.external_id}:
                        raise ConflictError(
                            "import project ID belongs to a different source"
                        )
                    proposed[identity] = project.parent_engagement_id
        for identity, parent in proposed.items():
            visited = {identity}
            while parent is not None:
                if parent in visited:
                    raise ConflictError("project hierarchy would contain a cycle")
                visited.add(parent)
                if len(visited) > 10_000:
                    raise ConflictError(
                        "project hierarchy exceeds the bounded batch validation limit"
                    )
                parent = (
                    proposed[parent]
                    if parent in proposed
                    else (await self.store.get(Engagement, parent)).parent_engagement_id
                )

    async def start(self):
        if (
            self.host is None
            or self.host.closed
            or self.host.lock.descriptor is None
            or self.host.socket_root is None
        ):
            raise RuntimeError("Work batch progress requires exclusive host ownership")
        if self.host.coordinator.coordination is not self.coordination:
            raise ValueError(
                "Work batch progress must use the canonical host coordinator"
            )
        if self.closed:
            raise RuntimeError("Create a new Work batch service after closing")
        if self.task is None:
            # diagnostic-expected: this exclusive-host task owns SQL-only batch
            # progress, records failures, and is stopped and joined by close.
            self.task = asyncio.create_task(self._run(), name="nebula-work-batches")

    async def _run(self):
        while not self._stop.is_set():
            try:
                await self.reconcile()
            except Exception as error:
                record_caught_exception(
                    "runtime",
                    "work.batch_progress_failed",
                    "Accepted Work batches remain saved while their SQL coordinator waits to retry.",
                    error,
                    stage="work-batches",
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.2)
            except TimeoutError:
                # diagnostic-expected: this bounded wake-up checks saved work;
                # close sets the event and joins the owned loop.
                pass

    async def close(self):
        self.closed = True
        self._stop.set()
        if self.task is not None:

            async def join():
                await self.task

            await finish_cleanup(join())
        await self.store.event_history.close()

    async def reconcile(self, *, limit=16):
        if self.host is None or self.host.lock.descriptor is None or self.host.closed:
            raise RuntimeError("Only the exclusive host advances Work batches")
        async with self._serial:
            async with self.coordination.database.session() as session:
                rows = list(
                    await session.scalars(
                        select(RuntimeCommandRow)
                        .where(
                            RuntimeCommandRow.name == WORK_BATCH_COMMAND,
                            RuntimeCommandRow.state == "queued",
                            RuntimeCommandRow.sequence > self._cursor,
                        )
                        .order_by(RuntimeCommandRow.sequence)
                        .limit(limit)
                    )
                )
                pending = [
                    (row.id, row.actor_id, row.payload, row.sequence) for row in rows
                ]
            self._cursor = pending[-1][3] if len(pending) == limit else 0
            for identity, actor, payload, _sequence in pending:
                raw = await asyncio.to_thread(
                    self.delivery.vault.get,
                    payload["normalized_reference"],
                    purpose="work-batch-plan:" + actor,
                )
                original = await asyncio.to_thread(
                    self.delivery.vault.get,
                    payload["request_reference"],
                    purpose=self.delivery._intent_purpose(actor),
                )
                intent = HttpIntent.model_validate_json(original)
                body = (
                    WorkImportBatch
                    if intent.method == "POST"
                    else WorkProjectBatchUpdate
                ).model_validate_json(raw)
                await self._advance(identity, actor, body)

    async def _held(self, session, project_ids):
        host = await session.get(
            DeploymentHostRow,
            "host",
            with_for_update={"read": True},
            populate_existing=True,
        )
        if host is not None and host.shutdown_requested:
            raise _Waiting("Core is holding new work for coordinated shutdown")
        handoffs = await session.scalars(
            select(ProjectHandoffRow).where(
                ProjectHandoffRow.project_id.in_(project_ids),
                ProjectHandoffRow.admission_held.is_(True),
                ProjectHandoffRow.phase != "verified",
            )
        )
        if next(iter(handoffs), None) is not None:
            raise _Waiting("A participating project is completing its fenced handoff")

    async def _fences(self, session, project_ids):
        """Acquire every write participant in canonical order, including NULL."""
        fences = []
        for identity in project_ids:
            exists = await session.get(EntityRow, identity)
            if exists is not None:
                if exists.kind != Engagement.entity_kind:
                    raise ConflictError("Work project ID belongs to another entity")
                # Existing global projects need a counter row before absence
                # can be locked. New projects get theirs only at final commit.
                await self._counter(session, identity)
            owner = await session.get(
                ProjectOwnershipRow,
                identity,
                with_for_update=True,
                populate_existing=True,
            )
            if owner is None or owner.runtime_id is None:
                fences.append(
                    _Fence(identity, None, owner.fencing_token if owner else 0)
                )
                continue
            process = await session.get(RuntimeProcessRow, owner.runtime_id)
            release = (
                await session.get(RuntimeReleaseRow, process.version)
                if process is not None
                else None
            )
            if (
                release is None
                or WORK_BATCH_FEATURE
                not in ReleaseManifest.model_validate(release.manifest).features
            ):
                raise _Waiting(
                    "A participating project needs a runtime supporting coordinated Work batches; existing work can continue"
                )
            fence = _Fence(identity, owner.runtime_id, owner.fencing_token)
            await self.coordination._check(
                session,
                ProjectLease(identity, fence.runtime_id, fence.token),
                lock=False,
            )
            fences.append(fence)
        return fences

    async def _counter(self, session, identity):
        """A metadata-only NULL counter is not a request to prepare execution."""
        exists = await session.scalar(
            select(EntityRow.id).where(
                EntityRow.id == identity,
                EntityRow.kind == Engagement.entity_kind,
            )
        )
        if exists is None:
            raise CoordinationConflict("project does not exist")
        await self.coordination._insert_absent(
            session,
            ProjectOwnershipRow,
            {
                "project_id": identity,
                "runtime_id": None,
                "fencing_token": 0,
                "lease_expires_at_ms": 0,
                "command_sequence": 0,
                "admission_requested": False,
            },
            ["project_id"],
        )

    async def _check_fences(self, session, fences):
        await self._held(session, [fence.project_id for fence in fences])
        for fence in fences:
            if fence.runtime_id is not None:
                await self.coordination._check(
                    session,
                    ProjectLease(fence.project_id, fence.runtime_id, fence.token),
                    lock=False,
                )
            else:
                owner = await session.get(
                    ProjectOwnershipRow,
                    fence.project_id,
                    populate_existing=True,
                )
                if owner is not None and (owner.runtime_id, owner.fencing_token) != (
                    None,
                    fence.token,
                ):
                    raise LeaseLost("The ownerless project's fencing counter changed")

    async def _participants(self, session, batch_id):
        return list(
            await session.scalars(
                select(RuntimeWorkBatchProjectRow)
                .where(RuntimeWorkBatchProjectRow.batch_id == batch_id)
                .order_by(RuntimeWorkBatchProjectRow.project_id)
                .with_for_update()
            )
        )

    async def _stage(self, batch_id, body):
        async with self.coordination._write() as session:
            await self._locks(session)
            parent = await session.get(
                RuntimeCommandRow, batch_id, with_for_update=True
            )
            if parent is None or parent.state != "queued":
                return False
            participants = await self._participants(session, batch_id)
            if all(participant.state == "barrier" for participant in participants):
                return True
            project_ids = [participant.project_id for participant in participants]
            await self._held(session, project_ids)
            # An earlier accepted batch may still be waiting for an older
            # owner. Later batches cannot silently pass it on a shared lane.
            earlier = await session.scalar(
                select(RuntimeWorkBatchProjectRow.batch_id)
                .join(
                    RuntimeCommandRow,
                    RuntimeCommandRow.id == RuntimeWorkBatchProjectRow.batch_id,
                )
                .where(
                    RuntimeWorkBatchProjectRow.project_id.in_(project_ids),
                    RuntimeWorkBatchProjectRow.state.in_(("waiting", "barrier")),
                    RuntimeCommandRow.sequence < parent.sequence,
                )
                .limit(1)
            )
            if earlier is not None:
                raise _Waiting("An earlier Work batch is waiting on a shared project")
            await self._validate(body)
            fences = await self._fences(session, project_ids)
            now = await self.coordination._clock_ms(session)
            for participant, fence in zip(participants, fences, strict=True):
                owner = await session.get(ProjectOwnershipRow, fence.project_id)
                if owner is not None:
                    owner.command_sequence += 1
                    sequence = owner.command_sequence
                else:
                    sequence = (
                        await session.scalar(
                            select(func.max(RuntimeCommandRow.sequence)).where(
                                RuntimeCommandRow.project_id == fence.project_id,
                            )
                        )
                        or 0
                    ) + 1
                payload = {"format": 1, "batch_id": batch_id}
                barrier_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"nebula:work-batch:{batch_id}:{fence.project_id}",
                    )
                )
                session.add(
                    RuntimeCommandRow(
                        id=barrier_id,
                        project_id=fence.project_id,
                        actor_id="work-batch:" + batch_id,
                        idempotency_key=fence.project_id,
                        sequence=sequence,
                        name=WORK_BATCH_BARRIER,
                        lane="work",
                        payload=payload,
                        payload_hash=sha256(_json(payload)).hexdigest(),
                        accepted_at_ms=now,
                        state="queued",
                    )
                )
                participant.state = "barrier"
                participant.barrier_command_id = barrier_id
                participant.barrier_sequence = sequence
                participant.runtime_id = fence.runtime_id
                participant.fencing_token = fence.token
            await session.flush()
            await self._check_fences(session, fences)
            return True

    async def _head(self, session, participant):
        head = await session.scalar(
            select(RuntimeCommandRow)
            .where(
                RuntimeCommandRow.project_id == participant.project_id,
                RuntimeCommandRow.lane == "work",
                RuntimeCommandRow.state.in_(_ACTIVE),
            )
            .order_by(RuntimeCommandRow.sequence)
            .limit(1)
        )
        if (
            head is None
            or head.id != participant.barrier_command_id
            or head.state != "queued"
        ):
            raise _Waiting("Earlier project work must finish before the Work batch")
        unknown = await session.scalar(
            select(RuntimeCommandRow.id)
            .where(
                RuntimeCommandRow.project_id == participant.project_id,
                RuntimeCommandRow.state.in_(("running", "uncertain")),
            )
            .limit(1)
        )
        if unknown is not None:
            raise _Waiting("A participating project has unfinished or uncertain work")

    async def _import(self, body):
        results = []
        for entry in body.projects:
            identity = entry.engagement_id or _import_id(body.source, entry.external_id)
            try:
                project = await self.store.get(Engagement, identity)
            except NotFoundError:
                # diagnostic-expected: a new source project is created inside
                # the same final SQL transaction as every item and receipt.
                project = await create_engagement_with_default_scope(
                    self.store,
                    Engagement(
                        id=identity,
                        name=entry.name,
                        description=entry.description,
                        status=entry.status,
                        parent_engagement_id=entry.parent_engagement_id,
                        workspace_path=entry.workspace_path,
                        metadata={
                            "work_import": {
                                "source": body.source,
                                "external_id": entry.external_id,
                            }
                        },
                    ),
                )
            scoped = _ProjectStore(self.store.database, identity)
            service = WorkService(scoped)
            item_results = []
            try:
                for item_entry in entry.items:
                    request_id = sha256(
                        json.dumps(
                            [entry.external_id, item_entry.external_id],
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    actor = "import:" + body.source
                    item = await service.create(
                        identity,
                        WorkCreate(
                            title=item_entry.title,
                            description=item_entry.description,
                            status=item_entry.status,
                            priority=item_entry.priority,
                            source_kind="import",
                            source_id=item_entry.external_id,
                            request_id=request_id,
                        ),
                        actor_id=actor,
                        notify=False,
                    )
                    update_id = None
                    if item_entry.update is not None:
                        update = await service.check_in(
                            identity,
                            item.id,
                            WorkCheckIn(
                                **item_entry.update.model_dump(),
                                request_id=request_id,
                            ),
                            actor_kind="import",
                            actor_id=actor,
                            notify=False,
                        )
                        update_id = update.id
                    item_results.append(
                        WorkImportItemResult(
                            external_id=item_entry.external_id,
                            item_id=item.id,
                            update_id=update_id,
                        )
                    )
            finally:
                await scoped.event_history.close()
            results.append(
                WorkImportProjectResult(
                    external_id=entry.external_id,
                    engagement_id=project.id,
                    items=item_results,
                )
            )
        return WorkImportResult(projects=results)

    async def _apply(self, body):
        if isinstance(body, WorkImportBatch):
            return await self._import(body)
        metadata = _MetadataStore(self.store.database, set(body.project_ids))
        try:
            return await WorkService(metadata).update_projects(body)
        finally:
            await metadata.event_history.close()

    async def _frames(self, session, parent, status, result):
        body = _json(result)
        frames = [
            (
                "head",
                _json(
                    {
                        "status": status,
                        "headers": [["content-type", "application/json"]],
                    }
                ),
            )
        ]
        frames.extend(
            ("body", body[start : start + MAX_FRAME_BYTES])
            for start in range(0, len(body), MAX_FRAME_BYTES)
        )
        frames.append(("end", b""))
        for sequence, (kind, content) in enumerate(frames, 1):
            reference = await asyncio.to_thread(
                self.delivery.vault.put,
                content,
                purpose=self.delivery._frame_purpose(parent.id, sequence),
            )
            session.add(
                RuntimeDeliveryRow(
                    id=str(uuid4()),
                    project_id=GLOBAL_EXECUTION_PROJECT,
                    command_id=parent.id,
                    sequence=sequence,
                    kind=kind,
                    content_reference=reference,
                )
            )
        parent.state = "succeeded" if status < 400 else "failed"
        parent.result = {"http_status": status}

    async def _finish(self, batch_id, actor, body):
        async with self.coordination._write() as session:
            await self._locks(session)
            parent = await session.get(
                RuntimeCommandRow, batch_id, with_for_update=True
            )
            if parent is None or parent.state != "queued":
                return
            participants = await self._participants(session, batch_id)
            await self._held(session, [part.project_id for part in participants])
            fences = await self._fences(
                session, [part.project_id for part in participants]
            )
            for participant in participants:
                await self._head(session, participant)
            await self._validate(body)
            if not await self.delivery.valid_actor(actor):
                raise HTTPException(
                    401, "The accepting device session expired or was revoked"
                )
            try:
                result = await self._apply(body)
            except ValidationError:
                # diagnostic-expected: an older accepted plan can pass the
                # import DTO but fail domain construction. Roll back the whole
                # final transaction before publishing a typed 422 response.
                raise _InvalidBatch(
                    "The saved Work batch does not match its project or Work record schema"
                ) from None
            # Newly imported metadata becomes eligible for ordinary admission
            # only with its complete default scope, Work and response frames.
            for participant in participants:
                await self._counter(session, participant.project_id)
                owner = await session.get(ProjectOwnershipRow, participant.project_id)
                owner.command_sequence = max(
                    owner.command_sequence, participant.barrier_sequence
                )
            await session.flush()
            await self._check_fences(session, fences)
            await self._frames(session, parent, 200, result.model_dump(mode="json"))
            for participant, fence in zip(participants, fences, strict=True):
                barrier = await session.get(
                    RuntimeCommandRow, participant.barrier_command_id
                )
                barrier.state = "succeeded"
                barrier.runtime_id, barrier.fencing_token = (
                    fence.runtime_id,
                    fence.token,
                )
                barrier.result = {"batch_id": batch_id}
                participant.state = "complete"
                participant.runtime_id, participant.fencing_token = (
                    fence.runtime_id,
                    fence.token,
                )
            await session.flush()
            await self._check_fences(session, fences)

    async def _failure(self, batch_id, status, detail):
        # The failed SQL transaction has already rolled back. Only this typed
        # coordinator can retire its never-dispatched barriers and publish the
        # durable failure. No project entity or unrelated command is changed.
        async with self.coordination._write() as session:
            await self._locks(session)
            parent = await session.get(
                RuntimeCommandRow, batch_id, with_for_update=True
            )
            if parent is None or parent.state != "queued":
                return
            participants = await self._participants(session, batch_id)
            for participant in participants:
                if participant.barrier_command_id is not None:
                    barrier = await session.get(
                        RuntimeCommandRow, participant.barrier_command_id
                    )
                    if barrier.state != "queued" or barrier.name != WORK_BATCH_BARRIER:
                        raise CoordinationConflict(
                            "Work batch barrier lost its coordinator ownership"
                        )
                    barrier.state = "failed"
                    barrier.result = {"batch_id": batch_id, "http_status": status}
                participant.state = "failed"
            await self._frames(session, parent, status, {"detail": detail})

    async def _advance(self, batch_id, actor, body):
        try:
            if await self._stage(batch_id, body):
                await self._finish(batch_id, actor, body)
        except (_Waiting, LeaseLost) as error:
            # diagnostic-expected: readiness/fencing holds preserve both the
            # queued receipt and exact lane barriers for later host progress.
            async with self.coordination._write() as session:
                parent = await session.get(
                    RuntimeCommandRow, batch_id, with_for_update=True
                )
                if parent is not None and parent.state == "queued":
                    parent.result = {"detail": str(error)}
        except HTTPException as error:
            # diagnostic-expected: expired/revoked accepting actors receive a
            # durable authentication response after the SQL attempt rolls back.
            await self._failure(batch_id, error.status_code, str(error.detail))
        except _InvalidBatch as error:
            # diagnostic-expected: invalid saved input receives a durable 422;
            # this type excludes stored-record corruption and runner failures.
            await self._failure(batch_id, 422, str(error))
        except (NotFoundError, ConflictError) as error:
            # diagnostic-expected: missing metadata and revision/source/hierarchy
            # conflicts receive durable 404/409 responses without partial writes.
            await self._failure(
                batch_id, 404 if isinstance(error, NotFoundError) else 409, str(error)
            )
