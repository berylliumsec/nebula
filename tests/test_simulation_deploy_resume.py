"""Promotion and code rollback resume from every step against a modelled host.

These tests run the real ``nebula_deployer`` entry points (``promote``,
``rollback``, ``activate_host``) with the real ``StagedDeployer`` and the real
supervisor host activation. Only the machine boundary is modelled: the Core
HTTP API (deployment status, health and the operation ledger) and the
``nebula-core.service`` unit. The modelled ledger keeps Core's contract: an
operation ID is dispatched once, and a replay returns its saved receipt, so a
``failed`` or ``uncertain`` receipt is never dispatched again under that ID.
Handoffs verify asynchronously, as they do in Core, so a first promotion
returns ``waiting_for_handoffs`` and a rerun must resume it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts/simulation"
sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location(
    "simulation_nebula_deployer_resume", SCRIPT_DIR / "nebula_deployer.py"
)
assert spec is not None and spec.loader is not None
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)

OLD = "5" * 40
NEW = "3" * 40
SCHEMA = 5


def package(version: str) -> str:
    return "3.0.0-beta.3+" + version[:16]


def digest(version: str) -> str:
    return hashlib.sha256(version.encode()).hexdigest()


class Host:
    """The modelled Core host: supervisor unit, status, health and ledger."""

    def __init__(self, releases: Path) -> None:
        self.releases = releases
        self.unit = "active"
        self.host_version = OLD
        self.pid = 4000
        self.revision = 40
        self.installed = {OLD}
        self.processes = {
            "api-old": {"version": OLD, "role": "api", "state": "ready"},
            "exec-old-1": {"version": OLD, "role": "execution", "state": "ready"},
            "exec-old-2": {"version": OLD, "role": "execution", "state": "ready"},
        }
        self.api_runtime = "api-old"
        self.target = OLD
        self.projects = {
            "project-1": {"runtime_id": "exec-old-1", "fencing_token": 3},
            "project-2": {"runtime_id": "exec-old-2", "fencing_token": 5},
        }
        self.handoffs: dict[str, dict] = {}
        self.ledger: dict[str, dict] = {}
        self.dispatched: list[tuple[str, str, dict]] = []
        self.faults: dict[str, list[str]] = {}
        self.verify_handoffs = True
        # Hosts from this change on list unretired processes in their status.
        self.lists_processes = False
        self.spawned = 0
        self.restarts = 0

    # ---------------------------------------------------------------- reads
    def status(self) -> dict:
        if self.verify_handoffs:
            for project_id, handoff in self.handoffs.items():
                if handoff["phase"] == "requested":
                    project = self.projects[project_id]
                    project["runtime_id"] = handoff["target_runtime_id"]
                    project["fencing_token"] += 1
                    handoff["phase"] = "verified"
                    self.revision += 1
        resident = sorted(
            {
                row["version"]
                for row in self.processes.values()
                if row["role"] == "execution" and row["state"] != "retired"
            }
        )
        listing = (
            {
                "processes": [
                    {"runtime_id": runtime_id, **row}
                    for runtime_id, row in sorted(self.processes.items())
                    if row["state"] != "retired"
                ]
            }
            if self.lists_processes
            else {}
        )
        return {
            **listing,
            "protocol": "nebula.runtime/v1",
            "revision": self.revision,
            "api_version": self.processes[self.api_runtime]["version"],
            "target_execution_version": self.target,
            "resident_versions": resident,
            "staged_versions": sorted(self.installed - set(resident)),
            "projects": [
                {
                    "project_id": project_id,
                    "runtime_id": row["runtime_id"],
                    "runtime_version": self.processes[row["runtime_id"]]["version"]
                    if row["runtime_id"]
                    else None,
                    "fencing_token": row["fencing_token"],
                    "phase": self.handoffs.get(project_id, {}).get("phase"),
                    "target_version": self.handoffs.get(project_id, {}).get(
                        "target_version"
                    ),
                    "blockers": [],
                    "admission_held": False,
                }
                for project_id, row in sorted(self.projects.items())
            ],
            "health": "ready",
            "detail": "",
        }

    def health(self) -> dict:
        if self.unit != "active":
            raise OSError("Connection refused")
        return {
            "status": "ok",
            "database": "ok",
            "commit": self.host_version,
            "recovery_version": package(self.host_version),
            "schema_version": SCHEMA,
        }

    def get(self, path: str, **_parameters):
        if self.unit != "active":
            raise OSError("Connection refused")
        if path == "health":
            return self.health()
        if path == "deployment/status":
            return self.status()
        if path == "engagements":
            return []
        if path.startswith("deployment/operations/"):
            return dict(self.ledger[path.removeprefix("deployment/operations/")])
        raise AssertionError(path)

    # ------------------------------------------------------------- ledger
    def post(self, path: str, request: dict) -> dict:
        assert path == "deployment/operations"
        if self.unit != "active":
            raise OSError("Connection refused")
        operation_id = request["operation_id"]
        action, parameters = request["action"], request["parameters"]
        request_hash = json.dumps([action, parameters], sort_keys=True)
        saved = self.ledger.get(operation_id)
        if saved is not None:
            if saved["request_hash"] != request_hash:
                raise OSError("HTTP Error 409: operation ID identifies another request")
            return {k: v for k, v in saved.items() if k != "request_hash"}
        fault = (self.faults.get(action) or [None]).pop(0)
        self.dispatched.append((operation_id, action, dict(parameters)))
        state, result = "succeeded", None
        if fault == "rejected":
            state = "failed"
            result = {
                "detail": "refused before any effect",
                "error_type": "OperationRejected",
            }
        elif fault == "changed":
            self.revision += 1
            state = "failed"
            result = {
                "detail": "deployment changed before this operation",
                "error_type": "DeploymentChanged",
            }
        elif fault == "uncertain":
            state = "uncertain"
            result = {
                "detail": "The operation did not report a confirmed result.",
                "error_type": "TimeoutError",
            }
        else:
            result = self.dispatch(action, parameters)
            if fault == "uncertain_after_effect":
                state = "uncertain"
                result = {
                    "detail": "The operation did not report a confirmed result.",
                    "error_type": "TimeoutError",
                }
        receipt = {
            "operation_id": operation_id,
            "action": action,
            "state": state,
            "result": result,
        }
        self.ledger[operation_id] = {**receipt, "request_hash": request_hash}
        return receipt

    def _revision(self, parameters: dict) -> None:
        if parameters["expected_revision"] != self.revision:
            raise AssertionError("modelled host received a stale revision")

    def dispatch(self, action: str, parameters: dict) -> dict:
        if action == "stage":
            version = Path(parameters["source"]).name
            assert parameters["manifest_sha256"] == digest(version)
            if version not in self.installed:
                self.installed.add(version)
                self.revision += 1
            return {
                "version": version,
                "manifest_sha256": parameters["manifest_sha256"],
            }
        if action == "migrate":
            self._revision(parameters)
            return {"version": parameters["version"], "state": "current"}
        if action == "prepare_api":
            return self._prepare_api(parameters["version"])
        if action == "promote_api":
            self._revision(parameters)
            self._promote(parameters["runtime_id"])
            return self.status()
        if action == "target_execution":
            self.target = parameters["version"]
            self.revision += 1
            return self.status()
        if action == "rollback_code":
            self._revision(parameters)
            self.target = parameters["version"]
            self.revision += 1
            self._promote(self._prepare_api(parameters["version"])["runtime_id"])
            return self.status()
        if action == "prepare_execution":
            self.spawned += 1
            runtime_id = f"exec-{parameters['version'][:3]}-{self.spawned}"
            self.processes[runtime_id] = {
                "version": parameters["version"],
                "role": "execution",
                "state": "ready",
            }
            self.revision += 1
            return {"runtime_id": runtime_id, "version": parameters["version"]}
        if action == "handoff":
            project_id = parameters["project_id"]
            assert (
                project_id not in self.handoffs
                or self.handoffs[project_id]["phase"] == "verified"
            )
            target = parameters["target_runtime_id"]
            assert self.processes[target]["state"] == "ready"
            self.handoffs[project_id] = {
                "phase": "requested",
                "target_runtime_id": target,
                "target_version": self.processes[target]["version"],
            }
            self.revision += 1
            return self.status()
        if action == "retire":
            runtime_id = parameters["runtime_id"]
            assert runtime_id not in {
                row["runtime_id"] for row in self.projects.values()
            }
            self.processes[runtime_id]["state"] = "retired"
            self.revision += 1
            return {"runtime_id": runtime_id}
        raise AssertionError(action)

    def _prepare_api(self, version: str) -> dict:
        assert version in self.installed
        for runtime_id, row in self.processes.items():
            if (
                row["role"] == "api"
                and row["version"] == version
                and row["state"] == "ready"
            ):
                return {"runtime_id": runtime_id, "version": version, "role": "api"}
        self.spawned += 1
        runtime_id = f"api-{version[:3]}-{self.spawned}"
        self.processes[runtime_id] = {
            "version": version,
            "role": "api",
            "state": "ready",
        }
        self.revision += 1
        return {"runtime_id": runtime_id, "version": version, "role": "api"}

    def _promote(self, runtime_id: str) -> None:
        assert self.processes[runtime_id]["state"] == "ready"
        self.api_runtime = runtime_id
        self.revision += 1

    # -------------------------------------------------------- service unit
    def service(self, action: str) -> None:
        if action == "stop":
            self.unit = "inactive"
            return
        assert action == "start" and self.unit == "inactive"
        self.host_version = (self.releases.parent / "current").resolve().name
        self.pid += 1
        self.restarts += 1
        self.unit = "active"

    def identity(self) -> dict:
        return {
            "package_version": package(self.host_version),
            "pid": self.pid,
            "process_birth": f"birth-{self.pid}",
        }

    # ------------------------------------------------------------ checks
    def owners(self) -> dict[str, str]:
        return {
            project_id: self.processes[row["runtime_id"]]["version"]
            for project_id, row in self.projects.items()
            if row["runtime_id"]
        }

    def actions(self, action: str) -> list[str]:
        return [key for key, name, _ in self.dispatched if name == action]


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    releases = tmp_path / "releases"
    for version in (OLD, NEW):
        release = releases / version
        release.mkdir(parents=True)
        (release / "nebula-runtime.json").write_text(
            json.dumps(
                {
                    "version": version,
                    "schema_family": SCHEMA,
                    "schema_min": SCHEMA,
                    "schema_max": SCHEMA,
                }
            )
        )
    (tmp_path / "current").symlink_to(releases / OLD)
    core = Host(releases)
    monkeypatch.setattr(deployer, "RELEASES", releases)
    monkeypatch.setattr(deployer, "CURRENT", tmp_path / "current")
    monkeypatch.setattr(deployer, "STATE", tmp_path / "deployer-state.json")
    monkeypatch.setattr(deployer, "api", lambda: core)
    monkeypatch.setattr(deployer, "deployment_post", core.post)
    monkeypatch.setattr(
        deployer,
        "runtime_release",
        lambda version: (releases / version, digest(version)),
    )
    monkeypatch.setattr(deployer, "runtime_package_version", package)
    monkeypatch.setattr(deployer, "unit_state", lambda _unit: core.unit)
    monkeypatch.setattr(deployer, "service", core.service)

    def host_connection(*, allow_stale: bool = False):
        if core.unit != "active":
            if allow_stale:
                return None
            raise RuntimeError("Core runtime discovery names a stale process")
        return core.identity()

    def service_identity():
        if core.unit != "active":
            raise RuntimeError("The supervisor Core service must be active")
        return {"main_pid": core.pid, "main_process_birth": f"birth-{core.pid}"}

    def require_process(service, runtime, version):
        if (
            runtime.get("pid") != service.get("main_pid")
            or core.host_version != version
        ):
            raise RuntimeError("Discovered Core process is not the accepted release")

    monkeypatch.setattr(deployer, "host_connection", host_connection)
    monkeypatch.setattr(deployer, "supervisor_service_identity", service_identity)
    monkeypatch.setattr(deployer, "supervisor_service_configuration", lambda: {})
    monkeypatch.setattr(deployer, "require_runtime_process_in_service", require_process)
    clock = [0.0]

    def monotonic() -> float:
        clock[0] += 1
        return clock[0]

    monkeypatch.setattr(
        deployer, "time", SimpleNamespace(monotonic=monotonic, sleep=lambda _s: None)
    )
    deployer.atomic_json(
        deployer.STATE,
        {
            "deployment_mode": "supervisor",
            "active": OLD,
            "host_active": OLD,
            "previous": "1" * 40,
            "pending": NEW,
        },
    )
    return core


def promote_until_converged(core: Host, limit: int = 10) -> list[dict]:
    results = []
    for _ in range(limit):
        results.append(deployer.promote())
        if results[-1]["status"] == "promoted":
            return results
    raise AssertionError(f"promotion did not converge: {results[-1]}")


def rollback_until_converged(core: Host, limit: int = 10) -> list[dict]:
    results = []
    for _ in range(limit):
        results.append(deployer.rollback())
        if results[-1]["status"] == "promoted":
            return results
    raise AssertionError(f"rollback did not converge: {results[-1]}")


def test_promotion_resumes_after_api_promotion_and_between_handoffs(host: Host):
    host.verify_handoffs = False
    first = deployer.promote()
    assert first["status"] == "waiting_for_handoffs"
    assert host.status()["api_version"] == NEW and host.host_version == NEW
    # Handoff verification is asynchronous; every rerun resumes the journal.
    second = deployer.promote()
    assert second["status"] == "waiting_for_handoffs"
    host.verify_handoffs = True
    promote_until_converged(host)
    assert host.owners() == {"project-1": NEW, "project-2": NEW}
    assert host.restarts == 1
    assert len(host.actions("promote_api")) == 1
    assert len(host.actions("prepare_execution")) == 2
    saved = deployer.state()
    assert saved["runtime_deployment"]["state"] == "complete"
    assert saved["active"] == NEW and saved["previous"] == OLD
    assert saved["pending"] is None


def test_activate_host_after_api_promotion_reads_back_the_recorded_activation(
    host: Host,
):
    host.verify_handoffs = False
    assert deployer.promote()["status"] == "waiting_for_handoffs"
    activation = deployer.activate_host()
    assert activation["status"] == "host_activated"
    assert activation["version"] == NEW
    assert activation["api_version"] == OLD  # recorded before the restart
    assert activation["current_readback"]["api_version"] == NEW
    assert host.restarts == 1


def test_code_rollback_after_completed_promotion_runs_to_completion(host: Host):
    promote_until_converged(host)
    rollback_until_converged(host)
    assert host.host_version == OLD
    assert host.status()["api_version"] == OLD
    assert host.target == OLD
    assert host.owners() == {"project-1": OLD, "project-2": OLD}
    assert host.restarts == 2
    saved = deployer.state()
    assert saved["runtime_deployment"]["action"] == "rollback"
    assert saved["runtime_deployment"]["state"] == "complete"
    assert saved["active"] == OLD


def test_rollback_reruns_resume_after_api_promotion(host: Host):
    promote_until_converged(host)
    host.verify_handoffs = False
    first = deployer.rollback()
    assert first["status"] == "waiting_for_handoffs"
    assert deployer.rollback()["status"] == "waiting_for_handoffs"
    host.verify_handoffs = True
    rollback_until_converged(host)
    assert host.owners() == {"project-1": OLD, "project-2": OLD}
    assert host.restarts == 2


def test_rollback_supersedes_a_promotion_stalled_mid_handoff(host: Host):
    host.verify_handoffs = False
    assert deployer.promote()["status"] == "waiting_for_handoffs"
    host.verify_handoffs = True
    rollback_until_converged(host)
    assert host.host_version == OLD
    assert host.status()["api_version"] == OLD and host.target == OLD
    assert host.owners() == {"project-1": OLD, "project-2": OLD}
    saved = deployer.state()
    rollback = saved["runtime_deployment"]
    assert rollback["action"] == "rollback" and rollback["version"] == OLD
    assert rollback["state"] == "complete"
    assert rollback["superseded"]["action"] == "promote"
    assert rollback["superseded"]["version"] == NEW
    assert saved["active"] == OLD


def test_rollback_supersedes_a_promotion_stalled_before_api_promotion(
    host: Host,
):
    host.faults["prepare_api"] = ["uncertain"]
    with pytest.raises(RuntimeError, match="uncertain"):
        deployer.promote()
    assert host.host_version == NEW and host.status()["api_version"] == OLD
    rollback_until_converged(host)
    assert host.host_version == OLD and host.restarts == 2
    assert host.status()["api_version"] == OLD and host.target == OLD
    assert host.owners() == {"project-1": OLD, "project-2": OLD}


def test_rollback_supersedes_a_promotion_that_never_activated_its_host(
    host: Host,
):
    host.faults["migrate"] = ["rejected"]
    with pytest.raises(RuntimeError, match="failed"):
        deployer.promote()
    assert host.host_version == OLD
    results = rollback_until_converged(host)
    assert host.restarts == 0
    assert host.status()["api_version"] == OLD and host.target == OLD
    assert host.owners() == {"project-1": OLD, "project-2": OLD}
    activation = deployer.state()["runtime_deployment"]["host_activation"]
    assert activation["result"]["already_active"] is True
    assert results[-1]["status"] == "promoted"


def test_failed_operation_is_asked_again_on_the_next_run(host: Host):
    host.faults["prepare_api"] = ["rejected"]
    with pytest.raises(RuntimeError, match="failed"):
        deployer.promote()
    attempts = host.actions("prepare_api")
    assert len(attempts) == 1
    promote_until_converged(host)
    attempts = host.actions("prepare_api")
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    step = deployer.state()["runtime_deployment"]["steps"]["api"]
    assert step["receipt"]["state"] == "succeeded"
    assert step["attempts"][0]["receipt"]["state"] == "failed"


def test_changed_revision_is_read_again_within_the_same_run(host: Host):
    host.faults["promote_api"] = ["changed", "changed"]
    promote_until_converged(host)
    assert len(host.actions("promote_api")) == 3
    assert host.status()["api_version"] == NEW


def test_uncertain_promotion_with_observed_effect_is_settled_without_resending(
    host: Host,
):
    host.faults["promote_api"] = ["uncertain_after_effect"]
    with pytest.raises(RuntimeError, match="uncertain"):
        deployer.promote()
    assert host.status()["api_version"] == NEW
    promote_until_converged(host)
    assert len(host.actions("promote_api")) == 1
    step = deployer.state()["runtime_deployment"]["steps"]["promote-api"]
    assert step["receipt"]["state"] == "uncertain"
    assert step["settled"]["evidence"]["api_version"] == NEW


def test_uncertain_operation_without_effect_is_asked_again(host: Host):
    host.faults["migrate"] = ["uncertain"]
    with pytest.raises(RuntimeError, match="uncertain"):
        deployer.promote()
    promote_until_converged(host)
    assert len(host.actions("migrate")) == 2
    assert host.status()["api_version"] == NEW


def test_uncertain_handoff_is_asked_again_only_when_status_proves_no_effect(
    host: Host,
):
    host.faults["handoff"] = ["uncertain"]
    first = deployer.promote()
    assert first["status"] == "waiting_for_handoffs"
    assert len(host.actions("prepare_execution")) == 1
    promote_until_converged(host)
    # The prepared target is reused; no second worker is started for it.
    assert len(host.actions("prepare_execution")) == 2
    assert len(host.actions("handoff")) == 3
    assert host.owners() == {"project-1": NEW, "project-2": NEW}


def test_uncertain_prepared_worker_is_never_started_again(host: Host):
    host.faults["prepare_execution"] = ["uncertain"]
    result = deployer.promote()
    assert result["status"] == "waiting_for_handoffs"
    assert deployer.promote()["status"] == "waiting_for_handoffs"
    assert len(host.actions("prepare_execution")) == 1
    detail = json.dumps(deployer.promote()["pending"])
    assert "prepare_execution" in detail


def test_uncertain_worker_start_without_a_process_is_asked_again(host: Host):
    host.lists_processes = True
    host.faults["prepare_execution"] = ["uncertain"]
    assert deployer.promote()["status"] == "waiting_for_handoffs"
    promote_until_converged(host)
    assert len(host.actions("prepare_execution")) == 3
    assert host.owners() == {"project-1": NEW, "project-2": NEW}
    step = next(
        entry
        for key, entry in deployer.state()["runtime_deployment"]["steps"].items()
        if key.startswith("worker/") and entry.get("attempts")
    )
    assert step["attempts"][0]["receipt"]["state"] == "uncertain"


def test_uncertain_worker_start_with_an_unowned_process_waits(host: Host):
    host.lists_processes = True
    host.faults["prepare_execution"] = ["uncertain_after_effect"]
    for _ in range(3):
        result = deployer.promote()
        assert result["status"] == "waiting_for_handoffs"
    assert len(host.actions("prepare_execution")) == 1
    assert "prepare_execution" in json.dumps(result["pending"])


def test_uncertain_retirement_is_settled_when_status_lists_no_process(host: Host):
    host.lists_processes = True
    host.faults["retire"] = ["uncertain_after_effect"]
    assert deployer.promote()["status"] == "waiting_for_handoffs"
    promote_until_converged(host)
    retired = [
        params["runtime_id"] for _, name, params in host.dispatched if name == "retire"
    ]
    assert sorted(retired) == ["exec-old-1", "exec-old-2"]
    steps = deployer.state()["runtime_deployment"]["steps"]
    settled = [
        entry
        for key, entry in steps.items()
        if key.startswith("retire/") and "settled" in entry
    ]
    assert settled and settled[0]["settled"]["evidence"]["state"] == "retired"


def test_uncertain_retirement_without_a_listing_is_asked_again(host: Host):
    host.faults["retire"] = ["uncertain"]
    assert deployer.promote()["status"] == "waiting_for_handoffs"
    promote_until_converged(host)
    retired = [
        params["runtime_id"] for _, name, params in host.dispatched if name == "retire"
    ]
    assert sorted(retired) == ["exec-old-1", "exec-old-1", "exec-old-2"]
    assert host.processes["exec-old-1"]["state"] == "retired"


def test_refused_handoff_gets_a_new_target_on_a_later_run(host: Host):
    host.faults["handoff"] = ["rejected"]
    first = deployer.promote()
    assert first["status"] == "waiting_for_handoffs"
    assert [row["project_id"] for row in first["pinned"]] == ["project-1"]
    promote_until_converged(host)
    assert host.owners() == {"project-1": NEW, "project-2": NEW}
    # The refused target was retired; a newly prepared one took the project.
    first_target = host.ledger[host.actions("prepare_execution")[0]]["result"]
    assert len(host.actions("prepare_execution")) == 3
    assert host.processes[first_target["runtime_id"]]["state"] == "retired"


def test_rollback_journal_without_migration_passes_host_receipts(
    host: Host, tmp_path: Path
):
    record = {
        "runtime_deployment": {
            "id": "d" * 32,
            "action": "rollback",
            "version": OLD,
            "steps": {
                "stage": {
                    "operation_id": "d" * 32 + ":stage",
                    "action": "stage",
                    "parameters": {"manifest_sha256": digest(OLD)},
                    "receipt": {"state": "succeeded"},
                }
            },
        }
    }
    operations = deployer.require_stage_migration_receipts(
        record, version=OLD, manifest_sha256=digest(OLD)
    )
    assert operations["migration_operation_id"] is None
    assert operations["stage_operation_id"] == "d" * 32 + ":stage"
