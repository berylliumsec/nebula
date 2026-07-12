import asyncio

import pytest
from pydantic import ValidationError

from nebula.v3.sandbox import (
    AnalysisOnlyRunner,
    ContainerSandboxRunner,
    SandboxError,
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


@pytest.mark.parametrize(
    "access,expected_mode",
    [
        (SandboxWorkspaceAccess.NONE, None),
        (SandboxWorkspaceAccess.READ, "ro"),
        (SandboxWorkspaceAccess.WRITE, "rw"),
    ],
)
def test_workspace_mount_is_omitted_or_scoped_by_declared_access(
    tmp_path, access, expected_mode
):
    runner = ContainerSandboxRunner(runtime="/usr/bin/podman")
    request = _request(tmp_path, workspace_access=access)
    workspace = runner._validate(request)
    argv = runner._argv(request, workspace)
    mounts = [argument for argument in argv if argument.startswith("--mount=")]

    if expected_mode is None:
        assert workspace is None
        assert mounts == []
        assert "--workdir=/tmp" in argv
    else:
        assert workspace == tmp_path.resolve()
        assert mounts == [
            f"--mount=type=bind,src={tmp_path.resolve()},dst=/workspace,{expected_mode}"
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
