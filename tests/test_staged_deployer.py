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
            "operation_id": record["runtime_deployment"]["host_activation"]["operation_id"],
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
    assert actions.count("handoff") == 1
    assert actions.index("retire") < actions.index("prepare_execution", 1)


def test_third_execution_version_stays_staged():
    host, record = Host(), {"active": "v2", "pending": "v3"}
    host.state["resident_versions"] = ["v1", "v2"]
    assert (
        host.deployer(record).promote("v3")["status"] == "api_promoted_execution_staged"
    )
    actions = [action for action, _ in host.calls]
    assert "prepare_execution" not in actions and "handoff" not in actions
    assert record["pending"] == "v3"


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
