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

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .database import (
    Database,
    BootstrapStateRow,
    DeploymentHostRow,
    DeploymentReceiptRow,
    EntityRow,
    MemoryJobRow,
    ProjectAdmissionHoldRow,
    ProviderTurnQueueRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeCapacityReservationRow,
    RuntimeInstanceRow,
    RuntimeProcessRow,
    RuntimeReleaseRow,
    RuntimeResourceRow,
    RuntimeRecoveryRow,
    SchemaVersionRow,
)
from .deployment_protocol import (
    DeploymentStatus,
    GLOBAL_EXECUTION_PROJECT,
    HandoffPhase,
    MAX_EXECUTION_VERSIONS,
    ProcessDeploymentStatus,
    ProjectDeploymentStatus,
    ProjectRecoveryStatus,
    ReleaseManifest,
    ResourceSnapshot,
    SHARED_MEMORY_BOOTSTRAP_KEY,
    WorkerIdentity,
)
from .runtime_coordination import (
    CoordinationConflict,
    DeploymentChanged,
    LEASE_MS,
    LeaseLost,
    OperationRejected,
    ProjectLease,
    RecoveryBlocked,
    RuntimeCoordination,
    STORAGE_REPORT_COMMAND,
    ShutdownBlocked,
    pending_storage_dispatches,
    project_worker_commands,
    requeue_interrupted_storage_report,
)
from .process_identity import same_process

_RESIDENT = ("reserved", "starting", "aborting", "ready", "draining", "unreachable")
# A replaced API keeps serving the requests and streams it already holds for
# this long. It exceeds the API's 35 s idempotent-acceptance wait, so accepted
# work commits before the supervisor retires the process; clients reconnect
# their streams to the promoted API. Its process then has the worker's 5 s
# graceful shutdown and the 30 s retirement wait to exit.
API_DRAIN_GRACE_MS = 60_000
# Past this, a replaced API that is still resident means automatic retirement
# failed; aggregate health reports it instead of hiding it.
API_DRAIN_BOUND_MS = 180_000
EXECUTION_FAILED_RECEIPT = "project.execution_failed"
_UNFINISHED = ("queued", "running", "uncertain")
_PENDING_STORAGE_ALERT = "A recorded storage alert has not opened its incident chat yet"


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
            raise OperationRejected("candidate does not support the current SQL schema")
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
                raise OperationRejected(
                    "candidate protocol/schema is incompatible with a resident runtime"
                )
        if role == "execution":
            required = set(manifest.required_api_features)
            required.update(
                set(manifest.features) & {"chat.accept/v1", "chat.resume/v1"}
            )
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
                    raise OperationRejected(
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
            if STORAGE_REPORT_COMMAND not in manifest.features:
                # A saved storage report must run on a release that knows it;
                # an older target would strand it in its project.
                pending = await session.scalar(
                    select(RuntimeCommandRow.project_id)
                    .where(
                        RuntimeCommandRow.name == STORAGE_REPORT_COMMAND,
                        RuntimeCommandRow.state.in_(_UNFINISHED),
                    )
                    .limit(1)
                )
                if pending is not None:
                    raise CoordinationConflict(
                        f"{await self._project_label_in(session, pending)} has "
                        "storage reports waiting to run. Let them finish, or "
                        "disable their storage source, before moving execution to "
                        "a release that cannot run storage reports"
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
                required.update(
                    set(resident_manifest.features)
                    & {"chat.accept/v1", "chat.resume/v1"}
                )
            # An API reads accepted chat requests for as long as they exist.
            required.update(
                await session.scalars(
                    select(RuntimeCommandRow.name)
                    .where(
                        RuntimeCommandRow.name.in_(("chat.accept/v1", "chat.resume/v1"))
                    )
                    .distinct()
                )
            )
            # A storage report needs an API that knows it only until it runs.
            if (
                await session.scalar(
                    select(RuntimeCommandRow.id)
                    .where(
                        RuntimeCommandRow.name == STORAGE_REPORT_COMMAND,
                        RuntimeCommandRow.state.in_(_UNFINISHED),
                    )
                    .limit(1)
                )
                is not None
            ):
                required.add(STORAGE_REPORT_COMMAND)
            missing = required - set(manifest.features)
            if missing:
                raise OperationRejected(
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
            if host.shutdown_requested:
                raise CoordinationConflict(
                    "the host is draining for coordinated shutdown"
                )
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
                detail = (
                    f"Release {version} is staged: no execution worker can "
                    "start as it while two other execution versions remain "
                    "resident. " + await self._resident_version_pins(session)
                )[:4000]
                if host.detail != detail:
                    host.detail = detail
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
                raise DeploymentChanged(
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
                raise OperationRejected("candidate API is not ready and healthy")
            release = await session.get(RuntimeReleaseRow, process.version)
            assert release is not None
            await self._compatible(
                session, ReleaseManifest.model_validate(release.manifest), role="api"
            )
            if host.api_runtime_id and host.api_runtime_id != runtime_id:
                previous = await session.get(RuntimeProcessRow, host.api_runtime_id)
                if previous is not None:
                    # The drain starts now; the supervisor retires the
                    # previous API once API_DRAIN_GRACE_MS has passed.
                    previous.state, previous.updated_at_ms = "draining", now
            host.api_runtime_id, host.revision = runtime_id, host.revision + 1
            host.detail = (
                "API traffic is promoted. Execution owners continue independently."
            )
            release.state = "active"
            await self._receipt(
                session,
                "api.promoted",
                {
                    "runtime_id": runtime_id,
                    "version": process.version,
                    "revision": host.revision,
                },
            )

    async def _require_startable_target(self, session, host, release) -> None:
        """Refuse an execution target that no new worker could start as.

        Automatic admission and expired-owner recovery start workers at the
        target, and ``reserve_worker`` refuses a third resident execution
        version. Recording such a target would leave new and recovering
        projects waiting for a worker that never starts.
        """
        if await self._compatible(
            session,
            ReleaseManifest.model_validate(release.manifest),
            role="execution",
        ):
            return
        resident = sorted(
            set(
                await session.scalars(
                    select(RuntimeProcessRow.version).where(
                        RuntimeProcessRow.role == "execution",
                        RuntimeProcessRow.state.in_(_RESIDENT),
                    )
                )
            )
        )
        raise OperationRejected(
            f"Release {release.version} cannot become the execution target: "
            f"execution versions {', '.join(resident)} are resident and at most "
            f"{MAX_EXECUTION_VERSIONS} can run at once, so its workers could not "
            "start. Move projects off one of those versions and retire it first. "
            f"The execution target stays {host.target_version or 'unset'}. "
            + await self._resident_version_pins(session)
        )

    async def execution_capacity_blocker(self, version: str) -> str:
        """Why no execution worker of ``version`` can start, for a refusal."""
        async with self.database.session() as session:
            return (
                f"Release {version} cannot start an execution worker: "
                f"at most {MAX_EXECUTION_VERSIONS} execution versions can run at "
                "once and two others are resident, so no project can switch to "
                "it yet. " + await self._resident_version_pins(session)
            )

    async def _resident_version_pins(self, session, *, limit: int = 10) -> str:
        """Name which projects keep each resident execution version, and how
        that version retires.

        A version stays resident while any of its execution processes is
        unretired: a project owner, or an unowned process (prepared, draining
        after release, or unreachable) that still holds a slot.
        """
        processes = list(
            await session.scalars(
                select(RuntimeProcessRow)
                .where(
                    RuntimeProcessRow.role == "execution",
                    RuntimeProcessRow.state.in_(_RESIDENT),
                )
                .order_by(RuntimeProcessRow.version, RuntimeProcessRow.runtime_id)
            )
        )
        owners: dict[str, list[str]] = {}
        for project_id, runtime_id in await session.execute(
            select(ProjectOwnershipRow.project_id, ProjectOwnershipRow.runtime_id)
            .where(
                ProjectOwnershipRow.runtime_id.in_(
                    [row.runtime_id for row in processes]
                )
            )
            .order_by(ProjectOwnershipRow.project_id)
        ):
            owners.setdefault(runtime_id, []).append(project_id)
        handoffs = {
            row.project_id: row
            for row in await session.scalars(
                select(ProjectHandoffRow).where(
                    ProjectHandoffRow.project_id.in_(
                        [item for values in owners.values() for item in values]
                    )
                )
            )
        }
        versions: dict[str, list[RuntimeProcessRow]] = {}
        for row in processes:
            versions.setdefault(row.version, []).append(row)
        parts = []
        for version, rows in versions.items():
            projects, unowned = [], []
            for row in rows:
                owned = owners.get(row.runtime_id, [])
                if not owned:
                    unowned.append(f"{row.runtime_id} ({row.state})")
                for project_id in owned:
                    label = await self._project_label_in(session, project_id)
                    handoff = handoffs.get(project_id)
                    if handoff is not None and handoff.phase != HandoffPhase.VERIFIED:
                        label += f" [handoff {handoff.phase}"
                        if handoff.blockers:
                            label += ": " + "; ".join(
                                str(item)[:200] for item in handoff.blockers[:3]
                            )
                        label += "]"
                    projects.append(label)
            held = []
            if projects:
                held.append(
                    f"{len(projects)} project(s): "
                    + ", ".join(projects[:limit])
                    + (
                        f" and {len(projects) - limit} more"
                        if len(projects) > limit
                        else ""
                    )
                )
            if unowned:
                held.append(
                    f"{len(unowned)} unowned worker(s): "
                    + ", ".join(unowned[:limit])
                    + (
                        f" and {len(unowned) - limit} more"
                        if len(unowned) > limit
                        else ""
                    )
                )
            parts.append(f"Version {version} is kept resident by " + " and ".join(held))
        return (
            "; ".join(parts)
            + ". A version retires once each of its projects has moved to the "
            "other resident version (a deployment handoff) or gone idle so its "
            "worker retires, and its unowned workers are retired."
        )

    async def target_execution(self, version: str) -> None:
        async with self.coordination._write() as session:
            host = await self._host(session)
            release = await session.get(RuntimeReleaseRow, version)
            if release is None:
                raise CoordinationConflict("candidate is not installed")
            await self._require_startable_target(session, host, release)
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

    async def migration_preflight(
        self, version: str, *, expected_revision: int | None
    ) -> dict:
        async with self.coordination._write() as session:
            host = await self._host(session)
            if expected_revision is not None and host.revision != expected_revision:
                raise DeploymentChanged("deployment changed before migration")
            release = await session.get(RuntimeReleaseRow, version)
            if release is None:
                raise CoordinationConflict("stage the migration release first")
            manifest = ReleaseManifest.model_validate(release.manifest)
            if not manifest.additive_migrations:
                raise CoordinationConflict(
                    "online deployment requires additive migrations"
                )
            await self._compatible(session, manifest, role="api")
            heads = list(
                await session.scalars(text("SELECT version_num FROM alembic_version"))
            )
            if len(heads) != 1:
                raise CoordinationConflict(
                    "migration requires one verified database head"
                )
            schema = int(
                await session.scalar(select(func.max(SchemaVersionRow.version))) or 0
            )
            return {
                "version": version,
                "revision": host.revision,
                "schema": schema,
                "current_head": heads[0],
                "target_head": manifest.migration_head,
            }

    async def rollback_code(self, version: str, *, expected_revision: int) -> None:
        """Change desired retained code without restoring old data."""
        async with self.coordination._write() as session:
            host = await self._host(session)
            if host.revision != expected_revision:
                raise DeploymentChanged("deployment changed before code rollback")
            release = await session.get(RuntimeReleaseRow, version)
            if release is None:
                raise CoordinationConflict("rollback code is not retained")
            await self._require_startable_target(session, host, release)
            host.rollback_version, host.target_version = host.target_version, version
            host.revision += 1
            await self._receipt(
                session, "execution.code_rollback", {"version": version}
            )

    async def project_label(self, project_id: str) -> str:
        """Name a project for operator-facing refusals without loading its model."""
        async with self.database.session() as session:
            return await self._project_label_in(session, project_id)

    @staticmethod
    async def _project_label_in(session, project_id: str) -> str:
        name = await session.scalar(
            select(EntityRow.payload["name"].as_string()).where(
                EntityRow.id == project_id, EntityRow.kind == "engagements"
            )
        )
        # The name is the operator's own label; keep it one bounded line so a
        # receipt or host detail cannot be reshaped by its characters.
        if not isinstance(name, str):
            return f"Project {project_id}"
        printable = "".join(c if c.isprintable() else " " for c in name)
        name = " ".join(printable.split()).replace('"', "'")[:80]
        return f'Project "{name}" ({project_id})' if name else f"Project {project_id}"

    async def record_execution_failure(
        self, lease: ProjectLease, *, detail: str, cleanup_complete: bool
    ) -> None:
        """Keep a failed owner's operator-safe reason for deployment status.

        Only the exact current grant is recorded; a moved grant has no failure
        to report. The receipt is evidence only and changes no ownership.
        """
        async with self.coordination._write() as session:
            owner = await session.get(ProjectOwnershipRow, lease.project_id)
            if owner is None or (owner.runtime_id, owner.fencing_token) != (
                lease.runtime_id,
                lease.fencing_token,
            ):
                return
            await self._receipt(
                session,
                EXECUTION_FAILED_RECEIPT,
                {
                    "project_id": lease.project_id,
                    "runtime_id": lease.runtime_id,
                    "fencing_token": lease.fencing_token,
                    "detail": str(detail)[:1000],
                    "cleanup_complete": bool(cleanup_complete),
                },
            )

    async def _owner_failures(self, session, owners) -> dict[str, dict]:
        """The latest recorded failure of each project's exact current owner."""
        current = {
            owner.project_id: (owner.runtime_id, owner.fencing_token)
            for owner in owners
            if owner.runtime_id is not None
        }
        failures: dict[str, dict] = {}
        if not current:
            return failures
        for row in await session.scalars(
            select(DeploymentReceiptRow)
            .where(
                DeploymentReceiptRow.action == EXECUTION_FAILED_RECEIPT,
                DeploymentReceiptRow.result["project_id"]
                .as_string()
                .in_(sorted(current)),
            )
            .order_by(DeploymentReceiptRow.occurred_at_ms)
        ):
            result = row.result if isinstance(row.result, dict) else {}
            project_id = result.get("project_id")
            if isinstance(project_id, str) and current.get(project_id) == (
                result.get("runtime_id"),
                result.get("fencing_token"),
            ):
                failures[project_id] = result
        return failures

    @staticmethod
    def _failure_blocker(failure: dict) -> str:
        detail = str(failure.get("detail") or "The project application stopped")
        outcome = (
            "its own cleanup finished, but it still owns the project"
            if failure.get("cleanup_complete") is True
            else "its cleanup is unverified, so it keeps the project's ownership"
        )
        return (
            f"Execution worker {failure.get('runtime_id')} failed: {detail}; " + outcome
        )

    async def shutdown_preflight(self, *, expected_revision: int) -> dict:
        """Refuse shutdown while project work, effects or charges remain."""
        async with self.coordination._write() as session:
            host = await self._host(session)
            if host.revision != expected_revision:
                raise CoordinationConflict("deployment changed before shutdown")
            blockers = await self._shutdown_blockers(session)
            if blockers:
                raise CoordinationConflict("; ".join(blockers[:20]))
            return {"revision": host.revision, "ready": True, "blockers": []}

    async def _shutdown_blockers(
        self, session, project_id=None, *, allow_queued_commands=False
    ):
        blockers = []
        command_states = (
            ("running", "uncertain") if allow_queued_commands else _UNFINISHED
        )
        # A queued storage report is durable and runs again after a restart;
        # it never holds a shutdown (a source could otherwise keep posting).
        command_blocks = and_(
            RuntimeCommandRow.state.in_(command_states),
            or_(
                RuntimeCommandRow.state != "queued",
                RuntimeCommandRow.name != STORAGE_REPORT_COMMAND,
            ),
        )
        if project_id is None:
            # A queued request of a project whose worker Core is holding (it
            # waits for memory, say) has not started and cannot start before
            # the stop. It stays saved and runs when the project next gets a
            # worker, so it never holds a shutdown.
            command_blocks = and_(
                command_blocks,
                or_(
                    RuntimeCommandRow.state != "queued",
                    RuntimeCommandRow.project_id == GLOBAL_EXECUTION_PROJECT,
                    select(ProjectOwnershipRow.project_id)
                    .where(
                        ProjectOwnershipRow.project_id == RuntimeCommandRow.project_id,
                        ProjectOwnershipRow.runtime_id.is_not(None),
                    )
                    .exists(),
                    ~select(ProjectAdmissionHoldRow.project_id)
                    .where(
                        ProjectAdmissionHoldRow.project_id
                        == RuntimeCommandRow.project_id
                    )
                    .exists(),
                ),
            )
        for model, predicate, label in (
            (RuntimeCommandRow, command_blocks, "Command"),
            (RuntimeResourceRow, RuntimeResourceRow.state != "closed", "Resource"),
            (
                RuntimeCapacityReservationRow,
                RuntimeCapacityReservationRow.state == "held",
                "Capacity reservation",
            ),
            (
                MemoryJobRow,
                MemoryJobRow.state.in_(
                    ("reserved", "starting", "running", "updating", "uncertain")
                ),
                "Compute job",
            ),
        ):
            query = select(model.id).where(predicate)
            if project_id is not None:
                query = query.where(model.project_id == project_id)
            blockers.extend(
                label + " " + identity + " remains active or unresolved"
                for identity in await session.scalars(query.limit(101))
            )
        query = (
            select(ProviderTurnQueueRow.turn_id)
            .join(EntityRow, EntityRow.id == ProviderTurnQueueRow.turn_id)
            .where(ProviderTurnQueueRow.state.in_(("queued", "running")))
        )
        if project_id is not None:
            query = query.where(EntityRow.engagement_id == project_id)
        blockers.extend(
            "Provider turn " + identity + " is admitted or queued"
            for identity in await session.scalars(query.limit(101))
        )
        states = {
            "chat_turns": (
                "queued",
                "routing",
                "finalizing",
                "waiting_approval",
                "waiting_callback",
            ),
            "chat_goals": ("running",),
            "chat_subagents": ("running",),
            "runs": ("queued", "planning", "running", "waiting_approval", "cancelling"),
            "command_executions": ("running", "interrupted"),
            "harness_sessions": ("starting", "idle", "running", "waiting_approval"),
            "harness_turns": ("queued", "running", "waiting_approval"),
            "native_hook_executions": ("running", "interrupted"),
            "tool_calls": ("running", "waiting_approval", "approved"),
        }
        conditions = [
            (EntityRow.kind == kind)
            & EntityRow.payload["status"].as_string().in_(active)
            for kind, active in states.items()
        ]
        conditions.append(
            (EntityRow.kind == "chat_schedules")
            & EntityRow.payload["enabled"].as_boolean().is_(True)
        )

        conditions.append(
            (EntityRow.kind == "approvals")
            & EntityRow.payload["continuation"]["adapter_status"]
            .as_string()
            .in_(("pending", "unknown"))
        )
        conditions.append(
            (EntityRow.kind == "chat_turns")
            & (EntityRow.payload["status"].as_string() == "interrupted")
            & EntityRow.payload["request_snapshot"]["recovery"]["required"]
            .as_boolean()
            .is_(True)
        )
        query = select(EntityRow.id, EntityRow.kind).where(or_(*conditions))
        if project_id is not None:
            query = query.where(EntityRow.engagement_id == project_id)
        blockers.extend(
            kind + " " + identity + " needs a safe pause or outcome"
            for identity, kind in await session.execute(query.limit(101))
        )
        # A pause or stop recorded during recovery keeps the interrupted
        # owner's claim: its effect is unresolved whatever the status says.
        query = select(EntityRow.id, EntityRow.kind).where(
            EntityRow.kind.in_(("chat_turns", "chat_goals")),
            EntityRow.payload["execution_claim_id"].as_string().is_not(None),
        )
        if project_id is not None:
            query = query.where(EntityRow.engagement_id == project_id)
        blockers.extend(
            kind
            + " "
            + identity
            + " retains an execution claim requiring reconciliation"
            for identity, kind in await session.execute(query.limit(101))
        )
        return blockers

    async def _paused_inventory(self, session, project_id):
        rows = list(
            await session.execute(
                select(EntityRow.id, EntityRow.kind, EntityRow.payload)
                .where(
                    EntityRow.engagement_id == project_id,
                    or_(
                        EntityRow.payload["status"]
                        .as_string()
                        .in_(("paused", "blocked")),
                        (EntityRow.kind == "chat_schedules")
                        & EntityRow.payload["enabled"].as_boolean().is_(False),
                    ),
                )
                .order_by(EntityRow.id)
                .limit(10_001)
            )
        )
        if len(rows) > 10_000:
            raise CoordinationConflict(
                "Paused-work inventory exceeds its shutdown bound"
            )
        return checkpoint_digest({"paused": [list(row) for row in rows]})

    async def prepare_shutdown(self, *, expected_revision: int) -> list[ProjectLease]:
        async with self.coordination._write() as session:
            host = await self._host(session)
            if host.revision != expected_revision:
                # Starting or retiring workers move the revision while this
                # request waits for the host; nothing has changed yet.
                raise DeploymentChanged("deployment changed before shutdown")
            owners = list(
                await session.scalars(
                    select(ProjectOwnershipRow)
                    .where(ProjectOwnershipRow.runtime_id.is_not(None))
                    .order_by(ProjectOwnershipRow.project_id)
                    .with_for_update()
                )
            )
            blockers = await self._shutdown_blockers(session)
            if blockers:
                # Nothing is held yet, so the receipt can say the refusal had
                # no effect and name what must finish, pause or stop first.
                raise ShutdownBlocked(
                    "Shutdown is waiting for this work to finish, pause or stop: "
                    + "; ".join(blockers[:20])
                    + (f"; and {len(blockers) - 20} more" if len(blockers) > 20 else "")
                )
            leases = [
                ProjectLease(row.project_id, row.runtime_id, row.fencing_token)
                for row in owners
                if row.runtime_id is not None
            ]
            for lease in leases:
                try:
                    await self.coordination._check(session, lease, lock=False)
                except LeaseLost as error:
                    raise LeaseLost(
                        f"Project {lease.project_id} is owned by execution worker "
                        f"{lease.runtime_id}, which is no longer live ({error}); "
                        "its recovery must finish before shutdown"
                    ) from error
                process = await session.get(RuntimeProcessRow, lease.runtime_id)
                if process is not None:
                    release = await session.get(RuntimeReleaseRow, process.version)
                    if (
                        release is None
                        or "project.quiesce/v1"
                        not in ReleaseManifest.model_validate(release.manifest).features
                    ):
                        raise ShutdownBlocked(
                            f"Project {lease.project_id}: migrate this idle project "
                            "to a version supporting verified quiescence before shutdown"
                        )
            host.shutdown_requested = True
            host.revision += 1
            host.detail = "New work is held while idle project controllers quiesce."
            return leases

    async def quiesce_checkpoint(
        self, lease: ProjectLease, *, local_blockers: list[str]
    ) -> dict:
        async with self.coordination._write() as session:
            host = await session.get(
                DeploymentHostRow,
                "host",
                with_for_update={"read": True},
                populate_existing=True,
            )
            owner = await session.get(
                ProjectOwnershipRow,
                lease.project_id,
                with_for_update=True,
                populate_existing=True,
            )
            project_retirement = bool(
                owner is not None
                and owner.runtime_id == lease.runtime_id
                and owner.fencing_token == lease.fencing_token
                and owner.retirement_requested
            )
            if not project_retirement and (host is None or not host.shutdown_requested):
                raise CoordinationConflict(
                    "Hold host or project admission before quiescing a project"
                )
            await self.coordination._check(session, lease, lock=True)
            if (
                project_retirement
                and await self._project_has_open_chat(session, lease.project_id)
                and not await self._retiring_for_capacity(session, lease)
            ):
                raise CoordinationConflict(
                    "An open chat still requires this project's worker"
                )
            if project_retirement and await self._project_has_pending_chat_start(
                session, lease.project_id
            ):
                raise CoordinationConflict("A chat start is pending for this project")
            if project_retirement and await self._project_has_pending_storage_alert(
                session, lease.project_id
            ):
                raise CoordinationConflict(_PENDING_STORAGE_ALERT)
            handoff = await session.get(ProjectHandoffRow, lease.project_id)
            if handoff is not None and handoff.phase != HandoffPhase.VERIFIED:
                raise CoordinationConflict(
                    "Resolve the project's existing handoff before quiescence"
                )
            blockers = [
                *local_blockers,
                *await self._shutdown_blockers(
                    session,
                    lease.project_id,
                    allow_queued_commands=project_retirement,
                ),
            ]
            if blockers:
                raise CoordinationConflict("; ".join(blockers[:20]))
            result = {
                "project_id": lease.project_id,
                "paused_sha256": await self._paused_inventory(
                    session, lease.project_id
                ),
            }
            await self.coordination._check(session, lease, lock=False)
            return result

    @staticmethod
    async def _project_has_open_chat(session, project_id: str) -> bool:
        return (
            await session.scalar(
                select(EntityRow.id)
                .where(
                    EntityRow.engagement_id == project_id,
                    EntityRow.kind == "chat_sessions",
                    EntityRow.payload["metadata"]["archived_at"].as_string().is_(None),
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    async def _retiring_for_capacity(session, lease: ProjectLease) -> bool:
        """Whether this exact owner's retirement makes room for another project.

        Such a retirement may release a worker whose project still has open
        chats: they are saved state, and the project gets a worker again when
        its chats are used or viewed.
        """
        latest = await session.scalar(
            select(DeploymentReceiptRow)
            .where(
                DeploymentReceiptRow.action == "project.retirement_requested",
                DeploymentReceiptRow.result["project_id"].as_string()
                == lease.project_id,
                DeploymentReceiptRow.result["runtime_id"].as_string()
                == lease.runtime_id,
                DeploymentReceiptRow.result["fencing_token"].as_integer()
                == lease.fencing_token,
            )
            .order_by(
                DeploymentReceiptRow.occurred_at_ms.desc(),
                DeploymentReceiptRow.id.desc(),
            )
            .limit(1)
        )
        return latest is not None and latest.result.get("reason") == "capacity"

    @staticmethod
    async def _project_has_pending_chat_start(session, project_id: str) -> bool:
        return (
            await session.scalar(
                select(RuntimeCommandRow.id)
                .where(
                    RuntimeCommandRow.project_id == project_id,
                    or_(
                        RuntimeCommandRow.chat_start.is_(True),
                        RuntimeCommandRow.name.in_(
                            ("chat.accept/v1", "chat.resume/v1")
                        ),
                    ),
                    RuntimeCommandRow.state.in_(("queued", "running", "uncertain")),
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    async def _project_has_pending_storage_alert(session, project_id: str) -> bool:
        return bool(
            await session.scalar(
                select(
                    pending_storage_dispatches(project_id, session.bind.dialect.name)
                )
            )
        )

    async def request_project_retirement(
        self, lease: ProjectLease, *, for_capacity: bool = False
    ) -> bool:
        """Fence one idle project's admission before its controller quiesces.

        A false result means an open chat, a pending chat start or queued
        project work still needs the owner. Other unresolved project work raises
        a blocker and leaves the ownership grant unchanged. ``for_capacity``
        retires an owner whose project has only open chats, to give its memory
        to a project waiting for a worker.
        """
        async with self.coordination._write() as session:
            owner = await session.get(
                ProjectOwnershipRow,
                lease.project_id,
                with_for_update=True,
                populate_existing=True,
            )
            if owner is None or (owner.runtime_id, owner.fencing_token) != (
                lease.runtime_id,
                lease.fencing_token,
            ):
                raise CoordinationConflict(
                    "Only the exact current project owner can request retirement"
                )
            await self.coordination._check(session, lease, lock=False)
            if not owner.retirement_requested:
                if not for_capacity and await self._project_has_open_chat(
                    session, lease.project_id
                ):
                    return False
                if await self._project_has_pending_chat_start(
                    session, lease.project_id
                ):
                    return False
                # Queued project work (a rename or delete) would be stranded
                # by releasing its only worker. Submit and this check share
                # the ownership row lock, so none can be queued after the hold.
                if await session.scalar(
                    select(project_worker_commands(lease.project_id))
                ):
                    return False
                # A recorded storage alert opens its incident chat from this
                # worker's dispatch loop; retiring first would lose it.
                if await self._project_has_pending_storage_alert(
                    session, lease.project_id
                ):
                    return False
                host = await session.get(DeploymentHostRow, "host")
                if host is not None and host.shutdown_requested:
                    raise CoordinationConflict(
                        "Host shutdown already holds project admission"
                    )
                blockers = await self._shutdown_blockers(
                    session, lease.project_id, allow_queued_commands=True
                )
                if blockers:
                    raise CoordinationConflict("; ".join(blockers[:20]))
                owner.retirement_requested = True
                owner.admission_requested = False
            handoff = await session.get(ProjectHandoffRow, lease.project_id)
            if handoff is not None and handoff.phase != HandoffPhase.VERIFIED:
                raise CoordinationConflict(
                    "Resolve the project's existing handoff before retirement"
                )
            await self.coordination._check(session, lease, lock=False)
            await self._receipt(
                session,
                "project.retirement_requested",
                {
                    "project_id": lease.project_id,
                    "runtime_id": lease.runtime_id,
                    "fencing_token": lease.fencing_token,
                    **({"reason": "capacity"} if for_capacity else {}),
                },
            )
            return True

    async def abort_project_retirement(
        self, lease: ProjectLease, *, worker_status: dict
    ) -> None:
        """Release a hold only after the worker refuses before cleanup.

        A worker refuses before stopping anything in every state short of
        quiescence: ready, still starting or prepared after a handoff, or
        already failed. Keeping the hold for those would fail the project's
        reads and chat admission with nothing left to wait for.
        """
        blockers = worker_status.get("quiesce_blockers")
        if (
            worker_status.get("runtime_id") != lease.runtime_id
            or worker_status.get("state")
            not in {"ready", "starting", "prepared", "failed"}
            or not isinstance(blockers, list)
            or not blockers
            or worker_status.get("quiesce_checkpoint") is not None
        ):
            raise CoordinationConflict(
                "Only a signed quiescence refusal from a worker that has not "
                "begun quiescing can abort retirement"
            )
        async with self.coordination._write() as session:
            owner = await session.get(
                ProjectOwnershipRow,
                lease.project_id,
                with_for_update=True,
                populate_existing=True,
            )
            if (
                owner is None
                or (owner.runtime_id, owner.fencing_token)
                != (lease.runtime_id, lease.fencing_token)
                or not owner.retirement_requested
            ):
                raise CoordinationConflict(
                    "The exact project retirement hold is no longer active"
                )
            await self.coordination._check(session, lease, lock=False)
            owner.retirement_requested = False
            owner.admission_requested = False
            await self._receipt(
                session,
                "project.retirement_aborted",
                {
                    "project_id": lease.project_id,
                    "runtime_id": lease.runtime_id,
                    "fencing_token": lease.fencing_token,
                    "blockers": [str(item)[:400] for item in blockers[:20]],
                },
            )

    async def release_quiesced_project(
        self, lease: ProjectLease, *, checkpoint: dict
    ) -> None:
        async with self.coordination._write() as session:
            host = await session.get(
                DeploymentHostRow,
                "host",
                with_for_update={"read": True},
                populate_existing=True,
            )
            owner = await session.get(
                ProjectOwnershipRow, lease.project_id, with_for_update=True
            )
            if (
                owner is not None
                and owner.runtime_id is None
                and owner.fencing_token == lease.fencing_token
            ):
                return
            project_retirement = bool(
                owner is not None
                and owner.runtime_id == lease.runtime_id
                and owner.fencing_token == lease.fencing_token
                and owner.retirement_requested
            )
            if (
                not project_retirement and (host is None or not host.shutdown_requested)
            ) or checkpoint.get("project_id") != lease.project_id:
                raise CoordinationConflict(
                    "Quiesced project proof does not match held admission"
                )
            await self.coordination._check(session, lease, lock=False)
            if await self._shutdown_blockers(
                session,
                lease.project_id,
                allow_queued_commands=project_retirement,
            ) or (
                project_retirement
                and await self._project_has_pending_storage_alert(
                    session, lease.project_id
                )
            ):
                raise CoordinationConflict(
                    "Project acquired work while quiescing; retain its owner"
                )
            if checkpoint.get("paused_sha256") != await self._paused_inventory(
                session, lease.project_id
            ):
                raise CoordinationConflict(
                    "Previously paused work changed during quiescence"
                )
            await self.coordination._check(session, lease, lock=False)
            owner = await session.get(ProjectOwnershipRow, lease.project_id)
            assert owner is not None
            owner.runtime_id, owner.lease_expires_at_ms = None, 0
            if project_retirement:
                owner.retirement_requested = False
                owner.admission_requested = False
            # The worker's application is closed for good: an execution process
            # attaches once. Draining keeps any later claim off it and stops it
            # counting as an unassigned process until it is retired.
            process = await session.get(
                RuntimeProcessRow, lease.runtime_id, with_for_update=True
            )
            if process is not None and process.state == "ready":
                process.state = "draining"
                process.updated_at_ms = await self.coordination._clock_ms(session)
            await self._receipt(
                session,
                "project.quiesced",
                {
                    "project_id": lease.project_id,
                    "runtime_id": lease.runtime_id,
                    "fencing_token": lease.fencing_token,
                    "reason": (
                        "idle_chat_project" if project_retirement else "host_shutdown"
                    ),
                },
            )

    async def abort_shutdown(
        self,
        *,
        detail: str = "Shutdown is incomplete; retained work remains available.",
    ) -> None:
        async with self.coordination._write() as session:
            host = await self._host(session)
            host.shutdown_requested = False
            host.revision += 1
            host.detail = detail[:4000]

    async def begin_shutdown(self, *, expected_revision: int) -> list[str]:
        async with self.coordination._write() as session:
            host = await self._host(session)
            if host.revision != expected_revision:
                raise CoordinationConflict("deployment changed before shutdown")
            processes = list(
                await session.scalars(
                    select(RuntimeProcessRow)
                    .where(RuntimeProcessRow.state != "retired")
                    .order_by(RuntimeProcessRow.runtime_id)
                    .with_for_update()
                )
            )
            if any(
                row.state in {"reserved", "starting", "aborting"} for row in processes
            ):
                raise CoordinationConflict(
                    "reconcile pending worker startup before shutdown"
                )
            owner = await session.scalar(
                select(ProjectOwnershipRow.project_id)
                .where(ProjectOwnershipRow.runtime_id.is_not(None))
                .limit(1)
            )
            blockers = await self._shutdown_blockers(session)
            if owner is not None or blockers:
                raise CoordinationConflict(
                    "Project work, unresolved effects or charged resources prevent shutdown"
                    + (": " + "; ".join(blockers[:10]) if blockers else "")
                )
            host.shutdown_requested, host.api_runtime_id = True, None
            host.revision += 1
            host.detail = "Coordinated shutdown has fenced all new admissions."
            for row in processes:
                row.state = "draining"
            await self._receipt(
                session,
                "host.shutdown_admitted",
                {"runtimes": [row.runtime_id for row in processes]},
            )
            return [row.runtime_id for row in processes]

    async def resume_after_shutdown(self) -> None:
        async with self.coordination._write() as session:
            host = await self._host(session)
            remaining = await session.scalar(
                select(RuntimeProcessRow.runtime_id)
                .where(RuntimeProcessRow.state != "retired")
                .limit(1)
            )
            if host.shutdown_requested and remaining is None:
                host.shutdown_requested = False
                host.revision += 1
                host.detail = "The prior coordinated shutdown completed; the host can admit workers."

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

    @staticmethod
    async def _admitted_turns(session, project_id):
        turns = list(
            await session.scalars(
                select(ProviderTurnQueueRow.turn_id)
                .join(EntityRow, EntityRow.id == ProviderTurnQueueRow.turn_id)
                .where(
                    EntityRow.engagement_id == project_id,
                    EntityRow.kind == "chat_turns",
                    ProviderTurnQueueRow.state == "running",
                )
                .order_by(ProviderTurnQueueRow.turn_id)
                .limit(65)
            )
        )
        if len(turns) > 64:
            raise CoordinationConflict(
                "Verify the project's provider inventory before handoff; more than 64 admissions remain"
            )
        return turns

    @staticmethod
    async def _has_participants(session, project_id):
        return (
            await session.scalar(
                select(EntityRow.id)
                .where(
                    EntityRow.engagement_id == project_id,
                    EntityRow.kind.in_(
                        ("approvals", "chat_schedules", "chat_goals", "chat_subagents")
                    ),
                )
                .limit(1)
            )
            is not None
        )

    async def project_requires_participants(self, project_id: str) -> bool:
        async with self.database.session() as session:
            return await self._has_participants(session, project_id)

    async def _participants_supported(self, session, project_id, *runtime_ids):
        from .runtime_context_requirements import require_runtime_context

        await require_runtime_context(session, project_id, runtime_ids[-1])
        if not await self._has_participants(session, project_id):
            return True
        for runtime_id in runtime_ids:
            process = await session.get(RuntimeProcessRow, runtime_id)
            release = (
                await session.get(RuntimeReleaseRow, process.version)
                if process
                else None
            )
            if (
                release is None
                or "project.participants/v1"
                not in ReleaseManifest.model_validate(release.manifest).features
            ):
                return False
        return True

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
            if not await self._participants_supported(
                session, project_id, owner.runtime_id, target_runtime_id
            ):
                raise CoordinationConflict(
                    "This project needs participant restoration support on both execution versions"
                )
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
                admitted_turn_ids=await self._admitted_turns(session, project_id),
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
            if not await self._participants_supported(
                session, lease.project_id, lease.runtime_id, row.target_runtime_id
            ):
                observed.append(
                    "Both execution versions must support durable participant restoration"
                )
                pinned = True
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
                    finishing_provider = (
                        resource.kind == "provider_turn"
                        and resource.state == "active"
                        and resource.restoration == "durable"
                    )
                    pinned = (
                        pinned
                        or resource.restoration == "pinned"
                        or (resource.kind not in supported and not finishing_provider)
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
            capacity = int(
                await session.scalar(
                    select(func.count())
                    .select_from(RuntimeCapacityReservationRow)
                    .where(
                        RuntimeCapacityReservationRow.project_id == lease.project_id,
                        RuntimeCapacityReservationRow.state == "held",
                    )
                )
                or 0
            )
            if capacity:
                observed.append(
                    f"{capacity} host capacity reservation(s) still require positive completion evidence"
                )
            admissions = await self._admitted_turns(session, lease.project_id)
            if admissions:
                observed.append(
                    f"{len(admissions)} running provider admission(s) still require their current owner"
                )
            if not row.admission_held and not pinned:
                # The old runtime remained usable while pinned. Re-draining
                # must include everything it admitted during that interval.
                row.admitted_turn_ids = await self._admitted_turns(
                    session, lease.project_id
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
        from .runtime_project_handoff import snapshot_project_checkpoint
        from .runtime_checkpoint_generation import assert_checkpoint_generation

        scanned, generation = await snapshot_project_checkpoint(self, project_id)
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
            await assert_checkpoint_generation(session, project_id, generation)
            await self.coordination._check(session, old, lock=False)
            now = await self.coordination._clock_ms(session)
            await self.coordination._live_execution(
                session, handoff.target_runtime_id, now
            )
            if not await self._participants_supported(
                session,
                project_id,
                handoff.source_runtime_id,
                handoff.target_runtime_id,
            ):
                raise CoordinationConflict(
                    "The target cannot restore this project's durable participants"
                )
            target_release = await session.get(
                RuntimeReleaseRow, handoff.target_version
            )
            assert target_release is not None
            required = set(
                await session.scalars(
                    select(RuntimeCommandRow.name)
                    .where(
                        RuntimeCommandRow.project_id == project_id,
                        RuntimeCommandRow.state.in_(("queued", "running", "uncertain")),
                        RuntimeCommandRow.name.in_(
                            ("chat.accept/v1", "chat.resume/v1", STORAGE_REPORT_COMMAND)
                        ),
                    )
                    .distinct()
                )
            )
            if not required <= set(
                ReleaseManifest.model_validate(target_release.manifest).features
            ):
                raise CoordinationConflict(
                    "The target cannot read this project's accepted chat requests"
                    " or storage reports"
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
            capacity = await session.scalar(
                select(RuntimeCapacityReservationRow.id)
                .where(
                    RuntimeCapacityReservationRow.project_id == project_id,
                    RuntimeCapacityReservationRow.state == "held",
                )
                .limit(1)
            )
            admissions = await self._admitted_turns(session, project_id)
            if (
                unfinished is not None
                or resources is not None
                or capacity is not None
                or admissions
            ):
                raise CoordinationConflict(
                    "project changed after its transferable checkpoint"
                )
            if handoff.checkpoint.get("format") == "nebula.project-checkpoint/v1":
                if scanned != handoff.checkpoint:
                    raise CoordinationConflict(
                        "Project state changed after its handoff checkpoint; verify a fresh boundary"
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
            if row.checkpoint.get("format") == "nebula.project-recovery/v1":
                raise CoordinationConflict(
                    "use recovery verification for an expired-owner claim"
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

    async def _recovery_blockers(self, session, project_id, target_runtime_id):
        reasons = []
        # An approval whose adapter delivery is unconfirmed stays flagged on
        # the approval itself. Its dead owner can never confirm it, and the
        # decision is never sent again, so it does not hold project recovery;
        # reconciliation records a pending delivery as unknown.
        for identity in await session.scalars(
            select(EntityRow.id)
            .where(
                EntityRow.engagement_id == project_id,
                EntityRow.kind.in_(("chat_turns", "chat_goals")),
                EntityRow.payload["execution_claim_id"].as_string().is_not(None),
            )
            .limit(101)
        ):
            reasons.append(
                f"Conversation work {identity} retains an execution claim requiring reconciliation"
            )
        for row in await session.scalars(
            select(RuntimeCommandRow).where(
                RuntimeCommandRow.project_id == project_id,
                RuntimeCommandRow.state.in_(("running", "uncertain")),
            )
        ):
            reasons.append(f"Command {row.id} needs an explicit outcome")
        for row in await session.scalars(
            select(RuntimeResourceRow).where(
                RuntimeResourceRow.project_id == project_id,
                RuntimeResourceRow.state != "closed",
            )
        ):
            if row.state != "restorable" or row.restoration == "pinned":
                reasons.append(
                    row.detail or f"Resource {row.id} needs completion evidence"
                )
        for identity in await session.scalars(
            select(RuntimeCapacityReservationRow.id).where(
                RuntimeCapacityReservationRow.project_id == project_id,
                RuntimeCapacityReservationRow.state == "held",
            )
        ):
            reasons.append(
                f"Capacity reservation {identity} still needs positive completion evidence"
            )
        running = await session.scalar(
            select(ProviderTurnQueueRow.turn_id)
            .join(EntityRow, EntityRow.id == ProviderTurnQueueRow.turn_id)
            .where(
                EntityRow.engagement_id == project_id,
                ProviderTurnQueueRow.state == "running",
            )
            .limit(1)
        )
        if running is not None:
            reasons.append(
                f"Provider turn {running} needs explicit interrupted-effect reconciliation"
            )
        target = await session.get(RuntimeProcessRow, target_runtime_id)
        if target is not None:
            release = await session.get(RuntimeReleaseRow, target.version)
            supported = (
                set(
                    ReleaseManifest.model_validate(
                        release.manifest
                    ).restorable_resources
                )
                if release
                else set()
            )
            for resource in await session.scalars(
                select(RuntimeResourceRow).where(
                    RuntimeResourceRow.project_id == project_id,
                    RuntimeResourceRow.state == "restorable",
                )
            ):
                if resource.kind not in supported:
                    reasons.append(
                        f"The target cannot restore {resource.kind} {resource.id}"
                    )
        return list(dict.fromkeys(reasons))[:100]

    async def begin_recovery(
        self,
        project_id: str,
        target_runtime_id: str,
        *,
        expected_owner: ProjectLease,
        expected_handoff_revision: int | None,
        operation_id: str,
        actor_id: str,
    ) -> ProjectLease:
        """Claim an expired incarnation with admission held and no effect replay."""
        if expected_owner.project_id != project_id:
            raise CoordinationConflict("recovery owner belongs to another project")
        if any(not value or len(value) > 200 for value in (operation_id, actor_id)):
            raise ValueError(
                "recovery requires a bounded operation and authenticated actor"
            )
        from .runtime_project_handoff import snapshot_project_checkpoint
        from .runtime_checkpoint_generation import assert_checkpoint_generation

        scanned, generation = await snapshot_project_checkpoint(self, project_id)
        async with self.coordination._write() as session:
            await self._host(session)
            prior = await session.get(RuntimeRecoveryRow, operation_id)
            owner = await session.get(
                ProjectOwnershipRow, project_id, with_for_update=True
            )
            if prior is not None:
                if (
                    prior.project_id != project_id
                    or prior.target_runtime_id != target_runtime_id
                    or prior.source_runtime_id != expected_owner.runtime_id
                    or prior.source_fencing_token != expected_owner.fencing_token
                    or prior.expected_handoff_revision != expected_handoff_revision
                    or prior.actor_id != actor_id
                ):
                    raise CoordinationConflict(
                        "recovery operation ID identifies another request"
                    )
                lease = ProjectLease(
                    project_id, target_runtime_id, prior.target_fencing_token
                )
                await self.coordination._check(session, lease, lock=False)
                return lease
            if owner is None or (owner.runtime_id, owner.fencing_token) != (
                expected_owner.runtime_id,
                expected_owner.fencing_token,
            ):
                raise CoordinationConflict(
                    "project ownership changed; read fresh recovery state"
                )
            await assert_checkpoint_generation(session, project_id, generation)
            now = await self.coordination._clock_ms(session)
            if owner.lease_expires_at_ms > now:
                raise CoordinationConflict("the project owner is still live")
            if target_runtime_id == expected_owner.runtime_id:
                raise CoordinationConflict(
                    "recovery requires a new process incarnation"
                )
            await self.coordination._live_execution(session, target_runtime_id, now)
            handoff = await session.get(
                ProjectHandoffRow, project_id, with_for_update=True
            )
            if (handoff.revision if handoff else None) != expected_handoff_revision:
                raise CoordinationConflict("handoff changed; read fresh recovery state")
            for previous in await session.scalars(
                select(RuntimeRecoveryRow).where(
                    RuntimeRecoveryRow.project_id == project_id,
                    RuntimeRecoveryRow.phase.in_(("held", "prepared")),
                )
            ):
                previous.phase = "blocked"
                previous.blockers = [
                    "Its target expired; recovery continued with a new incarnation"
                ]
                previous.revision += 1
                previous.updated_at_ms = now
            # A storage report records atomically and idempotently and its
            # response commits with its outcome, so it simply runs again.
            for report in list(
                await session.scalars(
                    select(RuntimeCommandRow).where(
                        RuntimeCommandRow.project_id == project_id,
                        RuntimeCommandRow.state == "running",
                        RuntimeCommandRow.name == STORAGE_REPORT_COMMAND,
                    )
                )
            ):
                requeue_interrupted_storage_report(report)
            await session.flush()
            await session.execute(
                update(RuntimeCommandRow)
                .where(
                    RuntimeCommandRow.project_id == project_id,
                    RuntimeCommandRow.state == "running",
                )
                .values(state="uncertain")
            )
            for resource in await session.scalars(
                select(RuntimeResourceRow).where(
                    RuntimeResourceRow.project_id == project_id,
                    RuntimeResourceRow.state == "active",
                )
            ):
                resource.state = "uncertain"
                resource.updated_at_ms = now
            target = await session.get(RuntimeProcessRow, target_runtime_id)
            if target is None:
                raise CoordinationConflict(
                    "recovery requires a retained execution worker"
                )
            if not await self._participants_supported(
                session, project_id, target_runtime_id
            ):
                raise CoordinationConflict(
                    "The recovery target cannot restore this project's durable participants"
                )
            release = await session.get(RuntimeReleaseRow, target.version)
            if (
                release is None
                or "project.handoff/v1"
                not in ReleaseManifest.model_validate(release.manifest).features
            ):
                raise CoordinationConflict(
                    "the recovery target must support prepared project restoration"
                )
            checkpoint = {
                "format": "nebula.project-recovery/v1",
                "operation_id": operation_id,
                "project_state": scanned,
            }
            blockers = await self._recovery_blockers(
                session, project_id, target_runtime_id
            )
            # Context and checkpoint reads must not consume the new owner's
            # lease before the recovery grant is committed.
            now = await self.coordination._live_execution(
                session, target_runtime_id, now
            )
            owner.runtime_id, owner.fencing_token, owner.lease_expires_at_ms = (
                target_runtime_id,
                owner.fencing_token + 1,
                now + LEASE_MS,
            )
            values = dict(
                project_id=project_id,
                revision=(handoff.revision + 1) if handoff else 1,
                source_runtime_id=expected_owner.runtime_id,
                source_fencing_token=expected_owner.fencing_token,
                target_runtime_id=target_runtime_id,
                target_version=target.version,
                phase=HandoffPhase.TRANSFERRED,
                admission_held=True,
                blockers=blockers,
                checkpoint=checkpoint,
                admitted_turn_ids=[],
                updated_at_ms=now,
            )
            if handoff is None:
                session.add(ProjectHandoffRow(**values))
            else:
                for key, value in values.items():
                    setattr(handoff, key, value)
            session.add(
                RuntimeRecoveryRow(
                    operation_id=operation_id,
                    actor_id=actor_id,
                    project_id=project_id,
                    revision=1,
                    source_runtime_id=expected_owner.runtime_id,
                    source_fencing_token=expected_owner.fencing_token,
                    target_runtime_id=target_runtime_id,
                    target_fencing_token=owner.fencing_token,
                    expected_handoff_revision=expected_handoff_revision,
                    phase="held",
                    checkpoint=checkpoint,
                    blockers=blockers,
                    created_at_ms=now,
                    updated_at_ms=now,
                )
            )
            await self._receipt(
                session,
                "project.recovery_claimed",
                {
                    "operation_id": operation_id,
                    "project_id": project_id,
                    "actor_id": actor_id,
                    "runtime_id": target_runtime_id,
                    "fencing_token": owner.fencing_token,
                    "blockers": blockers,
                },
            )
            return ProjectLease(project_id, target_runtime_id, owner.fencing_token)

    async def recovery_status(self, project_id: str) -> dict | None:
        async with self.database.session() as session:
            row = await session.scalar(
                select(RuntimeRecoveryRow)
                .where(
                    RuntimeRecoveryRow.project_id == project_id,
                )
                .order_by(
                    RuntimeRecoveryRow.target_fencing_token.desc(),
                    RuntimeRecoveryRow.operation_id,
                )
                .limit(1)
            )
            if row is None:
                return None
            return {
                "operation_id": row.operation_id,
                "phase": row.phase,
                "revision": row.revision,
                "lease": {
                    "project_id": row.project_id,
                    "runtime_id": row.target_runtime_id,
                    "fencing_token": row.target_fencing_token,
                },
                "blockers": await self._recovery_blockers(
                    session, project_id, row.target_runtime_id
                )
                if row.phase in {"held", "prepared"}
                else row.blockers,
                "checkpoint_digest": checkpoint_digest(row.checkpoint),
            }

    async def pending_recovery_operation(self, lease: ProjectLease) -> str | None:
        """The unverified recovery this exact incarnation was granted, if any."""
        async with self.database.session() as session:
            return await session.scalar(
                select(RuntimeRecoveryRow.operation_id)
                .where(
                    RuntimeRecoveryRow.project_id == lease.project_id,
                    RuntimeRecoveryRow.target_runtime_id == lease.runtime_id,
                    RuntimeRecoveryRow.target_fencing_token == lease.fencing_token,
                    RuntimeRecoveryRow.phase.in_(("held", "prepared")),
                )
                .limit(1)
            )

    async def prepare_recovery(self, lease: ProjectLease, *, operation_id: str) -> str:
        from .runtime_project_handoff import snapshot_project_checkpoint
        from .runtime_checkpoint_generation import assert_checkpoint_generation

        scanned, generation = await snapshot_project_checkpoint(
            self, lease.project_id, lease=lease
        )
        async with self.coordination.fenced(lease) as session:
            await assert_checkpoint_generation(session, lease.project_id, generation)
            row = await session.get(RuntimeRecoveryRow, operation_id)
            if (
                row is None
                or (row.project_id, row.target_runtime_id, row.target_fencing_token)
                != (lease.project_id, lease.runtime_id, lease.fencing_token)
                or row.phase not in {"held", "prepared"}
            ):
                raise CoordinationConflict(
                    "this incarnation does not own pending recovery"
                )
            if not await self._participants_supported(
                session, lease.project_id, lease.runtime_id
            ):
                raise CoordinationConflict(
                    "The recovery target cannot restore this project's durable participants"
                )
            blockers = await self._recovery_blockers(
                session, lease.project_id, lease.runtime_id
            )
            if blockers:
                raise RecoveryBlocked(blockers)
            row.checkpoint = {
                "format": "nebula.project-recovery/v1",
                "operation_id": operation_id,
                "project_state": scanned,
            }
            row.phase, row.blockers, row.revision = "prepared", [], row.revision + 1
            row.updated_at_ms = await self.coordination._clock_ms(session)
            handoff = await session.get(ProjectHandoffRow, lease.project_id)
            if handoff is None or handoff.target_runtime_id != lease.runtime_id:
                raise CoordinationConflict("recovery admission record changed")
            handoff.checkpoint, handoff.blockers = row.checkpoint, []
            handoff.revision += 1
            return checkpoint_digest(row.checkpoint)

    async def verify_recovery(
        self, lease: ProjectLease, *, operation_id: str, restored_checkpoint_digest: str
    ) -> None:
        from .runtime_project_handoff import snapshot_project_checkpoint
        from .runtime_checkpoint_generation import assert_checkpoint_generation

        # An already-verified recovery needs only its identity and digest. The
        # project scan below serves only the prepared -> verified step. Running
        # it first made each supervisor handoff pass re-read the whole project,
        # and a busy project changes during that scan, so the pass never
        # succeeded and the loop rescanned the project indefinitely.
        async with self.coordination.fenced(lease) as session:
            row = await session.get(RuntimeRecoveryRow, operation_id)
            if (
                row is not None
                and row.phase == "verified"
                and (row.project_id, row.target_runtime_id, row.target_fencing_token)
                == (lease.project_id, lease.runtime_id, lease.fencing_token)
                and checkpoint_digest(row.checkpoint) == restored_checkpoint_digest
            ):
                return
        scanned, generation = await snapshot_project_checkpoint(
            self, lease.project_id, lease=lease
        )
        async with self.coordination.fenced(lease) as session:
            await assert_checkpoint_generation(session, lease.project_id, generation)
            row = await session.get(RuntimeRecoveryRow, operation_id)
            if (
                row is None
                or (row.project_id, row.target_runtime_id, row.target_fencing_token)
                != (lease.project_id, lease.runtime_id, lease.fencing_token)
                or row.phase not in {"prepared", "verified"}
            ):
                raise CoordinationConflict("this incarnation has not prepared recovery")
            if checkpoint_digest(row.checkpoint) != restored_checkpoint_digest:
                raise CoordinationConflict(
                    "worker did not restore the recovery checkpoint"
                )
            if row.phase == "verified":
                return
            if not await self._participants_supported(
                session, lease.project_id, lease.runtime_id
            ):
                raise CoordinationConflict(
                    "The recovery target cannot restore this project's durable participants"
                )
            if await self._recovery_blockers(
                session, lease.project_id, lease.runtime_id
            ):
                raise CoordinationConflict(
                    "recovery acquired unresolved effects before verification"
                )
            if row.checkpoint["project_state"] != scanned:
                raise CoordinationConflict(
                    "project changed before recovery verification"
                )
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
            row.phase, row.blockers, row.revision = "verified", [], row.revision + 1
            row.updated_at_ms = await self.coordination._clock_ms(session)
            handoff = await session.get(ProjectHandoffRow, lease.project_id)
            if handoff is None or handoff.target_runtime_id != lease.runtime_id:
                raise CoordinationConflict("recovery admission record changed")
            handoff.phase, handoff.admission_held, handoff.blockers = (
                HandoffPhase.VERIFIED,
                False,
                [],
            )
            handoff.revision += 1
            await self._receipt(
                session,
                "project.recovery_verified",
                {
                    "operation_id": operation_id,
                    "project_id": lease.project_id,
                    "runtime_id": lease.runtime_id,
                    "fencing_token": lease.fencing_token,
                },
            )

    async def relinquish_failed_restoration(self, lease: ProjectLease) -> None:
        """Expire a cleaned pre-activation grant; keep all evidence and its fence."""
        from .runtime_project_handoff import snapshot_project_checkpoint
        from .runtime_checkpoint_generation import assert_checkpoint_generation

        scanned, generation = await snapshot_project_checkpoint(
            self, lease.project_id, lease=lease
        )
        async with self.coordination._write() as session:
            await self.coordination._check(session, lease, lock=True)
            await assert_checkpoint_generation(session, lease.project_id, generation)
            handoff = await session.get(ProjectHandoffRow, lease.project_id)
            if (
                handoff is None
                or handoff.phase != HandoffPhase.TRANSFERRED
                or handoff.target_runtime_id != lease.runtime_id
            ):
                raise CoordinationConflict(
                    "only an unverified restoration may relinquish"
                )
            expected = handoff.checkpoint.get("project_state", handoff.checkpoint)
            if expected != scanned:
                raise CoordinationConflict(
                    "restoration changed project state; explicit recovery is required"
                )
            active = await session.scalar(
                select(RuntimeResourceRow.id)
                .where(
                    RuntimeResourceRow.runtime_id == lease.runtime_id,
                    RuntimeResourceRow.project_id == lease.project_id,
                    RuntimeResourceRow.state != "closed",
                )
                .limit(1)
            )
            command = await session.scalar(
                select(RuntimeCommandRow.id)
                .where(
                    RuntimeCommandRow.runtime_id == lease.runtime_id,
                    RuntimeCommandRow.project_id == lease.project_id,
                )
                .limit(1)
            )
            capacity = await session.scalar(
                select(RuntimeCapacityReservationRow.id)
                .where(
                    RuntimeCapacityReservationRow.runtime_id == lease.runtime_id,
                    RuntimeCapacityReservationRow.project_id == lease.project_id,
                    RuntimeCapacityReservationRow.state == "held",
                )
                .limit(1)
            )
            if active or command or capacity:
                raise CoordinationConflict(
                    "restoration retains admitted effects or capacity"
                )
            # Recheck immediately before intentional expiry, keeping the same
            # project lock through audits and revocation. There is no ordinary
            # fenced post-check because this transaction expires its own grant.
            await self.coordination._check(session, lease, lock=False)
            owner = await session.get(
                ProjectOwnershipRow, lease.project_id, with_for_update=True
            )
            if owner is None or (owner.runtime_id, owner.fencing_token) != (
                lease.runtime_id,
                lease.fencing_token,
            ):
                raise CoordinationConflict(
                    "restoration grant changed before relinquishment"
                )
            owner.lease_expires_at_ms = 0
            await self._receipt(
                session,
                "project.restore_relinquished",
                {
                    "project_id": lease.project_id,
                    "runtime_id": lease.runtime_id,
                    "fencing_token": lease.fencing_token,
                },
            )

    async def retirement_blockers(self, runtime_id: str) -> list[str]:
        async with self.database.session() as session:
            return await self._retirement_blockers(session, runtime_id)

    async def _retirement_blockers(self, session, runtime_id: str) -> list[str]:
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
            *("Project " + item + " still belongs to this worker" for item in owners),
            *("Resource " + item + " remains resident" for item in resources),
            *("Command " + item + " is unfinished or uncertain" for item in commands),
            *("Capacity reservation " + item + " remains held" for item in capacity),
        ]

    async def retirement_readback(self, runtime_id: str) -> WorkerIdentity | None:
        """Read a completed exact retirement; never admit or repeat an effect."""
        async with self.coordination._write() as session:
            await self._host(session)
            return await self._retirement_readback(session, runtime_id)

    async def _retirement_readback(
        self, session, runtime_id: str
    ) -> WorkerIdentity | None:
        row = await session.get(RuntimeProcessRow, runtime_id)
        runtime = await session.get(RuntimeInstanceRow, runtime_id)
        if (
            row is None
            or row.state != "retired"
            or row.pid <= 0
            or not row.process_birth
            or not row.endpoint
            or runtime is None
            or runtime.lease_expires_at_ms != 0
            or (runtime.version, runtime.role) != (row.version, row.role)
            or same_process(row.pid, row.process_birth)
            or await self._retirement_blockers(session, runtime_id)
        ):
            return None
        receipt = await session.scalar(
            select(DeploymentReceiptRow)
            .where(
                DeploymentReceiptRow.action == "worker.retired",
                DeploymentReceiptRow.result["runtime_id"].as_string() == runtime_id,
            )
            .order_by(DeploymentReceiptRow.occurred_at_ms.desc())
            .limit(1)
        )
        if receipt is None:
            return None
        identity = WorkerIdentity(
            runtime_id=row.runtime_id,
            version=row.version,
            role="api" if row.role == "api" else "execution",
            pid=row.pid,
            process_birth=row.process_birth,
            endpoint=row.endpoint,
        )
        # Runtime IDs identify immutable incarnations and cannot revive after
        # retirement. Older receipts bind that row by ID/version/role; newer
        # receipts additionally retain the complete process identity.
        proof = receipt.result
        exact = identity.model_dump(mode="json")
        if any(
            proof.get(key) != exact[key] for key in ("runtime_id", "version", "role")
        ):
            return None
        if any(
            key in proof and proof[key] != exact[key]
            for key in ("pid", "process_birth", "endpoint")
        ):
            return None
        return identity

    async def retire_missing_api(self, identity: WorkerIdentity) -> None:
        """Release only an exactly identified API proven absent by its host.

        The host calls this after same-namespace observation. A missing socket
        alone never establishes process absence; execution owners use their
        separate fenced recovery protocol.
        """
        if identity.role != "api" or same_process(identity.pid, identity.process_birth):
            raise CoordinationConflict(
                "Only a positively absent API incarnation can be released"
            )
        async with self.coordination._write() as session:
            host = await self._host(session)
            row = await session.get(
                RuntimeProcessRow, identity.runtime_id, with_for_update=True
            )
            if row is None or (
                row.role,
                row.pid,
                row.process_birth,
                row.endpoint,
                row.version,
            ) != (
                "api",
                identity.pid,
                identity.process_birth,
                identity.endpoint,
                identity.version,
            ):
                raise CoordinationConflict(
                    "Missing API proof belongs to another incarnation"
                )
            if row.state == "retired":
                return
            blockers = await self.retirement_blockers(identity.runtime_id)
            if blockers:
                raise CoordinationConflict("; ".join(blockers[:10]))
            if same_process(identity.pid, identity.process_birth):
                raise CoordinationConflict("API process absence is no longer verified")
            if host.api_runtime_id == identity.runtime_id:
                host.api_runtime_id = None
            row.state = "retired"
            row.updated_at_ms = await self.coordination._clock_ms(session)
            await session.execute(
                update(RuntimeInstanceRow)
                .where(RuntimeInstanceRow.id == identity.runtime_id)
                .values(lease_expires_at_ms=0)
            )
            host.revision += 1
            host.detail = "The previous API process ended; host recovery and project workers remain available."
            await self._receipt(
                session,
                "api.process_absent",
                {"runtime_id": identity.runtime_id, "version": identity.version},
            )

    async def drained_apis(self, *, grace_ms: int = API_DRAIN_GRACE_MS) -> list[str]:
        """Replaced APIs whose drain grace has passed, oldest first.

        Only a promotion moves an API to draining outside shutdown, so these
        own no project, command or resource; ``begin_retirement`` still
        rechecks every hold before the process is asked to stop.
        """
        async with self.database.session() as session:
            host = await session.get(DeploymentHostRow, "host")
            if host is None or host.shutdown_requested:
                return []
            now = await self.coordination._clock_ms(session)
            return list(
                await session.scalars(
                    select(RuntimeProcessRow.runtime_id)
                    .where(
                        RuntimeProcessRow.role == "api",
                        RuntimeProcessRow.state == "draining",
                        RuntimeProcessRow.updated_at_ms <= now - grace_ms,
                        *(
                            [RuntimeProcessRow.runtime_id != host.api_runtime_id]
                            if host.api_runtime_id is not None
                            else []
                        ),
                    )
                    .order_by(RuntimeProcessRow.updated_at_ms)
                )
            )

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
                raise OperationRejected(
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
                identity.model_dump(mode="json"),
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
            owners = list(await session.scalars(select(ProjectOwnershipRow)))
            # A failed worker keeps its grant alive; say so instead of ready.
            failures = await self._owner_failures(session, owners)
            # The recovery that granted each current owner, if one did. A
            # target runtime receives at most one grant per fencing token.
            owner_runtimes = {
                owner.runtime_id for owner in owners if owner.runtime_id is not None
            }
            recoveries: dict[tuple[str, str | None, int], RuntimeRecoveryRow] = (
                {
                    (
                        row.project_id,
                        row.target_runtime_id,
                        row.target_fencing_token,
                    ): row
                    for row in await session.scalars(
                        select(RuntimeRecoveryRow).where(
                            RuntimeRecoveryRow.target_runtime_id.in_(owner_runtimes)
                        )
                    )
                }
                if owner_runtimes
                else {}
            )
            for owner in owners:
                recovery = recoveries.get(
                    (owner.project_id, owner.runtime_id, owner.fencing_token)
                )
                process, handoff = (
                    processes.get(owner.runtime_id)
                    if owner.runtime_id is not None
                    else None,
                    handoffs.get(owner.project_id),
                )
                failure = failures.get(owner.project_id)
                projects.append(
                    ProjectDeploymentStatus(
                        project_id=owner.project_id,
                        runtime_id=owner.runtime_id,
                        runtime_version=process.version if process else None,
                        fencing_token=owner.fencing_token,
                        phase=HandoffPhase(handoff.phase) if handoff else None,
                        target_version=handoff.target_version if handoff else None,
                        blockers=[
                            *(handoff.blockers if handoff else []),
                            *([self._failure_blocker(failure)] if failure else []),
                        ],
                        admission_held=handoff.admission_held if handoff else False,
                        recovery=ProjectRecoveryStatus(
                            operation_id=recovery.operation_id,
                            source_runtime_id=recovery.source_runtime_id,
                            source_fencing_token=recovery.source_fencing_token,
                            target_runtime_id=recovery.target_runtime_id,
                            target_fencing_token=recovery.target_fencing_token,
                            phase=recovery.phase,
                        )
                        if recovery is not None
                        else None,
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
            now = await self.coordination._clock_ms(session)
            live_runtimes = set(
                await session.scalars(
                    select(RuntimeInstanceRow.id).where(
                        RuntimeInstanceRow.lease_expires_at_ms > now
                    )
                )
            )

            def replaced_api(row: RuntimeProcessRow) -> bool:
                return (
                    row.role == "api"
                    and row.state == "draining"
                    and not host.shutdown_requested
                    and row.runtime_id != host.api_runtime_id
                )

            # A replaced API is expected to wind down, so its lapsing lease
            # is not a fault until its drain outlives the bound.
            degraded = bool(failures) or any(
                row.state == "unreachable"
                or (replaced_api(row) and now - row.updated_at_ms >= API_DRAIN_BOUND_MS)
                or (
                    row.state in _RESIDENT
                    and row.runtime_id not in live_runtimes
                    and not replaced_api(row)
                )
                for row in processes.values()
            )
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
                processes=[
                    ProcessDeploymentStatus(
                        runtime_id=row.runtime_id,
                        version=row.version,
                        role=row.role,
                        state=row.state,
                        ready_verified=(
                            row.state == "ready"
                            and row.runtime_id in live_runtimes
                            and same_process(row.pid, row.process_birth)
                        ),
                    )
                    for row in sorted(
                        processes.values(), key=lambda row: row.runtime_id
                    )
                    if row.state in _RESIDENT
                ],
            )
