"""Disposable Core for Butler terminal browser acceptance.

The terminal service and HTTP API are real. Its OCI process is synthetic so
selected browser lanes do not need to build Kali eight times.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import uvicorn
from fastapi import HTTPException, Request
from pydantic import BaseModel

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.container_terminal import (
    ContainerTerminalPreflightRequest,
    ContainerTerminalService,
    ContainerTerminalStartRequest,
)
from nebula.v3.domain import Engagement, RunnerIsolation, RunnerProfile, RunnerRuntime
from nebula.v3.runtime_platform import HumanTerminalRuntimeResolution, RuntimePlatform
from nebula.v3.sandbox import PreparedContainerImage
from nebula.v3.storage import NebulaStore


class SyntheticTerminalProcess:
    def __init__(self, container_name: str) -> None:
        self.container_name = container_name
        self.closed = 0
        self._output: asyncio.Queue[bytes] = asyncio.Queue()
        self._exit = asyncio.Event()

    async def read(self, maximum_bytes: int = 32_768) -> bytes:
        return await self._output.get()

    async def write(self, data: bytes) -> None:
        del data

    def resize(self, columns: int, rows: int) -> None:
        del columns, rows

    async def wait(self) -> int:
        await self._exit.wait()
        return -15

    async def close(self) -> None:
        self.closed += 1
        if not self._exit.is_set():
            self._exit.set()
            await self._output.put(b"")


class SyntheticTerminalRunner:
    def __init__(self) -> None:
        self.processes: list[SyntheticTerminalProcess] = []

    async def open_terminal(
        self,
        request: object,
        *,
        container_name: str,
        columns: int,
        rows: int,
        vpn_config: str | None = None,
    ) -> SyntheticTerminalProcess:
        del request, columns, rows, vpn_config
        process = SyntheticTerminalProcess(container_name)
        self.processes.append(process)
        return process


class SyntheticTerminalPlatform:
    execution_enabled = True

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runner = SyntheticTerminalRunner()
        self.profile = RunnerProfile(
            id="synthetic-terminal-runner",
            name="Synthetic terminal runner",
            runtime=RunnerRuntime.PODMAN,
            executable="/nonexistent/podman",
            platform="linux/amd64",
            isolation=RunnerIsolation.ROOTLESS,
            enabled=True,
            healthy=True,
        )
        self.image = PreparedContainerImage(
            source_reference="docker.io/kalilinux/kali-rolling:latest",
            base_resolved_reference="docker.io/kalilinux/kali-rolling@sha256:"
            + "b" * 64,
            base_digest="sha256:" + "b" * 64,
            resolved_reference="sha256:" + "c" * 64,
            digest="sha256:" + "c" * 64,
            platform="linux/amd64",
            configured_user="",
            installed_packages=("kali-linux-headless", "iputils-ping"),
            refreshed=True,
            detail="synthetic terminal process for paired Core acceptance",
            security_tools=("hashcat", "nmap", "printf"),
            security_tool_packages=("hashcat", "nmap"),
            security_tool_provenance=(
                ("hashcat", ("hashcat",)),
                ("nmap", ("nmap",)),
                ("printf", ("hashcat",)),
            ),
            security_tool_manifest_sha256="d" * 64,
        )

    def workspace_for(self, engagement_id: str) -> Path:
        workspace = self.root / "workspaces" / engagement_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def resolve_human_terminal_profile(self, engagement_id: str) -> RunnerProfile:
        del engagement_id
        return self.profile

    async def resolve_human_terminal_runtime(
        self, engagement_id: str
    ) -> HumanTerminalRuntimeResolution:
        return HumanTerminalRuntimeResolution(
            profile=self.profile,
            runner=self.runner,  # type: ignore[arg-type]
            workspace=self.workspace_for(engagement_id),
            image=self.image,
        )

    async def cleanup_operator_terminals(self) -> None:
        pass


class SeedRequest(BaseModel):
    engagement_id: str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--static-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    store = NebulaStore(args.root / "nebula.db")
    artifacts = ArtifactStore(args.root / "artifacts")
    tool_platform = RuntimePlatform(
        store=store,
        artifact_store=artifacts,
        data_root=args.root,
        execution_enabled=False,
    )
    platform = SyntheticTerminalPlatform(args.root)
    service = ContainerTerminalService(
        store=store, tool_platform=platform, operator_id=lambda: "terminal-fixture"
    )
    app = create_app(
        store,
        artifact_store=artifacts,
        auth_token="terminal-fixture",
        tool_platform=tool_platform,
        container_terminal_service=service,
        execution_data_root=args.root,
        static_dir=args.static_dir,
        bootstrap_workspace=True,
        allow_insecure_device_pairing=True,
    )

    def require_fixture_auth(request: Request) -> None:
        if request.headers.get("authorization") != "Bearer terminal-fixture":
            raise HTTPException(status_code=401)

    @app.post("/api/v1/fixture/terminal-seed")
    async def seed(body: SeedRequest, request: Request) -> dict[str, str]:
        require_fixture_auth(request)
        store.get(Engagement, body.engagement_id)
        preflight = ContainerTerminalPreflightRequest(engagement_id=body.engagement_id)
        preview = await service.preflight(preflight)
        if not preview.preview_token or not preview.preview_fingerprint:
            raise HTTPException(
                status_code=409, detail=preview.detail or "terminal preflight failed"
            )
        started = await service.start(
            ContainerTerminalStartRequest(
                **preflight.model_dump(),
                preview_token=preview.preview_token,
                preview_fingerprint=preview.preview_fingerprint,
                client_idempotency_key="butler-terminal-acceptance",
            )
        )
        await service.claim(started.session_id, started.websocket_ticket)
        await service.launch(started.session_id)
        return {
            "session_id": started.session_id,
            "created_at": started.model_dump(mode="json")["created_at"],
        }

    app.router.routes.insert(0, app.router.routes.pop())

    @app.get("/api/v1/fixture/terminal-processes")
    async def processes(request: Request) -> dict[str, list[int]]:
        require_fixture_auth(request)
        return {"close_counts": [item.closed for item in platform.runner.processes]}

    app.router.routes.insert(0, app.router.routes.pop())

    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="error")


if __name__ == "__main__":
    main()
