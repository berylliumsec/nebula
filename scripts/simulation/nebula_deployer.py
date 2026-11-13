#!/usr/bin/env python3
"""Prepare exact commits and promote through the selected deployment protocol.

Install beside nebula_deploy_gate.py and nebula_staged_deployer.py on simulation. The initial same-build
cutover provisions current and its user service separately; this deployer
handles later main updates. It never changes the development checkout.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from typing import Any
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from urllib.error import HTTPError

from nebula_deploy_gate import CoreClient, blockers, service_token
from nebula_staged_deployer import StagedDeployer
from nebula_postgres_backup import (
    BACKUP_FILES,
    configured_postgres,
    contains_database_backup,
    restore_database_backup,
    snapshot_postgres,
    validate_database_backup,
)


INITIAL_COMMIT = "da8d9178ff2b7786e860e702dbee48944f2a8238"
ROOT = Path("/srv/apple-sim/nebula")
REPO = Path("/srv/apple-sim/repos/nebula")
CANDIDATES = ROOT / "candidates"
RELEASES = ROOT / "releases"
CURRENT = ROOT / "current"
DATA = ROOT / "state/v3"
BACKUPS = ROOT / "rollback-backups"
SQLITE_DATABASES = (
    "nebula.db",
    "butler-memory.sqlite3",
    "knowledge-index/chroma.sqlite3",
    "butler-memory-index/chroma.sqlite3",
)
STATE = ROOT / "deployer-state.json"
TOKEN_FILE = Path("/home/simulation/.config/nebula/service.env")
HELPER = "/home/simulation/.local/bin/simulation-git-credential"
POETRY = "/home/simulation/.local/share/poetry-2.1.3/bin/poetry"
PATH = "/home/simulation/.cargo/bin:/home/simulation/.local/bin:/usr/local/bin:/usr/bin:/bin"
SERVICE = "nebula-core.service"
EDGE = "nebula-lan-tls.service"
SHA = re.compile(r"[0-9a-f]{40}\Z")
GITHUB_REPOSITORY = "berylliumsec/nebula"
GITHUB_API = "https://api.github.com"
CI_ARTIFACT_LIMIT = 1024 * 1024
CI_RUN_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class _LegacyAttempt:
    def __init__(self, previous: str, candidate: str):
        self.previous, self.candidate = previous, candidate


def require_legacy_protocol(
    record: dict,
    *,
    expected_version: str | None = None,
    attempt: _LegacyAttempt | None = None,
) -> None:
    """A missing local mode is never evidence permitting data restoration."""
    verified_attempt = bool(
        attempt
        and record.get("previous") == attempt.previous
        and record.get("promoting") == attempt.candidate
    )
    client = api()
    try:
        status = client.get("deployment/status")
    except HTTPError as error:
        if error.code != 404:
            if verified_attempt:
                return
            raise RuntimeError(
                "Live deployment protocol is unavailable; retain state until it is verified"
            ) from error
    except OSError as error:
        if verified_attempt:
            return
        raise RuntimeError(
            "Live deployment protocol is unavailable; retain state until it is verified"
        ) from error
    else:
        if isinstance(status, dict) and status.get("protocol") == "nebula.runtime/v1":
            raise RuntimeError(
                "The live Core is supervised; adopt-supervisor before promotion or code rollback"
            )
        raise RuntimeError(
            "The live deployment protocol is unrecognized; offline mutation is blocked"
        )
    if verified_attempt:
        return
    health = client.get("health")
    expected = expected_version or record.get("active")
    if (
        not isinstance(health, dict)
        or health.get("status") != "ok"
        or health.get("commit") != expected
    ):
        raise RuntimeError(
            "A healthy exact legacy Core identity is required before offline mutation"
        )


def run(
    *argv: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    timeout: int = 1_800,
) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env={**os.environ, "PATH": PATH, **(env or {})},
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
        timeout=timeout,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-1_000:]
        raise RuntimeError(f"{argv[0]} exited {result.returncode}: {detail}")
    return result.stdout.strip() if capture and result.stdout else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def state() -> dict[str, Any]:
    return json.loads(STATE.read_text()) if STATE.is_file() else {}


def checked_host() -> None:
    if socket.gethostname() != "simulation" or os.getuid() != 1_000:
        raise RuntimeError(
            "Nebula deployer must run as the simulation user on simulation"
        )
    ROOT.mkdir(parents=True, exist_ok=True)
    if not REPO.is_dir() or not TOKEN_FILE.is_file():
        raise RuntimeError(
            "simulation Git source or production service environment is missing"
        )


def git_source() -> None:
    expected = {
        "remote.origin.url": "https://github.com/berylliumsec/nebula.git",
        "credential.helper": HELPER,
        "credential.https://github.com.useHttpPath": "true",
    }
    for key, value in expected.items():
        actual = run("git", "-C", str(REPO), "config", "--get", key, capture=True)
        if actual != value:
            raise RuntimeError(f"Git source {key} is not pinned for the reader App")


def commit_at(path: Path) -> str:
    commit = run("git", "-C", str(path), "rev-parse", "HEAD", capture=True)
    if not SHA.fullmatch(commit):
        raise RuntimeError("Git returned an invalid commit")
    return commit


def baseline() -> str:
    record = state()
    if record.get("deployment_mode") == "supervisor":
        status = api().get("deployment/status")
        active = status.get("api_version")
        if (
            status.get("protocol") != "nebula.runtime/v1"
            or not isinstance(active, str)
            or not SHA.fullmatch(active)
        ):
            raise RuntimeError(
                "The supervisor has no verified exact-commit API release"
            )
        if record.get("active") != active:
            raise RuntimeError(
                "Deployer state differs from the supervisor; reconcile the saved operation journal"
            )
        return active
    if CURRENT.is_symlink():
        active = CURRENT.resolve(strict=True)
        if active.parent != RELEASES or not SHA.fullmatch(active.name):
            raise RuntimeError(
                "current Nebula release points outside immutable releases"
            )
        return active.name
    if CURRENT.exists():
        raise RuntimeError("current Nebula release is not a symlink")
    return INITIAL_COMMIT


def candidate(sha: str) -> Path:
    if not SHA.fullmatch(sha):
        raise ValueError("candidate must be an exact commit")
    directory = CANDIDATES / sha
    if not directory.exists():
        CANDIDATES.mkdir(parents=True, exist_ok=True)
        run("git", "-C", str(REPO), "worktree", "add", "--detach", str(directory), sha)
        # The source-control-only checkout is sparse; a candidate needs all files.
        run("git", "-C", str(directory), "sparse-checkout", "disable")
    if commit_at(directory) != sha:
        raise RuntimeError("candidate checkout has the wrong commit")
    run("git", "-C", str(directory), "diff", "--quiet")
    run("git", "-C", str(directory), "diff", "--cached", "--quiet")
    return directory


def selected_plan(
    directory: Path, previous: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    build = directory / "build"
    build.mkdir(exist_ok=True)
    selected = build / "simulation-test-selection.json"
    head = commit_at(directory)
    commits = run(
        "git",
        "-C",
        str(directory),
        "rev-list",
        "--first-parent",
        "--reverse",
        f"{previous}..{head}",
        capture=True,
    ).splitlines()
    if not commits:
        raise RuntimeError("candidate has no commits after the live release")
    plan: dict[str, Any] = {
        "python": [],
        "frontend": [],
        "native": [],
        "playwright": [],
        "expected_tests": {
            kind: 0 for kind in ("python", "frontend", "native", "playwright")
        },
        "reviewed_commits": [],
    }
    overlaps: dict[str, list[str]] = {}
    cursor = previous
    for commit in commits:
        parent = run(
            "git", "-C", str(directory), "rev-parse", f"{commit}^", capture=True
        )
        if parent != cursor:
            raise RuntimeError(
                "candidate has a non-linear first-parent deployment history"
            )
        source = run(
            "git",
            "-C",
            str(directory),
            "show",
            f"{commit}:.github/test-selection.json",
            capture=True,
        )
        commit_plan = build / f"selection-{commit}.json"
        commit_plan.write_text(source + "\n")
        receipt = build / f"selection-receipt-{commit}.json"
        result = run(
            "/usr/bin/python3",
            "scripts/test_selection.py",
            "--baseline",
            parent,
            "--candidate",
            commit,
            "--plan",
            str(commit_plan),
            "--output",
            str(receipt),
            cwd=directory,
            capture=True,
        )
        reviewed = json.loads(result)
        if reviewed.get("coverage_review_required") or not reviewed.get(
            "change_digest"
        ):
            raise RuntimeError(
                f"commit {commit} lacks a reviewed diff-bound test selection"
            )
        for kind in ("python", "frontend", "native", "playwright"):
            targets = reviewed[kind]
            repeated = sorted(set(targets) & set(plan[kind]))
            if repeated:
                overlaps.setdefault(kind, []).extend(repeated)
            plan[kind].extend(target for target in targets if target not in plan[kind])
            plan["expected_tests"][kind] += reviewed["expected_tests"][kind]
        plan["reviewed_commits"].append(
            {
                "commit": commit,
                "baseline": parent,
                "change_digest": reviewed["change_digest"],
            }
        )
        cursor = commit
    if cursor != head:
        raise RuntimeError(
            "candidate deployment history did not reach its exact commit"
        )
    if overlaps:
        review = ROOT / "cumulative-reviews" / f"{previous}-{head}.json"
        if not review.is_file() or review.is_symlink():
            raise RuntimeError(
                f"overlapping selections require an exact cumulative review: {overlaps}"
            )
        receipt = build / "simulation-cumulative-selection-receipt.json"
        reviewed = json.loads(
            run(
                "/usr/bin/python3",
                "scripts/test_selection.py",
                "--baseline",
                previous,
                "--candidate",
                head,
                "--plan",
                str(review),
                "--output",
                str(receipt),
                cwd=directory,
                capture=True,
            )
        )
        if reviewed.get("coverage_review_required") or not reviewed.get(
            "change_digest"
        ):
            raise RuntimeError("cumulative selection failed diff-bound validation")
        if (
            reviewed.get("baseline_commit") != previous
            or reviewed.get("candidate_commit") != head
            or reviewed.get("reviewed_commits") != commits
        ):
            raise RuntimeError(
                "cumulative selection does not name the exact deployment history"
            )
        for kind in ("python", "frontend", "native", "playwright"):
            if reviewed[kind] != plan[kind]:
                raise RuntimeError(
                    f"cumulative {kind} selection differs from reviewed commit union"
                )
        plan["expected_tests"] = reviewed["expected_tests"]
        plan["reason"] = reviewed["reason"]
        plan["exclusions"] = reviewed["exclusions"]
        plan["cumulative_review"] = str(review)
        plan["cumulative_change_digest"] = reviewed["change_digest"]
        plan["overlaps"] = overlaps
    else:
        plan["reason"] = (
            f"Combined focused selections from {len(commits)} reviewed main commits."
        )
        plan["exclusions"] = (
            "Each commit keeps its own diff-bound review; no duplicate selectors."
        )
    atomic_json(selected, plan)
    impact = build / "simulation-playwright-selection.json"
    args = [
        "/usr/bin/python3",
        "scripts/playwright_impact.py",
        "select",
        "--manifest",
        "ui/playwright-impact.json",
        "--baseline",
        previous,
        "--candidate",
        "HEAD",
        "--review-reason",
        f"{plan['reason']} {plan['exclusions']}",
        "--output",
        str(impact),
        "--receipt",
        str(build / "simulation-playwright-receipt.md"),
    ]
    for selector in plan["playwright"]:
        qualified = (
            selector
            if selector.startswith(("area:", "entry:", "project:"))
            else f"entry:{selector}"
        )
        args += ["--selection", qualified]
    if not plan["playwright"]:
        args += ["--selection", "none"]
    run(*args, cwd=directory, capture=True)
    selected_impact = json.loads(impact.read_text())
    if selected_impact.get("coverage_review_required"):
        raise RuntimeError("Playwright impact selection requires review")
    entries = selected_impact.get("include")
    if not isinstance(entries, list):
        raise RuntimeError("Playwright selection is malformed")
    return plan, entries


class _StripAuthorizationRedirect(HTTPRedirectHandler):
    """Do not forward GitHub credentials to the artifact blob host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise RuntimeError("GitHub artifact redirect must use HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
            redirected.remove_header("authorization")
        return redirected


def github_token() -> str:
    """Read the deployer's existing repository credential without logging it."""
    result = subprocess.run(
        ["git", "credential", "fill"],
        cwd=REPO,
        env={**os.environ, "PATH": PATH, "GIT_TERMINAL_PROMPT": "0"},
        input="protocol=https\nhost=github.com\npath=berylliumsec/nebula.git\n\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("The pinned GitHub credential helper is unavailable")
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    token = values.get("password", "")
    if not token or values.get("host") != "github.com":
        raise RuntimeError("The pinned GitHub credential helper returned no token")
    return token


def github_bytes(url: str, *, limit: int) -> bytes:
    if not url.startswith(GITHUB_API + "/"):
        raise RuntimeError("CI evidence URL is outside the pinned GitHub API")
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token()}",
            "User-Agent": "NebulaSimulationDeployer",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = build_opener(_StripAuthorizationRedirect())
    try:
        with opener.open(request, timeout=60) as response:
            content = response.read(limit + 1)
    except (HTTPError, OSError) as error:
        raise RuntimeError("GitHub CI evidence could not be read") from error
    if len(content) > limit:
        raise RuntimeError("GitHub CI evidence exceeds its size bound")
    return content


def github_json(path: str) -> dict[str, Any]:
    if path.startswith("/") or ".." in Path(path).parts:
        raise RuntimeError("GitHub API path is invalid")
    raw = github_bytes(
        f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/{path}", limit=8 * 1024**2
    )
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("GitHub API returned invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("GitHub API returned an unexpected response")
    return value


def ci_selection_artifact(
    run_id: int, artifact: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    workflow_run = artifact.get("workflow_run")
    if isinstance(workflow_run, dict) and workflow_run.get("id") != run_id:
        raise RuntimeError("GitHub selection artifact belongs to another run")
    digest = artifact.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise RuntimeError("GitHub selection artifact has no SHA-256 digest")
    expected_digest = digest.removeprefix("sha256:")
    if not SHA256.fullmatch(expected_digest):
        raise RuntimeError("GitHub selection artifact digest is malformed")
    size = artifact.get("size_in_bytes")
    if (
        artifact.get("expired") is not False
        or type(size) is not int
        or size < 1
        or size > CI_ARTIFACT_LIMIT
    ):
        raise RuntimeError("GitHub selection artifact is expired or exceeds its bound")
    artifact_id = artifact.get("id")
    if type(artifact_id) is not int or artifact_id < 1:
        raise RuntimeError("GitHub selection artifact identity is invalid")
    archive = github_bytes(
        f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/actions/artifacts/{artifact_id}/zip",
        limit=CI_ARTIFACT_LIMIT,
    )
    if len(archive) != size:
        raise RuntimeError("GitHub selection artifact size does not match its metadata")
    if hashlib.sha256(archive).hexdigest() != expected_digest:
        raise RuntimeError("GitHub selection artifact SHA-256 does not match")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = [item for item in bundle.infolist() if not item.is_dir()]
            if (
                len(entries) != 1
                or entries[0].filename != "test-selection-receipt.json"
            ):
                raise RuntimeError(
                    "CI artifact contents do not match the selection receipt contract"
                )
            mode = entries[0].external_attr >> 16
            if stat.S_ISLNK(mode) or entries[0].file_size > CI_ARTIFACT_LIMIT:
                raise RuntimeError("CI selection receipt has an unsafe type or size")
            raw = bundle.read(entries[0])
        receipt = json.loads(raw)
    except (
        OSError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise RuntimeError(
            "GitHub selection artifact is not a valid receipt archive"
        ) from error
    if not isinstance(receipt, dict):
        raise RuntimeError("CI selection receipt must be a JSON object")
    return receipt, expected_digest


def complete_ci_artifact_listing(response: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = response.get("artifacts")
    total = response.get("total_count")
    if (
        not isinstance(artifacts, list)
        or type(total) is not int
        or total != len(artifacts)
        or any(not isinstance(item, dict) for item in artifacts)
    ):
        raise RuntimeError("GitHub CI artifact listing is incomplete or malformed")
    return artifacts


def selected_ci_jobs(plan: dict[str, Any]) -> set[str]:
    if plan.get("playwright"):
        raise RuntimeError(
            "The push CI workflow does not execute selected Playwright entries"
        )
    required = {"selection", "security"}
    python_tests = plan.get("python", [])
    if python_tests:
        versions = plan.get("python_versions", ["3.12"])
        required.update(f"python ({version})" for version in versions)
    checks = plan.get("checks", [])
    if plan.get("frontend") or "frontend-audit" in checks:
        required.add("frontend")
    if plan.get("native") or "desktop-ui-build" in checks:
        required.add("desktop-macos")
    if any(item.startswith("tests/test_terminal_") for item in python_tests):
        required.add("terminal-client")
    migration_prefixes = (
        "tests/v3/test_migrations.py",
        "tests/v3/test_database_migration",
        "tests/v3/test_postgresql_core.py",
        "tests/test_simulation_postgres_backup.py",
    )
    if any(item.startswith(migration_prefixes) for item in python_tests) or any(
        item.startswith(
            "tests/v3/test_work_batch_contracts.py::test_additive_migration_keeps_prior_data_and_enforces_metadata"
        )
        for item in python_tests
    ):
        required.add("database-migrations")
    if any("tests/v3/test_report_cpu_activation.py" in item for item in python_tests):
        required.add("cpu-workers-frozen-linux")
    if any(
        "tests/v3/test_packaging.py::test_core_archive_requires_native_async_database_runtime"
        in item
        for item in python_tests
    ):
        required.add("async-runtime-frozen-linux")
    if any(
        "tests/v3/test_packaged_runtime_continuity.py::test_real_packaged_windows_preserve_held_turn_and_draft_through_code_rollback"
        in item
        for item in python_tests
    ):
        required.add("native-runtime-continuity-linux")
    return required


def require_ci_jobs(jobs: list[dict[str, Any]], required: set[str]) -> dict[str, str]:
    conclusions: dict[str, list[tuple[str | None, str | None]]] = {}
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str):
            continue
        conclusions.setdefault(job["name"], []).append(
            (job.get("status"), job.get("conclusion"))
        )
    missing = sorted(
        name
        for name in required
        if not conclusions.get(name)
        or any(
            status != "completed" or conclusion != "success"
            for status, conclusion in conclusions[name]
        )
    )
    if missing:
        raise RuntimeError(f"CI selected jobs did not succeed: {', '.join(missing)}")
    return {name: "success" for name in sorted(required)}


def first_parent_commits(directory: Path, *, previous: str, sha: str) -> list[str]:
    commits = run(
        "git",
        "-C",
        str(directory),
        "rev-list",
        "--first-parent",
        "--reverse",
        f"{previous}..{sha}",
        capture=True,
    ).splitlines()
    if not commits or commits[-1] != sha:
        raise RuntimeError("CI acceptance requires a complete first-parent commit chain")
    parent = previous
    for commit in commits:
        actual = run(
            "git", "-C", str(directory), "rev-parse", f"{commit}^1", capture=True
        )
        if actual != parent:
            raise RuntimeError("CI acceptance first-parent chain has a gap")
        parent = commit
    return commits


def verify_ci_trust_inputs(
    directory: Path, *, trusted_baseline: str, sha: str
) -> None:
    # These files define which checks run and how the selection artifact is
    # validated. CI-only acceptance cannot trust a candidate to rewrite them.
    try:
        run(
            "git",
            "-C",
            str(directory),
            "diff",
            "--quiet",
            trusted_baseline,
            sha,
            "--",
            ".github/workflows",
            ".github/actions",
            "scripts/test_selection.py",
        )
    except RuntimeError as error:
        raise RuntimeError(
            "CI acceptance refuses changes to trusted workflows, local actions, "
            "or the test-selection validator"
        ) from error


def verify_ci_acceptance(
    run_id: str,
    *,
    previous: str,
    sha: str,
    directory: Path,
    trusted_baseline: str | None = None,
) -> dict[str, Any]:
    if not CI_RUN_ID.fullmatch(run_id):
        raise RuntimeError("CI run id must be a positive decimal identifier")
    trusted_baseline = trusted_baseline or previous
    verify_ci_trust_inputs(
        directory, trusted_baseline=trusted_baseline, sha=sha
    )
    first_parent = run(
        "git", "-C", str(directory), "rev-parse", f"{sha}^1", capture=True
    )
    if first_parent != previous:
        raise RuntimeError(
            "CI acceptance requires the supplied baseline as the candidate first parent"
        )
    run_info = github_json(f"actions/runs/{run_id}")
    repository = run_info.get("repository")
    if not isinstance(repository, dict):
        repository = {}
    if (
        run_info.get("head_sha") != sha
        or run_info.get("head_branch") != "main"
        or run_info.get("event") != "push"
        or run_info.get("status") != "completed"
        or run_info.get("conclusion") != "success"
        or str(repository.get("full_name", "")).lower() != GITHUB_REPOSITORY
        or type(run_info.get("run_attempt")) is not int
        or run_info.get("run_attempt") < 1
        or run_info.get("html_url")
        != f"https://github.com/{GITHUB_REPOSITORY}/actions/runs/{run_id}"
    ):
        raise RuntimeError(
            "CI run is not a successful push of the exact main candidate"
        )
    workflow_id = run_info.get("workflow_id")
    if type(workflow_id) is not int or workflow_id < 1:
        raise RuntimeError("CI run has no valid workflow identity")
    workflow = github_json(f"actions/workflows/{workflow_id}")
    if (
        workflow.get("path") != ".github/workflows/ci.yml"
        or workflow.get("state") != "active"
    ):
        raise RuntimeError("CI run did not use the active pinned CI workflow")

    artifacts = github_json(f"actions/runs/{run_id}/artifacts?per_page=100")
    artifact_rows = complete_ci_artifact_listing(artifacts)
    artifact_matches = [
        item
        for item in artifact_rows
        if isinstance(item, dict)
        if item.get("name") == "test-selection-receipt"
    ]
    if len(artifact_matches) != 1:
        raise RuntimeError("CI run must contain exactly one selection receipt artifact")
    selection, artifact_digest = ci_selection_artifact(int(run_id), artifact_matches[0])
    committed = json.loads(
        run(
            "git",
            "-C",
            str(directory),
            "show",
            f"{sha}:.github/test-selection.json",
            capture=True,
        )
    )
    if selection != committed:
        raise RuntimeError(
            "CI selection receipt differs from the candidate's committed plan"
        )
    with tempfile.TemporaryDirectory(prefix="nebula-ci-acceptance-") as temporary:
        plan_path = Path(temporary) / "test-selection.json"
        plan_path.write_text(json.dumps(committed, sort_keys=True) + "\n")
        validator_path = Path(temporary) / "test_selection.py"
        validator_path.write_text(
            run(
                "git",
                "-C",
                str(directory),
                "show",
                f"{trusted_baseline}:scripts/test_selection.py",
                capture=True,
            )
        )
        validated = json.loads(
            run(
                "/usr/bin/python3",
                str(validator_path),
                "--baseline",
                previous,
                "--candidate",
                sha,
                "--plan",
                str(plan_path),
                cwd=directory,
                capture=True,
            )
        )
    if validated.get("coverage_review_required"):
        raise RuntimeError("Candidate selection plan fails independent validation")
    digest = validated.get("change_digest")
    if not isinstance(digest, str) or selection.get("change_digest") != digest:
        raise RuntimeError(
            "CI selection digest does not bind the live-baseline source diff"
        )

    jobs: list[dict[str, Any]] = []
    page = 1
    while True:
        result = github_json(f"actions/runs/{run_id}/jobs?per_page=100&page={page}")
        current = result.get("jobs")
        if not isinstance(current, list):
            raise RuntimeError("GitHub CI jobs response is malformed")
        jobs.extend(current)
        if len(current) < 100:
            break
        page += 1
        if page > 20:
            raise RuntimeError("GitHub CI job list exceeds its bound")
    required_jobs = selected_ci_jobs(selection)
    selected_conclusions = require_ci_jobs(jobs, required_jobs)
    return {
        "source": "github-actions-ci",
        "run_id": int(run_id),
        "run_url": run_info.get("html_url"),
        "run_attempt": run_info.get("run_attempt"),
        "workflow_path": workflow["path"],
        "commit": sha,
        "baseline": previous,
        "selection_change_digest": digest,
        "selection_plan_sha256": hashlib.sha256(
            json.dumps(selection, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "artifact_sha256": artifact_digest,
        "selected_jobs": selected_conclusions,
    }


def verify_ci_acceptance_chain(
    run_ids: list[str], *, previous: str, sha: str, directory: Path
) -> dict[str, Any]:
    commits = first_parent_commits(directory, previous=previous, sha=sha)
    if len(run_ids) != len(commits):
        raise RuntimeError(
            "CI acceptance requires exactly one run for each first-parent commit"
        )
    if len(set(run_ids)) != len(run_ids):
        raise RuntimeError("CI acceptance run IDs must be unique")

    receipts: list[dict[str, Any]] = []
    baseline = previous
    for run_id, commit in zip(run_ids, commits, strict=True):
        receipt = verify_ci_acceptance(
            run_id,
            previous=baseline,
            sha=commit,
            directory=directory,
            trusted_baseline=previous,
        )
        if receipt.get("baseline") != baseline or receipt.get("commit") != commit:
            raise RuntimeError("CI acceptance receipt does not match its chain position")
        receipts.append(receipt)
        baseline = commit
    if baseline != sha:
        raise RuntimeError("CI acceptance chain does not end at the candidate")
    if len(receipts) == 1:
        return receipts[0]
    return {
        "source": "github-actions-ci-chain",
        "baseline": previous,
        "commit": sha,
        "receipts": receipts,
    }


def validate_ci_acceptance_record(
    ci: dict[str, Any], *, previous: str, sha: str,
    expected_commits: list[str] | None = None,
) -> None:
    def validate_run(item: dict[str, Any], *, baseline: str, commit: str) -> None:
        run_id = item.get("run_id")
        if (
            not SHA.fullmatch(baseline)
            or not SHA.fullmatch(commit)
            or item.get("source") != "github-actions-ci"
            or item.get("commit") != commit
            or item.get("baseline") != baseline
            or type(run_id) is not int
            or run_id < 1
            or type(item.get("run_attempt")) is not int
            or item["run_attempt"] < 1
            or item.get("run_url")
            != f"https://github.com/{GITHUB_REPOSITORY}/actions/runs/{run_id}"
            or item.get("workflow_path") != ".github/workflows/ci.yml"
            or not SHA256.fullmatch(str(item.get("artifact_sha256", "")))
            or not SHA256.fullmatch(str(item.get("selection_change_digest", "")))
            or not SHA256.fullmatch(str(item.get("selection_plan_sha256", "")))
            or not isinstance(item.get("selected_jobs"), dict)
            or not item["selected_jobs"]
            or any(value != "success" for value in item["selected_jobs"].values())
        ):
            raise RuntimeError("release CI acceptance record is invalid")

    if ci.get("source") == "github-actions-ci":
        validate_run(ci, baseline=previous, commit=sha)
        if expected_commits is not None and expected_commits != [sha]:
            raise RuntimeError("release CI acceptance record is invalid")
        return
    if (
        ci.get("source") != "github-actions-ci-chain"
        or ci.get("baseline") != previous
        or ci.get("commit") != sha
        or not SHA.fullmatch(previous)
        or not SHA.fullmatch(sha)
        or not isinstance(ci.get("receipts"), list)
        or len(ci["receipts"]) < 2
    ):
        raise RuntimeError("release CI acceptance record is invalid")
    baseline = previous
    run_ids: set[int] = set()
    for item in ci["receipts"]:
        if not isinstance(item, dict):
            raise RuntimeError("release CI acceptance record is invalid")
        commit = item.get("commit")
        if not isinstance(commit, str):
            raise RuntimeError("release CI acceptance record is invalid")
        validate_run(item, baseline=baseline, commit=commit)
        run_id = item["run_id"]
        if run_id in run_ids:
            raise RuntimeError("release CI acceptance record is invalid")
        run_ids.add(run_id)
        baseline = item["commit"]
    if baseline != sha:
        raise RuntimeError("release CI acceptance record is invalid")
    if expected_commits is not None and [
        item["commit"] for item in ci["receipts"]
    ] != expected_commits:
        raise RuntimeError("release CI acceptance record is invalid")


def ci_acceptance_receipts(ci: dict[str, Any]) -> list[dict[str, Any]]:
    if ci.get("source") == "github-actions-ci":
        return [ci]
    receipts = ci.get("receipts")
    if ci.get("source") == "github-actions-ci-chain" and isinstance(receipts, list):
        return receipts
    raise RuntimeError("CI acceptance record has an unsupported source")


def browser_args(entry: dict[str, Any]) -> list[str]:
    test_match, project, shard = (
        entry.get(key) for key in ("test_match", "project", "shard")
    )
    if not all(
        isinstance(value, str) and value for value in (test_match, project, shard)
    ):
        raise RuntimeError("Playwright selection has an invalid entry")
    if not test_match.startswith("tests/") or not test_match.endswith(
        (".spec.ts", ".spec.tsx")
    ):
        raise RuntimeError("Playwright selection must name an individual spec")
    args = [test_match, "--project", project, "--workers", "1", "--shard", shard]
    if entry.get("grep"):
        args += ["--grep", str(entry["grep"])]
    if project == "real-core" and shard != "1/1":
        args.append("--fully-parallel")
    return args


def write_runtime_manifest(directory: Path, version: str, **compatibility) -> str:
    runtime = directory / "dist/nebula-runtime"
    executable = runtime / "bin/nebula-core"
    if not executable.is_file() or executable.is_symlink():
        raise RuntimeError("The retained onedir Core executable is missing")
    files = {}
    for path in [
        *sorted(runtime.rglob("*")),
        *sorted((directory / "ui/dist").rglob("*")),
    ]:
        if path.is_symlink():
            raise RuntimeError("Runtime bundles cannot contain symlinked assets")
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = sha256(path)
    manifest = {
        "format": "nebula.runtime-bundle/v1",
        "version": version,
        "supervisor_protocol": "nebula.runtime/v1",
        "core_executable": "dist/nebula-runtime/bin/nebula-core",
        "ui_directory": "ui/dist",
        "files": files,
    }
    for key in (
        "supervisor_protocol",
        "api_protocol_min",
        "api_protocol_max",
        "execution_protocol_min",
        "execution_protocol_max",
        "schema_family",
        "schema_min",
        "schema_max",
        "additive_migrations",
        "migration_head",
        "features",
        "required_api_features",
        "restorable_resources",
        "execution_reserve_bytes",
    ):
        if key in compatibility:
            manifest[key] = compatibility[key]
    atomic_json(directory / "nebula-runtime.json", manifest)
    return sha256(directory / "nebula-runtime.json")


def copy_canonical_runtime_bundle(directory: Path, staging: Path) -> None:
    source = directory / "ui/src-tauri/resources/nebula-runtime"
    manifest = source / "nebula-runtime.json"
    checksum = source / "nebula-runtime.sha256"
    if source.is_symlink() or not manifest.is_file() or not checksum.is_file():
        raise RuntimeError(
            "The built canonical runtime requires its manifest and digest"
        )
    if any(path.is_symlink() for path in source.rglob("*")):
        raise RuntimeError("Canonical runtime assets cannot contain symlinks")
    expected = checksum.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or sha256(manifest) != expected:
        raise RuntimeError("The canonical runtime manifest does not match its digest")
    destination = staging / "dist/nebula-runtime"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)


def prepare(*, ci_run_ids: list[str] | None = None) -> dict[str, Any]:
    git_source()
    previous = baseline()
    run("git", "-C", str(REPO), "fetch", "--no-tags", "origin", "main")
    sha = run("git", "-C", str(REPO), "rev-parse", "origin/main", capture=True)
    if sha == previous:
        if ci_run_ids is not None:
            raise RuntimeError("CI-backed preparation requires a new main candidate")
        return {"status": "current", "commit": sha}
    if not SHA.fullmatch(sha):
        raise RuntimeError("origin/main is not an exact commit")
    run("git", "-C", str(REPO), "merge-base", "--is-ancestor", previous, sha)
    if (RELEASES / sha).is_dir():
        release = verified_release(sha)
        receipt = json.loads((release / "simulation-acceptance.json").read_text())
        if receipt.get("previous") != previous:
            raise RuntimeError(
                "accepted candidate was reviewed against a different live commit"
            )
        if ci_run_ids is not None:
            accepted_ci = verify_ci_acceptance_chain(
                ci_run_ids, previous=previous, sha=sha, directory=candidate(sha)
            )
            if receipt.get("ci_acceptance") != accepted_ci:
                raise RuntimeError(
                    "immutable release was not accepted by the requested CI run"
                )
        current_state = state()
        current_state["pending"] = sha
        atomic_json(STATE, current_state)
        return receipt
    directory = candidate(sha)
    ci_acceptance = (
        verify_ci_acceptance_chain(
            ci_run_ids, previous=previous, sha=sha, directory=directory
        )
        if ci_run_ids is not None
        else None
    )
    plan, entries = selected_plan(directory, previous)
    if ci_acceptance is not None:
        reviewed = plan.get("reviewed_commits", [])
        receipts = ci_acceptance_receipts(ci_acceptance)
        accepted_commits = [
            {
                "commit": item["commit"],
                "baseline": item["baseline"],
                "change_digest": item["selection_change_digest"],
            }
            for item in receipts
        ]
        if reviewed != accepted_commits:
            raise RuntimeError(
                "deployer selection does not match the ordered CI receipt chain"
            )
    run(POETRY, "check", cwd=directory)
    run(POETRY, "install", "--with", "dev", "--no-interaction", cwd=directory)
    run("npm", "--prefix", "ui", "ci", "--no-audit", "--no-fund", cwd=directory)
    collected = 0
    if ci_acceptance is None:
        for target in plan["python"]:
            listed = run(
                POETRY,
                "run",
                "pytest",
                "--collect-only",
                "-q",
                target,
                cwd=directory,
                capture=True,
            )
            match = re.search(r"(\d+) tests? collected", listed)
            if match is None:
                raise RuntimeError("selected Python target collected no tests")
            collected += int(match.group(1))
        if collected != plan["expected_tests"]["python"]:
            raise RuntimeError(
                "Python collection disagrees with the reviewed test count"
            )
        if plan["frontend"]:
            listed_frontend = run(
                str(directory / "ui/node_modules/.bin/vitest"),
                "list",
                *plan["frontend"],
                cwd=directory,
                capture=True,
            )
            frontend_count = sum(
                line.startswith("ui/src/") for line in listed_frontend.splitlines()
            )
        else:
            frontend_count = 0
    else:
        collected = plan["expected_tests"]["python"]
        frontend_count = plan["expected_tests"]["frontend"]
    if ci_acceptance is None:
        if frontend_count != plan["expected_tests"]["frontend"]:
            raise RuntimeError(
                "frontend collection disagrees with the reviewed test count"
            )
        for target in plan["python"]:
            run(POETRY, "run", "pytest", "-q", target, cwd=directory)
        for target in plan["frontend"]:
            run(
                "npm",
                "--prefix",
                "ui",
                "test",
                "--",
                target.removeprefix("ui/"),
                cwd=directory,
            )
    # Native selections are exact Rust test names; a Linux host must not claim
    # to have run macOS-only cases. Keep that candidate pending for review.
    if plan["native"] and ci_acceptance is None:
        raise RuntimeError("selected native tests need an approved macOS runner")
    run("npm", "--prefix", "ui", "run", "lint", "--if-present", cwd=directory)
    run("npm", "--prefix", "ui", "run", "audit:diagnostics", cwd=directory)
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    build_env = {"NEBULA_BUILD_TIMESTAMP": timestamp}
    run("npm", "--prefix", "ui", "run", "build", cwd=directory, env=build_env)
    run(
        POETRY,
        "run",
        "python",
        "-m",
        "scripts.build_nebula_core",
        cwd=directory,
        env=build_env,
    )
    run(
        POETRY,
        "run",
        "python",
        "-m",
        "scripts.verify_stabilization_build",
        "--web",
        "ui/dist",
        "--core-identity",
        "build/nebula-core-metadata/BUILD_INFO.json",
        "--commit",
        sha,
        cwd=directory,
        capture=True,
    )
    test_python = ""
    if ci_acceptance is None and entries:
        test_python = run(
            POETRY, "env", "info", "--executable", cwd=directory, capture=True
        )
        if not Path(test_python).is_file() or not os.access(test_python, os.X_OK):
            raise RuntimeError(
                "selected browser tests require the candidate Python interpreter"
            )
    browser_env = {
        "NEBULA_TEST_PYTHON": test_python,
        "NEBULA_TEST_CORE_BIN": str(directory / "dist/nebula-core"),
        # The production unit runs `serve` with the built static directory.
        # Real-Core LAN tests must exercise that HTTPS path, not `ui` mode,
        # which does not forward the fixture's TLS certificate options.
        "NEBULA_TEST_CORE_EMBEDDED_UI": "0",
        "NEBULA_UI_TEST_COMMAND": "npm run preview -- --host 0.0.0.0 --port 1420",
    }
    listed = 0
    if ci_acceptance is None:
        for entry in entries:
            args = browser_args(entry)
            result = run(
                "npm",
                "--prefix",
                "ui",
                "run",
                "test:e2e",
                "--",
                *args,
                "--list",
                cwd=directory,
                env=browser_env,
                capture=True,
            )
            match = re.search(r"Total: (\d+) tests? in", result)
            if match is None or int(match.group(1)) < 1:
                raise RuntimeError("selected browser entry collected no tests")
            listed += int(match.group(1))
        if listed != plan["expected_tests"]["playwright"]:
            raise RuntimeError(
                "browser collection disagrees with the reviewed test count"
            )
        for entry in entries:
            run(
                "npm",
                "--prefix",
                "ui",
                "run",
                "test:e2e",
                "--",
                *browser_args(entry),
                cwd=directory,
                env=browser_env,
                timeout=600,
            )
    else:
        listed = plan["expected_tests"]["playwright"]
    if commit_at(directory) != sha:
        raise RuntimeError("candidate source changed during preparation")
    run("git", "-C", str(directory), "diff", "--quiet")
    release = RELEASES / sha
    if release.exists():
        raise RuntimeError(
            "immutable release already exists without an accepted receipt"
        )
    RELEASES.mkdir(parents=True, exist_ok=True)
    staging = RELEASES / f".{sha}.{os.getpid()}.new"
    staging.mkdir(mode=0o700)
    archive = ROOT / f".source-{sha}.{os.getpid()}.tar"
    try:
        run(
            "git",
            "-C",
            str(directory),
            "archive",
            "--format=tar",
            f"--output={archive}",
            sha,
        )
        # The release source is the Git tree, not the candidate's untracked
        # build products or a copied developer checkout.
        with tarfile.open(archive) as source:
            source.extractall(staging, filter="data")
    finally:
        archive.unlink(missing_ok=True)
    (staging / "dist").mkdir(exist_ok=True)
    shutil.copy2(directory / "dist/nebula-core", staging / "dist/nebula-core")
    copy_canonical_runtime_bundle(directory, staging)
    shutil.copytree(directory / "ui/dist", staging / "ui/dist", dirs_exist_ok=True)
    (staging / "build").mkdir(exist_ok=True)
    shutil.copytree(
        directory / "build/nebula-core-metadata", staging / "build/nebula-core-metadata"
    )
    shutil.copy2(
        directory / "build/simulation-test-selection.json",
        staging / "build/simulation-test-selection.json",
    )
    shutil.copy2(
        directory / "build/simulation-playwright-receipt.md",
        staging / "build/simulation-playwright-receipt.md",
    )
    compatibility = json.loads(
        run(
            POETRY,
            "run",
            "python",
            "-c",
            "import json; from nebula.v3.runtime_bundle import load_compatibility; "
            "print(json.dumps(load_compatibility()))",
            cwd=directory,
            capture=True,
        )
    )
    runtime_digest = write_runtime_manifest(staging, sha, **compatibility)
    receipt = {
        "commit": sha,
        "previous": previous,
        "built_at": timestamp,
        "python_tests": plan["expected_tests"]["python"],
        "frontend_tests": frontend_count,
        "browser_tests": listed,
        "core_sha256": sha256(staging / "dist/nebula-core"),
        "web_manifest_sha256": sha256(staging / "ui/dist/web-build.json"),
        "test_selection_sha256": sha256(staging / ".github/test-selection.json"),
        "cumulative_selection_sha256": sha256(
            staging / "build/simulation-test-selection.json"
        ),
        "status": "accepted_candidate",
        "runtime_manifest_sha256": runtime_digest,
    }
    if ci_acceptance is not None:
        receipt["ci_acceptance"] = ci_acceptance
    atomic_json(staging / "simulation-acceptance.json", receipt)
    os.replace(staging, release)
    for path in release.rglob("*"):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)
    release.chmod(release.stat().st_mode & ~0o222)
    current_state = state()
    current_state["pending"] = sha
    atomic_json(STATE, current_state)
    return receipt


def verified_release(sha: str) -> Path:
    if not SHA.fullmatch(sha):
        raise RuntimeError("release commit is invalid")
    release = RELEASES / sha
    if release.is_symlink() or release.resolve(strict=True).parent != RELEASES:
        raise RuntimeError("immutable release path is unsafe")
    receipt = json.loads((release / "simulation-acceptance.json").read_text())
    if receipt.get("commit") != sha or receipt.get("status") != "accepted_candidate":
        raise RuntimeError("release acceptance receipt is missing or mismatched")
    for relative, field in (
        ("dist/nebula-core", "core_sha256"),
        ("ui/dist/web-build.json", "web_manifest_sha256"),
        (".github/test-selection.json", "test_selection_sha256"),
        ("build/simulation-test-selection.json", "cumulative_selection_sha256"),
    ):
        expected = receipt.get(field)
        if expected is not None and sha256(release / relative) != expected:
            raise RuntimeError(f"release {relative} changed after acceptance")
    ci = receipt.get("ci_acceptance")
    if ci is not None:
        previous = receipt.get("previous")
        if not isinstance(ci, dict) or not isinstance(previous, str):
            raise RuntimeError("release CI acceptance record is invalid")
        validate_ci_acceptance_record(
            ci,
            previous=previous,
            sha=sha,
            expected_commits=first_parent_commits(
                REPO, previous=previous, sha=sha
            ),
        )
    return release


def api() -> CoreClient:
    return CoreClient("http://127.0.0.1:8000", service_token(TOKEN_FILE))


def service(action: str) -> None:
    if action not in {"start", "stop"}:
        raise ValueError("unsupported service action")
    run("systemctl", "--user", action, SERVICE, timeout=180)


def edge(action: str) -> None:
    if action not in {"start", "stop"}:
        raise ValueError("unsupported edge action")
    run("systemctl", "--user", action, EDGE, timeout=60)


def unit_state(unit: str) -> str:
    return run(
        "systemctl",
        "--user",
        "show",
        unit,
        "-p",
        "ActiveState",
        "--value",
        capture=True,
    )


def snapshot(label: str) -> Path:
    if not DATA.is_dir():
        raise RuntimeError("production Nebula data directory is missing")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
        raise ValueError("rollback label must be a single safe path component")
    backup = BACKUPS / label
    if backup.exists() or backup.is_symlink():
        raise RuntimeError("rollback backup already exists")
    backup.parent.mkdir(parents=True, exist_ok=True)
    staging = BACKUPS / f".{label}.{os.getpid()}.partial"
    if staging.exists() or staging.is_symlink():
        raise RuntimeError("rollback staging path already exists")
    postgres = configured_postgres(TOKEN_FILE)
    sqlite_databases = tuple(
        name for name in SQLITE_DATABASES if name != "nebula.db" or postgres is None
    )
    database_paths = {DATA / relative for relative in SQLITE_DATABASES}
    excluded = database_paths | {
        Path(f"{database}-{suffix}")
        for database in database_paths
        for suffix in ("wal", "shm")
    }

    def ignore_sidecars(path: str, names: list[str]) -> list[str]:
        directory = Path(path)
        return [
            name
            for name in names
            if directory / name in excluded
            or (directory == DATA and name in BACKUP_FILES)
        ]

    try:
        shutil.copytree(DATA, staging, symlinks=True, ignore=ignore_sidecars)
        if postgres is not None:
            snapshot_postgres(staging, postgres)
        for relative in sqlite_databases:
            source_path = DATA / relative
            if not source_path.is_file() or source_path.is_symlink():
                raise RuntimeError(f"rollback SQLite source is missing: {relative}")
            destination_path = staging / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
            try:
                destination = sqlite3.connect(destination_path)
                try:
                    source.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone() != (
                        "ok",
                    ):
                        raise RuntimeError(
                            f"rollback SQLite backup failed integrity check: {relative}"
                        )
                finally:
                    destination.close()
            finally:
                source.close()
        if backup.exists() or backup.is_symlink():
            raise RuntimeError("rollback backup appeared during snapshot")
        os.replace(staging, backup)
    except BaseException:
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise
    return backup


def retain_single_rollback_snapshot(keep: Path) -> list[str]:
    """Remove superseded coherent snapshots after a healthy cutover.

    The deploy lock is held by the caller. Validate every top-level entry before
    deleting anything; an unfamiliar or mounted path needs operator review.
    """

    if keep.parent != BACKUPS or keep.is_symlink() or not keep.is_dir():
        raise RuntimeError("current rollback snapshot is not a local directory")
    if not contains_database_backup(keep):
        raise RuntimeError("current rollback snapshot has no regular database")
    entries = sorted(BACKUPS.iterdir(), key=lambda path: path.name)
    if keep not in entries:
        raise RuntimeError("current rollback snapshot is missing")
    mounts = {
        Path(line.split()[4].replace("\\040", " "))
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
    }
    obsolete = [path for path in entries if path != keep]
    for path in obsolete:
        if (
            not re.fullmatch(r"(?:initial|pre|post)-[A-Za-z0-9._-]+", path.name)
            or not stat.S_ISDIR(path.lstat().st_mode)
            or path.is_symlink()
            or any(mount == path or path in mount.parents for mount in mounts)
        ):
            raise RuntimeError(f"rollback cleanup needs review: {path.name}")
    for path in obsolete:
        shutil.rmtree(path)
    if list(BACKUPS.iterdir()) != [keep]:
        raise RuntimeError("rollback cleanup did not leave exactly one snapshot")
    return [path.name for path in obsolete]


def finish_rollback_retention(record: dict[str, Any], keep: Path) -> dict[str, Any]:
    """Report cleanup separately from the already committed Core promotion."""

    try:
        removed = retain_single_rollback_snapshot(keep)
    except (OSError, RuntimeError, ValueError) as exc:
        result = {
            "status": "needs_attention",
            "retained": str(keep),
            "reason": str(exc)[:300],
        }
    else:
        for field in ("initial_backup", "post_state"):
            previous = record.get(field)
            if (
                isinstance(previous, str)
                and Path(previous).parent == BACKUPS
                and Path(previous) != keep
                and not Path(previous).exists()
            ):
                record.pop(field)
        result = {
            "status": "complete",
            "retained": str(keep),
            "removed": removed,
        }
    record["rollback_retention"] = result
    try:
        atomic_json(STATE, record)
    except OSError as exc:
        result = {
            "status": "needs_attention",
            "retained": str(keep),
            "reason": f"retention result could not be recorded: {exc}"[:300],
            "removed": result.get("removed", []),
        }
    return result


def require_stopped() -> None:
    state_name = unit_state(SERVICE)
    if state_name != "inactive":
        raise RuntimeError(f"Nebula Core writer is still {state_name}")


def switch_to(release: Path) -> None:
    temporary = CURRENT.with_name(f".current.{os.getpid()}.new")
    temporary.symlink_to(release)
    os.replace(temporary, CURRENT)


def wait_ready(commit: str) -> None:
    client = api()
    until = time.monotonic() + 480
    while time.monotonic() < until:
        try:
            health = client.get("health")
            if (
                not isinstance(health, dict)
                or health.get("commit") != commit
                or health.get("status") != "ok"
            ):
                raise ValueError("Core health identity or status is invalid")
            request = Request("http://127.0.0.1:8000/web-build.json")
            with urlopen(request, timeout=10) as response:
                web = json.load(response)
            if web.get("commit") == commit and web.get("dirty") is False:
                return
        except (OSError, ValueError, TimeoutError):
            time.sleep(2)
    raise RuntimeError("Core did not serve its accepted build within 480 seconds")


def legacy_promote() -> dict[str, Any]:
    record = state()
    new = record.get("pending")
    if not isinstance(new, str):
        if record.get("post_state"):
            return {
                "status": "no_pending_candidate",
                "rollback_retention": {
                    "status": "deferred_recovery",
                    "reason": "A rollback preserved failed post-deployment state for review.",
                },
            }
        backup = record.get("rollback_backup") or record.get("initial_backup")
        if isinstance(backup, str) and backup:
            retention = finish_rollback_retention(record, Path(backup))
            return {"status": "no_pending_candidate", "rollback_retention": retention}
        return {"status": "no_pending_candidate"}
    release = verified_release(new)
    previous = baseline()
    if previous == INITIAL_COMMIT and not CURRENT.is_symlink():
        raise RuntimeError("initial same-build cutover has not been completed")
    if record.get("active") != previous:
        raise RuntimeError("deployer state does not match the running release")
    require_legacy_protocol(record, expected_version=previous)
    attempt = _LegacyAttempt(previous, new)
    command = run(
        "systemctl",
        "--user",
        "show",
        SERVICE,
        "-p",
        "ExecStart",
        "--value",
        capture=True,
    )
    if str(CURRENT / "dist/nebula-core") not in command:
        raise RuntimeError("production unit does not run the guarded current release")
    if unit_state(SERVICE) != "active" or unit_state(EDGE) != "active":
        raise RuntimeError("production Core and HTTPS edge must both be active")
    reasons = blockers(api())
    if reasons:
        return {"status": "pending_active_work", "commit": new, "blockers": reasons}
    # Close the public ingress first, then re-read Core. New phone and desktop
    # work cannot enter between the final audit and stopping its writer.
    edge("stop")
    try:
        reasons = blockers(api())
    except Exception:
        edge("start")
        raise
    if reasons:
        edge("start")
        return {"status": "pending_active_work", "commit": new, "blockers": reasons}
    try:
        service("stop")
        require_stopped()
    except Exception:
        # A packaged Core that reports an unexpected stop result still needs
        # its previous release and public ingress restored before we fail.
        if unit_state(SERVICE) != "active":
            service("start")
        wait_ready(previous)
        edge("start")
        raise
    label = f"pre-{new}-{int(time.time())}"
    try:
        backup = snapshot(label)
    except Exception:
        service("start")
        wait_ready(previous)
        edge("start")
        raise
    record.update(
        {"previous": previous, "rollback_backup": str(backup), "promoting": new}
    )
    atomic_json(STATE, record)
    try:
        switch_to(release)
        service("start")
        wait_ready(new)
        edge("start")
    except Exception:
        try:
            rollback(_legacy_attempt=attempt)
        except Exception as recovery:
            raise RuntimeError(
                f"candidate failed; automatic rollback also failed: {recovery}"
            ) from recovery
        raise
    record.update({"active": new, "pending": None, "promoting": None})
    atomic_json(STATE, record)
    retention = finish_rollback_retention(record, backup)
    return {
        "status": "promoted"
        if retention["status"] == "complete"
        else "promoted_cleanup_incomplete",
        "commit": new,
        "previous": previous,
        "rollback_backup": str(backup),
        "rollback_retention": retention,
    }


def offline_rollback(
    *, _legacy_attempt: _LegacyAttempt | None = None
) -> dict[str, Any]:
    record = state()
    if record.get("deployment_mode") == "supervisor":
        raise RuntimeError(
            "Supervisor code rollback preserves live data; offline restoration requires a separate stopped disaster-recovery procedure"
        )
    require_legacy_protocol(record, attempt=_legacy_attempt)
    failed_commit = record.get("promoting") or record.get("active")
    previous = record.get("previous")
    backup = Path(str(record.get("rollback_backup", "")))
    if not isinstance(previous, str) or backup.parent != BACKUPS or not backup.is_dir():
        raise RuntimeError("a coherent rollback backup is unavailable")
    old_release = RELEASES / previous
    if not old_release.is_dir():
        raise RuntimeError("previous immutable release is missing")
    validate_database_backup(backup, TOKEN_FILE)
    edge("stop")
    service("stop")
    require_stopped()
    preserved = BACKUPS / f"post-{time.time_ns()}"
    record["post_state"] = str(preserved)
    record["rollback_attempt"] = {"status": "preserving", "post_state": str(preserved)}
    atomic_json(STATE, record)
    os.replace(DATA, preserved)
    try:
        postgres = configured_postgres(TOKEN_FILE)
        if postgres is not None:
            # File preservation alone cannot retain failed server database state.
            snapshot_postgres(preserved, postgres)
        restore_database_backup(backup, TOKEN_FILE)
        shutil.copytree(
            backup,
            DATA,
            symlinks=True,
            ignore=lambda path, names: [
                name for name in names if Path(path) == backup and name in BACKUP_FILES
            ],
        )
        switch_to(old_release)
        service("start")
        wait_ready(previous)
        edge("start")
    except Exception as failure:
        service("stop")
        record["rollback_attempt"]["status"] = "incomplete"
        try:
            atomic_json(STATE, record)
            if not DATA.exists():
                shutil.copytree(
                    preserved,
                    DATA,
                    symlinks=True,
                    ignore=lambda path, names: [
                        name
                        for name in names
                        if Path(path) == preserved and name in BACKUP_FILES
                    ],
                )
        except OSError as recovery:
            raise RuntimeError(
                f"rollback incomplete; recover files and database from {preserved}"
            ) from recovery
        raise RuntimeError(
            f"rollback incomplete; Core is stopped and failed state is preserved at {preserved}"
        ) from failure
    record.update(
        {
            "active": previous,
            "pending": failed_commit if failed_commit != previous else None,
            "promoting": None,
            "post_state": str(preserved),
            "rollback_attempt": {"status": "complete", "post_state": str(preserved)},
        }
    )
    atomic_json(STATE, record)
    return {
        "status": "rolled_back",
        "commit": previous,
        "preserved_post_state": str(preserved),
    }


def deployment_post(path: str, payload: dict) -> dict:
    client = api()
    request = Request(
        f"{client.origin}/api/v1/{path}",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {client.token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def runtime_release(sha: str) -> tuple[Path, str]:
    release = verified_release(sha)
    receipt = json.loads((release / "simulation-acceptance.json").read_text())
    digest = receipt.get("runtime_manifest_sha256")
    if not isinstance(digest, str) or sha256(release / "nebula-runtime.json") != digest:
        raise RuntimeError(
            "The accepted release has no verified supervisor runtime manifest"
        )
    return release, digest


def staged_deployer(record: dict) -> StagedDeployer:
    return StagedDeployer(
        record,
        get=api().get,
        post=deployment_post,
        save=lambda value: atomic_json(STATE, value),
        verify=runtime_release,
    )


def adopt_supervisor() -> dict:
    status = api().get("deployment/status")
    if status.get("protocol") != "nebula.runtime/v1" or not isinstance(
        status.get("api_version"), str
    ):
        raise RuntimeError("A healthy supervisor API is required before adoption")
    record = state()
    record.update(deployment_mode="supervisor", active=status["api_version"])
    atomic_json(STATE, record)
    return {"status": "supervisor_adopted", "commit": status["api_version"]}


def promote() -> dict[str, Any]:
    record = state()
    if record.get("deployment_mode") != "supervisor":
        return legacy_promote()
    pending = record.get("pending")
    if not isinstance(pending, str):
        return {
            "status": "no_pending_candidate",
            "deployment": api().get("deployment/status"),
        }
    return staged_deployer(record).promote(pending)


def rollback(*, _legacy_attempt: _LegacyAttempt | None = None) -> dict[str, Any]:
    record = state()
    if record.get("deployment_mode") != "supervisor":
        return offline_rollback(_legacy_attempt=_legacy_attempt)
    previous = record.get("previous")
    if not isinstance(previous, str):
        raise RuntimeError("No retained code rollback target is recorded")
    return staged_deployer(record).rollback(previous)


def bootstrap() -> dict[str, Any]:
    if baseline() != INITIAL_COMMIT or not CURRENT.is_symlink():
        raise RuntimeError(
            "initial live release must be the exact previously deployed build"
        )
    if not DATA.is_dir() or not (DATA / "nebula.db").is_file():
        raise RuntimeError("initial live state is missing")
    initial_backup = BACKUPS / f"initial-{INITIAL_COMMIT}"
    if not initial_backup.is_dir() or not (initial_backup / "nebula.db").is_file():
        raise RuntimeError("initial coherent rollback backup is missing")
    with sqlite3.connect(
        f"file:{initial_backup / 'nebula.db'}?mode=ro&immutable=1", uri=True
    ) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("initial rollback backup failed SQLite quick_check")
    if unit_state(SERVICE) != "active" or unit_state(EDGE) != "active":
        raise RuntimeError("initial Core and HTTPS edge must both be active")
    wait_ready(INITIAL_COMMIT)
    record = state()
    if record.get("active") not in (None, INITIAL_COMMIT):
        raise RuntimeError("deployer already owns a different active commit")
    record["active"] = INITIAL_COMMIT
    record["initial_backup"] = str(initial_backup)
    atomic_json(STATE, record)
    return {"status": "bootstrapped", "commit": INITIAL_COMMIT}


def cycle() -> dict[str, Any]:
    prepared = prepare()
    return {"prepared": prepared, "promotion": promote()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "promote",
            "rollback",
            "offline-rollback",
            "adopt-supervisor",
            "bootstrap",
            "cycle",
            "status",
        ),
    )
    parser.add_argument(
        "--ci-run-id",
        action="append",
        help=(
            "accept selected checks from a successful exact-main CI push run; "
            "repeat once per first-parent commit from the live baseline to the candidate"
        ),
    )
    args = parser.parse_args()
    checked_host()
    if args.ci_run_id is not None and args.action != "prepare":
        raise RuntimeError("--ci-run-id is supported only with prepare")
    with (ROOT / ".deploy.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = (
            {"status": "state", **state()}
            if args.action == "status"
            else prepare(ci_run_ids=args.ci_run_id)
            if args.action == "prepare"
            else globals()[args.action.replace("-", "_")]()
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
        print(f"simulation Nebula deployer: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
