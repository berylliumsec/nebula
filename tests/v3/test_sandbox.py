import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nebula.v3.sandbox import (
    AnalysisOnlyRunner,
    ContainerEgressController,
    ContainerRuntimeType,
    ContainerSandboxRunner,
    ContainerToolPackRuntimeAdapter,
    EgressRule,
    RunnerIsolationMode,
    RunnerPlatform,
    RunnerProfile,
    SandboxError,
    SandboxExecutionKind,
    SandboxNetwork,
    SandboxRequest,
    SandboxUnavailable,
    SandboxWorkspaceAccess,
)


DIGEST_IMAGE = "registry.invalid/nebula-tool@sha256:" + "a" * 64


def _request(tmp_path, **changes):
    values = {
        "image": DIGEST_IMAGE,
        "command": ["nmap", "-sV", "10.20.30.40"],
        "workspace": tmp_path,
    }
    values.update(changes)
    return SandboxRequest(**values)


def test_analysis_only_runner_fails_closed_instead_of_using_host(tmp_path):
    runner = AnalysisOnlyRunner()
    available, reason = asyncio.run(runner.available())
    assert available is False
    assert "container runner" in reason
    with pytest.raises(SandboxUnavailable, match="never fall back to the host"):
        asyncio.run(runner.run(_request(tmp_path)))


def test_sandbox_request_rejects_nul_and_unbounded_scoped_network(tmp_path):
    with pytest.raises(ValidationError, match="NUL"):
        _request(tmp_path, command=["sh", "bad\x00argument"])
    with pytest.raises(ValidationError, match="network_name"):
        _request(tmp_path, network=SandboxNetwork.SCOPED)


def test_container_argv_is_direct_and_contains_hardening_flags(tmp_path):
    runner = ContainerSandboxRunner(
        runtime="/usr/bin/podman",
        egress_enforced_networks={"nebula-scope-eng-1"},
    )
    request = _request(
        tmp_path,
        network=SandboxNetwork.SCOPED,
        network_name="nebula-scope-eng-1",
        pinned_hosts={"target.example.test": "10.20.30.40"},
        environment={"LANG": "C.UTF-8"},
    )
    workspace = runner._validate(request)
    argv = runner._argv(request, workspace)

    assert argv[:3] == ["/usr/bin/podman", "run", "--rm"]
    assert "--name=nebula-tool" in argv
    assert "--pull=never" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--user=65532:65532" in argv
    assert "--network=nebula-scope-eng-1" in argv
    assert "--add-host=target.example.test:10.20.30.40" in argv
    assert argv[-4:] == [DIGEST_IMAGE, "nmap", "-sV", "10.20.30.40"]
    assert all(value not in {"sh", "-c", "/bin/sh"} for value in argv[:-4])


def test_container_terminal_argv_adds_tty_without_host_shell_fallback(tmp_path):
    runner = ContainerSandboxRunner(runtime="/usr/bin/docker")
    request = _request(
        tmp_path,
        command=["/bin/bash", "--noprofile", "--norc", "-i"],
        workspace_access=SandboxWorkspaceAccess.WRITE,
    )
    workspace = runner._validate(request)
    argv = runner._argv(
        request,
        workspace,
        container_name="nebula-terminal-abc123",
        interactive=True,
        tty=True,
    )

    assert argv[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert "--interactive" in argv
    assert "--tty" in argv
    assert "--network=none" in argv
    assert f"--mount=type=bind,src={tmp_path.resolve()},dst=/workspace" in argv
    assert argv[-5:] == [
        DIGEST_IMAGE,
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-i",
    ]
    assert all(value not in {"-c", "/bin/sh"} for value in argv[:-5])
    with pytest.raises(SandboxError, match="requires interactive"):
        runner._argv(request, workspace, tty=True)


def test_orphan_cleanup_removes_only_strict_terminal_namespace(tmp_path, monkeypatch):
    runner = ContainerSandboxRunner(runtime="/usr/bin/podman")
    removed: list[str] = []

    async def capture(*arguments):
        assert arguments == ("ps", "--all", "--format", "{{.Names}}")
        return (
            "nebula-terminal-good123\n"
            "nebula-exec-preserve\n"
            "nebula-terminal-bad/name\n"
            "customer-container\n",
            "",
            0,
        )

    async def remove(name):
        removed.append(name)

    monkeypatch.setattr(runner, "_capture", capture)
    monkeypatch.setattr(runner, "_force_remove", remove)
    asyncio.run(runner.cleanup_terminal_containers())

    assert removed == ["nebula-terminal-good123"]


@pytest.mark.parametrize(
    "access,expected_suffix",
    [
        (SandboxWorkspaceAccess.NONE, None),
        (SandboxWorkspaceAccess.READ, ",readonly=true"),
        (SandboxWorkspaceAccess.WRITE, ""),
    ],
)
@pytest.mark.parametrize("runtime", ["/usr/bin/docker", "/usr/bin/podman"])
def test_workspace_mount_is_omitted_or_scoped_by_declared_access(
    tmp_path, runtime, access, expected_suffix
):
    runner = ContainerSandboxRunner(runtime=runtime)
    request = _request(tmp_path, workspace_access=access)
    workspace = runner._validate(request)
    argv = runner._argv(request, workspace)
    mounts = [argument for argument in argv if argument.startswith("--mount=")]

    if expected_suffix is None:
        assert workspace is None
        assert mounts == []
        assert "--workdir=/tmp" in argv
    else:
        assert workspace == tmp_path.resolve()
        assert mounts == [
            f"--mount=type=bind,src={tmp_path.resolve()},dst=/workspace{expected_suffix}"
        ]
        assert "--workdir=/workspace" in argv


def test_runner_rejects_unpinned_images_secrets_and_unapproved_networks(tmp_path):
    runner = ContainerSandboxRunner(runtime="/usr/bin/podman")
    with pytest.raises(SandboxError, match="pinned"):
        runner._validate(_request(tmp_path, image="registry.invalid/latest"))
    with pytest.raises(SandboxError, match="OPENAI_API_KEY"):
        runner._validate(
            _request(tmp_path, environment={"OPENAI_API_KEY": "must-not-pass"})
        )
    with pytest.raises(SandboxUnavailable, match="egress-enforced"):
        runner._validate(
            _request(
                tmp_path,
                network=SandboxNetwork.SCOPED,
                network_name="ordinary-bridge",
            )
        )


def test_runtime_discovery_ignores_ambient_path_and_accepts_explicit_absolute_path(
    tmp_path, monkeypatch
):
    trusted = tmp_path / "trusted" / "podman"
    trusted.parent.mkdir()
    trusted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    trusted.chmod(0o755)
    untrusted = tmp_path / "untrusted"
    untrusted.mkdir()
    (untrusted / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (untrusted / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", str(untrusted))
    monkeypatch.delenv("NEBULA_V3_CONTAINER_RUNTIME", raising=False)
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", (trusted,))

    assert ContainerSandboxRunner().runtime == str(trusted)
    assert ContainerSandboxRunner(runtime=str(trusted)).runtime == str(trusted)
    with pytest.raises(ValueError, match="absolute path"):
        ContainerSandboxRunner(runtime="docker")
    with pytest.raises(ValueError, match="docker or podman"):
        ContainerSandboxRunner(runtime=str(tmp_path / "other"))


def test_runner_profiles_require_supported_explicit_runtime_combinations():
    with pytest.raises(ValidationError, match="absolute path"):
        RunnerProfile(
            runtime_type=ContainerRuntimeType.PODMAN,
            executable="podman",
            platform=RunnerPlatform.LINUX,
            isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
        )
    with pytest.raises(ValidationError, match="podman_machine"):
        RunnerProfile(
            runtime_type=ContainerRuntimeType.PODMAN,
            executable="/usr/bin/podman",
            platform=RunnerPlatform.MACOS,
            isolation_mode=RunnerIsolationMode.DOCKER_DESKTOP_VM,
        )
    with pytest.raises(ValidationError, match="must match"):
        RunnerProfile(
            runtime_type=ContainerRuntimeType.DOCKER,
            executable="/usr/bin/podman",
            platform=RunnerPlatform.LINUX,
            isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
        )


def test_linux_rootless_podman_profile_is_certified(monkeypatch):
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    runner = ContainerSandboxRunner(
        profile=RunnerProfile(
            runtime_type=ContainerRuntimeType.PODMAN,
            executable="/usr/bin/podman",
            platform=RunnerPlatform.LINUX,
            isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
        )
    )

    async def capture(*arguments):
        assert arguments == ("info", "--format", "json")
        return '{"host":{"security":{"rootless":true},"os":"linux"}}', "", 0

    monkeypatch.setattr(runner, "_capture", capture)
    available, detail = asyncio.run(runner.available())
    assert available is True
    assert "rootless Podman" in detail


def test_linux_podman_named_connection_rejects_remote_ssh(monkeypatch):
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    runner = ContainerSandboxRunner(
        profile=RunnerProfile(
            runtime_type=ContainerRuntimeType.PODMAN,
            executable="/usr/bin/podman",
            platform=RunnerPlatform.LINUX,
            isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
            context="remote-lab",
        )
    )

    async def capture(*arguments):
        assert arguments[0] == "system"
        return (
            '[{"Name":"remote-lab","URI":"ssh://runner.example/run/podman.sock"}]',
            "",
            0,
        )

    monkeypatch.setattr(runner, "_capture", capture)
    available, detail = asyncio.run(runner.available())
    assert available is False
    assert "local Unix socket" in detail


def test_linux_docker_rejects_remote_context_and_ambient_tcp_endpoint(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    runner = ContainerSandboxRunner(
        profile=RunnerProfile(
            runtime_type=ContainerRuntimeType.DOCKER,
            executable="/usr/bin/docker",
            platform=RunnerPlatform.LINUX,
            isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
            context="nebula-local",
        )
    )

    async def remote_context(*arguments):
        assert arguments[:2] == ("context", "inspect")
        return '[{"Endpoints":{"docker":{"Host":"tcp://runner.example:2376"}}}]', "", 0

    monkeypatch.setattr(runner, "_capture", remote_context)
    available, detail = asyncio.run(runner.available())
    assert available is False
    assert "local absolute Unix socket" in detail

    monkeypatch.setenv("DOCKER_HOST", "tcp://runner.example:2376")
    available, detail = asyncio.run(runner.available())
    assert available is False
    assert "remote TCP/SSH" in detail


def test_local_rootless_docker_profile_is_certified(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    runner = ContainerSandboxRunner(
        profile=RunnerProfile(
            runtime_type=ContainerRuntimeType.DOCKER,
            executable="/usr/bin/docker",
            platform=RunnerPlatform.LINUX,
            isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
        )
    )

    async def capture(*arguments):
        if arguments[0] == "context":
            return (
                '[{"Endpoints":{"docker":{"Host":"unix:///run/user/1000/docker.sock"}}}]',
                "",
                0,
            )
        return '{"OSType":"linux","SecurityOptions":["name=rootless"]}', "", 0

    monkeypatch.setattr(runner, "_capture", capture)
    available, detail = asyncio.run(runner.available())
    assert available is True
    assert "rootless Docker" in detail


def test_macos_docker_desktop_vm_requires_local_linux_desktop(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    runner = ContainerSandboxRunner(
        profile=RunnerProfile(
            runtime_type=ContainerRuntimeType.DOCKER,
            executable="/usr/local/bin/docker",
            platform=RunnerPlatform.MACOS,
            isolation_mode=RunnerIsolationMode.DOCKER_DESKTOP_VM,
            context="desktop-linux",
        )
    )

    async def capture(*arguments):
        if arguments[0] == "context":
            return (
                '[{"Endpoints":{"docker":{"Host":"unix:///Users/me/.docker/run/docker.sock"}}}]',
                "",
                0,
            )
        return '{"OSType":"linux","OperatingSystem":"Docker Desktop"}', "", 0

    monkeypatch.setattr(runner, "_capture", capture)
    available, detail = asyncio.run(runner.available())
    assert available is True
    assert "Docker Desktop VM" in detail
    assert runner._runtime_argv() == [
        "/usr/local/bin/docker",
        "--context",
        "desktop-linux",
    ]


def test_macos_podman_machine_requires_running_rootless_loopback_connection(
    monkeypatch,
):
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    runner = ContainerSandboxRunner(
        profile=RunnerProfile(
            runtime_type=ContainerRuntimeType.PODMAN,
            executable="/opt/homebrew/bin/podman",
            platform=RunnerPlatform.MACOS,
            isolation_mode=RunnerIsolationMode.PODMAN_MACHINE,
            machine_name="podman-machine-default",
        )
    )

    async def capture(*arguments):
        if arguments[0] == "machine":
            return '[{"State":"running","Rootful":false}]', "", 0
        if arguments[0] == "system":
            return (
                '[{"Name":"podman-machine-default","URI":"ssh://core@127.0.0.1:51234/run/user/501/podman.sock"}]',
                "",
                0,
            )
        return '{"host":{"security":{"rootless":true},"os":"linux"}}', "", 0

    monkeypatch.setattr(runner, "_capture", capture)
    available, detail = asyncio.run(runner.available())
    assert available is True
    assert "Podman Machine" in detail
    assert runner._runtime_argv() == [
        "/opt/homebrew/bin/podman",
        "--connection",
        "podman-machine-default",
    ]


def test_parser_and_local_tool_contracts_are_networkless_and_read_only(tmp_path):
    with pytest.raises(ValidationError, match="cannot write"):
        _request(
            tmp_path,
            execution_kind=SandboxExecutionKind.PARSER,
            workspace_access=SandboxWorkspaceAccess.WRITE,
        )
    with pytest.raises(ValidationError, match="must use network=none"):
        _request(
            tmp_path,
            execution_kind=SandboxExecutionKind.PARSER,
            network=SandboxNetwork.SCOPED,
            network_name="legacy",
        )


def test_network_execution_requires_rules_and_certified_per_invocation_helper(
    tmp_path, monkeypatch
):
    runner = ContainerSandboxRunner(runtime="/usr/bin/podman")

    async def healthy():
        return True, "test runner"

    monkeypatch.setattr(runner, "available", healthy)
    request = _request(
        tmp_path,
        network=SandboxNetwork.SCOPED,
        execution_kind=SandboxExecutionKind.NETWORK_TOOL,
        egress_rules=[EgressRule(address="10.20.30.40", ports=[443, 80, 443])],
        pinned_hosts={"target.example.test": "10.20.30.40"},
    )
    assert request.egress_rules[0].ports == [80, 443]
    with pytest.raises(SandboxUnavailable, match="certified per-invocation"):
        asyncio.run(runner.run(request))


def test_actual_workspace_execution_requires_configured_roots(tmp_path, monkeypatch):
    runner = ContainerSandboxRunner(runtime="/usr/bin/podman")

    async def healthy():
        return True, "test runner"

    monkeypatch.setattr(runner, "available", healthy)
    with pytest.raises(SandboxUnavailable, match="configured workspace roots"):
        asyncio.run(
            runner.run(
                _request(
                    tmp_path,
                    workspace_access=SandboxWorkspaceAccess.READ,
                )
            )
        )


def test_egress_helper_requires_digest_and_absolute_executable():
    with pytest.raises(ValueError, match="sha256"):
        ContainerEgressController(helper_image="example.invalid/helper:latest")
    with pytest.raises(ValueError, match="absolute"):
        ContainerEgressController(
            helper_image="example.invalid/helper@sha256:" + "b" * 64,
            helper_executable="helper",
        )


def test_egress_helper_creates_one_filtered_namespace_and_cleans_it_up(
    tmp_path, monkeypatch
):
    calls = []

    class FakeProcess:
        def __init__(self, *, ready=False):
            self.returncode = None
            self.stdout = asyncio.StreamReader() if ready else None
            if self.stdout is not None:
                self.stdout.feed_data(b"READY\n")

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9
            if self.stdout is not None:
                self.stdout.feed_eof()

    async def create_process(*argv, **kwargs):
        calls.append((list(argv), kwargs))
        return FakeProcess(ready=len(calls) == 1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    controller = ContainerEgressController(
        helper_image="example.invalid/helper@sha256:" + "b" * 64
    )
    request = _request(
        tmp_path,
        network=SandboxNetwork.SCOPED,
        execution_kind=SandboxExecutionKind.NETWORK_TOOL,
        egress_rules=[
            EgressRule(address="10.20.30.40", ports=[443]),
            EgressRule(address="2001:db8::1", ports=[8443]),
        ],
    )

    async def scenario():
        lease = await controller.acquire(
            runtime_argv=["/usr/bin/podman"],
            runtime_environment={"HOME": "/tmp/home"},
            request=request,
            container_name="nebula-call",
            seccomp_profile=None,
        )
        assert lease.network_mode == "container:nebula-call-egress"
        await lease.close()

    asyncio.run(scenario())
    helper_argv = calls[0][0]
    assert "--network=bridge" in helper_argv
    assert "--cap-add=NET_ADMIN" in helper_argv
    assert "--allow" in helper_argv
    assert "tcp://10.20.30.40:443" in helper_argv
    assert "tcp://[2001:db8::1]:8443" in helper_argv
    assert not any(value.startswith("--mount=") for value in helper_argv)
    assert calls[1][0][-3:] == ["stop", "--time=0", "nebula-call-egress"]


def test_toolpack_runtime_adapter_inspects_exact_digest_platform_and_user(
    monkeypatch,
):
    runner = ContainerSandboxRunner(runtime="/usr/bin/podman")
    adapter = ContainerToolPackRuntimeAdapter(runner=runner, platform="linux/amd64")
    image = "example.invalid/tool@sha256:" + "c" * 64

    async def require_runner():
        return None

    async def runtime_command(*arguments, timeout_seconds):
        assert arguments[:3] == ("image", "inspect", image)
        assert timeout_seconds == 30
        return (
            '{"RepoDigests":["example.invalid/tool@sha256:'
            + "c" * 64
            + '"],"Os":"linux","Architecture":"amd64",'
            '"Config":{"User":"10001:10001"}}',
            "",
            0,
        )

    monkeypatch.setattr(adapter, "_require_runner", require_runner)
    monkeypatch.setattr(adapter, "_runtime_command", runtime_command)
    info = asyncio.run(adapter.inspect(image))
    assert info.image == image
    assert info.digest == "sha256:" + "c" * 64
    assert info.platform == "linux/amd64"
    assert info.user == "10001:10001"


def test_toolpack_runtime_adapter_smoke_test_is_offline_hardened_argv(
    monkeypatch,
):
    runner = ContainerSandboxRunner(runtime="/usr/bin/podman")
    adapter = ContainerToolPackRuntimeAdapter(runner=runner, platform="linux/amd64")
    image = "example.invalid/tool@sha256:" + "d" * 64
    observed = []

    async def run(request):
        observed.append(request)
        return SimpleNamespace(exit_code=0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr(runner, "run", run)
    result = asyncio.run(
        adapter.smoke_test(
            image=image,
            command=["/usr/local/bin/tool", "--self-test"],
            timeout_seconds=15,
        )
    )
    assert result.exit_code == 0
    request = observed[0]
    assert request.network == SandboxNetwork.NONE
    assert request.execution_kind == SandboxExecutionKind.LOCAL_TOOL
    assert request.workspace_access == SandboxWorkspaceAccess.NONE
    assert request.command == ["/usr/local/bin/tool", "--self-test"]
