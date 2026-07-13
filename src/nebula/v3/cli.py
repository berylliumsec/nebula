"""Command-line entry points for the Nebula 3 headless control plane."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
import socket
import subprocess
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
from sqlalchemy.engine import make_url

from .api import create_app
from .artifacts import ArtifactStore
from .database import Database
from .domain import (
    AgentRun,
    Artifact,
    Engagement,
    ProviderProfile,
    RunnerProfile,
    RunBudget,
    ToolPackInstallation,
)
from .missions import MissionService
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
from .toolpack_sdk import (
    init_tool_pack,
    pack_tool_pack,
    validate_tool_pack_directory,
)
from .toolpacks import manifest_digest
from .tool_platform import ToolPlatform, default_tool_platform
from .version import build_metadata

app = typer.Typer(
    name="nebula3",
    help="Nebula 3 local-first security engagement control plane.",
    no_args_is_help=True,
)
tools_app = typer.Typer(
    name="tools",
    help="Install, verify, and author isolated Nebula tool packs.",
    no_args_is_help=True,
)
app.add_typer(tools_app, name="tools")


@tools_app.callback()
def tools_options(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the stable machine-readable JSON format.",
        ),
    ] = False,
) -> None:
    """Configure stable output shared by tool operator and author commands."""

    del json_output  # Tool commands are intentionally JSON-only today.


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


def _services(value: Path | None = None) -> tuple[Path, NebulaStore, ArtifactStore]:
    root = _data_dir(value)
    database_url = os.getenv("NEBULA_V3_DATABASE_URL")
    database = Database(database_url or root / "nebula.db")
    artifact_root = Path(os.getenv("NEBULA_V3_ARTIFACT_DIR", root / "artifacts"))
    return root, NebulaStore(database), ArtifactStore(artifact_root)


def _tool_services(
    value: Path | None = None,
) -> tuple[ToolPlatform, NebulaStore]:
    root, store, artifacts = _services(value)
    return (
        default_tool_platform(store=store, artifact_store=artifacts, data_root=root),
        store,
    )


def _print(value: Any) -> None:
    typer.echo(json.dumps(value, sort_keys=True, indent=2, default=str))


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
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
    except (socket.gaierror, ValueError):
        return False
    return bool(addresses) and all(address.is_loopback for address in addresses)


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
    handshake_stdout: Annotated[
        bool,
        typer.Option(hidden=True, help="Read a Tauri bootstrap token from stdin."),
    ] = False,
    static_dir: Annotated[
        Path | None,
        typer.Option(help="Serve a built browser workspace from this directory."),
    ] = None,
    open_browser: Annotated[bool, typer.Option(hidden=True)] = False,
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
    root, store, artifacts = _services(data_dir)
    tool_platform = default_tool_platform(
        store=store, artifact_store=artifacts, data_root=root
    )
    api = create_app(
        store,
        artifact_store=artifacts,
        auth_token=auth_token,
        static_dir=static_dir,
        enable_human_pty=_is_loopback(host),
        human_pty_root=_data_dir(data_dir) / "human-sessions",
        tool_platform=tool_platform,
    )
    try:
        bind_address = ipaddress.ip_address(host)
    except ValueError:
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
    config = uvicorn.Config(api, host=host, port=port, log_level="info")
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
    server.run(sockets=[listener])


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
        handshake_stdout=False,
        static_dir=frontend,
        open_browser=not no_browser,
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@tools_app.command("init")
def tools_init(
    directory: Annotated[Path, typer.Argument()],
    name: Annotated[str, typer.Option(help="Canonical pack name.")],
    publisher: Annotated[str, typer.Option()] = "local",
) -> None:
    """Create a conservative declarative tool-pack project."""

    created = init_tool_pack(directory, name=name, publisher=publisher)
    _print({"status": "created", "path": str(created)})


@tools_app.command("validate")
def tools_validate(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    require_digests: Annotated[
        bool,
        typer.Option(help="Reject unresolved development digest placeholders."),
    ] = False,
) -> None:
    """Validate schemas, policy mappings, images, and source-tree safety."""

    manifest = validate_tool_pack_directory(
        directory, allow_digest_placeholders=not require_digests
    )
    _print(
        {
            "status": "valid",
            "identity": manifest.identity,
            "manifest_digest": manifest_digest(manifest),
            "tools": [tool.name for tool in manifest.tools],
        }
    )


@tools_app.command("pack")
def tools_pack(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    destination: Annotated[Path, typer.Argument()],
) -> None:
    """Build a deterministic local `.nebula-toolpack` archive."""

    output = pack_tool_pack(directory, destination)
    _print({"status": "packed", "path": str(output)})


@tools_app.command("test")
def tools_test(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
) -> None:
    """Run offline manifest, policy, schema, and parser-fixture checks."""

    manifest = validate_tool_pack_directory(directory, allow_digest_placeholders=True)
    fixtures = sorted(
        path.relative_to(directory).as_posix()
        for path in (directory / "tests" / "parser-fixtures").glob("**/*")
        if path.is_file()
    )
    _print(
        {
            "status": "passed",
            "identity": manifest.identity,
            "tools": [tool.name for tool in manifest.tools],
            "parser_fixtures": fixtures,
            "smoke_tests": sum(len(tool.smoke_tests) for tool in manifest.tools),
            "note": "OCI smoke tests run only after an immutable installation",
        }
    )


@tools_app.command("build")
def tools_build(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    runtime: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    tag: Annotated[str, typer.Option(help="Temporary local OCI image tag.")],
    platform: Annotated[str, typer.Option()] = "linux/amd64",
    containerfile: Annotated[Path, typer.Option()] = Path("Containerfile"),
) -> None:
    """Build one author-selected image without executing it or editing manifests."""

    validate_tool_pack_directory(directory, allow_digest_placeholders=True)
    executable = runtime.expanduser().resolve()
    if executable.name not in {"docker", "podman"}:
        raise typer.BadParameter("runtime must be an absolute docker or podman binary")
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise typer.BadParameter("platform must be linux/amd64 or linux/arm64")
    if not tag or any(character.isspace() for character in tag):
        raise typer.BadParameter("tag must be a non-blank OCI image reference")
    definition = containerfile
    if not definition.is_absolute():
        definition = directory / definition
    definition = definition.expanduser().resolve(strict=True)
    if directory.resolve() not in definition.parents:
        raise typer.BadParameter("Containerfile must remain inside the pack directory")
    result = subprocess.run(
        [
            str(executable),
            "build",
            f"--platform={platform}",
            "--file",
            str(definition),
            "--tag",
            tag,
            str(directory.resolve()),
        ],
        check=False,
    )
    if result.returncode:
        raise typer.Exit(code=result.returncode)
    _print(
        {
            "status": "built",
            "tag": tag,
            "platform": platform,
            "next": "push the image, resolve its digest, then run tools pack",
        }
    )


@tools_app.command("catalog")
def tools_catalog(
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """List entries from the verified curated catalog."""

    platform, _ = _tool_services(data_dir)
    _print({"entries": asyncio.run(platform.catalog())})


@tools_app.command("list")
def tools_list(
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """List installed packs and current tool availability."""

    platform, store = _tool_services(data_dir)
    installations = store.list_entities(ToolPackInstallation, limit=1_000)
    _print(
        {
            "installations": [item.model_dump(mode="json") for item in installations],
            "tools": platform.list_tools(),
        }
    )


@tools_app.command("install")
def tools_install(
    catalog_id: Annotated[str, typer.Argument()],
    runner: Annotated[str, typer.Option("--runner")],
    version: Annotated[str | None, typer.Option()] = None,
    data_dir: Annotated[Path | None, typer.Option()] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm the signed pack permissions.")
    ] = False,
) -> None:
    """Explicitly install one signed catalog pack into a configured runner."""

    if not yes:
        raise typer.BadParameter("review the catalog permissions and pass --yes")
    platform, _ = _tool_services(data_dir)
    installed = asyncio.run(
        platform.install_catalog(catalog_id, runtime_profile_id=runner, version=version)
    )
    _print(installed.model_dump(mode="json"))


@tools_app.command("install-local")
def tools_install_local(
    bundle: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    runner: Annotated[str, typer.Option("--runner")],
    data_dir: Annotated[Path | None, typer.Option()] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm execution of this unsigned local pack."),
    ] = False,
) -> None:
    """Install an unsigned local pack only when device developer mode is enabled."""

    if not yes:
        raise typer.BadParameter("unsigned local packs require explicit --yes")
    payload = bundle.read_bytes()
    platform, _ = _tool_services(data_dir)
    installed = asyncio.run(
        platform.install_local(
            payload, runtime_profile_id=runner, confirm_permissions=True
        )
    )
    _print(installed.model_dump(mode="json"))


@tools_app.command("install-collection")
def tools_install_collection(
    collection_id: Annotated[str, typer.Argument()],
    runner: Annotated[str, typer.Option("--runner")],
    data_dir: Annotated[Path | None, typer.Option()] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm all collection permissions.")
    ] = False,
) -> None:
    """Install the latest signed members of one curated collection."""

    if not yes:
        raise typer.BadParameter("review all collection permissions and pass --yes")
    platform, _ = _tool_services(data_dir)
    installed = asyncio.run(
        platform.install_collection(collection_id, runtime_profile_id=runner)
    )
    _print({"installations": [item.model_dump(mode="json") for item in installed]})


@tools_app.command("verify")
def tools_verify(
    installation_id: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Reinspect exact image digests and rerun offline smoke tests."""

    platform, _ = _tool_services(data_dir)
    verified = asyncio.run(platform.verify(installation_id))
    _print(verified.model_dump(mode="json"))


@tools_app.command("update")
def tools_update(
    installation_id: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Install a newer signed version side by side after explicit confirmation."""

    if not yes:
        raise typer.BadParameter("tool-pack updates require explicit --yes")
    platform, _ = _tool_services(data_dir)
    updated = asyncio.run(platform.update(installation_id))
    _print(updated.model_dump(mode="json"))


@tools_app.command("remove")
def tools_remove(
    installation_id: Annotated[str, typer.Argument()],
    data_dir: Annotated[Path | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Disable a pack while preserving historical manifest locks."""

    if not yes:
        raise typer.BadParameter("tool-pack removal requires explicit --yes")
    platform, _ = _tool_services(data_dir)
    disabled = platform.disable(installation_id)
    _print(disabled.model_dump(mode="json"))


@tools_app.command("doctor")
def tools_doctor(
    runner: Annotated[str | None, typer.Option("--runner")] = None,
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Verify configured runners and report fail-closed tool availability."""

    platform, store = _tool_services(data_dir)
    profiles = store.list_entities(RunnerProfile, limit=1_000)
    if runner is not None:
        profiles = [profile for profile in profiles if profile.id == runner]
    checked = [
        asyncio.run(platform.verify_runner(profile.id)).model_dump(mode="json")
        for profile in profiles
    ]
    _print({"runners": checked, "tools": platform.list_tools()})


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
    max_concurrency: Annotated[int, typer.Option(min=1, max=2)] = 1,
    max_tokens: Annotated[int, typer.Option(min=1)] = 32_000,
    tool_names: Annotated[
        list[str] | None,
        typer.Option(
            "--tool",
            help="Assigned executable tool name; repeat for multiple tools.",
        ),
    ] = None,
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
    """Run a durable supervised mission with explicit tools and budgets."""

    selected_tools = list(dict.fromkeys(tool_names or ()))
    if max_tool_calls > 0 and not selected_tools:
        raise typer.BadParameter(
            "a positive --max-tool-calls budget requires at least one --tool"
        )
    if selected_tools and max_tool_calls == 0:
        raise typer.BadParameter("--tool requires a positive --max-tool-calls budget")
    if selected_tools and provider_id is None:
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
                    max_tokens=max_tokens,
                ),
                provider_id=provider_id,
                model=selected_model,
            )
            return dict(result)

    async def execute_tools() -> AgentRun:
        assert selected_provider is not None
        platform = default_tool_platform(
            store=store,
            artifact_store=artifacts,
            data_root=root,
        )
        service = MissionService(
            store,
            provider_factory=lambda _profile: selected_provider,
            tool_components_factory=platform.mission_components,
            max_active_missions=1,
        )
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
                    max_tokens=max_tokens,
                    per_target_active_operations=1,
                ),
                tool_names=selected_tools,
                actor_id="cli-operator",
            )
            while queued.id in service.active_run_ids:
                await asyncio.sleep(0.05)
            return store.get(AgentRun, queued.id)
        finally:
            await service.shutdown()

    if selected_tools:
        run = asyncio.run(execute_tools())
        _print(
            {
                "run_id": run.id,
                "status": run.status,
                "summary": run.metadata.get("final_summary"),
                "error": run.metadata.get("error"),
                "waiting_approval": run.metadata.get("waiting_approval", False),
                "tool_pack_digests": run.tool_pack_digests,
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
    database_url = os.getenv("NEBULA_V3_DATABASE_URL") or (
        f"sqlite:///{(root / 'nebula.db').as_posix()}"
    )
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("migrations"))
    )
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")
    safe_url = make_url(database_url).render_as_string(hide_password=True)
    _print({"status": "ok", "revision": "head", "database": safe_url})


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
            except Exception:
                valid = False
            if not valid:
                corrupt_artifacts.append(artifact.id)
        if len(page) < 1_000:
            break
        offset += len(page)
    orphan_digests = sorted(set(artifacts.iter_digests()) - referenced_digests)
    api = create_app(store, artifact_store=artifacts, auth_token="doctor")
    healthy = artifact_ok and not corrupt_artifacts
    report = {
        "status": "ok" if healthy else "error",
        **build_metadata(),
        "data_dir": str(root),
        "database": store.database.health(),
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
        "sandbox": {
            "available": sandbox_available,
            "detail": sandbox_detail,
            "mode": "execution" if sandbox_available else "analysis-only",
            "host_fallback": False,
        },
    }
    _print(report)
    if report["status"] != "ok":
        raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
