"""Live protocol evidence prevents accidental offline supervisor rollback."""

import importlib.util
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile
from urllib.error import HTTPError

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/simulation"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "protocol_deployer", SCRIPTS / "nebula_deployer.py"
)
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)
OLD, NEW = "a" * 40, "b" * 40


def setup(monkeypatch, get):
    effects = []
    record = {"active": OLD, "previous": OLD, "pending": NEW, "promoting": NEW}
    monkeypatch.setattr(deployer, "state", lambda: record)
    monkeypatch.setattr(deployer, "api", lambda: SimpleNamespace(get=get))
    monkeypatch.setattr(deployer, "verified_release", lambda _: Path("/inert/release"))
    monkeypatch.setattr(deployer, "baseline", lambda: OLD)
    for name in ("service", "edge", "snapshot", "restore_database_backup"):
        monkeypatch.setattr(
            deployer, name, lambda *args, **kwargs: effects.append(args)
        )
    monkeypatch.setattr(
        deployer,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "protocol guard must precede host commands"
        ),
    )
    return record, effects


@pytest.mark.parametrize(
    "entry", ["promote", "legacy_promote", "rollback", "offline_rollback"]
)
def test_missing_local_marker_never_allows_offline_supervisor_mutation(
    monkeypatch, entry
):
    record, effects = setup(
        monkeypatch, lambda path: {"protocol": "nebula.runtime/v1", "api_version": OLD}
    )
    with pytest.raises(RuntimeError, match="supervised"):
        getattr(deployer, entry)()
    assert not effects and "deployment_mode" not in record


@pytest.mark.parametrize("entry", ["promote", "offline_rollback"])
def test_unavailable_protocol_without_verified_attempt_fails_closed(monkeypatch, entry):
    def unavailable(path):
        raise OSError("inert unavailable listener")

    _, effects = setup(monkeypatch, unavailable)
    with pytest.raises(RuntimeError, match="protocol is unavailable"):
        getattr(deployer, entry)()
    assert not effects


def test_unavailable_failed_legacy_cutover_requires_matching_attempt(monkeypatch):
    def unavailable(path):
        raise OSError("inert unavailable candidate")

    record, effects = setup(monkeypatch, unavailable)
    deployer.require_legacy_protocol(record, attempt=deployer._LegacyAttempt(OLD, NEW))
    with pytest.raises(RuntimeError):
        deployer.require_legacy_protocol(
            record, attempt=deployer._LegacyAttempt(OLD, "c" * 40)
        )
    assert not effects


def test_live_supervisor_vetoes_even_a_prior_legacy_attempt(monkeypatch):
    _, effects = setup(monkeypatch, lambda path: {"protocol": "nebula.runtime/v1"})
    with pytest.raises(RuntimeError, match="supervised"):
        deployer.offline_rollback(_legacy_attempt=deployer._LegacyAttempt(OLD, NEW))
    assert not effects


def test_legacy_discovery_requires_unsupported_route_and_exact_health(monkeypatch):
    def legacy(path):
        if path == "deployment/status":
            raise HTTPError("http://inert", 404, "legacy", {}, None)
        return {"status": "ok", "commit": OLD}

    record, effects = setup(monkeypatch, legacy)
    deployer.require_legacy_protocol(record, expected_version=OLD)
    with pytest.raises(RuntimeError, match="exact legacy"):
        deployer.require_legacy_protocol(record, expected_version=NEW)
    assert not effects


def _ci_plan(**overrides):
    plan = {
        "change_digest": "c" * 64,
        "reviewed_by": "Codex",
        "reason": "Focused deployer acceptance coverage.",
        "exclusions": "No local test execution during deployment.",
        "python_browser": False,
        "python_node": False,
        "python_versions": ["3.12"],
        "checks": [],
        "expected_tests": {"python": 1, "frontend": 0, "native": 0, "playwright": 0},
        "python": [
            "tests/test_deployer_protocol.py::test_ci_acceptance_matches_exact_run_and_selection"
        ],
        "frontend": [],
        "native": [],
        "playwright": [],
    }
    plan.update(overrides)
    return plan


def _selection_zip(selection):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("test-selection-receipt.json", json.dumps(selection))
    return output.getvalue()


def test_ci_acceptance_matches_exact_run_and_selection(monkeypatch, tmp_path):
    selection = _ci_plan()
    archive = _selection_zip(selection)
    digest = hashlib.sha256(archive).hexdigest()
    run_info = {
        "head_sha": NEW,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": "berylliumsec/nebula"},
        "workflow_id": 27,
        "run_attempt": 1,
        "html_url": "https://github.com/berylliumsec/nebula/actions/runs/98765",
    }
    responses = {
        "actions/runs/98765": run_info,
        "actions/workflows/27": {
            "path": ".github/workflows/ci.yml",
            "state": "active",
        },
        "actions/runs/98765/artifacts?per_page=100": {
            "total_count": 1,
            "artifacts": [
                {
                    "id": 41,
                    "name": "test-selection-receipt",
                    "expired": False,
                    "size_in_bytes": len(archive),
                    "digest": f"sha256:{digest}",
                }
            ],
        },
        "actions/runs/98765/jobs?per_page=100&page=1": {
            "jobs": [
                {"name": "selection", "status": "completed", "conclusion": "success"},
                {"name": "security", "status": "completed", "conclusion": "success"},
                {
                    "name": "python (3.12)",
                    "status": "completed",
                    "conclusion": "success",
                },
            ]
        },
    }
    monkeypatch.setattr(
        deployer,
        "github_json",
        lambda path: responses[path],
    )
    monkeypatch.setattr(deployer, "github_bytes", lambda *_args, **_kwargs: archive)

    def fake_run(*args, **_kwargs):
        if "diff" in args:
            return ""
        if "rev-parse" in args:
            return OLD
        if "show" in args:
            return json.dumps(selection)
        if any(argument.endswith("test_selection.py") for argument in args):
            return json.dumps(selection)
        pytest.fail(f"unexpected command in CI acceptance verifier: {args!r}")

    monkeypatch.setattr(deployer, "run", fake_run)

    receipt = deployer.verify_ci_acceptance(
        "98765", previous=OLD, sha=NEW, directory=tmp_path
    )
    assert receipt["source"] == "github-actions-ci"
    assert receipt["run_id"] == 98765
    assert receipt["baseline"] == OLD
    assert receipt["selection_change_digest"] == "c" * 64
    assert receipt["selected_jobs"] == {
        "python (3.12)": "success",
        "security": "success",
        "selection": "success",
    }


def _ci_run_receipt(run_id, commit, baseline):
    return {
        "source": "github-actions-ci",
        "run_id": run_id,
        "run_url": f"https://github.com/berylliumsec/nebula/actions/runs/{run_id}",
        "run_attempt": 1,
        "workflow_path": ".github/workflows/ci.yml",
        "commit": commit,
        "baseline": baseline,
        "selection_change_digest": "c" * 64,
        "selection_plan_sha256": "d" * 64,
        "artifact_sha256": "e" * 64,
        "selected_jobs": {"selection": "success", "security": "success"},
    }


def test_ci_acceptance_chain_binds_each_commit_to_exact_run(monkeypatch, tmp_path):
    middle = "c" * 40
    commits = [middle, NEW]
    receipts = [
        _ci_run_receipt(98765, middle, OLD),
        _ci_run_receipt(98766, NEW, middle),
    ]
    calls = []
    monkeypatch.setattr(
        deployer,
        "first_parent_commits",
        lambda _directory, *, previous, sha: commits,
    )

    def verify(run_id, *, previous, sha, directory, trusted_baseline):
        calls.append((run_id, previous, sha, directory, trusted_baseline))
        return receipts[len(calls) - 1]

    monkeypatch.setattr(deployer, "verify_ci_acceptance", verify)
    accepted = deployer.verify_ci_acceptance_chain(
        ["98765", "98766"], previous=OLD, sha=NEW, directory=tmp_path
    )

    assert calls == [
        ("98765", OLD, middle, tmp_path, OLD),
        ("98766", middle, NEW, tmp_path, OLD),
    ]
    assert accepted == {
        "source": "github-actions-ci-chain",
        "baseline": OLD,
        "commit": NEW,
        "receipts": receipts,
    }


def test_ci_acceptance_chain_rejects_missing_extra_and_duplicate_runs(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        deployer,
        "first_parent_commits",
        lambda *_args, **_kwargs: ["c" * 40, NEW],
    )
    monkeypatch.setattr(
        deployer,
        "verify_ci_acceptance",
        lambda *_args, **_kwargs: pytest.fail("invalid run list reached verification"),
    )
    for run_ids in (["98765"], ["98765", "98766", "98767"], ["98765", "98765"]):
        with pytest.raises(RuntimeError):
            deployer.verify_ci_acceptance_chain(
                run_ids, previous=OLD, sha=NEW, directory=tmp_path
            )


def test_ci_acceptance_chain_rejects_reordered_or_gap_receipts(
    monkeypatch, tmp_path
):
    middle = "c" * 40
    monkeypatch.setattr(
        deployer,
        "first_parent_commits",
        lambda *_args, **_kwargs: [middle, NEW],
    )
    monkeypatch.setattr(
        deployer,
        "verify_ci_acceptance",
        lambda run_id, **kwargs: _ci_run_receipt(
            int(run_id), NEW if run_id == "98765" else middle, kwargs["previous"]
        ),
    )
    with pytest.raises(RuntimeError, match="chain position"):
        deployer.verify_ci_acceptance_chain(
            ["98765", "98766"], previous=OLD, sha=NEW, directory=tmp_path
        )

    def broken_chain(*_args, **_kwargs):
        raise RuntimeError("CI acceptance first-parent chain has a gap")

    monkeypatch.setattr(deployer, "first_parent_commits", broken_chain)
    with pytest.raises(RuntimeError, match="first-parent chain has a gap"):
        deployer.verify_ci_acceptance_chain(
            ["98765", "98766"], previous=OLD, sha=NEW, directory=tmp_path
        )


def test_ci_acceptance_chain_receipt_readback_rejects_tampering():
    middle = "c" * 40
    valid = {
        "source": "github-actions-ci-chain",
        "baseline": OLD,
        "commit": NEW,
        "receipts": [
            _ci_run_receipt(98765, middle, OLD),
            _ci_run_receipt(98766, NEW, middle),
        ],
    }
    deployer.validate_ci_acceptance_record(
        valid, previous=OLD, sha=NEW, expected_commits=[middle, NEW]
    )
    with pytest.raises(RuntimeError, match="acceptance record is invalid"):
        deployer.validate_ci_acceptance_record(
            valid, previous=OLD, sha=NEW, expected_commits=[NEW]
        )

    broken = {
        **valid,
        "receipts": [valid["receipts"][0], {**valid["receipts"][1], "baseline": OLD}],
    }
    with pytest.raises(RuntimeError, match="acceptance record is invalid"):
        deployer.validate_ci_acceptance_record(broken, previous=OLD, sha=NEW)


def test_ci_acceptance_rejects_modified_trusted_workflow_or_validator(
    monkeypatch, tmp_path
):
    calls = []

    def fake_run(*args, **_kwargs):
        calls.append(args)
        if "diff" in args:
            raise RuntimeError("git diff found changed trusted inputs")
        pytest.fail(f"unexpected command: {args!r}")

    monkeypatch.setattr(deployer, "run", fake_run)
    with pytest.raises(RuntimeError, match="changes to trusted workflows"):
        deployer.verify_ci_trust_inputs(
            tmp_path, trusted_baseline=OLD, sha=NEW
        )

    diff_args = next(args for args in calls if "diff" in args)
    assert ".github/workflows" in diff_args
    assert ".github/actions" in diff_args
    assert "scripts/test_selection.py" in diff_args


def test_selected_ci_jobs_reject_playwright_not_run_on_push():
    plan = _ci_plan()
    assert deployer.selected_ci_jobs(plan) == {
        "selection",
        "security",
        "python (3.12)",
    }
    with pytest.raises(RuntimeError, match="does not execute selected Playwright"):
        deployer.selected_ci_jobs(_ci_plan(playwright=["assistant.spec.ts"]))


def test_ci_acceptance_rejects_incomplete_artifact_listing():
    with pytest.raises(RuntimeError, match="artifact listing is incomplete"):
        deployer.complete_ci_artifact_listing(
            {"total_count": 101, "artifacts": [{"name": "test-selection-receipt"}]}
        )


def test_ci_acceptance_rejects_pull_request_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(deployer, "run", lambda *args, **_kwargs: OLD)
    monkeypatch.setattr(
        deployer,
        "github_json",
        lambda _path: {
            "head_sha": NEW,
            "head_branch": "main",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": "berylliumsec/nebula"},
            "run_attempt": 1,
            "html_url": "https://github.com/berylliumsec/nebula/actions/runs/98765",
        },
    )
    with pytest.raises(
        RuntimeError, match="successful push of the exact main candidate"
    ):
        deployer.verify_ci_acceptance(
            "98765", previous=OLD, sha=NEW, directory=tmp_path
        )


def test_ci_acceptance_rejects_a_failed_selected_job():
    with pytest.raises(RuntimeError, match="python \(3.12\)"):
        deployer.require_ci_jobs(
            [
                {"name": "selection", "status": "completed", "conclusion": "success"},
                {
                    "name": "python (3.12)",
                    "status": "completed",
                    "conclusion": "failure",
                },
            ],
            {"selection", "python (3.12)"},
        )
