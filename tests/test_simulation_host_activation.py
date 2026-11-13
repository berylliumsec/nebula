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
    with pytest.raises(RuntimeError, match="changed the existing execution owner"):
        deployer.require_preserved_workers(
            deployer.host_status_snapshot(unowned), deployer.host_status_snapshot(_status())
        )
    candidate = "c" * 40
    admitted_status = _status(runtime_id="new-worker")
    admitted_status["projects"][0]["runtime_version"] = candidate
    admitted_status["resident_versions"].append(candidate)
    assert deployer.require_preserved_workers(
        deployer.host_status_snapshot(unowned),
        deployer.host_status_snapshot(admitted_status),
        admitted_version=candidate,
    ) == {"retained": 0}
    unexpected_status = _status(runtime_id="new-worker")
    with pytest.raises(RuntimeError, match="outside the exact admitted release"):
        deployer.require_preserved_workers(
            deployer.host_status_snapshot(unowned),
            deployer.host_status_snapshot(unexpected_status),
            admitted_version=candidate,
        )


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
        lambda **_kwargs: {"version": previous, "pid": 123},
    )
    monkeypatch.setattr(
        deployer,
        "supervisor_service_identity",
        lambda: {"main_pid": 123},
    )
    monkeypatch.setattr(deployer, "current_release_version", lambda: previous)
    monkeypatch.setattr(
        deployer,
        "read_supervisor_health",
        lambda: (
            {"status": "ok", "commit": previous, "recovery_version": previous},
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
