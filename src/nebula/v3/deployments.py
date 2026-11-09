"""SQL authority for staged promotion, safe project handoff and retirement.

Process creation, IPC and artifact verification happen outside these short
transactions. Host-row serialization bounds overlap; the project-row fence
serializes transfers with authoritative execution commits on both dialects.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .database import (
    Database,
    BootstrapStateRow,
    DeploymentHostRow,
    DeploymentReceiptRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeCapacityReservationRow,
    RuntimeInstanceRow,
    RuntimeProcessRow,
    RuntimeReleaseRow,
    RuntimeResourceRow,
    SchemaVersionRow,
)
from .deployment_protocol import (
    DeploymentStatus,
    HandoffPhase,
    MAX_EXECUTION_VERSIONS,
    ProjectDeploymentStatus,
    ReleaseManifest,
    ResourceSnapshot,
    SHARED_MEMORY_BOOTSTRAP_KEY,
    WorkerIdentity,
)
from .runtime_coordination import (
    CoordinationConflict,
    LEASE_MS,
    ProjectLease,
    RuntimeCoordination,
)
from .process_identity import same_process

_RESIDENT = ("reserved", "starting", "aborting", "ready", "draining", "unreachable")
_UNFINISHED = ("queued", "running", "uncertain")


def _checkpoint(value: dict) -> dict:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > 2 * 1024**2:
        raise ValueError("handoff checkpoints must be bounded durable references")

    def check(item):
        if isinstance(item, dict):
            if any(
                key.casefold()
                in {
                    "token",
                    "auth_token",
                    "password",
                    "api_key",
                    "private_key",
                    "secret",
                }
                for key in item
            ):
                raise ValueError(
                    "handoff checkpoints contain references, never credentials"
                )
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)

    check(value)
    return json.loads(encoded)


def checkpoint_digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class DeploymentCoordinator:
    def __init__(
        self,
        database: Database,
        *,
        runtime_admission: Callable[
            [AsyncSession, ReleaseManifest, str], Awaitable[None]
        ]
        | None = None,
    ):
        self.database, self.coordination = database, RuntimeCoordination(database)
        self.runtime_admission = runtime_admission

    async def _host(self, session):
        await self.coordination._insert_absent(
            session,
            DeploymentHostRow,
            {
                "key": "host",
                "revision": 1,
                "api_runtime_id": None,
                "target_version": None,
                "rollback_version": None,
                "detail": "No runtime has been promoted.",
            },
            ["key"],
        )
        await session.execute(
            update(DeploymentHostRow)
            .where(DeploymentHostRow.key == "host")
            .values(revision=DeploymentHostRow.revision)
        )
        row = await session.get(DeploymentHostRow, "host", populate_existing=True)
        assert row is not None
        return row

    async def _receipt(self, session, action, result):
        session.add(
            DeploymentReceiptRow(
                id=str(uuid4()),
                action=action,
                result=result,
                occurred_at_ms=await self.coordination._clock_ms(session),
            )
        )

    async def stage(
        self, manifest: ReleaseManifest, directory: Path, digest: str
    ) -> None:
        if len(digest) != 64 or any(
            character not in "abcdef0123456789" for character in digest
        ):
            raise ValueError("a verified manifest checksum is required")
        async with self.coordination._write() as session:
            host = await self._host(session)
            existing = await session.get(RuntimeReleaseRow, manifest.version)
            if existing is not None:
                if (
                    existing.manifest_sha256 != digest
                    or Path(existing.install_path) != directory
                ):
                    raise CoordinationConflict(
                        "installed runtime versions are immutable"
                    )
                return
            session.add(
                RuntimeReleaseRow(
                    version=manifest.version,
                    manifest_sha256=digest,
                    manifest=manifest.model_dump(mode="json"),
                    install_path=str(directory),
                    state="staged",
                    staged_at_ms=await self.coordination._clock_ms(session),
                )
            )
            host.revision += 1
            await self._receipt(
                session,
                "release.staged",
                {"version": manifest.version, "manifest_sha256": digest},
            )

    async def _compatible(self, session, manifest, *, role):
        schema = int(
            await session.scalar(select(func.max(SchemaVersionRow.version))) or 0
        )
        if not manifest.schema_min <= schema <= manifest.schema_max:
            raise CoordinationConflict(
                "candidate does not support the current SQL schema"
            )
        versions = set(
            await session.scalars(
                select(RuntimeProcessRow.version).where(
                    RuntimeProcessRow.state.in_(_RESIDENT)
                )
            )
        )
        for version in versions:
            row = await session.get(RuntimeReleaseRow, version)
            if row is None or not manifest.compatible_with(
                ReleaseManifest.model_validate(row.manifest), schema_version=schema
            ):
                raise CoordinationConflict(
                    "candidate protocol/schema is incompatible with a resident runtime"
                )
        if role == "execution":
            required = set(manifest.required_api_features)
            if "chat.accept/v1" in manifest.features:
                required.add("chat.accept/v1")
            host = await session.get(DeploymentHostRow, "host")
            api = (
                await session.get(RuntimeProcessRow, host.api_runtime_id)
                if host is not None and host.api_runtime_id
                else None
            )
            if api is not None:
                api_release = await session.get(RuntimeReleaseRow, api.version)
                assert api_release is not None
                provided = set(
                    ReleaseManifest.model_validate(api_release.manifest).features
                )
                if not required <= provided:
                    raise CoordinationConflict(
                        "Promote an API with the execution candidate's required features first"
                    )
            execution_versions = set(
                await session.scalars(
                    select(RuntimeProcessRow.version).where(
                        RuntimeProcessRow.role == "execution",
                        RuntimeProcessRow.state.in_(_RESIDENT),
                    )
                )
            )
            if (
                manifest.version not in execution_versions
                and len(execution_versions) >= MAX_EXECUTION_VERSIONS
            ):
                return False
        else:
            required = set()
            for version in await session.scalars(
                select(RuntimeProcessRow.version).where(
                    RuntimeProcessRow.role == "execution",
                    RuntimeProcessRow.state.in_(_RESIDENT),
                )
            ):
                resident = await session.get(RuntimeReleaseRow, version)
                assert resident is not None
                resident_manifest = ReleaseManifest.model_validate(resident.manifest)
                required.update(resident_manifest.required_api_features)
                if "chat.accept/v1" in resident_manifest.features:
                    required.add("chat.accept/v1")
            if await session.scalar(
                select(RuntimeCommandRow.id)
                .where(RuntimeCommandRow.name == "chat.accept/v1")
                .limit(1)
            ):
                required.add("chat.accept/v1")
            missing = required - set(manifest.features)
            if missing:
                raise CoordinationConflict(
                    "Candidate API lacks features required by resident or accepted work: "
                    + ", ".join(sorted(missing))
                )
        return True

    async def reserve_worker(
        self, version: str, *, role: str, endpoint: str, credential_reference: str
    ) -> str | None:
        if role not in {"execution", "api"}:
            raise ValueError("unknown independently supervised worker role")
        async with self.coordination._write() as session:
            host = await self._host(session)
            memory_import = await session.get(
                BootstrapStateRow, SHARED_MEMORY_BOOTSTRAP_KEY
            )
            if memory_import is not None and memory_import.status != "complete":
                raise CoordinationConflict(
                    "Shared memory import is incomplete; resolve it before starting another runtime"
                )
            release = await session.get(RuntimeReleaseRow, version)
            if release is None:
                raise CoordinationConflict("stage and verify the runtime bundle first")
            if not await self._compatible(
                session, ReleaseManifest.model_validate(release.manifest), role=role
            ):
                host.detail = "Two execution versions remain resident. The candidate is staged until the older version can retire."
                host.revision += 1
                return None
            if self.runtime_admission is not None:
                await self.runtime_admission(
                    session, ReleaseManifest.model_validate(release.manifest), role
                )
            identity = str(uuid4())
            now = await self.coordination._clock_ms(session)
            session.add(
                RuntimeProcessRow(
                    runtime_id=identity,
                    version=version,
                    role=role,
                    pid=0,
                    process_birth="pending",
                    endpoint=endpoint,
                    credential_reference=credential_reference,
                    state="reserved",
                    updated_at_ms=now,
                )
            )
            session.add(
                RuntimeInstanceRow(
                    id=identity,
                    version=version,
                    role=role,
                    heartbeat_at_ms=now,
                    lease_expires_at_ms=now + LEASE_MS,
                )
            )
            host.revision += 1
            return identity

    async def worker_ready(self, identity: WorkerIdentity) -> None:
        if not same_process(identity.pid, identity.process_birth):
            raise CoordinationConflict(
                "worker birth identity does not match the live OS process"
            )
        async with self.coordination._write() as session:
            host = await self._host(session)
            row = await session.get(RuntimeProcessRow, identity.runtime_id)
            if (
                row is None
                or row.version != identity.version
                or row.role != identity.role
                or row.endpoint != identity.endpoint
            ):
                raise CoordinationConflict(
                    "worker does not match its reserved process incarnation"
                )
            if row.state not in {"starting", "ready"}:
                raise CoordinationConflict(
                    "retired or unreachable processes cannot replace their incarnation"
                )
            if (row.pid, row.process_birth) != (
                identity.pid,
                identity.process_birth,
            ):
                raise CoordinationConflict(
                    "worker PID identity cannot be replaced in place"
                )
            row.pid, row.process_birth, row.state = (
                identity.pid,
                identity.process_birth,
                "ready",
            )
            row.updated_at_ms = await self.coordination._clock_ms(session)
            runtime = await session.get(RuntimeInstanceRow, identity.runtime_id)
            if runtime is None or runtime.lease_expires_at_ms <= row.updated_at_ms:
                raise CoordinationConflict("worker startup lease expired")
            await session.flush()
            if identity.role == "execution":
                await self.coordination._live_execution(
                    session, identity.runtime_id, row.updated_at_ms
                )
            host.revision += 1

    async def bind_spawn(self, identity: WorkerIdentity) -> None:
        """Bind the PID before releasing the private bootstrap pipe to the child."""
        if not same_process(identity.pid, identity.process_birth):
            raise CoordinationConflict("spawned process birth identity is unverified")
        async with self.coordination._write() as session:
            await self._host(session)
            row = await session.get(
                RuntimeProcessRow, identity.runtime_id, with_for_update=True
            )
            if (
                row is None
                or row.state != "reserved"
                or row.pid
                or (row.version, row.role, row.endpoint)
                != (identity.version, identity.role, identity.endpoint)
            ):
                raise CoordinationConflict(
                    "spawn reservation was cancelled or belongs to another worker"
                )
            row.pid, row.process_birth, row.state = (
                identity.pid,
                identity.process_birth,
                "starting",
            )
            row.updated_at_ms = await self.coordination._clock_ms(session)

    async def begin_start_abort(self, runtime_id: str) -> bool:
        """Fence an unready candidate before any external termination action.

        Readiness publication and this transition share the host lock. A ready
        worker may already own work even if its launch caller lost the response.
        """
        async with self.coordination._write() as session:
            host = await self._host(session)
            row = await session.get(RuntimeProcessRow, runtime_id, with_for_update=True)
            if row is None or row.state not in {"reserved", "starting", "aborting"}:
                return False
            row.state, row.updated_at_ms = (
                "aborting",
                await self.coordination._clock_ms(session),
            )
            await session.execute(
                update(RuntimeInstanceRow)
                .where(RuntimeInstanceRow.id == runtime_id)
                .values(lease_expires_at_ms=0)
            )
            host.revision += 1
            await self._receipt(
                session,
                "worker.start_aborting",
                {"runtime_id": runtime_id, "version": row.version},
            )
            return True

    async def abort_start(self, runtime_id: str) -> None:
        async with self.coordination._write() as session:
            host = await self._host(session)
            row = await session.get(RuntimeProcessRow, runtime_id, with_for_update=True)
            if row is None or row.state not in {"reserved", "starting", "aborting"}:
                raise CoordinationConflict(
                    "only an unready startup reservation can be aborted"
                )
            if row.pid and same_process(row.pid, row.process_birth):
                raise CoordinationConflict(
                    "the spawned worker must exit before its startup slot is released"
                )
            # A zero PID has never received bootstrap. Late bind/ready calls
            # are fenced out by this terminal state and expired incarnation.
            row.state, row.updated_at_ms = (
                "retired",
                await self.coordination._clock_ms(session),
            )
            await session.execute(
                update(RuntimeInstanceRow)
                .where(RuntimeInstanceRow.id == runtime_id)
                .values(lease_expires_at_ms=0)
            )
            host.revision += 1
            await self._receipt(
                session,
                "worker.start_aborted",
                {"runtime_id": runtime_id, "version": row.version},
            )

    async def promote_api(self, runtime_id: str, *, expected_revision: int) -> None:
        async with self.coordination._write() as session:
            host = await self._host(session)
            if host.revision != expected_revision:
                raise CoordinationConflict(
                    "deployment status changed; read it before promoting"
                )
            process = await session.get(RuntimeProcessRow, runtime_id)
            runtime = await session.get(RuntimeInstanceRow, runtime_id)
            now = await self.coordination._clock_ms(session)
            if (
                process is None
                or process.role != "api"
                or process.state != "ready"
                or runtime is None
                or runtime.lease_expires_at_ms <= now
            ):
                raise CoordinationConflict("candidate API is not ready and healthy")
            release = await session.get(RuntimeReleaseRow, process.version)
            assert release is not None
            await self._compatible(
                session, ReleaseManifest.model_validate(release.manifest), role="api"
            )
            if host.api_runtime_id and host.api_runtime_id != runtime_id:
                previous = await session.get(RuntimeProcessRow, host.api_runtime_id)
                if previous is not None:
                    previous.state = "draining"
            host.api_runtime_id, host.revision = runtime_id, host.revision + 1
            host.detail = (
                "API traffic is promoted. Execution owners continue independently."
            )
            release.state = "active"
            await self._receipt(
                session,
                "api.promoted",
                {"runtime_id": runtime_id, "version": process.version},
            )

    async def target_execution(self, version: str) -> None:
        async with self.coordination._write() as session:
            host = await self._host(session)
            release = await session.get(RuntimeReleaseRow, version)
            if release is None:
                raise CoordinationConflict("candidate is not installed")
            await self._compatible(
                session,
                ReleaseManifest.model_validate(release.manifest),
                role="execution",
            )
            if host.target_version != version:
                host.rollback_version, host.target_version = (
                    host.target_version,
                    version,
                )
                host.revision += 1
            await self._receipt(
                session,
                "execution.target",
                {"version": version, "rollback_version": host.rollback_version},
            )

    async def resource(self, lease: ProjectLease, snapshot: ResourceSnapshot) -> None:
        if (snapshot.project_id, snapshot.runtime_id, snapshot.fencing_token) != (
            lease.project_id,
            lease.runtime_id,
            lease.fencing_token,
        ):
            raise CoordinationConflict(
                "resource descriptor does not belong to this project owner"
            )
        values = snapshot.model_dump(mode="json")
        values["checkpoint"] = _checkpoint(values["checkpoint"])
        async with self.coordination.fenced(lease) as session:
            row = await session.get(RuntimeResourceRow, snapshot.id)
            if row is not None and (
                row.project_id,
                row.runtime_id,
                row.fencing_token,
            ) != (lease.project_id, lease.runtime_id, lease.fencing_token):
                raise CoordinationConflict(
                    "resource identity already belongs to another incarnation"
                )
            values["updated_at_ms"] = await self.coordination._clock_ms(session)
            if row is None:
                session.add(RuntimeResourceRow(**values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            handoff = await session.get(ProjectHandoffRow, lease.project_id)
            if handoff is not None and handoff.phase == HandoffPhase.TRANSFERABLE:
                handoff.phase, handoff.admission_held = HandoffPhase.FINISHING, True
                handoff.blockers = [
                    "Resource state changed; the source must verify a new safe boundary"
                ]

    async def request_handoff(self, project_id: str, target_runtime_id: str) -> None:
        async with self.coordination._write() as session:
            await self._host(session)
            owner = await session.get(
                ProjectOwnershipRow, project_id, with_for_update=True
            )
            target = await session.get(RuntimeProcessRow, target_runtime_id)
            now = await self.coordination._clock_ms(session)
            if (
                owner is None
                or owner.runtime_id is None
                or owner.lease_expires_at_ms <= now
            ):
                raise CoordinationConflict(
                    "recovery requires an expired-owner claim, not a live handoff"
                )
            if target is None or target.role != "execution" or target.state != "ready":
                raise CoordinationConflict("target execution worker is not ready")
            await self.coordination._live_execution(session, target_runtime_id, now)
            existing = await session.get(ProjectHandoffRow, project_id)
            if owner.runtime_id == target_runtime_id:
                if existing is not None and existing.phase in {
                    HandoffPhase.REQUESTED,
                    HandoffPhase.FINISHING,
                    HandoffPhase.TRANSFERABLE,
                }:
                    existing.phase, existing.admission_held = (
                        HandoffPhase.VERIFIED,
                        False,
                    )
                    existing.blockers, existing.checkpoint = (
                        [],
                        {"cancelled_before_transfer": True},
                    )
                    existing.revision += 1
                return
            if existing is not None and existing.phase != HandoffPhase.VERIFIED:
                if existing.target_runtime_id == target_runtime_id:
                    return
                if existing.phase == HandoffPhase.TRANSFERRED:
                    raise CoordinationConflict(
                        "a transferred project must finish restoration or use expired-lease recovery"
                    )
                await self._receipt(
                    session,
                    "handoff.retargeted",
                    {
                        "project_id": project_id,
                        "old_target_runtime_id": existing.target_runtime_id,
                        "target_runtime_id": target_runtime_id,
                    },
                )
            values = dict(
                project_id=project_id,
                revision=1 if existing is None else existing.revision + 1,
                source_runtime_id=owner.runtime_id,
                source_fencing_token=owner.fencing_token,
                target_runtime_id=target_runtime_id,
                target_version=target.version,
                phase=HandoffPhase.REQUESTED,
                admission_held=True,
                blockers=[],
                checkpoint={},
                updated_at_ms=now,
            )
            if existing is None:
                session.add(ProjectHandoffRow(**values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            await self._receipt(
                session,
                "handoff.requested",
                {
                    "project_id": project_id,
                    "source_runtime_id": owner.runtime_id,
                    "target_runtime_id": target_runtime_id,
                    "target_version": target.version,
                },
            )

    async def checkpoint(
        self, lease: ProjectLease, *, checkpoint: dict, blockers: list[str]
    ) -> HandoffPhase:
        checkpoint = _checkpoint(checkpoint)
        if len(blockers) > 100 or any(
            not isinstance(item, str) or len(item) > 4000 for item in blockers
        ):
            raise ValueError("handoff blockers must be bounded operator explanations")
        async with self.coordination.fenced(lease) as session:
            row = await session.get(ProjectHandoffRow, lease.project_id)
            if row is None or (row.source_runtime_id, row.source_fencing_token) != (
                lease.runtime_id,
                lease.fencing_token,
            ):
                raise CoordinationConflict(
                    "no handoff belongs to this source incarnation"
                )
            if row.phase not in {
                HandoffPhase.REQUESTED,
                HandoffPhase.FINISHING,
                HandoffPhase.TRANSFERABLE,
            }:
                raise CoordinationConflict("ownership has already transferred")
            target_release = await session.get(RuntimeReleaseRow, row.target_version)
            assert target_release is not None
            supported = ReleaseManifest.model_validate(
                target_release.manifest
            ).restorable_resources
            observed = list(blockers)
            pinned = False
            for resource in await session.scalars(
                select(RuntimeResourceRow).where(
                    RuntimeResourceRow.project_id == lease.project_id,
                    RuntimeResourceRow.state != "closed",
                )
            ):
                if (
                    resource.state != "restorable"
                    or resource.restoration == "pinned"
                    or resource.kind not in supported
                ):
                    pinned = (
                        pinned
                        or resource.restoration == "pinned"
                        or resource.kind not in supported
                        or resource.state == "uncertain"
                    )
                    observed.append(
                        resource.detail
                        or f"{resource.kind} {resource.id} cannot be restored by the candidate"
                    )
            commands = int(
                await session.scalar(
                    select(func.count())
                    .select_from(RuntimeCommandRow)
                    .where(
                        RuntimeCommandRow.project_id == lease.project_id,
                        RuntimeCommandRow.state.in_(("running", "uncertain")),
                    )
                )
                or 0
            )
            if commands:
                observed.append(
                    f"{commands} unfinished or uncertain command(s) require their current owner"
                )
            row.phase = (
                HandoffPhase.FINISHING if observed else HandoffPhase.TRANSFERABLE
            )
            # A pinned project remains usable on its old runtime. Releasing
            # admission does not transfer it; a later retry first drains again.
            row.admission_held = not pinned
            row.checkpoint, row.blockers = checkpoint, list(dict.fromkeys(observed))
            row.revision += 1
            row.updated_at_ms = await self.coordination._clock_ms(session)
            return HandoffPhase(row.phase)

    async def transfer(self, project_id: str) -> ProjectLease:
        async with self.coordination._write() as session:
            await self._host(session)
            owner = await session.get(
                ProjectOwnershipRow, project_id, with_for_update=True
            )
            handoff = await session.get(
                ProjectHandoffRow, project_id, with_for_update=True
            )
            if (
                owner is not None
                and handoff is not None
                and handoff.phase in {HandoffPhase.TRANSFERRED, HandoffPhase.VERIFIED}
                and owner.runtime_id == handoff.target_runtime_id
                and owner.fencing_token == handoff.source_fencing_token + 1
            ):
                existing = ProjectLease(
                    project_id, owner.runtime_id, owner.fencing_token
                )
                await self.coordination._check(session, existing, lock=False)
                return existing
            if (
                owner is None
                or handoff is None
                or handoff.phase != HandoffPhase.TRANSFERABLE
                or handoff.blockers
            ):
                raise CoordinationConflict(
                    "project has not reached a transferable boundary"
                )
            old = ProjectLease(
                project_id, handoff.source_runtime_id, handoff.source_fencing_token
            )
            await self.coordination._check(session, old, lock=False)
            now = await self.coordination._clock_ms(session)
            await self.coordination._live_execution(
                session, handoff.target_runtime_id, now
            )
            unfinished = await session.scalar(
                select(RuntimeCommandRow.id)
                .where(
                    RuntimeCommandRow.project_id == project_id,
                    RuntimeCommandRow.state.in_(("running", "uncertain")),
                )
                .limit(1)
            )
            resources = await session.scalar(
                select(RuntimeResourceRow.id)
                .where(
                    RuntimeResourceRow.project_id == project_id,
                    RuntimeResourceRow.state.not_in(("closed", "restorable")),
                )
                .limit(1)
            )
            if unfinished is not None or resources is not None:
                raise CoordinationConflict(
                    "project changed after its transferable checkpoint"
                )
            source_expiry = owner.lease_expires_at_ms
            owner.runtime_id, owner.fencing_token, owner.lease_expires_at_ms = (
                handoff.target_runtime_id,
                owner.fencing_token + 1,
                now + LEASE_MS,
            )
            handoff.phase, handoff.revision, handoff.updated_at_ms = (
                HandoffPhase.TRANSFERRED,
                handoff.revision + 1,
                now,
            )
            await session.flush()
            if await self.coordination._clock_ms(session) >= source_expiry:
                raise CoordinationConflict(
                    "source lease expired during handoff; retry through recovery"
                )
            lease = ProjectLease(project_id, owner.runtime_id, owner.fencing_token)
            await self._receipt(
                session,
                "handoff.transferred",
                {
                    "project_id": project_id,
                    "runtime_id": lease.runtime_id,
                    "fencing_token": lease.fencing_token,
                    "checkpoint_digest": checkpoint_digest(handoff.checkpoint),
                },
            )
            return lease

    async def verified(
        self, lease: ProjectLease, *, restored_checkpoint_digest: str
    ) -> None:
        async with self.coordination.fenced(lease) as session:
            row = await session.get(ProjectHandoffRow, lease.project_id)
            if (
                row is None
                or row.phase not in {HandoffPhase.TRANSFERRED, HandoffPhase.VERIFIED}
                or row.target_runtime_id != lease.runtime_id
            ):
                raise CoordinationConflict(
                    "target does not own this transferred checkpoint"
                )
            if checkpoint_digest(row.checkpoint) != restored_checkpoint_digest:
                raise CoordinationConflict(
                    "worker did not restore the accepted checkpoint"
                )
            if row.phase == HandoffPhase.VERIFIED:
                return
            for resource in await session.scalars(
                select(RuntimeResourceRow).where(
                    RuntimeResourceRow.project_id == lease.project_id,
                    RuntimeResourceRow.state == "restorable",
                )
            ):
                resource.runtime_id, resource.fencing_token = (
                    lease.runtime_id,
                    lease.fencing_token,
                )
            row.phase, row.blockers = HandoffPhase.VERIFIED, []
            row.admission_held = False
            row.revision += 1
            row.updated_at_ms = await self.coordination._clock_ms(session)
            await self._receipt(
                session,
                "handoff.verified",
                {"project_id": lease.project_id, "runtime_id": lease.runtime_id},
            )

    async def retirement_blockers(self, runtime_id: str) -> list[str]:
        async with self.database.session() as session:
            owners = list(
                await session.scalars(
                    select(ProjectOwnershipRow.project_id).where(
                        ProjectOwnershipRow.runtime_id == runtime_id
                    )
                )
            )
            resources = list(
                await session.scalars(
                    select(RuntimeResourceRow.id).where(
                        RuntimeResourceRow.runtime_id == runtime_id,
                        RuntimeResourceRow.state != "closed",
                    )
                )
            )
            commands = list(
                await session.scalars(
                    select(RuntimeCommandRow.id).where(
                        RuntimeCommandRow.runtime_id == runtime_id,
                        RuntimeCommandRow.state.in_(_UNFINISHED),
                    )
                )
            )
            capacity = list(
                await session.scalars(
                    select(RuntimeCapacityReservationRow.id).where(
                        RuntimeCapacityReservationRow.runtime_id == runtime_id,
                        RuntimeCapacityReservationRow.state == "held",
                    )
                )
            )
            return [
                *(
                    "Project " + item + " still belongs to this worker"
                    for item in owners
                ),
                *("Resource " + item + " remains resident" for item in resources),
                *(
                    "Command " + item + " is unfinished or uncertain"
                    for item in commands
                ),
                *(
                    "Capacity reservation " + item + " remains held"
                    for item in capacity
                ),
            ]

    async def begin_retirement(self, runtime_id: str) -> WorkerIdentity:
        async with self.coordination._write() as session:
            host = await self._host(session)
            row = await session.get(
                RuntimeProcessRow,
                runtime_id,
                with_for_update=True,
                populate_existing=True,
            )
            if row is None or not row.pid or row.state == "retired":
                raise CoordinationConflict(
                    "there is no live process incarnation to retire"
                )
            if host.api_runtime_id == runtime_id:
                raise CoordinationConflict(
                    "promote another API before retiring the stable listener's worker"
                )
            blockers = await self.retirement_blockers(runtime_id)
            if blockers:
                raise CoordinationConflict("; ".join(blockers[:10]))
            row.state = "draining"
            row.updated_at_ms = await self.coordination._clock_ms(session)
            host.revision += 1
            return WorkerIdentity(
                runtime_id=row.runtime_id,
                version=row.version,
                role="api" if row.role == "api" else "execution",
                pid=row.pid,
                process_birth=row.process_birth,
                endpoint=row.endpoint,
            )

    async def retired(self, identity: WorkerIdentity) -> None:
        """Called only after the host has verified this exact process is gone."""
        if same_process(identity.pid, identity.process_birth):
            raise CoordinationConflict(
                "this exact worker is still alive; retain its assets and resident slot"
            )
        async with self.coordination._write() as session:
            host = await self._host(session)
            row = await session.get(RuntimeProcessRow, identity.runtime_id)
            if row is None or (
                row.pid,
                row.process_birth,
                row.endpoint,
                row.version,
            ) != (
                identity.pid,
                identity.process_birth,
                identity.endpoint,
                identity.version,
            ):
                raise CoordinationConflict(
                    "retirement proof belongs to another process incarnation"
                )
            if row.state == "retired":
                return
            if (
                row.state not in {"draining", "unreachable"}
                or host.api_runtime_id == row.runtime_id
            ):
                raise CoordinationConflict("worker has not been admitted to retirement")
            if await self.retirement_blockers(identity.runtime_id):
                raise CoordinationConflict(
                    "worker acquired an owner, resource or command before retirement"
                )
            row.state, row.updated_at_ms = (
                "retired",
                await self.coordination._clock_ms(session),
            )
            await session.execute(
                update(RuntimeInstanceRow)
                .where(RuntimeInstanceRow.id == row.runtime_id)
                .values(lease_expires_at_ms=0)
            )
            host.revision += 1
            await self._receipt(
                session,
                "worker.retired",
                {
                    "runtime_id": row.runtime_id,
                    "version": row.version,
                    "role": row.role,
                },
            )

    async def resolve_uncertain(
        self,
        lease: ProjectLease,
        command_id: str,
        *,
        actor_id: str,
        result: dict,
        succeeded: bool,
    ) -> None:
        if not actor_id or len(actor_id) > 200:
            raise ValueError("an authenticated operator or Butler actor is required")
        result = _checkpoint(result)
        async with self.coordination.fenced(lease) as session:
            row = await session.get(RuntimeCommandRow, command_id)
            if (
                row is None
                or row.project_id != lease.project_id
                or row.state != "uncertain"
            ):
                raise CoordinationConflict(
                    "only an uncertain command in this project can be resolved"
                )
            row.state, row.result = "succeeded" if succeeded else "failed", result
            await self._receipt(
                session,
                "command.resolved",
                {
                    "command_id": command_id,
                    "project_id": lease.project_id,
                    "actor_id": actor_id,
                    "outcome": row.state,
                    "replayed": False,
                },
            )

    async def status(self) -> DeploymentStatus:
        async with self.coordination._write() as session:
            host = await self._host(session)
            processes = {
                row.runtime_id: row
                for row in await session.scalars(select(RuntimeProcessRow))
            }
            releases = list(await session.scalars(select(RuntimeReleaseRow)))
            handoffs = {
                row.project_id: row
                for row in await session.scalars(select(ProjectHandoffRow))
            }
            projects = []
            for owner in await session.scalars(select(ProjectOwnershipRow)):
                process, handoff = (
                    processes.get(owner.runtime_id)
                    if owner.runtime_id is not None
                    else None,
                    handoffs.get(owner.project_id),
                )
                projects.append(
                    ProjectDeploymentStatus(
                        project_id=owner.project_id,
                        runtime_id=owner.runtime_id,
                        runtime_version=process.version if process else None,
                        fencing_token=owner.fencing_token,
                        phase=HandoffPhase(handoff.phase) if handoff else None,
                        target_version=handoff.target_version if handoff else None,
                        blockers=handoff.blockers if handoff else [],
                        admission_held=handoff.admission_held if handoff else False,
                    )
                )
            resident = sorted(
                {
                    row.version
                    for row in processes.values()
                    if row.role == "execution" and row.state in _RESIDENT
                }
            )
            api = (
                processes.get(host.api_runtime_id)
                if host.api_runtime_id is not None
                else None
            )
            degraded = any(row.state == "unreachable" for row in processes.values())
            return DeploymentStatus(
                revision=host.revision,
                api_version=api.version if api else None,
                target_execution_version=host.target_version,
                resident_versions=resident,
                staged_versions=[
                    row.version
                    for row in releases
                    if row.state in {"staged", "active"} and row.version not in resident
                ],
                projects=projects,
                health="degraded" if degraded else "ready" if api else "starting",
                detail=host.detail,
            )
