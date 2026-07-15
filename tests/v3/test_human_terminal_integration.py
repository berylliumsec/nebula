from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.container_terminal import terminal_prompt_command, terminal_ps0
from nebula.v3.domain import Engagement
from nebula.v3.sandbox import (
    ContainerImagePreparer,
    ContainerRuntimeType,
    ContainerSandboxRunner,
    RunnerPlatform,
    RunnerProfile,
    SandboxContainerUser,
    SandboxExecutionKind,
    SandboxLimits,
    SandboxNetwork,
    SandboxRequest,
    SandboxRootFilesystem,
    SandboxWorkspaceAccess,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.terminal_history import Osc633CommandParser, TerminalCommandHistory


RUNTIME = os.getenv("NEBULA_TEST_CONTAINER_RUNTIME")
ENABLED = os.getenv("NEBULA_TEST_KALI_TERMINAL") == "1"
pytestmark = pytest.mark.skipif(
    not (RUNTIME and ENABLED),
    reason="set NEBULA_TEST_KALI_TERMINAL=1 with a real rootless runtime",
)


def test_real_kali_terminal_is_root_writable_networked_and_ephemeral(tmp_path):
    assert RUNTIME is not None
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o777)
    workspace.chmod(0o777)
    profile = RunnerProfile.from_runtime(RUNTIME)
    if (
        profile.runtime_type == ContainerRuntimeType.DOCKER
        and profile.platform == RunnerPlatform.LINUX
    ):
        rootless_socket = Path(
            os.getenv(
                "NEBULA_TEST_DOCKER_SOCKET",
                f"/run/user/{os.getuid()}/docker.sock",
            )
        )
        if not rootless_socket.is_socket():
            pytest.skip(f"rootless Docker socket is unavailable: {rootless_socket}")
        context_name = "nebula-test-rootless"
        subprocess.run(
            [
                RUNTIME,
                "context",
                "create",
                context_name,
                "--docker",
                f"host=unix://{rootless_socket}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        profile = profile.model_copy(update={"context": context_name})
    runner = ContainerSandboxRunner(profile=profile, workspace_roots=[tmp_path])
    store = NebulaStore(tmp_path / "terminal-audit.db")
    project = store.create(Engagement(name="Real Kali selective recording"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    history = TerminalCommandHistory(
        store.database,
        store=store,
        artifact_store=artifacts,
    )

    async def terminal(
        image: str,
        name: str,
        commands: bytes,
        *,
        environment: dict[str, str] | None = None,
        parser: Osc633CommandParser | None = None,
    ) -> bytes:
        request = SandboxRequest(
            image=image,
            command=["/bin/bash", "--noprofile", "--norc", "-i"],
            workspace=workspace,
            workspace_access=SandboxWorkspaceAccess.WRITE,
            network=SandboxNetwork.UNRESTRICTED,
            execution_kind=SandboxExecutionKind.HUMAN_TERMINAL,
            container_user=SandboxContainerUser.ROOT,
            root_filesystem=SandboxRootFilesystem.WRITABLE,
            environment={
                "LANG": "C.UTF-8",
                "TERM": "xterm-256color",
                **(environment or {}),
            },
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
                parsed = parser.feed(chunk) if parser is not None else None
                output.extend(parsed.passthrough if parsed is not None else chunk)
                if parsed is not None:
                    for capture in parsed.captures:
                        history.record_capture(
                            engagement_id=project.id,
                            session_id=name,
                            operator_id="integration-test",
                            capture=capture,
                        )
            assert await asyncio.wait_for(process.wait(), timeout=10) == 0
            if parser is not None:
                tail = parser.flush()
                output.extend(tail.passthrough)
                interrupted = parser.finish_active(
                    detail="integration shell exited before its final prompt"
                )
                if interrupted is not None:
                    history.record_capture(
                        engagement_id=project.id,
                        session_id=name,
                        operator_id="integration-test",
                        capture=interrupted,
                    )
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
        assert b"0" in first.replace(b"\r", b"\n").splitlines()
        assert b"network-ok" in first
        assert b"nmap-ok" in first
        assert b"ping-installed" in first
        assert b"apt-ok" in first
        assert (workspace / "kali-workspace.txt").read_text() == "persisted"

        assert "nmap" in image.security_tools
        assert "printf" not in image.security_tools
        nonce = "realKaliSelectiveAudit123"
        parser = history.new_parser(
            nonce=nonce,
            engagement_id=project.id,
            session_id="nebula-terminal-kali-integration-audit",
            operator_id="integration-test",
            runtime_image_digest=image.digest,
            manifest_sha256=image.security_tool_manifest_sha256,
            default_tools=image.security_tools,
        )
        audited = await terminal(
            image.resolved_reference,
            "nebula-terminal-kali-integration-audit",
            b"printf 'unselected-result\\n'\nnmap --version\nexit\n",
            environment={
                "HISTFILE": "/dev/null",
                "PS0": terminal_ps0(nonce),
                "PROMPT_COMMAND": terminal_prompt_command(nonce),
            },
            parser=parser,
        )
        assert b"unselected-result" in audited
        assert b"Nmap version" in audited
        records = {record.command: record for record in history.all_records(project.id)}
        plain = records["printf 'unselected-result\\n'"]
        selected = records["nmap --version"]
        assert plain.capture_decision == "not_selected"
        assert plain.raw_output_available is False
        assert selected.capture_decision == "selected_tool"
        assert selected.matched_tools == ["nmap"]
        assert selected.raw_output_available is True
        assert (
            b"Nmap version"
            in history.output_bytes(project.id, selected.id, raw=True)[0]
        )

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
