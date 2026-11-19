"""The deployer accepts CI evidence by default and backs up only on request."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts/simulation"
sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location(
    "simulation_nebula_deployer_ci_default", SCRIPT_DIR / "nebula_deployer.py"
)
assert spec is not None and spec.loader is not None
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)

OLD, MID, NEW = "a" * 40, "b" * 40, "c" * 40


def _selections(monkeypatch, playwright: dict[str, list] | None = None):
    playwright = playwright or {}

    def git(*args, capture=False, **_kwargs):
        assert args[:3] == ("git", "-C", args[2]) and args[3] == "show"
        commit = args[4].split(":", 1)[0]
        return json.dumps({"playwright": playwright.get(commit, [])})

    monkeypatch.setattr(deployer, "run", git)
    monkeypatch.setattr(
        deployer, "first_parent_commits", lambda directory, previous, sha: [MID, NEW]
    )


def _runs(by_commit: dict[str, list[dict]]):
    seen: list[str] = []

    def github_json(path: str):
        seen.append(path)
        assert path.startswith("actions/workflows/ci.yml/runs?")
        commit = path.split("head_sha=", 1)[1].split("&", 1)[0]
        assert "branch=main" in path and "event=push" in path
        return {"workflow_runs": by_commit.get(commit, [])}

    return github_json, seen


def _run(run_id: int, commit: str, conclusion: str = "success", **overrides):
    return {
        "id": run_id,
        "head_sha": commit,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": conclusion,
        **overrides,
    }


def test_discovery_picks_the_newest_successful_push_run_per_commit(monkeypatch):
    _selections(monkeypatch)
    github_json, seen = _runs(
        {
            MID: [_run(11, MID), _run(15, MID), _run(19, MID, "failure")],
            NEW: [_run(21, NEW), _run(22, NEW, event="pull_request")],
        }
    )
    monkeypatch.setattr(deployer, "github_json", github_json)

    assert deployer.discover_ci_run_ids(Path("."), previous=OLD, sha=NEW) == [
        "15",
        "21",
    ]
    assert len(seen) == 2


@pytest.mark.parametrize(
    "runs, reason",
    [
        ([], "no run yet"),
        ([_run(31, NEW, "failure")], "completed/failure"),
        ([_run(32, NEW, None, status="in_progress")], "in_progress/-"),
    ],
)
def test_discovery_stops_with_the_commit_and_reason(monkeypatch, runs, reason):
    _selections(monkeypatch)
    github_json, _seen = _runs({MID: [_run(11, MID)], NEW: runs})
    monkeypatch.setattr(deployer, "github_json", github_json)

    with pytest.raises(deployer.CiEvidenceUnavailable) as caught:
        deployer.discover_ci_run_ids(Path("."), previous=OLD, sha=NEW)
    assert NEW[:12] in str(caught.value) and reason in str(caught.value)
    assert "--local-tests" in str(caught.value)


def test_discovery_refuses_playwright_selections_push_ci_cannot_prove(monkeypatch):
    _selections(monkeypatch, playwright={MID: ["area:memory-budgets"]})
    monkeypatch.setattr(
        deployer, "github_json", lambda path: pytest.fail("no CI lookup needed")
    )

    with pytest.raises(deployer.CiEvidenceUnavailable, match="Playwright"):
        deployer.discover_ci_run_ids(Path("."), previous=OLD, sha=NEW)


class _Stop(Exception):
    pass


def _prepare_until_acceptance(tmp_path, monkeypatch, discovered):
    candidate = tmp_path / "candidate"
    calls: dict[str, object] = {}
    monkeypatch.setattr(deployer, "git_source", lambda: None)
    monkeypatch.setattr(deployer, "baseline", lambda: OLD)
    monkeypatch.setattr(deployer, "state", lambda: {})
    monkeypatch.setattr(deployer, "RELEASES", tmp_path / "releases")
    monkeypatch.setattr(deployer, "candidate", lambda sha: candidate)
    monkeypatch.setattr(
        deployer,
        "run",
        lambda *args, capture=False, **_kwargs: NEW if "rev-parse" in args else "",
    )

    def discover(directory, *, previous, sha):
        calls["discovered"] = (directory, previous, sha)
        return discovered

    def chain(run_ids, **kwargs):
        calls["chain"] = run_ids
        raise _Stop

    def plan(directory, previous):
        calls["local_plan"] = True
        raise _Stop

    monkeypatch.setattr(deployer, "discover_ci_run_ids", discover)
    monkeypatch.setattr(deployer, "verify_ci_acceptance_chain", chain)
    monkeypatch.setattr(deployer, "selected_plan", plan)
    return calls, candidate


def test_prepare_accepts_discovered_ci_evidence_by_default(tmp_path, monkeypatch):
    calls, candidate = _prepare_until_acceptance(tmp_path, monkeypatch, ["15", "21"])
    with pytest.raises(_Stop):
        deployer.prepare()
    assert calls["discovered"] == (candidate, OLD, NEW)
    assert calls["chain"] == ["15", "21"]


def test_prepare_runs_local_tests_only_when_asked(tmp_path, monkeypatch):
    calls, _candidate = _prepare_until_acceptance(tmp_path, monkeypatch, ["15"])
    with pytest.raises(_Stop):
        deployer.prepare(local_tests=True)
    assert "discovered" not in calls and "chain" not in calls
    assert calls["local_plan"] is True
    with pytest.raises(RuntimeError, match="cannot be combined"):
        deployer.prepare(local_tests=True, ci_run_ids=["15"])


def test_prepare_never_falls_back_to_local_tests(tmp_path, monkeypatch):
    calls, _candidate = _prepare_until_acceptance(tmp_path, monkeypatch, [])

    def missing(directory, *, previous, sha):
        raise deployer.CiEvidenceUnavailable("no successful main push CI run")

    monkeypatch.setattr(deployer, "discover_ci_run_ids", missing)
    with pytest.raises(deployer.CiEvidenceUnavailable):
        deployer.prepare()
    assert "local_plan" not in calls


class LegacyClient:
    def __init__(self, commit):
        self.commit = commit

    def get(self, path):
        if path == "deployment/status":
            raise HTTPError("http://fixture", 404, "legacy route absent", {}, None)
        return {"status": "ok", "commit": self.commit}


def _legacy(tmp_path, monkeypatch, record, *, healthy=True):
    current = tmp_path / "current"
    current.symlink_to(tmp_path / "old-release")
    events: list[str] = []
    saved: list[dict] = []
    monkeypatch.setattr(deployer, "CURRENT", current)
    monkeypatch.setattr(deployer, "RELEASES", tmp_path)
    (tmp_path / OLD).mkdir()
    monkeypatch.setattr(deployer, "state", lambda: record)
    monkeypatch.setattr(deployer, "verified_release", lambda sha: tmp_path / sha)
    monkeypatch.setattr(deployer, "baseline", lambda: OLD)
    monkeypatch.setattr(
        deployer, "run", lambda *args, **kwargs: str(current / "dist/nebula-core")
    )
    monkeypatch.setattr(deployer, "require_legacy_protocol", lambda *a, **k: None)
    states = {"core": "active"}
    monkeypatch.setattr(
        deployer,
        "unit_state",
        lambda unit: states["core"] if unit == deployer.SERVICE else "active",
    )
    monkeypatch.setattr(deployer, "blockers", lambda core: [])
    monkeypatch.setattr(deployer, "api", lambda: LegacyClient(OLD))
    monkeypatch.setattr(
        deployer, "edge", lambda action: events.append(f"edge:{action}")
    )

    def service(action):
        states["core"] = "active" if action == "start" else "inactive"
        events.append(f"core:{action}")

    monkeypatch.setattr(deployer, "service", service)
    monkeypatch.setattr(deployer, "require_stopped", lambda: None)
    monkeypatch.setattr(
        deployer,
        "snapshot",
        lambda label: events.append("snapshot") or tmp_path / label,
    )
    monkeypatch.setattr(
        deployer, "atomic_json", lambda path, value: saved.append(dict(value))
    )
    monkeypatch.setattr(
        deployer,
        "switch_to",
        lambda release: events.append(f"switch:{Path(release).name[:1]}"),
    )

    def wait_ready(sha):
        events.append(f"health:{sha[:1]}")
        if sha == NEW and not healthy:
            raise RuntimeError("new Core unhealthy")

    monkeypatch.setattr(deployer, "wait_ready", wait_ready)
    monkeypatch.setattr(
        deployer,
        "rollback",
        lambda **_: pytest.fail("no snapshot was requested to restore"),
    )
    monkeypatch.setattr(
        deployer,
        "finish_rollback_retention",
        lambda saved_record, backup: pytest.fail("no snapshot to retain"),
    )
    return events, saved


def test_legacy_promotion_takes_no_backup_unless_requested(tmp_path, monkeypatch):
    stale = str(tmp_path / "rollback-backups/pre-old")
    record = {"active": OLD, "pending": NEW, "rollback_backup": stale}
    events, saved = _legacy(tmp_path, monkeypatch, record)

    result = deployer.promote()

    assert "snapshot" not in events
    assert result["status"] == "promoted" and result["rollback_backup"] is None
    assert result["rollback_retention"] == {"status": "not_requested"}
    # An older snapshot is never left as this promotion's restore target.
    assert all("rollback_backup" not in item for item in saved)


def test_failed_legacy_promotion_without_backup_restores_code_only(
    tmp_path, monkeypatch
):
    record = {"active": OLD, "pending": NEW}
    events, _saved = _legacy(tmp_path, monkeypatch, record, healthy=False)

    with pytest.raises(RuntimeError, match="new Core unhealthy"):
        deployer.promote()

    assert "snapshot" not in events
    # The candidate stays stopped, the previous release serves again, then ingress.
    assert events[-5:] == [
        "core:stop",
        f"switch:{OLD[:1]}",
        "core:start",
        f"health:{OLD[:1]}",
        "edge:start",
    ]


def test_supervisor_promotion_backs_up_only_on_request(tmp_path, monkeypatch):
    record = {"deployment_mode": "supervisor", "pending": NEW}
    backups: list[str] = []
    monkeypatch.setattr(deployer, "state", lambda: record)
    monkeypatch.setattr(
        deployer,
        "operator_database_backup",
        lambda label: backups.append(label) or tmp_path / label,
    )

    class Staged:
        def promote(self, version):
            return {"status": "promoted", "version": version}

    monkeypatch.setattr(deployer, "staged_deployer", lambda saved: Staged())

    assert deployer.promote() == {"status": "promoted", "version": NEW}
    assert backups == []
    result = deployer.promote(backup=True)
    assert len(backups) == 1 and backups[0].startswith(f"operator-pre-{NEW}-")
    assert result["operator_backup"] == str(tmp_path / backups[0])
