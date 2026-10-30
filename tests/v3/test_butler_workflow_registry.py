"""The Butler audit must notice new UI API actions without granting them authority."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHECK = UI / "scripts/check-butler-workflow-registry.mjs"
REGISTRY = ROOT / "docs/design/butler-workflow-registry.json"


def _check(registry: Path = REGISTRY) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(CHECK), "--registry", str(registry)],
        cwd=UI,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_reviewed_workflow_registry_matches_ui_api_calls() -> None:
    result = _check()
    assert result.returncode == 0, result.stderr
    assert "unreviewed signatures" in result.stdout


def test_registry_rejects_new_ui_api_call_without_review(tmp_path: Path) -> None:
    inventory = json.loads(REGISTRY.read_text())
    inventory["ui_api_calls"][0]["count"] += 1
    candidate = tmp_path / "drift.json"
    candidate.write_text(json.dumps(inventory))

    result = _check(candidate)
    assert result.returncode != 0
    assert "UI API calls changed; review operator workflows" in result.stderr


def test_registry_rejects_unsupported_action_marked_supported(tmp_path: Path) -> None:
    inventory = json.loads(REGISTRY.read_text())
    gap = next(row for row in inventory["actions"] if row["id"] == "chat.rewind")
    gap["status"] = "supported"
    gap["butler"]["change"] = "missing"
    candidate = tmp_path / "false-support.json"
    candidate.write_text(json.dumps(inventory))

    result = _check(candidate)
    assert result.returncode != 0
    assert "supported action has no change operation" in result.stderr


def test_registry_rejects_operation_missing_from_core_catalog(tmp_path: Path) -> None:
    inventory = json.loads(REGISTRY.read_text())
    action = next(row for row in inventory["actions"] if row["id"] == "chat.archive")
    action["butler"]["change"] = "nebula.work.unlisted_action"
    candidate = tmp_path / "unlisted-action.json"
    candidate.write_text(json.dumps(inventory))

    result = _check(candidate)
    assert result.returncode != 0
    assert "Butler operation is not in the Core catalog" in result.stderr
