"""Stable public listener and retained-process orchestration owned by the host."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from builtins import BaseExceptionGroup
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response, WebSocket
from sqlalchemy import select
from starlette.responses import JSONResponse

from .async_utils import finish_cleanup
from .authentication import CoreAuthentication
from .database import (
    ProjectOwnershipRow,
    ProjectHandoffRow,
    RuntimeInstanceRow,
    RuntimeProcessRow,
    RuntimeReleaseRow,
)
from .deployment_protocol import ReleaseManifest, WorkerIdentity
from .diagnostics import record_caught_exception
from .deployments import _RESIDENT
from .release_store import ReleaseStore
from .runtime_coordination import CoordinationConflict, ProjectLease
from .runtime_http import RuntimeHttpProxy
from .runtime_ipc import RuntimeIpcError
from .runtime_processes import RuntimeProcessHost
from .runtime_routing import RuntimeRouting
from .runtime_memory import RuntimeMemoryAdmission
from .runtime_host_services import HostSharedServices
from .storage import NebulaStore


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
        if owner.closed or not owner.started:
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
        try:
            await self.app(scope, receive, send)
        finally:
            owner.viewers.discard(task)


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
    ):
        if not operator_token:
            raise ValueError("supervisor requires normal Core authentication")
        self.host, self.coordinator = host, host.coordinator
        self.shared_services = shared_services or HostSharedServices(
            host,
            memory_budgets=worker_admission.budgets
            if isinstance(worker_admission, RuntimeMemoryAdmission)
            else None,
        )
        if self.shared_services.host is not host:
            raise ValueError("Shared services must use this supervisor's process host")
        self.store = NebulaStore(self.coordinator.database)
        self.shared_services.configure_memory_control(operator_token)
        self.retained = ReleaseStore(retained_root or host.root / "releases")
        self.worker_admission = worker_admission
        if isinstance(worker_admission, RuntimeMemoryAdmission):
            if worker_admission.budgets.database is not self.coordinator.database:
                raise ValueError(
                    "runtime admission and deployment must share the host database"
                )
            self.coordinator.runtime_admission = worker_admission.check
        self.routing = RuntimeRouting(
            self.coordinator.coordination, host.credentials.directory
        )
        self.proxy = RuntimeHttpProxy(self.routing.api_upstream)
        self.started, self.closed = False, False
        self._operations = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._handoff_task = None
        self._handoff_serial = asyncio.Lock()
        self.viewers: set[asyncio.Task] = set()
        authentication = CoreAuthentication(self.store, operator_token)

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
        @api.api_route("/api/v1/memory-budgets/{path:path}", methods=["GET", "POST"])
        async def memory_control(request: Request):
            if request.url.path.endswith("/apply"):
                return _Forward(self.proxy)
            return _Forward(self.shared_services.memory_control.app)

        @api.api_route(
            "/{path:path}",
            methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
        )
        async def forward(request: Request):
            if request.url.path.startswith("/internal/"):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            return _Forward(self.proxy)

        @api.websocket("/{path:path}")
        async def websocket(websocket: WebSocket):
            if websocket.url.path.startswith("/internal/"):
                await websocket.close(code=4404)
                return
            await self.proxy(websocket.scope, websocket.receive, websocket.send)

        self.app = _Availability(api, self)

    async def start(self):
        async with self._operations:
            if self.closed:
                raise RuntimeError("a closed supervisor cannot restart")
            if not self.started:
                await self.host.start()
                try:
                    await self.shared_services.start()
                except BaseException:
                    await self.shared_services.close()
                    await self.host.close()
                    raise
                self.started = True
                self._handoff_task = asyncio.create_task(
                    self._handoff_loop(), name="nebula-host-handoffs"
                )

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
                rows = list(
                    await session.scalars(
                        select(ProjectHandoffRow)
                        .where(
                            ProjectHandoffRow.phase.in_(("transferable", "transferred"))
                        )
                        .limit(64)
                    )
                )
            for row in rows:
                if row.checkpoint.get("format") != "nebula.project-checkpoint/v1":
                    continue
                try:
                    await self._advance_project_handoff(row)
                except Exception as error:
                    # A blocked target must not starve transfers for other projects.
                    record_caught_exception(
                        "runtime",
                        "runtime.project_restore.waiting",
                        "This project remains held pending target restoration.",
                        error,
                        stage="handoff",
                    )
                    async with self.coordinator.coordination._write() as session:
                        pending = await session.get(ProjectHandoffRow, row.project_id)
                        if pending is not None and pending.phase == "transferred":
                            pending.blockers = [
                                "Target runtime "
                                + pending.target_runtime_id
                                + " has not verified restoration; queued work remains held"
                            ]
                            pending.revision += 1

    async def _advance_project_handoff(self, row):
        if row.phase == "transferable":
            async with self._operations:
                self._active()
                await self.coordinator.transfer(row.project_id)
        lease = await self.attach_execution(row.project_id, row.target_runtime_id)
        client = await self.host.rpc(row.target_runtime_id)
        try:
            status = await client.call("execution.status/v1", {}, timeout=5)
        finally:
            await client.close()
        if status.get("state") != "ready" or not status.get(
            "restored_checkpoint_digest"
        ):
            raise CoordinationConflict(
                "The target has not verified its restored project state"
            )
        await self.coordinator.verified(
            lease, restored_checkpoint_digest=status["restored_checkpoint_digest"]
        )

    async def _handoff_loop(self):
        while not self.closed and not self.host.closed:
            try:
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

    async def prepare_api(self, version: str) -> WorkerIdentity:
        async with self._operations:
            self._active()
            async with self.coordinator.database.session() as session:
                release = await session.get(RuntimeReleaseRow, version)
                if release is None:
                    raise CoordinationConflict(
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
            # Inactive APIs own only viewers. A graceful retirement may detach
            # them; their accepted commands and delivery remain in SQL.
            for process in list(processes):
                if process.state == "draining":
                    await self.host.retire(process.runtime_id)
                    await self.proxy.discard_endpoint(process.endpoint)
                    processes.remove(process)
            if any(process.version == version for process in processes):
                raise CoordinationConflict(
                    "An earlier candidate incarnation still needs recovery"
                )
            if len(processes) >= 2:
                raise CoordinationConflict(
                    "Two API processes remain resident; reconcile the inactive candidate"
                )
            if self.worker_admission is None:
                raise CoordinationConflict(
                    "Host worker memory reserve must be configured before candidate startup"
                )
            await self.worker_admission(manifest, "api")
            spawned = await self.host.spawn(version, "api")
            if spawned is None:
                raise CoordinationConflict("The candidate remains staged")
            return spawned

    async def prepare_execution(self, version: str) -> WorkerIdentity:
        """Prepare an unassigned process; controllers wait for a project grant."""
        async with self._operations:
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
            spawned = await self.host.spawn(version, "execution")
            if spawned is None:
                raise CoordinationConflict("The execution candidate remains staged")
            return spawned

    async def attach_execution(self, project_id: str, runtime_id: str, *, timeout=30):
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
                if lease is None:
                    lease = await self.coordinator.coordination.claim_project(
                        project_id, runtime_id
                    )
                await client.call("execution.attach/v1", asdict(lease), timeout=5)
                async with asyncio.timeout(timeout):
                    while True:
                        status = await client.call("execution.status/v1", {}, timeout=5)
                        if status.get("lease") != asdict(lease):
                            raise RuntimeIpcError(
                                "Execution attachment does not match the committed grant"
                            )
                        if status.get("state") == "ready":
                            return lease
                        if status.get("state") != "starting":
                            raise CoordinationConflict(
                                "Project startup needs reconciliation; its saved work remains held"
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

    async def close(self):
        if self._close_task is None:
            self.closed, self.started = True, False
            caller = asyncio.current_task()

            async def cleanup():
                # Stop admitting operations immediately, then drain every
                # accepted copy/RPC/preparation before releasing host authority.
                async with self._operations:
                    await self._detach_resources(caller)

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
        if self._handoff_task is not None and self._handoff_task is not caller:
            self._handoff_task.cancel()
            await asyncio.gather(self._handoff_task, return_exceptions=True)
        try:
            await self.proxy.close()
        except BaseException as error:
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
                failures.append(error)
        if failures:
            raise BaseExceptionGroup(
                "Supervisor detachment cleanup did not complete", failures
            )
