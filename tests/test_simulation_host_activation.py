"""Focused guards for code-only simulation supervisor activation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts/simulation"
sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location(
    "simulation_nebula_deployer", SCRIPT_DIR / "nebula_deployer.py"
)
assert spec is not None and spec.loader is not None
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)
staged_spec = importlib.util.spec_from_file_location(
    "simulation_nebula_staged_deployer", SCRIPT_DIR / "nebula_staged_deployer.py"
)
assert staged_spec is not None and staged_spec.loader is not None
staged_module = importlib.util.module_from_spec(staged_spec)
staged_spec.loader.exec_module(staged_module)


def _status(*, runtime_id: str = "worker-1", fencing_token: int = 7) -> dict:
    return {
        "protocol": "nebula.runtime/v1",
        "health": "ready",
        "api_version": "a" * 40,
        "resident_versions": ["b" * 40],
        "projects": [
            {
                "project_id": "child-project",
                "runtime_id": runtime_id,
                "runtime_version": "b" * 40,
                "fencing_token": fencing_token,
                "admission_held": False,
            }
        ],
    }


def test_degraded_worker_snapshot_requires_explicit_acceptance() -> None:
    status = _status()
    status["health"] = "degraded"
    with pytest.raises(RuntimeError, match="degraded baseline"):
        deployer.host_status_snapshot(status)

    snapshot = deployer.host_status_snapshot(status, allow_degraded=True)
    assert snapshot["health"] == "degraded"
    assert snapshot["projects"]["child-project"]["runtime_id"] == "worker-1"


def test_degraded_worker_post_readback_requires_degraded_baseline_and_healthy_api_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit, api_version = "b" * 40, "a" * 40
    package_version = "3.0.0-beta.3+80d43d956ba75c35"
    process_birth = "linux:36155290-b5d2-43ea-9ee9-94c8b3220809:240197513"
    monkeypatch.setattr(
        deployer, "runtime_package_version", lambda _version: package_version
    )
    monkeypatch.setattr(deployer, "unit_state", lambda _unit: "active")
    monkeypatch.setattr(
        deployer,
        "host_connection",
        lambda **_kwargs: {
            "package_version": package_version,
            "pid": 434411,
            "process_birth": process_birth,
        },
    )
    monkeypatch.setattr(deployer, "supervisor_service_identity", lambda: {})
    monkeypatch.setattr(
        deployer, "require_runtime_process_in_service", lambda *_args: None
    )
    status = _status()
    status["health"] = "degraded"
    health = {
        "status": "ok",
        "database": "ok",
        "commit": commit,
        "recovery_version": package_version,
    }
    monkeypatch.setattr(deployer, "read_supervisor_health", lambda: (health, status))
    baseline = deployer.host_status_snapshot(status, allow_degraded=True)

    readback = deployer.wait_supervisor_host_ready(
        commit,
        expected_api_version=api_version,
        prior_status=baseline,
        allow_degraded_workers=True,
        degraded_baseline=True,
        timeout=1,
    )
    assert readback["deployment_health"] == "degraded"
    assert readback["degraded_baseline_accepted"] is True

    with pytest.raises(RuntimeError, match="ready supervisor API status"):
        deployer.wait_supervisor_host_ready(
            commit,
            expected_api_version=api_version,
            prior_status=None,
            allow_degraded_workers=True,
            degraded_baseline=False,
            timeout=1,
        )
    health["database"] = "unavailable"
    with pytest.raises(RuntimeError, match="within 0s"):
        deployer.wait_supervisor_host_ready(
            commit,
            expected_api_version=api_version,
            prior_status=baseline,
            allow_degraded_workers=True,
            degraded_baseline=True,
            timeout=0,
        )


def test_needs_attention_activation_retries_only_from_verified_previous_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous, candidate = "b" * 40, "c" * 40
    package_version = "3.0.0-beta.3+80d43d956ba75c35"
    prior = deployer.host_status_snapshot(_status())
    journal = {
        "state": "needs_attention",
        "previous_version": previous,
        "prior_status": prior,
    }
    record = {"host_activation": journal}
    monkeypatch.setattr(deployer, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(deployer, "current_release_version", lambda: previous)
    monkeypatch.setattr(deployer, "unit_state", lambda _unit: "active")
    monkeypatch.setattr(deployer, "runtime_package_version", lambda _version: package_version)
    monkeypatch.setattr(
        deployer,
        "host_connection",
        lambda **_kwargs: {"package_version": package_version, "pid": 123},
    )
    monkeypatch.setattr(deployer, "supervisor_service_identity", lambda: {})
    monkeypatch.setattr(
        deployer, "require_runtime_process_in_service", lambda *_args: None
    )
    health = {
        "status": "ok",
        "database": "ok",
        "commit": previous,
        "recovery_version": package_version,
    }
    monkeypatch.setattr(
        deployer,
        "read_supervisor_health",
        lambda: (health, _status()),
    )

    deployer.reconcile_attention_activation(
        record, journal, version=candidate, allow_degraded_workers=False
    )

    assert journal["state"] == "prepared"
    assert journal["needs_attention_reconciled"] == {
        "previous_version": previous,
        "recovery_version": package_version,
        "retained_project_owners": 1,
    }
    assert deployer.json.loads((tmp_path / "state.json").read_text())[
        "host_activation"
    ] == journal

    journal["state"] = "needs_attention"
    monkeypatch.setattr(
        deployer, "current_release_version", lambda: "d" * 40
    )
    with pytest.raises(RuntimeError, match="recorded active previous host"):
        deployer.reconcile_attention_activation(
            record, journal, version=candidate, allow_degraded_workers=False
        )


def test_host_activation_requires_exact_existing_worker_ownership() -> None:
    before = deployer.host_status_snapshot(_status())

    assert (
        deployer.require_preserved_workers(
            before, deployer.host_status_snapshot(_status())
        )
        == {"retained": 1}
    )
    with pytest.raises(RuntimeError, match="changed the existing execution owner"):
        deployer.require_preserved_workers(
            before, deployer.host_status_snapshot(_status(runtime_id="worker-2"))
        )
    with pytest.raises(RuntimeError, match="changed the existing execution owner"):
        deployer.require_preserved_workers(
            before, deployer.host_status_snapshot(_status(fencing_token=8))
        )
    unowned = _status()
    unowned["projects"][0].update(runtime_id=None, runtime_version=None)
    changed_unowned = _status()
    changed_unowned["projects"][0].update(runtime_id=None, runtime_version=None)
    changed_unowned["projects"][0]["fencing_token"] += 1
    with pytest.raises(RuntimeError, match="fencing token"):
        deployer.require_preserved_workers(
            deployer.host_status_snapshot(unowned),
            deployer.host_status_snapshot(changed_unowned),
        )
    with pytest.raises(RuntimeError, match="outside the exact admitted release"):
        deployer.require_preserved_workers(
            deployer.host_status_snapshot(unowned), deployer.host_status_snapshot(_status())
        )
    candidate = "c" * 40
    admitted_status = _status(runtime_id="new-worker")
    admitted_status["projects"][0]["runtime_version"] = candidate
    admitted_status["projects"][0]["fencing_token"] += 1
    admitted_status["resident_versions"].append(candidate)
    assert deployer.require_preserved_workers(
        deployer.host_status_snapshot(unowned),
        deployer.host_status_snapshot(admitted_status),
        admitted_version=candidate,
    ) == {"retained": 0}
    unexpected_status = _status(runtime_id="new-worker")
    unexpected_status["projects"][0]["runtime_version"] = candidate
    with pytest.raises(RuntimeError, match="outside the exact admitted release"):
        deployer.require_preserved_workers(
            deployer.host_status_snapshot(unowned),
            deployer.host_status_snapshot(unexpected_status),
            admitted_version=candidate,
        )
    stale_fence = _status(runtime_id="new-worker")
    stale_fence["projects"][0]["runtime_version"] = candidate
    with pytest.raises(RuntimeError, match="outside the exact admitted release"):
        deployer.require_preserved_workers(
            deployer.host_status_snapshot(unowned),
            deployer.host_status_snapshot(stale_fence),
            admitted_version=candidate,
        )


def test_package_version_discovery_binds_to_release_and_authenticated_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit, api_version = "b" * 40, "c" * 40
    package_version = "3.0.0-beta.3+80d43d956ba75c35"
    release = tmp_path / commit
    bundle_manifest_path = release / "dist/nebula-runtime/nebula-runtime.json"
    bundle_manifest_path.parent.mkdir(parents=True)
    bundle_manifest_path.parent.chmod(0o700)
    deployer.atomic_json(bundle_manifest_path, {"version": package_version})
    relative = "dist/nebula-runtime/nebula-runtime.json"
    deployer.atomic_json(
        release / "nebula-runtime.json",
        {"version": commit, "files": {relative: deployer.sha256(bundle_manifest_path)}},
    )
    monkeypatch.setattr(
        deployer,
        "runtime_release",
        lambda requested: (release, "d" * 64) if requested == commit else None,
    )
    assert deployer.runtime_package_version(commit) == package_version

    data_root = tmp_path / "state/v3"
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True)
    runtime.chmod(0o700)
    process_birth = "linux:36155290-b5d2-43ea-9ee9-94c8b3220809:240197513"
    deployer.atomic_json(
        runtime / "connection.json",
        {
            "protocol": "nebula-desktop-runtime-v1",
            "data_root": str(data_root.resolve()),
            "endpoint": "http://127.0.0.1:8000/api/v1",
            "pid": 434411,
            "process_birth": process_birth,
            "version": package_version,
            "startup_id": "startup-1",
        },
    )
    monkeypatch.setattr(deployer, "DATA", data_root)
    monkeypatch.setattr(
        deployer,
        "linux_process_birth",
        lambda pid: process_birth if pid == 434411 else None,
    )
    monkeypatch.setattr(deployer, "unit_state", lambda _unit: "active")
    monkeypatch.setattr(
        deployer, "supervisor_service_identity", lambda: {"main_pid": 434411}
    )
    monkeypatch.setattr(
        deployer, "require_runtime_process_in_service", lambda *_args: None
    )
    health = {
        "status": "ok",
        "database": "ok",
        "commit": commit,
        "recovery_version": package_version,
    }
    status = {
        "protocol": "nebula.runtime/v1",
        "health": "ready",
        "api_version": api_version,
        "resident_versions": [commit],
        "projects": [],
    }
    monkeypatch.setattr(deployer, "read_supervisor_health", lambda: (health, status))

    readback = deployer.wait_supervisor_host_ready(
        commit, expected_api_version=api_version, prior_status=None, timeout=1
    )

    assert readback["health_commit"] == commit
    assert readback["recovery_version"] == package_version
    assert readback["runtime_pid"] == 434411
    assert readback["runtime_process_birth"] == process_birth
    health["commit"] = "a" * 40
    with pytest.raises(RuntimeError, match="Core and recovery identities"):
        deployer.wait_supervisor_host_ready(
            commit, expected_api_version=api_version, prior_status=None, timeout=1
        )
    health["commit"] = commit
    health["recovery_version"] = "unselected-recovery"
    with pytest.raises(RuntimeError, match="Core and recovery identities"):
        deployer.wait_supervisor_host_ready(
            commit, expected_api_version=api_version, prior_status=None, timeout=1
        )

    deployer.atomic_json(bundle_manifest_path, {"version": "tampered-package"})
    with pytest.raises(RuntimeError, match="not hash-bound"):
        deployer.runtime_package_version(commit)


def test_host_process_identity_accepts_verified_onefile_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = "b" * 40
    release = tmp_path / version
    executable = release / "dist/nebula-core"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"immutable runtime executable")
    deployer.atomic_json(
        release / "simulation-acceptance.json",
        {"core_sha256": deployer.sha256(executable)},
    )
    monkeypatch.setattr(
        deployer, "runtime_release", lambda _version: (release, "d" * 64)
    )

    def snapshot(
        pid: int,
        parent_pid: int,
        birth: str,
        process_executable: Path,
    ) -> dict:
        return {
            "pid": pid,
            "parent_pid": parent_pid,
            "process_birth": birth,
            "effective_uid": deployer.os.getuid(),
            "executable": process_executable,
        }

    processes = {
        434314: snapshot(434314, 1113, "main-birth", executable),
        434411: snapshot(434411, 434314, "core-birth", executable),
        1113: snapshot(1113, 1, "ancestor-birth", tmp_path / "systemd"),
        1: snapshot(1, 0, "init-birth", tmp_path / "init"),
    }
    monkeypatch.setattr(deployer, "linux_process_snapshot", processes.__getitem__)
    unit = {
        "main_pid": 434314,
        "main_process_birth": "main-birth",
        "exec_start": "mercury-run /srv/apple-sim/nebula/current/dist/nebula-core",
    }
    monkeypatch.setattr(deployer, "supervisor_service_identity", lambda: unit)
    service = dict(unit)
    runtime = {"pid": 434411, "process_birth": "core-birth"}

    deployer.require_runtime_process_in_service(service, runtime, version)

    processes[434411]["parent_pid"] = 1113
    with pytest.raises(RuntimeError, match="not a unit MainPID descendant"):
        deployer.require_runtime_process_in_service(service, runtime, version)
    processes[434411]["parent_pid"] = 434314
    runtime["process_birth"] = "reused-pid"
    with pytest.raises(RuntimeError, match="birth identity changed"):
        deployer.require_runtime_process_in_service(service, runtime, version)
    runtime["process_birth"] = "core-birth"
    processes[434411]["executable"] = release / "different-core"
    with pytest.raises(RuntimeError, match="does not match the accepted onefile executable"):
        deployer.require_runtime_process_in_service(service, runtime, version)
    processes[434411]["executable"] = executable
    processes[434314]["executable"] = release / "different-parent"
    with pytest.raises(RuntimeError, match="does not match the accepted onefile executable"):
        deployer.require_runtime_process_in_service(service, runtime, version)


def test_host_readback_rejects_unverified_owner_release() -> None:
    before = deployer.host_status_snapshot(_status())
    after_status = _status()
    after_status["resident_versions"] = []
    after_status["projects"][0].update(runtime_id=None, runtime_version=None)

    after = deployer.host_status_snapshot(after_status)

    with pytest.raises(RuntimeError, match="changed the existing execution owner"):
        deployer.require_preserved_workers(before, after)
    after["projects"]["child-project"]["fencing_token"] += 1
    with pytest.raises(RuntimeError, match="changed the existing execution owner"):
        deployer.require_preserved_workers(before, after)


def test_host_activation_switches_only_current_code_and_records_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    releases = tmp_path / "releases"
    releases.mkdir()
    old = releases / ("a" * 40)
    new = releases / ("b" * 40)
    old.mkdir()
    new.mkdir()
    current = tmp_path / "current"
    current.symlink_to(old)
    monkeypatch.setattr(deployer, "RELEASES", releases)
    monkeypatch.setattr(deployer, "CURRENT", current)
    monkeypatch.setattr(deployer, "STATE", tmp_path / "state.json")

    unit = {"state": "active"}

    def service(action: str) -> None:
        unit["state"] = "inactive" if action == "stop" else "active"

    monkeypatch.setattr(deployer, "service", service)
    monkeypatch.setattr(deployer, "unit_state", lambda _name: unit["state"])
    monkeypatch.setattr(
        deployer,
        "wait_supervisor_host_ready",
        lambda version, **_kwargs: {
            "version": version,
            "health_commit": version,
            "api_version": "a" * 40,
            "retained_project_owners": 1,
        },
    )
    record = {"deployment_mode": "supervisor"}
    journal = {"operation_id": "op-1", "version": "b" * 40, "state": "prepared"}

    readback = deployer._switch_supervisor_host(
        "b" * 40,
        release=new,
        expected_api_version="a" * 40,
        prior_status=deployer.host_status_snapshot(_status()),
        journal=journal,
        record=record,
    )

    assert current.resolve() == new
    assert unit["state"] == "active"
    assert record["host_active"] == "b" * 40
    assert journal["state"] == "complete"
    assert journal["readback"] == readback
    assert record["host_activation"] is journal


def test_interrupted_activation_restarts_selected_release_without_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = "a" * 40
    version = "b" * 40
    api_version = "c" * 40
    releases = tmp_path / "releases"
    releases.mkdir()
    release = releases / version
    release.mkdir()
    (release / "nebula-runtime.json").write_text(
        '{"version": "' + version + '"}'
    )
    current = tmp_path / "current"
    current.symlink_to(release)
    monkeypatch.setattr(deployer, "RELEASES", releases)
    monkeypatch.setattr(deployer, "CURRENT", current)
    monkeypatch.setattr(deployer, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(
        deployer,
        "runtime_release",
        lambda _version: (release, "d" * 64),
    )
    monkeypatch.setattr(
        deployer,
        "runtime_package_version",
        lambda _version: "3.0.0-beta.3+80d43d956ba75c35",
    )
    monkeypatch.setattr(
        deployer, "require_stage_migration_receipts", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(deployer, "host_connection", lambda **_kwargs: None)
    monkeypatch.setattr(deployer, "current_release_version", lambda: version)
    monkeypatch.setattr(
        deployer, "supervisor_service_configuration", lambda: {"kill_mode": "process"}
    )
    unit = {"state": "inactive"}
    monkeypatch.setattr(deployer, "unit_state", lambda _name: unit["state"])
    monkeypatch.setattr(
        deployer,
        "service",
        lambda action: unit.update(state="active" if action == "start" else "inactive"),
    )
    monkeypatch.setattr(
        deployer,
        "wait_supervisor_host_ready",
        lambda target, **_kwargs: {
            "version": target,
            "health_commit": target,
            "recovery_version": target,
            "api_version": api_version,
            "retained_project_owners": 1,
        },
    )
    prior_status = deployer.host_status_snapshot(
        {
            "protocol": "nebula.runtime/v1",
            "health": "ready",
            "api_version": api_version,
            "resident_versions": [previous],
            "projects": [
                {
                    "project_id": "child-project",
                    "runtime_id": "worker-1",
                    "runtime_version": previous,
                    "fencing_token": 7,
                    "admission_held": False,
                }
            ],
        }
    )
    record = {
        "deployment_mode": "supervisor",
        "pending": version,
        "active": api_version,
        "runtime_deployment": {
            "action": "promote",
            "version": version,
            "host_activation": {
                "version": version,
                "operation_id": "activation-op",
                "state": "requested",
            },
        },
        "host_activation": {
            "operation_id": "activation-op",
            "state": "prepared",
            "version": version,
            "previous_version": previous,
            "prior_status": prior_status,
        },
    }

    result = deployer.activate_supervisor_host(record, version)

    assert result["operation_id"] == "activation-op"
    assert record["host_activation"]["state"] == "complete"
    assert record["host_active"] == version
    assert unit["state"] == "active"


def test_failed_host_activation_can_journal_code_only_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    releases = tmp_path / "releases"
    releases.mkdir()
    old = releases / ("a" * 40)
    new = releases / ("b" * 40)
    old.mkdir()
    new.mkdir()
    current = tmp_path / "current"
    current.symlink_to(new)
    monkeypatch.setattr(deployer, "RELEASES", releases)
    monkeypatch.setattr(deployer, "CURRENT", current)
    monkeypatch.setattr(deployer, "STATE", tmp_path / "state.json")
    unit = {"state": "active"}

    def service(action: str) -> None:
        unit["state"] = "inactive" if action == "stop" else "active"

    monkeypatch.setattr(deployer, "service", service)
    monkeypatch.setattr(deployer, "unit_state", lambda _name: unit["state"])
    monkeypatch.setattr(
        deployer,
        "wait_supervisor_host_ready",
        lambda version, **_kwargs: {"version": version, "api_version": "a" * 40},
    )
    record = {"deployment_mode": "supervisor"}
    journal = {"operation_id": "op-2", "state": "rolling_back"}
    rollback = {
        "operation_id": "op-2:rollback",
        "version": "a" * 40,
        "state": "rolling_back",
        "code_only": True,
    }

    deployer._switch_supervisor_host(
        "a" * 40,
        release=old,
        expected_api_version="a" * 40,
        prior_status=deployer.host_status_snapshot(_status()),
        journal=rollback,
        journal_container=journal,
        record=record,
    )

    assert current.resolve() == old
    assert journal["rollback"]["state"] == "complete"
    assert journal["rollback"]["code_only"] is True
    assert record["host_active"] == "a" * 40


def test_staged_promotion_journals_host_intent_before_callback() -> None:
    version = "b" * 40
    record = {"active": "a" * 40}
    events: list[str] = []
    durable_host_entries: list[dict] = []

    def get(_path: str) -> dict:
        return {
            "protocol": "nebula.runtime/v1",
            "revision": 9,
            "api_version": version,
            "resident_versions": ["a" * 40],
            "projects": [],
            "health": "ready",
        }

    def post(_path: str, request: dict) -> dict:
        result = {"runtime_id": "api-new"} if request["action"] == "prepare_api" else {}
        return {
            "operation_id": request["operation_id"],
            "state": "succeeded",
            "result": result,
        }

    def activate(target: str) -> dict:
        entry = record["runtime_deployment"]["host_activation"]
        assert entry["version"] == target == version
        assert entry["state"] == "requested"
        assert durable_host_entries[-1] == entry
        events.append("activate")
        return {
            "operation_id": entry["operation_id"],
            "status": "host_activated",
            "version": target,
            "health_commit": target,
        }

    runner = staged_module.StagedDeployer(
        record,
        get=get,
        post=post,
        save=lambda _record: durable_host_entries.append(
            dict(_record["runtime_deployment"]["host_activation"])
            if isinstance(
                _record.get("runtime_deployment", {}).get("host_activation"), dict
            )
            else {}
        ),
        verify=lambda _version: (Path("/immutable/release"), "d" * 64),
        activate_host=activate,
    )
    journal = runner._journal("promote", version)
    runner._stage_and_promote_api(journal, version)

    assert events == ["activate"]
    assert journal["host_activation"]["state"] == "complete"
    assert journal["host_activation"]["result"]["operation_id"] == (
        journal["id"] + ":host-activation"
    )


def test_host_rollback_retry_returns_the_recorded_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = "a" * 40
    current = "b" * 40
    api_version = "c" * 40
    saved_readback = {
        "version": previous,
        "health_commit": previous,
        "api_version": api_version,
    }
    record = {
        "deployment_mode": "supervisor",
        "host_active": previous,
        "host_activation": {
            "state": "rolled_back",
            "version": current,
            "previous_version": previous,
            "rollback": {
                "state": "complete",
                "operation_id": "rollback-op",
                "version": previous,
                "previous_version": current,
                "expected_api_version": api_version,
                "readback": saved_readback,
            },
        },
    }
    monkeypatch.setattr(deployer, "state", lambda: record)
    monkeypatch.setattr(
        deployer,
        "host_connection",
        lambda **_kwargs: {
            "package_version": "3.0.0-beta.3+80d43d956ba75c35",
            "pid": 123,
        },
    )
    monkeypatch.setattr(
        deployer,
        "runtime_package_version",
        lambda _version: "3.0.0-beta.3+80d43d956ba75c35",
    )
    monkeypatch.setattr(
        deployer,
        "supervisor_service_identity",
        lambda: {"main_pid": 123},
    )
    monkeypatch.setattr(
        deployer, "require_runtime_process_in_service", lambda *_args: None
    )
    monkeypatch.setattr(deployer, "current_release_version", lambda: previous)
    monkeypatch.setattr(
        deployer,
        "read_supervisor_health",
        lambda: (
            {
                "status": "ok",
                "database": "ok",
                "commit": previous,
                "recovery_version": "3.0.0-beta.3+80d43d956ba75c35",
            },
            {
                "protocol": "nebula.runtime/v1",
                "health": "ready",
                "api_version": api_version,
                "resident_versions": [],
                "projects": [],
            },
        ),
    )
    monkeypatch.setattr(deployer, "STATE", tmp_path / "state.json")

    result = deployer.rollback_host()

    assert result["status"] == "host_rolled_back"
    assert result["operation_id"] == "rollback-op"
    assert result["version"] == previous


@pytest.mark.parametrize(
    "state", ["rolling_back", "service_stopped_release_selected", "service_started"]
)
def test_interrupted_host_rollback_replays_exact_journaled_target(
    state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous, current, api_version = "a" * 40, "b" * 40, "c" * 40
    record = {
        "deployment_mode": "supervisor",
        "host_active": current,
        "host_activation": {
            "state": "complete",
            "version": current,
            "previous_version": previous,
            "rollback": {
                "operation_id": "rollback-op",
                "state": state,
                "version": previous,
                "previous_version": current,
                "expected_api_version": api_version,
                "prior_status": {"api_version": api_version},
                "code_only": True,
            },
        },
    }
    selected: list[str] = []

    def switch(version: str, **kwargs: object) -> dict[str, str]:
        selected.append(version)
        assert version == previous
        assert kwargs["journal"] is record["host_activation"]["rollback"]
        return {"version": previous, "health_commit": previous}

    monkeypatch.setattr(deployer, "state", lambda: record)
    monkeypatch.setattr(deployer, "current_release_version", lambda: current)
    monkeypatch.setattr(
        deployer,
        "runtime_release",
        lambda _version: (tmp_path / previous, "d" * 64),
    )
    monkeypatch.setattr(deployer, "_switch_supervisor_host", switch)
    monkeypatch.setattr(deployer, "STATE", tmp_path / "state.json")

    result = deployer.rollback_host()

    assert selected == [previous]
    assert result["operation_id"] == "rollback-op"
    assert result["version"] == previous
    assert record["host_activation"]["rollback"]["state"] == "complete"


@pytest.mark.parametrize("nested", [True, False])
def test_interrupted_rollback_restoration_replays_original_host(
    nested: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous, current, api_version = "a" * 40, "b" * 40, "c" * 40
    restore = {
        "operation_id": "rollback-op:restore-current",
        "restore_of": "rollback-op",
        "state": "rolling_back" if nested else "service_started",
        "version": current,
        "previous_version": previous,
        "expected_api_version": api_version,
        "prior_status": {"api_version": api_version},
        "code_only": True,
    }
    rollback = (
        {
            "operation_id": "rollback-op",
            "state": "restoring_current_host",
            "version": previous,
            "restore_operation": restore,
        }
        if nested
        else restore
    )
    record = {
        "deployment_mode": "supervisor",
        "host_active": current,
        "host_activation": {
            "state": "complete",
            "version": current,
            "previous_version": previous,
            "rollback": rollback,
        },
    }
    switched: list[str] = []

    def switch(version: str, **kwargs: object) -> dict[str, str]:
        switched.append(version)
        assert version == current
        assert kwargs["journal"] is restore
        return {"version": current, "health_commit": current}

    monkeypatch.setattr(deployer, "state", lambda: record)
    monkeypatch.setattr(deployer, "current_release_version", lambda: previous)
    monkeypatch.setattr(
        deployer,
        "runtime_release",
        lambda _version: (tmp_path / current, "d" * 64),
    )
    monkeypatch.setattr(deployer, "_switch_supervisor_host", switch)
    monkeypatch.setattr(deployer, "STATE", tmp_path / "state.json")

    result = deployer.rollback_host()

    assert switched == [current]
    assert result["status"] == "host_current_restored_after_failed_rollback"
    assert result["operation_id"] == restore["operation_id"]
    assert result["version"] == current
    assert record["host_active"] == current


def _uncertain_budget_deployment(tmp_path: Path) -> tuple[dict, dict, dict]:
    previous, version, pending, deployment_id = (
        "a" * 40,
        "b" * 40,
        "c" * 40,
        "d" * 32,
    )
    manifest_sha256 = "e" * 64
    release = tmp_path / "releases" / version
    pending_release = tmp_path / "releases" / pending
    pending_release.mkdir(parents=True)
    deployer.atomic_json(
        pending_release / "simulation-acceptance.json",
        {"commit": pending, "previous": previous, "status": "accepted_candidate"},
    )
    receipts = {
        deployment_id + ":stage": {
            "operation_id": deployment_id + ":stage",
            "action": "stage",
            "state": "succeeded",
            "result": {"version": version, "manifest_sha256": manifest_sha256},
        },
        deployment_id + ":migrate": {
            "operation_id": deployment_id + ":migrate",
            "action": "migrate",
            "state": "succeeded",
            "result": {"revision": 40700},
        },
        deployment_id + ":api": {
            "operation_id": deployment_id + ":api",
            "action": "prepare_api",
            "state": "uncertain",
            "result": {"detail": "unconfirmed", "error_type": "MemoryBudgetError"},
        },
    }
    steps = {
        "stage": {
            "operation_id": deployment_id + ":stage",
            "action": "stage",
            "parameters": {"source": str(release), "manifest_sha256": manifest_sha256},
            "receipt": receipts[deployment_id + ":stage"],
        },
        "migrate": {
            "operation_id": deployment_id + ":migrate",
            "action": "migrate",
            "parameters": {"version": version, "expected_revision": 40699},
            "receipt": receipts[deployment_id + ":migrate"],
        },
        "api": {
            "operation_id": deployment_id + ":api",
            "action": "prepare_api",
            "parameters": {"version": version},
            "receipt": receipts[deployment_id + ":api"],
        },
    }
    host_operation_id = deployment_id + ":host-activation"
    host_readback = {
        "health_commit": version,
        "recovery_version": "package-b",
        "api_version": previous,
        "resident_versions": [previous],
    }
    host_result = {
        "status": "host_activated",
        "version": version,
        "health_commit": version,
        "operation_id": host_operation_id,
    }
    record = {
        "deployment_mode": "supervisor",
        "active": previous,
        "pending": pending,
        "host_active": version,
        "host_activation": {
            "operation_id": host_operation_id,
            "version": version,
            "previous_version": previous,
            "state": "complete",
            "readback": host_readback,
        },
        "runtime_deployment": {
            "id": deployment_id,
            "action": "promote",
            "version": version,
            "previous": previous,
            "state": "running",
            "steps": steps,
            "host_activation": {
                "operation_id": host_operation_id,
                "version": version,
                "state": "complete",
                "result": host_result,
            },
        },
    }
    status = {
        "protocol": "nebula.runtime/v1",
        "health": "ready",
        "api_version": previous,
        "target_execution_version": version,
        "resident_versions": [previous],
    }
    receipts.update(
        {
            "deployment/operations/" + key: value
            for key, value in list(receipts.items())
        }
    )
    receipts["deployment/status"] = status
    meta = {
        "previous": previous,
        "version": version,
        "pending": pending,
        "deployment_id": deployment_id,
        "manifest_sha256": manifest_sha256,
        "package_version": "package-b",
        "host_readback": host_readback,
    }
    return record, receipts, meta


def _configure_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: dict,
    receipts: dict,
    meta: dict,
) -> None:
    monkeypatch.setattr(deployer, "STATE", tmp_path / "deployer-state.json")
    deployer.atomic_json(deployer.STATE, record)

    class Core:
        def get(self, path: str) -> dict:
            return receipts[path]

    monkeypatch.setattr(deployer, "api", Core)
    monkeypatch.setattr(
        deployer,
        "runtime_release",
        lambda _version: (tmp_path / "releases" / meta["version"], meta["manifest_sha256"]),
    )
    monkeypatch.setattr(
        deployer,
        "verified_release",
        lambda _version: tmp_path / "releases" / meta["pending"],
    )
    monkeypatch.setattr(deployer, "current_release_version", lambda: meta["version"])
    monkeypatch.setattr(deployer, "unit_state", lambda _service: "active")
    monkeypatch.setattr(
        deployer, "runtime_package_version", lambda _version: meta["package_version"]
    )
    monkeypatch.setattr(
        deployer,
        "host_connection",
        lambda **_kwargs: {"package_version": meta["package_version"], "pid": 1234},
    )
    monkeypatch.setattr(deployer, "supervisor_service_identity", lambda: {})
    monkeypatch.setattr(
        deployer, "require_runtime_process_in_service", lambda *_args: None
    )
    health = {
        "status": "ok",
        "database": "ok",
        "commit": meta["version"],
        "recovery_version": meta["package_version"],
    }
    monkeypatch.setattr(
        deployer,
        "read_supervisor_health",
        lambda: (health, receipts["deployment/status"]),
    )


def test_reconcile_pre_spawn_memory_error_archives_journal_and_preserves_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, receipts, meta = _uncertain_budget_deployment(tmp_path)
    _configure_reconciliation(tmp_path, monkeypatch, record, receipts, meta)

    result = deployer.reconcile_runtime_deployment(meta["deployment_id"])

    assert result["status"] == "reconciled_no_effect"
    saved = deployer.state()
    assert "runtime_deployment" not in saved
    assert saved["pending"] == meta["pending"]
    assert saved["active"] == meta["previous"]
    assert saved["host_active"] == meta["version"]
    archived = saved["runtime_deployment_history"][0]
    assert archived["journal"]["id"] == meta["deployment_id"]
    assert archived["classification"] == "pre_spawn_memory_budget_rejection"
    assert archived["core_receipts"]["stage"]["state"] == "succeeded"
    assert archived["core_receipts"]["migrate"]["state"] == "succeeded"
    assert archived["core_receipts"]["api"]["state"] == "uncertain"
    assert archived["live_readback"]["target_execution_version"] == meta["version"]
    assert deployer.reconcile_runtime_deployment(meta["deployment_id"])["status"] == (
        "already_reconciled"
    )


def test_reconcile_accepts_legacy_top_level_host_activation_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, receipts, meta = _uncertain_budget_deployment(tmp_path)
    # This is the saved pre-nesting shape: only the exact completed receipt and
    # readback are top-level; the deployment journal has no host_activation key.
    record["runtime_deployment"].pop("host_activation")
    _configure_reconciliation(tmp_path, monkeypatch, record, receipts, meta)

    result = deployer.reconcile_runtime_deployment(meta["deployment_id"])

    assert result["status"] == "reconciled_no_effect"
    archived = deployer.state()["runtime_deployment_history"][0]
    assert "host_activation" not in archived["journal"]
    assert archived["live_readback"]["api_version"] == meta["previous"]
    assert archived["live_readback"]["target_execution_version"] == meta["version"]


def test_reconcile_rejects_unknown_error_and_changed_api_or_resident_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for case in ("unknown-error", "changed-api", "changed-resident"):
        case_path = tmp_path / case
        case_path.mkdir()
        with monkeypatch.context() as scoped:
            record, receipts, meta = _uncertain_budget_deployment(case_path)
            if case == "unknown-error":
                receipt = receipts["deployment/operations/" + meta["deployment_id"] + ":api"]
                receipt["result"]["error_type"] = "RuntimeError"
                record["runtime_deployment"]["steps"]["api"]["receipt"] = receipt
            elif case == "changed-api":
                receipts["deployment/status"]["api_version"] = meta["version"]
            else:
                receipts["deployment/status"]["resident_versions"] = [meta["version"]]
            _configure_reconciliation(case_path, scoped, record, receipts, meta)
            with pytest.raises(RuntimeError, match="pre-spawn|state changed"):
                deployer.reconcile_runtime_deployment(meta["deployment_id"])
            assert deployer.state()["runtime_deployment"]["id"] == meta["deployment_id"]


def test_prepare_rejects_replacing_candidate_under_nonterminal_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, _receipts, meta = _uncertain_budget_deployment(tmp_path)
    monkeypatch.setattr(deployer, "STATE", tmp_path / "deployer-state.json")
    deployer.atomic_json(deployer.STATE, record)
    monkeypatch.setattr(deployer, "git_source", lambda: None)
    monkeypatch.setattr(deployer, "baseline", lambda: meta["previous"])

    def git_run(*args, capture=False, **_kwargs):
        if "rev-parse" in args:
            return "f" * 40 if capture else ""
        return ""

    monkeypatch.setattr(deployer, "run", git_run)
    with pytest.raises(RuntimeError, match="different deployment operation"):
        deployer.prepare()
    saved = deployer.state()
    assert saved["pending"] == meta["pending"]
    assert saved["runtime_deployment"]["id"] == meta["deployment_id"]
