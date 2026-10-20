"""Bounded retention for the simulation host's immutable Core deployer."""

from __future__ import annotations

import importlib.util
import json
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


def _snapshot(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir()
    (path / "nebula.db").write_bytes(b"coherent test database")
    return path


def test_retention_keeps_current_snapshot_and_removes_only_older_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backups = tmp_path / "rollback-backups"
    backups.mkdir()
    keep = _snapshot(backups, "pre-" + "b" * 40 + "-123")
    initial = _snapshot(backups, "initial-" + "a" * 40)
    prior = _snapshot(backups, "pre-" + "a" * 40 + "-122")
    post = _snapshot(backups, "post-121")
    unrelated = tmp_path / "unrelated-data"
    unrelated.write_text("retain me")
    (prior / "external-link").symlink_to(unrelated)
    state_file = tmp_path / "deployer-state.json"
    monkeypatch.setattr(deployer, "BACKUPS", backups)
    monkeypatch.setattr(deployer, "STATE", state_file)
    record = {
        "rollback_backup": str(keep),
        "initial_backup": str(initial),
        "post_state": str(post),
    }

    result = deployer.finish_rollback_retention(record, keep)

    assert result == {
        "status": "complete",
        "retained": str(keep),
        "removed": sorted([initial.name, prior.name, post.name]),
    }
    assert list(backups.iterdir()) == [keep]
    assert unrelated.read_text() == "retain me"
    saved = json.loads(state_file.read_text())
    assert saved["rollback_backup"] == str(keep)
    assert "initial_backup" not in saved
    assert "post_state" not in saved


def test_retention_rejects_unfamiliar_entry_before_any_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backups = tmp_path / "rollback-backups"
    backups.mkdir()
    keep = _snapshot(backups, "pre-" + "b" * 40 + "-123")
    prior = _snapshot(backups, "pre-" + "a" * 40 + "-122")
    outside = tmp_path / "outside"
    outside.mkdir()
    (backups / "unknown").symlink_to(outside)
    monkeypatch.setattr(deployer, "BACKUPS", backups)

    with pytest.raises(RuntimeError, match="needs review"):
        deployer.retain_single_rollback_snapshot(keep)

    assert keep.is_dir() and prior.is_dir() and outside.is_dir()


def test_retention_failure_preserves_healthy_core_state_and_current_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backups = tmp_path / "rollback-backups"
    backups.mkdir()
    keep = _snapshot(backups, "pre-" + "b" * 40 + "-123")
    prior = _snapshot(backups, "pre-" + "a" * 40 + "-122")
    (backups / "unexpected-file").write_text("operator review")
    state_file = tmp_path / "deployer-state.json"
    monkeypatch.setattr(deployer, "BACKUPS", backups)
    monkeypatch.setattr(deployer, "STATE", state_file)
    record = {"active": "b" * 40, "pending": None, "rollback_backup": str(keep)}

    result = deployer.finish_rollback_retention(record, keep)

    assert result["status"] == "needs_attention"
    assert keep.is_dir() and prior.is_dir()
    saved = json.loads(state_file.read_text())
    assert saved["active"] == "b" * 40
    assert saved["rollback_backup"] == str(keep)
    assert saved["rollback_retention"]["status"] == "needs_attention"


def test_successful_retry_clears_pointer_to_previously_removed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backups = tmp_path / "rollback-backups"
    backups.mkdir()
    keep = _snapshot(backups, "pre-" + "b" * 40 + "-123")
    state_file = tmp_path / "deployer-state.json"
    monkeypatch.setattr(deployer, "BACKUPS", backups)
    monkeypatch.setattr(deployer, "STATE", state_file)
    record = {
        "rollback_backup": str(keep),
        "initial_backup": str(backups / ("initial-" + "a" * 40)),
    }

    result = deployer.finish_rollback_retention(record, keep)

    assert result["status"] == "complete"
    assert result["removed"] == []
    assert "initial_backup" not in json.loads(state_file.read_text())


def test_retention_receipt_write_failure_is_reported_without_losing_healthy_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backups = tmp_path / "rollback-backups"
    backups.mkdir()
    keep = _snapshot(backups, "pre-" + "b" * 40 + "-123")
    prior = _snapshot(backups, "pre-" + "a" * 40 + "-122")
    monkeypatch.setattr(deployer, "BACKUPS", backups)
    monkeypatch.setattr(
        deployer,
        "atomic_json",
        lambda path, record: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    record = {"active": "b" * 40, "pending": None, "rollback_backup": str(keep)}

    result = deployer.finish_rollback_retention(record, keep)

    assert result["status"] == "needs_attention"
    assert result["removed"] == [prior.name]
    assert "disk unavailable" in result["reason"]
    assert keep.is_dir()
    assert record["active"] == "b" * 40


def test_no_pending_candidate_retries_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep = tmp_path / "rollback-backups" / ("pre-" + "b" * 40 + "-123")
    record = {"active": "b" * 40, "pending": None, "rollback_backup": str(keep)}
    seen: list[Path] = []
    monkeypatch.setattr(deployer, "state", lambda: record)
    monkeypatch.setattr(
        deployer,
        "finish_rollback_retention",
        lambda saved, backup: seen.append(backup) or {"status": "complete"},
    )

    result = deployer.promote()

    assert result["status"] == "no_pending_candidate"
    assert result["rollback_retention"]["status"] == "complete"
    assert seen == [keep]


def test_no_pending_candidate_preserves_unreviewed_rollback_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = {
        "active": "a" * 40,
        "pending": None,
        "rollback_backup": str(tmp_path / "pre-backup"),
        "post_state": str(tmp_path / "post-state"),
    }
    monkeypatch.setattr(deployer, "state", lambda: record)
    monkeypatch.setattr(
        deployer,
        "finish_rollback_retention",
        lambda saved, backup: pytest.fail("unreviewed rollback state was pruned"),
    )

    result = deployer.promote()

    assert result["status"] == "no_pending_candidate"
    assert result["rollback_retention"]["status"] == "deferred_recovery"


@pytest.mark.parametrize("cleanup_ok", [True, False])
def test_promotion_prunes_only_after_new_core_is_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_ok: bool
) -> None:
    old = "a" * 40
    new = "b" * 40
    current = tmp_path / "current"
    current.symlink_to(tmp_path / "old-release")
    keep = tmp_path / "rollback-backups" / f"pre-{new}-123"
    events: list[str] = []
    record = {"active": old, "pending": new}
    monkeypatch.setattr(deployer, "CURRENT", current)
    monkeypatch.setattr(deployer, "state", lambda: record.copy())
    monkeypatch.setattr(deployer, "verified_release", lambda sha: tmp_path / sha)
    monkeypatch.setattr(deployer, "baseline", lambda: old)
    monkeypatch.setattr(
        deployer, "run", lambda *args, **kwargs: str(current / "dist/nebula-core")
    )
    monkeypatch.setattr(deployer, "unit_state", lambda unit: "active")
    monkeypatch.setattr(deployer, "blockers", lambda core: [])
    monkeypatch.setattr(deployer, "api", lambda: object())
    monkeypatch.setattr(
        deployer, "edge", lambda action: events.append(f"edge:{action}")
    )
    monkeypatch.setattr(
        deployer, "service", lambda action: events.append(f"core:{action}")
    )
    monkeypatch.setattr(
        deployer, "require_stopped", lambda: events.append("core:stopped")
    )
    monkeypatch.setattr(deployer, "snapshot", lambda label: keep)
    monkeypatch.setattr(
        deployer, "atomic_json", lambda path, value: events.append("state:committed")
    )
    monkeypatch.setattr(
        deployer, "switch_to", lambda release: events.append("release:switched")
    )
    monkeypatch.setattr(
        deployer, "wait_ready", lambda sha: events.append(f"health:{sha}")
    )
    monkeypatch.setattr(
        deployer,
        "finish_rollback_retention",
        lambda saved, backup: (
            events.append("retention")
            or {"status": "complete" if cleanup_ok else "needs_attention"}
        ),
    )
    monkeypatch.setattr(
        deployer,
        "rollback",
        lambda: pytest.fail("cleanup failure must not roll back healthy Core"),
    )

    result = deployer.promote()

    assert result["status"] == (
        "promoted" if cleanup_ok else "promoted_cleanup_incomplete"
    )
    assert events.index(f"health:{new}") < events.index("edge:start")
    assert events.index("edge:start") < events.index("retention")
    assert events[-2:] == ["state:committed", "retention"]


def test_failed_promotion_never_prunes_rollback_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = "a" * 40, "b" * 40
    current = tmp_path / "current"
    current.symlink_to(tmp_path / "old-release")
    events: list[str] = []
    monkeypatch.setattr(deployer, "CURRENT", current)
    monkeypatch.setattr(deployer, "state", lambda: {"active": old, "pending": new})
    monkeypatch.setattr(deployer, "verified_release", lambda sha: tmp_path / sha)
    monkeypatch.setattr(deployer, "baseline", lambda: old)
    monkeypatch.setattr(
        deployer, "run", lambda *args, **kwargs: str(current / "dist/nebula-core")
    )
    monkeypatch.setattr(deployer, "unit_state", lambda unit: "active")
    monkeypatch.setattr(deployer, "blockers", lambda core: [])
    monkeypatch.setattr(deployer, "api", lambda: object())
    monkeypatch.setattr(deployer, "edge", lambda action: None)
    monkeypatch.setattr(deployer, "service", lambda action: None)
    monkeypatch.setattr(deployer, "require_stopped", lambda: None)
    monkeypatch.setattr(deployer, "snapshot", lambda label: tmp_path / "new-backup")
    monkeypatch.setattr(deployer, "atomic_json", lambda path, value: None)
    monkeypatch.setattr(deployer, "switch_to", lambda release: None)
    monkeypatch.setattr(
        deployer,
        "wait_ready",
        lambda sha: (_ for _ in ()).throw(RuntimeError("new Core unhealthy")),
    )
    monkeypatch.setattr(deployer, "rollback", lambda: events.append("rollback"))
    monkeypatch.setattr(
        deployer,
        "finish_rollback_retention",
        lambda saved, backup: pytest.fail("failed promotion pruned a snapshot"),
    )

    with pytest.raises(RuntimeError, match="new Core unhealthy"):
        deployer.promote()

    assert events == ["rollback"]
