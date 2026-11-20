"""Live protocol evidence prevents accidental offline supervisor rollback."""

import importlib.util
import hashlib
import io
import json
import os
import subprocess
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
PR_MERGE, PR_HEAD, TREE = "e" * 40, "f" * 40, "9" * 40


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
    monkeypatch.setattr(
        deployer,
        "run",
        lambda *_args, **_kwargs: json.dumps({"playwright": []}),
    )

    def verify(
        run_id,
        *,
        previous,
        sha,
        directory,
        trusted_baseline,
        supplemental_playwright=None,
        commit_playwright=None,
    ):
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


def test_ci_acceptance_chain_accepts_exact_tree_pr_playwright_evidence(
    monkeypatch, tmp_path
):
    selection = _ci_plan(playwright=["area:browser-harness"])
    monkeypatch.setattr(
        deployer,
        "first_parent_commits",
        lambda *_args, **_kwargs: [PR_MERGE],
    )
    monkeypatch.setattr(
        deployer,
        "run",
        lambda *args, **_kwargs: (
            json.dumps(selection)
            if "show" in args
            else pytest.fail(f"unexpected git command: {args!r}")
        ),
    )
    monkeypatch.setattr(deployer, "commit_parents", lambda *_args: [OLD, PR_HEAD])
    monkeypatch.setattr(deployer, "git_tree", lambda *_args: TREE)
    pr_ci = {
        "head_commit": PR_HEAD,
        "base_commit": OLD,
        "merge_commit": PR_MERGE,
        "tree_sha": TREE,
        "run_id": 60001,
        "selection_change_digest": "c" * 64,
        "selection_plan_sha256": "d" * 64,
    }
    pr_playwright = {**pr_ci, "run_id": 60002}
    monkeypatch.setattr(
        deployer, "verify_pr_ci_supplement", lambda *_args, **_kwargs: pr_ci
    )
    monkeypatch.setattr(
        deployer,
        "verify_pr_playwright_supplement",
        lambda *_args, **_kwargs: pr_playwright,
    )
    main_runs = []

    def verify_main(*args, supplemental_playwright=None, **kwargs):
        main_runs.append((args, kwargs, supplemental_playwright))
        receipt = _ci_run_receipt(60003, PR_MERGE, OLD)
        receipt["supplemental_pr_evidence"] = supplemental_playwright
        return receipt

    monkeypatch.setattr(deployer, "verify_ci_acceptance", verify_main)
    accepted = deployer.verify_ci_acceptance_chain(
        ["60003"],
        previous=OLD,
        sha=PR_MERGE,
        directory=tmp_path,
        pr_ci_run_ids=["60001"],
        pr_playwright_run_ids=["60002"],
    )

    assert len(main_runs) == 1
    assert main_runs[0][2] == {
        "pull_request_ci": pr_ci,
        "playwright": pr_playwright,
    }
    assert accepted["supplemental_pr_evidence"] == main_runs[0][2]


@pytest.mark.parametrize("head_tree", ["8" * 40, TREE])
def test_pr_playwright_binding_checks_exact_merge_tree(
    monkeypatch, tmp_path, head_tree
):
    run_info = {
        "head_sha": PR_HEAD,
        "head_branch": "codex/pr-fixture",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": "berylliumsec/nebula"},
        "workflow_id": 77,
        "run_attempt": 1,
        "html_url": "https://github.com/berylliumsec/nebula/actions/runs/60004",
    }
    pull = {
        "number": 989,
        "state": "closed",
        "merged_at": "2026-11-13T18:36:18Z",
        "merge_commit_sha": PR_MERGE,
        "base": {"sha": OLD},
        "head": {"sha": PR_HEAD, "ref": "codex/pr-fixture"},
    }
    monkeypatch.setattr(
        deployer,
        "github_json",
        lambda path: (
            run_info
            if path == "actions/runs/60004"
            else {"path": ".github/workflows/playwright-impact.yml", "state": "active"}
        ),
    )
    monkeypatch.setattr(deployer, "github_json_value", lambda _path: [pull])

    def fake_run(*args, **_kwargs):
        if "rev-list" in args:
            return f"{PR_MERGE} {OLD} {PR_HEAD}"
        if "rev-parse" in args:
            return TREE if args[-1] == f"{PR_MERGE}^{{tree}}" else head_tree
        pytest.fail(f"unexpected git command: {args!r}")

    monkeypatch.setattr(deployer, "run", fake_run)
    if head_tree == TREE:
        accepted = deployer.bind_pr_run(
            "60004",
            head_sha=PR_HEAD,
            base_sha=OLD,
            merge_sha=PR_MERGE,
            workflow_path=".github/workflows/playwright-impact.yml",
            directory=tmp_path,
        )
        assert accepted["tree_sha"] == TREE
    else:
        with pytest.raises(RuntimeError, match="PR head tree differs"):
            deployer.bind_pr_run(
                "60004",
                head_sha=PR_HEAD,
                base_sha=OLD,
                merge_sha=PR_MERGE,
                workflow_path=".github/workflows/playwright-impact.yml",
                directory=tmp_path,
            )


def test_pr_playwright_matrix_requires_every_selected_shard():
    def entry(project):
        return {
            "area": "browser-harness",
            "grep": "browser harness",
            "project": project,
            "runtime": "real-core",
            "shard": "1/1",
            "test_match": "tests/real-core.spec.ts",
        }

    include = [entry("assistant-real-desktop"), entry("assistant-real-webkit-390")]
    jobs = [
        {"name": "plan", "status": "completed", "conclusion": "success"},
        {"name": "gate", "status": "completed", "conclusion": "success"},
        {
            "name": "playwright (browser-harness, browser harness, assistant-real-desktop, real-core, 1/1, tests/real-core.spec.ts)",
            "status": "completed",
            "conclusion": "success",
        },
        {
            "name": "playwright (browser-harness, browser harness, assistant-real-webkit-390, real-core, 1/1, tests/real-core.spec.ts)",
            "status": "completed",
            "conclusion": "success",
        },
    ]
    assert len(deployer.validate_playwright_matrix_jobs(jobs, include)) == 4
    with pytest.raises(RuntimeError, match="cover every selected entry"):
        deployer.validate_playwright_matrix_jobs(jobs[:-1], include)


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


def test_ci_acceptance_chain_rejects_reordered_or_gap_receipts(monkeypatch, tmp_path):
    middle = "c" * 40
    monkeypatch.setattr(
        deployer,
        "first_parent_commits",
        lambda *_args, **_kwargs: [middle, NEW],
    )
    monkeypatch.setattr(
        deployer,
        "run",
        lambda *_args, **_kwargs: json.dumps({"playwright": []}),
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
        deployer.verify_ci_trust_inputs(tmp_path, trusted_baseline=OLD, sha=NEW)

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


TERMINAL_CLIENT_MATRIX_JOBS = sorted(
    f"terminal-client ({runner}, {version})"
    for runner in ("ubuntu-latest", "macos-15")
    for version in ("3.11", "3.12", "3.13")
)


def _successful_jobs(names):
    return [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in names
    ]


def test_terminal_client_ci_acceptance_requires_every_matrix_job():
    required = deployer.selected_ci_jobs(
        _ci_plan(python=["tests/test_terminal_transport.py"])
    )
    assert required == {"selection", "security", "python (3.12)", "terminal-client"}
    jobs = _successful_jobs(
        ["selection", "security", "python (3.12)", *TERMINAL_CLIENT_MATRIX_JOBS]
    )
    assert deployer.require_ci_jobs(jobs, required) == {
        name: "success" for name in required
    }
    # A changed matrix (new runner or Python) still satisfies the family.
    expanded = [*jobs, *_successful_jobs(["terminal-client (windows-2025, 3.14)"])]
    assert deployer.require_ci_jobs(expanded, required)["terminal-client"] == "success"
    for conclusion, status in (
        ("failure", "completed"),
        ("cancelled", "completed"),
        (None, "in_progress"),
    ):
        broken = [dict(job) for job in jobs]
        broken[-1].update(status=status, conclusion=conclusion)
        with pytest.raises(RuntimeError, match="terminal-client"):
            deployer.require_ci_jobs(broken, required)
    without_family = [
        job for job in jobs if not job["name"].startswith("terminal-client")
    ]
    with pytest.raises(RuntimeError, match="terminal-client"):
        deployer.require_ci_jobs(without_family, required)


def test_required_ci_job_family_never_crosses_a_bare_prefix():
    required = {"python (3.12)", "security"}
    jobs = _successful_jobs(
        ["python (3.11)", "python (3.13)", "security-audit", "securityscan (x)"]
    )
    with pytest.raises(
        RuntimeError, match=r"did not succeed: python \(3\.12\), security$"
    ):
        deployer.require_ci_jobs(jobs, required)
    # A failure in a neighbouring family does not taint an exact match.
    jobs = _successful_jobs(["python (3.12)", "security"]) + [
        {"name": "python (3.11)", "status": "completed", "conclusion": "failure"},
        {"name": "security-audit", "status": "completed", "conclusion": "failure"},
    ]
    assert deployer.require_ci_jobs(jobs, required) == {
        "python (3.12)": "success",
        "security": "success",
    }
    assert not deployer.ci_job_in_family("terminal-clientx (a)", "terminal-client")
    assert not deployer.ci_job_in_family("terminal-client(a)", "terminal-client")
    assert deployer.ci_job_in_family("terminal-client (a)", "terminal-client")


def test_recorded_terminal_client_jobs_accept_family_or_exact_legacy_form():
    expected = {
        name: "success"
        for name in deployer.selected_ci_jobs(
            _ci_plan(python=["tests/test_terminal_transport.py"])
        )
    }
    assert deployer.recorded_ci_jobs_match(dict(expected), expected)
    legacy = {
        **{
            name: value
            for name, value in expected.items()
            if name != "terminal-client"
        },
        **dict.fromkeys(TERMINAL_CLIENT_MATRIX_JOBS, "success"),
    }
    assert deployer.recorded_ci_jobs_match(legacy, expected)
    # Anything but the exact legacy expansion is rejected.
    assert not deployer.recorded_ci_jobs_match(
        {**legacy, "terminal-client": "success"}, expected
    )
    assert not deployer.recorded_ci_jobs_match(
        {k: v for k, v in legacy.items() if k != TERMINAL_CLIENT_MATRIX_JOBS[0]},
        expected,
    )
    assert not deployer.recorded_ci_jobs_match(
        {**legacy, TERMINAL_CLIENT_MATRIX_JOBS[0]: "failure"}, expected
    )
    assert not deployer.recorded_ci_jobs_match(
        {k: v for k, v in expected.items() if k != "security"}, expected
    )
    # Without a terminal-client family, the legacy names are never accepted.
    plain = {"selection": "success", "security": "success"}
    assert not deployer.recorded_ci_jobs_match(
        {**plain, **dict.fromkeys(TERMINAL_CLIENT_MATRIX_JOBS, "success")}, plain
    )


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


def test_deployment_post_honours_retry_after_and_resubmits_the_same_operation(
    monkeypatch,
):
    # A stopping supervisor answers 503 with Retry-After while the operation
    # continues; the same operation ID is resubmitted until its receipt exists.
    monkeypatch.setattr(
        deployer,
        "api",
        lambda: SimpleNamespace(origin="http://127.0.0.1:8000", token="inert"),
    )
    sleeps, bodies = [], []
    monkeypatch.setattr(deployer.time, "sleep", sleeps.append)
    receipt = {"operation_id": "op-1", "state": "succeeded", "result": {}}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(request, timeout):
        bodies.append(request.data)
        if len(bodies) < 3:
            raise HTTPError(
                request.full_url, 503, "restarting", {"Retry-After": "1"}, None
            )
        return Response(json.dumps(receipt).encode())

    monkeypatch.setattr(deployer, "urlopen", urlopen)
    payload = {"operation_id": "op-1", "action": "stage", "parameters": {}}
    assert deployer.deployment_post("deployment/operations", payload) == receipt
    assert sleeps == [1, 1] and len(set(bodies)) == 1

    def refused(request, timeout):
        raise HTTPError(request.full_url, 503, "unavailable", {}, None)

    monkeypatch.setattr(deployer, "urlopen", refused)
    with pytest.raises(HTTPError):
        deployer.deployment_post("deployment/operations", payload)
    assert sleeps == [1, 1]


def _git(directory, *args):
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


def _squash_history(tmp_path):
    """main: base -> main_parent; PR branch from base merges main_parent;
    main then gets the PR as one squash commit with the same tree."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "main.txt").write_text("main\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "main parent")
    parent = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "-b", "feature", base)
    (tmp_path / "feature.txt").write_text("feature\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "feature")
    stale_head = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "merge", "-q", "--no-edit", "main")
    head = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "-q", "--squash", "feature")
    _git(tmp_path, "commit", "-q", "-m", "squash")
    squash = _git(tmp_path, "rev-parse", "HEAD")
    return {"base": base, "parent": parent, "stale_head": stale_head,
            "head": head, "squash": squash}


def test_squash_merge_counts_only_for_a_head_with_the_identical_tree(tmp_path):
    history = _squash_history(tmp_path)
    assert deployer.require_pr_merge_shape(
        tmp_path, merge_sha=history["squash"], base_sha=history["parent"],
        head_sha=history["head"],
    ) == "squash"
    # A head that never merged main's parent did not test this tree.
    with pytest.raises(RuntimeError, match="does not contain its main parent"):
        deployer.require_pr_merge_shape(
            tmp_path, merge_sha=history["squash"], base_sha=history["parent"],
            head_sha=history["stale_head"],
        )
    # A squash commit naming another parent is not this merge.
    with pytest.raises(RuntimeError, match="merge commit parents"):
        deployer.require_pr_merge_shape(
            tmp_path, merge_sha=history["squash"], base_sha=history["base"],
            head_sha=history["head"],
        )
    # A head whose tree differs (a later push, say) is refused.
    _git(tmp_path, "checkout", "-q", "feature")
    (tmp_path / "late.txt").write_text("late\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "late push")
    late = _git(tmp_path, "rev-parse", "HEAD")
    with pytest.raises(RuntimeError, match="PR head tree differs"):
        deployer.require_pr_merge_shape(
            tmp_path, merge_sha=history["squash"], base_sha=history["parent"],
            head_sha=late,
        )


def test_two_parent_merge_shape_is_unchanged(tmp_path):
    history = _squash_history(tmp_path)
    _git(tmp_path, "reset", "-q", "--hard", history["parent"])
    _git(tmp_path, "merge", "-q", "--no-ff", "--no-edit", history["head"])
    merge = _git(tmp_path, "rev-parse", "HEAD")
    assert deployer.require_pr_merge_shape(
        tmp_path, merge_sha=merge, base_sha=history["parent"],
        head_sha=history["head"],
    ) == "merge"


def test_ci_acceptance_chain_binds_a_squash_merge_to_its_pr_head(
    monkeypatch, tmp_path
):
    selection = _ci_plan(playwright=["area:browser-harness"])
    monkeypatch.setattr(
        deployer, "first_parent_commits", lambda *_args, **_kwargs: [PR_MERGE]
    )
    monkeypatch.setattr(
        deployer,
        "run",
        lambda *args, **_kwargs: (
            json.dumps(selection)
            if "show" in args
            else pytest.fail(f"unexpected git command: {args!r}")
        ),
    )
    monkeypatch.setattr(deployer, "commit_parents", lambda *_args: [OLD])
    monkeypatch.setattr(deployer, "git_tree", lambda *_args: TREE)
    monkeypatch.setattr(
        deployer, "squash_merged_pr_head", lambda _directory, sha: (PR_HEAD, 1236)
    )
    shapes = []
    monkeypatch.setattr(
        deployer,
        "require_pr_merge_shape",
        lambda _directory, **kwargs: shapes.append(kwargs) or "squash",
    )
    pr_ci = {
        "head_commit": PR_HEAD, "base_commit": OLD, "merge_commit": PR_MERGE,
        "tree_sha": TREE, "run_id": 60001,
        "selection_change_digest": "c" * 64, "selection_plan_sha256": "d" * 64,
    }
    seen = []

    def supplement(run_id, **kwargs):
        seen.append((run_id, kwargs["head_sha"], kwargs["base_sha"]))
        return {**pr_ci, "run_id": int(run_id)}

    monkeypatch.setattr(deployer, "verify_pr_ci_supplement", supplement)
    monkeypatch.setattr(deployer, "verify_pr_playwright_supplement", supplement)
    monkeypatch.setattr(
        deployer,
        "verify_ci_acceptance",
        lambda *_args, supplemental_playwright=None, **_kwargs: {
            **_ci_run_receipt(60003, PR_MERGE, OLD),
            "supplemental_pr_evidence": supplemental_playwright,
        },
    )

    accepted = deployer.verify_ci_acceptance_chain(
        ["60003"], previous=OLD, sha=PR_MERGE, directory=tmp_path,
        pr_ci_run_ids=["60001"], pr_playwright_run_ids=["60002"],
    )

    assert shapes == [{"merge_sha": PR_MERGE, "base_sha": OLD, "head_sha": PR_HEAD}]
    assert seen == [("60001", PR_HEAD, OLD), ("60002", PR_HEAD, OLD)]
    assert accepted["supplemental_pr_evidence"]["playwright"]["run_id"] == 60002


def test_squash_pr_binding_accepts_an_older_recorded_base(monkeypatch, tmp_path):
    """A squash PR's recorded base can predate main's parent; the shape
    check proves its head contains that parent instead."""
    run_info = {
        "head_sha": PR_HEAD, "head_branch": "claude/pr-fixture",
        "event": "pull_request", "status": "completed", "conclusion": "success",
        "repository": {"full_name": "berylliumsec/nebula"}, "workflow_id": 77,
        "run_attempt": 1,
        "html_url": "https://github.com/berylliumsec/nebula/actions/runs/60004",
    }
    pull = {
        "number": 1236, "state": "closed", "merged_at": "2026-11-20T12:31:45Z",
        "merge_commit_sha": PR_MERGE, "base": {"sha": "7" * 40},
        "head": {"sha": PR_HEAD, "ref": "claude/pr-fixture"},
    }
    monkeypatch.setattr(
        deployer,
        "github_json",
        lambda path: run_info if path == "actions/runs/60004"
        else {"path": ".github/workflows/ci.yml", "state": "active"},
    )
    monkeypatch.setattr(deployer, "github_json_value", lambda _path: [pull])

    def fake_run(*args, **_kwargs):
        if "rev-list" in args:
            return f"{PR_MERGE} {OLD}"
        if "merge-base" in args:
            return ""
        if "rev-parse" in args:
            return TREE
        pytest.fail(f"unexpected git command: {args!r}")

    monkeypatch.setattr(deployer, "run", fake_run)
    bound = deployer.bind_pr_run(
        "60004", head_sha=PR_HEAD, base_sha=OLD, merge_sha=PR_MERGE,
        workflow_path=".github/workflows/ci.yml", directory=tmp_path,
    )
    assert bound["pull_request"] == 1236 and bound["tree_sha"] == TREE


def test_pr_playwright_matrix_matches_github_truncated_job_names():
    """GitHub cuts job names at 100 characters, so long greps hide the
    project. Every selected entry still needs its own successful job."""
    grep = "a project held for a Core update pauses the composer and resumes on release$"

    def entry(project):
        return {"area": "project-hold", "project": project,
                "test_match": "tests/runtime-continuity.spec.ts",
                "runtime": "real-core", "shard": "1/1", "grep": grep}

    projects = ["model-desktop", "model-compact", "model-webkit-390"]
    include = [entry(project) for project in projects]
    shown = (
        "playwright (project-hold, a project held for a Core update pauses "
        "the composer and resumes on rel..."
    )
    assert len(shown) == 100
    jobs = [
        {"name": "plan", "status": "completed", "conclusion": "success"},
        {"name": "gate", "status": "completed", "conclusion": "success"},
        *({"name": shown, "status": "completed", "conclusion": "success"}
          for _ in projects),
    ]
    accepted = deployer.validate_playwright_matrix_jobs(jobs, include)
    assert len(accepted) == 2 + len(projects)
    # One job short of the selected entries does not cover them.
    with pytest.raises(RuntimeError, match="cover every selected entry"):
        deployer.validate_playwright_matrix_jobs(jobs[:-1], include)
    # A failed instance fails closed.
    failed = [*jobs[:-1], {**jobs[-1], "conclusion": "failure"}]
    with pytest.raises(RuntimeError, match="did not succeed"):
        deployer.validate_playwright_matrix_jobs(failed, include)
    # A job for an entry nobody selected is refused.
    stray = [*jobs, {"name": "playwright (other-area, x, desktop, mock, 1/1, tests/x.spec.ts)",
                     "status": "completed", "conclusion": "success"}]
    with pytest.raises(RuntimeError, match="lacks its matrix identity"):
        deployer.validate_playwright_matrix_jobs(stray, include)


def _dispatch_fixture(monkeypatch, *, event="workflow_dispatch", head=PR_MERGE,
                      candidate=PR_MERGE, baseline=None):
    selection = _ci_plan(playwright=["area:browser-harness"])
    entry = {"area": "browser-harness", "grep": "browser harness",
             "project": "desktop", "runtime": "mock", "shard": "1/1",
             "test_match": "tests/interface.spec.ts"}
    plan = {"candidate_sha": candidate, "baseline_sha": baseline,
            "coverage_review_required": False, "include": [entry],
            "review_reason": "Run the merged commit's reviewed selection."}
    run_info = {
        "head_sha": head, "head_branch": "claude/evidence", "event": event,
        "status": "completed", "conclusion": "success",
        "repository": {"full_name": "berylliumsec/nebula"}, "workflow_id": 78,
        "run_attempt": 1,
        "html_url": "https://github.com/berylliumsec/nebula/actions/runs/70001",
    }
    monkeypatch.setattr(
        deployer, "github_json",
        lambda path: run_info if path == "actions/runs/70001"
        else {"path": ".github/workflows/playwright-impact.yml", "state": "active"},
    )
    monkeypatch.setattr(deployer, "verify_ci_trust_inputs", lambda *_a, **_k: None)
    monkeypatch.setattr(
        deployer, "supplemental_artifact_files",
        lambda *_args: (
            {"playwright-impact-plan.json": json.dumps(plan).encode(),
             "playwright-impact-receipt.md": b"receipt"},
            {"expired": False, "expires_at": "2026-12-20T00:00:00Z"},
            "a" * 64,
        ),
    )
    monkeypatch.setattr(deployer, "run", lambda *_args, **_kwargs: json.dumps(selection))
    calls = []

    def expected(selection, **kwargs):
        calls.append(kwargs)
        return ({**plan, "candidate_sha": PR_MERGE, "baseline_sha": None}, "receipt")

    monkeypatch.setattr(deployer, "expected_pr_playwright_plan", expected)
    name = "playwright (browser-harness, browser harness, desktop, mock, 1/1, tests/interface.spec.ts)"
    monkeypatch.setattr(
        deployer, "github_run_jobs",
        lambda _run_id: [
            {"name": "plan", "status": "completed", "conclusion": "success"},
            {"name": "gate", "status": "completed", "conclusion": "success"},
            {"name": name, "status": "completed", "conclusion": "success"},
        ],
    )
    return calls


def test_exact_commit_playwright_dispatch_is_browser_evidence(monkeypatch, tmp_path):
    calls = _dispatch_fixture(monkeypatch)
    evidence = deployer.verify_commit_playwright_run(
        "70001", commit=PR_MERGE, directory=tmp_path, trusted_baseline=OLD
    )
    assert evidence["source"] == "github-actions-commit-playwright"
    assert evidence["commit"] == PR_MERGE and evidence["run_id"] == 70001
    assert len(evidence["selected_jobs"]) == 3
    # The trusted selector is asked for the commit itself, with no baseline,
    # and the operator's recorded reason.
    assert calls == [{
        "head_sha": PR_MERGE, "base_sha": None, "directory": tmp_path,
        "trusted_baseline": OLD,
        "review_reason": "Run the merged commit's reviewed selection.",
    }]


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"event": "pull_request"}, "dispatch on the exact commit"),
        ({"head": "1" * 40}, "dispatch on the exact commit"),
        ({"candidate": "1" * 40}, "bound to the exact commit"),
        ({"baseline": OLD}, "bound to the exact commit"),
    ],
)
def test_exact_commit_playwright_dispatch_refuses_other_runs(
    monkeypatch, tmp_path, changes, message
):
    _dispatch_fixture(monkeypatch, **changes)
    with pytest.raises(RuntimeError, match=message):
        deployer.verify_commit_playwright_run(
            "70001", commit=PR_MERGE, directory=tmp_path, trusted_baseline=OLD
        )


def test_ci_acceptance_chain_uses_exact_commit_evidence_without_pr_runs(
    monkeypatch, tmp_path
):
    selection = _ci_plan(playwright=["area:browser-harness"])
    monkeypatch.setattr(
        deployer, "first_parent_commits", lambda *_args, **_kwargs: [PR_MERGE]
    )
    monkeypatch.setattr(deployer, "run", lambda *_args, **_kwargs: json.dumps(selection))
    monkeypatch.setattr(
        deployer, "verify_pr_ci_supplement",
        lambda *_a, **_k: pytest.fail("no PR evidence is used"),
    )
    evidence = {"source": "github-actions-commit-playwright", "run_id": 70001}
    monkeypatch.setattr(
        deployer, "verify_commit_playwright_run", lambda run_id, **_k: evidence
    )
    seen = []

    def verify_main(*_args, supplemental_playwright=None, commit_playwright=None, **_k):
        seen.append((supplemental_playwright, commit_playwright))
        return {**_ci_run_receipt(60003, PR_MERGE, OLD),
                "commit_playwright_evidence": commit_playwright}

    monkeypatch.setattr(deployer, "verify_ci_acceptance", verify_main)
    accepted = deployer.verify_ci_acceptance_chain(
        ["60003"], previous=OLD, sha=PR_MERGE, directory=tmp_path,
        commit_playwright_run_ids={PR_MERGE: "70001"},
    )
    assert seen == [(None, evidence)]
    assert accepted["commit_playwright_evidence"] == evidence
    # Evidence for a commit that selects no browser checks is refused.
    monkeypatch.setattr(
        deployer, "run",
        lambda *_args, **_kwargs: json.dumps(_ci_plan(playwright=[])),
    )
    with pytest.raises(RuntimeError, match="without browser checks"):
        deployer.verify_ci_acceptance_chain(
            ["60003"], previous=OLD, sha=PR_MERGE, directory=tmp_path,
            commit_playwright_run_ids={PR_MERGE: "70001"},
        )
