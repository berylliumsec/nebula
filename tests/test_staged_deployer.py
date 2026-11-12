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
                {"project_id": "project", "runtime_id": "old", "runtime_version": "v1"}
            ],
        }
        self.receipts, self.calls, self.saved = {}, [], []
        self.lose_promotion = False
        self.pinned = False
        self.migration_fails = False
        self.data = {"continuing": "keep"}

    def get(self, path):
        assert path == "deployment/status"
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
        self.calls.append(action)
        result = {}
        if action == "migrate" and self.migration_fails:
            return {
                "operation_id": key,
                "state": "failed",
                "result": {"detail": "incompatible migration"},
            }
        if action in {"prepare_api", "prepare_execution"}:
            result["runtime_id"] = "worker-" + values["version"]
        if action == "promote_api":
            self.state["api_version"] = values["runtime_id"].removeprefix("worker-")
        if action in {"target_execution", "rollback_code"}:
            self.state["target_execution_version"] = values["version"]
        if action == "handoff":
            project = self.state["projects"][0]
            if self.pinned:
                project.update(
                    blockers=["An open terminal needs this runtime"],
                    admission_held=False,
                )
            else:
                version = values["target_runtime_id"].removeprefix("worker-")
                project.update(
                    runtime_id=values["target_runtime_id"],
                    runtime_version=version,
                    phase="verified",
                )
        self.state["revision"] += 1
        receipt = {
            "operation_id": key,
            "action": action,
            "state": "succeeded",
            "result": result,
        }
        self.receipts[key] = receipt
        if action == "promote_api" and self.lose_promotion:
            self.lose_promotion = False
            raise OSError("lost reply after committed promotion")
        return receipt

    def deployer(self, record):
        return module.StagedDeployer(
            record,
            get=self.get,
            post=self.post,
            save=lambda value: self.saved.append(copy.deepcopy(value)),
            verify=lambda version: (Path("/retained") / version, "a" * 64),
        )


def test_promotion_moves_project_without_touching_continuing_data():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    result = host.deployer(record).promote("v2")
    assert result["status"] == "promoted"
    assert host.state["projects"][0]["runtime_version"] == "v2"
    assert host.data == {"continuing": "keep"}
    assert record["previous"] == "v1" and record["pending"] is None


def test_lost_promotion_reply_reuses_the_exact_operation():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.lose_promotion = True
    with pytest.raises(OSError):
        host.deployer(record).promote("v2")
    journal_id = record["runtime_deployment"]["id"]
    assert host.deployer(record).promote("v2")["status"] == "promoted"
    assert host.calls.count("promote_api") == 1
    assert record["runtime_deployment"]["id"] == journal_id


def test_third_execution_version_stays_staged():
    host, record = Host(), {"active": "v2", "pending": "v3"}
    host.state["resident_versions"] = ["v1", "v2"]
    assert (
        host.deployer(record).promote("v3")["status"] == "api_promoted_execution_staged"
    )
    assert "prepare_execution" not in host.calls and "handoff" not in host.calls
    assert record["pending"] == "v3"


def test_pinned_project_retains_its_old_execution_owner():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.pinned = True
    result = host.deployer(record).promote("v2")
    assert result["status"] == "promoted_with_pinned_projects"
    assert result["pinned"][0]["runtime_id"] == "old"
    assert "retire" not in host.calls


def test_code_rollback_keeps_data_committed_on_newer_code():
    host, record = Host(), {"active": "v2", "previous": "v1"}
    host.state["api_version"] = "v2"
    host.state["projects"][0]["runtime_version"] = "v2"
    host.data["after-promotion"] = "also keep"
    host.deployer(record).rollback("v1")
    assert host.state["api_version"] == "v1"
    assert host.data == {"continuing": "keep", "after-promotion": "also keep"}
    assert "rollback_code" in host.calls
    assert "migrate" not in host.calls


def test_failed_migration_prevents_promotion_and_preserves_journal():
    host, record = Host(), {"active": "v1", "pending": "v2"}
    host.migration_fails = True
    with pytest.raises(RuntimeError, match="incompatible migration"):
        host.deployer(record).promote("v2")
    assert "promote_api" not in host.calls
    assert host.state["api_version"] == "v1"
    assert record["runtime_deployment"]["state"] == "running"
