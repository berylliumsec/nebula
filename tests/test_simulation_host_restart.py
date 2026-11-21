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


@pytest.fixture(autouse=True)
def _core_without_project_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """These fake Cores predate project holds (#1232): the bounded wait runs."""
    monkeypatch.setattr(deployer, "place_project_hold", lambda *_a, **_k: None)


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
    monkeypatch.setattr(deployer, "socket_activation", lambda: None)
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
        if path == "deployment/operations":
            return {"running": []}
        if path == "deployment/status":
            # The busy turn's owner is a live, verified worker.
            return {
                "projects": [
                    {
                        "project_id": "project-1",
                        "runtime_id": "worker-1",
                        "runtime_version": NEW,
                        "blockers": [],
                    }
                ],
                "processes": [
                    {"runtime_id": "worker-1", "state": "ready", "ready_verified": True}
                ],
            }
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


class UnavailableOwnerCore:
    """A project whose turn stays working while its owner cannot advance it.

    ``owners`` yields the project's owner per status read: each is
    (runtime_id, ready_verified, blockers). After ``idle_after`` activity
    reads the turn finishes.
    """

    def __init__(self, owners, idle_after: int | None = None) -> None:
        self.owners = list(owners)
        self.idle_after = idle_after
        self.status_reads = 0
        self.activity_reads = 0

    def all_entities(self, path: str, **_parameters) -> list[dict]:
        assert path == "engagements"
        return [{"id": "project-apple", "name": "Apple Bug Bounty"}]

    def _owner(self):
        return self.owners[min(self.status_reads, len(self.owners) - 1)]

    def get(self, path: str, **parameters):
        if path == "chat/session-activity":
            assert parameters["engagement_id"] == "project-apple"
            idle = (
                self.idle_after is not None and self.activity_reads >= self.idle_after
            )
            self.activity_reads += 1
            return [
                {
                    "session_id": "chat-1",
                    "state": "idle" if idle else "working",
                    "turn_id": None if idle else "turn-7",
                }
            ]
        assert path == "deployment/status", path
        runtime_id, verified, blockers = self._owner()
        self.status_reads += 1
        return {
            "projects": [
                {
                    "project_id": "project-apple",
                    "runtime_id": runtime_id,
                    "runtime_version": "3.0.0+old",
                    "blockers": blockers,
                }
            ],
            "processes": [
                {
                    "runtime_id": runtime_id,
                    "state": "ready",
                    "ready_verified": verified,
                }
            ],
        }


def _bounded_quiet_wait(monkeypatch: pytest.MonkeyPatch, core) -> None:
    monkeypatch.setattr(deployer, "api", lambda: core)
    monkeypatch.setattr(deployer, "HOST_QUIET_POLL_SECONDS", 0.02, raising=False)
    monkeypatch.setattr(deployer, "HOST_QUIET_WAIT_SECONDS", 30, raising=False)
    monkeypatch.setattr(deployer, "HOST_UNAVAILABLE_OWNER_SECONDS", 0.2, raising=False)


def test_restart_boundary_names_a_turn_held_by_an_unresponsive_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frozen owner keeps its process (Core recovers only exited ones) and
    its lapsed lease, so its turn never settles: refuse with the exact
    blocker instead of spending the whole quiet bound on every run."""
    core = UnavailableOwnerCore([("worker-frozen", False, [])])
    _bounded_quiet_wait(monkeypatch, core)
    started = time.monotonic()

    with pytest.raises(RuntimeError) as refused:
        deployer.wait_quiet_restart_boundary()

    assert time.monotonic() - started < 10
    message = str(refused.value)
    assert 'Project "Apple Bug Bounty" (project-apple)' in message
    assert "turn-7" in message
    assert "worker-frozen (version 3.0.0+old)" in message
    assert "heartbeat lease lapsed" in message
    assert "supervisor was not stopped" in message


def test_restart_boundary_names_a_turn_held_by_a_failed_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed application keeps renewing its lease, so Core never recovers
    the project; the refusal carries Core's own failure blocker."""
    failure = (
        "Execution worker worker-failed failed: boom; its cleanup is "
        "unverified, so it keeps the project's ownership"
    )
    core = UnavailableOwnerCore([("worker-failed", True, [failure])])
    _bounded_quiet_wait(monkeypatch, core)

    with pytest.raises(RuntimeError) as refused:
        deployer.wait_quiet_restart_boundary()

    assert failure in str(refused.value)
    assert "turn-7" in str(refused.value)


def test_restart_boundary_waits_out_an_owner_that_core_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead owner is reconciled and replaced by Core within its lease; the
    resumed turn then finishes, so activation proceeds."""
    core = UnavailableOwnerCore(
        [("worker-dead", False, [])] * 3 + [("worker-new", True, [])],
        idle_after=8,
    )
    _bounded_quiet_wait(monkeypatch, core)

    result = deployer.wait_quiet_restart_boundary()

    assert result["quiet_after_seconds"] >= 0
    assert core.activity_reads == 9


def test_restart_boundary_does_not_blame_a_pinned_live_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handoff blockers on a live owner are ordinary running work."""
    core = UnavailableOwnerCore(
        [("worker-live", True, ["1 resident handle(s) in harness require it"])]
    )
    _bounded_quiet_wait(monkeypatch, core)
    monkeypatch.setattr(deployer, "HOST_QUIET_WAIT_SECONDS", 0.4, raising=False)

    with pytest.raises(RuntimeError, match="stayed queued, admitting or running"):
        deployer.wait_quiet_restart_boundary()


class ReconnectingCore(QuietCore):
    """Busy chat work owned by workers whose versions are exact commits."""

    def __init__(self, versions: dict[str, str], busy_reads: int = 10_000) -> None:
        super().__init__(busy_reads=busy_reads)
        self.versions = versions

    def get(self, path: str, **parameters):
        if path == "deployment/status":
            status = super().get(path, **parameters)
            status["api_version"] = self.versions["api"]
            status["resident_versions"] = [self.versions["worker"]]
            status["projects"][0]["runtime_version"] = self.versions["worker"]
            return status
        return super().get(path, **parameters)


def test_activation_restarts_under_running_turns_when_every_worker_reconnects(
    host: Unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workers with #1023 wait out the host replacement, so a project that
    never goes idle no longer holds the deployment."""
    core = ReconnectingCore({"api": API, "worker": API})
    monkeypatch.setattr(deployer, "api", lambda: core)
    monkeypatch.setattr(deployer, "_includes_host_reconnect", lambda _version: True)
    monkeypatch.setattr(deployer, "HOST_QUIET_WAIT_SECONDS", 0.3, raising=False)
    record = promotion_record()

    result = deployer.activate_supervisor_host(record, NEW)

    assert result["status"] == "host_activated"
    boundary = record["host_activation"]["restart_boundary"]
    assert boundary["waited"] is False
    assert boundary["reconnecting_versions"] == [API]
    assert core.reads == []


def test_activation_still_waits_when_one_worker_predates_host_reconnect(
    host: Unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_worker = "c" * 40
    core = ReconnectingCore({"api": API, "worker": old_worker})
    monkeypatch.setattr(deployer, "api", lambda: core)
    monkeypatch.setattr(
        deployer, "_includes_host_reconnect", lambda version: version != old_worker
    )
    monkeypatch.setattr(deployer, "HOST_QUIET_WAIT_SECONDS", 0.3, raising=False)
    old_supervisor = dict(host.supervisor)
    record = promotion_record()

    with pytest.raises(RuntimeError, match="host restart boundary"):
        deployer.activate_supervisor_host(record, NEW)

    assert "stop" not in host.events
    assert alive(old_supervisor)


def test_host_reconnect_support_follows_commit_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def git(*argv: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *argv],
            check=True, capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        ).stdout.strip()

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "before")
    before = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "reconnect")
    reconnect = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "after")
    after = git("rev-parse", "HEAD")
    monkeypatch.setattr(deployer, "REPO", tmp_path)
    monkeypatch.setattr(deployer, "HOST_RECONNECT_COMMIT", reconnect)

    assert deployer._includes_host_reconnect(after)
    assert deployer._includes_host_reconnect(reconnect)
    assert not deployer._includes_host_reconnect(before)
    assert not deployer._includes_host_reconnect("d" * 40)


class HoldingCore(QuietCore):
    """A Core with project holds: a held project's turn stops after a few reads."""

    def __init__(self) -> None:
        super().__init__(busy_reads=10_000)
        self.holds: dict[str, dict] = {}
        self.released: list[str] = []
        self.held_reads = 0

    def get(self, path: str, **parameters):
        if path == "chat/session-activity" and self.holds:
            self.held_reads += 1
            if self.held_reads > 2:
                # Core stopped the held project's turn after its grace.
                return [{"session_id": "chat-1", "state": "idle"}]
        return super().get(path, **parameters)


def _holding(monkeypatch: pytest.MonkeyPatch, core: HoldingCore) -> None:
    monkeypatch.setattr(deployer, "api", lambda: core)
    monkeypatch.setattr(deployer, "PROJECT_HOLD_POLL_SECONDS", 0.01, raising=False)

    def write(path: str, payload: dict, *, idempotency_key: str):
        parts = path.split("/")
        if path.endswith("/release"):
            core.released.append(parts[3])
            return {"hold": {"id": parts[3], "status": "released", "restored_goals": []}}
        hold_id = f"hold-{parts[1]}"
        core.holds[parts[1]] = {**payload, "key": idempotency_key}
        return {"hold": {"id": hold_id, "status": "holding"}}

    monkeypatch.setattr(deployer, "core_write", write)
    monkeypatch.setattr(
        deployer,
        "place_project_hold",
        _real_place_project_hold,
    )


_real_place_project_hold = deployer.place_project_hold


def test_restart_boundary_holds_busy_projects_and_activation_releases_them(
    host: Unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older workers still need a quiet boundary; Core's hold makes one
    instead of waiting for the project to settle on its own."""
    core = HoldingCore()
    _holding(monkeypatch, core)
    record = promotion_record()

    result = deployer.activate_supervisor_host(record, NEW)

    assert result["status"] == "host_activated"
    boundary = record["host_activation"]["restart_boundary"]
    assert boundary["holds"] == {"project-1": "hold-project-1"}
    placed = core.holds["project-1"]
    assert placed["deployment_id"] == f"host-restart-{NEW}"
    assert placed["key"] == f"project-hold:host-restart-{NEW}:project-1"
    # Released once the new host is up, and no longer saved.
    assert core.released == ["hold-project-1"]
    assert result["restart_holds"]["released"]["project-1"]["status"] == "released"
    assert deployer._saved_holds() == {}


def test_restart_holds_are_released_when_activation_fails(
    host: Unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = HoldingCore()
    _holding(monkeypatch, core)

    def fail(*_args, **_kwargs):
        raise RuntimeError("switch failed")

    monkeypatch.setattr(deployer, "_switch_supervisor_host", fail)

    with pytest.raises(RuntimeError, match="switch failed"):
        deployer.activate_supervisor_host(promotion_record(), NEW)

    assert core.released == ["hold-project-1"]
    assert deployer._saved_holds() == {}


def test_restart_release_keeps_an_operators_stop_holds(
    host: Unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = HoldingCore()
    _holding(monkeypatch, core)
    deployer._save_holds(
        {
            "project-9": {
                "hold_id": "hold-operator",
                "deployment_id": "operator-stop-20261120T000000Z",
            }
        }
    )

    deployer.activate_supervisor_host(promotion_record(), NEW)

    assert "hold-operator" not in core.released
    assert set(deployer._saved_holds()) == {"project-9"}


def test_unrouted_hold_falls_back_to_the_bounded_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    from urllib.error import HTTPError

    def missing(*_args, **_kwargs):
        raise HTTPError("u", 404, "Not Found", {}, io.BytesIO(b'{"detail":"Not Found"}'))

    monkeypatch.setattr(deployer, "core_write", missing)
    assert (
        _real_place_project_hold(
            "project-1", deployment_id="d", reason="r", grace_seconds=0
        )
        is None
    )

    def unknown(*_args, **_kwargs):
        raise HTTPError(
            "u", 404, "Not Found", {},
            io.BytesIO(b'{"detail":"engagements entity not found: project-1"}'),
        )

    monkeypatch.setattr(deployer, "core_write", unknown)
    with pytest.raises(HTTPError):
        _real_place_project_hold(
            "project-1", deployment_id="d", reason="r", grace_seconds=0
        )


def test_operator_hold_covers_live_projects_and_skips_host_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-Core stop holds every project with a worker, not idle ones."""
    monkeypatch.setattr(deployer, "STATE", tmp_path / "deployer-state.json")

    class StopCore(HoldingCore):
        def all_entities(self, path: str, **_parameters) -> list[dict]:
            return [{"id": "project-1"}, {"id": "project-idle"}]

        def get(self, path: str, **parameters):
            if path == "deployment/status":
                return {
                    "projects": [
                        {"project_id": "project-1", "runtime_id": "worker-1"},
                        {"project_id": "project-idle", "runtime_id": None},
                        {"project_id": "__nebula_host_work__", "runtime_id": "host"},
                    ]
                }
            if path == "chat/session-activity":
                return []
            return super().get(path, **parameters)

    core = StopCore()
    _holding(monkeypatch, core)

    result = deployer.hold_command(None, "Whole-Core stop", 60)

    assert result["status"] == "held"
    assert result["holds"] == {"project-1": "hold-project-1"}
    assert core.holds["project-1"]["grace_seconds"] == 60
    assert core.holds["project-1"]["deployment_id"].startswith("operator-stop-")
    assert set(deployer._saved_holds()) == {"project-1"}

    released = deployer.release_project_holds(reason="Core is back")
    assert released["released"]["project-1"]["status"] == "released"
    assert deployer._saved_holds() == {}


class HeldListener:
    """`nebula-core.socket` in front of the emulated unit.

    While the socket listens and the service is stopped, a queued connection
    starts the service from `current` at once, as systemd socket activation
    does. A start that fails trips the start limit and fails the socket too.
    """

    def __init__(self, unit: Unit) -> None:
        self.unit = unit
        self.state = "active"
        self.events: list[str] = []
        self.started_versions: list[str] = []
        unit_stop, unit_start = unit.stop, unit.start

        def stop() -> None:
            unit_stop()
            if self.state == "active" and unit.state == "inactive":
                start()

        def start() -> None:
            was_active = unit.state == "active"
            unit_start()
            if unit.state == "active" and not was_active:
                self.started_versions.append(unit.supervisor["version"])
            elif unit.state == "failed" and self.fail_on_start_failure:
                self.state = "failed"

        self.fail_on_start_failure = False
        unit.stop, unit.start = stop, start

    def state_of(self, name: str) -> str:
        return self.state if name == deployer.SOCKET else self.unit.active()

    def __call__(self, action: str) -> None:
        self.events.append(action)
        if action == "reset-failed" and self.state == "failed":
            self.state = "inactive"
        elif action == "start":
            self.state = "active"


@pytest.fixture
def held(host: Unit, monkeypatch: pytest.MonkeyPatch) -> HeldListener:
    listener = HeldListener(host)
    monkeypatch.setattr(
        deployer,
        "socket_activation",
        lambda: {"socket": deployer.SOCKET, "listen": deployer.SOCKET_LISTEN},
    )
    monkeypatch.setattr(deployer, "unit_state", listener.state_of)
    monkeypatch.setattr(deployer, "socket_unit", listener)
    monkeypatch.setattr(deployer, "unit_property", lambda *_args: "")
    return listener


def switch(journal: dict | None = None) -> tuple[dict, dict]:
    journal = journal or {"operation_id": "op-1", "version": NEW, "state": "prepared"}
    readback = deployer._switch_supervisor_host(
        NEW,
        release=deployer.RELEASES / NEW,
        expected_api_version=API,
        prior_status=deployer.host_status_snapshot(status_for()),
        journal=journal,
        record={"deployment_mode": "supervisor"},
    )
    return readback, journal


def test_held_listener_starts_only_the_selected_release_after_the_stop(
    host: Unit, held: HeldListener
) -> None:
    """A connection queued on the held socket starts the stopped service at
    once. The release is selected before the stop, so that start is the new
    host, never the old one again."""
    old = dict(host.supervisor)

    readback, journal = switch()

    assert not alive(old)
    assert held.started_versions == [NEW]
    assert readback["version"] == NEW and readback["socket_listening"] is True
    assert journal["state"] == "complete"
    assert journal["socket_activation"]["socket"] == deployer.SOCKET
    assert host.supervisor["version"] == NEW and held.state == "active"


def test_held_listener_is_restored_after_failed_starts_trip_its_limit(
    host: Unit, held: HeldListener
) -> None:
    """A release that exits during startup fails the socket with the start
    limit; connections would be refused even after a later start succeeds."""
    held.fail_on_start_failure = True
    host.start_failures[NEW] = 2

    readback, _journal = switch()

    assert readback["version"] == NEW and readback["socket_listening"] is True
    assert held.events.count("reset-failed") == 2
    assert held.state == "active" and host.supervisor["version"] == NEW


def test_unconfigured_socket_keeps_the_stop_then_select_order(host: Unit) -> None:
    """Without the socket unit nothing can start the service mid-switch, and
    a stopped writer is still required before the release changes."""
    old = dict(host.supervisor)

    readback, journal = switch()

    assert not alive(old)
    assert journal["socket_activation"] is None and "socket" not in journal
    assert "socket_listening" not in readback
    assert host.supervisor["version"] == NEW


class BusyOperations(QuietCore):
    """A Core whose supervisor runs a deployment operation for `busy` reads."""

    def __init__(self, busy: int, *, listing: bool = True, status: int = 503) -> None:
        super().__init__()
        self.busy, self.listing, self.operation_reads = busy, listing, 0
        self.status = status

    def get(self, path: str, **parameters):
        if path != "deployment/operations":
            return super().get(path, **parameters)
        if not self.listing:
            raise deployer.HTTPError(path, self.status, "Refused", {}, None)
        self.operation_reads += 1
        running = self.operation_reads <= self.busy
        return {
            "running": [{"operation_id": "stage-1", "action": "stage"}]
            if running
            else []
        }


def test_host_swap_waits_for_a_running_deployment_operation(
    host: Unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = BusyOperations(busy=3)
    monkeypatch.setattr(deployer, "api", lambda: core)
    monkeypatch.setattr(deployer, "HOST_OPERATION_POLL_SECONDS", 0.01)
    record = promotion_record()

    result = deployer.activate_supervisor_host(record, NEW)

    assert result["status"] == "host_activated"
    assert core.operation_reads == 4
    assert record["host_activation"]["running_operations"]["supported"] is True
    assert host.supervisor["version"] == NEW


def test_host_swap_refuses_before_stopping_while_an_operation_keeps_running(
    host: Unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop would wait for the operation and reach its grace, killing the
    host mid-operation; the deployer refuses and the old host keeps running."""
    old = dict(host.supervisor)
    monkeypatch.setattr(deployer, "api", lambda: BusyOperations(busy=10_000))
    monkeypatch.setattr(deployer, "HOST_OPERATION_POLL_SECONDS", 0.01)
    monkeypatch.setattr(deployer, "HOST_OPERATION_WAIT_SECONDS", 0.1)

    with pytest.raises(RuntimeError, match="stage stage-1.*was not stopped"):
        deployer.activate_supervisor_host(promotion_record(), NEW)

    assert alive(old) and host.active() == "active"
    assert deployer.current_release_version() == OLD
    assert host.events == ["start"]


@pytest.mark.parametrize("status", [404, 405, 503])
def test_host_swap_proceeds_against_a_core_without_the_operation_listing(
    host: Unit, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """An older supervisor relays the read to its API worker: 503
    "reconnecting" in practice, 404 or 405 from other releases."""
    monkeypatch.setattr(
        deployer, "api", lambda: BusyOperations(0, listing=False, status=status)
    )
    record = promotion_record()

    deployer.activate_supervisor_host(record, NEW)

    assert record["host_activation"]["running_operations"] == {
        "supported": False,
        "status": status,
    }
    assert host.supervisor["version"] == NEW


def test_host_swap_refuses_when_the_listing_is_not_authorized(
    host: Unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = dict(host.supervisor)
    monkeypatch.setattr(
        deployer, "api", lambda: BusyOperations(0, listing=False, status=401)
    )

    with pytest.raises(deployer.HTTPError):
        deployer.activate_supervisor_host(promotion_record(), NEW)

    assert alive(old) and deployer.current_release_version() == OLD


class UnreachableAfterFirstListing(BusyOperations):
    """The new release never answers: any later listing read fails."""

    def get(self, path: str, **parameters):
        if path == "deployment/operations" and self.operation_reads >= 1:
            raise deployer.HTTPError(path, 503, "Starting", {}, None)
        return super().get(path, **parameters)


def test_code_only_rollback_never_reads_the_failed_release(
    host: Unit, held: HeldListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring the previous code must not depend on the broken candidate's
    API: the operation listing is read once, before the forward stop only."""
    core = UnreachableAfterFirstListing(busy=0)
    monkeypatch.setattr(deployer, "api", lambda: core)
    host.start_failures[NEW] = 99
    record = promotion_record()

    with pytest.raises(RuntimeError, match="restored supervisor code to " + OLD):
        deployer.activate_supervisor_host(record, NEW)

    assert core.operation_reads == 1
    assert record["host_activation"]["state"] == "rolled_back"
    assert record["host_activation"]["rollback"]["state"] == "complete"
    assert host.supervisor["version"] == OLD and host.active() == "active"


def test_activation_resumes_a_release_selected_while_the_old_host_still_runs(
    host: Unit, held: HeldListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupted after selecting the release and before the stop: the old
    host may still run turns, so the resume waits for operations and the
    quiet boundary again before it stops it."""
    old = dict(host.supervisor)
    record = promotion_record()
    record["host_activation"] = {
        "operation_id": "deployment-1:host-activation",
        "state": "release_selected",
        "version": NEW,
        "previous_version": OLD,
        "prior_status": deployer.host_status_snapshot(status_for()),
    }
    deployer.atomic_json(deployer.STATE, record)
    deployer.switch_to(deployer.RELEASES / NEW)
    core = BusyOperations(busy=1)
    monkeypatch.setattr(deployer, "api", lambda: core)
    monkeypatch.setattr(deployer, "HOST_OPERATION_POLL_SECONDS", 0.01)

    result = deployer.activate_supervisor_host(record, NEW)

    assert result["status"] == "host_activated"
    assert core.operation_reads == 2
    assert "restart_boundary" in record["host_activation"]
    assert not alive(old)
    assert host.supervisor["version"] == NEW and held.started_versions == [NEW]


def test_rollback_host_resumes_a_restore_interrupted_after_selecting_its_release(
    host: Unit, held: HeldListener
) -> None:
    """A socket-held code-only rollback selects the previous release before
    its stop; a crash there leaves the rollback journal at release_selected."""
    host("stop")
    deployer.switch_to(deployer.RELEASES / NEW)
    host("start")
    candidate = dict(host.supervisor)
    deployer.switch_to(deployer.RELEASES / OLD)
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
                "state": "release_selected",
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
    assert not alive(candidate)
    assert host.supervisor["version"] == OLD and host.active() == "active"


def unit_properties(**overrides: str):
    values = {
        ("nebula-core.service", "TriggeredBy"): "nebula-core.socket",
        ("nebula-core.service", "Requires"): "basic.target nebula-core.socket",
        ("nebula-core.socket", "Listen"): "127.0.0.1:8000 (Stream)",
        ("nebula-core.socket", "Accept"): "no",
        ("nebula-core.service", "TimeoutStopUSec"): "1min 30s",
        ("nebula-core.service", "ExecStart"): (
            "{ path=/usr/local/bin/mercury-run ; argv[]=/usr/local/bin/mercury-run "
            "/srv/apple-sim/nebula/current/dist/nebula-core serve --port 8000 "
            "--listen-fd 3 ; ignore_errors=no }"
        ),
    }
    for key, value in overrides.items():
        unit, name = key.split("__")
        values[(f"nebula-core.{unit}", name)] = value
    return lambda unit, name: values[(unit, name)]


def test_socket_activation_reads_a_complete_held_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployer, "unit_property", unit_properties())
    assert deployer.socket_activation() == {
        "socket": "nebula-core.socket",
        "listen": "127.0.0.1:8000 (Stream)",
    }


def test_socket_activation_is_absent_without_a_socket_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deployer,
        "unit_property",
        unit_properties(service__TriggeredBy="", service__Requires="basic.target"),
    )
    assert deployer.socket_activation() is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"service__Requires": "basic.target"}, "Requires=nebula-core.socket"),
        ({"socket__Listen": "0.0.0.0:8000 (Stream)"}, "listen only on 127.0.0.1:8000"),
        (
            {"socket__Listen": "127.0.0.1:8000 (Stream) [::1]:8000 (Stream)"},
            "listen only",
        ),
        ({"socket__Accept": "yes"}, "Accept=no"),
        ({"service__TimeoutStopUSec": "30s"}, "TimeoutStopSec above 40s"),
        (
            {"service__ExecStart": "{ argv[]=/x/nebula-core serve --listen-fd 4 }"},
            "--listen-fd 3",
        ),
    ],
)
def test_socket_activation_refuses_an_unsafe_held_listener(
    monkeypatch: pytest.MonkeyPatch, overrides: dict, message: str
) -> None:
    monkeypatch.setattr(deployer, "unit_property", unit_properties(**overrides))
    with pytest.raises(RuntimeError, match=message):
        deployer.socket_activation()


@pytest.mark.parametrize(
    ("span", "seconds"),
    [
        ("1min 30s", 90.0),
        ("45s", 45.0),
        ("500ms", 0.5),
        ("2h", 7200.0),
        ("infinity", None),
    ],
)
def test_systemd_time_spans(span: str, seconds: float | None) -> None:
    assert deployer.systemd_seconds(span) == seconds


def test_missing_time_span_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="no time span"):
        deployer.systemd_seconds("")
