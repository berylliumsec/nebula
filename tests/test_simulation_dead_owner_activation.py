"""Exact operator acceptance for a proven-dead simulation execution worker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts/simulation"
sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location(
    "dead_owner_activation_deployer", SCRIPT_DIR / "nebula_deployer.py"
)
assert spec is not None and spec.loader is not None
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)
OLD = "a" * 40
NEW = "b" * 40

def test_dead_owner_acceptance_requires_exact_busy_turns_and_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = {
        "project_id": "project-example", "runtime_id": "worker-dead",
        "fencing_token": 51, "turn_ids": ["turn-7"],
        "failed_turn_ids": ["failed-turn-1"],
        "effect_call_ids": ["call-1"],
        "acceptance": "accept unconfirmed effects without replay",
    }
    class Core:
        def get(self, path: str):
            assert path == "deployment/status"
            return {"projects": [{"project_id": "project-example", "fencing_token": 51}]}

    monkeypatch.setattr(deployer, "api", Core)
    monkeypatch.setattr(deployer, "_restart_boundary_activity", lambda _client: {
        "project-example": {"name": "Example Project", "turns": ["turn-7"]}
    })
    monkeypatch.setattr(deployer, "_unavailable_turn_owners", lambda _client, _busy: {
        "project-example": {"runtime_id": "worker-dead"}
    })
    monkeypatch.setattr(deployer, "prove_dead_owner", lambda _accepted: {
        "pid": 100, "process_birth": "linux:boot:1"
    })
    accepted_result = deployer.wait_quiet_restart_boundary(accepted)
    assert accepted_result["accepted_dead_owner"] == accepted
    assert accepted_result["dead_process"]["pid"] == 100

    accepted["turn_ids"] = ["another-turn"]
    with pytest.raises(RuntimeError, match="no longer matches"):
        deployer.wait_quiet_restart_boundary(accepted)


def test_dead_owner_proof_refuses_live_process_and_changed_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = {
        "project_id": "00000000-0000-0000-0000-000000000001",
        "runtime_id": "00000000-0000-0000-0000-000000000002",
        "failed_turn_ids": ["00000000-0000-0000-0000-000000000004"],
        "effect_call_ids": ["00000000-0000-0000-0000-000000000003"],
    }
    monkeypatch.setattr(deployer, "_readonly_postgres_rows", lambda query: (
        ["123|linux:boot:1|execution|ready"] if "runtime_processes" in query
        else ["00000000-0000-0000-0000-000000000004"] if "kind = 'chat_turns'" in query
        else ["00000000-0000-0000-0000-000000000003|failed|workspace_write"]
    ))
    monkeypatch.setattr(deployer, "linux_process_birth", lambda _pid: "linux:boot:1")
    with pytest.raises(RuntimeError, match="not proven to exit"):
        deployer.prove_dead_owner(accepted)

    monkeypatch.setattr(deployer, "linux_process_birth", lambda _pid: None)
    assert deployer.prove_dead_owner(accepted)["pid"] == 123
    monkeypatch.setattr(deployer, "_readonly_postgres_rows", lambda query: (
        ["123|linux:boot:1|execution|ready"] if "runtime_processes" in query
        else ["00000000-0000-0000-0000-000000000004"] if "kind = 'chat_turns'" in query
        else []
    ))
    with pytest.raises(RuntimeError, match="write-call identities changed"):
        deployer.prove_dead_owner(accepted)


def test_dead_owner_continuity_requires_verified_exact_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = {"runtime_id": "worker-dead", "runtime_version": OLD, "fencing_token": 51}
    new = {"runtime_id": "worker-new", "runtime_version": NEW, "fencing_token": 52}
    before = {"projects": {"project-example": old}, "resident_versions": [OLD]}
    after = {"projects": {"project-example": new}, "resident_versions": [OLD, NEW]}
    accepted = {"project_id": "project-example", "runtime_id": "worker-dead", "fencing_token": 51}
    monkeypatch.setattr(deployer, "verified_dead_owner_recovery", lambda *_args: False)
    with pytest.raises(deployer.DeadOwnerRecoveryPending):
        deployer.require_preserved_workers(
            before, after, admitted_version=NEW, accepted_dead_owner=accepted
        )
    monkeypatch.setattr(deployer, "verified_dead_owner_recovery", lambda *_args: True)
    deployer.require_preserved_workers(
        before, after, admitted_version=NEW, accepted_dead_owner=accepted
    )
    after["projects"]["project-example"]["runtime_version"] = OLD
    with pytest.raises(RuntimeError, match="changed the existing execution owner"):
        deployer.require_preserved_workers(
            before, after, admitted_version=NEW, accepted_dead_owner=accepted
        )
