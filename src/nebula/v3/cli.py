"""Command-line entry points for the Nebula 3 headless control plane."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import asyncio
import hashlib
import ipaddress
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import make_url

from .api import create_app
from .artifacts import ArtifactStore
from .automation_runtime import AutomationRuntimeManager
from .automation_tools import (
    AutomationToolPlatform,
    PROCESS_IO_NAME,
    RUN_COMMAND_NAME,
)
from .database import Database
from .diagnostics import (
    configure_diagnostics,
    get_diagnostics,
    install_exception_hooks,
    record_diagnostic,
)
from .domain import (
    AgentRun,
    Artifact,
    Engagement,
    ProviderProfile,
    RunBudget,
)
from .missions import MissionService
from .mcp_gateway import serve as serve_mcp_gateway
from .exporter import export_engagement
from .importer import import_2x_engagement
from .orchestration import (
    ModelSpecialist,
    SpecialistRole,
    StaticSpecialist,
    StaticSupervisor,
    sqlite_mission_runtime,
)
from .providers import ProviderRegistry, provider_from_profile
from .sandbox import ContainerSandboxRunner
from .storage import NebulaStore
from .runtime_platform import default_runtime_platform
from .terminal_history import TerminalCommandRow
from .version import build_metadata

app = typer.Typer(
    name="nebula3",
    help="Nebula 3 local-first security engagement control plane.",
    no_args_is_help=True,
)
runtime_app = typer.Typer(
    name="runtime",
    help="Inspect and prepare the pinned automation command runtime.",
    no_args_is_help=True,
)
diagnostics_app = typer.Typer(
    name="diagnostics",
    help="Inspect and configure privacy-preserving local diagnostics.",
    no_args_is_help=True,
)
app.add_typer(runtime_app, name="runtime")
app.add_typer(diagnostics_app, name="diagnostics")


@app.command("mcp-gateway", hidden=True)
def mcp_gateway(
    socket_path: Annotated[Path, typer.Option("--socket")],
) -> None:
    """Run the authenticated session gateway shim for a packaged Core."""

    token = os.environ.pop("NEBULA_MCP_GATEWAY_TOKEN", None)
    if not token:
        raise typer.BadParameter("gateway token environment is required")
    exit_code = asyncio.run(serve_mcp_gateway(socket_path, token))
    if exit_code:
        raise typer.Exit(exit_code)


def _data_dir(value: Path | None = None) -> Path:
    configured = value or Path(
        os.getenv(
            "NEBULA_V3_DATA_DIR",
            Path.home() / ".local" / "share" / "nebula" / "v3",
        )
    )
    path = configured.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _diagnostic_manager(root: Path, *, level_override: str | None = None):
    current = get_diagnostics()
    expected_log_dir = Path(os.getenv("NEBULA_V3_LOG_DIR", root / "logs")).resolve()
    if (
        current is None
        or current.data_dir != root
        or current.log_dir != expected_log_dir
        or level_override is not None
    ):
        current = configure_diagnostics(root, level_override=level_override)
        install_exception_hooks()
    return current


def _services(
    value: Path | None = None, *, diagnostics_level: str | None = None
) -> tuple[Path, NebulaStore, ArtifactStore]:
    root = _data_dir(value)
    _diagnostic_manager(root, level_override=diagnostics_level)
    database_url = os.getenv("NEBULA_V3_DATABASE_URL")
    database = Database(database_url or root / "nebula.db")
    artifact_root = Path(os.getenv("NEBULA_V3_ARTIFACT_DIR", root / "artifacts"))
    return root, NebulaStore(database), ArtifactStore(artifact_root)


def _automation_services(value: Path | None = None) -> AutomationRuntimeManager:
    root, store, artifacts = _services(value)
    platform = default_runtime_platform(
        store=store, artifact_store=artifacts, data_root=root
    )
    return AutomationRuntimeManager(
        store=store,
        artifact_store=artifacts,
        data_root=root,
        workspace_resolver=platform.workspace_for,
        runtime_resolver=platform.resolve_human_terminal_runtime,
        cached_runtime_provider=platform.last_automation_runtime_metadata,
    )


def _print(value: Any) -> None:
    typer.echo(json.dumps(value, sort_keys=True, indent=2, default=str))


@runtime_app.command("status")
def runtime_status(
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Report runner, digest, readiness, and the generated binary inventory."""

    manager = _automation_services(data_dir)
    _print(asyncio.run(manager.runtime_info()).model_dump(mode="json"))


@runtime_app.command("prepare")
def runtime_prepare(
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Pull the exact pinned image if needed, then verify its inventory."""

    manager = _automation_services(data_dir)
    result = asyncio.run(manager.prepare())
    _print(result.model_dump(mode="json"))
    if not result.ready:
        raise typer.Exit(code=1)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError as caught_error:
        record_caught_exception(
            "diagnostics",
            "diagnostics.cli.caught_failure_001",
            "A handled diagnostics operation raised an exception.",
            caught_error,
            stage="cli",
        )
        pass
    try:
        addresses = {
            ipaddress.ip_address(sockaddr[0])
            for _family, _type, _protocol, _canonical, sockaddr in socket.getaddrinfo(
                host,
                None,
                type=socket.SOCK_STREAM,
            )
        }
    except (socket.gaierror, ValueError) as caught_error:
        record_caught_exception(
            "diagnostics",
            "diagnostics.cli.caught_failure_002",
            "A handled diagnostics operation raised an exception.",
            caught_error,
            stage="cli",
        )
        return False
    return bool(addresses) and all(address.is_loopback for address in addresses)


def _browser_diagnostic_ingress_allowed(
    host: str,
    *,
    handshake_stdout: bool,
    allow_remote: bool,
    explicitly_enabled: bool,
) -> bool:
    """Keep browser event ingestion local unless remote use is deliberate."""

    return not handshake_stdout and (
        _is_loopback(host) or (allow_remote and explicitly_enabled)
    )


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=0, max=65535)] = 8000,
    token: Annotated[
        str | None,
        typer.Option(envvar="NEBULA_V3_API_TOKEN", help="Bearer token."),
    ] = None,
    data_dir: Annotated[Path | None, typer.Option()] = None,
    allow_remote: Annotated[
        bool,
        typer.Option(help="Acknowledge binding beyond loopback."),
    ] = False,
    allow_browser_diagnostics: Annotated[
        bool,
        typer.Option(
            help=(
                "Accept authenticated browser diagnostic events on a remote binding. "
                "Loopback browser workspaces enable this automatically."
            )
        ),
    ] = False,
    handshake_stdout: Annotated[
        bool,
        typer.Option(hidden=True, help="Read a Tauri bootstrap token from stdin."),
    ] = False,
    static_dir: Annotated[
        Path | None,
        typer.Option(help="Serve a built browser workspace from this directory."),
    ] = None,
    open_browser: Annotated[bool, typer.Option(hidden=True)] = False,
    diagnostics_level: Annotated[
        str | None,
        typer.Option(
            "--diagnostics-level",
            help="Process-only diagnostics level override (debug, info, warning, error, critical).",
        ),
    ] = None,
) -> None:
    """Run the versioned REST/WebSocket API."""

    if not _is_loopback(host) and not allow_remote:
        raise typer.BadParameter(
            "remote binding is disabled by default; pass --allow-remote after configuring perimeter controls"
        )
    if handshake_stdout:
        if not _is_loopback(host):
            raise typer.BadParameter("desktop sidecars may bind only to loopback")
        raw = sys.stdin.buffer.readline(16_385)
        if len(raw) > 16_384 or not raw.endswith(b"\n"):
            raise typer.BadParameter("invalid desktop bootstrap frame")
        try:
            bootstrap = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            record_caught_exception(
                "diagnostics",
                "diagnostics.cli.caught_failure_003",
                "A handled diagnostics operation raised an exception.",
                exc,
                stage="cli",
            )
            raise typer.BadParameter("malformed desktop bootstrap frame") from exc
        if (
            not isinstance(bootstrap, dict)
            or bootstrap.get("protocol") != "nebula-sidecar-v1"
            or not isinstance(bootstrap.get("ipc_token"), str)
            or not 32 <= len(bootstrap["ipc_token"]) <= 256
        ):
            raise typer.BadParameter("unsupported desktop bootstrap protocol")
        auth_token = bootstrap["ipc_token"]
    else:
        auth_token = token or secrets.token_urlsafe(32)
    root, store, artifacts = _services(data_dir, diagnostics_level=diagnostics_level)
    record_diagnostic(
        "info",
        "api",
        "api.process.starting",
        "Nebula Core API is starting.",
        outcome="started",
        metadata={"mode": "desktop" if handshake_stdout else "headless"},
    )
    tool_platform = default_runtime_platform(
        store=store, artifact_store=artifacts, data_root=root
    )
    api = create_app(
        store,
        artifact_store=artifacts,
        auth_token=auth_token,
        static_dir=static_dir,
        tool_platform=tool_platform,
        execution_data_root=root,
        bootstrap_workspace=True,
        # The authenticated browser workspace served by a loopback Core owns
        # its interface diagnostics just like the native shell does. Remote
        # bindings remain closed to browser diagnostic ingress by default.
        allow_browser_diagnostic_events=_browser_diagnostic_ingress_allowed(
            host,
            handshake_stdout=handshake_stdout,
            allow_remote=allow_remote,
            explicitly_enabled=allow_browser_diagnostics,
        ),
    )
    try:
        bind_address = ipaddress.ip_address(host)
    except ValueError as caught_error:
        record_caught_exception(
            "diagnostics",
            "diagnostics.cli.caught_failure_004",
            "A handled diagnostics operation raised an exception.",
            caught_error,
            stage="cli",
        )
        bind_address = None
    family = (
        socket.AF_INET6
        if bind_address is not None and bind_address.version == 6
        else socket.AF_INET
    )
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(2048)
    port = int(listener.getsockname()[1])
    if handshake_stdout:
        # The desktop supervisor deliberately reads exactly one bounded line.
        sys.stdout.write(
            json.dumps(
                {
                    "protocol": "nebula-sidecar-v1",
                    "host": "127.0.0.1",
                    "port": port,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        sys.stdout.flush()
    else:
        display_host = f"[{host}]" if family == socket.AF_INET6 else host
        _print(
            {
                "kind": "nebula-api-ready",
                "url": f"http://{display_host}:{port}",
                "token": auth_token,
                "local_only": _is_loopback(host),
            }
        )
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/#token={auth_token}")
    # Request summaries are emitted by the sanitizing API middleware. Raw
    # server access logs can include untrusted path/query data and stay off.
    config = uvicorn.Config(
        api, host=host, port=port, log_level="error", access_log=False
    )
    server = uvicorn.Server(config)
    if handshake_stdout:

        def stop_when_desktop_disconnects() -> None:
            try:
                while sys.stdin.buffer.read(4096):
                    pass
            finally:
                server.should_exit = True

        threading.Thread(
            target=stop_when_desktop_disconnects,
            name="nebula-desktop-lifetime",
            daemon=True,
        ).start()
    try:
        server.run(sockets=[listener])
    except BaseException as exc:
        record_caught_exception(
            "diagnostics",
            "diagnostics.cli.caught_failure_005",
            "A handled diagnostics operation raised an exception.",
            exc,
            stage="cli",
        )
        record_diagnostic(
            "critical",
            "api",
            "api.process.failed",
            "Nebula Core API stopped unexpectedly.",
            outcome="failure",
            stage="server",
            retryable=True,
            exception=exc,
        )
        raise
    finally:
        record_diagnostic(
            "info",
            "api",
            "api.process.stopped",
            "Nebula Core API stopped.",
            outcome="success",
        )


@app.command()
def ui(
    data_dir: Annotated[Path | None, typer.Option()] = None,
    no_browser: Annotated[bool, typer.Option()] = False,
) -> None:
    """Launch the browser workspace against a loopback-only ephemeral API."""

    port = _free_port()
    token = secrets.token_urlsafe(32)
    configured_ui = os.getenv("NEBULA_V3_UI_DIR")
    frozen_root = (
        Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else None
    )
    frontend = (
        Path(configured_ui).expanduser().resolve()
        if configured_ui
        else (
            frozen_root / "ui" / "dist"
            if frozen_root is not None
            else Path(__file__).resolve().parents[3] / "ui" / "dist"
        )
    )
    if not (frontend / "index.html").is_file():
        raise typer.BadParameter(
            "built browser workspace not found; run `npm --prefix ui run build` "
            "or set NEBULA_V3_UI_DIR"
        )
    serve(
        host="127.0.0.1",
        port=port,
        token=token,
        data_dir=data_dir,
        allow_remote=False,
        allow_browser_diagnostics=False,
        handshake_stdout=False,
        static_dir=frontend,
        open_browser=not no_browser,
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])



@app.command("import-2x")
def import_2x(
    source: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    data_dir: Annotated[Path | None, typer.Option()] = None,
    allow_external_knowledge: Annotated[
        bool,
        typer.Option(
            help="Explicitly authorize reading a configured Chroma directory outside the engagement."
        ),
    ] = False,
) -> None:
    """Import a Nebula 2.x engagement side-by-side without modifying it."""

    _, store, artifacts = _services(data_dir)
    report = import_2x_engagement(
        source,
        store,
        artifacts,
        allow_external_chroma=allow_external_knowledge,
    )
    _print(report.model_dump(mode="json"))
    if report.status != "completed":
        raise typer.Exit(code=1)


@app.command("export")
def export_command(
    engagement_id: Annotated[str, typer.Argument()],
    destination: Annotated[Path, typer.Argument()],
    data_dir: Annotated[Path | None, typer.Option()] = None,
    overwrite: Annotated[bool, typer.Option()] = False,
) -> None:
    """Export an integrity-manifested engagement bundle."""

    _, store, artifacts = _services(data_dir)
    manifest = export_engagement(
        engagement_id=engagement_id,
        destination=destination,
        store=store,
        artifact_store=artifacts,
        overwrite=overwrite,
    )
    _print(manifest.model_dump(mode="json"))


@app.command("run")
def run_mission(
    engagement_id: Annotated[str, typer.Argument()],
    objective: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path | None, typer.Option()] = None,
    max_duration: Annotated[int, typer.Option(min=1)] = 3600,
    max_tool_calls: Annotated[int, typer.Option(min=0)] = 0,
    max_artifact_queries: Annotated[int, typer.Option(min=0)] = 200,
    max_concurrency: Annotated[int, typer.Option(min=1, max=2)] = 1,
    max_tokens: Annotated[int, typer.Option(min=1)] = 32_000,
    provider_id: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Persisted provider profile ID; omitted means offline analysis.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Runtime model ID; defaults to the provider profile."),
    ] = None,
) -> None:
    """Run a durable mission with the fixed command runtime when budgeted."""

    executable = max_tool_calls > 0
    if executable and provider_id is None:
        raise typer.BadParameter("executable missions require --provider")
    root, store, artifacts = _services(data_dir)
    store.get(Engagement, engagement_id)
    selected_provider = None
    selected_model = model
    if provider_id is None and model is not None:
        raise typer.BadParameter("--model requires --provider")
    if provider_id is not None:
        profile = store.get(ProviderProfile, provider_id)
        registry = ProviderRegistry()
        registry.register(provider_from_profile(profile))
        selected_provider = registry.get(provider_id)
        selected_model = selected_model or selected_provider.config.default_model
        if selected_model is None:
            raise typer.BadParameter(
                "the selected provider needs --model or a configured default model"
            )
        if profile.model_allowlist and selected_model not in profile.model_allowlist:
            raise typer.BadParameter(
                f"model {selected_model!r} is outside the provider profile allowlist"
            )

    async def execute_analysis() -> dict[str, Any]:
        specialist = (
            ModelSpecialist(
                selected_provider,
                model=selected_model,
                max_output_tokens=min(2048, max_tokens),
            )
            if selected_provider is not None
            else StaticSpecialist()
        )
        async with sqlite_mission_runtime(
            checkpoint_path=root / "checkpoints.db",
            store=store,
            supervisor=StaticSupervisor(),
            specialists={SpecialistRole.SCOPE_PLANNING: specialist},
        ) as runtime:
            result = await runtime.start(
                engagement_id=engagement_id,
                objective=objective,
                budget=RunBudget(
                    max_concurrency=max_concurrency,
                    max_duration_seconds=max_duration,
                    max_tool_calls=max_tool_calls,
                    max_artifact_queries=max_artifact_queries,
                    max_tokens=max_tokens,
                ),
                provider_id=provider_id,
                model=selected_model,
            )
            return dict(result)

    async def execute_tools() -> AgentRun:
        assert selected_provider is not None
        workspace_platform = default_runtime_platform(
            store=store,
            artifact_store=artifacts,
            data_root=root,
        )
        manager = AutomationRuntimeManager(
            store=store,
            artifact_store=artifacts,
            data_root=root,
            workspace_resolver=workspace_platform.workspace_for,
            runtime_resolver=workspace_platform.resolve_human_terminal_runtime,
            cached_runtime_provider=workspace_platform.last_automation_runtime_metadata,
        )
        platform = AutomationToolPlatform(
            manager=manager,
            store=store,
            artifact_store=artifacts,
            workspace_resolver=workspace_platform.workspace_for,
        )
        service = MissionService(
            store,
            provider_factory=lambda _profile: selected_provider,
            tool_components_factory=platform.mission_components,
            max_active_missions=1,
        )
        await manager.startup()
        await service.startup()
        try:
            queued = await service.start_mission(
                engagement_id=engagement_id,
                objective=objective,
                provider_id=provider_id or "",
                model=selected_model or "",
                budget=RunBudget(
                    max_concurrency=max_concurrency,
                    max_delegation_depth=1,
                    max_duration_seconds=max_duration,
                    max_tool_calls=max_tool_calls,
                    max_artifact_queries=max_artifact_queries,
                    max_tokens=max_tokens,
                    per_target_active_operations=1,
                ),
                tool_names=[RUN_COMMAND_NAME, PROCESS_IO_NAME],
                actor_id="cli-operator",
            )
            while queued.id in service.active_run_ids:
                await asyncio.sleep(0.05)
            return store.get(AgentRun, queued.id)
        finally:
            await service.shutdown()
            await manager.shutdown()

    if executable:
        run = asyncio.run(execute_tools())
        _print(
            {
                "run_id": run.id,
                "status": run.status,
                "summary": run.metadata.get("final_summary"),
                "error": run.metadata.get("error"),
                "waiting_approval": run.metadata.get("waiting_approval", False),
                "runtime_digest": run.runtime_snapshot.get("automation_runtime_digest"),
            }
        )
        return

    state = asyncio.run(execute_analysis())
    _print(
        {
            "run_id": state.get("run_id"),
            "summary": state.get("final_summary"),
            "errors": state.get("errors", {}),
        }
    )


@app.command()
def worker(
    once: Annotated[bool, typer.Option(help="Check capability and exit.")] = True,
) -> None:
    """Validate the dedicated rootless worker boundary."""

    if not once:
        raise typer.BadParameter(
            "durable worker mode is release-gated; use --once for a capability check"
        )

    async def check() -> tuple[bool, str]:
        return await ContainerSandboxRunner().available()

    available, detail = asyncio.run(check())
    _print(
        {
            "worker": "ready" if available else "analysis-only",
            "sandbox_available": available,
            "detail": detail,
            "host_fallback": False,
        }
    )


@app.command()
def migrate(
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Upgrade the configured SQLite/PostgreSQL schema with Alembic."""

    root = _data_dir(data_dir)
    _diagnostic_manager(root)
    database_url = os.getenv("NEBULA_V3_DATABASE_URL") or (
        f"sqlite:///{(root / 'nebula.db').as_posix()}"
    )
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("migrations"))
    )
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    try:
        command.upgrade(config, "head")
    except Exception as exc:
        record_caught_exception(
            "diagnostics",
            "diagnostics.cli.caught_failure_006",
            "A handled diagnostics operation raised an exception.",
            exc,
            stage="cli",
        )
        record_diagnostic(
            "critical",
            "storage",
            "storage.migration.failed",
            "The Nebula database migration failed.",
            outcome="failure",
            stage="migration",
            retryable=False,
            exception=exc,
        )
        raise
    record_diagnostic(
        "info",
        "storage",
        "storage.migration.completed",
        "The Nebula database migration completed.",
        outcome="success",
        stage="migration",
    )
    safe_url = make_url(database_url).render_as_string(hide_password=True)
    _print({"status": "ok", "revision": "head", "database": safe_url})


@diagnostics_app.command("status")
def diagnostics_status(
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Show logger health, active levels, rotation, queue, and disk usage."""

    root = _data_dir(data_dir)
    _print(_diagnostic_manager(root).status())


@diagnostics_app.command("set-level")
def diagnostics_set_level(
    level: Annotated[
        str, typer.Argument(help="debug, info, warning, error, or critical")
    ],
    feature: Annotated[
        str | None,
        typer.Option("--feature", help="Optional feature-domain override."),
    ] = None,
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Persist a global or per-feature local diagnostics level."""

    root = _data_dir(data_dir)
    manager = _diagnostic_manager(root)
    payload = manager.settings.as_dict()
    if feature is None:
        payload["global_level"] = level
    else:
        feature_levels = dict(payload["feature_levels"])
        feature_levels[feature] = level
        payload["feature_levels"] = feature_levels
    try:
        settings = manager.update_settings(payload)
    except ValueError as exc:
        record_caught_exception(
            "diagnostics",
            "diagnostics.cli.caught_failure_007",
            "A handled diagnostics operation raised an exception.",
            exc,
            stage="cli",
        )
        raise typer.BadParameter(str(exc)) from exc
    _print({"status": "ok", "settings": settings.as_dict()})


@diagnostics_app.command("reset-levels")
def diagnostics_reset_levels(
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Restore errors-only diagnostics and remove feature overrides."""

    root = _data_dir(data_dir)
    settings = _diagnostic_manager(root).reset_settings()
    _print({"status": "ok", "settings": settings.as_dict()})


@diagnostics_app.command("export")
def diagnostics_export(
    output: Annotated[Path, typer.Argument(help="Destination .zip file.")],
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Generate a local diagnostics support bundle."""

    root = _data_dir(data_dir)
    destination = _diagnostic_manager(root).export(output)
    _print({"status": "ok", "path": str(destination)})


@app.command()
def doctor(
    data_dir: Annotated[Path | None, typer.Option()] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the machine-readable report (the default output format).",
        ),
    ] = False,
) -> None:
    """Check storage, API schema, artifact integrity, and runner isolation."""

    del json_output  # Retained explicitly for stable release automation.

    root, store, artifacts = _services(data_dir)

    async def sandbox_health() -> tuple[bool, str]:
        return await ContainerSandboxRunner().available()

    sandbox_available, sandbox_detail = asyncio.run(sandbox_health())
    artifact_probe = None
    artifact_ok = False
    try:
        with tempfile.NamedTemporaryFile(dir=artifacts.root, delete=False) as stream:
            stream.write(b"nebula-doctor")
            artifact_probe = Path(stream.name)
        artifact_ok = artifact_probe.read_bytes() == b"nebula-doctor"
    finally:
        if artifact_probe:
            artifact_probe.unlink(missing_ok=True)
    checked_artifacts = 0
    corrupt_artifacts: list[str] = []
    referenced_digests: set[str] = set()
    offset = 0
    while True:
        page = store.list_entities(Artifact, offset=offset, limit=1_000)
        for artifact in page:
            checked_artifacts += 1
            referenced_digests.add(artifact.sha256)
            try:
                valid = artifacts.verify(artifact)
            except Exception as caught_error:
                record_caught_exception(
                    "diagnostics",
                    "diagnostics.cli.caught_failure_008",
                    "A handled diagnostics operation raised an exception.",
                    caught_error,
                    stage="cli",
                )
                valid = False
            if not valid:
                corrupt_artifacts.append(artifact.id)
        if len(page) < 1_000:
            break
        offset += len(page)
    orphan_digests = sorted(set(artifacts.iter_digests()) - referenced_digests)
    terminal_audit_errors: list[str] = []
    terminal_audit_checked = 0
    command_events: dict[str, dict[str, Any]] = {}
    terminal_spool_root = artifacts.root.parent / "terminal-audit-spool"
    if terminal_spool_root.exists():
        terminal_audit_errors.extend(
            f"spool:{path.name}"
            for path in sorted(terminal_spool_root.iterdir())
            if path.is_file()
        )
    with store.database.session() as session:
        terminal_rows = session.scalars(select(TerminalCommandRow)).all()
    for row in terminal_rows:
        terminal_audit_checked += 1
        expected_command_hash = hashlib.sha256(row.command.encode("utf-8")).hexdigest()
        if row.command_sha256 != expected_command_hash:
            terminal_audit_errors.append(f"{row.id}:command_hash")
        if row.observed_output_bytes < row.captured_output_bytes:
            terminal_audit_errors.append(f"{row.id}:output_byte_count")
        if row.output_truncated != (
            row.observed_output_bytes > row.captured_output_bytes
        ):
            terminal_audit_errors.append(f"{row.id}:truncation_state")
        output_artifacts: dict[str, Artifact] = {}
        for field_name, artifact_id in (
            ("raw_output", row.raw_output_artifact_id),
            ("redacted_output", row.redacted_output_artifact_id),
        ):
            if artifact_id is None:
                continue
            try:
                artifact = store.get(Artifact, artifact_id)
                output_artifacts[field_name] = artifact
                if not artifacts.verify(artifact):
                    terminal_audit_errors.append(f"{row.id}:{field_name}_integrity")
                if artifact.engagement_id != row.engagement_id:
                    terminal_audit_errors.append(f"{row.id}:{field_name}_project")
                if artifact.metadata.get("terminal_command_id") != row.id:
                    terminal_audit_errors.append(f"{row.id}:{field_name}_metadata")
            except Exception as caught_error:
                record_caught_exception(
                    "diagnostics",
                    "diagnostics.cli.caught_failure_009",
                    "A handled diagnostics operation raised an exception.",
                    caught_error,
                    stage="cli",
                )
                terminal_audit_errors.append(f"{row.id}:{field_name}_missing")
        if row.capture_decision == "legacy_metadata_only":
            continue
        output_expected = row.capture_decision in {
            "selected_tool",
            "capture_failed",
            "legacy_all_commands",
        }
        if output_expected:
            raw_artifact = output_artifacts.get("raw_output")
            if raw_artifact is None:
                terminal_audit_errors.append(f"{row.id}:raw_output_reference")
            else:
                if raw_artifact.size != row.captured_output_bytes:
                    terminal_audit_errors.append(f"{row.id}:captured_output_bytes")
                if (
                    not row.output_truncated
                    and raw_artifact.sha256 != row.output_sha256
                ):
                    terminal_audit_errors.append(f"{row.id}:output_hash")
            if output_artifacts.get("redacted_output") is None:
                terminal_audit_errors.append(f"{row.id}:redacted_output_reference")
            if row.output_sha256 is None:
                terminal_audit_errors.append(f"{row.id}:output_hash_missing")
        elif (
            output_artifacts
            or row.output_sha256 is not None
            or row.observed_output_bytes
            or row.captured_output_bytes
            or row.output_truncated
        ):
            terminal_audit_errors.append(f"{row.id}:unexpected_output_capture")
        if row.capture_decision == "classification_failed":
            terminal_audit_errors.append(f"{row.id}:classification_failed")
        try:
            matched_tools = json.loads(row.matched_tools)
        except (TypeError, json.JSONDecodeError) as caught_error:
            record_caught_exception(
                "diagnostics",
                "diagnostics.cli.caught_failure_010",
                "A handled diagnostics operation raised an exception.",
                caught_error,
                stage="cli",
            )
            matched_tools = None
            terminal_audit_errors.append(f"{row.id}:matched_tools_invalid")
        if row.capture_decision == "selected_tool" and not matched_tools:
            terminal_audit_errors.append(f"{row.id}:selected_tool_missing_match")
        if row.session_id not in command_events:
            events: list[Any] = []
            after_sequence = 0
            while True:
                operation_page = store.replay_operation_events(
                    row.session_id, after_sequence=after_sequence, limit=10_000
                )
                events.extend(operation_page)
                if len(operation_page) < 10_000:
                    break
                after_sequence = operation_page[-1].sequence
            command_events[row.session_id] = {
                str(event.payload.get("record_id")): event
                for event in events
                if event.event_type == "container_terminal.command"
            }
        event = command_events[row.session_id].get(row.id)
        if event is None:
            terminal_audit_errors.append(f"{row.id}:command_event_missing")
            continue
        expected_event_values = {
            "status": row.status,
            "exit_code": row.exit_code,
            "command_sha256": row.command_sha256,
            "output_sha256": row.output_sha256,
            "raw_output_artifact_id": row.raw_output_artifact_id,
            "redacted_output_artifact_id": row.redacted_output_artifact_id,
            "observed_output_bytes": row.observed_output_bytes,
            "captured_output_bytes": row.captured_output_bytes,
            "output_truncated": row.output_truncated,
        }
        if row.capture_decision != "legacy_all_commands":
            expected_event_values.update(
                {
                    "capture_decision": row.capture_decision,
                    "matched_tools": matched_tools,
                    "recording_policy_revision": row.recording_policy_revision,
                    "runtime_image_digest": row.runtime_image_digest,
                }
            )
        if event.actor_id != row.operator_id or any(
            event.payload.get(key) != value
            for key, value in expected_event_values.items()
        ):
            terminal_audit_errors.append(f"{row.id}:command_event_mismatch")
    api = create_app(store, artifact_store=artifacts, auth_token="doctor")
    diagnostics_health = _diagnostic_manager(root).status()
    workspace_platform = default_runtime_platform(
        store=store, artifact_store=artifacts, data_root=root
    )
    runtime_manager = AutomationRuntimeManager(
        store=store,
        artifact_store=artifacts,
        data_root=root,
        workspace_resolver=workspace_platform.workspace_for,
        runtime_resolver=workspace_platform.resolve_human_terminal_runtime,
        cached_runtime_provider=workspace_platform.last_automation_runtime_metadata,
    )
    runtime_report: dict[str, Any] = asyncio.run(
        runtime_manager.runtime_info()
    ).model_dump(mode="json")
    healthy = (
        artifact_ok
        and not corrupt_artifacts
        and not terminal_audit_errors
        and diagnostics_health["writable"]
        and not diagnostics_health["degraded"]
    )
    report = {
        "status": "ok" if healthy else "error",
        **build_metadata(),
        "data_dir": str(root),
        "database": store.database.health(),
        "diagnostics": diagnostics_health,
        "artifacts": {
            "writable": artifact_ok,
            "path": str(artifacts.root),
            "checked": checked_artifacts,
            "corrupt": len(corrupt_artifacts),
            "corrupt_ids": corrupt_artifacts[:100],
            "orphan_blobs": len(orphan_digests),
            "orphan_digests": orphan_digests[:100],
        },
        "api": {"openapi_version": api.openapi().get("openapi"), "version": "v1"},
        "terminal_audit": {
            "checked": terminal_audit_checked,
            "errors": len(terminal_audit_errors),
            "error_records": terminal_audit_errors[:100],
        },
        "sandbox": {
            "available": sandbox_available,
            "detail": sandbox_detail,
            "mode": "execution" if sandbox_available else "analysis-only",
            "host_fallback": False,
        },
        "automation_runtime": runtime_report,
    }
    _print(report)
    if report["status"] != "ok":
        raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
