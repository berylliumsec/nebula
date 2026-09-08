import json
from pathlib import Path

import pytest

from scripts.playwright_impact import (
    changed_paths,
    select_plan,
    stable_entries,
    successful_release_baseline,
)


ROOT = Path(__file__).parents[1]
MANIFEST = json.loads((ROOT / "ui/playwright-impact.json").read_text())


def plan(paths, *, baseline="base", scope="impacted"):
    return select_plan(MANIFEST, baseline, "candidate", paths, scope)


def areas(result):
    return {entry["area"] for entry in result["include"]}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("ui/src/components/ChatComposer.tsx", "assistant"),
        ("ui/src/components/BrowserPanel.tsx", "browser"),
        ("ui/src/components/MobileDisclosure.tsx", "mobile-layout"),
        ("ui/src/components/ProjectPicker.tsx", "project-lifecycle"),
        ("src/nebula/v3/api.py", "core-api"),
        ("ui/src/components/ThemePicker.tsx", "themes"),
        ("ui/src-tauri/src/main.rs", "desktop-shell"),
    ],
)
def test_representative_area_mapping(path, expected):
    result_areas = areas(plan([path]))
    assert expected in result_areas
    if expected not in {"mobile-layout"}:
        assert "mobile-layout" not in result_areas


def test_documentation_and_empty_diffs_select_no_jobs():
    assert plan(["docs/operator.md"])["include"] == []
    assert plan([])["include"] == []


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/nebula3-release.yml",
        "ui/playwright.config.ts",
        "ui/tests/fixtures/session.ts",
        "ui/src/App.tsx",
        "ui/src/ui.css",
        "ui/package-lock.json",
        "pyproject.toml",
    ],
)
def test_global_changes_fail_closed_to_full(path):
    result = plan([path])
    assert result["reason"] == "full_fallback"
    assert result["include"] == stable_entries(MANIFEST["full_include"])


def test_unknown_watched_path_fails_closed_to_full():
    result = plan(["ui/src/unknown/new-surface.ts"])
    assert "unmapped:ui/src/unknown/new-surface.ts" in result["fallbacks"]
    assert len(result["include"]) == len(MANIFEST["full_include"])


def test_missing_baseline_and_manual_scope_select_full():
    assert plan([], baseline=None)["reason"] == "full_fallback"
    assert plan([], scope="full")["reason"] == "manual_full_scope"


def test_entries_are_deduplicated_and_stably_ordered():
    result = plan([
        "ui/src/components/MobileChat.tsx",
        "ui/src/components/UnclassifiedWidget.tsx",
    ])
    keys = [(e["area"], e["project"], e.get("grep", "")) for e in result["include"]]
    assert keys == sorted(set(keys))


def test_deleted_and_renamed_paths_preserve_all_impact_paths():
    status = "D\tui/src/components/ThemePicker.tsx\nR100\tdocs/old.md\tui/src/components/BrowserPanel.tsx\n"
    assert changed_paths(status) == [
        "docs/old.md",
        "ui/src/components/BrowserPanel.tsx",
        "ui/src/components/ThemePicker.tsx",
    ]
    assert areas(plan(changed_paths(status))) >= {"browser", "themes"}


def test_invalid_name_status_is_rejected():
    with pytest.raises(ValueError):
        changed_paths("R100\tonly-one-path")


def test_latest_successful_preparation_ignores_newer_failed_release():
    runs = {"workflow_runs": [
        {"id": 13, "head_branch": "nebula-v3.0.0-alpha.13", "head_sha": "bad", "conclusion": "failure"},
        {"id": 12, "head_branch": "nebula-v3.0.0-alpha.12", "head_sha": "cancelled", "conclusion": "cancelled"},
        {"id": 9, "head_branch": "nebula-v3.0.0-alpha.9", "head_sha": "green", "conclusion": "success"},
    ]}
    assert successful_release_baseline(runs, "candidate") == {
        "tag": "nebula-v3.0.0-alpha.9", "sha": "green", "run_id": "9"
    }


def test_candidate_success_is_not_its_own_baseline():
    runs = {"workflow_runs": [
        {"id": 14, "head_branch": "nebula-v3.0.0-alpha.14", "head_sha": "candidate", "conclusion": "success"},
        {"id": 9, "head_branch": "nebula-v3.0.0-alpha.9", "head_sha": "green", "conclusion": "success"},
    ]}
    assert successful_release_baseline(runs, "candidate")["sha"] == "green"


def test_full_manifest_covers_every_permanent_playwright_project():
    expected = {
        "assistant-real-desktop", "assistant-real-compact",
        "assistant-real-chromium-320", "assistant-real-chromium-390",
        "assistant-real-chromium-430", "assistant-real-webkit-320",
        "assistant-real-webkit-390", "assistant-real-webkit-430",
        "browser-chromium-landscape", "browser-webkit-landscape", "desktop",
        "compact", "narrow", "mobile-chromium",
        "mobile-chromium-ledger-390", "mobile-chromium-small",
        "mobile-chromium-wide", "mobile-webkit-small", "mobile-webkit",
        "mobile-webkit-wide", "real-core",
    }
    assert {entry["project"] for entry in MANIFEST["full_include"]} == expected
    assert {
        entry["runtime"]
        for entries in [MANIFEST["full_include"], *MANIFEST["areas"].values()]
        for entry in entries
    } <= {"mock", "real-core", "real-core-sandbox"}
