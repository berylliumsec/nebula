#!/usr/bin/env python3
"""Prepare exact commits and promote through the selected deployment protocol.

Install beside nebula_deploy_gate.py and nebula_staged_deployer.py on simulation. The initial same-build
cutover provisions current and its user service separately; this deployer
handles later main updates. It never changes the development checkout.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
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
from uuid import uuid4
from typing import Any
from urllib.parse import urlsplit
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


def github_json_value(path: str) -> Any:
    if path.startswith("/") or ".." in Path(path).parts:
        raise RuntimeError("GitHub API path is invalid")
    raw = github_bytes(
        f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/{path}", limit=8 * 1024**2
    )
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("GitHub API returned invalid JSON") from error
    if not isinstance(value, (dict, list)):
        raise RuntimeError("GitHub API returned an unexpected response")
    return value


def github_json(path: str) -> dict[str, Any]:
    value = github_json_value(path)
    if not isinstance(value, dict):
        raise RuntimeError("GitHub API returned an unexpected response")
    return value


def ci_selection_artifact(
    run_id: int, artifact: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    archive, expected_digest = ci_artifact_archive(run_id, artifact)
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


def ci_artifact_archive(run_id: int, artifact: dict[str, Any]) -> tuple[bytes, str]:
    workflow_run = artifact.get("workflow_run")
    if isinstance(workflow_run, dict) and workflow_run.get("id") != run_id:
        raise RuntimeError("GitHub CI artifact belongs to another run")
    digest = artifact.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise RuntimeError("GitHub CI artifact has no SHA-256 digest")
    expected_digest = digest.removeprefix("sha256:")
    if not SHA256.fullmatch(expected_digest):
        raise RuntimeError("GitHub CI artifact digest is malformed")
    size = artifact.get("size_in_bytes")
    if (
        artifact.get("expired") is not False
        or type(size) is not int
        or size < 1
        or size > CI_ARTIFACT_LIMIT
    ):
        raise RuntimeError("GitHub CI artifact is expired or exceeds its bound")
    artifact_id = artifact.get("id")
    if type(artifact_id) is not int or artifact_id < 1:
        raise RuntimeError("GitHub CI artifact identity is invalid")
    archive = github_bytes(
        f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/actions/artifacts/{artifact_id}/zip",
        limit=CI_ARTIFACT_LIMIT,
    )
    if len(archive) != size:
        raise RuntimeError("GitHub CI artifact size does not match its metadata")
    if hashlib.sha256(archive).hexdigest() != expected_digest:
        raise RuntimeError("GitHub CI artifact SHA-256 does not match")
    return archive, expected_digest


def named_ci_artifact(run_id: int, name: str) -> tuple[bytes, dict[str, Any], str]:
    artifacts = complete_ci_artifact_listing(
        github_json(f"actions/runs/{run_id}/artifacts?per_page=100")
    )
    matches = [item for item in artifacts if item.get("name") == name]
    if len(matches) != 1:
        raise RuntimeError(f"CI run must contain exactly one {name} artifact")
    archive, digest = ci_artifact_archive(run_id, matches[0])
    return archive, matches[0], digest


def require_unexpired_artifact(artifact: dict[str, Any]) -> str:
    expires_at = artifact.get("expires_at")
    if not isinstance(expires_at, str):
        raise RuntimeError("Supplemental CI artifact has no expiry timestamp")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("Supplemental CI artifact expiry is malformed") from error
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise RuntimeError("Supplemental CI artifact is expired")
    return expires_at


def selected_ci_jobs(
    plan: dict[str, Any], *, supplemental_playwright_verified: bool = False
) -> set[str]:
    if plan.get("playwright") and not supplemental_playwright_verified:
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
            "scripts/playwright_impact.py",
        )
    except RuntimeError as error:
        raise RuntimeError(
            "CI acceptance refuses changes to trusted workflows, local actions, "
            "or the test-selection and Playwright validators"
        ) from error


def validate_ci_selection(
    selection: dict[str, Any],
    *,
    previous: str,
    sha: str,
    directory: Path,
    trusted_baseline: str,
) -> tuple[str, str]:
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
    with tempfile.TemporaryDirectory(prefix="nebula-ci-selection-") as temporary:
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
    selection_hash = hashlib.sha256(
        json.dumps(selection, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return digest, selection_hash


def commit_parents(directory: Path, sha: str) -> list[str]:
    values = run(
        "git",
        "-C",
        str(directory),
        "rev-list",
        "--parents",
        "-n",
        "1",
        sha,
        capture=True,
    ).split()
    if not values or values[0] != sha:
        raise RuntimeError("Git returned an invalid commit identity")
    return values[1:]


def git_tree(directory: Path, sha: str) -> str:
    tree = run(
        "git", "-C", str(directory), "rev-parse", f"{sha}^{{tree}}", capture=True
    )
    if not SHA.fullmatch(tree):
        raise RuntimeError("Git returned an invalid tree identity")
    return tree


def require_merged_pr_for_commit(
    *, head_sha: str, base_sha: str, merge_sha: str, directory: Path
) -> dict[str, Any]:
    pulls = github_json_value(f"commits/{head_sha}/pulls")
    if not isinstance(pulls, list):
        raise RuntimeError("GitHub associated pull request listing is malformed")
    matches = [
        item
        for item in pulls
        if isinstance(item, dict)
        and item.get("state") == "closed"
        and item.get("merged_at")
        and item.get("merge_commit_sha") == merge_sha
        and isinstance(item.get("base"), dict)
        and item["base"].get("sha") == base_sha
        and isinstance(item.get("head"), dict)
        and item["head"].get("sha") == head_sha
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "PR CI evidence is not bound to one exact merged pull request"
        )
    if not isinstance(matches[0].get("number"), int) or matches[0]["number"] < 1:
        raise RuntimeError("Merged pull request identity is invalid")
    parent_list = commit_parents(directory, merge_sha)
    if len(parent_list) != 2 or parent_list != [base_sha, head_sha]:
        raise RuntimeError("PR CI evidence does not match the merge commit parents")
    merge_tree = git_tree(directory, merge_sha)
    if git_tree(directory, head_sha) != merge_tree:
        raise RuntimeError("PR head tree differs from the exact main merge tree")
    return matches[0]


def bind_pr_run(
    run_id: str,
    *,
    head_sha: str,
    base_sha: str,
    merge_sha: str,
    workflow_path: str,
    directory: Path,
) -> dict[str, Any]:
    if not CI_RUN_ID.fullmatch(run_id):
        raise RuntimeError(
            "Supplemental PR run id must be a positive decimal identifier"
        )
    run_info = github_json(f"actions/runs/{run_id}")
    repository = run_info.get("repository")
    if not isinstance(repository, dict):
        repository = {}
    if (
        run_info.get("head_sha") != head_sha
        or run_info.get("event") != "pull_request"
        or run_info.get("status") != "completed"
        or run_info.get("conclusion") != "success"
        or str(repository.get("full_name", "")).lower() != GITHUB_REPOSITORY
        or type(run_info.get("run_attempt")) is not int
        or run_info.get("run_attempt") < 1
        or run_info.get("html_url")
        != f"https://github.com/{GITHUB_REPOSITORY}/actions/runs/{run_id}"
    ):
        raise RuntimeError("Supplemental PR run is not successful on the exact PR head")
    workflow_id = run_info.get("workflow_id")
    if type(workflow_id) is not int or workflow_id < 1:
        raise RuntimeError("Supplemental PR run has no valid workflow identity")
    workflow = github_json(f"actions/workflows/{workflow_id}")
    if workflow.get("path") != workflow_path or workflow.get("state") != "active":
        raise RuntimeError("Supplemental PR run did not use the pinned active workflow")
    pull = require_merged_pr_for_commit(
        head_sha=head_sha,
        base_sha=base_sha,
        merge_sha=merge_sha,
        directory=directory,
    )
    if run_info.get("head_branch") != pull.get("head", {}).get("ref"):
        raise RuntimeError(
            "Supplemental PR run branch differs from its merged pull request"
        )
    return {
        "run_id": int(run_id),
        "run_url": run_info["html_url"],
        "run_attempt": run_info["run_attempt"],
        "workflow_path": workflow_path,
        "head_commit": head_sha,
        "base_commit": base_sha,
        "merge_commit": merge_sha,
        "tree_sha": git_tree(directory, merge_sha),
        "pull_request": pull["number"],
    }


def supplemental_artifact_files(
    run_id: int, artifact_name: str, expected_names: set[str]
) -> tuple[dict[str, bytes], dict[str, Any], str]:
    archive, metadata, digest = named_ci_artifact(run_id, artifact_name)
    require_unexpired_artifact(metadata)
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            files: dict[str, bytes] = {}
            entries = [entry for entry in bundle.infolist() if not entry.is_dir()]
            if len(entries) != len(expected_names):
                raise RuntimeError("Supplemental artifact file set is incomplete")
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or entry.file_size > CI_ARTIFACT_LIMIT
                ):
                    raise RuntimeError("Supplemental artifact contains an unsafe file")
                name = path.name
                if name not in expected_names or name in files:
                    raise RuntimeError("Supplemental artifact file set is invalid")
                files[name] = bundle.read(entry)
            if set(files) != expected_names:
                raise RuntimeError("Supplemental artifact file set is incomplete")
    except (
        OSError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
    ) as error:
        raise RuntimeError("Supplemental CI artifact is not a valid archive") from error
    return files, metadata, digest


def validate_playwright_matrix_jobs(
    jobs: list[dict[str, Any]], include: list[dict[str, Any]]
) -> dict[str, str]:
    def signature(entry: dict[str, Any]) -> tuple[str, ...]:
        fields = ("area", "grep", "project", "runtime", "shard")
        if any(
            not isinstance(entry.get(field), str) or not entry[field]
            for field in fields
        ):
            raise RuntimeError("Playwright matrix entry is malformed")
        return tuple(entry[field] for field in fields)

    expected = Counter(signature(entry) for entry in include)
    actual: Counter[tuple[str, ...]] = Counter()
    allowed_prefixes = {"playwright", "playwright-2", "playwright-3", "playwright-4"}
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str):
            continue
        name = job["name"]
        lane = name.partition(" (")[0]
        if lane not in allowed_prefixes or " (" not in name:
            continue
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            raise RuntimeError("A selected Playwright matrix job did not succeed")
        fields = name.partition(" (")[2].split(", ")
        if len(fields) < 5:
            raise RuntimeError("GitHub Playwright job name lacks its matrix identity")
        actual[tuple(fields[:5])] += 1
    if actual != expected:
        raise RuntimeError(
            "Successful Playwright jobs do not cover every selected entry"
        )
    required = {"plan", "gate"}
    accepted = require_ci_jobs(jobs, required)
    for identity, count in sorted(expected.items()):
        label = json.dumps(identity, separators=(",", ":"))
        for index in range(count):
            accepted[f"playwright:{label}:{index + 1}"] = "success"
    return accepted


def github_run_jobs(run_id: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    page = 1
    while True:
        result = github_json(f"actions/runs/{run_id}/jobs?per_page=100&page={page}")
        current = result.get("jobs")
        if not isinstance(current, list) or any(
            not isinstance(job, dict) for job in current
        ):
            raise RuntimeError("GitHub CI jobs response is malformed")
        jobs.extend(current)
        if len(current) < 100:
            break
        page += 1
        if page > 20:
            raise RuntimeError("GitHub CI job list exceeds its bound")
    return jobs


def verify_pr_ci_supplement(
    run_id: str,
    *,
    head_sha: str,
    base_sha: str,
    merge_sha: str,
    directory: Path,
    trusted_baseline: str,
) -> dict[str, Any]:
    binding = bind_pr_run(
        run_id,
        head_sha=head_sha,
        base_sha=base_sha,
        merge_sha=merge_sha,
        workflow_path=".github/workflows/ci.yml",
        directory=directory,
    )
    verify_ci_trust_inputs(directory, trusted_baseline=trusted_baseline, sha=head_sha)
    artifacts = complete_ci_artifact_listing(
        github_json(f"actions/runs/{run_id}/artifacts?per_page=100")
    )
    matches = [
        artifact
        for artifact in artifacts
        if artifact.get("name") == "test-selection-receipt"
    ]
    if len(matches) != 1:
        raise RuntimeError("PR CI run must contain exactly one selection receipt")
    artifact_expires_at = require_unexpired_artifact(matches[0])
    selection, artifact_digest = ci_selection_artifact(int(run_id), matches[0])
    if not isinstance(selection.get("playwright"), list) or not selection["playwright"]:
        raise RuntimeError("PR CI companion does not validate the browser selection")
    selection_digest, selection_hash = validate_ci_selection(
        selection,
        previous=base_sha,
        sha=head_sha,
        directory=directory,
        trusted_baseline=trusted_baseline,
    )
    jobs = github_run_jobs(int(run_id))
    required = selected_ci_jobs(selection, supplemental_playwright_verified=True)
    selected_jobs = require_ci_jobs(jobs, required)
    return {
        "source": "github-actions-pull-request-ci",
        **binding,
        "selection_change_digest": selection_digest,
        "selection_plan_sha256": selection_hash,
        "artifact_sha256": artifact_digest,
        "artifact_expires_at": artifact_expires_at,
        "selected_jobs": selected_jobs,
    }


def expected_pr_playwright_plan(
    selection: dict[str, Any],
    *,
    head_sha: str,
    base_sha: str,
    directory: Path,
    trusted_baseline: str,
) -> tuple[dict[str, Any], str]:
    with tempfile.TemporaryDirectory(prefix="nebula-pr-playwright-") as temporary:
        temporary_path = Path(temporary)
        script = temporary_path / "playwright_impact.py"
        script.write_text(
            run(
                "git",
                "-C",
                str(directory),
                "show",
                f"{trusted_baseline}:scripts/playwright_impact.py",
                capture=True,
            )
        )
        manifest = temporary_path / "playwright-impact.json"
        manifest.write_text(
            run(
                "git",
                "-C",
                str(directory),
                "show",
                f"{head_sha}:ui/playwright-impact.json",
                capture=True,
            )
        )
        output = temporary_path / "expected-plan.json"
        receipt = temporary_path / "expected-receipt.md"
        reviewed_reason = f"{selection.get('reason', '')} Exclusions: {selection.get('exclusions', '')}"
        selectors = [
            item
            if item.startswith(("area:", "entry:", "project:"))
            else f"entry:{item}"
            for item in selection["playwright"]
        ]
        args = [
            "/usr/bin/python3",
            str(script),
            "select",
            "--manifest",
            str(manifest),
            "--baseline",
            base_sha,
            "--candidate",
            head_sha,
            "--scope",
            "impacted",
            "--review-reason",
            reviewed_reason,
            "--output",
            str(output),
            "--receipt",
            str(receipt),
        ]
        for selector in selectors or ["none"]:
            args.extend(("--selection", selector))
        run(*args, cwd=directory)
        plan = json.loads(output.read_text())
        return plan, receipt.read_text()


def verify_pr_playwright_supplement(
    run_id: str,
    *,
    head_sha: str,
    base_sha: str,
    merge_sha: str,
    directory: Path,
    trusted_baseline: str,
) -> dict[str, Any]:
    binding = bind_pr_run(
        run_id,
        head_sha=head_sha,
        base_sha=base_sha,
        merge_sha=merge_sha,
        workflow_path=".github/workflows/playwright-impact.yml",
        directory=directory,
    )
    verify_ci_trust_inputs(directory, trusted_baseline=trusted_baseline, sha=head_sha)
    files, artifact, artifact_digest = supplemental_artifact_files(
        int(run_id),
        "playwright-impact-receipt",
        {
            "playwright-impact-plan.json",
            "playwright-impact-receipt.md",
            "reviewed-tests.json",
        },
    )
    try:
        selection = json.loads(files["reviewed-tests.json"])
        received_plan = json.loads(files["playwright-impact-plan.json"])
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("PR Playwright artifact contains invalid JSON") from error
    if not isinstance(selection, dict) or not isinstance(received_plan, dict):
        raise RuntimeError("PR Playwright artifact has an invalid object shape")
    if not isinstance(selection.get("playwright"), list) or not selection["playwright"]:
        raise RuntimeError("PR Playwright run has no reviewed browser selection")
    selection_digest, selection_hash = validate_ci_selection(
        selection,
        previous=base_sha,
        sha=head_sha,
        directory=directory,
        trusted_baseline=trusted_baseline,
    )
    expected_plan, expected_receipt = expected_pr_playwright_plan(
        selection,
        head_sha=head_sha,
        base_sha=base_sha,
        directory=directory,
        trusted_baseline=trusted_baseline,
    )
    if (
        expected_plan.get("coverage_review_required")
        or not expected_plan.get("include")
        or received_plan != expected_plan
        or files["playwright-impact-receipt.md"].decode("utf-8") != expected_receipt
    ):
        raise RuntimeError(
            "PR Playwright artifact differs from the pinned selector result"
        )
    jobs = github_run_jobs(int(run_id))
    selected_jobs = validate_playwright_matrix_jobs(jobs, expected_plan["include"])
    if artifact.get("expired") is not False:
        raise RuntimeError("PR Playwright artifact is expired")
    return {
        "source": "github-actions-pull-request-playwright",
        **binding,
        "selection_change_digest": selection_digest,
        "selection_plan_sha256": selection_hash,
        "playwright_plan_sha256": hashlib.sha256(
            json.dumps(expected_plan, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "artifact_sha256": artifact_digest,
        "artifact_expires_at": artifact["expires_at"],
        "selected_entries": expected_plan["include"],
        "selected_jobs": selected_jobs,
    }


def verify_ci_acceptance(
    run_id: str,
    *,
    previous: str,
    sha: str,
    directory: Path,
    trusted_baseline: str | None = None,
    supplemental_playwright: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not CI_RUN_ID.fullmatch(run_id):
        raise RuntimeError("CI run id must be a positive decimal identifier")
    trusted_baseline = trusted_baseline or previous
    verify_ci_trust_inputs(directory, trusted_baseline=trusted_baseline, sha=sha)
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

    artifact_rows = complete_ci_artifact_listing(
        github_json(f"actions/runs/{run_id}/artifacts?per_page=100")
    )
    artifact_matches = [
        item for item in artifact_rows if item.get("name") == "test-selection-receipt"
    ]
    if len(artifact_matches) != 1:
        raise RuntimeError("CI run must contain exactly one selection receipt artifact")
    selection, artifact_digest = ci_selection_artifact(int(run_id), artifact_matches[0])
    digest, selection_hash = validate_ci_selection(
        selection,
        previous=previous,
        sha=sha,
        directory=directory,
        trusted_baseline=trusted_baseline,
    )
    if bool(selection.get("playwright")) != (supplemental_playwright is not None):
        raise RuntimeError(
            "Selected Playwright checks require exact merged-PR supplemental evidence"
        )
    jobs = github_run_jobs(int(run_id))
    required_jobs = selected_ci_jobs(
        selection,
        supplemental_playwright_verified=supplemental_playwright is not None,
    )
    selected_conclusions = require_ci_jobs(jobs, required_jobs)
    receipt = {
        "source": "github-actions-ci",
        "run_id": int(run_id),
        "run_url": run_info.get("html_url"),
        "run_attempt": run_info.get("run_attempt"),
        "workflow_path": workflow["path"],
        "commit": sha,
        "baseline": previous,
        "selection_change_digest": digest,
        "selection_plan_sha256": selection_hash,
        "artifact_sha256": artifact_digest,
        "selected_jobs": selected_conclusions,
    }
    if supplemental_playwright is not None:
        receipt["supplemental_pr_evidence"] = supplemental_playwright
    return receipt


def verify_ci_acceptance_chain(
    run_ids: list[str],
    *,
    previous: str,
    sha: str,
    directory: Path,
    pr_ci_run_ids: list[str] | None = None,
    pr_playwright_run_ids: list[str] | None = None,
) -> dict[str, Any]:
    commits = first_parent_commits(directory, previous=previous, sha=sha)
    if len(run_ids) != len(commits):
        raise RuntimeError(
            "CI acceptance requires exactly one run for each first-parent commit"
        )
    if len(set(run_ids)) != len(run_ids):
        raise RuntimeError("CI acceptance run IDs must be unique")

    pr_ci_run_ids = pr_ci_run_ids or []
    pr_playwright_run_ids = pr_playwright_run_ids or []
    if (
        len(set(pr_ci_run_ids)) != len(pr_ci_run_ids)
        or len(set(pr_playwright_run_ids)) != len(pr_playwright_run_ids)
        or len(set((*run_ids, *pr_ci_run_ids, *pr_playwright_run_ids)))
        != len(run_ids) + len(pr_ci_run_ids) + len(pr_playwright_run_ids)
    ):
        raise RuntimeError("CI acceptance run IDs must be unique")
    playwright_commits = []
    for commit in commits:
        committed = json.loads(
            run(
                "git",
                "-C",
                str(directory),
                "show",
                f"{commit}:.github/test-selection.json",
                capture=True,
            )
        )
        selected = committed.get("playwright")
        if not isinstance(selected, list):
            raise RuntimeError("Committed Playwright selection is malformed")
        if selected:
            playwright_commits.append(commit)
    if len(pr_ci_run_ids) != len(playwright_commits) or len(
        pr_playwright_run_ids
    ) != len(playwright_commits):
        raise RuntimeError(
            "Each Playwright-selected merge requires one exact PR CI and Playwright run"
        )

    receipts: list[dict[str, Any]] = []
    baseline = previous
    supplemental_index = 0
    for run_id, commit in zip(run_ids, commits, strict=True):
        supplemental: dict[str, Any] | None = None
        if commit in playwright_commits:
            parents = commit_parents(directory, commit)
            if len(parents) != 2:
                raise RuntimeError(
                    "Supplemental Playwright evidence requires a two-parent merge commit"
                )
            base_sha, head_sha = parents
            pr_ci = verify_pr_ci_supplement(
                pr_ci_run_ids[supplemental_index],
                head_sha=head_sha,
                base_sha=base_sha,
                merge_sha=commit,
                directory=directory,
                trusted_baseline=previous,
            )
            pr_playwright = verify_pr_playwright_supplement(
                pr_playwright_run_ids[supplemental_index],
                head_sha=head_sha,
                base_sha=base_sha,
                merge_sha=commit,
                directory=directory,
                trusted_baseline=previous,
            )
            for item in (pr_ci, pr_playwright):
                if any(
                    item.get(field) != expected
                    for field, expected in (
                        ("head_commit", head_sha),
                        ("base_commit", base_sha),
                        ("merge_commit", commit),
                        ("tree_sha", git_tree(directory, commit)),
                    )
                ):
                    raise RuntimeError(
                        "Supplemental PR runs do not share the exact merge tree"
                    )
            if any(
                pr_ci.get(field) != pr_playwright.get(field)
                for field in ("selection_change_digest", "selection_plan_sha256")
            ):
                raise RuntimeError(
                    "Supplemental PR runs do not share one reviewed selection"
                )
            supplemental = {"pull_request_ci": pr_ci, "playwright": pr_playwright}
            supplemental_index += 1
        receipt = verify_ci_acceptance(
            run_id,
            previous=baseline,
            sha=commit,
            directory=directory,
            trusted_baseline=previous,
            supplemental_playwright=supplemental,
        )
        if receipt.get("baseline") != baseline or receipt.get("commit") != commit:
            raise RuntimeError(
                "CI acceptance receipt does not match its chain position"
            )
        if supplemental is not None and any(
            evidence.get(field) != receipt.get(field)
            for evidence in supplemental.values()
            for field in ("selection_change_digest", "selection_plan_sha256")
        ):
            raise RuntimeError(
                "Main and supplemental CI runs do not share one selection"
            )
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
    ci: dict[str, Any],
    *,
    previous: str,
    sha: str,
    expected_commits: list[str] | None = None,
    directory: Path | None = None,
) -> None:
    accepted_run_ids: set[int] = set()

    def validate_supplemental(
        evidence: dict[str, Any], *, baseline: str, commit: str
    ) -> None:
        pr_ci = evidence.get("pull_request_ci")
        playwright = evidence.get("playwright")
        if not isinstance(pr_ci, dict) or not isinstance(playwright, dict):
            raise RuntimeError("release CI supplemental PR evidence is invalid")
        common: tuple[str, ...] | None = None
        for item, source, workflow_path in (
            (
                pr_ci,
                "github-actions-pull-request-ci",
                ".github/workflows/ci.yml",
            ),
            (
                playwright,
                "github-actions-pull-request-playwright",
                ".github/workflows/playwright-impact.yml",
            ),
        ):
            head = item.get("head_commit")
            tree = item.get("tree_sha")
            run_id = item.get("run_id")
            pull_number = item.get("pull_request")
            if (
                item.get("source") != source
                or item.get("workflow_path") != workflow_path
                or item.get("base_commit") != baseline
                or item.get("merge_commit") != commit
                or not isinstance(head, str)
                or not SHA.fullmatch(head)
                or not isinstance(tree, str)
                or not SHA.fullmatch(tree)
                or type(run_id) is not int
                or run_id < 1
                or type(item.get("run_attempt")) is not int
                or item["run_attempt"] < 1
                or item.get("run_url")
                != f"https://github.com/{GITHUB_REPOSITORY}/actions/runs/{run_id}"
                or type(pull_number) is not int
                or pull_number < 1
                or not SHA256.fullmatch(str(item.get("artifact_sha256", "")))
                or not SHA256.fullmatch(str(item.get("selection_change_digest", "")))
                or not SHA256.fullmatch(str(item.get("selection_plan_sha256", "")))
                or not isinstance(item.get("selected_jobs"), dict)
                or not item["selected_jobs"]
                or any(value != "success" for value in item["selected_jobs"].values())
            ):
                raise RuntimeError("release CI supplemental PR evidence is invalid")
            expires_at = item.get("artifact_expires_at")
            try:
                expiry = datetime.fromisoformat(
                    expires_at.replace("Z", "+00:00")
                    if isinstance(expires_at, str)
                    else ""
                )
            except ValueError as error:
                raise RuntimeError(
                    "release CI supplemental artifact expiry is invalid"
                ) from error
            if expiry.tzinfo is None:
                raise RuntimeError("release CI supplemental artifact expiry is invalid")
            identity = (head, tree, str(pull_number))
            if common is not None and identity != common:
                raise RuntimeError("release supplemental PR runs do not share identity")
            common = identity
        if (
            not SHA256.fullmatch(str(playwright.get("playwright_plan_sha256", "")))
            or not isinstance(playwright.get("selected_entries"), list)
            or not playwright["selected_entries"]
            or pr_ci["run_id"] == playwright["run_id"]
            or pr_ci["selection_change_digest"]
            != playwright["selection_change_digest"]
            or pr_ci["selection_plan_sha256"]
            != playwright["selection_plan_sha256"]
        ):
            raise RuntimeError("release CI supplemental Playwright record is invalid")
        expected_jobs = {"plan": "success", "gate": "success"}
        for entry in playwright["selected_entries"]:
            if not isinstance(entry, dict):
                raise RuntimeError(
                    "release CI supplemental Playwright record is invalid"
                )
            signature = tuple(
                entry.get(field)
                for field in ("area", "grep", "project", "runtime", "shard")
            )
            if any(not isinstance(value, str) or not value for value in signature):
                raise RuntimeError(
                    "release CI supplemental Playwright record is invalid"
                )
            label = json.dumps(signature, separators=(",", ":"))
            index = 1
            while f"playwright:{label}:{index}" in expected_jobs:
                index += 1
            expected_jobs[f"playwright:{label}:{index}"] = "success"
        if playwright.get("selected_jobs") != expected_jobs:
            raise RuntimeError(
                "release CI supplemental Playwright shard record is invalid"
            )
        if directory is not None:
            head = playwright["head_commit"]
            parents = commit_parents(directory, commit)
            if len(parents) != 2 or parents != [baseline, head]:
                raise RuntimeError("release CI supplemental merge parents are invalid")
            expected_tree = git_tree(directory, commit)
            if (
                playwright["tree_sha"] != expected_tree
                or pr_ci["tree_sha"] != expected_tree
                or git_tree(directory, head) != expected_tree
            ):
                raise RuntimeError("release CI supplemental merge tree is invalid")

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
        if run_id in accepted_run_ids:
            raise RuntimeError("release CI acceptance run IDs are duplicated")
        accepted_run_ids.add(run_id)
        supplemental = item.get("supplemental_pr_evidence")
        if supplemental is not None:
            if not isinstance(supplemental, dict):
                raise RuntimeError("release CI supplemental PR evidence is invalid")
            validate_supplemental(supplemental, baseline=baseline, commit=commit)
            for evidence in supplemental.values():
                evidence_run_id = evidence["run_id"]
                if evidence_run_id in accepted_run_ids:
                    raise RuntimeError("release CI acceptance run IDs are duplicated")
                accepted_run_ids.add(evidence_run_id)
                if any(
                    evidence.get(field) != item.get(field)
                    for field in ("selection_change_digest", "selection_plan_sha256")
                ):
                    raise RuntimeError(
                        "release supplemental and main CI selections differ"
                    )
        if directory is not None:
            selection = json.loads(
                run(
                    "git",
                    "-C",
                    str(directory),
                    "show",
                    f"{commit}:.github/test-selection.json",
                    capture=True,
                )
            )
            if bool(selection.get("playwright")) != (supplemental is not None):
                raise RuntimeError(
                    "release CI record does not cover its Playwright selection"
                )
            expected_main_jobs = {
                name: "success"
                for name in selected_ci_jobs(
                    selection,
                    supplemental_playwright_verified=supplemental is not None,
                )
            }
            if item["selected_jobs"] != expected_main_jobs:
                raise RuntimeError("release CI selected-job record is incomplete")
            verify_ci_trust_inputs(directory, trusted_baseline=previous, sha=commit)
            if supplemental is not None:
                pr_ci = supplemental["pull_request_ci"]
                playwright = supplemental["playwright"]
                head = playwright["head_commit"]
                verify_ci_trust_inputs(directory, trusted_baseline=previous, sha=head)
                expected_pr_jobs = {
                    name: "success"
                    for name in selected_ci_jobs(
                        selection, supplemental_playwright_verified=True
                    )
                }
                if pr_ci["selected_jobs"] != expected_pr_jobs:
                    raise RuntimeError("release PR CI job record is incomplete")
                expected_plan, _ = expected_pr_playwright_plan(
                    selection,
                    head_sha=head,
                    base_sha=baseline,
                    directory=directory,
                    trusted_baseline=previous,
                )
                if playwright["selected_entries"] != expected_plan.get("include"):
                    raise RuntimeError(
                        "release PR Playwright entries differ from the trusted selector"
                    )

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


def prepare(
    *,
    ci_run_ids: list[str] | None = None,
    pr_ci_run_ids: list[str] | None = None,
    pr_playwright_run_ids: list[str] | None = None,
) -> dict[str, Any]:
    if ci_run_ids is None and (
        pr_ci_run_ids is not None or pr_playwright_run_ids is not None
    ):
        raise RuntimeError(
            "Supplemental PR CI runs require the main CI acceptance chain"
        )
    git_source()
    previous = baseline()
    run("git", "-C", str(REPO), "fetch", "--no-tags", "origin", "main")
    sha = run("git", "-C", str(REPO), "rev-parse", "origin/main", capture=True)
    if sha == previous:
        if (
            ci_run_ids is not None
            or pr_ci_run_ids is not None
            or pr_playwright_run_ids is not None
        ):
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
                ci_run_ids,
                previous=previous,
                sha=sha,
                directory=candidate(sha),
                pr_ci_run_ids=pr_ci_run_ids,
                pr_playwright_run_ids=pr_playwright_run_ids,
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
            ci_run_ids,
            previous=previous,
            sha=sha,
            directory=directory,
            pr_ci_run_ids=pr_ci_run_ids,
            pr_playwright_run_ids=pr_playwright_run_ids,
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
            directory=REPO,
        )
    return release


def api() -> CoreClient:
    return CoreClient("http://127.0.0.1:8000", service_token(TOKEN_FILE))


def service(action: str) -> None:
    if action not in {"start", "stop"}:
        raise ValueError("unsupported service action")
    run("systemctl", "--user", action, SERVICE, timeout=180)


def host_connection(*, allow_stale: bool = False) -> dict[str, Any] | None:
    """Read the authenticated runtime's private, process-qualified identity."""
    directory = DATA / "runtime"
    path = directory / "connection.json"
    if directory.is_symlink():
        raise RuntimeError("Core runtime discovery directory cannot be a symlink")
    try:
        details = directory.stat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise RuntimeError("Core runtime discovery directory is not private")
    if path.is_symlink():
        raise RuntimeError("Core runtime discovery cannot be a symlink")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or details.st_size > 65_536
        ):
            raise RuntimeError("Core runtime discovery file is not private")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
            payload = json.load(stream)
    finally:
        os.close(descriptor)
    package_version = payload.get("version") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("protocol") != "nebula-desktop-runtime-v1"
        or payload.get("data_root") != str(DATA.resolve())
        or type(payload.get("pid")) is not int
        or payload["pid"] < 1
        or not isinstance(payload.get("process_birth"), str)
        or not isinstance(package_version, str)
        or not package_version
        or len(package_version) > 128
        or any(ord(character) < 33 for character in package_version)
    ):
        raise RuntimeError("Core runtime discovery identity is invalid")
    endpoint = payload.get("endpoint")
    parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.path != "/api/v1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Core runtime discovery endpoint is invalid")
    if linux_process_birth(payload["pid"]) != payload["process_birth"]:
        if allow_stale:
            return None
        raise RuntimeError("Core runtime discovery names a stale process")
    return {**payload, "package_version": package_version}


def runtime_package_version(commit: str) -> str:
    """Read the package version from the hash-bound bundle for an exact release."""
    release, _ = runtime_release(commit)
    outer_manifest = json.loads((release / "nebula-runtime.json").read_text())
    relative = "dist/nebula-runtime/nebula-runtime.json"
    files = outer_manifest.get("files")
    expected = files.get(relative) if isinstance(files, dict) else None
    bundle_manifest_path = release / relative
    if (
        not isinstance(expected, str)
        or not SHA256.fullmatch(expected)
        or sha256(bundle_manifest_path) != expected
    ):
        raise RuntimeError("Accepted release runtime manifest is not hash-bound")
    bundle_manifest = json.loads(bundle_manifest_path.read_text())
    package_version = bundle_manifest.get("version")
    if (
        not isinstance(package_version, str)
        or not package_version
        or len(package_version) > 128
        or any(ord(character) < 33 for character in package_version)
    ):
        raise RuntimeError("Accepted release package version is invalid")
    return package_version


def linux_process_birth(pid: int) -> str | None:
    if type(pid) is not int or pid < 1:
        raise ValueError("a positive Core process identity is required")
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"linux:{boot}:{fields[19]}"
    except FileNotFoundError:
        return None
    except (OSError, IndexError) as error:
        raise RuntimeError(
            "Core process birth identity could not be verified"
        ) from error


def linux_process_snapshot(pid: int) -> dict[str, Any] | None:
    """Return race-resistant identity details for one live Linux process."""
    if type(pid) is not int or pid < 1:
        raise ValueError("a positive Core process identity is required")
    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if not stat_fields or stat_fields[0] == "Z":
            return None
        parent_pid = int(stat_fields[1])
        start_time = stat_fields[19]
        status = Path(f"/proc/{pid}/status").read_text()
        uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
        effective_uid = int(uid_line.split()[2])
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        if parent_pid < 0 or not start_time.isdigit():
            raise ValueError("process parent or start time is invalid")
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return {
            "pid": pid,
            "parent_pid": parent_pid,
            "process_birth": f"linux:{boot_id}:{start_time}",
            "effective_uid": effective_uid,
            "executable": executable,
        }
    except FileNotFoundError:
        return None
    except (OSError, IndexError, StopIteration, TypeError, ValueError) as error:
        raise RuntimeError(
            "Core process ancestry identity could not be verified"
        ) from error


def require_runtime_process_in_service(
    service_identity: dict[str, Any], runtime_identity: dict[str, Any], version: str
) -> None:
    """Bind the discovered Core executable to the active unit's process tree."""
    main_pid = service_identity.get("main_pid")
    main_birth = service_identity.get("main_process_birth")
    runtime_pid = runtime_identity.get("pid")
    runtime_birth = runtime_identity.get("process_birth")
    if (
        type(main_pid) is not int
        or type(runtime_pid) is not int
        or not isinstance(main_birth, str)
        or not isinstance(runtime_birth, str)
    ):
        raise RuntimeError("Supervisor process identities are incomplete")

    release, _ = runtime_release(version)
    accepted_executable = release / "dist/nebula-core"
    receipt = json.loads((release / "simulation-acceptance.json").read_text())
    expected_digest = receipt.get("core_sha256")
    if (
        accepted_executable.is_symlink()
        or not isinstance(expected_digest, str)
        or not SHA256.fullmatch(expected_digest)
        or sha256(accepted_executable) != expected_digest
    ):
        raise RuntimeError("Accepted supervisor executable is not hash-bound")
    accepted_executable = accepted_executable.resolve(strict=True)

    chain: dict[int, dict[str, Any]] = {}
    current_pid = runtime_pid
    for _ in range(64):
        if current_pid in chain:
            raise RuntimeError(
                "Discovered Core process is not a unit MainPID descendant"
            )
        process = linux_process_snapshot(current_pid)
        if process is None:
            raise RuntimeError("Supervisor process ancestry contains a stale PID")
        if process["effective_uid"] != os.getuid():
            raise RuntimeError("Supervisor process ancestry crosses a user boundary")
        chain[current_pid] = process
        if current_pid == runtime_pid and process["process_birth"] != runtime_birth:
            raise RuntimeError("Discovered Core process birth identity changed")
        if current_pid == main_pid:
            if process["process_birth"] != main_birth:
                raise RuntimeError("Supervisor MainPID birth identity changed")
            break
        if process["parent_pid"] == 0:
            raise RuntimeError(
                "Discovered Core process is not a unit MainPID descendant"
            )
        current_pid = process["parent_pid"]
    else:
        raise RuntimeError("Discovered Core process is not a unit MainPID descendant")

    for pid in {runtime_pid, main_pid}:
        executable = chain[pid]["executable"]
        if executable != accepted_executable or sha256(executable) != expected_digest:
            raise RuntimeError(
                "Supervisor process does not match the accepted onefile executable"
            )

    # Re-read every link in the chain to reject PID reuse or reparenting during
    # validation, and re-read systemd's identity to bind the ancestry to the
    # still-active unit.
    for pid, previous in chain.items():
        current = linux_process_snapshot(pid)
        if current is None or any(
            current[key] != previous[key]
            for key in ("process_birth", "parent_pid", "effective_uid", "executable")
        ):
            raise RuntimeError("Supervisor process ancestry changed during validation")
    latest_service = supervisor_service_identity()
    if (
        latest_service.get("main_pid") != main_pid
        or latest_service.get("main_process_birth") != main_birth
        or latest_service.get("exec_start") != service_identity.get("exec_start")
    ):
        raise RuntimeError("Supervisor unit identity changed during validation")


def supervisor_service_configuration() -> dict[str, Any]:
    if unit_state(EDGE) != "active":
        raise RuntimeError("The HTTPS edge must be active before host activation")
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
        raise RuntimeError(
            "The supervisor service does not execute the guarded current release"
        )
    kill_mode = run(
        "systemctl",
        "--user",
        "show",
        SERVICE,
        "-p",
        "KillMode",
        "--value",
        capture=True,
    )
    exit_type = run(
        "systemctl",
        "--user",
        "show",
        SERVICE,
        "-p",
        "ExitType",
        "--value",
        capture=True,
    )
    if kill_mode != "process" or exit_type != "main":
        raise RuntimeError(
            "Host activation requires KillMode=process and ExitType=main to preserve workers"
        )
    return {"exec_start": command, "kill_mode": kill_mode, "exit_type": exit_type}


def supervisor_service_identity() -> dict[str, Any]:
    if unit_state(SERVICE) != "active":
        raise RuntimeError("The supervisor Core service must be active")
    configuration = supervisor_service_configuration()
    main_pid = run(
        "systemctl",
        "--user",
        "show",
        SERVICE,
        "-p",
        "MainPID",
        "--value",
        capture=True,
    )
    try:
        main_pid_value = int(main_pid)
    except (TypeError, ValueError) as error:
        raise RuntimeError("Supervisor service MainPID is invalid") from error
    if main_pid_value < 1:
        raise RuntimeError("Supervisor service has no live MainPID")
    main_birth = linux_process_birth(main_pid_value)
    if main_birth is None:
        raise RuntimeError("Supervisor service MainPID is not a live process")
    return {
        **configuration,
        "main_pid": main_pid_value,
        "main_process_birth": main_birth,
    }


def host_status_snapshot(
    status: dict[str, Any], *, allow_degraded: bool = False
) -> dict[str, Any]:
    deployment_health = status.get("health") if isinstance(status, dict) else None
    if (
        not isinstance(status, dict)
        or status.get("protocol") != "nebula.runtime/v1"
        or not (
            deployment_health == "ready"
            or allow_degraded and deployment_health == "degraded"
        )
        or not isinstance(status.get("api_version"), str)
        or not SHA.fullmatch(status["api_version"])
    ):
        raise RuntimeError(
            "A ready supervisor API status is required unless the existing degraded baseline was explicitly accepted"
        )
    residents = status.get("resident_versions")
    projects = status.get("projects")
    if (
        not isinstance(residents, list)
        or any(
            not isinstance(version, str) or not SHA.fullmatch(version)
            for version in residents
        )
        or len(residents) > 16
        or not isinstance(projects, list)
        or len(projects) > 10_000
    ):
        raise RuntimeError("Supervisor worker inventory is invalid")
    owners: dict[str, dict[str, Any]] = {}
    for project in projects:
        if not isinstance(project, dict):
            raise RuntimeError("Supervisor project inventory is invalid")
        project_id = project.get("project_id")
        runtime_id = project.get("runtime_id")
        runtime_version = project.get("runtime_version")
        fencing_token = project.get("fencing_token")
        if (
            not isinstance(project_id, str)
            or not project_id
            or project_id in owners
            or (runtime_id is not None and (not isinstance(runtime_id, str) or not runtime_id))
            or (runtime_version is not None and not isinstance(runtime_version, str))
            or type(fencing_token) is not int
            or fencing_token < 0
        ):
            raise RuntimeError("Supervisor project owner identity is invalid")
        owners[project_id] = {
            "runtime_id": runtime_id,
            "runtime_version": runtime_version,
            "fencing_token": fencing_token,
        }
    return {
        "health": deployment_health,
        "api_version": status["api_version"],
        "resident_versions": sorted(set(residents)),
        "projects": owners,
    }


def require_preserved_workers(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    admitted_version: str | None = None,
) -> dict[str, Any]:
    previous_projects = before["projects"]
    current_projects = after["projects"]
    missing = sorted(set(previous_projects) - set(current_projects))
    if missing:
        raise RuntimeError("Host restart removed existing project records")
    retained = 0
    for project_id, previous in previous_projects.items():
        current = current_projects[project_id]
        if previous["runtime_id"] is None:
            if current["runtime_id"] is None:
                if current["fencing_token"] != previous["fencing_token"]:
                    raise RuntimeError(
                        f"Host restart changed the fencing token for unowned project {project_id}"
                    )
                continue
            if (
                admitted_version is None
                or current["runtime_version"] != admitted_version
                or current["fencing_token"] <= previous["fencing_token"]
            ):
                raise RuntimeError(
                    f"Host restart assigned an unowned project outside the exact admitted release: {project_id}"
                )
            continue
        if (
            current["runtime_id"] != previous["runtime_id"]
            or current["runtime_version"] != previous["runtime_version"]
            or current["fencing_token"] != previous["fencing_token"]
        ):
            raise RuntimeError(
                f"Host restart changed the existing execution owner for project {project_id}"
            )
        if previous["runtime_id"] is not None:
            retained += 1
    previous_residents = set(before["resident_versions"])
    current_residents = set(after["resident_versions"])
    if (
        not previous_residents <= current_residents
        or current_residents - previous_residents
        - ({admitted_version} if admitted_version is not None else set())
    ):
        raise RuntimeError("Host restart changed resident execution versions unexpectedly")
    return {"retained": retained}


def require_stage_migration_receipts(
    record: dict[str, Any], *, version: str, manifest_sha256: str
) -> dict[str, Any]:
    journal = record.get("runtime_deployment")
    steps = journal.get("steps") if isinstance(journal, dict) else None
    if (
        not isinstance(journal, dict)
        or journal.get("action") not in {"promote", "rollback"}
        or journal.get("version") != version
        or not isinstance(steps, dict)
    ):
        raise RuntimeError(
            "Host activation requires the exact durable deployment journal"
        )
    stage, migrate = steps.get("stage"), steps.get("migrate")
    if (
        not isinstance(stage, dict)
        or stage.get("action") != "stage"
        or not isinstance(stage.get("parameters"), dict)
        or stage["parameters"].get("manifest_sha256") != manifest_sha256
        or not isinstance(stage.get("receipt"), dict)
        or stage["receipt"].get("state") != "succeeded"
        or (
            journal.get("action") == "promote"
            and (
                not isinstance(migrate, dict)
                or migrate.get("action") != "migrate"
                or not isinstance(migrate.get("parameters"), dict)
                or migrate["parameters"].get("version") != version
                or not isinstance(migrate.get("receipt"), dict)
                or migrate["receipt"].get("state") != "succeeded"
            )
        )
    ):
        raise RuntimeError(
            "Host activation requires verified stage and additive-migration receipts"
        )
    return {
        "deployment_id": journal.get("id"),
        "stage_operation_id": stage.get("operation_id"),
        "migration_operation_id": migrate.get("operation_id"),
    }


def require_host_schema_compatibility(
    manifest: dict[str, Any], health: dict[str, Any]
) -> None:
    schema = health.get("schema_version")
    schema_family = manifest.get("schema_family")
    minimum = manifest.get("schema_min")
    maximum = manifest.get("schema_max")
    if (
        type(schema) is not int
        or type(schema_family) is not int
        or type(minimum) is not int
        or type(maximum) is not int
        or schema_family != schema
        or not minimum <= schema <= maximum
    ):
        raise RuntimeError(
            "Host release compatibility does not cover the live database schema"
        )


def read_supervisor_health() -> tuple[dict[str, Any], dict[str, Any]]:
    client = api()
    health = client.get("health")
    status = client.get("deployment/status")
    if not isinstance(health, dict) or not isinstance(status, dict):
        raise RuntimeError("Supervisor health readback is malformed")
    return health, status


def wait_supervisor_host_ready(
    version: str,
    *,
    expected_api_version: str,
    prior_status: dict[str, Any] | None,
    allow_degraded_workers: bool = False,
    degraded_baseline: bool = False,
    timeout: int = 480,
) -> dict[str, Any]:
    expected_package_version = runtime_package_version(version)
    deadline = time.monotonic() + timeout
    last_problem = "supervisor did not publish its accepted host identity"
    while time.monotonic() < deadline:
        try:
            if unit_state(SERVICE) != "active":
                last_problem = "supervisor service is not active"
                time.sleep(2)
                continue
            identity = host_connection(allow_stale=True)
            if (
                identity is None
                or identity.get("package_version") != expected_package_version
            ):
                last_problem = (
                    "runtime discovery does not name the requested host release"
                )
                time.sleep(2)
                continue
            service_identity = supervisor_service_identity()
            require_runtime_process_in_service(service_identity, identity, version)
            health, status = read_supervisor_health()
            if health.get("status") != "ok" or health.get("database") != "ok":
                last_problem = "host health is not ready"
                time.sleep(2)
                continue
            if (
                health.get("commit") != version
                or health.get("recovery_version") != expected_package_version
            ):
                raise RuntimeError(
                    "Host health does not report the accepted Core and recovery identities"
                )
            snapshot = host_status_snapshot(
                status,
                allow_degraded=(allow_degraded_workers and degraded_baseline),
            )
            if snapshot["api_version"] != expected_api_version:
                raise RuntimeError(
                    "Host activation unexpectedly changed the API release"
                )
            continuity = (
                require_preserved_workers(
                    prior_status, snapshot, admitted_version=version
                )
                if prior_status is not None
                else {"retained": 0}
            )
            return {
                "version": version,
                "health_commit": health["commit"],
                "recovery_version": health["recovery_version"],
                "api_version": snapshot["api_version"],
                "deployment_health": snapshot["health"],
                "degraded_baseline_accepted": bool(
                    allow_degraded_workers and degraded_baseline
                ),
                "resident_versions": snapshot["resident_versions"],
                "retained_project_owners": continuity["retained"],
                "runtime_pid": identity["pid"],
                "runtime_process_birth": identity["process_birth"],
            }
        except (OSError, ValueError, HTTPError) as error:
            last_problem = str(error)[:300]
            time.sleep(2)
    raise RuntimeError(
        f"Supervisor host {version} did not become ready within {timeout}s: {last_problem}"
    )


def current_release_version() -> str:
    if not CURRENT.is_symlink():
        raise RuntimeError("Current supervisor release is not an immutable symlink")
    release = CURRENT.resolve(strict=True)
    if release.parent != RELEASES or not SHA.fullmatch(release.name):
        raise RuntimeError(
            "Current supervisor release points outside immutable releases"
        )
    return release.name


def _save_host_activation(record: dict[str, Any], journal: dict[str, Any]) -> None:
    record["host_activation"] = journal
    atomic_json(STATE, record)


def _switch_supervisor_host(
    version: str,
    *,
    release: Path,
    expected_api_version: str,
    prior_status: dict[str, Any],
    journal: dict[str, Any],
    allow_degraded_workers: bool = False,
    journal_container: dict[str, Any] | None = None,
    record: dict[str, Any],
) -> dict[str, Any]:
    def persist() -> None:
        if journal_container is None:
            _save_host_activation(record, journal)
        else:
            journal_container["rollback"] = journal
            _save_host_activation(record, journal_container)

    if release.parent != RELEASES or release.name != version or release.is_symlink():
        raise RuntimeError("Host activation target is not the exact immutable release")
    if unit_state(SERVICE) != "inactive":
        service("stop")
    require_stopped()
    if current_release_version() != version:
        switch_to(release)
    journal["state"] = "service_stopped_release_selected"
    journal["current_release"] = str(release)
    persist()
    service("start")
    journal["state"] = "service_started"
    persist()
    readback = wait_supervisor_host_ready(
        version,
        expected_api_version=expected_api_version,
        prior_status=prior_status,
        allow_degraded_workers=allow_degraded_workers,
        degraded_baseline=prior_status.get("health") == "degraded",
    )
    journal["state"] = "complete"
    journal["readback"] = readback
    record["host_active"] = version
    persist()
    return readback


def reconcile_attention_activation(
    record: dict[str, Any],
    journal: dict[str, Any],
    *,
    version: str,
    allow_degraded_workers: bool,
) -> None:
    """Resume only when a needs-attention activation is back on its verified old host."""
    prior = journal.get("prior_status")
    previous = journal.get("previous_version")
    if (
        not isinstance(prior, dict)
        or not isinstance(previous, str)
        or not isinstance(prior.get("api_version"), str)
        or current_release_version() != previous
        or unit_state(SERVICE) != "active"
    ):
        raise RuntimeError(
            "Needs-attention activation is not on its recorded active previous host"
        )
    identity = host_connection(allow_stale=True)
    previous_package_version = runtime_package_version(previous)
    if identity is None or identity.get("package_version") != previous_package_version:
        raise RuntimeError(
            "Needs-attention activation has no exact previous Core process identity"
        )
    service_identity = supervisor_service_identity()
    require_runtime_process_in_service(service_identity, identity, previous)
    health, status = read_supervisor_health()
    if (
        health.get("status") != "ok"
        or health.get("database") != "ok"
        or health.get("commit") != previous
        or health.get("recovery_version") != previous_package_version
    ):
        raise RuntimeError(
            "Needs-attention activation previous Core health is not exact"
        )
    snapshot = host_status_snapshot(
        status,
        allow_degraded=(
            allow_degraded_workers and prior.get("health") == "degraded"
        ),
    )
    if snapshot["api_version"] != prior["api_version"]:
        raise RuntimeError(
            "Needs-attention activation previous API release is not exact"
        )
    continuity = require_preserved_workers(prior, snapshot, admitted_version=version)
    journal["state"] = "prepared"
    journal["needs_attention_reconciled"] = {
        "previous_version": previous,
        "recovery_version": previous_package_version,
        "retained_project_owners": continuity["retained"],
    }
    _save_host_activation(record, journal)


def activate_supervisor_host(
    record: dict[str, Any],
    version: str,
    *,
    allow_degraded_workers: bool = False,
) -> dict[str, Any]:
    """Restart only the foreground supervisor from an accepted staged release."""
    if record.get("deployment_mode") != "supervisor":
        raise RuntimeError("Host activation requires supervisor deployment mode")
    deployment = record.get("runtime_deployment")
    action = deployment.get("action") if isinstance(deployment, dict) else None
    valid_pending = action == "promote" and record.get("pending") == version
    valid_rollback = action == "rollback" and record.get("previous") == version
    if not (valid_pending or valid_rollback) or not SHA.fullmatch(version):
        raise RuntimeError(
            "Host activation must target the exact pending candidate or recorded code rollback"
        )
    release, manifest_sha256 = runtime_release(version)
    manifest = json.loads((release / "nebula-runtime.json").read_text())
    if manifest.get("version") != version:
        raise RuntimeError("Accepted host runtime manifest has a different version")
    expected_package_version = runtime_package_version(version)
    operations = require_stage_migration_receipts(
        record, version=version, manifest_sha256=manifest_sha256
    )
    deployment_activation = deployment.get("host_activation")
    requested_operation_id = (
        deployment_activation.get("operation_id")
        if isinstance(deployment_activation, dict)
        and deployment_activation.get("version") == version
        and deployment_activation.get("state") in {"requested", "complete"}
        else None
    )
    existing = record.get("host_activation")
    if isinstance(existing, dict) and existing.get("version") == version:
        recorded_allow_degraded = existing.get("allow_degraded_workers") is True
        if allow_degraded_workers and not recorded_allow_degraded:
            raise RuntimeError(
                "A retry cannot add degraded-worker acceptance to an existing activation journal"
            )
        allow_degraded_workers = recorded_allow_degraded
        if (
            requested_operation_id is not None
            and existing.get("operation_id") != requested_operation_id
        ):
            raise RuntimeError(
                "Staged deployment and durable host activation operation IDs differ"
            )
        if existing.get("state") == "needs_attention":
            reconcile_attention_activation(
                record,
                existing,
                version=version,
                allow_degraded_workers=allow_degraded_workers,
            )
        if existing.get("state") == "complete":
            connection = host_connection(allow_stale=True)
            if (
                current_release_version() != version
                or connection is None
                or connection.get("package_version") != expected_package_version
            ):
                raise RuntimeError(
                    "Completed host activation no longer matches current process identity"
                )
            prior = existing.get("prior_status")
            if not isinstance(prior, dict):
                raise RuntimeError("Host activation lost its pre-restart readback")
            wait_supervisor_host_ready(
                version,
                expected_api_version=prior["api_version"],
                prior_status=None,
                allow_degraded_workers=allow_degraded_workers,
                degraded_baseline=isinstance(prior, dict)
                and prior.get("health") == "degraded",
            )
            readback = existing.get("readback")
            if (
                not isinstance(readback, dict)
                or readback.get("health_commit") != version
            ):
                raise RuntimeError(
                    "Completed host activation lost its verified readback"
                )
            return {
                "operation_id": existing["operation_id"],
                "status": "host_activated",
                "version": version,
                "previous_version": existing["previous_version"],
                **readback,
            }
        if existing.get("state") in {
            "prepared",
            "service_stopped_release_selected",
            "service_started",
        }:
            prior = existing.get("prior_status")
            previous = existing.get("previous_version")
            operation_id = existing.get("operation_id")
            if (
                not isinstance(prior, dict)
                or not isinstance(prior.get("api_version"), str)
                or not isinstance(previous, str)
                or not isinstance(operation_id, str)
            ):
                raise RuntimeError("Interrupted host activation journal is incomplete")
            selected = current_release_version()
            identity = host_connection(allow_stale=True)
            previous_package_version = runtime_package_version(previous)
            if selected == version:
                if (
                    unit_state(SERVICE) == "active"
                    and identity is not None
                    and identity.get("package_version") == expected_package_version
                ):
                    try:
                        readback = wait_supervisor_host_ready(
                            version,
                            expected_api_version=prior["api_version"],
                            prior_status=prior,
                            allow_degraded_workers=allow_degraded_workers,
                            degraded_baseline=prior.get("health") == "degraded",
                        )
                    except Exception as error:
                        existing["readback_error"] = str(error)[:1000]
                        _save_host_activation(record, existing)
                    else:
                        existing["state"] = "complete"
                        existing["readback"] = readback
                        record["host_active"] = version
                        _save_host_activation(record, existing)
                        return {
                            "operation_id": operation_id,
                            "status": "host_activated",
                            "version": version,
                            "previous_version": previous,
                            **readback,
                        }
                supervisor_service_configuration()
                try:
                    readback = _switch_supervisor_host(
                        version,
                        release=release,
                        expected_api_version=prior["api_version"],
                        prior_status=prior,
                        journal=existing,
                        allow_degraded_workers=allow_degraded_workers,
                        record=record,
                    )
                except Exception as error:
                    existing["state"] = "rolling_back"
                    existing["activation_error"] = str(error)[:1000]
                    _save_host_activation(record, existing)
                    try:
                        old_release, _ = runtime_release(previous)
                        rollback_operation = {
                            "operation_id": operation_id + ":rollback",
                            "state": "rolling_back",
                            "version": previous,
                            "previous_version": version,
                            "expected_api_version": prior["api_version"],
                            "prior_status": prior,
                            "code_only": True,
                        }
                        rollback_readback = _switch_supervisor_host(
                            previous,
                            release=old_release,
                            expected_api_version=prior["api_version"],
                            prior_status=prior,
                            journal=rollback_operation,
                            allow_degraded_workers=allow_degraded_workers,
                            journal_container=existing,
                            record=record,
                        )
                    except Exception as rollback_error:
                        existing["state"] = "needs_attention"
                        existing["rollback_error"] = str(rollback_error)[:1000]
                        _save_host_activation(record, existing)
                        raise RuntimeError(
                            "Interrupted host activation failed and code-only rollback could not be verified"
                        ) from rollback_error
                    existing["state"] = "rolled_back"
                    existing["rollback_readback"] = rollback_readback
                    record["host_active"] = previous
                    _save_host_activation(record, existing)
                    raise RuntimeError(
                        f"Interrupted host activation failed; restored {previous} without restoring database or files: {error}"
                    ) from error
                return {
                    "operation_id": operation_id,
                    "status": "host_activated",
                    "version": version,
                    "previous_version": previous,
                    **readback,
                }
            if selected != previous or (
                identity is not None
                and identity.get("package_version") != previous_package_version
            ):
                raise RuntimeError(
                    "Interrupted host activation has an unexpected current release or process"
                )
            if unit_state(SERVICE) == "active":
                health, status = read_supervisor_health()
                if (
                    health.get("status") != "ok"
                    or health.get("database") != "ok"
                    or health.get("commit") != previous
                    or health.get("recovery_version") != previous_package_version
                    or host_status_snapshot(
                        status, allow_degraded=allow_degraded_workers
                    )["api_version"]
                    != prior["api_version"]
                ):
                    raise RuntimeError(
                        "Previous supervisor does not match the interrupted activation journal"
                    )
            readback = _switch_supervisor_host(
                version,
                release=release,
                expected_api_version=prior["api_version"],
                prior_status=prior,
                journal=existing,
                allow_degraded_workers=allow_degraded_workers,
                record=record,
            )
            return {
                "operation_id": operation_id,
                "status": "host_activated",
                "version": version,
                "previous_version": previous,
                **readback,
            }
    service_identity = supervisor_service_identity()
    prior_host = host_connection()
    if prior_host is None:
        raise RuntimeError("Current supervisor process identity is unavailable")
    previous_release = current_release_version()
    previous_version = previous_release
    require_runtime_process_in_service(service_identity, prior_host, previous_version)
    if prior_host["package_version"] != runtime_package_version(previous_version):
        raise RuntimeError(
            "Running supervisor package identity differs from the current release manifest"
        )
    if previous_version == version:
        raise RuntimeError("Pending host release is already the active supervisor")
    health, status = read_supervisor_health()
    if (
        health.get("status") != "ok"
        or health.get("database") != "ok"
        or health.get("commit") != previous_version
        or health.get("recovery_version")
        != runtime_package_version(previous_version)
    ):
        raise RuntimeError(
            "Current supervisor API/database health does not match its process identity"
        )
    require_host_schema_compatibility(manifest, health)
    prior_status = host_status_snapshot(status, allow_degraded=allow_degraded_workers)
    if allow_degraded_workers and prior_status["health"] != "degraded":
        raise RuntimeError(
            "--allow-degraded-workers requires a degraded deployment baseline"
        )
    if prior_status["api_version"] != record.get("active"):
        raise RuntimeError(
            "Deployer state differs from the current supervisor API version"
        )
    if version not in status.get("staged_versions", []):
        raise RuntimeError("Pending host release has not been staged by the supervisor")
    if prior_status["api_version"] == version:
        raise RuntimeError("Host activation must precede API promotion")
    if current_release_version() != previous_version:
        raise RuntimeError("Current release changed during host activation preflight")
    journal = {
        "operation_id": requested_operation_id or uuid4().hex,
        "state": "prepared",
        "version": version,
        "previous_version": previous_version,
        "previous_release": str(previous_release),
        "manifest_sha256": manifest_sha256,
        "expected_api_version": prior_status["api_version"],
        "prior_status": prior_status,
        "prior_process": {
            "pid": prior_host["pid"],
            "process_birth": prior_host["process_birth"],
        },
        "prior_service": service_identity,
        "operations": operations,
        "allow_degraded_workers": allow_degraded_workers,
        "degraded_baseline_reason": (
            "Accepted aggregate degraded status attributed by read-only diagnosis to stale old execution-worker leases; individual failed-worker equality is not asserted."
            if allow_degraded_workers
            else None
        ),
    }
    _save_host_activation(record, journal)
    try:
        readback = _switch_supervisor_host(
            version,
            release=release,
            expected_api_version=prior_status["api_version"],
            prior_status=prior_status,
            journal=journal,
            allow_degraded_workers=allow_degraded_workers,
            record=record,
        )
    except Exception as error:
        journal["state"] = "rolling_back"
        journal["activation_error"] = str(error)[:1000]
        _save_host_activation(record, journal)
        try:
            old_release, _ = runtime_release(previous_version)
            current_identity = host_connection()
            if (
                current_release_version() == previous_version
                and unit_state(SERVICE) == "active"
                and current_identity is not None
                and current_identity.get("package_version")
                == runtime_package_version(previous_version)
            ):
                rollback_readback = wait_supervisor_host_ready(
                    previous_version,
                    expected_api_version=prior_status["api_version"],
                    prior_status=prior_status,
                    allow_degraded_workers=allow_degraded_workers,
                    degraded_baseline=prior_status["health"] == "degraded",
                )
            else:
                rollback_operation = {
                    "operation_id": journal["operation_id"] + ":rollback",
                    "state": "rolling_back",
                    "version": previous_version,
                    "previous_version": version,
                    "expected_api_version": prior_status["api_version"],
                    "prior_status": prior_status,
                    "code_only": True,
                }
                rollback_readback = _switch_supervisor_host(
                    previous_version,
                    release=old_release,
                    expected_api_version=prior_status["api_version"],
                    prior_status=prior_status,
                    journal=rollback_operation,
                    allow_degraded_workers=allow_degraded_workers,
                    journal_container=journal,
                    record=record,
                )
        except Exception as rollback_error:
            journal["state"] = "needs_attention"
            journal["rollback_error"] = str(rollback_error)[:1000]
            _save_host_activation(record, journal)
            raise RuntimeError(
                "Host activation failed and code-only rollback could not be verified; preserve current database and worker state"
            ) from rollback_error
        journal["state"] = "rolled_back"
        journal["rollback_readback"] = rollback_readback
        record["host_active"] = previous_version
        _save_host_activation(record, journal)
        raise RuntimeError(
            f"Host activation failed; restored supervisor code to {previous_version} without restoring database or files: {error}"
        ) from error
    return {
        "operation_id": journal["operation_id"],
        "status": "host_activated",
        "version": version,
        "previous_version": previous_version,
        **readback,
    }


def activate_host(*, allow_degraded_workers: bool = False) -> dict[str, Any]:
    record = state()
    pending = record.get("pending")
    if not isinstance(pending, str):
        raise RuntimeError(
            "No accepted pending candidate is available for host activation"
        )
    return activate_supervisor_host(
        record, pending, allow_degraded_workers=allow_degraded_workers
    )


def rollback_host() -> dict[str, Any]:
    record = state()
    journal = record.get("host_activation")
    if (
        record.get("deployment_mode") != "supervisor"
        or not isinstance(journal, dict)
        or not isinstance(journal.get("previous_version"), str)
    ):
        raise RuntimeError("No verified supervisor host activation can be rolled back")
    prior_rollback = journal.get("rollback")
    restore_operation = None
    restore_parent = None
    if isinstance(prior_rollback, dict):
        nested_restore = prior_rollback.get("restore_operation")
        if prior_rollback.get("state") in {
            "restoring_current_host",
            "needs_attention",
        } and isinstance(nested_restore, dict):
            restore_operation = nested_restore
            restore_parent = prior_rollback
        elif (
            isinstance(prior_rollback.get("restore_of"), str)
            and prior_rollback.get("operation_id")
            == prior_rollback.get("restore_of") + ":restore-current"
        ):
            restore_operation = prior_rollback
    if (
        isinstance(restore_operation, dict)
        and restore_operation.get("state")
        in {"rolling_back", "service_stopped_release_selected", "service_started"}
        and restore_operation.get("code_only") is True
        and restore_operation.get("version") == journal.get("version")
        and isinstance(restore_operation.get("operation_id"), str)
        and isinstance(restore_operation.get("restore_of"), str)
    ):
        restored_version = restore_operation["version"]
        expected_api_version = restore_operation.get("expected_api_version")
        prior_status = restore_operation.get("prior_status")
        selected = current_release_version()
        if (
            not isinstance(expected_api_version, str)
            or not isinstance(prior_status, dict)
            or prior_status.get("api_version") != expected_api_version
            or selected not in {restored_version, journal.get("previous_version")}
        ):
            raise RuntimeError("Interrupted rollback restoration journal is invalid")
        release, _ = runtime_release(restored_version)
        readback = _switch_supervisor_host(
            restored_version,
            release=release,
            expected_api_version=expected_api_version,
            prior_status=prior_status,
            journal=restore_operation,
            journal_container=journal,
            record=record,
        )
        restore_operation["state"] = "complete"
        restore_operation["readback"] = readback
        if restore_parent is not None:
            restore_parent["state"] = "failed_current_restored"
            restore_parent["restored_readback"] = readback
            restore_parent["restore_operation"] = restore_operation
            journal["rollback"] = restore_parent
        else:
            journal["rollback"] = restore_operation
        journal["state"] = "complete"
        record["host_active"] = restored_version
        _save_host_activation(record, journal)
        return {
            "status": "host_current_restored_after_failed_rollback",
            "operation_id": restore_operation["operation_id"],
            "version": restored_version,
            "previous_version": journal["previous_version"],
            "code_only": True,
            **readback,
        }
    if (
        isinstance(prior_rollback, dict)
        and prior_rollback.get("state")
        in {"rolling_back", "service_stopped_release_selected", "service_started"}
        and prior_rollback.get("code_only") is True
        and prior_rollback.get("version") == journal.get("previous_version")
        and isinstance(prior_rollback.get("operation_id"), str)
    ):
        rolled_back_version = prior_rollback["version"]
        original_version = journal.get("version")
        expected_api_version = prior_rollback.get("expected_api_version")
        prior_status = prior_rollback.get("prior_status")
        selected = current_release_version()
        if (
            not isinstance(original_version, str)
            or not isinstance(expected_api_version, str)
            or not isinstance(prior_status, dict)
            or prior_status.get("api_version") != expected_api_version
            or selected not in {rolled_back_version, original_version}
        ):
            raise RuntimeError(
                "Interrupted host rollback journal or selected release is invalid"
            )
        release, _ = runtime_release(rolled_back_version)
        readback = _switch_supervisor_host(
            rolled_back_version,
            release=release,
            expected_api_version=expected_api_version,
            prior_status=prior_status,
            journal=prior_rollback,
            journal_container=journal,
            record=record,
        )
        prior_rollback["state"] = "complete"
        prior_rollback["readback"] = readback
        journal["rollback"] = prior_rollback
        journal["state"] = "rolled_back"
        record["host_active"] = rolled_back_version
        _save_host_activation(record, journal)
        return {
            "status": "host_rolled_back",
            "operation_id": prior_rollback["operation_id"],
            "version": rolled_back_version,
            "previous_version": original_version,
            "code_only": True,
            **readback,
        }
    if (
        isinstance(prior_rollback, dict)
        and prior_rollback.get("state") == "complete"
        and journal.get("state") in {"complete", "rolled_back"}
        and record.get("host_active") == prior_rollback.get("version")
    ):
        rolled_back_version = prior_rollback["version"]
        identity = host_connection(allow_stale=True)
        service_identity = supervisor_service_identity()
        if (
            current_release_version() != rolled_back_version
            or identity is None
            or identity.get("package_version")
            != runtime_package_version(rolled_back_version)
        ):
            raise RuntimeError(
                "Completed host rollback no longer matches current process identity"
            )
        require_runtime_process_in_service(
            service_identity, identity, rolled_back_version
        )
        health, status = read_supervisor_health()
        snapshot = host_status_snapshot(status)
        if (
            health.get("status") != "ok"
            or health.get("database") != "ok"
            or health.get("commit") != rolled_back_version
            or health.get("recovery_version")
            != runtime_package_version(rolled_back_version)
            or snapshot["api_version"] != prior_rollback.get("expected_api_version")
        ):
            raise RuntimeError("Completed host rollback readback is no longer current")
        if journal.get("state") != "rolled_back":
            journal["state"] = "rolled_back"
            _save_host_activation(record, journal)
        saved_readback = prior_rollback.get("readback")
        if not isinstance(saved_readback, dict):
            raise RuntimeError("Completed host rollback lost its readback receipt")
        return {
            "status": "host_rolled_back",
            "operation_id": prior_rollback["operation_id"],
            "version": rolled_back_version,
            "previous_version": prior_rollback["previous_version"],
            "code_only": True,
            **saved_readback,
        }
    if journal.get("state") != "complete" or journal.get("version") != record.get(
        "host_active"
    ):
        raise RuntimeError("No verified supervisor host activation can be rolled back")
    current_version = journal["version"]
    previous_version = journal["previous_version"]
    release, _ = runtime_release(previous_version)
    manifest = json.loads((release / "nebula-runtime.json").read_text())
    service_identity = supervisor_service_identity()
    current_identity = host_connection()
    if (
        current_identity is None
        or current_identity.get("package_version")
        != runtime_package_version(current_version)
        or current_release_version() != current_version
    ):
        raise RuntimeError("Current supervisor does not match its recorded activation")
    require_runtime_process_in_service(
        service_identity, current_identity, current_version
    )
    health, status = read_supervisor_health()
    if (
        health.get("status") != "ok"
        or health.get("database") != "ok"
        or health.get("commit") != current_version
        or health.get("recovery_version")
        != runtime_package_version(current_version)
    ):
        raise RuntimeError(
            "Current supervisor health is not verified for code rollback"
        )
    require_host_schema_compatibility(manifest, health)
    before = host_status_snapshot(status)
    operation_id = uuid4().hex
    operation = {
        "operation_id": operation_id,
        "state": "rolling_back",
        "version": previous_version,
        "previous_version": current_version,
        "previous_release": str(CURRENT.resolve(strict=True)),
        "expected_api_version": before["api_version"],
        "prior_status": before,
        "prior_process": {
            "pid": current_identity["pid"],
            "process_birth": current_identity["process_birth"],
        },
        "prior_service": service_identity,
        "code_only": True,
    }
    journal["rollback"] = operation
    _save_host_activation(record, journal)
    try:
        readback = _switch_supervisor_host(
            previous_version,
            release=release,
            expected_api_version=before["api_version"],
            prior_status=before,
            journal=operation,
            journal_container=journal,
            record=record,
        )
    except Exception as error:
        operation["state"] = "restoring_current_host"
        operation["rollback_error"] = str(error)[:1000]
        current_release, _ = runtime_release(current_version)
        restore = {
            "operation_id": operation["operation_id"] + ":restore-current",
            "restore_of": operation["operation_id"],
            "state": "rolling_back",
            "version": current_version,
            "previous_version": previous_version,
            "expected_api_version": before["api_version"],
            "prior_status": before,
            "code_only": True,
            "rollback_error": operation["rollback_error"],
        }
        operation["restore_operation"] = restore
        journal["rollback"] = operation
        _save_host_activation(record, journal)
        try:
            restored = _switch_supervisor_host(
                current_version,
                release=current_release,
                expected_api_version=before["api_version"],
                prior_status=before,
                journal=restore,
                journal_container=journal,
                record=record,
            )
        except Exception as restore_error:
            operation["state"] = "needs_attention"
            operation["restore_error"] = str(restore_error)[:1000]
            journal["rollback"] = operation
            _save_host_activation(record, journal)
            raise RuntimeError(
                "Code-only host rollback failed and restoring current code could not be verified; database and worker state were preserved"
            ) from restore_error
        operation["state"] = "failed_current_restored"
        operation["restored_readback"] = restored
        journal["rollback"] = operation
        journal["state"] = "complete"
        record["host_active"] = current_version
        _save_host_activation(record, journal)
        raise RuntimeError(
            f"Code-only host rollback failed; restored {current_version} without changing database or worker state: {error}"
        ) from error
    operation["state"] = "complete"
    operation["readback"] = readback
    journal["state"] = "rolled_back"
    journal["rollback"] = operation
    record["host_active"] = previous_version
    _save_host_activation(record, journal)
    return {
        "status": "host_rolled_back",
        "operation_id": operation["operation_id"],
        "version": previous_version,
        "previous_version": current_version,
        "code_only": True,
        **readback,
    }


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
        activate_host=lambda version: activate_supervisor_host(record, version),
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
            "activate-host",
            "rollback-host",
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
    parser.add_argument(
        "--ci-pr-run-id",
        action="append",
        help=(
            "supplement a Playwright-selected main merge with its successful exact-tree "
            "pull_request CI run; repeat in first-parent order for selected merges"
        ),
    )
    parser.add_argument(
        "--ci-playwright-run-id",
        action="append",
        help=(
            "supplement a Playwright-selected main merge with its successful exact-tree "
            "pull_request Playwright matrix run; repeat in first-parent order"
        ),
    )
    parser.add_argument(
        "--allow-degraded-workers",
        action="store_true",
        help=(
            "allow activate-host to proceed from a degraded aggregate worker baseline; "
            "API/database health and exact owner continuity remain required"
        ),
    )
    args = parser.parse_args()
    checked_host()
    if args.ci_run_id is not None and args.action != "prepare":
        raise RuntimeError("--ci-run-id is supported only with prepare")
    if (
        args.ci_pr_run_id is not None or args.ci_playwright_run_id is not None
    ) and args.action != "prepare":
        raise RuntimeError("Supplemental PR CI run IDs are supported only with prepare")
    if args.allow_degraded_workers and args.action != "activate-host":
        raise RuntimeError(
            "--allow-degraded-workers is supported only with activate-host"
        )
    with (ROOT / ".deploy.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = (
            {"status": "state", **state()}
            if args.action == "status"
            else prepare(
                ci_run_ids=args.ci_run_id,
                pr_ci_run_ids=args.ci_pr_run_id,
                pr_playwright_run_ids=args.ci_playwright_run_id,
            )
            if args.action == "prepare"
            else activate_host(allow_degraded_workers=args.allow_degraded_workers)
            if args.action == "activate-host"
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
