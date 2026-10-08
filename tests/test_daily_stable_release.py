"""Focused checks for daily release decisions before any external mutation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
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


def test_preflight_blocks_shared_change_before_tag() -> None:
    def fake_select(args: list[str], **_kwargs: object) -> object:
        output = Path(args[args.index("--output") + 1])
        output.write_text(json.dumps({"fallbacks": ["global:ui/src/ui.css"]}))
        return type("Result", (), {"returncode": 2})()

    with (
        patch.object(daily.subprocess, "run", side_effect=fake_select),
        pytest.raises(RuntimeError, match="global:ui/src/ui.css"),
    ):
        daily.preflight_impact("a" * 40, "b" * 40)


def test_reviewed_coverage_requires_exact_diff(tmp_path: Path) -> None:
    review = tmp_path / "review.json"
    payload = {
        "baseline_sha": "a" * 40,
        "change_digest": hashlib.sha256(b"expected diff").hexdigest(),
        "selection": ["entry:studio-dark-vision/desktop"],
        "review_reason": "Reviewed exact diff",
    }
    review.write_text(json.dumps(payload))
    with (
        patch.object(daily, "COVERAGE_REVIEW", review),
        patch.object(daily.subprocess, "check_output", return_value=b"different diff"),
    ):
        assert daily.reviewed_coverage("a" * 40, "b" * 40) == ("", "")


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
