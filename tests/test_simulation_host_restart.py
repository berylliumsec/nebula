"""Process-level guards for the supervisor restart inside host activation.

These tests run the deployer's real activation, stop and readiness code
against real processes. Only systemd is emulated: a KillMode=process unit
without Restart= whose main process is a onefile-style bootloader. Like the
PyInstaller bootloader, it forwards SIGTERM to its child (the supervisor),
and a stop timeout SIGKILLs only the bootloader.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux process identities"
)

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts/simulation"
sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location(
    "simulation_host_restart_deployer", SCRIPT_DIR / "nebula_deployer.py"
)
assert spec is not None and spec.loader is not None
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)

OLD = "a" * 40
NEW = "b" * 40
API = "c" * 40

# A foreground Core under its onefile bootloader. The parent forwards SIGTERM
# to the child supervisor, waits for it and exits like it. In "drain" mode
# the supervisor never finishes its graceful stop, like a release without
# #1021 holding an open viewer stream.
BOOTLOADER = r"""
import os, signal, sys, time
ready, announce = os.pipe()
child = os.fork()
if child == 0:
    if sys.argv[1] == "drain":
        signal.signal(signal.SIGTERM, lambda *_: None)
    os.write(announce, b"1")
    while True:
        time.sleep(1)
os.read(ready, 1)


def forward(signum, _frame):
    try:
        os.kill(child, signum)
    except ProcessLookupError:
        pass


signal.signal(signal.SIGTERM, forward)
print(child, flush=True)
_, status = os.waitpid(child, 0)
if os.WIFSIGNALED(status):
    signal.signal(os.WTERMSIG(status), signal.SIG_DFL)
    os.kill(os.getpid(), os.WTERMSIG(status))
sys.exit(os.WEXITSTATUS(status))
"""


def alive(identity: dict) -> bool:
    return deployer.linux_process_birth(identity["pid"]) == identity["process_birth"]


def package(version: str) -> str:
    return "3.0.0+" + version[:8]


def write_discovery(data: Path, pid: int, version: str) -> None:
    runtime = data / "runtime"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    temporary = runtime / ".connection.json.new"
    temporary.write_text(
        json.dumps(
            {
                "protocol": "nebula-desktop-runtime-v1",
                "data_root": str(data.resolve()),
                "pid": pid,
                "process_birth": deployer.linux_process_birth(pid),
                "version": package(version),
                "endpoint": "http://127.0.0.1:18999/api/v1",
            }
        )
    )
    temporary.chmod(0o600)
    os.replace(temporary, runtime / "connection.json")


def status_for(owner_version: str = OLD) -> dict:
    return {
        "protocol": "nebula.runtime/v1",
        "health": "ready",
        "api_version": API,
        "resident_versions": [OLD],
        "staged_versions": [OLD, NEW],
        "projects": [
            {
                "project_id": "project-1",
                "runtime_id": "worker-1",
                "runtime_version": owner_version,
                "fencing_token": 7,
                "admission_held": False,
            }
        ],
    }


class Unit:
    """`systemctl --user` for a KillMode=process unit without Restart=."""

    def __init__(self, data: Path, *, stop_timeout: float) -> None:
        self.data = data
        self.stop_timeout = stop_timeout
        self.state = "inactive"
        self.bootloader: subprocess.Popen | None = None
        self.supervisor: dict | None = None
        self.modes: dict[str, str] = {}
        self.start_failures: dict[str, int] = {}
        self.events: list[str] = []
        self.processes: list[int] = []
        self.bootloaders: list[subprocess.Popen] = []

    def __call__(self, action: str) -> None:
        self.events.append(action)
        getattr(self, action.replace("-", "_"))()

    def start(self) -> None:
        if self.state == "active":
            return
        version = deployer.current_release_version()
        if self.start_failures.get(version, 0):
            # The release exits during startup (a crash, or a listener probe
            # refused by TIME_WAIT): systemd leaves the unit failed.
            self.start_failures[version] -= 1
            self.state = "failed"
            return
        self.bootloader = subprocess.Popen(
            [sys.executable, "-c", BOOTLOADER, self.modes.get(version, "clean")],
            stdout=subprocess.PIPE,
            text=True,
        )
        child = int(self.bootloader.stdout.readline())
        self.bootloaders.append(self.bootloader)
        self.processes += [self.bootloader.pid, child]
        self.supervisor = {
            "pid": child,
            "process_birth": deployer.linux_process_birth(child),
            "version": version,
        }
        write_discovery(self.data, child, version)
        self.state = "active"

    def stop(self) -> None:
        if self.state != "active":
            return
        assert self.bootloader is not None
        self.bootloader.send_signal(signal.SIGTERM)
        try:
            code = self.bootloader.wait(self.stop_timeout)
        except subprocess.TimeoutExpired:
            # KillMode=process: the stop timeout kills only the main process.
            self.bootloader.kill()
            self.bootloader.wait()
            self.state = "failed"
            return
        self.state = "inactive" if code in {0, -signal.SIGTERM} else "failed"

    def reset_failed(self) -> None:
        if self.state == "failed":
            self.state = "inactive"

    def active(self) -> str:
        if self.state == "active" and self.bootloader.poll() is not None:
            self.state = "failed"
        return self.state

    def identity(self) -> dict:
        if self.active() != "active":
            raise RuntimeError("The supervisor Core service must be active")
        return {
            "kill_mode": "process",
            "main_pid": self.bootloader.pid,
            "main_process_birth": deployer.linux_process_birth(self.bootloader.pid),
        }

    def health(self) -> tuple[dict, dict]:
        if self.active() != "active":
            raise OSError("connection refused")
        version = self.supervisor["version"]
        return (
            {
                "status": "ok",
                "database": "ok",
                "commit": version,
                "recovery_version": package(version),
            },
            status_for(),
        )

    def cleanup(self) -> None:
        for pid in self.processes:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for bootloader in self.bootloaders:
            bootloader.wait()
            bootloader.stdout.close()


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    releases = tmp_path / "releases"
    for version in (OLD, NEW):
        (releases / version).mkdir(parents=True)
        (releases / version / "nebula-runtime.json").write_text(
            json.dumps({"version": version})
        )
    current = tmp_path / "current"
    current.symlink_to(releases / OLD)
    data = tmp_path / "state"
    monkeypatch.setattr(deployer, "RELEASES", releases)
    monkeypatch.setattr(deployer, "CURRENT", current)
    monkeypatch.setattr(deployer, "DATA", data)
    monkeypatch.setattr(deployer, "STATE", tmp_path / "deployer-state.json")
    unit = Unit(data, stop_timeout=6)
    monkeypatch.setattr(deployer, "service", unit)
    monkeypatch.setattr(deployer, "unit_state", lambda _name: unit.active())
    monkeypatch.setattr(deployer, "supervisor_service_identity", unit.identity)
    monkeypatch.setattr(deployer, "supervisor_service_configuration", lambda: {})
    monkeypatch.setattr(deployer, "read_supervisor_health", unit.health)
    monkeypatch.setattr(deployer, "confirm_api_read", lambda *_args: None)

    def require_process(_service, identity, version):
        if (
            identity["pid"] != unit.supervisor["pid"]
            or unit.supervisor["version"] != version
        ):
            raise RuntimeError("Discovered Core process is not the unit's supervisor")

    monkeypatch.setattr(deployer, "require_runtime_process_in_service", require_process)
    monkeypatch.setattr(deployer, "runtime_package_version", package)
    monkeypatch.setattr(
        deployer, "runtime_release", lambda version: (releases / version, "d" * 64)
    )
    monkeypatch.setattr(
        deployer, "require_stage_migration_receipts", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        deployer, "require_host_schema_compatibility", lambda *_args: None
    )
    # Bounds shortened for the test; the behavior under them is unchanged.
    monkeypatch.setattr(deployer, "SUPERVISOR_STOP_GRACE_SECONDS", 1, raising=False)
    monkeypatch.setattr(deployer, "HOST_RESTART_DELAY_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(deployer, "HOST_START_ATTEMPTS", 4, raising=False)
    monkeypatch.setattr(deployer, "HOST_QUIET_POLL_SECONDS", 0.05, raising=False)
    monkeypatch.setitem(
        deployer.wait_supervisor_host_ready.__kwdefaults__, "timeout", 5
    )
    monkeypatch.setattr(deployer, "api", lambda: QuietCore())
    unit("start")
    yield unit
    unit.cleanup()


class QuietCore:
    """Chat activity for one project: busy for `busy_reads`, then idle."""

    def __init__(self, busy_reads: int = 0, reads: list | None = None) -> None:
        self.busy_reads = busy_reads
        self.reads = reads if reads is not None else []

    def all_entities(self, path: str, **_parameters) -> list[dict]:
        assert path == "engagements"
        return [{"id": "project-1"}]

    def get(self, path: str, **parameters):
        assert path == "chat/session-activity", path
        self.reads.append(parameters["engagement_id"])
        busy = len(self.reads) <= self.busy_reads
        return [{"session_id": "chat-1", "state": "running" if busy else "idle"}]


def promotion_record() -> dict:
    record = {
        "deployment_mode": "supervisor",
        "pending": NEW,
        "active": API,
        "runtime_deployment": {
            "id": "deployment-1",
            "action": "promote",
            "version": NEW,
            "state": "running",
            "steps": {},
            "host_activation": {
                "version": NEW,
                "operation_id": "deployment-1:host-activation",
                "state": "requested",
            },
        },
    }
    deployer.atomic_json(deployer.STATE, record)
    return record


def test_failed_activation_restores_previous_host_when_discovery_names_the_stopped_supervisor(
    host: Unit,
) -> None:
    """The candidate never starts, so discovery still names the stopped old
    supervisor. The automatic code-only rollback must treat that as no running
    host and restore the previous release, not stop in needs_attention."""
    old_supervisor = dict(host.supervisor)
    host.start_failures[NEW] = 99
    record = promotion_record()

    with pytest.raises(RuntimeError, match="restored supervisor code to " + OLD):
        deployer.activate_supervisor_host(record, NEW)

    assert not alive(old_supervisor)
    assert record["host_activation"]["state"] == "rolled_back"
    assert record["host_activation"]["rollback"]["state"] == "complete"
    assert record["host_active"] == OLD
    assert deployer.current_release_version() == OLD
    assert host.active() == "active" and host.supervisor["version"] == OLD
    assert json.loads(deployer.STATE.read_text())["host_activation"]["state"] == (
        "rolled_back"
    )


def test_host_switch_bounds_a_supervisor_that_never_finishes_its_stop(
    host: Unit,
) -> None:
    """A release without #1021 waits forever for an open viewer stream. The
    unit's stop timeout would kill only the bootloader and leave the old
    supervisor running; the deployer kills that exact process after its grace."""
    host("stop")
    host.modes[OLD] = "drain"
    host("start")
    old_supervisor = dict(host.supervisor)
    journal = {"operation_id": "op-1", "version": NEW, "state": "prepared"}
    started = time.monotonic()

    readback = deployer._switch_supervisor_host(
        NEW,
        release=deployer.RELEASES / NEW,
        expected_api_version=API,
        prior_status=deployer.host_status_snapshot(status_for()),
        journal=journal,
        record={"deployment_mode": "supervisor"},
    )

    assert time.monotonic() - started < host.stop_timeout
    assert not alive(old_supervisor)
    assert journal["service_stop"]["forced"] is True
    assert journal["state"] == "complete"
    assert readback["version"] == NEW
    assert host.supervisor["version"] == NEW and host.active() == "active"


def test_host_switch_frees_an_orphan_left_by_a_stop_timeout(host: Unit) -> None:
    """A stop that already timed out left the unit failed and the supervisor
    child running; the next switch kills it before starting a new host."""
    host("stop")
    host.modes[OLD] = "drain"
    host("start")
    orphan = dict(host.supervisor)
    host.stop_timeout = 0.5
    host("stop")
    assert host.active() == "failed" and alive(orphan)
    journal = {"operation_id": "op-1", "version": NEW, "state": "prepared"}

    deployer._switch_supervisor_host(
        NEW,
        release=deployer.RELEASES / NEW,
        expected_api_version=API,
        prior_status=deployer.host_status_snapshot(status_for()),
        journal=journal,
        record={"deployment_mode": "supervisor"},
    )

    assert not alive(orphan)
    assert journal["service_stop"]["forced"] is True
    assert journal["service_stop"]["reset_failed"] is True
    assert host.supervisor["version"] == NEW and host.active() == "active"


def test_host_switch_starts_again_a_release_that_exits_before_ready(
    host: Unit,
) -> None:
    """Releases without #1023 exit when TIME_WAIT connections hold the port;
    without systemd Restart= the unit would stay down for the whole wait."""
    host.start_failures[NEW] = 2
    journal = {"operation_id": "op-1", "version": NEW, "state": "prepared"}

    readback = deployer._switch_supervisor_host(
        NEW,
        release=deployer.RELEASES / NEW,
        expected_api_version=API,
        prior_status=deployer.host_status_snapshot(status_for()),
        journal=journal,
        record={"deployment_mode": "supervisor"},
    )

    assert readback["host_starts"] == 3
    assert host.events.count("reset-failed") == 2
    assert host.supervisor["version"] == NEW and host.active() == "active"


def test_rollback_host_resumes_a_restore_left_with_no_supervisor(
    host: Unit,
) -> None:
    """needs_attention after an interrupted code-only restore, with the unit
    failed and discovery naming an exited process: rollback-host restores the
    previous host instead of refusing on the failed unit."""
    host("stop")
    deployer.switch_to(deployer.RELEASES / NEW)
    host.state = "failed"
    prior = deployer.host_status_snapshot(status_for())
    record = {
        "deployment_mode": "supervisor",
        "host_activation": {
            "operation_id": "op-1",
            "state": "needs_attention",
            "version": NEW,
            "previous_version": OLD,
            "prior_status": prior,
            "rollback": {
                "operation_id": "op-1:rollback",
                "state": "service_started",
                "version": OLD,
                "previous_version": NEW,
                "expected_api_version": API,
                "prior_status": prior,
                "code_only": True,
            },
        },
    }
    deployer.atomic_json(deployer.STATE, record)

    result = deployer.rollback_host()

    assert result["status"] == "host_rolled_back"
    assert result["version"] == OLD
    assert deployer.current_release_version() == OLD
    assert host.supervisor["version"] == OLD and host.active() == "active"


def test_activation_stops_the_host_only_after_chat_turns_settle(
    host: Unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn queued or admitting in a worker without #1023 fails while no host
    runs, so activation waits for chat work before it stops the supervisor."""
    reads: list[str] = []
    monkeypatch.setattr(deployer, "api", lambda: QuietCore(busy_reads=3, reads=reads))
    stopped_after: list[int] = []
    original_stop = host.stop

    def stop() -> None:
        stopped_after.append(len(reads))
        original_stop()

    host.stop = stop
    record = promotion_record()

    result = deployer.activate_supervisor_host(record, NEW)

    assert result["status"] == "host_activated"
    assert stopped_after == [4]
    assert record["host_activation"]["restart_boundary"]["quiet_after_seconds"] >= 0


def test_activation_does_not_stop_the_host_while_chat_turns_stay_active(
    host: Unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deployer, "api", lambda: QuietCore(busy_reads=10_000))
    monkeypatch.setattr(deployer, "HOST_QUIET_WAIT_SECONDS", 0.3, raising=False)
    old_supervisor = dict(host.supervisor)
    record = promotion_record()

    with pytest.raises(RuntimeError, match="host restart boundary"):
        deployer.activate_supervisor_host(record, NEW)

    assert "stop" not in host.events
    assert alive(old_supervisor)
    assert "host_activation" not in record
    assert deployer.current_release_version() == OLD
