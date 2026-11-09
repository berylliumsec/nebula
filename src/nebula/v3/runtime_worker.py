"""Private worker process lifecycle, independent of a desktop or API viewer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
from typing import Any

import uvicorn
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from .database import (
    Database,
    ProjectHandoffRow,
    ProviderToolManifestRow,
    RuntimeChatBindingRow,
    RuntimeProcessRow,
)
from .database_pool import PoolSettings
from .deployment_protocol import ReleaseManifest
from .deployments import DeploymentCoordinator
from .release_store import ReleaseStore
from .runtime_coordination import HEARTBEAT_SECONDS
from .runtime_context_requirements import (
    HOST_SEARCH_FEATURE,
    PROVIDER_TOOLS_FEATURE,
    STANDING_INSTRUCTIONS_FEATURE,
)
from .runtime_http import PrivateRuntimeHttp
from .runtime_ipc import RPC_PATH, RuntimeIpcError, RuntimeRpcServer
from .runtime_processes import (
    WorkerBootstrap,
    MigrationBootstrap,
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
    rpc: RuntimeRpcServer = field(repr=False)


WorkerFactory = Callable[[WorkerContext], AbstractAsyncContextManager[Any]]


async def run_migration(bootstrap: MigrationBootstrap) -> dict:
    """Apply this accepted executable's additive migrations, never a worker's."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    manifest = ReleaseStore.verify(
        Path(bootstrap.release_root),
        expected_manifest_sha256=bootstrap.manifest_sha256,
    )
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("migrations"))
    )
    if not manifest.additive_migrations or ScriptDirectory.from_config(
        config
    ).get_heads() != [manifest.migration_head]:
        raise RuntimeIpcError("Accepted migration head does not match this executable")
    database = Database(bootstrap.database, bootstrap=False)
    try:
        await database.initialize()
        schema = await database.current_schema_version()
        async with database.session() as session:
            heads = list(
                await session.scalars(text("SELECT version_num FROM alembic_version"))
            )
        if schema != bootstrap.expected_schema or heads != [bootstrap.expected_head]:
            raise RuntimeIpcError("SQL changed after migration preflight")
        await database.bootstrap()
        schema = await database.current_schema_version()
        async with database.session() as session:
            heads = list(
                await session.scalars(text("SELECT version_num FROM alembic_version"))
            )
        if (
            heads != [manifest.migration_head]
            or not manifest.schema_min <= schema <= manifest.schema_max
        ):
            raise RuntimeIpcError(
                "Migration did not reach the accepted compatible schema"
            )
        return {"version": manifest.version, "schema": schema, "head": heads[0]}
    finally:
        await database.dispose()


def migration_main() -> None:
    raw = sys.stdin.buffer.readline(64 * 1024 + 1)
    if len(raw) > 64 * 1024 or not raw.endswith(b"\n"):
        raise RuntimeIpcError("Invalid private migration bootstrap")
    bootstrap = MigrationBootstrap.model_validate_json(raw)
    print(json.dumps(asyncio.run(run_migration(bootstrap)), separators=(",", ":")))


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
    # Global SQL handlers and their command receipts must share the exact
    # task-owned Session. A second Database for the same URL is a different
    # transaction owner and deadlocks SQLite's outer writer.
    store = NebulaStore(context.database)
    routing = RuntimeRouting(
        context.coordinator.coordination,
        Path(bootstrap.credential_directory),
        request_admission=True,
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
        chat_acceptance_for=routing.supports_chat_acceptance,
        chat_resume_for=routing.supports_chat_resume,
        upstream_for=routing.upstream_for,
        static_dir=Path(bootstrap.release_root) / context.manifest.ui_directory,
        dispose_database=False,
    )
    async with app.router.lifespan_context(app):
        yield app


async def run_worker(
    bootstrap: WorkerBootstrap, *, execution_factory: WorkerFactory | None = None
):
    """Validate the committed incarnation before opening controllers or sockets.

    The execution role opens its private control socket before project assignment.
    Controllers are constructed only after an exact SQL ownership grant. Tests
    may inject an inert role; compatibility launchers remain separately gated.
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
    diagnostics = None
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
        if set(manifest.features) - {
            "chat.accept/v1",
            "chat.resume/v1",
            "project.handoff/v1",
            "project.participants/v1",
            "project.quiesce/v1",
            PROVIDER_TOOLS_FEATURE,
            STANDING_INSTRUCTIONS_FEATURE,
            HOST_SEARCH_FEATURE,
        }:
            raise RuntimeIpcError(
                "The retained worker cannot provide its declared release features"
            )
        schema = await database.current_schema_version()
        if not manifest.schema_min <= schema <= manifest.schema_max:
            raise RuntimeIpcError(
                "retained application cannot use the current SQL schema"
            )
        if bootstrap.identity.role == "api" or set(manifest.features) & {
            "chat.accept/v1",
            "chat.resume/v1",
        }:
            try:
                async with database.session() as session:
                    await session.execute(select(RuntimeChatBindingRow).limit(0))
            except SQLAlchemyError:
                raise RuntimeIpcError(
                    "The supervisor must apply additive chat migrations before worker readiness"
                ) from None
        if "project.handoff/v1" in manifest.features:
            try:
                async with database.session() as session:
                    await session.execute(select(ProjectHandoffRow).limit(0))
            except SQLAlchemyError:
                raise RuntimeIpcError(
                    "The supervisor must apply additive handoff migrations before readiness"
                ) from None
        if PROVIDER_TOOLS_FEATURE in manifest.features:
            try:
                async with database.session() as session:
                    await session.execute(select(ProviderToolManifestRow).limit(0))
            except SQLAlchemyError:
                # diagnostic-expected: missing additive schema holds readiness;
                # the independently retained worker never applies migrations.
                raise RuntimeIpcError(
                    "The supervisor must apply additive provider tool migrations before worker readiness"
                ) from None
        from .diagnostics import configure_diagnostics

        diagnostic_root = (
            Path(bootstrap.data_root)
            / "runtime"
            / "workers"
            / bootstrap.identity.runtime_id
        )
        diagnostics = configure_diagnostics(
            diagnostic_root,
            log_dir=diagnostic_root / "logs",
            settings_path=Path(
                os.getenv(
                    "NEBULA_V3_DIAGNOSTICS_SETTINGS",
                    str(Path(bootstrap.data_root) / "diagnostics-settings.json"),
                )
            ),
            desktop_parent=False,
        )
        factory: WorkerFactory
        if bootstrap.identity.role == "api":
            factory = api_application
        else:
            from .execution_worker import execution_application

            factory = execution_factory or execution_application
        rpc = RuntimeRpcServer(secret)
        context = WorkerContext(bootstrap, database, coordinator, manifest, secret, rpc)

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

        # diagnostic-expected: heartbeat failure requests server exit and is awaited before the worker returns.
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
                                        b"x-nebula-api-version"
                                        if bootstrap.identity.role == "api"
                                        else b"x-nebula-execution-version",
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
        if diagnostics is not None:
            await asyncio.to_thread(diagnostics.close)


def main():
    os.umask(0o077)
    bootstrap = read_worker_bootstrap(sys.stdin.buffer)
    asyncio.run(run_worker(bootstrap))
