from __future__ import annotations

import asyncio
import os

import pytest

from nebula.v3.sandbox import (
    ContainerImagePreparer,
    ContainerSandboxRunner,
    SandboxContainerUser,
    SandboxExecutionKind,
    SandboxLimits,
    SandboxNetwork,
    SandboxRequest,
    SandboxRootFilesystem,
    SandboxWorkspaceAccess,
)


RUNTIME = os.getenv("NEBULA_TEST_CONTAINER_RUNTIME")
ENABLED = os.getenv("NEBULA_TEST_KALI_TERMINAL") == "1"
pytestmark = pytest.mark.skipif(
    not (RUNTIME and ENABLED),
    reason="set NEBULA_TEST_KALI_TERMINAL=1 with a real rootless runtime",
)


def test_real_kali_terminal_is_root_writable_networked_and_ephemeral(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o777)
    workspace.chmod(0o777)
    runner = ContainerSandboxRunner(runtime=RUNTIME, workspace_roots=[tmp_path])

    async def terminal(image: str, name: str, commands: bytes) -> bytes:
        request = SandboxRequest(
            image=image,
            command=["/bin/bash", "--noprofile", "--norc", "-i"],
            workspace=workspace,
            workspace_access=SandboxWorkspaceAccess.WRITE,
            network=SandboxNetwork.UNRESTRICTED,
            execution_kind=SandboxExecutionKind.HUMAN_TERMINAL,
            container_user=SandboxContainerUser.ROOT,
            root_filesystem=SandboxRootFilesystem.WRITABLE,
            environment={"LANG": "C.UTF-8", "TERM": "xterm-256color"},
            limits=SandboxLimits(timeout_seconds=180),
        )
        process = await runner.open_terminal(
            request, container_name=name, columns=100, rows=30
        )
        try:
            await process.write(commands)
            output = bytearray()
            while True:
                chunk = await asyncio.wait_for(process.read(), timeout=180)
                if not chunk:
                    break
                output.extend(chunk)
            assert await asyncio.wait_for(process.wait(), timeout=10) == 0
            return bytes(output)
        finally:
            await process.close()

    async def scenario() -> None:
        profile = runner.profile
        assert profile is not None
        platform = "linux/arm64" if os.uname().machine == "arm64" else "linux/amd64"
        image = await ContainerImagePreparer(
            runner=runner,
            platform=platform,
            source_reference="docker.io/kalilinux/kali-rolling:latest",
            expected_repository="docker.io/kalilinux/kali-rolling",
        ).prepare()
        first = await terminal(
            image.resolved_reference,
            "nebula-terminal-kali-integration-1",
            b"id -u\n"
            b"touch /root/ephemeral-marker\n"
            b"printf persisted > /workspace/kali-workspace.txt\n"
            b"getent hosts kali.org >/dev/null && echo network-ok\n"
            b"command -v nmap >/dev/null && echo nmap-ok\n"
            b"command -v ping >/dev/null && echo ping-installed\n"
            b"apt-get update -qq && echo apt-ok\n"
            b"exit\n",
        )
        assert b"\r\n0\r\n" in first
        assert b"network-ok" in first
        assert b"nmap-ok" in first
        assert b"ping-installed" in first
        assert b"apt-ok" in first
        assert (workspace / "kali-workspace.txt").read_text() == "persisted"

        second = await terminal(
            image.resolved_reference,
            "nebula-terminal-kali-integration-2",
            b"test ! -e /root/ephemeral-marker && echo ephemeral-ok\n"
            b"cat /workspace/kali-workspace.txt\n"
            b"exit\n",
        )
        assert b"ephemeral-ok" in second
        assert b"persisted" in second
        names, _stderr, return_code = await runner._capture(
            "ps", "--all", "--format", "{{.Names}}"
        )
        assert return_code == 0
        assert "nebula-terminal-kali-integration" not in names

    asyncio.run(scenario())
