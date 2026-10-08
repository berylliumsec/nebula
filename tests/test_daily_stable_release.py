"""Focused checks for daily release decisions before any external mutation."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "daily_stable_release",
    Path(__file__).resolve().parents[1] / "scripts/daily_stable_release.py",
)
assert SPEC and SPEC.loader
daily = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily)


@pytest.mark.parametrize(
    ("previous", "next_version"),
    [
        ("nebula-v3.0.0-beta.3", "3.0.0"),
        ("nebula-v3.0.0", "3.0.1"),
        ("nebula-v3.2.9", "3.2.10"),
    ],
)
def test_next_stable_version(previous: str, next_version: str) -> None:
    assert daily.next_stable_version(previous) == next_version


def test_invalid_release_version_stops() -> None:
    with pytest.raises(RuntimeError, match="Cannot derive"):
        daily.next_stable_version("nebula-v2.9.0")


def test_main_requires_successful_push_ci() -> None:
    with (
        patch.object(daily, "MAIN_SHA", "a" * 40),
        patch.object(
            daily,
            "gh_json",
            return_value={
                "workflow_runs": [
                    {
                        "event": "pull_request",
                        "conclusion": "success",
                        "head_sha": "a" * 40,
                    },
                    {"event": "push", "conclusion": "failure", "head_sha": "a" * 40},
                ]
            },
        ),
        pytest.raises(RuntimeError, match="CI has not passed"),
    ):
        daily.green_main()


def test_impact_receipt_rejects_missing_baseline() -> None:
    def fake_download(*args: str, **_kwargs: object) -> str:
        directory = Path(args[args.index("--dir") + 1])
        (directory / "playwright-impact-plan.json").write_text(
            json.dumps(
                {
                    "coverage_review_required": False,
                    "baseline_sha": None,
                    "candidate_sha": "b" * 40,
                }
            )
        )
        return ""

    with (
        patch.object(daily, "run", side_effect=fake_download),
        pytest.raises(RuntimeError, match="accepted baseline"),
    ):
        daily.verify_impact_receipt(123, "nebula-v3.0.0", "a" * 40, "b" * 40)


@pytest.mark.parametrize(
    ("returncode", "blockers", "baseline"),
    [
        (1, [], "a" * 40),
        (2, ["no_valid_baseline"], None),
        (2, ["selection_expands_to_full_suite"], "a" * 40),
        (2, ["global:ui/src/ui.css"], None),
    ],
)
def test_preflight_rejects_invalid_baseline_or_selector(
    returncode: int, blockers: list[str], baseline: str | None
) -> None:
    def fake_select(args: list[str], **_kwargs: object) -> object:
        output = Path(args[args.index("--output") + 1])
        output.write_text(
            json.dumps(
                {
                    "fallbacks": blockers,
                    "baseline_sha": baseline,
                    "candidate_sha": "b" * 40,
                    "coverage_review_required": True,
                }
            )
        )
        return SimpleNamespace(returncode=returncode, stderr="selector failure")

    with (
        patch.object(daily.subprocess, "run", side_effect=fake_select),
        pytest.raises(RuntimeError, match="Cannot resolve daily coverage"),
    ):
        daily.preflight_impact("a" * 40, "b" * 40)


@pytest.fixture
def impact_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise the actual selector in a disposable git repo; never run the product."""
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "scripts").mkdir()
    (tmp_path / "ui").mkdir()
    for source in ("scripts/playwright_impact.py", "ui/playwright-impact.json"):
        shutil.copyfile(root / source, tmp_path / source)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)

    def commit() -> str:
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Release tests",
                "-c",
                "user.email=tests@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "test fixture",
            ],
            check=True,
        )
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    baseline = commit()
    return baseline, commit


@pytest.mark.parametrize(
    "path",
    ["ui/src/ui.css", "ui/src/unknown.ts", ".github/workflows/nebula3-release.yml"],
)
def test_preflight_automatically_resolves_shared_and_unmapped_changes(
    impact_repo, path
):
    baseline, commit = impact_repo
    changed = Path(path)
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("shared change")
    selection, reason = daily.preflight_impact(baseline, commit())
    assert set(selection.split(",")) == {
        "area:desktop-interface",
        "area:mobile-layout",
        "area:core-api",
    }
    assert path in reason
    assert "Other full-matrix profiles are excluded" in reason
    # The real selector validates the bounded union; no full approval is supplied.


def test_conservative_selection_preserves_matched_feature_journeys(impact_repo):
    baseline, commit = impact_repo
    changed = Path("ui/src/state/ThemeContext.tsx")
    changed.parent.mkdir(parents=True)
    changed.write_text("theme change")
    selection, _ = daily.preflight_impact(baseline, commit())
    assert "area:studio-dark-vision" in selection.split(",")
    assert "area:mobile-layout" in selection.split(",")


def test_known_changes_remain_focused(impact_repo):
    baseline, commit = impact_repo
    changed = Path("ui/src/components/ThemePicker.tsx")
    changed.parent.mkdir(parents=True)
    changed.write_text("focused change")
    assert daily.preflight_impact(baseline, commit()) == (
        "",
        "Daily stable release: automatic impacted selection.",
    )


def test_documentation_changes_keep_no_browser_jobs(impact_repo):
    baseline, commit = impact_repo
    Path("README.md").write_text("documentation only")
    assert daily.preflight_impact(baseline, commit())[0] == ""


def test_preflight_rejects_failed_conservative_validation():
    plan = {
        "coverage_review_required": True,
        "baseline_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "changed_files": ["ui/src/ui.css"],
        "fallbacks": ["global:ui/src/ui.css"],
    }

    def fake_select(args, **_kwargs):
        Path(args[args.index("--output") + 1]).write_text(json.dumps(plan))
        return SimpleNamespace(returncode=2, stderr="rejected catalog")

    with (
        patch.object(daily.subprocess, "run", side_effect=fake_select),
        pytest.raises(RuntimeError, match="failed validation before tagging"),
    ):
        daily.preflight_impact("a" * 40, "b" * 40)


@pytest.mark.parametrize("selection", [[], ["area:themes"]])
def test_receipt_requires_the_dispatched_conservative_selection(selection):
    def download(*args, **_kwargs):
        directory = Path(args[args.index("--dir") + 1])
        (directory / "playwright-impact-plan.json").write_text(
            json.dumps(
                {
                    "coverage_review_required": False,
                    "baseline_sha": "a" * 40,
                    "candidate_sha": "b" * 40,
                    "requested_selection": selection,
                    "include": [{"project": "desktop"}],
                }
            )
        )
        return ""

    with (
        patch.object(daily, "run", side_effect=download),
        pytest.raises(RuntimeError, match="accepted baseline or coverage"),
    ):
        daily.verify_impact_receipt(
            123, "nebula-v3.0.0", "a" * 40, "b" * 40, "area:core-api"
        )


def test_workflow_wait_ignores_older_run() -> None:
    started = datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc)
    runs = {
        "workflow_runs": [
            {
                "id": 1,
                "head_branch": "nebula-v3.0.0",
                "head_sha": "c" * 40,
                "event": "workflow_dispatch",
                "created_at": "2026-10-08T12:59:59Z",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 2,
                "head_branch": "nebula-v3.0.0",
                "head_sha": "c" * 40,
                "event": "workflow_dispatch",
                "created_at": "2026-10-08T13:00:00Z",
                "status": "completed",
                "conclusion": "success",
            },
        ]
    }
    with patch.object(daily, "gh_json", return_value=runs):
        assert (
            daily.wait_for_workflow(
                "nebula3-release.yml", "nebula-v3.0.0", "c" * 40, started
            )
            == 2
        )


def test_workflow_wait_stops_on_failed_run() -> None:
    started = datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc)
    runs = {
        "workflow_runs": [
            {
                "id": 2,
                "head_branch": "nebula-v3.0.0",
                "head_sha": "c" * 40,
                "event": "workflow_dispatch",
                "created_at": "2026-10-08T13:00:00Z",
                "status": "completed",
                "conclusion": "failure",
            },
        ]
    }
    with (
        patch.object(daily, "gh_json", return_value=runs),
        pytest.raises(RuntimeError, match="ended failure"),
    ):
        daily.wait_for_workflow(
            "nebula3-release.yml", "nebula-v3.0.0", "c" * 40, started
        )


@pytest.fixture
def release_driver(tmp_path, monkeypatch):
    """Run the driver with all GitHub/tag/build mutations replaced by mocks."""
    monkeypatch.chdir(tmp_path)
    Path("docs/releases").mkdir(parents=True)
    monkeypatch.setattr(daily, "REPOSITORY", "example/nebula")
    monkeypatch.setattr(daily, "MAIN_SHA", "b" * 40)
    previous = {"tag_name": "nebula-v3.0.0-beta.3"}
    commands = []
    state = {"source": "a" * 40, "ci": "success", "advanced": False}

    def run(*args, **_kwargs):
        commands.append(args)
        if args == ("git", "rev-parse", "HEAD"):
            return (
                "c" * 40
                if any(c[:2] == ("git", "commit") for c in commands)
                else "b" * 40
            )
        if args == ("git", "rev-parse", "origin/main"):
            return "d" * 40 if state["advanced"] else "b" * 40
        if args[:2] == ("git", "show"):
            return f"<!-- nebula-source-sha: {state['source']} -->"
        if args[:2] == ("git", "log"):
            return "b123 focused change"
        if args[:3] == ("gh", "release", "view") and "--json" in args:
            return json.dumps(
                {"isDraft": True, "isPrerelease": False, "tagName": "nebula-v3.0.0"}
            )
        return ""

    def api(endpoint):
        if "ci.yml/runs" in endpoint:
            return {
                "workflow_runs": [
                    {
                        "event": "push",
                        "head_sha": "b" * 40,
                        "conclusion": state["ci"],
                    }
                ]
            }
        return {"sha": "b" * 40}

    monkeypatch.setattr(daily, "run", run)
    monkeypatch.setattr(daily, "gh_json", api)
    monkeypatch.setattr(daily, "published_release", lambda: previous)
    with (
        patch.object(
            daily,
            "preflight_impact",
            return_value=(
                "area:core-api,area:desktop-interface,area:mobile-layout",
                "Automatic conservative coverage",
            ),
        ) as preflight,
        patch.object(daily, "wait_for_workflow", side_effect=[101, 102, 103]) as wait,
        patch.object(daily, "verify_impact_receipt") as receipt,
        patch.object(daily, "verify_assets") as assets,
        patch.object(
            daily.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ),
    ):
        yield SimpleNamespace(
            commands=commands,
            state=state,
            preflight=preflight,
            wait=wait,
            receipt=receipt,
            assets=assets,
        )


def assert_no_release_mutations(driver):
    assert not any(
        command[:2]
        in {
            ("git", "tag"),
            ("git", "push"),
            ("git", "commit"),
            ("git", "checkout"),
        }
        for command in driver.commands
    )
    assert not any(command[:2] == ("gh", "workflow") for command in driver.commands)
    driver.wait.assert_not_called()
    driver.receipt.assert_not_called()
    driver.assets.assert_not_called()


def test_no_new_source_commits_skip_without_tests_or_tags(release_driver):
    release_driver.state["source"] = "b" * 40
    daily.main()
    release_driver.preflight.assert_not_called()
    assert_no_release_mutations(release_driver)


@pytest.mark.parametrize("ci", ["failure", "cancelled", None])
def test_failed_or_incomplete_ci_stops_before_coverage_and_tagging(release_driver, ci):
    release_driver.state["ci"] = ci
    with pytest.raises(RuntimeError, match="CI has not passed"):
        daily.main()
    release_driver.preflight.assert_not_called()
    assert_no_release_mutations(release_driver)


def test_advancing_main_stops_before_tagging(release_driver):
    release_driver.state["advanced"] = True
    with pytest.raises(RuntimeError, match="Main advanced"):
        daily.main()
    release_driver.preflight.assert_not_called()
    assert_no_release_mutations(release_driver)


def test_coverage_validation_failure_stops_before_tagging(release_driver):
    release_driver.preflight.side_effect = RuntimeError("Cannot resolve daily coverage")
    with pytest.raises(RuntimeError, match="Cannot resolve daily coverage"):
        daily.main()
    assert_no_release_mutations(release_driver)


def test_daily_dispatch_carries_conservative_selection_and_checks_artifacts(
    release_driver,
):
    daily.main()
    preparation = next(
        command
        for command in release_driver.commands
        if command[:4] == ("gh", "workflow", "run", "nebula3-release.yml")
    )
    assert "scope=impacted" in preparation
    assert (
        "selection=area:core-api,area:desktop-interface,area:mobile-layout"
        in preparation
    )
    assert "review_reason=Automatic conservative coverage" in preparation
    assert not any("full_approval" in arg for arg in preparation)
    release_driver.receipt.assert_called_once_with(
        101,
        "nebula-v3.0.0",
        "a" * 40,
        "b" * 40,
        "area:core-api,area:desktop-interface,area:mobile-layout",
    )
    release_driver.assets.assert_called_once_with(101, "3.0.0")


@pytest.mark.parametrize("gate", ["receipt", "assets"])
def test_failed_receipt_or_artifacts_prevent_finalization(release_driver, gate):
    getattr(release_driver, gate).side_effect = RuntimeError("verification failed")
    with pytest.raises(RuntimeError, match="verification failed"):
        daily.main()
    assert not any(
        command[:4] == ("gh", "workflow", "run", "nebula3-release-finalize.yml")
        for command in release_driver.commands
    )
    assert not any(
        command[:3] == ("gh", "release", "edit") for command in release_driver.commands
    )
