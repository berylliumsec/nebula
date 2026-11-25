"""Butler supervisor read-only probes, blocker verification and gated tmp cleanup.

Network probes run against a fake executor (no host or network is contacted);
the cleanup scan runs its real shell script on temporary directories.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.v3.async_test_support import anyio_backend as anyio_backend

from nebula.v3 import butler_host_probes as probes_module
from nebula.v3.butler_host_ops import ButlerHostOps
from nebula.v3.butler_host_probes import (
    ButlerHostProbes,
    ProbeConfig,
    load_probe_config,
    parse_df,
    parse_lsof_listen,
    parse_mem,
    parse_scutil_nc,
    parse_tls_chain,
)
from nebula.v3.butler_supervisor_policy import (
    GuardrailViolation,
    SupervisorDisabled,
    load_policy,
)


HOSTS = json.dumps(
    {
        "core": {"local": True},
        "responder": {"address": "192.0.2.231", "user": "lab"},
        "mac": {"address": "192.0.2.40"},
    }
)
MIRROR = "/srv/lab/repo/mirror"

DF = (
    "Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
    "/dev/sda1        100000000  99000000   1000000      99% /\n"
)
MEMINFO = "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n"
VM_STAT = (
    "hw.memsize: 17179869184\n"
    "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
    "Pages free:                               1000.\n"
    "Pages active:                             5000.\n"
    "Pages inactive:                           2000.\n"
    "Pages speculative:                         500.\n"
    "Pages purgeable:                           100.\n"
)
SCUTIL = (
    "Available network connection services in the current set (*=enabled):\n"
    "* (Disconnected)   0A1B2C3D-0000-4000-8000-00000000AAAA IPSec              "
    '"Lab VPN"                        [IPSec]\n'
)
LSOF = (
    "COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
    "python3  4242 lab    5u  IPv4  12345      0t0  TCP *:8443 (LISTEN)\n"
)
TLS = (
    "CONNECTED(00000003)\n"
    "---\nCertificate chain\n"
    " 0 s:CN = service.example\n"
    "   i:C = XX, O = Example Interceptor, CN = Proxy CA\n"
    "-----BEGIN CERTIFICATE-----\n" + "A" * 64 + "\n-----END CERTIFICATE-----\n"
    " 1 s:C = XX, O = Example Interceptor, CN = Proxy CA\n"
    "   i:C = XX, O = Example Interceptor, CN = Proxy CA\n"
    "---\n    Verify return code: 19 (self-signed certificate in certificate chain)\n"
)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "NEBULA_BUTLER_SUPERVISOR_ENABLED": "1",
        "NEBULA_BUTLER_SUPERVISOR_HOSTS": HOSTS,
        "NEBULA_BUTLER_SUPERVISOR_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "NEBULA_BUTLER_SUPERVISOR_PROTECTED_PATHS": MIRROR,
        "NEBULA_BUTLER_SUPERVISOR_PRIVATE_STRINGS": "operator@example.test",
        "NEBULA_BUTLER_SUPERVISOR_EXTERNAL_HOSTS": "github.com,service.example",
    }
    env.update(extra)
    return env


def _audit(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


class FakeExec:
    """Stand-in for ButlerHostOps._exec: records argv, returns canned output."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[tuple[str, str, int]] = []

    def respond(self, contains: str, stdout: str, exit_code: int = 0) -> None:
        self.responses.append((contains, stdout, exit_code))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        async def _exec(
            ops: ButlerHostOps,
            policy: Any,
            *,
            tool: str,
            host: Any,
            argv: list[str],
            display: str,
            timeout: int,
            cwd: str | None,
            reason: str,
        ) -> dict[str, Any]:
            fake.calls.append({"tool": tool, "host": host.name, "argv": argv})
            stdout, code = "", 0
            for contains, out, exit_code in fake.responses:
                if contains in display:
                    stdout, code = out, exit_code
                    break
            entry = await ops._record(
                policy,
                {
                    "tool": tool,
                    "host": host.name,
                    "command": display,
                    "outcome": "completed",
                    "exit_code": code,
                },
            )
            return {
                "host": host.name,
                "exit_code": code,
                "timed_out": False,
                "duration_seconds": 0.0,
                "stdout": stdout,
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "audit": {"at": entry["at"]},
            }

        monkeypatch.setattr(ButlerHostOps, "_exec", _exec)


def _probes(
    tmp_path: Path,
    config: ProbeConfig | None = None,
    clock: Callable[[], float] = time.time,
    **extra: str,
) -> ButlerHostProbes:
    env = _env(tmp_path, **extra)
    return ButlerHostProbes(
        project_id="p1",
        actor="case-1",
        policy_loader=lambda: load_policy(env),
        config_loader=lambda: config or ProbeConfig(),
        clock=clock,
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeExec:
    runner = FakeExec()
    runner.install(monkeypatch)
    return runner


@pytest.fixture(autouse=True)
def _fresh_tokens() -> None:
    probes_module._reset_cleanup_tokens()


# --- parsers ---------------------------------------------------------------


def test_parsers_read_tool_output() -> None:
    [disk] = parse_df(DF)
    assert disk["free_bytes"] == 1_000_000 * 1024
    assert disk["percent_used"] == 99
    assert disk["mount"] == "/"
    assert parse_mem(MEMINFO)["available_bytes"] == 8_192_000 * 1024
    mac = parse_mem(VM_STAT)
    assert mac["available_bytes"] == (1000 + 2000 + 500 + 100) * 16384
    assert mac["total_bytes"] == 17179869184
    [vpn] = parse_scutil_nc(SCUTIL)
    assert vpn["name"] == "Lab VPN" and vpn["status"] == "Disconnected"
    [listener] = parse_lsof_listen(LSOF)
    assert listener["pid"] == 4242 and listener["port"] == 8443
    chain = parse_tls_chain(TLS)
    assert [c["depth"] for c in chain["chain"]] == [0, 1]
    assert "Interceptor" in chain["chain"][0]["issuer"]
    assert chain["verify_code"] == 19


def test_probe_config_reads_environment() -> None:
    config = load_probe_config(
        {
            "NEBULA_BUTLER_SUPERVISOR_CLEANUP_ROOTS": "/tmp/a, /tmp/b",
            "NEBULA_BUTLER_SUPERVISOR_CLEANUP_IDLE_MINUTES": "5",
        }
    )
    assert config.cleanup_roots == ("/tmp/a", "/tmp/b")
    assert config.cleanup_idle_minutes == 5
    assert config.cleanup_min_bytes == 2 * 1024**3
    assert load_probe_config({}).cleanup_roots == ()
    assert load_probe_config({}).lease_state_command is None


# --- probes ----------------------------------------------------------------


@pytest.mark.anyio
async def test_disk_free_runs_fixed_template_and_audits(
    tmp_path: Path, fake: FakeExec
) -> None:
    fake.respond("df -Pk", DF)
    result = await _probes(tmp_path).probe(
        {"kind": "disk_free", "host": "responder", "path": "/var/tmp"}
    )
    assert result["disk"]["percent_used"] == 99
    [call] = fake.calls
    assert call["host"] == "responder"
    assert call["argv"][0] == "ssh" and call["argv"][-1] == "df -Pk -- /var/tmp"
    tools = [entry["tool"] for entry in _audit(tmp_path)]
    assert tools == ["probe.disk_free", "probe"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {"kind": "disk_free", "path": "/tmp; rm -rf /"},
        {"kind": "disk_free", "path": "/tmp/../etc"},
        {"kind": "disk_free", "path": "relative"},
        {"kind": "disk_free", "host": "elsewhere"},
        {"kind": "tcp_reachable", "target": "203.0.113.9", "port": 22},
        {"kind": "tcp_reachable", "target": "responder", "port": 0},
        {"kind": "http_status", "url": "http://203.0.113.9/"},
        {"kind": "http_status", "url": "https://service.example/$(id)"},
        {"kind": "http_status", "url": "https://service.example/", "method": "POST"},
        {"kind": "tls_chain", "target": "evil.example.org"},
        {"kind": "tls_chain", "target": "service.example", "servername": "a b"},
        {"kind": "lsof_port", "host": "core"},
        {"kind": "lease_state"},
        {"kind": "verify_blocker", "claim_kind": "unknown"},
        {"kind": "shell"},
    ],
)
async def test_invalid_or_non_lab_probes_are_refused_audited_and_never_run(
    tmp_path: Path, fake: FakeExec, arguments: dict[str, Any]
) -> None:
    with pytest.raises(GuardrailViolation):
        await _probes(tmp_path).probe(arguments)
    assert fake.calls == []
    [entry] = _audit(tmp_path)
    assert entry["outcome"] == "refused"
    assert entry["tool"] == "probe"


@pytest.mark.anyio
async def test_kill_switch_stops_probes_and_cleanup(
    tmp_path: Path, fake: FakeExec
) -> None:
    probes = _probes(tmp_path, NEBULA_BUTLER_SUPERVISOR_ENABLED="0")
    with pytest.raises(SupervisorDisabled):
        await probes.probe({"kind": "mem_available"})
    with pytest.raises(SupervisorDisabled):
        await probes.tmp_cleanup({"reason": "off"})
    assert fake.calls == []


@pytest.mark.anyio
async def test_tcp_reachable_resolves_lab_names_only(
    tmp_path: Path, fake: FakeExec
) -> None:
    fake.respond("nc -z", "", 1)
    result = await _probes(tmp_path).probe(
        {"kind": "tcp_reachable", "target": "responder", "port": 22}
    )
    assert result["target"] == "192.0.2.231"
    assert result["reachable"] is False
    assert fake.calls[0]["argv"][-1] == "nc -z -w 5 192.0.2.231 22"


@pytest.mark.anyio
async def test_http_status_returns_status_only(tmp_path: Path, fake: FakeExec) -> None:
    fake.respond("curl", "204")
    result = await _probes(tmp_path).probe(
        {"kind": "http_status", "url": "https://service.example/health"}
    )
    assert result["status_code"] == 204
    command = fake.calls[0]["argv"][-1]
    assert "-I" in command and "-o /dev/null" in command
    assert " -d " not in command and "--data" not in command


@pytest.mark.anyio
async def test_mem_available_handles_both_platforms(
    tmp_path: Path, fake: FakeExec
) -> None:
    fake.respond("meminfo", VM_STAT)
    result = await _probes(tmp_path).probe({"kind": "mem_available", "host": "mac"})
    assert result["memory"]["method"].startswith("vm_stat")


@pytest.mark.anyio
async def test_listening_ports_and_empty_lsof(tmp_path: Path, fake: FakeExec) -> None:
    fake.respond("-iTCP:8443", LSOF)
    fake.respond("-iTCP:9", "", 1)
    probes = _probes(tmp_path)
    found = await probes.probe({"kind": "lsof_port", "port": 8443})
    assert found["listening"] is True and found["listeners"][0]["pid"] == 4242
    empty = await probes.probe({"kind": "lsof_port", "port": 9})
    assert empty["ok"] is True and empty["listening"] is False


@pytest.mark.anyio
async def test_lease_state_runs_the_configured_command(
    tmp_path: Path, fake: FakeExec
) -> None:
    fake.respond("lease-status", '{"holder": null}')
    config = ProbeConfig(lease_state_command="/opt/lab/bin/lease-status --json")
    result = await _probes(tmp_path, config).probe({"kind": "lease_state"})
    assert result["state"] == {"holder": None}
    assert fake.calls[0]["host"] == "core"


@pytest.mark.anyio
async def test_host_health_combines_disk_and_memory(
    tmp_path: Path, fake: FakeExec
) -> None:
    fake.respond("df -Pk", DF)
    fake.respond("meminfo", MEMINFO)
    config = ProbeConfig(health_paths=("/", "/var/tmp"))
    result = await _probes(tmp_path, config).probe({"kind": "host_health"})
    assert [d["path"] for d in result["disks"]] == ["/", "/var/tmp"]
    assert result["memory"]["available_bytes"] == 8_192_000 * 1024


@pytest.mark.anyio
async def test_raw_output_is_redacted(tmp_path: Path) -> None:
    # Real local execution (no network): the private string is assembled at run time.
    config = ProbeConfig(lease_state_command="printf 'owner operator@example.tes%s' t")
    result = await _probes(tmp_path, config).probe({"kind": "lease_state"})
    assert result["raw"]["exit_code"] == 0
    assert "operator@example.test" not in result["raw"]["stdout"]
    assert "[redacted]" in result["raw"]["stdout"]


# --- blocker verification --------------------------------------------------


@pytest.mark.anyio
async def test_verify_blocker_maps_claims_to_probes(
    tmp_path: Path, fake: FakeExec
) -> None:
    fake.respond("scutil", SCUTIL)
    fake.respond("df -Pk", DF)
    fake.respond("nc -z", "", 0)
    fake.respond("curl", "000", 7)
    fake.respond("openssl", TLS)
    probes = _probes(tmp_path)

    async def verify(**arguments: Any) -> dict[str, Any]:
        return await probes.probe({"kind": "verify_blocker", **arguments})

    vpn = await verify(claim_kind="no_vpn_config", host="mac")
    assert vpn["probe"] == "vpn_configs" and vpn["claim_supported"] is False
    named = await verify(claim_kind="no_vpn_config", host="mac", name_substring="Other")
    assert named["claim_supported"] is True
    disk = await verify(claim_kind="disk_full", path="/")
    assert disk["claim_supported"] is True
    host = await verify(claim_kind="host_unreachable", target="responder", port=22)
    assert host["claim_supported"] is False
    endpoint = await verify(
        claim_kind="endpoint_unreachable", url="https://service.example/"
    )
    assert endpoint["claim_supported"] is True
    tls = await verify(
        claim_kind="tls_intercepted",
        target="service.example",
        expected_issuer_substring="Public Root",
    )
    assert tls["claim_supported"] is True
    assert "A" * 64 not in tls["evidence"]["raw"]["stdout"]
    unknown = await verify(claim_kind="tls_intercepted", target="github.com")
    fake.responses.clear()
    assert unknown["claim_supported"] in {True, False, None}
    summaries = [e for e in _audit(tmp_path) if e["tool"] == "probe"]
    assert len(summaries) == 7
    assert "claim_supported" in summaries[0]["detail"]


# --- tmp cleanup (real scan script on temp directories) --------------------


def _age(path: Path, seconds: int = 3_600) -> None:
    stamp = time.time() - seconds
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.utime(os.path.join(root, name), (stamp, stamp), follow_symlinks=False)
    os.utime(path, (stamp, stamp))


def _cleanup_tree(tmp_path: Path) -> Path:
    root = tmp_path / "exports"
    (root / "old-export" / ".git").mkdir(parents=True)
    (root / "old-export" / "README").write_text("x")
    (root / "busy-export" / ".git").mkdir(parents=True)
    (root / "small-dir").mkdir()
    (root / "small-dir" / "file").write_text("x")
    elsewhere = tmp_path / "elsewhere" / ".git"
    elsewhere.mkdir(parents=True)
    (root / "linked").symlink_to(tmp_path / "elsewhere")
    _age(root)
    _age(tmp_path / "elsewhere")
    (root / "busy-export" / "new-file").write_text("fresh")
    return root


def _cleanup_config(root: Path, **overrides: Any) -> ProbeConfig:
    values: dict[str, Any] = {
        "cleanup_roots": (str(root),),
        "cleanup_idle_minutes": 20,
        "cleanup_min_bytes": 2 * 1024**3,
    }
    values.update(overrides)
    return ProbeConfig(**values)


@pytest.mark.anyio
async def test_cleanup_refuses_without_configured_roots(tmp_path: Path) -> None:
    with pytest.raises(GuardrailViolation) as caught:
        await _probes(tmp_path).tmp_cleanup({"reason": "clean"})
    assert caught.value.rule == "not_configured"
    assert _audit(tmp_path)[0]["outcome"] == "refused"


@pytest.mark.anyio
@pytest.mark.parametrize("root", ["/", "/home", MIRROR, "/srv/lab"])
async def test_cleanup_refuses_broad_or_protected_roots(
    tmp_path: Path, root: str
) -> None:
    probes = _probes(tmp_path, ProbeConfig(cleanup_roots=(root,)))
    with pytest.raises(GuardrailViolation):
        await probes.tmp_cleanup({"reason": "clean"})


@pytest.mark.anyio
async def test_cleanup_dry_run_then_delete_with_token(tmp_path: Path) -> None:
    root = _cleanup_tree(tmp_path)
    probes = _probes(tmp_path, _cleanup_config(root))
    dry = await probes.tmp_cleanup({"reason": "free disk"})
    assert dry["dry_run"] is True
    assert [c["path"] for c in dry["candidates"]] == [str(root / "old-export")]
    skipped = {Path(s["path"]).name: s["skip_reason"] for s in dry["skipped"]}
    assert skipped["busy-export"].startswith("modified_within")
    assert skipped["small-dir"] == "not_a_repository_export"
    assert "linked" not in skipped  # symlinks are never listed or followed
    assert (root / "old-export").exists()  # dry run deletes nothing

    with pytest.raises(GuardrailViolation) as caught:
        await probes.tmp_cleanup(
            {
                "reason": "free disk",
                "dry_run": False,
                "token": dry["token"],
                "paths": [str(root / "busy-export")],
            }
        )
    assert caught.value.rule == "cleanup_path_not_candidate"

    done = await probes.tmp_cleanup(
        {
            "reason": "free disk",
            "dry_run": False,
            "token": dry["token"],
            "paths": [str(root / "old-export")],
        }
    )
    assert [d["path"] for d in done["deleted"]] == [str(root / "old-export")]
    assert not (root / "old-export").exists()
    assert (tmp_path / "elsewhere" / ".git").exists()
    outcomes = [e["outcome"] for e in _audit(tmp_path) if e["tool"] == "tmp_cleanup"]
    assert outcomes == ["dry_run", "refused", "deleted"]


@pytest.mark.anyio
async def test_cleanup_rechecks_idleness_at_delete_time(tmp_path: Path) -> None:
    root = _cleanup_tree(tmp_path)
    probes = _probes(tmp_path, _cleanup_config(root))
    dry = await probes.tmp_cleanup({"reason": "free disk"})
    (root / "old-export" / "touched").write_text("new activity")
    done = await probes.tmp_cleanup(
        {
            "reason": "free disk",
            "dry_run": False,
            "token": dry["token"],
            "paths": [str(root / "old-export")],
        }
    )
    assert done["deleted"] == []
    assert done["kept"][0]["reason"].startswith("modified_within")
    assert (root / "old-export").exists()


@pytest.mark.anyio
async def test_cleanup_skips_directories_a_process_holds_open(tmp_path: Path) -> None:
    root = _cleanup_tree(tmp_path)
    probes = _probes(tmp_path, _cleanup_config(root))
    with open(root / "old-export" / "README") as handle:
        dry = await probes.tmp_cleanup({"reason": "free disk"})
        assert handle.readable()
    reasons = {Path(s["path"]).name: s["skip_reason"] for s in dry["skipped"]}
    # Either lsof saw the open file, or it could not run here; never a candidate.
    assert reasons["old-export"] in {
        "held_open_by_a_process",
        "open_file_check_unavailable",
    }
    assert dry["candidates"] == []


@pytest.mark.anyio
async def test_cleanup_token_expires_after_fifteen_minutes(tmp_path: Path) -> None:
    root = _cleanup_tree(tmp_path)
    now = [time.time()]
    probes = _probes(tmp_path, _cleanup_config(root), clock=lambda: now[0])
    dry = await probes.tmp_cleanup({"reason": "free disk"})
    now[0] += 16 * 60
    with pytest.raises(GuardrailViolation) as caught:
        await probes.tmp_cleanup(
            {
                "reason": "free disk",
                "dry_run": False,
                "token": dry["token"],
                "paths": [str(root / "old-export")],
            }
        )
    assert caught.value.rule == "cleanup_token_expired"
    assert (root / "old-export").exists()


def test_classify_never_deletes_when_open_file_check_is_unavailable(
    tmp_path: Path,
) -> None:
    probes = _probes(tmp_path)
    policy = load_policy(_env(tmp_path))
    config = ProbeConfig(cleanup_roots=("/tmp/x",))
    row = {
        "path": "/tmp/x/export",
        "has_git": True,
        "bytes": 1,
        "recent": False,
        "held_open": None,
    }
    assert (
        probes._classify(policy, config, ["/tmp/x"], row)
        == "open_file_check_unavailable"
    )
    assert (
        probes._classify(policy, config, ["/tmp/x"], {**row, "held_open": False})
        is None
    )
    assert (
        probes._classify(policy, config, ["/tmp/x"], {**row, "path": "/tmp/x/a/b"})
        == "unsafe_name_or_outside_root"
    )


def test_actions_register_two_operations(tmp_path: Path) -> None:
    names = [action.spec.name for action in _probes(tmp_path).actions()]
    assert names == ["nebula.supervisor.probe", "nebula.supervisor.tmp_cleanup"]
