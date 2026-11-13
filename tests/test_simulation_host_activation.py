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
            }
        ],
    }


def test_host_activation_requires_exact_existing_worker_ownership() -> None:
    before = deployer.host_status_snapshot(_status())

    assert (
        deployer.require_preserved_workers(
            before, deployer.host_status_snapshot(_status())
        )
        == 1
    )
    with pytest.raises(RuntimeError, match="changed the existing execution owner"):
        deployer.require_preserved_workers(
            before, deployer.host_status_snapshot(_status(runtime_id="worker-2"))
        )
    with pytest.raises(RuntimeError, match="changed the existing execution owner"):
        deployer.require_preserved_workers(
            before, deployer.host_status_snapshot(_status(fencing_token=8))
        )


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
