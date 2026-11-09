"""Production composition for the independently owned Core supervisor."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import webbrowser

import uvicorn

from .database import Database
from .database_pool import PoolSettings
from .deployments import DeploymentCoordinator
from .diagnostics import configure_diagnostics, record_caught_exception
from .memory_budget import MemoryBudgetService
from .memory_policy import MemoryBudgetError
from .process_identity import process_birth
from .release_store import ReleaseStore
from .runtime_host_services import HostSharedServices
from .runtime_launcher import (
    CONNECTION_NAME,
    DESKTOP_PROTOCOL,
    operator_token,
    runtime_root,
    write_private,
)
from .runtime_memory import RuntimeMemoryAdmission
from .runtime_processes import RuntimeProcessHost
from .supervisor import RuntimeSupervisor


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
    open_browser: bool = False,
    print_ready: bool = False,
    diagnostics_level: str | None = None,
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
    settings = PoolSettings.from_environment()
    database = Database(
        os.getenv("NEBULA_V3_DATABASE_URL") or data_root / "nebula.db",
        pool_settings=settings,
    )
    await database.initialize()
    coordinator = DeploymentCoordinator(database)
    budgets = MemoryBudgetService(
        database, runtime_defaults_bytes=3 * manifest.execution_reserve_bytes
    )
    admission = RuntimeMemoryAdmission(budgets)
    process_host = RuntimeProcessHost(
        coordinator, data_root, pool_settings=settings, operator_token=token
    )
    shared = HostSharedServices(process_host, memory_budgets=budgets)
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
        shutdown_requested=shutdown_requested,
    )
    listener = socket.socket(
        socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM
    )
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    connection = None
    try:
        # Bind before opening controllers or migrating legacy state into a live
        # runtime. An occupied compatibility port is never replaced silently.
        listener.bind((host, port))
        listener.listen(2048)
        port = listener.getsockname()[1]
        await supervisor.start()
        await supervisor.stage(bundle, manifest_sha256=manifest_sha256)
        await supervisor.select_recovery_release(manifest.version)
        status = await coordinator.status()
        if status.api_version is None:
            try:
                api = await supervisor.prepare_api(manifest.version)
                status = await coordinator.status()
                await supervisor.promote_api(
                    api.runtime_id, expected_revision=status.revision
                )
            except MemoryBudgetError as error:
                # The authenticated host control surface remains reachable so
                # the operator can resolve admission. Never weaken its policy.
                record_caught_exception(
                    "runtime",
                    "runtime.startup.admission_held",
                    "Core startup is waiting for the saved memory policy.",
                    error,
                    stage="startup",
                )
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
        await server.serve(sockets=[listener])
    finally:
        listener.close()
        try:
            await supervisor.close()
        finally:
            await database.dispose()
        if connection is not None:
            # A replacement's registration must not be removed by this process.
            from .runtime_launcher import discovery

            current = discovery(data_root)
            if current == connection:
                (root / CONNECTION_NAME).unlink(missing_ok=True)
