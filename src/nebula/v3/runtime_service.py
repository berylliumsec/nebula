"""Production composition for the independently owned Core supervisor."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import sys
import webbrowser

import uvicorn
from sqlalchemy import inspect

from .database import Base, Database
from .database_pool import PoolSettings
from .deployments import DeploymentCoordinator
from .diagnostics import configure_diagnostics, install_exception_hooks
from .memory_budget import MemoryBudgetService
from .process_identity import process_birth
from .release_store import ReleaseStore
from .runtime_host_services import HostSharedServices, create_host_knowledge_index
from .runtime_launcher import (
    CONNECTION_NAME,
    DESKTOP_PROTOCOL,
    operator_token,
    runtime_root,
    write_private,
    read_private,
)
from .runtime_memory import RuntimeMemoryAdmission
from .runtime_processes import RuntimeProcessHost
from .supervisor import RuntimeSupervisor

OFFLINE_ADOPTION = "offline-adoption.json"
# First descriptor passed by socket activation (sd_listen_fds).
LISTEN_FDS_START = 3
# While a holder keeps the public socket, a stopping supervisor relays open
# viewer streams this long, then closes them; viewers resume from their saved
# cursor on the next generation. An unbounded drain keeps the host lock, and
# with it the replacement, waiting on the longest-lived stream. A
# self-bound listener keeps relaying every exchange to completion.
VIEWER_DRAIN_SECONDS = 5
# An exchange still waiting for its response headers is never closed before
# it answers, up to this bound. The API worker answers an accepted request
# with its saved receipt within 15 s.
RELAY_ANSWER_LIMIT_SECONDS = 30


def relay_drain(server, supervisor, *, inherited: bool):
    """Start the bounded drain for a stopping generation, if a holder exists."""
    if not inherited:
        return None
    from .diagnostics import create_diagnostic_task
    from .supervisor import drain_relays

    async def when_stopping():
        while not server.should_exit:
            await asyncio.sleep(0.1)
        await drain_relays(
            supervisor, VIEWER_DRAIN_SECONDS, limit=RELAY_ANSWER_LIMIT_SECONDS
        )

    return create_diagnostic_task(
        when_stopping(),
        feature="runtime",
        event_code="runtime.relay_drain.failed",
        failure_message="Relayed viewer streams were not drained.",
        name="nebula-relay-drain",
    )


def inherited_listener(
    host: str, port: int, *, descriptor: int | None = None
) -> socket.socket | None:
    """Adopt a listening socket held by a longer-lived owner.

    When a holder that never restarts (a systemd socket unit) owns the public
    socket, connections that arrive while one supervisor generation replaces
    another wait in the kernel accept queue instead of being refused. The
    socket is adopted when socket activation names this exact process
    (LISTEN_PID/LISTEN_FDS), or when the operator passes its descriptor
    explicitly because a launcher (a onefile bootloader or wrapper) sits
    between the holder and Core. Returns None when nothing was passed.
    """
    activated = os.environ.get("LISTEN_PID") == str(os.getpid())
    count = os.environ.get("LISTEN_FDS")
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(name, None)
    if descriptor is None:
        if not activated:
            return None
        if count != "1":
            raise ValueError("Core accepts exactly one inherited listening socket")
        descriptor = LISTEN_FDS_START
    try:
        requested = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(
            "An inherited Core socket requires an IP address for --host"
        ) from None
    try:
        is_socket = stat.S_ISSOCK(os.fstat(descriptor).st_mode)
    except OSError as error:
        raise ValueError("The inherited Core descriptor is not open") from error
    if not is_socket:
        raise ValueError("The inherited Core descriptor is not a socket")
    # Validate before taking ownership: a wrong descriptor may belong to this
    # process (an event-loop socketpair). It is never closed or modified.
    listener = socket.socket(fileno=descriptor)
    try:
        if (
            listener.family not in (socket.AF_INET, socket.AF_INET6)
            or listener.type != socket.SOCK_STREAM
            or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
        ):
            raise ValueError("The inherited Core socket is not a listening TCP socket")
        bound = listener.getsockname()
        # The bound address, not the flag, decides exposure: a wildcard or
        # different address never passes as the requested loopback one.
        if bound[1] != port or ipaddress.ip_address(bound[0]) != requested:
            raise ValueError(
                "The inherited Core socket does not match the requested --host/--port"
            )
    except BaseException:
        listener.detach()
        raise
    listener.set_inheritable(False)
    return listener


async def initialize_database(database: Database, data_root: Path) -> None:
    """Bootstrap only empty or explicitly offline-adopted legacy storage.

    Routine recovery never applies a candidate's migrations to retained workers.
    A missing control table or column requires the previous supervisor's verified
    migration operation, rather than silently running a new Alembic head.
    """
    await database.initialize()
    marker = runtime_root(data_root) / OFFLINE_ADOPTION
    try:
        adoption = json.loads(read_private(marker))
    except FileNotFoundError:
        # diagnostic-expected: normal installations have no offline legacy-adoption marker.
        adoption = None
    if adoption is not None and adoption != {
        "state": "offline-legacy-adoption",
        "data_root": str(data_root.resolve()),
    }:
        raise ValueError("Offline adoption marker is invalid; preserve it for recovery")
    async with database.engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync: set(inspect(sync).get_table_names())
        )
    if not tables or adoption is not None:
        await database.bootstrap()
        marker.unlink(missing_ok=True)
        return
    async with database.engine.connect() as connection:

        def missing_columns(sync):
            inspection = inspect(sync)
            for name, table in Base.metadata.tables.items():
                if name not in tables:
                    return True
                columns = {column["name"] for column in inspection.get_columns(name)}
                if not set(table.columns.keys()).issubset(columns):
                    return True
            return False

        missing = await connection.run_sync(missing_columns)
    if missing:
        raise RuntimeError(
            "This retained supervisor needs a schema migration. Use the previous retained supervisor's verified migration operation; routine reopen never migrates existing workers."
        )


def runtime_pool_settings(location: str | Path) -> PoolSettings | None:
    """Require fleet budgets only for PostgreSQL, as Database itself does."""
    from sqlalchemy.engine import make_url

    dialect = make_url(Database._normalize_sync_url(location)).get_backend_name()
    return PoolSettings.from_environment() if dialect == "postgresql" else None


def configure_retained_resources(bundle: Path) -> None:
    """Each process uses resources from its own retained generation."""
    for name, directory in (
        ("PLAYWRIGHT_BROWSERS_PATH", "playwright-browsers"),
        ("NEBULA_NOTEBOOK_RUNTIME_DIR", "notebook-runtime"),
    ):
        resource = bundle / "resources" / directory
        if resource.is_dir():
            os.environ[name] = str(resource)
    addon = bundle / "resources/security-proxy/addon.py"
    if addon.is_file():
        os.environ["NEBULA_SECURITY_PROXY_ADDON"] = str(addon)


async def serve_runtime(
    *,
    data_root: Path,
    bundle: Path,
    manifest_sha256: str,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    allow_insecure_device_pairing: bool = False,
    allow_browser_diagnostics: bool = False,
    open_browser: bool = False,
    print_ready: bool = False,
    diagnostics_level: str | None = None,
    startup_id: str | None = None,
    listener: socket.socket | None = None,
) -> None:
    if not sys.platform.startswith("linux"):
        raise ValueError(
            "Independent Core workers currently require Linux pidfd support"
        )
    data_root = data_root.expanduser().resolve()
    root = runtime_root(data_root)
    token = operator_token(data_root, token)
    manifest = ReleaseStore.verify(bundle, expected_manifest_sha256=manifest_sha256)
    configure_retained_resources(bundle)
    configure_diagnostics(data_root, level_override=diagnostics_level)
    install_exception_hooks()
    location = os.getenv("NEBULA_V3_DATABASE_URL") or data_root / "nebula.db"
    settings = runtime_pool_settings(location)
    database = Database(
        location,
        bootstrap=False,
        pool_settings=settings,
    )
    coordinator = DeploymentCoordinator(database)
    budgets = MemoryBudgetService(
        database, runtime_defaults_bytes=3 * manifest.execution_reserve_bytes
    )
    admission = RuntimeMemoryAdmission(budgets)
    process_host = RuntimeProcessHost(
        coordinator, data_root, pool_settings=settings, operator_token=token
    )
    shared = HostSharedServices(
        process_host,
        memory_budgets=budgets,
        knowledge_index_factory=create_host_knowledge_index,
    )
    server = None

    async def shutdown_requested():
        if server is not None:
            server.should_exit = True

    supervisor = RuntimeSupervisor(
        process_host,
        operator_token=token,
        worker_admission=admission,
        shared_services=shared,
        recovery_version=None,
        allow_insecure_device_pairing=allow_insecure_device_pairing,
        allow_browser_diagnostics=allow_browser_diagnostics,
        shutdown_requested=shutdown_requested,
    )
    inherited = listener is not None
    if listener is None:
        listener = socket.socket(
            socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM
        )
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    connection = None
    automatic = None
    drain = None
    try:
        # Bind before opening controllers or migrating legacy state into a live
        # runtime. An occupied compatibility port is never replaced silently.
        # An inherited socket stays open in its holder across generations.
        if not inherited:
            listener.bind((host, port))
            listener.listen(2048)
        port = listener.getsockname()[1]
        from .automation_runtime import resolve_callback_origin

        os.environ["NEBULA_CALLBACK_ORIGIN"] = resolve_callback_origin(
            host=host, port=port, tls=tls_cert is not None
        )
        process_host.lock.acquire()
        await initialize_database(database, data_root)
        schema = await database.current_schema_version()
        if not manifest.schema_min <= schema <= manifest.schema_max:
            raise RuntimeError(
                "The retained supervisor is incompatible with this database schema"
            )
        await supervisor.start()
        await supervisor.stage(bundle, manifest_sha256=manifest_sha256)
        await supervisor.select_recovery_release(manifest.version)
        from .runtime_auto_admission import RuntimeAutoAdmission

        automatic = RuntimeAutoAdmission(supervisor, default_version=manifest.version)
        await automatic.start()
        scheme = "https" if tls_cert else "http"
        display_host = f"[{host}]" if ":" in host else host
        endpoint = f"{scheme}://{display_host}:{port}/api/v1"
        connection = {
            "protocol": DESKTOP_PROTOCOL,
            "data_root": str(data_root),
            "endpoint": endpoint,
            "pid": os.getpid(),
            "process_birth": process_birth(os.getpid()),
            "version": manifest.version,
            "startup_id": startup_id,
        }
        write_private(root / CONNECTION_NAME, json.dumps(connection).encode())
        if print_ready:
            print(
                json.dumps(
                    {
                        "kind": "nebula-api-ready",
                        "url": endpoint.removesuffix("/api/v1"),
                        "token": token,
                        "local_only": host in {"127.0.0.1", "::1", "localhost"},
                        "authentication": "bearer",
                    }
                ),
                flush=True,
            )
        if open_browser:
            webbrowser.open(endpoint.removesuffix("/api/v1") + "/#token=" + token)
        server = uvicorn.Server(
            uvicorn.Config(
                supervisor.app,
                host=host,
                port=port,
                log_level="error",
                access_log=False,
                ssl_certfile=str(tls_cert) if tls_cert else None,
                ssl_keyfile=str(tls_key) if tls_key else None,
            )
        )
        drain = relay_drain(server, supervisor, inherited=inherited)
        await server.serve(sockets=[listener])
    finally:
        if drain is not None:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
        listener.close()
        try:
            try:
                if automatic is not None:
                    await automatic.close()
            finally:
                await supervisor.close()
        finally:
            await database.dispose()
        if connection is not None:
            # A replacement's registration must not be removed by this process.
            from .runtime_launcher import discovery

            current = discovery(data_root)
            if current == connection:
                (root / CONNECTION_NAME).unlink(missing_ok=True)
