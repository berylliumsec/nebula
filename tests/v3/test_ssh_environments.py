import asyncio
import shlex
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from nebula.v3 import environments, ssh_environments
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Approval,
    ApprovalStatus,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    SshEnvironment,
    SshEnvironmentApproval,
    SshEnvironmentProbe,
    ToolCallOrigin,
    utc_now,
)
from nebula.v3.environments import (
    SshEnvironmentService,
    SshEnvironmentSettings,
    build_ssh_tool_plugins,
    resolve_ssh_environments,
    ssh_tool_name,
)
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore, NotFoundError
from nebula.v3.tools import ApprovalRequired, ToolInvocation
from nebula.v3.ssh_environments import (
    classify_ssh_failure,
    discover_hosts,
    parse_probe_output,
    parse_resolved_config,
    probe_host,
    remote_command_argv,
)


def write_config(tmp_path, text, name="config"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_discover_lists_concrete_hosts_with_aliases_and_comments(tmp_path):
    config = write_config(
        tmp_path,
        """
Host *
    ServerAliveInterval 30

Host research3 research3-lan research3s-MacBook.local
    # Resolve the MacBook by its LAN hostname;
    # verify the model before every run.
    HostName research3s-MacBook.local
    User research3

# >>> managed block >>>
Host jetson
    HostName 192.168.1.218
    # trailing comment is not a description
Host=bastion-*
    User ops
Match host foo exec "true"
    User nobody
""",
    )

    scan = discover_hosts(config)

    assert scan.exists
    assert [host.alias for host in scan.hosts] == ["research3", "jetson"]
    research3, jetson = scan.hosts
    assert research3.aliases == (
        "research3",
        "research3-lan",
        "research3s-MacBook.local",
    )
    assert (
        research3.comment
        == "Resolve the MacBook by its LAN hostname; verify the model before every run."
    )
    assert research3.line == 5
    assert jetson.comment == ""
    assert scan.skipped_patterns == ["*", "bastion-*"]
    assert scan.skipped_match_blocks == 1


def test_discover_follows_includes_once_and_keeps_first_alias_definition(tmp_path):
    (tmp_path / "config.d").mkdir()
    write_config(
        tmp_path / "config.d", "Host lab\n  User a\nHost dup\n  User b\n", "lab.conf"
    )
    config = write_config(
        tmp_path,
        "Host dup\n  User first\nInclude config.d/*.conf\nInclude config.d/*.conf\nInclude config\n",
    )

    scan = discover_hosts(config)

    assert [host.alias for host in scan.hosts] == ["dup", "lab"]
    assert scan.files_read == [str(config), str(tmp_path / "config.d" / "lab.conf")]


def test_discover_reports_missing_file(tmp_path):
    scan = discover_hosts(tmp_path / "absent")

    assert not scan.exists
    assert scan.hosts == []


def test_parse_resolved_config_keeps_only_operator_facts(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ssh_environments.Path, "home", classmethod(lambda cls: tmp_path)
    )
    output = f"""user research
hostname 192.168.1.221
port 2222
identityfile {tmp_path}/.ssh/id_ed25519
identityfile /etc/ssh/shared_key
controlmaster auto
batchmode no
serveraliveinterval 0
stricthostkeychecking ask
sendenv LANG
"""

    resolved = parse_resolved_config("research", output)

    assert resolved.hostname == "192.168.1.221"
    assert resolved.user == "research"
    assert resolved.port == 2222
    assert resolved.identity_files == ("~/.ssh/id_ed25519", "/etc/ssh/shared_key")
    assert resolved.options == {"controlmaster": "auto", "stricthostkeychecking": "ask"}


def test_parse_resolved_config_hides_ssh_default_key_candidates(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ssh_environments.Path, "home", classmethod(lambda cls: tmp_path)
    )
    defaults = "".join(
        f"identityfile {tmp_path}/.ssh/{name}\n"
        for name in ("id_rsa", "id_ecdsa", "id_ed25519")
    )

    assert (
        parse_resolved_config("lab", f"hostname lab\n{defaults}").identity_files == ()
    )
    assert parse_resolved_config(
        "lab", f"identityfile {tmp_path}/.ssh/id_ed25519\n"
    ).identity_files == ("~/.ssh/id_ed25519",)


def test_remote_command_runs_in_posix_shell_without_prompts(monkeypatch):
    monkeypatch.setattr(ssh_environments, "ssh_binary", lambda: "/usr/bin/ssh")

    argv = remote_command_argv("research3", "ls -la 'a b'", cwd="~/research dir")

    assert argv[:8] == [
        "/usr/bin/ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-T",
        "--",
        "research3",
    ]
    remote = shlex.split(argv[8])
    assert remote[:3] == ["exec", "/bin/sh", "-c"]
    assert remote[3] == "cd \"$HOME\"'/research dir' && ls -la 'a b'"


@pytest.mark.parametrize("alias", ["", "-oProxyCommand=x", "two words", "bastion-*"])
def test_remote_command_rejects_unsafe_aliases(monkeypatch, alias):
    monkeypatch.setattr(ssh_environments, "ssh_binary", lambda: "/usr/bin/ssh")

    with pytest.raises(ValueError):
        remote_command_argv(alias, "true")


@pytest.mark.parametrize(
    ("stderr", "status"),
    [
        ("Host key verification failed.", "host_key_untrusted"),
        ("research@1.2.3.4: Permission denied (publickey).", "auth_failed"),
        ("ssh: connect to host 1.2.3.4 port 22: Connection timed out", "unreachable"),
        (
            "ssh: Could not resolve hostname nope: Name or service not known",
            "unreachable",
        ),
        ("something else", "error"),
    ],
)
def test_classify_ssh_failure(stderr, status):
    assert classify_ssh_failure(stderr) == status


def test_parse_probe_output():
    result = parse_probe_output(
        "research3",
        "system=Darwin\narch=arm64\nos=macOS 26.0\nmodel=Mac17,5\ntools= python3 git xcrun\nsudo=no\nnoise\n",
        38,
    )

    assert result.status == "reachable"
    assert (result.system, result.arch, result.os_version, result.model) == (
        "Darwin",
        "arm64",
        "macOS 26.0",
        "Mac17,5",
    )
    assert result.tools == ("python3", "git", "xcrun")
    assert result.passwordless_sudo is False
    assert result.latency_ms == 38


def test_probe_classifies_connection_failures(monkeypatch):
    monkeypatch.setattr(ssh_environments, "ssh_binary", lambda: "/usr/bin/ssh")

    def fake_run(argv, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        return subprocess.CompletedProcess(
            argv, 255, "", "Host key verification failed.\n"
        )

    monkeypatch.setattr(ssh_environments.subprocess, "run", fake_run)

    result = probe_host("pi-one")

    assert result.status == "host_key_untrusted"
    assert result.detail == "Host key verification failed."


CONFIG = """
Host research3 research3-lan
    # Apple Silicon research Mac
    HostName research3s-MacBook.local
    User research3
Host jetson
    HostName 192.168.1.218
"""


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_environments, "ssh_binary", lambda: None)
    store = NebulaStore(tmp_path / "nebula.db")
    probes = []

    def fake_probe(alias):
        probes.append(alias)
        return ssh_environments.SshProbeResult(
            alias=alias,
            status="reachable",
            latency_ms=38,
            system="Darwin",
            os_version="macOS 26.0",
            arch="arm64",
            model="Mac17,5",
            tools=("python3", "git"),
            passwordless_sudo=False,
        )

    config = write_config(tmp_path, CONFIG)
    result = SshEnvironmentService(store, config_path=config, probe=fake_probe)
    result.probes = probes
    return result


def test_discovery_joins_config_hosts_with_saved_settings(service):
    service.save("jetson", SshEnvironmentSettings(enabled=True, display_name="Jetson"))
    orphan = SshEnvironment(id="ssh:retired", alias="retired", enabled=True)
    service.store.create(orphan)

    discovery = service.discover()

    assert discovery.config_exists
    assert [(host.alias, host.in_config) for host in discovery.hosts] == [
        ("research3", True),
        ("jetson", True),
        ("retired", False),
    ]
    research3, jetson, _ = discovery.hosts
    assert research3.environment is None
    assert research3.comment == "Apple Silicon research Mac"
    assert jetson.environment.enabled and jetson.environment.label == "Jetson"


def test_save_creates_once_then_updates_and_rejects_unknown_hosts(service):
    created = service.save(
        "research3",
        SshEnvironmentSettings(enabled=True, working_directory=" ~/research "),
    )
    updated = service.save(
        "research3",
        SshEnvironmentSettings(
            notes="Verify the model first.", expected_revision=created.revision
        ),
    )

    assert created.id == "ssh:research3"
    assert created.command_approval == SshEnvironmentApproval.ASK
    assert updated.enabled and updated.working_directory == "~/research"
    assert updated.notes == "Verify the model first."
    assert len(service.saved()) == 1
    with pytest.raises(NotFoundError):
        service.save("not-in-config", SshEnvironmentSettings(enabled=True))


def test_probe_records_fingerprint_and_forget_removes_settings(service):
    probed = asyncio.run(service.probe("research3"))

    assert service.probes == ["research3"]
    assert not probed.enabled
    assert probed.last_probe.status == "reachable"
    assert probed.last_probe.model == "Mac17,5"
    service.forget("research3")
    assert service.get("research3") is None


def test_chat_selection_defaults_to_enabled_hosts_and_rejects_disabled(service):
    service.save("research3", SshEnvironmentSettings(enabled=True))
    service.save("jetson", SshEnvironmentSettings(enabled=False))

    assert [item.alias for item in resolve_ssh_environments(service.store, None)] == [
        "research3"
    ]
    assert resolve_ssh_environments(service.store, []) == ()
    with pytest.raises(ValueError, match="not enabled"):
        resolve_ssh_environments(service.store, ["ssh:jetson"])


def test_chat_selection_adds_optional_config_metadata_to_model_tool(service):
    service.save(
        "research3",
        SshEnvironmentSettings(enabled=True, notes="Prefer it for exact-build tests."),
    )

    selected = resolve_ssh_environments(
        service.store, ["ssh:research3"], config_path=service.config_path
    )
    plugin = build_ssh_tool_plugins(selected)[0]

    assert selected[0].notes == (
        "SSH config metadata: Apple Silicon research Mac\n"
        "Nebula notes: Prefer it for exact-build tests."
    )
    assert "SSH config metadata: Apple Silicon research Mac" in plugin.spec.description
    assert "Nebula notes: Prefer it for exact-build tests." in plugin.spec.description
    # Enrichment is per turn; discovery never overwrites the operator's saved notes.
    assert service.get("research3").notes == "Prefer it for exact-build tests."


def test_chat_selection_without_config_metadata_keeps_saved_notes(service):
    service.save(
        "jetson", SshEnvironmentSettings(enabled=True, notes="Use for CUDA work.")
    )

    selected = resolve_ssh_environments(
        service.store, ["ssh:jetson"], config_path=service.config_path
    )

    assert selected[0].notes == "Use for CUDA work."
    assert (
        "SSH config metadata:"
        not in build_ssh_tool_plugins(selected)[0].spec.description
    )


def test_several_hosts_can_be_enabled_for_one_chat_at_once(service):
    service.save("research3", SshEnvironmentSettings(enabled=True))
    service.save("jetson", SshEnvironmentSettings(enabled=True))

    discovery = service.discover()
    assert [host.environment.enabled for host in discovery.hosts] == [True, True]
    automatic = resolve_ssh_environments(service.store, None)
    assert [item.alias for item in automatic] == ["jetson", "research3"]
    explicit = resolve_ssh_environments(service.store, ["ssh:research3", "ssh:jetson"])
    assert [item.alias for item in explicit] == ["research3", "jetson"]
    assert [plugin.spec.name for plugin in build_ssh_tool_plugins(automatic)] == [
        "ssh.jetson.run_command",
        "ssh.research3.run_command",
    ]


def test_api_lists_saves_probes_and_forgets(service):
    client = TestClient(
        create_app(
            service.store, allow_unauthenticated=True, ssh_environment_service=service
        )
    )

    listed = client.get("/api/v1/ssh-environments")
    saved = client.put(
        "/api/v1/ssh-environments/research3",
        json={"enabled": True, "command_approval": "allow"},
    )
    probed = client.post("/api/v1/ssh-environments/research3/probe")
    missing = client.put("/api/v1/ssh-environments/nope", json={"enabled": True})
    forgotten = client.delete("/api/v1/ssh-environments/research3")

    assert listed.status_code == 200
    assert [host["alias"] for host in listed.json()["hosts"]] == ["research3", "jetson"]
    assert saved.status_code == 200 and saved.json()["command_approval"] == "allow"
    assert probed.json()["last_probe"]["os_version"] == "macOS 26.0"
    assert probed.json()["enabled"] is True
    assert missing.status_code == 404
    assert forgotten.status_code == 204
    assert (
        client.get("/api/v1/ssh-environments").json()["hosts"][0]["environment"] is None
    )


def test_tool_spec_describes_host_and_follows_approval_policy():
    environment = SshEnvironment(
        id="ssh:research3s-MacBook.local",
        alias="research3s-MacBook.local",
        display_name="Research Mac",
        enabled=True,
        notes="Verify the model first.",
        working_directory="~/research",
        last_probe=SshEnvironmentProbe(
            status="reachable",
            os_version="macOS 26.0",
            arch="arm64",
            model="Mac17,5",
            passwordless_sudo=False,
        ),
    )
    allowed = environment.model_copy(
        update={"command_approval": SshEnvironmentApproval.ALLOW}
    )

    ask_plugin, allow_plugin = build_ssh_tool_plugins((environment, allowed))

    assert ask_plugin.spec.name == ssh_tool_name("research3s-MacBook.local")
    assert ask_plugin.spec.name.startswith("ssh.research3s-macbook-local-")
    assert ssh_tool_name("jetson") == "ssh.jetson.run_command"
    assert ask_plugin.spec.requires_approval is True
    assert allow_plugin.spec.requires_approval is False
    description = ask_plugin.spec.description
    assert description.splitlines()[0] == (
        "Run a shell command on the operator's machine Research Mac (SSH host research3s-MacBook.local)."
    )
    for fragment in (
        "macOS 26.0 arm64",
        "Mac17,5",
        "do not use sudo",
        "~/research",
        "Verify the model first.",
    ):
        assert fragment in description


def _local_shell(monkeypatch):
    """Run the would-be remote script locally instead of over ssh."""

    def argv(alias, command, *, cwd=None, path=None, connect_timeout=10):
        script = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        return ["/bin/sh", "-c", script]

    monkeypatch.setattr(environments.ssh, "remote_command_argv", argv)


def _platform_components(tmp_path, store, environment):
    store.create(Engagement(id="eng-1", name="Lab"))
    store.create(
        ChatTurn(
            id="turn-1",
            engagement_id="eng-1",
            session_id="session-1",
            provider_profile_id="provider-1",
            model="m",
            status=ChatTurnStatus.ROUTING,
        )
    )
    platform = RuntimePlatform(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core",
    )
    return platform.chat_components(
        engagement_id="eng-1",
        turn_id="turn-1",
        provider=None,
        model="m",
        ssh_environments=(environment,),
        allow_empty=True,
    )


def _invocation(components, tool_name, arguments):
    return ToolInvocation(
        engagement_id="eng-1",
        run_id="turn-1",
        origin=ToolCallOrigin.CHAT,
        chat_turn_id="turn-1",
        tool_name=tool_name,
        arguments=arguments,
        workspace=components.workspace,
        idempotency_key="chat:turn-1:step:0",
    )


def test_allowed_host_runs_through_the_broker_and_records_output(tmp_path, monkeypatch):
    _local_shell(monkeypatch)
    store = NebulaStore(tmp_path / "nebula.db")
    environment = SshEnvironment(
        id="ssh:lab",
        alias="lab",
        enabled=True,
        working_directory=str(tmp_path),
        command_approval=SshEnvironmentApproval.ALLOW,
    )
    components = _platform_components(tmp_path, store, environment)
    name = ssh_tool_name("lab")

    assert name in components.specs
    result = asyncio.run(
        components.broker.execute(
            _invocation(components, name, {"command": "pwd; echo oops >&2; exit 3"}),
            components.scope,
        )
    )

    assert result.exit_code == 3
    assert result.execution["ssh_alias"] == "lab"
    assert result.execution["cwd"] == str(tmp_path)
    assert result.receipt is not None


def test_ssh_start_failure_records_diagnostic_and_returns_failed_receipt(monkeypatch):
    environment = SshEnvironment(id="ssh:lab", alias="lab", enabled=True)
    plugin = build_ssh_tool_plugins((environment,))[0]
    recorded = []

    async def failed_command(*_args, **_kwargs):
        raise OSError("private remote path /secret/work was unavailable")

    monkeypatch.setattr(environments, "run_remote_command", failed_command)
    monkeypatch.setattr(
        environments,
        "record_caught_exception",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    result = asyncio.run(
        plugin.execute(SimpleNamespace(arguments={"command": "true"}), None)
    )

    assert result.exit_code == 1
    assert recorded[0][0][:3] == (
        "runtime",
        "runtime.ssh.command_start_failed",
        "An SSH command could not start.",
    )
    assert isinstance(recorded[0][0][3], OSError)
    assert recorded[0][1] == {
        "stage": "execute",
        "metadata": {"transport": "ssh", "operation": "run_command"},
    }


def test_ask_host_stops_for_approval_then_runs_once_approved(tmp_path, monkeypatch):
    _local_shell(monkeypatch)
    store = NebulaStore(tmp_path / "nebula.db")
    environment = SshEnvironment(id="ssh:lab", alias="lab", enabled=True)
    components = _platform_components(tmp_path, store, environment)
    invocation = _invocation(
        components, ssh_tool_name("lab"), {"command": "echo hello"}
    )

    with pytest.raises(ApprovalRequired) as pending:
        asyncio.run(components.broker.execute(invocation, components.scope))
    approval = store.update(
        Approval,
        pending.value.approval.id,
        {
            "status": ApprovalStatus.APPROVED,
            "decided_by": "operator",
            "decided_at": utc_now(),
        },
    )
    result = asyncio.run(
        components.broker.execute(invocation, components.scope, approval=approval)
    )

    assert result.exit_code == 0


def test_remote_command_timeout_stops_the_process(monkeypatch):
    _local_shell(monkeypatch)

    exit_code, _, _, timed_out = asyncio.run(
        environments.run_remote_command("lab", "sleep 5", cwd=None, timeout_seconds=1)
    )

    assert timed_out and exit_code is None


def _process_alive(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            state = handle.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


def test_cancelled_remote_command_stops_the_process(monkeypatch, tmp_path):
    import os
    import signal
    import time

    _local_shell(monkeypatch)
    marker = tmp_path / "pid"
    command = f"echo $$ > {shlex.quote(str(marker))}; exec sleep 30"

    async def scenario():
        task = asyncio.create_task(
            environments.run_remote_command(
                "lab", command, cwd=None, timeout_seconds=60
            )
        )
        while not marker.exists() or not marker.read_text().strip():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return int(marker.read_text())

    pid = asyncio.run(scenario())
    deadline = time.monotonic() + 2
    while _process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    if _process_alive(pid):
        os.killpg(pid, signal.SIGKILL)
        pytest.fail("the remote command kept running after the turn was cancelled")
