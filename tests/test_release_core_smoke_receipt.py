"""Focused release receipt contract; no package or browser is launched here."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from release_core_smoke_receipt import CASE, deb_sha256, verify_receipt, write_receipt  # noqa: E402

TAG = "nebula-v3.0.17"
COMMIT = "a" * 40


def browser_report(tmp_path: Path) -> Path:
    report = tmp_path / "playwright.json"
    report.write_text(json.dumps({
        "stats": {"expected": 1, "skipped": 0, "unexpected": 0, "flaky": 0},
        "suites": [{"specs": [{"title": CASE, "tests": [{
            "projectName": "real-core", "status": "expected",
            "results": [{"status": "passed", "retry": 0}],
        }]}]}],
    }))
    return report


def test_passed_receipt_binds_exact_installed_deb(tmp_path: Path):
    deb = tmp_path / "nebula.deb"
    deb.write_bytes(b"installed package")
    receipt = tmp_path / "release-core-smoke.json"
    write_receipt(receipt, tag=TAG, commit=COMMIT, deb=deb, exit_code=0, report=browser_report(tmp_path))
    payload = json.loads(receipt.read_text())
    assert payload["installed_deb_sha256"] == deb_sha256(deb)
    assert payload["selected_test_count"] == 1
    assert payload["retries"] == 0
    verify_receipt(receipt, tag=TAG, commit=COMMIT, deb=deb)


@pytest.mark.parametrize("change", ["failed", "tag", "commit", "deb", "count", "missing"])
def test_receipt_rejects_missing_failed_or_mismatched_smoke(tmp_path: Path, change: str):
    deb = tmp_path / "nebula.deb"
    deb.write_bytes(b"installed package")
    receipt = tmp_path / "release-core-smoke.json"
    write_receipt(
        receipt, tag=TAG, commit=COMMIT, deb=deb,
        exit_code=int(change == "failed"), report=browser_report(tmp_path),
    )
    if change == "tag":
        expected = {"tag": "nebula-v3.0.18"}
    elif change == "commit":
        expected = {"commit": "b" * 40}
    elif change == "count":
        payload = json.loads(receipt.read_text())
        payload["selected_test_count"] = 2
        receipt.write_text(json.dumps(payload))
        expected = {}
    elif change == "deb":
        deb.write_bytes(b"other package")
        expected = {}
    elif change == "missing":
        receipt.unlink()
        expected = {}
    else:
        expected = {}
    with pytest.raises(ValueError):
        verify_receipt(receipt, deb=deb, tag=expected.get("tag", TAG), commit=expected.get("commit", COMMIT))


@pytest.mark.parametrize("change", ["skipped", "retried", "wrong-case"])
def test_writer_rejects_nonpassing_or_retried_browser_result(tmp_path: Path, change: str):
    deb = tmp_path / "nebula.deb"
    deb.write_bytes(b"installed package")
    report = browser_report(tmp_path)
    payload = json.loads(report.read_text())
    if change == "skipped":
        payload["stats"].update(expected=0, skipped=1)
    elif change == "retried":
        payload["suites"][0]["specs"][0]["tests"][0]["results"][0]["retry"] = 1
    else:
        payload["suites"][0]["specs"][0]["title"] = "other case"
    report.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        write_receipt(
            tmp_path / "receipt.json", tag=TAG, commit=COMMIT,
            deb=deb, exit_code=0, report=report,
        )


def test_draft_validation_requires_the_smoke_receipt_before_publication():
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/nebula3-release-finalize.yml").read_text()
    )
    validate = workflow["jobs"]["validate"]
    provenance = next(step for step in validate["steps"] if step.get("id") == "release")
    assert 'grep -Fx "release-core-smoke-$RELEASE_TAG"' in provenance["run"]
    assert "scripts/release_core_smoke_receipt.py verify" in provenance["run"]
    assert workflow["jobs"]["draft-release"]["needs"] == "validate"
