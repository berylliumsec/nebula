"""Private worker process lifecycle, independent of a desktop or API viewer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
from typing import Any

import uvicorn

from .database import Database, RuntimeProcessRow
from .database_pool import PoolSettings
from .deployment_protocol import ReleaseManifest
from .deployments import DeploymentCoordinator
from .release_store import ReleaseStore
from .runtime_coordination import HEARTBEAT_SECONDS
from .runtime_http import PrivateRuntimeHttp
from .runtime_ipc import RPC_PATH, RuntimeIpcError, RuntimeRpcServer
from .runtime_processes import (
    WorkerBootstrap,
    read_worker_bootstrap,
    validate_worker_binding,
)


@dataclass
class WorkerContext:
    bootstrap: WorkerBootstrap
    database: Database
    coordinator: DeploymentCoordinator
    manifest: ReleaseManifest
    secret: bytes = field(repr=False)


WorkerFactory = Callable[[WorkerContext], AbstractAsyncContextManager[Any]]


@asynccontextmanager
async def api_application(context: WorkerContext) -> AsyncIterator[Any]:
    # Imports stay local to the selected role. This path never imports the
    # execution kernel or constructs any producer or shared host service.
    from .api_worker import create_api_worker
    from .runtime_delivery import RuntimeDelivery
    from .runtime_routing import RuntimeRouting
    from .runtime_vault import RuntimeVault
    from .storage import NebulaStore

    bootstrap = context.bootstrap
    if bootstrap.operator_token is None:
        raise RuntimeIpcError("API worker requires the private operator bootstrap")
    settings = (
        PoolSettings(**bootstrap.pool_settings) if bootstrap.pool_settings else None
    )
    store = NebulaStore(
        Database(bootstrap.database, bootstrap=False, pool_settings=settings)
    )
    routing = RuntimeRouting(
        context.coordinator.coordination, Path(bootstrap.credential_directory)
    )
    delivery = RuntimeDelivery(
        context.coordinator.coordination,
        RuntimeVault(Path(bootstrap.data_root) / "runtime" / "payloads"),
    )
    app = create_api_worker(
        store,
        delivery,
        operator_token=bootstrap.operator_token.get_secret_value(),
        project_for=routing.project_for,
        upstream_for=routing.upstream_for,
        static_dir=Path(bootstrap.release_root) / context.manifest.ui_directory,
    )
    async with app.router.lifespan_context(app):
        yield app


async def run_worker(
    bootstrap: WorkerBootstrap, *, execution_factory: WorkerFactory | None = None
):
    """Validate the committed incarnation before opening controllers or sockets.

    Execution construction is injected by the execution role integration. An
    unavailable role fails before readiness; there is no in-process fallback.
    Compatibility launchers remain disabled until that integration is complete.
    """
    if not bootstrap.identity.endpoint.startswith("unix:///"):
        raise RuntimeIpcError("workers require an explicit private Unix endpoint")
    settings = (
        PoolSettings(**bootstrap.pool_settings) if bootstrap.pool_settings else None
    )
    database = Database(bootstrap.database, bootstrap=False, pool_settings=settings)
    coordinator = DeploymentCoordinator(database)
    heartbeat = None
    server = None
    try:
        secret = await validate_worker_binding(bootstrap, coordinator)
        manifest = await asyncio.to_thread(
            ReleaseStore.verify,
            Path(bootstrap.release_root),
            expected_manifest_sha256=bootstrap.manifest_sha256,
        )
        if manifest.version != bootstrap.identity.version:
            raise RuntimeIpcError(
                "worker version differs from its retained application"
            )
        schema = await database.current_schema_version()
        if not manifest.schema_min <= schema <= manifest.schema_max:
            raise RuntimeIpcError(
                "retained application cannot use the current SQL schema"
            )
        factory = (
            api_application if bootstrap.identity.role == "api" else execution_factory
        )
        if factory is None:
            raise RuntimeIpcError("The execution role is not enabled in this release")
        context = WorkerContext(bootstrap, database, coordinator, manifest, secret)
        rpc = RuntimeRpcServer(secret)

        async def identity(payload):
            if payload:
                raise RuntimeIpcError("worker identity accepts no arguments")
            return bootstrap.identity.model_dump(mode="json")

        async def shutdown(payload):
            if payload:
                raise RuntimeIpcError("worker shutdown accepts no arguments")
            async with database.session() as session:
                process = await session.get(
                    RuntimeProcessRow, bootstrap.identity.runtime_id
                )
                if process is None or process.state != "draining":
                    raise RuntimeIpcError(
                        "Supervisor must fence retirement before worker shutdown"
                    )
            blockers = await coordinator.retirement_blockers(
                bootstrap.identity.runtime_id
            )
            if blockers:
                raise RuntimeIpcError("Resident project work prevents worker shutdown")
            assert server is not None
            server.should_exit = True
            return {"stopping": True}

        rpc.register("worker.identity", identity)
        rpc.register("worker.shutdown", shutdown)

        async def pulse():
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                try:
                    await coordinator.coordination.heartbeat(
                        bootstrap.identity.runtime_id
                    )
                except BaseException:
                    if server is not None:
                        server.should_exit = True
                    raise

        heartbeat = asyncio.create_task(pulse(), name="nebula-worker-heartbeat")
        async with factory(context) as application:
            private = PrivateRuntimeHttp(application, secret)

            async def multiplex(scope, receive, send):
                if scope["type"] == "http" and scope["path"] == RPC_PATH:
                    await rpc.app(scope, receive, send)
                else:

                    async def identified(message):
                        if message["type"] == "http.response.start":
                            message = {
                                **message,
                                "headers": [
                                    *message.get("headers", []),
                                    (
                                        b"x-nebula-api-version",
                                        bootstrap.identity.version.encode(),
                                    ),
                                ],
                            }
                        await send(message)

                    await private(scope, receive, identified)

            server = uvicorn.Server(
                uvicorn.Config(
                    multiplex,
                    uds=bootstrap.identity.endpoint[7:],
                    lifespan="off",
                    access_log=False,
                    log_level="error",
                    timeout_graceful_shutdown=5,
                )
            )
            if heartbeat.done():
                await heartbeat
            await server.serve()
            if heartbeat.done():
                await heartbeat
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        await database.dispose()


def main():
    os.umask(0o077)
    bootstrap = read_worker_bootstrap(sys.stdin.buffer)
    asyncio.run(run_worker(bootstrap))
