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
OTHER = "4" * 40
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
        self.migrated = False

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
        if path.startswith("deployment/migrations/"):
            version = path.rsplit("/", 1)[1]
            return {
                "version": version,
                "target_head": "target-head",
                "current_head": "target-head" if self.migrated else "old-head",
            }
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
            self.migrated = True
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

    def unowned_workers(self) -> set[str]:
        owners = {row["runtime_id"] for row in self.projects.values()}
        return {
            runtime_id
            for runtime_id, row in self.processes.items()
            if row["role"] == "execution"
            and row["state"] != "retired"
            and runtime_id not in owners
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
    monkeypatch.setattr(deployer, "wait_quiet_restart_boundary", lambda: {})

    def deployment_post(path, parameters):
        if path.endswith("/reconcile"):
            original = core.get(path.removesuffix("/reconcile"))
            if original["action"] == "migrate" and core.migrated:
                return {
                    **original,
                    "state": "succeeded",
                    "result": {"version": NEW, "state": "current"},
                }
            return original
        return core.post(path, parameters)

    monkeypatch.setattr(deployer, "deployment_post", deployment_post)
    monkeypatch.setattr(
        deployer,
        "reconcile_operation",
        lambda operation_id: deployment_post(
            "deployment/operations/" + operation_id + "/reconcile", {}
        ),
    )
    monkeypatch.setattr(
        deployer,
        "migration_status",
        lambda version: core.get("deployment/migrations/" + version),
    )
    monkeypatch.setattr(
        deployer,
        "runtime_release",
        lambda version: (releases / version, digest(version)),
    )
    monkeypatch.setattr(deployer, "runtime_package_version", package)
    monkeypatch.setattr(deployer, "unit_state", lambda _unit: core.unit)
    monkeypatch.setattr(deployer, "service", core.service)
    monkeypatch.setattr(
        deployer, "stop_supervisor_service", lambda _identity: core.service("stop")
    )

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


def test_degraded_host_activation_then_resume_holds_execution_target(
    host: Host, monkeypatch: pytest.MonkeyPatch
):
    """A degraded host preflight can be explicitly accepted without targeting execution."""
    host.processes["exec-prior-1"] = {
        "version": OTHER,
        "role": "execution",
        "state": "ready",
    }
    host.processes["exec-prior-2"] = {
        "version": OLD,
        "role": "execution",
        "state": "ready",
    }
    host.projects["project-1"]["runtime_id"] = "exec-prior-1"
    host.target = OLD
    original_status = host.status

    def degraded_status() -> dict:
        status = original_status()
        status["health"] = "degraded"
        return status

    monkeypatch.setattr(host, "status", degraded_status)
    monkeypatch.setattr(deployer, "supervisor_service_configuration", lambda: {})

    with pytest.raises(RuntimeError, match="degraded baseline was explicitly accepted"):
        deployer.promote()
    first = deployer.state()["runtime_deployment"]
    assert set(first["steps"]) == {"stage", "migrate"}
    assert host.host_version == OLD and host.target == OLD
    assert host.restarts == 0

    monkeypatch.setattr(deployer, "wait_quiet_restart_boundary", lambda: {})
    activation = deployer.activate_host(allow_degraded_workers=True)
    assert activation["status"] == "host_activated"
    assert activation["version"] == NEW
    assert host.restarts == 1 and host.target == OLD

    result = deployer.promote()
    assert result["status"] == "api_promoted_execution_staged"
    assert result["commit"] == NEW
    assert host.host_version == NEW
    assert host.status()["api_version"] == NEW
    assert host.status()["target_execution_version"] == OLD
    assert not host.actions("target_execution")
    assert deployer.state()["pending"] == NEW


def _superseded_manual_fixture(host: Host, monkeypatch: pytest.MonkeyPatch):
    manual = "2" * 40
    deployment_id = "a" * 32
    manual_prefix = "20261116-22222222"
    manual_release = host.releases / manual
    manual_release.mkdir()
    bundle_manifest = manual_release / "dist/nebula-runtime/nebula-runtime.json"
    bundle_manifest.parent.mkdir(parents=True)
    bundle_manifest.write_text(json.dumps({"version": package(manual)}))
    bundle_manifest_hash = hashlib.sha256(bundle_manifest.read_bytes()).hexdigest()
    manifest_path = manual_release / "nebula-runtime.json"
    manifest_path.write_text(
        json.dumps({
            "version": manual,
            "schema_family": SCHEMA,
            "migration_head": "head",
            "files": {"dist/nebula-runtime/nebula-runtime.json": bundle_manifest_hash},
        })
    )
    executable = manual_release / "dist/nebula-core"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"manual core executable")
    manual_manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        deployer, "runtime_release", lambda version: (host.releases / version, digest(version))
    )
    monkeypatch.setattr(deployer, "runtime_package_version", package)
    host.host_version = manual
    host.unit = "active"
    host.revision += 1
    host.installed.add(manual)
    host.processes = {
        "api-manual": {"version": manual, "role": "api", "state": "ready"},
        "exec-prior-1": {"version": OTHER, "role": "execution", "state": "ready"},
        "exec-prior-2": {"version": OLD, "role": "execution", "state": "ready"},
    }
    host.api_runtime = "api-manual"
    host.projects = {
        "project-1": {"runtime_id": "exec-prior-1", "fencing_token": 3},
        "project-2": {"runtime_id": "exec-prior-2", "fencing_token": 5},
    }
    host.target = OTHER
    host.lists_processes = True
    monkeypatch.setattr(deployer, "unit_state", lambda _unit: host.unit)
    monkeypatch.setattr(deployer, "current_release_version", lambda: manual)
    monkeypatch.setattr(deployer, "host_connection", lambda **_kwargs: host.identity())
    monkeypatch.setattr(
        deployer,
        "supervisor_service_identity",
        lambda: {
            "main_pid": host.pid,
            "main_process_birth": f"birth-{host.pid}",
            "exec_start": f"{manual_release}/dist/nebula-core",
        },
    )
    monkeypatch.setattr(
        deployer,
        "linux_process_snapshot",
        lambda pid: {
            "pid": pid,
            "parent_pid": 0,
            "process_birth": f"birth-{pid}",
            "effective_uid": deployer.os.getuid(),
            "executable": executable.resolve(),
        }
        if pid == host.pid
        else None,
    )
    proc_root = Path(deployer.STATE).parent / "proc"
    live_executable = proc_root / str(host.pid) / "exe"
    live_executable.parent.mkdir(parents=True)
    live_executable.symlink_to(executable)
    monkeypatch.setattr(deployer, "PROC_ROOT", proc_root)
    monkeypatch.setattr(deployer, "read_supervisor_health", lambda: (host.health(), host.status()))

    journal_steps = {}
    receipts = {}
    for step, action, parameters, receipt_state, result in (
        ("stage", "stage", {"source": str(host.releases / NEW), "manifest_sha256": digest(NEW)}, "succeeded", {"version": NEW, "manifest_sha256": digest(NEW)}),
        ("migrate", "migrate", {"version": NEW, "expected_revision": 40}, "succeeded", {"version": NEW, "state": "current", "target_head": "head", "current_head": "head"}),
        ("api", "prepare_api", {"version": NEW}, "succeeded", {"version": NEW, "runtime_id": "api-new", "role": "api"}),
        ("promote-api", "promote_api", {"runtime_id": "api-new", "expected_revision": 41}, "uncertain", {"error_type": "ValueError", "detail": "reply lost"}),
        ("target", "target_execution", {"version": NEW}, "uncertain", {"error_type": "ValueError", "detail": "reply lost"}),
    ):
        operation_id = f"{deployment_id}:{step}"
        receipt = {"operation_id": operation_id, "action": action, "state": receipt_state, "result": result}
        receipts[operation_id] = receipt
        entry = {"operation_id": operation_id, "action": action, "parameters": parameters, "receipt": receipt}
        if step in {"promote-api", "target"}:
            field = "api_version" if step == "promote-api" else "target_execution_version"
            entry["settled"] = {"by": "deployment_status", "evidence": {field: NEW}, "result": {field: NEW}}
        journal_steps[step] = entry
    deployment_journal = {
        "id": deployment_id,
        "action": "promote",
        "version": NEW,
        "previous": OLD,
        "state": "running",
        "steps": journal_steps,
        "host_activation": {
            "version": NEW,
            "operation_id": deployment_id + ":host-activation",
            "state": "complete",
            "result": {"operation_id": deployment_id + ":host-activation", "status": "host_activated", "version": NEW, "health_commit": NEW},
        },
    }
    for operation_id, receipt in receipts.items():
        host.ledger[operation_id] = {**receipt, "request_hash": "preserved"}
    legacy = {"operation_id": "a781:api:reserve-retry", "state": "uncertain", "result": {"error_type": "CoordinationConflict"}}
    state = {
        "deployment_mode": "supervisor",
        "active": OLD,
        "host_active": OLD,
        "previous": "1" * 40,
        "pending": None,
        "runtime_deployment": deployment_journal,
        "deployment_attempt_976_root_manifest": {"operation": legacy},
        "operator_disabled": True,
    }
    deployer.atomic_json(deployer.STATE, state)

    def manual_operation(action: str, suffix: str, request_parameters: dict, result: dict):
        operation_id = "manual:" + suffix
        receipt = {"operation_id": operation_id, "action": action, "state": "succeeded", "result": result}
        host.ledger[operation_id] = {**receipt, "request_hash": "manual"}
        return {"request": {"operation_id": operation_id, "action": action, "parameters": request_parameters}, "receipt": receipt}

    stage = manual_operation("stage", "stage", {"source": str(manual_release), "manifest_sha256": manual_manifest_hash}, {"version": manual, "manifest_sha256": manual_manifest_hash})
    migration = manual_operation("migrate", "migrate", {"version": manual}, {"version": manual, "state": "current", "target_head": "head", "current_head": "head"})
    prepare = manual_operation("prepare_api", "prepare", {"version": manual}, {"version": manual, "runtime_id": "api-manual", "role": "api"})
    promote = manual_operation("promote_api", "promote", {"runtime_id": "api-manual"}, {"api_version": manual})
    prefix_dir = Path(deployer.STATE).parent / "deployment-records"
    prefix_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(deployer, "DEPLOYMENT_RECORDS", prefix_dir)
    records = {
        "stage": stage,
        "migrate": migration,
        "host-switch-manual": {
            "phase": "complete", "version": manual, "previous_version": NEW,
            "operation_id": "host-manual", "readback": {
                "health_commit": manual, "api_version": manual,
                "runtime_package_version": package(manual), "ui_commit": manual,
                "owner_changes": {},
            },
        },
        "promote-api-manual": {
            "version": manual, "prepared_runtime_id": "api-manual",
            "steps": {"prepare-api": prepare, "promote-api": promote},
            "promoted_readback": {"api_version": manual},
        },
        "final-readback": {
            "target_commit": manual,
            "health": {"status": "ok", "database": "ok", "commit": manual},
            "deployment": {"api_version": manual, "health": "ready"},
        },
    }
    for name, record in records.items():
        (prefix_dir / f"{manual_prefix}-{name}.json").write_text(json.dumps(record))
    return deployment_id, manual_prefix, manual, legacy


def test_reconcile_superseded_manual_archives_full_uncertain_journal_and_aligns(
    host: Host, monkeypatch: pytest.MonkeyPatch
):
    deployment_id, prefix, manual, legacy = _superseded_manual_fixture(host, monkeypatch)
    original_journal = deployer.state()["runtime_deployment"]
    state_before = Path(deployer.STATE).read_bytes()
    dry_run = deployer.reconcile_superseded_manual_deployment(
        deployment_id, manual_prefix=prefix, dry_run=True
    )
    assert dry_run["status"] == "dry_run_validated"
    assert dry_run["proposed"]["classification"] == "superseded_by_manual_deployment"
    assert dry_run["proposed"]["manual_release"] == manual
    assert dry_run["proposed"]["uncertain_effects_preserved"] == [
        "promote-api",
        "target",
    ]
    assert Path(deployer.STATE).read_bytes() == state_before
    result = deployer.reconcile_superseded_manual_deployment(
        deployment_id, manual_prefix=prefix
    )
    assert result["status"] == "reconciled_superseded"
    saved = deployer.state()
    archived = saved["runtime_deployment_history"][0]
    assert archived["journal"] == original_journal
    assert archived["uncertain_effects_preserved"] == ["promote-api", "target"]
    assert archived["core_receipts"]["target"]["state"] == "uncertain"
    assert "runtime_deployment" not in saved
    assert saved["active"] == saved["host_active"] == manual
    assert saved["canonical_runtime_version"] == OTHER
    assert saved["deployment_attempt_976_root_manifest"]["operation"] == legacy
    assert saved["operator_disabled"] is True
    live_readback = archived["live_readback"]["host_executable"]
    assert live_readback["path"] == str(host.releases / manual / "dist/nebula-core")
    assert live_readback["sha256"] == hashlib.sha256(b"manual core executable").hexdigest()
    assert live_readback["identity_source"] == "current_manual_release_and_live_process_image"


def test_reconcile_superseded_manual_accepts_manual_binary_without_acceptance_receipt(
    host: Host, monkeypatch: pytest.MonkeyPatch
):
    deployment_id, prefix, manual, _legacy = _superseded_manual_fixture(host, monkeypatch)
    executable = host.releases / manual / "dist/nebula-core"
    assert not (host.releases / manual / "simulation-acceptance.json").exists()
    result = deployer.reconcile_superseded_manual_deployment(
        deployment_id, manual_prefix=prefix, dry_run=True
    )
    identity = result["proposed"]["live_readback"]["host_executable"]
    assert identity["path"] == str(executable)
    assert identity["sha256"] == hashlib.sha256(executable.read_bytes()).hexdigest()


@pytest.mark.parametrize("failure", ["path", "digest"])
def test_reconcile_superseded_manual_refuses_live_executable_mismatch(
    host: Host, monkeypatch: pytest.MonkeyPatch, failure: str
):
    deployment_id, prefix, manual, _legacy = _superseded_manual_fixture(host, monkeypatch)
    executable = host.releases / manual / "dist/nebula-core"
    live_executable = deployer.PROC_ROOT / str(host.pid) / "exe"
    if failure == "path":
        other = executable.parent / "other-core"
        other.write_bytes(b"other core executable")
        live_executable.unlink()
        live_executable.symlink_to(other)
    else:
        original_sha256 = deployer.sha256

        def wrong_live_digest(path: Path) -> str:
            if path == live_executable:
                return "0" * 64
            return original_sha256(path)

        monkeypatch.setattr(deployer, "sha256", wrong_live_digest)

    with pytest.raises(RuntimeError, match="process image differs"):
        deployer.reconcile_superseded_manual_deployment(
            deployment_id, manual_prefix=prefix, dry_run=True
        )


def test_reconcile_superseded_manual_requires_explicit_degraded_acceptance(
    host: Host, monkeypatch: pytest.MonkeyPatch
):
    deployment_id, prefix, _manual, _legacy = _superseded_manual_fixture(host, monkeypatch)
    original_status = host.status

    def degraded_status() -> dict:
        status = original_status()
        status["health"] = "degraded"
        return status

    monkeypatch.setattr(host, "status", degraded_status)
    monkeypatch.setattr(deployer, "read_supervisor_health", lambda: (host.health(), host.status()))
    with pytest.raises(RuntimeError, match="Current authenticated Core health"):
        deployer.reconcile_superseded_manual_deployment(
            deployment_id, manual_prefix=prefix
        )
    result = deployer.reconcile_superseded_manual_deployment(
        deployment_id, manual_prefix=prefix, allow_degraded_baseline=True
    )
    assert result["status"] == "reconciled_superseded"
    assert result["deployment"]["live_readback"]["degraded_baseline_accepted"] is True


@pytest.mark.parametrize(
    "unsafe",
    ["old_resident", "old_owner", "settlement", "api_mismatch", "malformed_readback"],
)
def test_reconcile_superseded_manual_refuses_unproven_supersession(
    host: Host, monkeypatch: pytest.MonkeyPatch, unsafe: str
):
    deployment_id, prefix, _manual, _legacy = _superseded_manual_fixture(host, monkeypatch)
    if unsafe == "old_resident":
        host.processes["old-leftover"] = {"version": NEW, "role": "execution", "state": "ready"}
    elif unsafe == "old_owner":
        host.projects["project-1"]["runtime_id"] = "old-leftover"
        host.processes["old-leftover"] = {"version": NEW, "role": "execution", "state": "ready"}
    elif unsafe == "settlement":
        record = deployer.state()
        record["runtime_deployment"]["steps"]["target"]["settled"]["evidence"]["target_execution_version"] = OLD
        deployer.atomic_json(deployer.STATE, record)
    else:
        if unsafe == "api_mismatch":
            host.processes["api-old"] = {"version": OLD, "role": "api", "state": "ready"}
            host.api_runtime = "api-old"
        else:
            monkeypatch.setattr(deployer, "read_supervisor_health", lambda: (None, []))
    with pytest.raises(RuntimeError):
        deployer.reconcile_superseded_manual_deployment(
            deployment_id, manual_prefix=prefix
        )
    assert deployer.state()["runtime_deployment"]["id"] == deployment_id


def test_reconcile_superseded_manual_refuses_receipt_prefix_mismatch(
    host: Host, monkeypatch: pytest.MonkeyPatch
):
    deployment_id, prefix, _manual, _legacy = _superseded_manual_fixture(host, monkeypatch)
    wrong_prefix = "20261116-ffffffff"
    for path in Path(deployer.DEPLOYMENT_RECORDS).glob(f"{prefix}-*.json"):
        (Path(deployer.DEPLOYMENT_RECORDS) / path.name.replace(prefix, wrong_prefix, 1)).write_text(path.read_text())
    with pytest.raises(RuntimeError, match="later release"):
        deployer.reconcile_superseded_manual_deployment(
            deployment_id, manual_prefix=wrong_prefix
        )
    assert deployer.state()["runtime_deployment"]["id"] == deployment_id


@pytest.mark.parametrize(
    "inventory_field",
    ["resident_versions", "project_runtime_version", "process_version", "process_runtime_version"],
)
def test_reconcile_superseded_manual_refuses_old_runtime_package_identity(
    host: Host, monkeypatch: pytest.MonkeyPatch, inventory_field: str
):
    deployment_id, prefix, _manual, _legacy = _superseded_manual_fixture(host, monkeypatch)
    original_read = deployer.read_supervisor_health
    old_package_version = package(NEW)

    def read_with_old_package():
        health, status = original_read()
        status = json.loads(json.dumps(status))
        if inventory_field == "resident_versions":
            status["resident_versions"].append(old_package_version)
        elif inventory_field == "project_runtime_version":
            status["projects"][0]["runtime_version"] = old_package_version
        elif inventory_field == "process_version":
            status["processes"].append(
                {"runtime_id": "old-package", "version": old_package_version}
            )
        else:
            status["processes"].append(
                {
                    "runtime_id": "old-package-runtime-version",
                    "version": OTHER,
                    "runtime_version": old_package_version,
                }
            )
        return health, status

    monkeypatch.setattr(deployer, "read_supervisor_health", read_with_old_package)
    with pytest.raises(RuntimeError, match="still owns or runs"):
        deployer.reconcile_superseded_manual_deployment(
            deployment_id, manual_prefix=prefix
        )
    assert deployer.state()["runtime_deployment"]["id"] == deployment_id


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
    # The source the replaced promotion moved away from is retired too.
    assert host.unowned_workers() == set()


def test_rollback_retires_a_target_the_replaced_promotion_never_used(host: Host):
    host.faults["handoff"] = ["uncertain"]
    assert deployer.promote()["status"] == "waiting_for_handoffs"
    [prepared] = host.unowned_workers()
    with pytest.raises(RuntimeError, match="before rollback"):
        deployer.rollback()
    # The original operation finishes with a proven no-effect refusal.
    operation = host.actions("handoff")[0]
    host.ledger[operation]["state"] = "failed"
    rollback_until_converged(host)
    assert host.owners() == {"project-1": OLD, "project-2": OLD}
    assert host.processes[prepared]["state"] == "retired"
    assert host.unowned_workers() == set()


def test_rollback_supersedes_a_promotion_stalled_before_api_promotion(
    host: Host,
):
    host.faults["prepare_api"] = ["uncertain"]
    with pytest.raises(RuntimeError, match="uncertain"):
        deployer.promote()
    assert host.host_version == NEW and host.status()["api_version"] == OLD
    with pytest.raises(RuntimeError, match="before rollback"):
        deployer.rollback()
    host.ledger[host.actions("prepare_api")[0]]["state"] = "failed"
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


def test_uncertain_migration_without_effect_proof_stays_held(host: Host):
    host.faults["migrate"] = ["uncertain"]
    for _ in range(2):
        with pytest.raises(RuntimeError, match="uncertain"):
            deployer.promote()
    assert len(host.actions("migrate")) == 1
    assert host.status()["api_version"] == OLD


def test_uncertain_handoff_absence_does_not_prove_original_request_finished(host: Host):
    host.faults["handoff"] = ["uncertain"]
    for _ in range(3):
        assert deployer.promote()["status"] == "waiting_for_handoffs"
    assert len(host.actions("handoff")) == 1


def test_uncertain_prepared_worker_is_never_started_again(host: Host):
    host.faults["prepare_execution"] = ["uncertain"]
    result = deployer.promote()
    assert result["status"] == "waiting_for_handoffs"
    assert deployer.promote()["status"] == "waiting_for_handoffs"
    assert len(host.actions("prepare_execution")) == 1
    detail = json.dumps(deployer.promote()["pending"])
    assert "prepare_execution" in detail


def test_uncertain_worker_start_absence_keeps_original_request_held(host: Host):
    host.lists_processes = True
    host.faults["prepare_execution"] = ["uncertain"]
    for _ in range(3):
        assert deployer.promote()["status"] == "waiting_for_handoffs"
    assert len(host.actions("prepare_execution")) == 1


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


def test_uncertain_retirement_without_a_listing_stays_held(host: Host):
    host.faults["retire"] = ["uncertain"]
    for _ in range(3):
        result = deployer.promote()
        assert result["status"] == "waiting_for_handoffs"
    assert len(host.actions("retire")) == 1
    assert "uncertain" in json.dumps(result["pending"])


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


def test_uncertain_migration_settles_only_after_exact_head_readback(host: Host):
    host.faults["migrate"] = ["uncertain_after_effect"]
    with pytest.raises(RuntimeError, match="uncertain"):
        deployer.promote()
    promote_until_converged(host)
    assert len(host.actions("migrate")) == 1
    step = deployer.state()["runtime_deployment"]["steps"]["migrate"]
    assert step["receipt"]["state"] == "succeeded"


def test_later_success_receipt_replaces_unknown_before_retry(host: Host):
    host.faults["migrate"] = ["uncertain_after_effect"]
    with pytest.raises(RuntimeError, match="uncertain"):
        deployer.promote()
    operation_id = host.actions("migrate")[0]
    host.ledger[operation_id]["state"] = "succeeded"
    host.ledger[operation_id]["result"] = {"version": NEW, "state": "migrated"}
    promote_until_converged(host)
    assert len(host.actions("migrate")) == 1


@pytest.mark.parametrize("action", ["migrate", "prepare_api", "prepare_execution"])
def test_rollback_waits_for_inflight_promotion_operation(host: Host, action):
    host.faults[action] = ["uncertain"]
    try:
        deployer.promote()
    except RuntimeError:
        pass
    before = len(host.dispatched)
    with pytest.raises(RuntimeError, match="before rollback"):
        deployer.rollback()
    assert len(host.dispatched) == before
    assert deployer.state()["runtime_deployment"]["action"] == "promote"


def _replace_verified_target(host):
    """A cleanly retired verified target is followed by a fresh same-release grant."""
    host.lists_processes = True
    host.verify_handoffs = False
    assert deployer.promote()["status"] == "waiting_for_handoffs"
    host.verify_handoffs = True
    host.status()
    host.verify_handoffs = False
    assert deployer.promote()["status"] == "waiting_for_handoffs"
    host.verify_handoffs = True
    record = deployer.state()
    journal = record["runtime_deployment"]
    project_id = "project-1"
    original_target = journal["target_runtime_by_project"][project_id]
    source = journal["source_runtime_by_project"][project_id]
    assert host.processes[source]["state"] == "retired"
    host.processes[original_target]["state"] = "retired"
    successor = "same-release-successor"
    host.processes[successor] = {
        "version": NEW,
        "role": "execution",
        "state": "ready",
        "ready_verified": True,
    }
    host.projects[project_id]["runtime_id"] = successor
    host.projects[project_id]["fencing_token"] += 1
    return project_id, original_target, source, successor, record


def test_verified_successor_resumes_without_retargeting_or_replaying_handoff(host):
    project_id, original_target, source, successor, record = _replace_verified_target(
        host
    )
    original_steps = json.loads(json.dumps(record["runtime_deployment"]["steps"]))
    promote_until_converged(host)
    after = deployer.state()["runtime_deployment"]
    assert after["target_runtime_by_project"][project_id] == original_target
    assert host.projects[project_id]["runtime_id"] == successor
    assert after["reconciled_successors"][project_id]["runtime_id"] == successor
    for step, value in original_steps.items():
        assert after["steps"][step] == value
    calls = list(host.dispatched)
    assert deployer.promote()["status"] == "no_pending_candidate"
    assert host.dispatched == calls
    handoffs = [
        parameters
        for _, action, parameters in calls
        if action == "handoff" and parameters["project_id"] == project_id
    ]
    assert len(handoffs) == 1 and handoffs[0]["target_runtime_id"] == original_target
    assert host.processes[source]["state"] == "retired"


@pytest.mark.parametrize(
    "missing",
    [
        "inventory",
        "handoff",
        "source_retirement",
        "stale_fence",
        "target_alive",
        "source_owned",
        "not_ready",
        "wrong_release",
        "wrong_role",
        "unverified",
        "held",
        "blockers",
    ],
)
def test_successor_with_incomplete_proof_keeps_the_deployment_held(host, missing):
    project_id, original_target, source, successor, record = _replace_verified_target(
        host
    )
    if missing == "inventory":
        host.lists_processes = False
    elif missing in {"handoff", "source_retirement"}:
        for key, step in list(record["runtime_deployment"]["steps"].items()):
            parameters = step.get("parameters") or {}
            if (
                missing == "handoff"
                and step["action"] == "handoff"
                and parameters.get("project_id") == project_id
            ) or (
                missing == "source_retirement"
                and step["action"] == "retire"
                and parameters.get("runtime_id") == source
            ):
                del record["runtime_deployment"]["steps"][key]
        deployer.atomic_json(deployer.STATE, record)
    elif missing == "stale_fence":
        host.projects[project_id]["fencing_token"] = (
            record["runtime_deployment"]["source_fencing_by_project"][project_id] + 1
        )
    elif missing == "target_alive":
        host.processes[original_target]["state"] = "ready"
    elif missing == "source_owned":
        host.projects["another-project"] = {"runtime_id": source, "fencing_token": 1}
    elif missing == "not_ready":
        host.processes[successor]["ready_verified"] = False
    elif missing == "wrong_release":
        host.processes[successor]["version"] = OLD
    elif missing == "wrong_role":
        host.processes[successor]["role"] = "api"
    elif missing == "unverified":
        host.handoffs[project_id]["phase"] = "transferred"
    else:
        original_status = host.status

        def changed_status():
            value = original_status()
            row = next(
                row for row in value["projects"] if row["project_id"] == project_id
            )
            row["admission_held"] = missing == "held"
            row["blockers"] = (
                ["Synthetic unresolved blocker"] if missing == "blockers" else []
            )
            return value

        host.status = changed_status
    calls = list(host.dispatched)
    result = deployer.promote()
    assert result["status"] == "waiting_for_handoffs"
    assert host.dispatched == calls
    assert (
        deployer.state()["runtime_deployment"]["target_runtime_by_project"][project_id]
        == original_target
    )


@pytest.mark.parametrize("retirement_proven", [True, False])
def test_successor_reads_original_retirement_receipt_before_holding(
    host, monkeypatch, retirement_proven
):
    project_id, original_target, source, successor, record = _replace_verified_target(
        host
    )
    step = next(
        step
        for step in record["runtime_deployment"]["steps"].values()
        if step["action"] == "retire" and step["parameters"]["runtime_id"] == source
    )
    operation_id = step["operation_id"]
    original = {
        **host.ledger[operation_id],
        "state": "uncertain",
        "result": {"error_type": "CoordinationConflict"},
    }
    host.ledger[operation_id] = original
    step["receipt"] = {
        key: value for key, value in original.items() if key != "request_hash"
    }
    deployer.atomic_json(deployer.STATE, record)
    reads = []

    def readback(requested_id):
        reads.append(requested_id)
        assert requested_id == operation_id
        if retirement_proven:
            host.ledger[operation_id] = {
                **original,
                "state": "succeeded",
                "result": {
                    "runtime_id": source,
                    "state": "retired",
                    "reconciled": True,
                },
            }
        return {
            key: value
            for key, value in host.ledger[operation_id].items()
            if key != "request_hash"
        }

    monkeypatch.setattr(deployer, "reconcile_operation", readback)
    before = list(host.dispatched)
    if retirement_proven:
        promote_until_converged(host)
        saved = deployer.state()["runtime_deployment"]
        assert saved["reconciled_successors"][project_id]["runtime_id"] == successor
        assert saved["target_runtime_by_project"][project_id] == original_target
        assert (
            saved["steps"][
                next(
                    key
                    for key, value in saved["steps"].items()
                    if value["operation_id"] == operation_id
                )
            ]["receipt"]["state"]
            == "succeeded"
        )
    else:
        assert deployer.promote()["status"] == "waiting_for_handoffs"
        assert host.dispatched == before
    assert reads == [operation_id]
    assert len(host.actions("retire")) == len(
        [item for item in before if item[1] == "retire"]
    ) + (1 if retirement_proven else 0)
