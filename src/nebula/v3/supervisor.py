"""Stable public listener and retained-process orchestration owned by the host."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from builtins import BaseExceptionGroup
from dataclasses import asdict, replace
import hashlib
from pathlib import Path
import threading

from fastapi import Depends, FastAPI, Request, Response, WebSocket
import httpx
from sqlalchemy import and_, or_, select
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from .async_utils import cancel_and_join, finish_cleanup
from .authentication import CoreAuthentication
from .authentication_api import AuthenticationApi
from .database import (
    DeploymentHostRow,
    ProjectOwnershipRow,
    ProjectHandoffRow,
    RuntimeInstanceRow,
    RuntimeProcessRow,
    RuntimeReleaseRow,
    RuntimeRecoveryRow,
)
from .deployment_protocol import ReleaseManifest, WorkerIdentity
from .diagnostics import (
    create_diagnostic_task,
    record_caught_exception,
    record_diagnostic,
)
from .deployments import _RESIDENT, checkpoint_digest
from .event_history import pause_between_batches, prune_orphaned_event_history
from .release_store import ReleaseStore
from .memory_policy import MemoryBudgetError
from .runtime_coordination import CoordinationConflict, OperationRejected, ProjectLease
from .runtime_bootstrap import RuntimeBootstrap
from .runtime_http import HttpUpstream, RuntimeHttpProxy
from .runtime_identity import mount_runtime_identity
from .runtime_ipc import RuntimeIpcError
from .runtime_processes import RuntimeProcessHost
from .runtime_routing import RuntimeRouting
from .runtime_memory import RuntimeMemoryAdmission
from .runtime_host_services import HostSharedServices, create_host_knowledge_index
from .runtime_management import RuntimeManagement
from .storage import NebulaStore


# Paired-device cookies the listener recognised recently, and for how long.
# This only selects forwarding capacity, never authority.
_RECOGNISED_DEVICES = 256
_RECOGNISED_DEVICE_SECONDS = 30.0


class _Forward(Response):
    def __init__(self, proxy):
        super().__init__()
        self.proxy = proxy

    async def __call__(self, scope, receive, send):
        await self.proxy(scope, receive, send)


class _Availability:
    def __init__(self, app, supervisor):
        self.app, self.supervisor = app, supervisor

    async def __call__(self, scope, receive, send):
        owner = self.supervisor
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if owner.closing or owner.closed or not owner.started:
            if scope["type"] == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1012,
                        "reason": "Nebula is reconnecting",
                    }
                )
            else:
                await JSONResponse(
                    {"detail": "Nebula is reconnecting. Saved work continues."},
                    status_code=503,
                )(scope, receive, send)
            return
        task = asyncio.current_task()
        owner.viewers.add(task)

        async def answer(message):
            # A drain may close an exchange only after it has answered; one
            # still waiting for its upstream receipt must not become a 500.
            if message["type"] in {"http.response.start", "websocket.accept"}:
                owner.answered.setdefault(task, asyncio.get_running_loop().time())
            await send(message)

        try:
            await self.app(scope, receive, answer)
        finally:
            owner.viewers.discard(task)
            owner.answered.pop(task, None)


async def drain_relays(owner, seconds, *, limit, answer_grace=1.0):
    """Close the exchanges a stopping generation still relays, boundedly.

    Used only while a longer-lived holder keeps the public socket, so the next
    generation answers as soon as this one exits. An exchange that already
    answered (an open viewer stream) is closed after ``seconds`` and resumes
    from its cursor on the next generation. One still waiting for its response
    headers (an accepted request whose receipt is pending upstream) is never
    closed before it answers, up to ``limit``; after answering it gets
    ``answer_grace`` to finish a complete response.
    """
    loop = asyncio.get_running_loop()
    began = loop.time()
    await asyncio.sleep(seconds)
    while True:
        pending = {task for task in owner.viewers if not task.done()}
        if not pending:
            return
        now = loop.time()
        for task in pending:
            answered = owner.answered.get(task)
            if (answered is not None and now - answered >= answer_grace) or (
                now - began >= limit
            ):
                task.cancel()
        await asyncio.sleep(0.1)


class RuntimeSupervisor:
    def __init__(
        self,
        host: RuntimeProcessHost,
        *,
        operator_token: str,
        retained_root: Path | None = None,
        worker_admission: Callable[[ReleaseManifest, str], Awaitable[None]]
        | None = None,
        shared_services: HostSharedServices | None = None,
        recovery_version: str | None = None,
        allow_insecure_device_pairing: bool = False,
        allow_browser_diagnostics: bool = False,
        shutdown_requested: Callable[[], Awaitable[None]] | None = None,
    ):
        if not operator_token:
            raise ValueError("supervisor requires normal Core authentication")
        self.host, self.coordinator = host, host.coordinator
        self.shared_services = shared_services or HostSharedServices(
            host,
            knowledge_index_factory=create_host_knowledge_index,
            memory_budgets=worker_admission.budgets
            if isinstance(worker_admission, RuntimeMemoryAdmission)
            else None,
        )
        if self.shared_services.host is not host:
            raise ValueError("Shared services must use this supervisor's process host")
        self.store = NebulaStore(self.coordinator.database)
        self.shared_services.configure_memory_control(operator_token)
        self.shared_services.host_settings.observations.allow_browser_diagnostics = (
            allow_browser_diagnostics
        )
        self.retained = ReleaseStore(retained_root or host.root / "releases")
        self.worker_admission = worker_admission
        if isinstance(worker_admission, RuntimeMemoryAdmission):
            if worker_admission.budgets.database is not self.coordinator.database:
                raise ValueError(
                    "runtime admission and deployment must share the host database"
                )
            if worker_admission.exited is None:
                # A worker this host proves dead holds no memory; counting it
                # would refuse the very replacement that retires it.
                worker_admission.exited = getattr(host, "proven_exited", None)
            self.coordinator.runtime_admission = worker_admission.check
        self.routing = RuntimeRouting(
            self.coordinator.coordination, host.credentials.directory
        )
        self.proxy = RuntimeHttpProxy(self._api_upstream)
        self.bootstrap = RuntimeBootstrap(self.coordinator, self.routing)
        self._recovery_version = recovery_version
        self._shutdown_requested = shutdown_requested
        self._shutdown_committed = False
        self.started, self.closed, self.closing = False, False, False
        self._operations = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._handoff_task = None
        self._prune_stop = threading.Event()
        self._prune_task: asyncio.Task | None = None
        self._handoff_serial = asyncio.Lock()
        self._next_process_reconciliation = 0.0
        self._activated_handoffs: set[str] = set()
        # The last waiting reason logged per project, so a transfer that keeps
        # waiting is recorded when its reason changes, not on every tick.
        self._restore_waiting: dict[str, tuple] = {}
        # The last relinquishment refusal reported per project, reported once.
        self._relinquish_waiting: dict[str, str] = {}
        self.viewers: set[asyncio.Task] = set()
        # Relayed exchanges that have sent their response start, by loop time.
        self.answered: dict[asyncio.Task, float] = {}
        authentication = CoreAuthentication(self.store, operator_token)
        self.authentication = authentication
        self._recognised_devices: OrderedDict[bytes, float] = OrderedDict()
        self._credential_lookups = asyncio.Semaphore(4)

        @asynccontextmanager
        async def lifespan(_app):
            await self.start()
            try:
                yield
            finally:
                await self.close()

        api = FastAPI(
            lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
        )
        self.http_api = api
        api.state.authentication_api = AuthenticationApi(
            api,
            self.store,
            authentication,
            allow_insecure_pairing=allow_insecure_device_pairing,
        )
        self.management = RuntimeManagement(self, api, authentication)
        self.shared_services.browser_bridge.mount(api, authentication)
        self.shared_services.host_settings.mount(api, authentication, self.proxy)
        mount_runtime_identity(api, self.store, authentication)

        @api.get("/api/v1/health/live")
        async def liveness():
            # Reachability only: no credential lookup, database session or
            # worker call, so a busy Core still answers its viewers.
            return JSONResponse(
                {"status": "alive"}, headers={"Cache-Control": "no-store"}
            )

        @api.get("/api/v1/health", dependencies=[Depends(authentication.require_auth)])
        async def health():
            return JSONResponse(
                await self.bootstrap.health(),
                headers={"Cache-Control": "private, no-store"},
            )

        @api.exception_handler(RuntimeIpcError)
        async def disconnected(_request, _error):
            return JSONResponse(
                {
                    "detail": "Nebula is reconnecting. Saved work has not been resubmitted."
                },
                status_code=503,
            )

        @api.exception_handler(CoordinationConflict)
        async def conflict(_request, error):
            return JSONResponse({"detail": str(error)}, status_code=409)

        @api.get(
            "/api/v1/deployment/status",
            dependencies=[Depends(authentication.require_auth)],
        )
        async def status():
            return await self.coordinator.status()

        @api.get(
            "/api/v1/deployment/releases",
            dependencies=[Depends(authentication.require_auth)],
        )
        async def releases():
            async with self.coordinator.database.session() as session:
                rows = list(await session.scalars(select(RuntimeReleaseRow)))
            return [
                {
                    "version": row.version,
                    "state": row.state,
                    "manifest_sha256": row.manifest_sha256,
                }
                for row in rows
            ]

        @api.api_route("/api/v1/memory-budgets", methods=["GET", "PUT"])
        @api.api_route(
            "/api/v1/memory-budgets/{path:path}", methods=["GET", "POST", "PUT"]
        )
        async def memory_control(request: Request):
            if request.url.path.endswith("/apply"):
                return _Forward(self.proxy)
            return _Forward(self.shared_services.memory_control.app)

        @api.api_route(
            "/{path:path}",
            methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
        )
        async def forward(request: Request):
            path = request.url.path
            if path == "/internal" or path.startswith("/internal/"):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            if (
                request.method in {"GET", "HEAD"}
                and path != "/api"
                and not path.startswith("/api/")
                and not await self.bootstrap.api_promoted()
                and self.bootstrap.assets is not None
            ):
                return _Forward(self.bootstrap.assets.app)
            return _Forward(self.proxy)

        @api.websocket("/{path:path}")
        async def websocket(websocket: WebSocket):
            if websocket.url.path.startswith("/internal/"):
                await websocket.close(code=4404)
                return
            await self.proxy(websocket.scope, websocket.receive, websocket.send)

        self.app = _Availability(api, self)

    async def _api_upstream(self, scope: dict) -> HttpUpstream:
        """Forward to the promoted API worker with capacity chosen by caller.

        The listener relays every public request before the API worker
        authenticates it. A request presenting no credential this listener
        recognises therefore travels in its own connection pool, and never on
        the reserved control lane, so callers the API worker will refuse (for
        example with uploads that never finish) cannot hold the connections an
        operator's Stop, decision, read or change needs. The choice authorizes
        nothing: the API worker still authenticates every request in full.
        """
        upstream = await self.routing.api_upstream(scope)
        if scope["type"] != "http" or await self._presents_credential(scope):
            return upstream
        return replace(
            upstream,
            lane="work" if upstream.lane == "control" else upstream.lane,
            public=True,
        )

    async def _presents_credential(self, scope: dict) -> bool:
        """Whether a request carries the operator token or a live paired-device
        session; this selects forwarding capacity only."""
        connection = HTTPConnection(scope)
        if self.authentication.presents_operator_token(connection):
            return True
        cookie = connection.cookies.get("nebula_device")
        if not cookie:
            return False
        # Browsers send the paired-device cookie with every request. A device
        # recognised recently keeps operator capacity without a lookup per
        # request; revocation still takes effect at the API worker at once.
        digest = hashlib.sha256(cookie.encode("utf-8", "surrogateescape")).digest()
        now = asyncio.get_running_loop().time()
        recognised = self._recognised_devices.pop(digest, None)
        if recognised is not None and recognised > now:
            self._recognised_devices[digest] = recognised
            return True
        try:
            # Bound the lookups any volume of unknown cookies can start.
            async with self._credential_lookups:
                device = await self.authentication.device_for_token(cookie)
        except Exception as error:
            # The API worker still authenticates the request; only its
            # forwarding capacity falls back to the public pool.
            record_caught_exception(
                "api",
                "api.runtime.credential_check_failed",
                "A paired-device session could not be checked before forwarding.",
                error,
                stage="forward",
            )
            return False
        if device is None:
            return False
        self._recognised_devices[digest] = now + _RECOGNISED_DEVICE_SECONDS
        while len(self._recognised_devices) > _RECOGNISED_DEVICES:
            self._recognised_devices.popitem(last=False)
        return True

    async def start(self):
        async with self._operations:
            if self.closed:
                raise RuntimeError("a closed supervisor cannot restart")
            if not self.started:
                await self.host.start()
                try:
                    await self.host.reconcile_processes()
                    self._next_process_reconciliation = (
                        asyncio.get_running_loop().time() + 5
                    )
                    if self._recovery_version is not None:
                        await self.bootstrap.select_release(self._recovery_version)
                    await self.shared_services.start()
                except BaseException:
                    await self.shared_services.close()
                    await self.host.close()
                    raise
                self.started = True
                # diagnostic-expected: the owned handoff loop records operation failures and is cancelled and joined on detach.
                self._handoff_task = asyncio.create_task(
                    self._handoff_loop(), name="nebula-host-handoffs"
                )
                # Global history maintenance uses the unscoped host database.
                # An execution worker's scoped database locks its project lease.
                self._prune_task = create_diagnostic_task(
                    prune_orphaned_event_history(
                        self.store.database,
                        pause=pause_between_batches,
                        stop=self._prune_stop,
                    ),
                    feature="storage",
                    event_code="storage.event_history.prune",
                    failure_message="Removing event history of deleted records stopped.",
                    name="nebula-host-event-history-prune",
                )

    async def select_recovery_release(self, version: str) -> None:
        async with self._operations:
            self._active()
            await self.bootstrap.select_release(version)
            self._recovery_version = version

    async def request_project_handoff(self, project_id, target_runtime_id):
        async with self._operations:
            self._active()
            async with self.coordinator.database.session() as session:
                owner = await session.get(ProjectOwnershipRow, project_id)
                source = (
                    await session.get(RuntimeProcessRow, owner.runtime_id)
                    if owner and owner.runtime_id
                    else None
                )
                target = await session.get(RuntimeProcessRow, target_runtime_id)
                for process in (source, target):
                    release = (
                        await session.get(RuntimeReleaseRow, process.version)
                        if process
                        else None
                    )
                    if (
                        release is None
                        or "project.handoff/v1"
                        not in ReleaseManifest.model_validate(release.manifest).features
                    ):
                        raise CoordinationConflict(
                            "Both execution versions must support verified project handoff"
                        )
            client = await self.host.rpc(target_runtime_id)
            try:
                status = await client.call("execution.status/v1", {}, timeout=5)
                binding = status.get("lease")
                if status.get("state") != "unassigned" and (
                    not binding or binding.get("project_id") != project_id
                ):
                    raise CoordinationConflict(
                        "Prepare an unassigned execution process for this project"
                    )
            finally:
                await client.close()
            await self.coordinator.request_handoff(project_id, target_runtime_id)

    async def advance_project_handoffs(self):
        async with self._handoff_serial:
            self._active()
            async with self.coordinator.database.session() as session:
                # A verified transfer needs this loop only while its target
                # still owns the project (to activate a prepared target after
                # a restart). Once ownership moved on it is finished history;
                # retrying it would fail on every tick and could even claim
                # the project for a stale target.
                query = (
                    select(ProjectHandoffRow)
                    .outerjoin(
                        ProjectOwnershipRow,
                        ProjectOwnershipRow.project_id == ProjectHandoffRow.project_id,
                    )
                    .where(
                        or_(
                            ProjectHandoffRow.phase.in_(
                                ("transferable", "transferred")
                            ),
                            and_(
                                ProjectHandoffRow.phase == "verified",
                                ProjectOwnershipRow.runtime_id
                                == ProjectHandoffRow.target_runtime_id,
                            ),
                        )
                    )
                    .order_by(ProjectHandoffRow.project_id)
                )
                if self._activated_handoffs:
                    query = query.where(
                        ProjectHandoffRow.target_runtime_id.not_in(
                            self._activated_handoffs
                        )
                    )
                rows = list(await session.scalars(query.limit(64)))
            for row in rows:
                checkpoint_format = row.checkpoint.get("format")
                if checkpoint_format not in {
                    "nebula.project-checkpoint/v1",
                    "nebula.project-recovery/v1",
                }:
                    continue
                try:
                    if checkpoint_format == "nebula.project-recovery/v1":
                        await self._advance_project_recovery(row)
                    else:
                        await self._advance_project_handoff(row)
                    self._restore_waiting.pop(row.project_id, None)
                except Exception as error:
                    # A blocked target must not starve transfers for other projects.
                    signature = (
                        row.phase,
                        row.target_runtime_id,
                        type(error).__name__,
                        str(error),
                    )
                    if self._restore_waiting.get(row.project_id) != signature:
                        self._restore_waiting[row.project_id] = signature
                        record_caught_exception(
                            "runtime",
                            "runtime.project_restore.waiting",
                            "This project remains held pending target restoration."
                            if row.phase == "transferred"
                            else "A project transfer is waiting to continue.",
                            error,
                            stage="handoff",
                        )
                    if row.phase != "transferred":
                        continue
                    if await self._relinquish_failed_restoration(row):
                        continue
                    blockers = [
                        "Target runtime "
                        + row.target_runtime_id
                        + " has not verified restoration; queued work remains held"
                    ]
                    async with self.coordinator.coordination._write() as session:
                        pending = await session.get(ProjectHandoffRow, row.project_id)
                        if (
                            pending is not None
                            and pending.phase == "transferred"
                            and pending.blockers != blockers
                        ):
                            pending.blockers = blockers
                            pending.revision += 1

    async def _relinquish_failed_restoration(self, row) -> bool:
        """Give back the grant of a target that failed before activation.

        A restoration target whose project failed before activation keeps a
        live process, and its heartbeat would renew the held grant forever.
        When the target reports a failed, never-activated and cleaned
        restoration of exactly this grant, it relinquishes it: the lease
        expires with its fence and evidence kept, the target process exits,
        and expired-owner recovery restores the project on a fresh target.
        Anything else (a target still starting, uncleaned or activated) keeps
        the hold.
        """
        try:
            client = await self.host.rpc(row.target_runtime_id)
        except Exception:
            # diagnostic-expected: an unreachable target is already reported as a waiting restoration; a gone one lets its lease lapse.
            return False
        try:
            try:
                status = await client.call("execution.status/v1", {}, timeout=5)
            except Exception:
                # diagnostic-expected: an unreachable target is already reported as a waiting restoration; a gone one lets its lease lapse.
                return False
            lease = status.get("lease") or {}
            if (
                status.get("runtime_id") != row.target_runtime_id
                or status.get("state") != "failed"
                or status.get("activation_started")
                or not status.get("cleanup_complete")
                or lease.get("project_id") != row.project_id
                or lease.get("runtime_id") != row.target_runtime_id
            ):
                return False
            try:
                await client.call("execution.relinquish/v1", {}, timeout=30)
            except Exception as error:
                # A refused relinquishment keeps the hold; say why once.
                signature = f"{row.target_runtime_id}:{type(error).__name__}:{error}"
                if self._relinquish_waiting.get(row.project_id) != signature:
                    self._relinquish_waiting[row.project_id] = signature
                    record_caught_exception(
                        "runtime",
                        "runtime.project_restore.relinquish_waiting",
                        "A failed restoration target could not give back its project yet; the project stays held.",
                        error,
                        stage="handoff",
                    )
                return False
        finally:
            await client.close()
        self._relinquish_waiting.pop(row.project_id, None)
        record_diagnostic(
            "warning",
            "runtime",
            "runtime.project_restore.relinquished",
            "A restoration target failed before activation and gave back its project; Core recovers it on a fresh worker.",
            outcome="recovering",
            stage="handoff",
            retryable=True,
            metadata={"entity_id": row.project_id, "kind": "project"},
        )
        return True

    async def _advance_project_handoff(self, row):
        if row.phase == "transferable":
            async with self._operations:
                self._active()
                await self.coordinator.transfer(row.project_id)
        lease = await self.attach_execution(
            row.project_id, row.target_runtime_id, allow_prepared=True
        )
        client = await self.host.rpc(row.target_runtime_id)
        try:
            status = await client.call("execution.status/v1", {}, timeout=5)
        finally:
            await client.close()
        if status.get("state") not in {"prepared", "ready"} or not status.get(
            "restored_checkpoint_digest"
        ):
            raise CoordinationConflict(
                "The target has not verified its restored project state"
            )
        await self.coordinator.verified(
            lease, restored_checkpoint_digest=status["restored_checkpoint_digest"]
        )
        if status.get("state") == "prepared":
            await self.activate_execution(lease)
        self._activated_handoffs.add(row.target_runtime_id)

    async def _recovery_grant(self, row, *, check_blockers=False):
        """Read the original recovery identity; this path never claims an owner."""
        operation_id = row.checkpoint.get("operation_id")
        async with self.coordinator.database.session() as session:
            recovery = (
                await session.get(RuntimeRecoveryRow, operation_id)
                if operation_id
                else None
            )
            if recovery is None:
                raise CoordinationConflict(
                    "The saved recovery operation is unavailable"
                )
            lease = ProjectLease(
                recovery.project_id,
                recovery.target_runtime_id,
                recovery.target_fencing_token,
            )
        async with self.coordinator.coordination.fenced(lease) as session:
            recovery = await session.get(RuntimeRecoveryRow, operation_id)
            current = await session.get(ProjectHandoffRow, row.project_id)
            if (
                recovery is None
                or current is None
                or (
                    recovery.project_id,
                    recovery.target_runtime_id,
                    recovery.source_runtime_id,
                    recovery.source_fencing_token,
                )
                != (
                    row.project_id,
                    row.target_runtime_id,
                    row.source_runtime_id,
                    row.source_fencing_token,
                )
                or (
                    current.target_runtime_id,
                    current.source_runtime_id,
                    current.source_fencing_token,
                )
                != (
                    recovery.target_runtime_id,
                    recovery.source_runtime_id,
                    recovery.source_fencing_token,
                )
                or recovery.checkpoint.get("format") != "nebula.project-recovery/v1"
                or recovery.checkpoint.get("operation_id") != operation_id
                or current.checkpoint != recovery.checkpoint
                or recovery.phase not in {"held", "prepared", "verified"}
                or (
                    recovery.project_id,
                    recovery.target_runtime_id,
                    recovery.target_fencing_token,
                )
                != (lease.project_id, lease.runtime_id, lease.fencing_token)
                or (current.phase, current.admission_held)
                != (
                    ("verified", False)
                    if recovery.phase == "verified"
                    else ("transferred", True)
                )
            ):
                raise CoordinationConflict(
                    "The saved recovery operation, target or checkpoint changed"
                )
            host = await session.get(DeploymentHostRow, "host")
            if host is not None and host.shutdown_requested:
                raise CoordinationConflict(
                    "Core is holding recovery for coordinated shutdown"
                )
            if check_blockers:
                blockers = await self.coordinator._recovery_blockers(
                    session, lease.project_id, lease.runtime_id
                )
                if blockers:
                    raise CoordinationConflict("; ".join(blockers[:10]))
            return (
                lease,
                operation_id,
                recovery.phase,
                checkpoint_digest(recovery.checkpoint),
            )

    async def _recovery_status(self, lease):
        client = await self.host.rpc(lease.runtime_id)
        try:
            status = await client.call("execution.status/v1", {}, timeout=5)
        finally:
            await client.close()
        if status.get("runtime_id") != lease.runtime_id:
            raise RuntimeIpcError("Recovery status belongs to another incarnation")
        return status

    async def _advance_project_recovery(self, row, *, expected_lease=None):
        lease, operation_id, phase, digest = await self._recovery_grant(row)
        if expected_lease is not None and lease != expected_lease:
            raise CoordinationConflict("The original recovery grant changed")
        status = await self._recovery_status(lease)
        if status.get("state") in {"unassigned", "starting"} and phase != "verified":
            # Attach even while effects are unresolved: the worker holds its
            # restoration (nothing is constructed or activated) but runs the
            # operator's pause and stop. Verification below still requires
            # every blocker to be resolved.
            await self._recovery_grant(row)
            await self.attach_execution(
                lease.project_id,
                lease.runtime_id,
                allow_prepared=True,
                expected_lease=lease,
            )
            status = await self._recovery_status(lease)
        current, current_operation, phase, digest = await self._recovery_grant(
            row,
            check_blockers=status.get("state") != "ready",
        )
        if (
            current != lease
            or current_operation != operation_id
            or status.get("lease") != asdict(lease)
            or status.get("recovery_operation_id") != operation_id
            or status.get("restored_checkpoint_digest") != digest
            or status.get("state") not in {"prepared", "ready"}
            or (status.get("state") == "ready" and phase != "verified")
        ):
            raise CoordinationConflict(
                "The target has not prepared this exact recovery checkpoint"
            )
        await self.coordinator.verify_recovery(
            lease,
            operation_id=operation_id,
            restored_checkpoint_digest=digest,
        )
        if status.get("state") == "prepared":
            await self.activate_execution(lease)
        self._activated_handoffs.add(lease.runtime_id)
        return lease

    async def _handoff_loop(self):
        while not self.closed and not self.host.closed:
            try:
                now = asyncio.get_running_loop().time()
                if now >= self._next_process_reconciliation:
                    await self.host.reconcile_processes()
                    self._next_process_reconciliation = now + 5
                await self.advance_project_handoffs()
            except Exception as error:
                record_caught_exception(
                    "runtime",
                    "runtime.handoff.reconciliation_waiting",
                    "Project transfers retain their saved phase while waiting for readiness.",
                    error,
                    stage="handoff",
                )
            await asyncio.sleep(0.5)

    async def stage(self, source: Path, *, manifest_sha256: str) -> ReleaseManifest:
        async with self._operations:
            self._active()
            manifest, installed = await asyncio.to_thread(
                self.retained.install, source, expected_manifest_sha256=manifest_sha256
            )
            await self.coordinator.stage(manifest, installed, manifest_sha256)
            return manifest

    def _active(self):
        if self.closed or not self.started:
            raise RuntimeError("the active supervisor host lock is required")
        if self._shutdown_committed:
            raise CoordinationConflict("Host shutdown is committed")

    async def prepare_api(self, version: str) -> WorkerIdentity:
        async with self._operations:
            try:
                self._active()
            except CoordinationConflict as error:
                raise OperationRejected(str(error)) from error
            async with self.coordinator.database.session() as session:
                release = await session.get(RuntimeReleaseRow, version)
                if release is None:
                    raise OperationRejected(
                        "Stage and verify this release before preparing it"
                    )
                manifest = ReleaseManifest.model_validate(release.manifest)
                processes = list(
                    await session.scalars(
                        select(RuntimeProcessRow).where(
                            RuntimeProcessRow.role == "api",
                            RuntimeProcessRow.state.in_(_RESIDENT),
                        )
                    )
                )
                now = await self.coordinator.coordination._clock_ms(session)
                ready = []
                for process in processes:
                    runtime = await session.get(RuntimeInstanceRow, process.runtime_id)
                    if (
                        process.version == version
                        and process.state == "ready"
                        and runtime is not None
                        and runtime.lease_expires_at_ms > now
                    ):
                        ready.append(process)
            if ready:
                if len(ready) != 1:
                    raise CoordinationConflict(
                        "Multiple candidate incarnations need explicit reconciliation"
                    )
                client = await self.host.rpc(ready[0].runtime_id)
                try:
                    identity = WorkerIdentity.model_validate(
                        await client.call("worker.identity", {}, timeout=5)
                    )
                finally:
                    await client.close()
                if (
                    identity.runtime_id,
                    identity.pid,
                    identity.process_birth,
                    identity.version,
                    identity.role,
                    identity.endpoint,
                ) != (
                    ready[0].runtime_id,
                    ready[0].pid,
                    ready[0].process_birth,
                    version,
                    "api",
                    ready[0].endpoint,
                ):
                    raise RuntimeIpcError(
                        "Candidate identity differs from its committed incarnation"
                    )
                return identity
            # Validate admission before retiring old viewers: refusals here
            # have no effect and can be retried after their cause is resolved.
            # A previously promoted API can remain draining after rollback.
            # Admit capacity first, then retire it through the same guarded
            # path as other inactive viewers before spawning a fresh runtime.
            if any(
                process.version == version and process.state != "draining"
                for process in processes
            ):
                raise OperationRejected(
                    "An earlier candidate incarnation still needs recovery"
                )
            if (
                len([process for process in processes if process.state != "draining"])
                >= 2
            ):
                raise OperationRejected(
                    "Two API processes remain resident; reconcile the inactive candidate"
                )
            if self.worker_admission is None:
                raise OperationRejected(
                    "Host worker memory reserve must be configured before candidate startup"
                )
            try:
                await self.worker_admission(manifest, "api")
            except (CoordinationConflict, MemoryBudgetError) as error:
                if isinstance(error, OperationRejected):
                    raise
                raise OperationRejected(str(error)) from error
            # Inactive APIs own only viewers. A graceful retirement may detach
            # them; their accepted commands and delivery remain in SQL.
            for process in list(processes):
                if process.state == "draining":
                    await self.host.retire(process.runtime_id)
                    await self.proxy.discard_endpoint(process.endpoint)
                    processes.remove(process)
            spawned = await self.host.spawn(version, "api")
            if spawned is None:
                raise CoordinationConflict("The candidate remains staged")
            return spawned

    async def prepare_execution(self, version: str) -> WorkerIdentity:
        """Prepare an unassigned process; controllers wait for a project grant."""
        async with self._operations:
            # Nothing is reserved or spawned before host.spawn, so a refusal
            # here is a rejection the caller may safely ask again.
            try:
                self._active()
                async with self.coordinator.database.session() as session:
                    release = await session.get(RuntimeReleaseRow, version)
                    if release is None:
                        raise CoordinationConflict(
                            "Stage and verify this release before preparing it"
                        )
                    manifest = ReleaseManifest.model_validate(release.manifest)
                if self.worker_admission is None:
                    raise CoordinationConflict(
                        "Host worker memory reserve must be configured before candidate startup"
                    )
                await self.worker_admission(manifest, "execution")
            except (CoordinationConflict, MemoryBudgetError) as error:
                if isinstance(error, OperationRejected):
                    raise
                raise OperationRejected(str(error)) from error
            spawned = await self.host.spawn(version, "execution")
            if spawned is None:
                raise OperationRejected(
                    "The execution candidate remains staged. "
                    + await self.coordinator.execution_capacity_blocker(version)
                )
            return spawned

    async def attach_execution(
        self,
        project_id: str,
        runtime_id: str,
        *,
        timeout=30,
        allow_prepared=False,
        expected_lease: ProjectLease | None = None,
    ):
        """Attach fresh work, or resume an interrupted assignment response.

        Live transfers and expired-owner recovery use their dedicated protocols;
        this path cannot replace an existing project owner.
        """
        async with self._operations:
            self._active()
            client = await self.host.rpc(runtime_id)
            try:
                status = await client.call("execution.status/v1", {}, timeout=5)
                if status.get("runtime_id") != runtime_id:
                    raise RuntimeIpcError(
                        "Execution status belongs to another incarnation"
                    )
                binding = status.get("lease")
                if binding is not None and binding.get("project_id") != project_id:
                    raise CoordinationConflict(
                        "Prepare a new process for another project"
                    )
                async with self.coordinator.database.session() as session:
                    owner = await session.get(ProjectOwnershipRow, project_id)
                    if owner is not None and owner.runtime_id not in (None, runtime_id):
                        raise CoordinationConflict(
                            "Use verified handoff or explicit expired-owner recovery for this project"
                        )
                    lease = (
                        ProjectLease(project_id, runtime_id, owner.fencing_token)
                        if owner is not None and owner.runtime_id == runtime_id
                        else None
                    )
                # An execution process attaches once. A bound worker may only
                # resume its own current grant; claiming for it again would
                # leave the project owned by a worker that cannot accept it.
                if binding is not None and (
                    lease is None
                    or binding != asdict(lease)
                    or status.get("state")
                    not in {"starting", "recovering", "prepared", "ready"}
                ):
                    raise CoordinationConflict(
                        "This execution process already ended its project grant; "
                        "prepare a new process"
                    )
                if binding is None and status.get("state") != "unassigned":
                    raise CoordinationConflict(
                        "This execution process cannot accept a project grant"
                    )
                if lease is None:
                    if expected_lease is not None:
                        raise CoordinationConflict(
                            "The original recovery grant is no longer owned"
                        )
                    lease = await self.coordinator.coordination.claim_project(
                        project_id, runtime_id
                    )
                if expected_lease is not None and lease != expected_lease:
                    raise CoordinationConflict("The original recovery grant changed")
                await client.call("execution.attach/v1", asdict(lease), timeout=5)
                async with asyncio.timeout(timeout):
                    while True:
                        try:
                            status = await client.call(
                                "execution.status/v1", {}, timeout=5
                            )
                        except httpx.TimeoutException:
                            # diagnostic-expected: a worker loading its project can miss one status read; keep polling within the attach bound.
                            await asyncio.sleep(0.05)
                            continue
                        if status.get("lease") != asdict(lease):
                            raise RuntimeIpcError(
                                "Execution attachment does not match the committed grant"
                            )
                        if status.get("state") == "ready" or (
                            allow_prepared
                            and status.get("state") in {"prepared", "recovering"}
                        ):
                            return lease
                        if status.get("state") != "starting":
                            raise CoordinationConflict(
                                "Project startup needs reconciliation; its saved work remains held"
                            )
                        await asyncio.sleep(0.05)
            finally:
                await client.close()

    async def reconcile_dead_project(self, *, expected_owner: ProjectLease):
        from .runtime_orphan_recovery import reconcile_dead_project

        async with self._operations:
            self._active()
            return await reconcile_dead_project(
                self.coordinator,
                expected_owner,
                launch_origin=self.host.launch_origin,
            )

    async def evict_execution(self, *, expected_owner: ProjectLease) -> dict:
        """Stop a source worker whose deployment handoff ran out of time.

        Used by a deployment after its grace period: the project's owner is
        still the exact grant the deployer journaled and runs an execution
        version behind the deployment target. Stopping the worker is a
        planned crash; expired-owner recovery then settles what it left
        without replaying any effect and restores every project it owned on
        the target version. A refusal here changed nothing.
        """
        async with self._operations:
            self._active()
            async with self.coordinator.database.session() as session:
                host = await session.get(DeploymentHostRow, "host")
                owner = await session.get(
                    ProjectOwnershipRow, expected_owner.project_id
                )
                process = (
                    await session.get(RuntimeProcessRow, expected_owner.runtime_id)
                    if owner is not None
                    else None
                )
                affected = (
                    sorted(
                        await session.scalars(
                            select(ProjectOwnershipRow.project_id).where(
                                ProjectOwnershipRow.runtime_id
                                == expected_owner.runtime_id
                            )
                        )
                    )
                    if process is not None
                    else []
                )
            if owner is None or (owner.runtime_id, owner.fencing_token) != (
                expected_owner.runtime_id,
                expected_owner.fencing_token,
            ):
                raise OperationRejected(
                    "Project ownership changed; read fresh deployment status"
                )
            if (
                host is None
                or host.target_version is None
                or host.shutdown_requested
                or process is None
                or process.role != "execution"
                or process.version == host.target_version
                or host.api_runtime_id == process.runtime_id
            ):
                raise OperationRejected(
                    "Only a source worker behind the deployment target can be stopped"
                )
            try:
                signalled = await self.host.stop_overdue_source(process.runtime_id)
            except RuntimeIpcError as error:
                raise OperationRejected(str(error)) from error
            return {
                "project_id": expected_owner.project_id,
                "runtime_id": expected_owner.runtime_id,
                "fencing_token": expected_owner.fencing_token,
                "version": process.version,
                "affected_project_ids": affected,
                "signalled": signalled,
                "effects_replayed": False,
            }

    async def recover_project(
        self,
        project_id: str,
        target_runtime_id: str,
        *,
        expected_owner: ProjectLease,
        expected_handoff_revision: int | None,
        operation_id: str,
        actor_id: str,
    ):
        async with self._operations:
            self._active()
            # Everything up to begin_recovery's commit changes nothing, so a
            # refusal here is a rejection the caller may safely ask again.
            try:
                client = await self.host.rpc(target_runtime_id)
                try:
                    status = await client.call("execution.status/v1", {}, timeout=5)
                    binding = status.get("lease")
                    if status.get("state") != "unassigned" and (
                        not binding or binding.get("project_id") != project_id
                    ):
                        raise CoordinationConflict(
                            "Prepare an unassigned recovery target"
                        )
                finally:
                    await client.close()
                lease = await self.coordinator.begin_recovery(
                    project_id,
                    target_runtime_id,
                    expected_owner=expected_owner,
                    expected_handoff_revision=expected_handoff_revision,
                    operation_id=operation_id,
                    actor_id=actor_id,
                )
            except (
                CoordinationConflict,
                RuntimeIpcError,
                httpx.HTTPError,
                OSError,
            ) as error:
                # Never a database error: a failed COMMIT has an unknown outcome.
                if isinstance(error, OperationRejected):
                    raise
                raise OperationRejected(
                    str(error)
                    if isinstance(error, (CoordinationConflict, RuntimeIpcError))
                    else f"The recovery target did not answer ({type(error).__name__})"
                ) from error
        async with self._handoff_serial:
            async with self.coordinator.database.session() as session:
                row = await session.get(ProjectHandoffRow, project_id)
            if row is None or row.checkpoint.get("operation_id") != operation_id:
                raise CoordinationConflict("The original recovery operation changed")
            return await self._advance_project_recovery(row, expected_lease=lease)

    async def activate_execution(self, lease: ProjectLease, *, timeout=30):
        """Start restored controllers only after their durable verification."""
        async with self._operations:
            self._active()
            client = await self.host.rpc(lease.runtime_id)
            try:
                await client.call("execution.activate/v1", {}, timeout=5)
                async with asyncio.timeout(timeout):
                    while True:
                        status = await client.call("execution.status/v1", {}, timeout=5)
                        if status.get("lease") != asdict(lease):
                            raise RuntimeIpcError(
                                "Activation does not match the committed grant"
                            )
                        if status.get("state") == "ready":
                            return lease
                        if status.get("state") not in {"prepared", "starting"}:
                            raise CoordinationConflict(
                                "Restored project activation requires reconciliation"
                            )
                        await asyncio.sleep(0.05)
            finally:
                await client.close()

    async def promote_api(self, runtime_id: str, *, expected_revision: int):
        async with self._operations:
            self._active()
            await self.coordinator.promote_api(
                runtime_id, expected_revision=expected_revision
            )

    async def rollback_code(self, version: str, *, expected_revision: int):
        async with self._operations:
            self._active()
            await self.coordinator.rollback_code(
                version, expected_revision=expected_revision
            )
        try:
            identity = await self.prepare_api(version)
            status = await self.coordinator.status()
            await self.promote_api(
                identity.runtime_id, expected_revision=status.revision
            )
        except OperationRejected as error:
            raise CoordinationConflict(str(error)) from error

    async def shutdown(self, *, expected_revision: int):
        async with self._operations:
            self._active()
            result = await self.host.shutdown(expected_revision=expected_revision)
            self._shutdown_committed = True
            return result

    async def close(self):
        if self._close_task is None:
            # Refuse new requests immediately. Deployment operations already
            # accepted, including those whose request a stopping generation
            # cancelled, finish with a definite receipt before host authority
            # is released; only then do later operations fail as closed.
            self.closing = True
            pending = set(self.management.inflight)
            caller = asyncio.current_task()

            async def cleanup():
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                self.closed, self.started = True, False
                # Drain every accepted copy/RPC/preparation before releasing
                # host authority.
                async with self._operations:
                    await self._detach_resources(caller)

            # diagnostic-expected: this detach task is shielded and joined by finish_cleanup below.
            self._close_task = asyncio.create_task(
                cleanup(), name="nebula-supervisor-detach"
            )

        close_task = self._close_task
        assert close_task is not None

        async def join():
            await close_task

        await finish_cleanup(join())

    async def _detach_resources(self, caller):
        failures = []
        self._prune_stop.set()
        if self._prune_task is not None:
            try:
                await cancel_and_join(self._prune_task)
            except BaseException as error:
                # diagnostic-expected: preserve this cleanup failure for the BaseExceptionGroup after every resource is attempted.
                failures.append(error)
        if self._handoff_task is not None and self._handoff_task is not caller:
            try:
                await cancel_and_join(self._handoff_task)
            except Exception:
                # diagnostic-expected: the handoff loop records its own failures; detachment continues.
                pass
        try:
            await self.proxy.close()
        except BaseException as error:
            # diagnostic-expected: preserve this cleanup failure for the BaseExceptionGroup after every resource is attempted.
            failures.append(error)
        pending = {task for task in self.viewers if task is not caller}
        if pending:
            _done, waiting = await asyncio.wait(pending, timeout=5)
            for task in waiting:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for operation in (
            self.shared_services.close,
            self.host.close,
            self.store.event_history.close,
            self.store.database.dispose,
        ):
            try:
                await operation()
            except BaseException as error:
                # diagnostic-expected: preserve this cleanup failure for the BaseExceptionGroup after every resource is attempted.
                failures.append(error)
        if failures:
            raise BaseExceptionGroup(
                "Supervisor detachment cleanup did not complete", failures
            )
