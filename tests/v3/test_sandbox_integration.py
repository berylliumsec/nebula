from __future__ import annotations

import asyncio
import os

import pytest

from nebula.v3.sandbox import (
    ContainerSandboxRunner,
    SandboxLimits,
    SandboxNetwork,
    SandboxRequest,
    SandboxWorkspaceAccess,
)


RUNTIME = os.getenv("NEBULA_TEST_CONTAINER_RUNTIME")
IMAGE = os.getenv("NEBULA_TEST_TOOLBOX_IMAGE")
pytestmark = pytest.mark.skipif(
    not (RUNTIME and IMAGE),
    reason="a real rootless runtime and operator-runtime image are required",
)


def test_real_rootless_runtime_streams_raw_code_and_persists_only_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o777)
    workspace.chmod(0o777)
    runner = ContainerSandboxRunner(
        runtime=RUNTIME,
        allow_unpinned_images=True,
        workspace_roots=[tmp_path],
    )
    chunks: list[tuple[str, bytes]] = []

    async def capture(channel: str, payload: bytes) -> None:
        chunks.append((channel, payload))

    async def scenario() -> None:
        available, detail = await runner.available()
        assert available, detail
        request = SandboxRequest(
            image=IMAGE,
            command=[
                "/opt/nebula/bin/nebula-toolbox",
                "code",
                "--language",
                "python",
            ],
            workspace=workspace,
            workspace_access=SandboxWorkspaceAccess.WRITE,
            network=SandboxNetwork.NONE,
            limits=SandboxLimits(
                cpu_count=1,
                memory_mb=512,
                pids=128,
                timeout_seconds=30,
                output_bytes=200_000,
            ),
        )
        source = b"""\
from pathlib import Path
import sys
Path('/workspace/persisted.txt').write_text('kept', encoding='utf-8')
print('stdin=' + repr(sys.stdin.read()))
print('x' * 70000)
"""
        result = await runner.run_stream(
            request,
            input_bytes=source,
            on_chunk=capture,
            container_name="nebula-integration-python",
        )
        assert result.exit_code == 0
        assert result.stdout.startswith("stdin=''\n")
        assert result.stdout.count("x") == 70_000
        assert result.stderr == ""
        assert chunks
        assert all(channel in {"stdout", "stderr"} for channel, _ in chunks)
        assert all(0 < len(payload) <= 32 * 1024 for _, payload in chunks)

        followup = request.model_copy(
            update={
                "command": [
                    "/opt/nebula/bin/nebula-toolbox",
                    "code",
                    "--language",
                    "bash",
                ]
            }
        )
        second = await runner.run_stream(
            followup,
            input_bytes=b"printf 'persisted=%s\\n' \"$(cat persisted.txt)\"\n",
            container_name="nebula-integration-bash",
        )
        assert second.exit_code == 0
        assert second.stdout == "persisted=kept\n"

    asyncio.run(scenario())


def test_real_rootless_runtime_opens_and_removes_container_terminal(tmp_path):
    workspace = tmp_path / "terminal-workspace"
    workspace.mkdir(mode=0o777)
    workspace.chmod(0o777)
    runner = ContainerSandboxRunner(
        runtime=RUNTIME,
        allow_unpinned_images=True,
        workspace_roots=[tmp_path],
    )

    async def scenario() -> None:
        available, detail = await runner.available()
        assert available, detail
        request = SandboxRequest(
            image=IMAGE,
            command=["/bin/bash", "--noprofile", "--norc", "-i"],
            workspace=workspace,
            workspace_access=SandboxWorkspaceAccess.WRITE,
            network=SandboxNetwork.NONE,
            environment={"LANG": "C.UTF-8", "TERM": "xterm-256color"},
            limits=SandboxLimits(timeout_seconds=30),
        )
        process = await runner.open_terminal(
            request,
            container_name="nebula-terminal-integration",
            columns=100,
            rows=30,
        )
        try:
            await process.write(
                b"printf 'terminal-container-only\\n'\nprintf kept > terminal.txt\nexit\n"
            )
            output = bytearray()
            while True:
                chunk = await asyncio.wait_for(process.read(), timeout=10)
                if not chunk:
                    break
                output.extend(chunk)
            assert await asyncio.wait_for(process.wait(), timeout=10) == 0
            assert b"terminal-container-only" in output
            assert (workspace / "terminal.txt").read_text() == "kept"
        finally:
            await process.close()

    asyncio.run(scenario())
