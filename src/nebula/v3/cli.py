"""Command-line entry points for the Nebula 3 headless control plane."""

from __future__ import annotations

from contextvars import ContextVar
from functools import partial, wraps

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
import time
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn
from nebula.chat_cli import app as chat_app
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
from .context import default_output_tokens
from .credentials import CredentialStore
from .database import Database, DatabaseInUseError, vacuum_sqlite
from .database_pool import PoolSettings
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
    McpApprovalMode,
    ProviderProfile,
    RunBudget,
)
from .missions import MissionService
from .mcp_gateway import serve as serve_mcp_gateway
from .mcp_import import (
    McpImportDefaults,
    McpImportRequest,
    all_mcp_profiles,
    export_mcp_config,
    import_mcp_config,
    mcp_config_json_schema,
)
from .event_history import pause_between_batches, prune_orphaned_event_history
from .exporter import ExportError, export_engagement
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
from .storage import NebulaStore, NotFoundError
from .runtime_platform import default_runtime_platform
from .terminal_history import TerminalCommandRow
from .version import build_metadata

app = typer.Typer(
    name="nebula-core",
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
mcp_app = typer.Typer(
    name="mcp",
    help="Import and export MCP server profiles as mcpServers JSON.",
    no_args_is_help=True,
)
maintenance_app = typer.Typer(
    name="maintenance",
    help="Remove event history of deleted records and reclaim database space.",
    no_args_is_help=True,
)
app.add_typer(runtime_app, name="runtime")
app.add_typer(diagnostics_app, name="diagnostics")
app.add_typer(mcp_app, name="mcp")
app.add_typer(chat_app, name="chat")
app.add_typer(maintenance_app, name="maintenance")


_cli_stores: ContextVar[list[NebulaStore] | None] = ContextVar(
    "nebula_cli_stores", default=None
)


def _async_command(function=None, *, server_loop: bool = False):
    """Typer calls synchronously; one loop owns each command's async resources."""

    if function is None:
        return partial(_async_command, server_loop=server_loop)

    @wraps(function)
    def command(*args, **kwargs):
        async def run():
            stores: list[NebulaStore] = []
            token = _cli_stores.set(stores)
            try:
                return await function(*args, **kwargs)
            finally:
                from .async_utils import close_store_resources

                try:
                    await close_store_resources(stores)
                finally:
                    _cli_stores.reset(token)

        if server_loop:
            # Server.run selected Uvicorn's default loop before constructing
            # resources. Preserve that ordering while awaiting serve ourselves.
            uvicorn.Config("nebula.v3.api:create_app").setup_event_loop()
        return asyncio.run(run())

    return command


@app.command("mcp-gateway", hidden=True)
@_async_command
async def mcp_gateway(
    socket_path: Annotated[Path, typer.Option("--socket")],
) -> None:
    """Run the authenticated session gateway shim for a packaged Core."""

    token = os.environ.pop("NEBULA_MCP_GATEWAY_TOKEN", None)
    if not token:
        raise typer.BadParameter("gateway token environment is required")
    exit_code = await serve_mcp_gateway(socket_path, token)
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
    value: Path | None = None,
    *,
    diagnostics_level: str | None = None,
    runtime_pool: bool = False,
) -> tuple[Path, NebulaStore, ArtifactStore]:
    root = _data_dir(value)
    _diagnostic_manager(root, level_override=diagnostics_level)
    database_url = os.getenv("NEBULA_V3_DATABASE_URL")
    stores = _cli_stores.get()
    settings = None
    if (
        not runtime_pool
        and stores is not None
        and database_url
        and database_url.startswith(("postgres://", "postgresql://", "postgresql+"))
    ):
        settings = PoolSettings.from_environment().auxiliary()
    database = Database(database_url or root / "nebula.db", pool_settings=settings)
    artifact_root = Path(os.getenv("NEBULA_V3_ARTIFACT_DIR", root / "artifacts"))
    store = NebulaStore(database)
    if stores is not None:
        stores.append(store)
    return root, store, ArtifactStore(artifact_root)


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
@_async_command
async def runtime_status(
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Report runner, digest, readiness, and the generated binary inventory."""

    manager = _automation_services(data_dir)
    _print((await manager.runtime_info()).model_dump(mode="json"))


@runtime_app.command("prepare")
@_async_command
async def runtime_prepare(
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Pull the exact pinned image if needed, then verify its inventory."""

    manager = _automation_services(data_dir)
    result = await manager.prepare()
    _print(result.model_dump(mode="json"))
    if not result.ready:
        raise typer.Exit(code=1)


@mcp_app.command("import")
@_async_command
async def mcp_import(
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    apply: Annotated[
        bool, typer.Option(help="Save the servers; without it, only preview.")
    ] = False,
    replace: Annotated[
        bool,
        typer.Option(
            help="Overwrite existing servers with the same name instead of "
            "updating only what changed."
        ),
    ] = False,
    skip_existing: Annotated[
        bool, typer.Option(help="Leave servers that already exist untouched.")
    ] = False,
    enable: Annotated[
        bool,
        typer.Option(
            help="Enable new servers. Local programs also need --trust-local-programs."
        ),
    ] = False,
    approval: Annotated[
        McpApprovalMode,
        typer.Option(help="Tool approval for new servers whose file sets none."),
    ] = McpApprovalMode.RISK_BASED,
    trust_local_programs: Annotated[
        bool,
        typer.Option(
            help="Trust local programs this import enables, or keeps enabled "
            "after their launch settings change."
        ),
    ] = False,
    reject_literal_secrets: Annotated[
        bool,
        typer.Option(
            help="Fail entries with literal credentials instead of vaulting them."
        ),
    ] = False,
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Import MCP servers from a Claude, Cursor, or VS Code style JSON file.

    Servers that already exist are updated with only what changed. New servers
    start disabled unless --enable is given; enabled servers still need a probe
    before agents can use their tools.
    """

    if replace and skip_existing:
        raise typer.BadParameter("choose either --replace or --skip-existing")

    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        record_caught_exception(
            "harnesses",
            "harnesses.cli.mcp_import_unreadable",
            "An MCP import file could not be read.",
            exc,
            stage="cli",
        )
        raise typer.BadParameter(f"{source} is not a readable JSON file") from exc
    if not isinstance(document, dict):
        raise typer.BadParameter("the MCP configuration must be a JSON object")
    _, store, _ = _services(data_dir)
    report = await import_mcp_config(
        McpImportRequest(
            config=document,
            dry_run=not apply,
            on_conflict="replace" if replace else "skip" if skip_existing else "update",
            literal_secrets="reject" if reject_literal_secrets else "vault",
            source_name=source.name,
            defaults=McpImportDefaults(enabled=enable, default_approval=approval),
            trust_local_programs=trust_local_programs,
        ),
        store=store,
        credential_store=CredentialStore(),
    )
    _print(report.model_dump(mode="json"))
    if report.invalid:
        raise typer.Exit(code=1)


@mcp_app.command("export")
@_async_command
async def mcp_export(
    destination: Annotated[
        Path | None, typer.Argument(help="Write here instead of standard output.")
    ] = None,
    overwrite: Annotated[bool, typer.Option()] = False,
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Export MCP servers as mcpServers JSON; stored credentials become ${NAME}."""

    _, store, _ = _services(data_dir)
    report = export_mcp_config((await all_mcp_profiles(store)))
    for warning in report.warnings:
        typer.echo(warning, err=True)
    text = json.dumps(report.config, indent=2, sort_keys=True) + "\n"
    if destination is None:
        typer.echo(text, nl=False)
        return
    if destination.exists() and not overwrite:
        raise typer.BadParameter(f"{destination} exists; pass --overwrite")
    destination.write_text(text, encoding="utf-8")


@mcp_app.command("schema")
def mcp_schema() -> None:
    """Print the JSON Schema for MCP import files, for editor validation."""

    typer.echo(json.dumps(mcp_config_json_schema(), indent=2))


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
@_async_command(server_loop=True)
async def serve(
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
    tls_cert: Annotated[
        Path | None, typer.Option(help="TLS certificate chain for HTTPS.")
    ] = None,
    tls_key: Annotated[
        Path | None, typer.Option(help="TLS private key for HTTPS.")
    ] = None,
    allow_insecure_device_pairing: Annotated[
        bool,
        typer.Option(hidden=True, help="Permit paired-device cookies without HTTPS."),
    ] = False,
    allow_unauthenticated_remote: Annotated[
        bool,
        typer.Option(
            help="Explicitly expose the complete API without authentication (unsafe)."
        ),
    ] = False,
) -> None:
    """Run the versioned REST/WebSocket API."""

    if not _is_loopback(host) and not allow_remote:
        raise typer.BadParameter(
            "remote binding is disabled by default; pass --allow-remote after configuring perimeter controls"
        )
    if allow_unauthenticated_remote and (not allow_remote or _is_loopback(host)):
        raise typer.BadParameter(
            "--allow-unauthenticated-remote requires an explicit remote --allow-remote binding"
        )
    if (tls_cert is None) != (tls_key is None):
        raise typer.BadParameter("--tls-cert and --tls-key must be supplied together")
    if tls_cert is not None and (
        not tls_cert.is_file() or not tls_key or not tls_key.is_file()
    ):
        raise typer.BadParameter("TLS certificate and key must be readable files")
    from .runtime_launcher import retain_bundle
    from .runtime_service import serve_runtime

    root = _data_dir(data_dir)
    if handshake_stdout:
        raise typer.BadParameter(
            "desktop lifetime protocol has changed; use desktop-connect --bootstrap-stdin"
        )
    if allow_unauthenticated_remote:
        raise typer.BadParameter(
            "independent Core requires authentication; use normal device pairing"
        )
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as probe:
        try:
            probe.bind((host, port))
        except OSError as error:
            typer.echo(
                f"cannot bind {host}:{port}: {error.strerror or error}", err=True
            )
            raise typer.Exit(code=1) from error
    bundle, digest = await asyncio.to_thread(retain_bundle, root, static_dir=static_dir)
    await serve_runtime(
        data_root=root,
        bundle=bundle,
        manifest_sha256=digest,
        host=host,
        port=port,
        token=token,
        tls_cert=tls_cert,
        tls_key=tls_key,
        allow_insecure_device_pairing=allow_insecure_device_pairing,
        open_browser=open_browser,
        print_ready=True,
        diagnostics_level=diagnostics_level,
    )


@app.command()
def headless(
    host: Annotated[
        str, typer.Option(help="Bind address; use 0.0.0.0 for remote clients.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=0, max=65535)] = 8765,
    data_dir: Annotated[Path | None, typer.Option()] = None,
    token: Annotated[
        str | None,
        typer.Option(
            envvar="NEBULA_V3_API_TOKEN", help="Host administration bearer token."
        ),
    ] = None,
    allow_remote: Annotated[
        bool, typer.Option(help="Acknowledge accepting remote paired clients.")
    ] = False,
    tls_cert: Annotated[
        Path | None, typer.Option(help="Core HTTPS certificate chain.")
    ] = None,
    tls_key: Annotated[
        Path | None, typer.Option(help="Core HTTPS private key.")
    ] = None,
) -> None:
    """Run Core without a desktop, browser or frontend build; chat via paired CLI."""
    if not _is_loopback(host) and (
        not allow_remote or tls_cert is None or tls_key is None
    ):
        raise typer.BadParameter(
            "remote headless Core requires --allow-remote, --tls-cert and --tls-key"
        )
    serve(
        host=host,
        port=port,
        token=token,
        data_dir=data_dir,
        allow_remote=allow_remote,
        allow_browser_diagnostics=False,
        handshake_stdout=False,
        static_dir=None,
        open_browser=False,
        diagnostics_level=None,
        tls_cert=tls_cert,
        tls_key=tls_key,
        allow_insecure_device_pairing=False,
        allow_unauthenticated_remote=False,
    )


@app.command()
def ui(
    data_dir: Annotated[Path | None, typer.Option()] = None,
    no_browser: Annotated[bool, typer.Option()] = False,
    port: Annotated[
        int,
        typer.Option(min=0, max=65535, help="Listening port (0 chooses a free port)."),
    ] = 0,
    lan: Annotated[
        bool, typer.Option(help="Serve the workspace to paired LAN devices.")
    ] = False,
    host: Annotated[
        str | None, typer.Option(help="LAN bind address (defaults to 0.0.0.0).")
    ] = None,
    tls_cert: Annotated[
        Path | None, typer.Option(help="Trusted LAN TLS certificate chain.")
    ] = None,
    tls_key: Annotated[
        Path | None, typer.Option(help="Trusted LAN TLS private key.")
    ] = None,
    allow_insecure_lan: Annotated[
        bool,
        typer.Option(help="Explicitly allow unencrypted LAN access (unsafe)."),
    ] = False,
    no_auth: Annotated[
        bool,
        typer.Option(
            help="Disable authentication and imply LAN mode (unsafe; requires --allow-insecure-lan)."
        ),
    ] = False,
) -> None:
    """Launch the browser workspace locally or for securely paired LAN devices."""

    port = port or _free_port()
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
    if no_auth:
        lan = True
    selected_host = host or ("0.0.0.0" if lan else "127.0.0.1")
    if not lan and not _is_loopback(selected_host):
        raise typer.BadParameter("non-loopback UI binding requires --lan")
    if no_auth and not allow_insecure_lan:
        raise typer.BadParameter(
            "--no-auth implies --lan and requires --allow-insecure-lan"
        )
    if lan and not allow_insecure_lan and (tls_cert is None or tls_key is None):
        raise typer.BadParameter(
            "LAN UI requires --tls-cert and --tls-key; use --allow-insecure-lan only on a trusted test network"
        )
    serve(
        host=selected_host,
        port=port,
        token=token,
        data_dir=data_dir,
        allow_remote=lan,
        allow_browser_diagnostics=False,
        handshake_stdout=False,
        static_dir=frontend,
        open_browser=not no_browser and not lan,
        tls_cert=tls_cert,
        tls_key=tls_key,
        allow_insecure_device_pairing=allow_insecure_lan,
        allow_unauthenticated_remote=no_auth,
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@app.command("import-2x")
@_async_command
async def import_2x(
    source: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    data_dir: Annotated[Path | None, typer.Option()] = None,
    allow_external_knowledge: Annotated[
        bool,
        typer.Option(
            help="Explicitly authorize reading a configured Chroma directory outside the engagement."
        ),
    ] = False,
) -> None:
    """Import a Nebula 2.x engagement without modifying it."""

    _, store, artifacts = _services(data_dir)
    report = await import_2x_engagement(
        source,
        store,
        artifacts,
        allow_external_chroma=allow_external_knowledge,
    )
    _print(report.model_dump(mode="json"))
    if report.status != "completed":
        raise typer.Exit(code=1)


@app.command("export")
@_async_command
async def export_command(
    engagement_id: Annotated[str, typer.Argument()],
    destination: Annotated[Path, typer.Argument()],
    data_dir: Annotated[Path | None, typer.Option()] = None,
    overwrite: Annotated[bool, typer.Option()] = False,
) -> None:
    """Export an integrity-manifested engagement bundle."""

    _, store, artifacts = _services(data_dir)
    try:
        manifest = await export_engagement(
            engagement_id=engagement_id,
            destination=destination,
            store=store,
            artifact_store=artifacts,
            overwrite=overwrite,
        )
    except (ExportError, NotFoundError) as exc:
        # diagnostic-expected: known export failures become a bounded operator message and exit status.
        typer.echo(f"export failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _print(manifest.model_dump(mode="json"))


@app.command("run")
@_async_command
async def run_mission(
    engagement_id: Annotated[str, typer.Argument()],
    objective: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path | None, typer.Option()] = None,
    max_duration: Annotated[int | None, typer.Option(min=1)] = None,
    max_tool_calls: Annotated[int | None, typer.Option(min=0)] = None,
    max_artifact_queries: Annotated[int | None, typer.Option(min=0)] = None,
    max_concurrency: Annotated[int, typer.Option(min=1, max=2)] = 1,
    max_tokens: Annotated[int | None, typer.Option(min=1)] = None,
    max_cost: Annotated[float | None, typer.Option(min=0)] = None,
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
    """Run a durable mission with the fixed command runtime when requested."""

    requests_tools = max_tool_calls is None or max_tool_calls > 0
    if requests_tools and max_tool_calls is not None and provider_id is None:
        raise typer.BadParameter("executable missions require --provider")
    executable = provider_id is not None and requests_tools
    root, store, artifacts = _services(data_dir)
    (await store.get(Engagement, engagement_id))
    selected_provider = None
    selected_model = model
    if provider_id is None and model is not None:
        raise typer.BadParameter("--model requires --provider")
    if provider_id is not None:
        profile = await store.get(ProviderProfile, provider_id)
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
                max_output_tokens=(
                    await default_output_tokens(
                        store, provider_id, selected_model, token_budget=max_tokens
                    )
                ),
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
                    max_cost_usd=max_cost,
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
                    max_cost_usd=max_cost,
                    per_target_active_operations=1,
                ),
                tool_names=[RUN_COMMAND_NAME, PROCESS_IO_NAME],
                actor_id="cli-operator",
            )
            while queued.id in service.active_run_ids:
                await asyncio.sleep(0.05)
            return await store.get(AgentRun, queued.id)
        finally:
            await service.shutdown()
            await manager.shutdown()

    if executable:
        run = await execute_tools()
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

    state = await execute_analysis()
    _print(
        {
            "run_id": state.get("run_id"),
            "summary": state.get("final_summary"),
            "errors": state.get("errors", {}),
        }
    )


@app.command()
@_async_command
async def worker(
    once: Annotated[bool, typer.Option(help="Check capability and exit.")] = True,
) -> None:
    """Validate the dedicated rootless worker boundary."""

    if not once:
        raise typer.BadParameter(
            "durable worker mode is release-gated; use --once for a capability check"
        )

    async def check() -> tuple[bool, str]:
        return await ContainerSandboxRunner().available()

    available, detail = await check()
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
    database_url = Database._normalize_sync_url(
        os.getenv("NEBULA_V3_DATABASE_URL") or root / "nebula.db"
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


@app.command("migrate-to-postgresql")
def migrate_to_postgresql(
    data_dir: Annotated[
        Path | None, typer.Option(help="Existing Core data directory.")
    ] = None,
    source: Annotated[
        Path | None, typer.Option(help="SQLite source; defaults to DATA_DIR/nebula.db.")
    ] = None,
    target_url_file: Annotated[
        Path | None,
        typer.Option(
            help="Read the PostgreSQL URL from this private file instead of NEBULA_V3_DATABASE_URL."
        ),
    ] = None,
    inspect_only: Annotated[
        bool,
        typer.Option(
            "--inspect",
            help="Inspect the SQLite source without copying or configuring a target.",
        ),
    ] = False,
    confirm_core_stopped: Annotated[
        bool,
        typer.Option(
            "--confirm-core-stopped",
            help="Acknowledge that every Core using this SQLite source is stopped.",
        ),
    ] = False,
    batch_size: Annotated[
        int,
        typer.Option(min=1, max=10_000, help="Maximum rows held by each copy batch."),
    ] = 1_000,
    receipt: Annotated[
        Path | None,
        typer.Option(help="Save a private verification receipt to a new file."),
    ] = None,
) -> None:
    """Copy an existing SQLite Core database to an empty PostgreSQL database.

    Stop Core and back up its complete data directory first. The source and Core
    configuration are preserved. Only after verification, set
    NEBULA_V3_DATABASE_URL for the Core service and restart with the same data
    directory. See docs/postgresql-migration.md for setup and recovery.
    """

    from .database_migration import (
        MigrationError,
        inspect_sqlite_source,
        migrate_sqlite_to_postgresql,
    )

    root = (
        (
            data_dir
            or Path(
                os.getenv("NEBULA_V3_DATA_DIR", Path.home() / ".local/share/nebula/v3")
            )
        )
        .expanduser()
        .resolve()
    )
    source_path = (source or root / "nebula.db").expanduser().resolve()
    target_url = ""
    try:
        if inspect_only:
            _print(inspect_sqlite_source(source_path))
            return
        if not confirm_core_stopped:
            raise MigrationError(
                "Stop every Core using the source, back up the complete data directory, "
                "then repeat with --confirm-core-stopped."
            )
        if receipt is not None:
            receipt = receipt.expanduser().absolute()
            if receipt.exists() or receipt.is_symlink():
                raise MigrationError(
                    "Receipt already exists; choose a new --receipt file."
                )
            if not receipt.parent.is_dir():
                raise MigrationError(
                    "Receipt directory does not exist; create it first."
                )
        target_url = (
            target_url_file.expanduser().read_text(encoding="utf-8").strip()
            if target_url_file is not None
            else os.getenv("NEBULA_V3_DATABASE_URL", "").strip()
        )
        if not target_url:
            raise MigrationError(
                "Set NEBULA_V3_DATABASE_URL or pass --target-url-file for an empty "
                "PostgreSQL database."
            )

        def progress(event: dict[str, Any]) -> None:
            typer.echo(json.dumps(event, sort_keys=True), err=True)

        report = migrate_sqlite_to_postgresql(
            source_path,
            target_url,
            batch_size=batch_size,
            confirm_core_stopped=True,
            progress=progress,
        ).as_dict()
    except MigrationError as exc:
        # The migration boundary supplies operator-safe errors; never print a
        # raw driver exception or a connection URL containing credentials.
        message = str(exc)
        if target_url:
            message = message.replace(target_url, "[redacted connection URL]")
            try:
                password = make_url(target_url).password
            except Exception:  # diagnostic-expected: malformed URLs receive the same credential-free failure.
                password = None
            if password:
                message = message.replace(password, "[redacted]")
        typer.echo(f"PostgreSQL migration failed: {message[:1_000]}", err=True)
        raise typer.Exit(code=1) from None
    except OSError:
        typer.echo(
            "PostgreSQL migration failed: a source, target URL, or receipt file could "
            "not be read. Check its path and permissions.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    except Exception:
        typer.echo(
            "PostgreSQL migration failed. Verify the source schema, PostgreSQL "
            "connectivity, and an empty target before retrying. Core configuration "
            "has not been changed.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    _print(report)
    if receipt is not None:
        try:
            descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(report, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            typer.echo(
                "The database copy was verified, but its receipt could not be saved. "
                "Preserve the JSON result above before configuring Core.",
                err=True,
            )
            raise typer.Exit(code=1) from None
    typer.echo(
        "Copy verified. Configure NEBULA_V3_DATABASE_URL for Core, then restart "
        "with the same data directory and verify `nebula-core doctor --json`.",
        err=True,
    )


@maintenance_app.command("prune-events")
@_async_command
async def maintenance_prune_events(
    data_dir: Annotated[Path | None, typer.Option()] = None,
    batch_size: Annotated[
        int, typer.Option(min=1, max=10_000, help="Rows per committed batch.")
    ] = 5_000,
) -> None:
    """Delete run and operation events whose record was deleted.

    Core removes this history in the background when it starts, so this is
    only needed before `maintenance vacuum`. It is safe while Core runs.
    """

    _, store, _ = _services(data_dir)
    started = time.monotonic()
    report = await prune_orphaned_event_history(
        store.database, batch_size=batch_size, pause=pause_between_batches
    )
    _print({**report.as_dict(), "seconds": round(time.monotonic() - started, 1)})


@maintenance_app.command("vacuum")
def maintenance_vacuum(
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Return the space deleted rows left in the SQLite database to the disk.

    Stop Core first: this refuses while anything has the database open. It
    needs free disk for a temporary copy of the database and a write-ahead
    log of the same size.
    """

    root = _data_dir(data_dir)
    _diagnostic_manager(root)
    database_url = os.getenv("NEBULA_V3_DATABASE_URL")
    if database_url:
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" or url.database in {None, "", ":memory:"}:
            raise typer.BadParameter(
                "vacuum rewrites a SQLite database file; this database is not one"
            )
        path = Path(str(url.database))
    else:
        path = root / "nebula.db"
    try:
        result = vacuum_sqlite(path)
    except (DatabaseInUseError, FileNotFoundError) as exc:
        # diagnostic-expected: a refused vacuum becomes a bounded operator message and exit status.
        typer.echo(f"vacuum failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _print(result)


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
@_async_command
async def doctor(
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

    sandbox_available, sandbox_detail = await sandbox_health()
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
        page = await store.list_entities(Artifact, offset=offset, limit=1_000)
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
    async with store.database.session() as session:
        terminal_rows = (await session.scalars(select(TerminalCommandRow))).all()
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
                artifact = await store.get(Artifact, artifact_id)
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
                operation_page = await store.replay_operation_events(
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
    runtime_report: dict[str, Any] = (await runtime_manager.runtime_info()).model_dump(
        mode="json"
    )
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
        "database": (await store.database.health()),
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


@app.command("runtime-worker", hidden=True)
def runtime_worker_command(
    bootstrap_stdin: Annotated[bool, typer.Option()] = False,
) -> None:
    """Start one verified retained role using a bounded private bootstrap."""
    if not bootstrap_stdin:
        raise typer.BadParameter("runtime workers require --bootstrap-stdin")
    from .runtime_worker import main as worker_main

    worker_main()


@app.command("runtime-service", hidden=True)
@_async_command(server_loop=True)
async def runtime_service_command(
    bundle: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    manifest_sha256: Annotated[str, typer.Option()],
    data_dir: Annotated[Path | None, typer.Option()] = None,
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=0, max=65535)] = 0,
) -> None:
    from .runtime_service import serve_runtime

    if not _is_loopback(host):
        raise typer.BadParameter("internal desktop startup requires loopback")
    await serve_runtime(
        data_root=_data_dir(data_dir),
        bundle=bundle,
        manifest_sha256=manifest_sha256,
        host=host,
        port=port,
    )


@app.command("desktop-connect", hidden=True)
def desktop_connect_command(
    data_dir: Annotated[Path | None, typer.Option()] = None,
    static_dir: Annotated[Path | None, typer.Option()] = None,
    bootstrap_stdin: Annotated[bool, typer.Option()] = False,
) -> None:
    from .runtime_launcher import DESKTOP_PROTOCOL, desktop_connection

    if bootstrap_stdin:
        raw = sys.stdin.buffer.readline(16385)
        try:
            frame = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise typer.BadParameter("invalid desktop bootstrap frame") from error
        if (
            len(raw) > 16384
            or not raw.endswith(b"\n")
            or not isinstance(frame, dict)
            or frame.get("protocol") != DESKTOP_PROTOCOL
        ):
            raise typer.BadParameter("unsupported desktop bootstrap protocol")
    result = desktop_connection(_data_dir(data_dir), static_dir=static_dir)
    typer.echo(json.dumps(result, separators=(",", ":")))


@app.command("runtime-stop")
def runtime_stop_command(
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Explicitly stop local Core after its durable work is safe to stop."""
    from .runtime_launcher import shutdown_runtime

    _print(shutdown_runtime(_data_dir(data_dir)))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
