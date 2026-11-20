"""Staged deployment retries preserve continuing data and bound resident versions."""

import copy
import importlib.util
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from nebula.v3.runtime_management import Identifier

SOURCE = (
    Path(__file__).resolve().parents[1] / "scripts/simulation/nebula_staged_deployer.py"
)
SPEC = importlib.util.spec_from_file_location("staged_deployer", SOURCE)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class Host:
    def __init__(self):
        self.state = {
            "protocol": "nebula.runtime/v1",
            "revision": 1,
            "api_version": "v1",
            "resident_versions": ["v1"],
            "projects": [
                {
                    "project_id": "project",
                    "runtime_id": "old",
                    "runtime_version": "v1",
                    "fencing_token": 1,
                    "phase": None,
                }
            ],
        }
        self.receipts, self.calls, self.saved = {}, [], []
        self.live_runtimes = {"old": "v1"}
        self.worker_versions = {}
        self.lose_promotion = False
        self.lose_handoff = False
        self.hold_unverified = False
        self.auto_retire_no_chat = False
        self.pinned = False
        self.migration_fails = False
        self.data = {"continuing": "keep"}

    def get(self, path):
        assert path == "deployment/status"
        if not self.hold_unverified:
            changed = False
            for project in self.state["projects"]:
                if project.get("phase") == "transferred":
                    project["phase"] = "verified"
                    changed = True
                runtime_id = project.get("runtime_id")
                if (
                    self.auto_retire_no_chat
                    and project.get("phase") == "verified"
                    and runtime_id in self.worker_versions
                ):
                    self.live_runtimes.pop(runtime_id, None)
                    project["runtime_id"] = None
                    project["runtime_version"] = None
                    changed = True
            if changed:
                self.state["revision"] += 1
                self._refresh_resident_versions()
        return copy.deepcopy(self.state)

    def post(self, path, request):
        assert path == "deployment/operations"
        TypeAdapter(Identifier).validate_python(request["operation_id"])
        key, action, values = (
            request["operation_id"],
            request["action"],
            request["parameters"],
        )
        if key in self.receipts:
            return self.receipts[key]
        self.calls.append((action, values.copy()))
        result = {}
        if action == "migrate" and self.migration_fails:
            return {
                "operation_id": key,
                "state": "failed",
                "result": {"detail": "incompatible migration"},
            }
        if action in {"prepare_api", "prepare_execution"}:
            if action == "prepare_api":
                result["runtime_id"] = "worker-api-" + values["version"]
            else:
                result["runtime_id"] = (
                    "worker-" + values["version"] + "-" + str(len(self.worker_versions))
                )
                self.worker_versions[result["runtime_id"]] = values["version"]
                self.live_runtimes[result["runtime_id"]] = values["version"]
                self._refresh_resident_versions()
        if action == "promote_api":
            self.state["api_version"] = values["runtime_id"].removeprefix("worker-api-")
        if action in {"target_execution", "rollback_code"}:
            resident = self.state["resident_versions"]
            if values["version"] not in resident and len(resident) >= 2:
                # Core refuses a target no worker could start as.
                receipt = {
                    "operation_id": key,
                    "action": action,
                    "state": "failed",
                    "result": {
                        "detail": "Release "
                        + values["version"]
                        + " cannot become the execution target"
                    },
                }
                self.receipts[key] = receipt
                return receipt
            self.state["target_execution_version"] = values["version"]
        if action == "handoff":
            project = next(
                item
                for item in self.state["projects"]
                if item["project_id"] == values["project_id"]
            )
            if self.pinned:
                project.update(
                    blockers=["An open terminal needs this runtime"],
                    admission_held=False,
                )
                receipt = {
                    "operation_id": key,
                    "action": action,
                    "state": "failed",
                    "result": {"detail": "An open terminal needs this runtime"},
                }
                self.receipts[key] = receipt
                return receipt
            else:
                version = self.worker_versions[values["target_runtime_id"]]
                phase = "transferred" if self.lose_handoff else "verified"
                project.update(
                    runtime_id=values["target_runtime_id"],
                    runtime_version=version,
                    target_version=version,
                    phase=phase,
                    fencing_token=project.get("fencing_token", 0) + 1,
                )
        if action == "retire":
            self.live_runtimes.pop(values["runtime_id"], None)
            self._refresh_resident_versions()
            result["runtime_id"] = values["runtime_id"]
        self.state["revision"] += 1
        receipt = {
            "operation_id": key,
            "action": action,
            "state": "succeeded",
            "result": result,
        }
        self.receipts[key] = receipt
        if action == "handoff" and self.lose_handoff:
            self.lose_handoff = False
            raise OSError("lost reply after committed handoff")
        if action == "promote_api" and self.lose_promotion:
            self.lose_promotion = False
            raise OSError("lost reply after committed promotion")
        return receipt

    def _refresh_resident_versions(self):
        self.state["resident_versions"] = sorted(set(self.live_runtimes.values()))

    def deployer(self, record):
        return module.StagedDeployer(
            record,
            get=self.get,
            post=self.post,
            save=lambda value: self.saved.append(copy.deepcopy(value)),
            verify=lambda version: (Path("/retained") / version, "a" * 64),
        )

    def deployer_with_activation(self, record, activate_host):
        return module.StagedDeployer(
            record,
            get=self.get,
            post=self.post,
            save=lambda value: self.saved.append(copy.deepcopy(value)),
            verify=lambda version: (Path("/retained") / version, "a" * 64),
            activate_host=activate_host,
        )


def test_promotion_moves_project_without_touching_continuing_data():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    result = host.deployer(record).promote("v2")
    assert result["status"] == "promoted"
    assert host.state["projects"][0]["runtime_version"] == "v2"
    assert host.state["projects"][0]["phase"] == "verified"
    assert host.state["projects"][0]["fencing_token"] == 2
    assert "old" not in host.live_runtimes
    assert host.data == {"continuing": "keep"}
    assert record["previous"] == "v1" and record["pending"] is None


def test_verified_no_chat_target_can_be_retired_before_source_release():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.auto_retire_no_chat = True
    result = host.deployer(record).promote("v2")
    actions = [action for action, _ in host.calls]
    assert result["status"] == "promoted"
    assert host.state["projects"][0]["runtime_id"] is None
    assert "old" not in host.live_runtimes
    assert actions.index("handoff") < actions.index("retire", actions.index("handoff"))


def test_host_activation_runs_after_migration_and_before_api_promotion():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    events = []

    def activate_host(version):
        events.append("activate_host")
        return {
            "operation_id": record["runtime_deployment"]["host_activation"][
                "operation_id"
            ],
            "status": "host_activated",
            "version": version,
            "health_commit": version,
        }

    def post(path, request):
        events.append(request["action"])
        return host.post(path, request)

    deployer = host.deployer_with_activation(record, activate_host)
    deployer.post = post
    deployer.promote("v2")
    assert events.index("migrate") < events.index("activate_host")
    assert events.index("activate_host") < events.index("prepare_api")
    assert record["runtime_deployment"]["host_activation"]["result"]["version"] == "v2"


def test_projects_move_serially_and_retire_each_source_before_next_target():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.state["projects"].append(
        {
            "project_id": "project-2",
            "runtime_id": "old-2",
            "runtime_version": "v1",
            "fencing_token": 1,
            "phase": None,
        }
    )
    host.live_runtimes["old-2"] = "v1"
    result = host.deployer(record).promote("v2")
    actions = [action for action, _ in host.calls]
    first_prepare = actions.index("prepare_execution")
    first_retire = actions.index("retire")
    second_prepare = actions.index("prepare_execution", first_prepare + 1)
    assert result["status"] == "promoted"
    assert first_prepare < first_retire < second_prepare
    assert "old" not in host.live_runtimes and "old-2" not in host.live_runtimes


def test_shared_source_stops_before_preparing_another_target():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.state["projects"].append(
        {
            "project_id": "project-2",
            "runtime_id": "old",
            "runtime_version": "v1",
            "fencing_token": 1,
            "phase": None,
        }
    )
    result = host.deployer(record).promote("v2")
    actions = [action for action, _ in host.calls]
    assert result["status"] == "waiting_for_handoffs"
    assert actions.count("prepare_execution") == 0
    assert "retire" not in actions


def test_lost_promotion_reply_reuses_the_exact_operation():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.lose_promotion = True
    with pytest.raises(OSError):
        host.deployer(record).promote("v2")
    journal_id = record["runtime_deployment"]["id"]
    assert host.deployer(record).promote("v2")["status"] == "promoted"
    assert [action for action, _ in host.calls].count("promote_api") == 1
    assert record["runtime_deployment"]["id"] == journal_id


def test_lost_handoff_reply_reconciles_before_retiring_or_advancing():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.state["projects"].append(
        {
            "project_id": "project-2",
            "runtime_id": "old-2",
            "runtime_version": "v1",
            "fencing_token": 1,
            "phase": None,
        }
    )
    host.live_runtimes["old-2"] = "v1"
    host.lose_handoff = True
    host.hold_unverified = True
    result = host.deployer(record).promote("v2")
    actions = [action for action, _ in host.calls]
    assert result["status"] == "waiting_for_handoffs"
    assert actions.count("prepare_execution") == 1
    assert "retire" not in actions
    host.hold_unverified = False
    assert host.deployer(record).promote("v2")["status"] == "promoted"
    actions = [action for action, _ in host.calls]
    handoffs = [
        parameters["project_id"]
        for action, parameters in host.calls
        if action == "handoff"
    ]
    assert sorted(handoffs) == ["project", "project-2"]
    first_prepare = actions.index("prepare_execution")
    second_prepare = actions.index("prepare_execution", first_prepare + 1)
    assert actions.index("retire") < second_prepare


def test_third_execution_version_stays_staged():
    host, record = Host(), {"active": "v2", "pending": "v3"}
    host.state["resident_versions"] = ["v1", "v2"]
    host.state["target_execution_version"] = "v2"
    assert (
        host.deployer(record).promote("v3")["status"] == "api_promoted_execution_staged"
    )
    actions = [action for action, _ in host.calls]
    assert "promote_api" in actions and host.state["api_version"] == "v3"
    assert "target_execution" not in actions
    assert "prepare_execution" not in actions and "handoff" not in actions
    assert host.state["target_execution_version"] == "v2"
    assert record["pending"] == "v3"
    assert "target" not in record["runtime_deployment"]["steps"]


def test_staged_execution_names_the_projects_pinning_each_version():
    host, record = Host(), {"active": "v2", "pending": "v3"}
    host.state["projects"].append(
        {
            "project_id": "apple",
            "runtime_id": "current",
            "runtime_version": "v2",
            "fencing_token": 1,
            "phase": "finishing",
            "blockers": [],
        }
    )
    host.live_runtimes["current"] = "v2"
    host._refresh_resident_versions()
    host.state["processes"] = [
        {"runtime_id": "old", "version": "v1", "role": "execution"},
        {"runtime_id": "current", "version": "v2", "role": "execution"},
        {"runtime_id": "spare", "version": "v2", "role": "execution"},
    ]

    result = host.deployer(record).promote("v3")

    assert result["status"] == "api_promoted_execution_staged"
    assert "versions v1, v2 are resident" in result["detail"]
    assert "version v1 is kept by project" in result["detail"]
    assert (
        "version v2 is kept by apple (handoff finishing) and unowned worker(s) spare"
        in result["detail"]
    )
    assert "then rerun this deployment" in result["detail"]
    assert result["blockers"] == [
        {
            "version": "v1",
            "projects": [
                {
                    "project_id": "project",
                    "runtime_id": "old",
                    "phase": None,
                    "blockers": [],
                }
            ],
            "unowned_runtime_ids": [],
        },
        {
            "version": "v2",
            "projects": [
                {
                    "project_id": "apple",
                    "runtime_id": "current",
                    "phase": "finishing",
                    "blockers": [],
                }
            ],
            "unowned_runtime_ids": ["spare"],
        },
    ]


def test_staged_promotion_resumes_once_an_older_version_retires():
    host, record = Host(), {"active": "v2", "pending": "v3"}
    host.state["projects"].append(
        {
            "project_id": "project-2",
            "runtime_id": "current",
            "runtime_version": "v2",
            "fencing_token": 1,
            "phase": None,
        }
    )
    host.live_runtimes["current"] = "v2"
    host._refresh_resident_versions()
    first = host.deployer(record).promote("v3")
    assert first["status"] == "api_promoted_execution_staged"
    journal_id = record["runtime_deployment"]["id"]

    # The v1 project goes idle and its worker retires, freeing a slot.
    host.live_runtimes.pop("old")
    host.state["projects"][0].update(runtime_id=None, runtime_version=None)
    host._refresh_resident_versions()
    assert host.deployer(record).promote("v3")["status"] == "promoted"
    actions = [action for action, _ in host.calls]
    assert actions.count("promote_api") == 1
    assert actions.count("target_execution") == 1
    assert host.state["target_execution_version"] == "v3"
    assert host.state["projects"][1]["runtime_version"] == "v3"
    assert record["runtime_deployment"]["id"] == journal_id


def test_code_rollback_to_a_third_version_keeps_the_target():
    host, record = Host(), {"active": "v3", "previous": "v1"}
    host.live_runtimes = {"old": "v2", "new": "v3"}
    host._refresh_resident_versions()
    host.state["api_version"] = "v3"
    host.state["target_execution_version"] = "v3"
    result = host.deployer(record).rollback("v1")
    assert result["status"] == "api_promoted_execution_staged"
    actions = [action for action, _ in host.calls]
    assert "rollback_code" not in actions and "prepare_execution" not in actions
    assert host.state["api_version"] == "v1"
    assert host.state["target_execution_version"] == "v3"


def test_pinned_project_retains_its_old_execution_owner():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.pinned = True
    result = host.deployer(record).promote("v2")
    assert result["status"] == "waiting_for_handoffs"
    assert result["pinned"][0]["runtime_id"] == "old"
    actions = [action for action, _ in host.calls]
    assert actions.count("retire") == 1
    assert host.state["projects"][0]["runtime_id"] == "old"


def test_code_rollback_keeps_data_committed_on_newer_code():
    host, record = Host(), {"active": "v2", "previous": "v1"}
    host.state["api_version"] = "v2"
    host.state["projects"][0]["runtime_version"] = "v2"
    host.data["after-promotion"] = "also keep"
    host.deployer(record).rollback("v1")
    assert host.state["api_version"] == "v1"
    assert host.data == {"continuing": "keep", "after-promotion": "also keep"}
    actions = [action for action, _ in host.calls]
    assert "rollback_code" in actions
    assert "migrate" not in actions


def test_failed_migration_prevents_promotion_and_preserves_journal():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.migration_fails = True
    with pytest.raises(RuntimeError, match="incompatible migration"):
        host.deployer(record).promote("v2")
    assert "promote_api" not in [action for action, _ in host.calls]
    assert host.state["api_version"] == "v1"
    assert record["runtime_deployment"]["state"] == "running"


def _stall_then_recover(
    host, *, version="v2", source_fence=1, recovery_phase="verified"
):
    """Stall the first handoff, then let Core recover the project itself."""
    stalled = []
    original = host.post

    def post(path, request):
        receipt = original(path, request)
        if request["action"] == "handoff" and not stalled:
            # Core accepted the request, but the source never reached a safe
            # boundary: ownership stays with the old worker.
            stalled.append(request["parameters"]["target_runtime_id"])
            host.state["projects"][0].update(
                runtime_id="old",
                runtime_version="v1",
                fencing_token=1,
                phase="finishing",
                target_version="v2",
                admission_held=True,
            )
        return receipt

    return post, stalled


def _recover(host, *, runtime_version, source_fence=1, phase="verified", blockers=None):
    host.live_runtimes.pop("old", None)
    host.live_runtimes["recovered"] = runtime_version
    host._refresh_resident_versions()
    host.state["projects"][0].update(
        runtime_id="recovered",
        runtime_version=runtime_version,
        fencing_token=source_fence + 1,
        phase=phase,
        target_version=runtime_version,
        admission_held=phase != "verified",
        blockers=blockers or [],
        recovery={
            "operation_id": "auto-recovery",
            "source_runtime_id": "old",
            "source_fencing_token": source_fence,
            "target_runtime_id": "recovered",
            "target_fencing_token": source_fence + 1,
            "phase": phase,
        },
    )
    host.state["revision"] += 1


def _with_processes(host):
    get = host.get

    def read(path):
        value = get(path)
        value["processes"] = [
            {
                "runtime_id": runtime_id,
                "version": version,
                "role": "execution",
                "state": "ready",
                "ready_verified": True,
            }
            for runtime_id, version in host.live_runtimes.items()
        ]
        return value

    return read


def _recovery_deployer(host, record):
    deployer = host.deployer(record)
    deployer.get = _with_processes(host)
    return deployer


def test_stalled_handoff_adopts_core_recovery_onto_the_new_version():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.hold_unverified = True
    deployer = _recovery_deployer(host, record)
    deployer.post, stalled = _stall_then_recover(host)
    assert deployer.promote("v2")["status"] == "waiting_for_handoffs"
    target = stalled[0]

    # The operator stops the stalled source; Core recovers the project onto a
    # fresh worker of the target version that this deployer never prepared.
    _recover(host, runtime_version="v2")
    host.hold_unverified = False
    result = _recovery_deployer(host, record).promote("v2")

    assert result["status"] == "promoted"
    assert target not in host.live_runtimes
    assert [action for action, _ in host.calls].count("handoff") == 1
    adopted = record["runtime_deployment"]["reconciled_recoveries"]["project"]
    assert adopted["runtime_id"] == "recovered"
    assert adopted["recovery_operation_id"] == "auto-recovery"
    assert adopted["source_runtime_id"] == "old"


def test_recovery_from_an_older_grant_is_not_adopted():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.hold_unverified = True
    deployer = _recovery_deployer(host, record)
    deployer.post, _ = _stall_then_recover(host)
    deployer.promote("v2")
    # The journaled source grant was fence 1; a recovery that started from an
    # earlier grant proves nothing about this deployment's source.
    _recover(host, runtime_version="v2", source_fence=0)
    host.state["projects"][0]["fencing_token"] = 2
    host.state["projects"][0]["recovery"]["target_fencing_token"] = 2
    result = _recovery_deployer(host, record).promote("v2")
    assert result["status"] == "waiting_for_handoffs"
    assert "reconciled_recoveries" not in record["runtime_deployment"]


def test_unverified_recovery_keeps_waiting_with_its_blocker():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.hold_unverified = True
    deployer = _recovery_deployer(host, record)
    deployer.post, _ = _stall_then_recover(host)
    deployer.promote("v2")
    _recover(
        host,
        runtime_version="v2",
        phase="transferred",
        blockers=["Target runtime recovered has not verified restoration"],
    )
    result = _recovery_deployer(host, record).promote("v2")
    assert result["status"] == "waiting_for_handoffs"
    assert "reconciled_recoveries" not in record["runtime_deployment"]


def test_recovery_onto_the_old_version_hands_off_again_under_a_new_generation():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.hold_unverified = True
    deployer = _recovery_deployer(host, record)
    deployer.post, stalled = _stall_then_recover(host)
    deployer.promote("v2")
    _recover(host, runtime_version="v1")
    host.hold_unverified = False
    result = _recovery_deployer(host, record).promote("v2")

    assert result["status"] == "promoted"
    project = host.state["projects"][0]
    assert project["runtime_version"] == "v2"
    assert "recovered" not in host.live_runtimes
    assert stalled[0] not in host.live_runtimes
    journal = record["runtime_deployment"]
    assert journal["handoff_generation"] == {"project": 1}
    handoffs = [step for step in journal["steps"] if step.startswith("handoff/")]
    assert len(handoffs) == 2 and any(step.endswith(":g1") for step in handoffs)


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _grace_deployer(host, record, clock, *, grace=60):
    deployer = module.StagedDeployer(
        record,
        get=_with_processes(host),
        post=host.post,
        save=lambda value: host.saved.append(copy.deepcopy(value)),
        verify=lambda version: (Path("/retained") / version, "a" * 64),
        handoff_grace_seconds=grace,
        recovery_wait_seconds=120,
        poll_seconds=5,
        clock=clock,
        sleep=clock.sleep,
    )
    return deployer


def _evict_then_recover(host, deployer, clock):
    """Core's side: a stopped source's project is recovered a little later."""
    stopped = []
    post = deployer.post

    def evicting_post(path, request):
        receipt = post(path, request)
        if request["action"] == "evict_execution":
            owner = request["parameters"]["expected_owner"]
            stopped.append((owner, clock.now))
            host.live_runtimes.pop(owner["runtime_id"], None)
            receipt["result"] = {**owner, "affected_project_ids": [owner["project_id"]]}
        return receipt

    get = deployer.get

    def recovering_get(path):
        if stopped and clock.now >= stopped[-1][1] + 20:
            project = host.state["projects"][0]
            if project.get("runtime_id") == stopped[-1][0]["runtime_id"]:
                _recover(host, runtime_version="v2")
        return get(path)

    deployer.post, deployer.get = evicting_post, recovering_get
    return stopped


def test_stalled_handoff_stops_its_source_after_grace_and_completes():
    host, record, clock = Host(), {"active": "v1", "pending": "v2"}, Clock()
    host.hold_unverified = True
    deployer = _grace_deployer(host, record, clock)
    deployer.post, _ = _stall_then_recover(host)
    stopped = _evict_then_recover(host, deployer, clock)

    result = deployer.promote("v2")

    assert result["status"] == "promoted"
    assert len(stopped) == 1
    owner, at = stopped[0]
    assert owner == {"project_id": "project", "runtime_id": "old", "fencing_token": 1}
    assert at - 1_000.0 >= 60
    journal = record["runtime_deployment"]
    assert journal["evicted_sources"]["project"]["runtime_id"] == "old"
    assert journal["reconciled_recoveries"]["project"]["runtime_id"] == "recovered"
    assert host.state["projects"][0]["runtime_version"] == "v2"


def test_pinned_project_is_stopped_after_grace_instead_of_blocking():
    host, record, clock = Host(), {"active": "v1", "pending": "v2"}, Clock()
    host.pinned = True
    deployer = _grace_deployer(host, record, clock)
    stopped = _evict_then_recover(host, deployer, clock)

    result = deployer.promote("v2")

    assert result["status"] == "promoted"
    assert [owner["runtime_id"] for owner, _ in stopped] == ["old"]
    assert host.state["projects"][0]["runtime_id"] == "recovered"


def test_without_grace_a_stalled_handoff_reports_and_never_stops_its_source():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.hold_unverified = True
    deployer = _recovery_deployer(host, record)
    deployer.post, _ = _stall_then_recover(host)
    assert deployer.promote("v2")["status"] == "waiting_for_handoffs"
    assert "evict_execution" not in [action for action, _ in host.calls]


def test_promotion_reports_each_phase_duration():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    result = host.deployer(record).promote("v2")
    assert result["status"] == "promoted"
    steps = [step["step"] for step in result["timings"]["steps"]]
    assert steps == [
        "stage_migrate_host_and_api",
        "target_execution",
        "project_handoffs",
    ]
    assert all(step["seconds"] >= 0 for step in result["timings"]["steps"])
    assert result["timings"]["total_seconds"] >= 0


IDLE_SESSION = "harness_sessions 7f265d4f remains idle; its restoration is unavailable"


def test_idle_unrestorable_sessions_are_stopped_without_waiting_out_grace():
    """Idle harness sessions Core cannot restore never hand off by
    themselves; the source is stopped at once instead of after the grace."""
    host, record, clock = Host(), {"active": "v1", "pending": "v2"}, Clock()
    host.pinned = True
    original = host.post

    def post(path, request):
        receipt = original(path, request)
        if request["action"] == "handoff":
            host.state["projects"][0]["blockers"] = [IDLE_SESSION, IDLE_SESSION.replace("7f265d4f", "df734f1a")]
        return receipt

    host.post = post
    deployer = _grace_deployer(host, record, clock, grace=180)
    stopped = _evict_then_recover(host, deployer, clock)

    assert deployer.promote("v2")["status"] == "promoted"
    assert [owner["runtime_id"] for owner, _ in stopped] == ["old"]
    assert stopped[0][1] - 1_000.0 < 180


def test_other_blockers_still_get_their_grace():
    host, record, clock = Host(), {"active": "v1", "pending": "v2"}, Clock()
    host.pinned = True
    original = host.post

    def post(path, request):
        receipt = original(path, request)
        if request["action"] == "handoff":
            host.state["projects"][0]["blockers"] = [
                IDLE_SESSION, "An accepted provider turn is finishing on its current runtime",
            ]
        return receipt

    host.post = post
    deployer = _grace_deployer(host, record, clock, grace=180)
    stopped = _evict_then_recover(host, deployer, clock)

    assert deployer.promote("v2")["status"] == "promoted"
    assert stopped[0][1] - 1_000.0 >= 180


def test_promotion_retires_unowned_workers_of_older_versions():
    """A spare old worker owns no project but stays resident, which would
    hold the next release at "execution staged"; promotion retires it."""
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.live_runtimes["spare-old"] = "v1"
    host._refresh_resident_versions()
    deployer = _recovery_deployer(host, record)

    assert deployer.promote("v2")["status"] == "promoted"
    retired = [values["runtime_id"] for action, values in host.calls if action == "retire"]
    assert "spare-old" in retired
    assert host.state["resident_versions"] == ["v2"]
