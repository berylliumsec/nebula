"""Command-line entry points for the Nebula 3 headless control plane."""

from __future__ import annotations

import asyncio
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
from sqlalchemy.engine import make_url

from .api import create_app
from .artifacts import ArtifactStore
from .database import Database
from .domain import Artifact, Engagement, ProviderProfile, RunBudget
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
from .version import build_metadata

app = typer.Typer(
    name="nebula3",
    help="Nebula 3 local-first security engagement control plane.",
    no_args_is_help=True,
)


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
    _, store, artifacts = _services(data_dir)
    api = create_app(
        store,
        artifact_store=artifacts,
        auth_token=auth_token,
        static_dir=static_dir,
        enable_human_pty=_is_loopback(host),
        human_pty_root=_data_dir(data_dir) / "human-sessions",
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
    """Run a durable analysis mission with an optional explicit model provider."""

    if max_tool_calls != 0:
        raise typer.BadParameter(
            "executable mission tools are release-gated; use --max-tool-calls 0"
        )
    root, store, _ = _services(data_dir)
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

    async def execute() -> dict[str, Any]:
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
                    max_duration_seconds=max_duration,
                    max_tool_calls=max_tool_calls,
                    max_tokens=max_tokens,
                ),
                provider_id=provider_id,
                model=selected_model,
            )
            return dict(result)

    state = asyncio.run(execute())
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
