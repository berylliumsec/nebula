#!/usr/bin/env python3
"""Prepare exact commits and promote through the selected deployment protocol.

Install beside nebula_deploy_gate.py and nebula_staged_deployer.py on simulation. The initial same-build
cutover provisions current and its user service separately; this deployer
handles later main updates. It never changes the development checkout.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from uuid import uuid4
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from urllib.error import HTTPError
from http.client import HTTPException

from nebula_deploy_gate import (
    TRANSIENT_BUDGET_SECONDS,
    TRANSIENT_FIRST_DELAY_SECONDS,
    TRANSIENT_MAX_DELAY_SECONDS,
    CoreClient,
    blockers,
    service_token,
    tolerate_transient,
    transient_reason,
)
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
PROC_ROOT = Path("/proc")
DATA = ROOT / "state/v3"
BACKUPS = ROOT / "rollback-backups"
DEPLOYMENT_RECORDS = ROOT / "deployment-records"
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
# Acceptance records written before required jobs matched by family listed
# each terminal-client matrix job by its exact GitHub name. Validation accepts
# only this exact legacy expansion in place of the "terminal-client" family.
LEGACY_TERMINAL_CLIENT_CI_JOBS = frozenset(
    f"terminal-client ({runner}, {version})"
    for runner in ("ubuntu-latest", "macos-15")
    for version in ("3.11", "3.12", "3.13")
)
DEPLOYMENT_HISTORY_LIMIT = 32
SUPERVISOR_STOP_GRACE_SECONDS = 30
SUPERVISOR_EXIT_SECONDS = 30
# A project's old worker gets this long to reach a safe handoff boundary.
# After it, the deployer stops that worker and adopts Core's crash-style
# recovery of its projects, so a deployment never waits on a project.
HANDOFF_GRACE_SECONDS = 180
# How long a stopped worker's projects may take to recover on the target.
HANDOFF_RECOVERY_WAIT_SECONDS = 300
HOST_START_ATTEMPTS = 16
HOST_RESTART_DELAY_SECONDS = 5
HOST_QUIET_WAIT_SECONDS = 300
HOST_QUIET_POLL_SECONDS = 5
# Workers built from this commit or later wait out a supervisor replacement
# for host reads (#1023), so a turn may safely cross the host restart.
HOST_RECONNECT_COMMIT = "445c5067072df94daa8100340e1c54ee76b3d35c"
# Longer than an owner lease (30 s) plus Core's dead-owner recovery pass, so
# an owner that recovers on its own is never reported.
HOST_UNAVAILABLE_OWNER_SECONDS = 60
# Project holds (Core #1232): turns may finish for the grace period, then
# Core stops them. The wait covers the grace, Core's 15 s hold pass and the
# stop settling.
PROJECT_HOLD_GRACE_SECONDS = 180
PROJECT_HOLD_WAIT_SECONDS = 600
PROJECT_HOLD_POLL_SECONDS = 5
HOST_RESTART_HOLD_PREFIX = "host-restart-"
# A promote or cycle that finds a prebuild holding the deploy lock waits this
# long for it: the prebuild is producing the release that promote needs.
PREBUILD_LOCK_WAIT_SECONDS = 1800
LOCK_POLL_SECONDS = 2


class StepTimings:
    """Wall-clock seconds for each deployment step, reported as it ends.

    ``begin`` ends the running step and starts the next one. Use ``run`` around
    the whole operation: a step that raises is still recorded and reported, so
    a failed run shows how far it got and how long each step took.
    """

    def __init__(self, label: str, clock=time.monotonic) -> None:
        self.label = label
        self.clock = clock
        self.started = clock()
        self.steps: list[dict[str, Any]] = []
        self._current: tuple[str, float] | None = None

    def begin(self, name: str) -> None:
        self.end()
        self._current = (name, self.clock())

    def end(self, *, failed: bool = False) -> None:
        if self._current is None:
            return
        name, started = self._current
        self._current = None
        seconds = round(self.clock() - started, 1)
        entry: dict[str, Any] = {"step": name, "seconds": seconds}
        if failed:
            entry["failed"] = True
        self.steps.append(entry)
        print(
            f"{self.label}: {name} {'failed after' if failed else 'took'} {seconds}s",
            file=sys.stderr,
            flush=True,
        )

    @contextmanager
    def run(self):
        try:
            yield self
        except BaseException:
            self.end(failed=True)
            raise
        self.end()

    def record(self) -> dict[str, Any]:
        self.end()
        return {
            "steps": list(self.steps),
            "total_seconds": round(self.clock() - self.started, 1),
        }


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


def selected_plan(directory: Path, previous: str) -> dict[str, Any]:
    """Validate every commit's diff-bound selection and record their union."""
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
    # Tests run in CI, once per commit; a selection repeated by later commits
    # is recorded, not re-reviewed, because nothing is counted or run here.
    plan["overlaps"] = overlaps
    plan["reason"] = (
        f"Combined focused selections from {len(commits)} reviewed main commits; "
        "CI ran each commit's selection."
    )
    plan["exclusions"] = "Each commit keeps its own diff-bound review."
    atomic_json(selected, plan)
    return plan


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


def ci_job_in_family(name: str, family: str) -> bool:
    """Return whether a GitHub job name belongs to a required job family.

    GitHub names each matrix instance "<job name> (<matrix values>)", so a
    family is the exact job name or that name followed by " (". A bare prefix
    never matches, which keeps "python (3.12)" apart from "python (3.11)".
    """
    return name == family or name.startswith(f"{family} (")


def require_ci_jobs(jobs: list[dict[str, Any]], required: set[str]) -> dict[str, str]:
    named = [
        job
        for job in jobs
        if isinstance(job, dict) and isinstance(job.get("name"), str)
    ]
    missing = []
    for family in sorted(required):
        members = [job for job in named if ci_job_in_family(job["name"], family)]
        if not members or any(
            job.get("status") != "completed" or job.get("conclusion") != "success"
            for job in members
        ):
            missing.append(family)
    if missing:
        raise RuntimeError(f"CI selected jobs did not succeed: {', '.join(missing)}")
    return {name: "success" for name in sorted(required)}


def recorded_ci_jobs_match(recorded: Any, expected: dict[str, str]) -> bool:
    """Compare a recorded selected-job map with the recomputed expectation.

    Records written before family matching may list the six exact
    terminal-client matrix jobs instead of the "terminal-client" family.
    """
    if recorded == expected:
        return True
    if "terminal-client" not in expected or not isinstance(recorded, dict):
        return False
    legacy = {
        name: value for name, value in expected.items() if name != "terminal-client"
    }
    legacy.update(dict.fromkeys(LEGACY_TERMINAL_CLIENT_CI_JOBS, "success"))
    return recorded == legacy


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
        raise RuntimeError(
            "CI acceptance requires a complete first-parent commit chain"
        )
    parent = previous
    for commit in commits:
        actual = run(
            "git", "-C", str(directory), "rev-parse", f"{commit}^1", capture=True
        )
        if actual != parent:
            raise RuntimeError("CI acceptance first-parent chain has a gap")
        parent = commit
    return commits


def verify_ci_trust_inputs(directory: Path, *, trusted_baseline: str, sha: str) -> None:
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


def require_pr_merge_shape(
    directory: Path, *, merge_sha: str, base_sha: str, head_sha: str
) -> str:
    """Prove that a PR head tested exactly the tree main received.

    A merge commit must have exactly ``[base, head]`` as parents. A squash
    merge has one parent, ``base``; its PR head must contain that parent and
    have the identical tree, so the head's diff from ``base`` is exactly the
    squash commit's diff. Either way the head tree equals the main tree.
    Returns the shape, "merge" or "squash".
    """
    parents = commit_parents(directory, merge_sha)
    if parents == [base_sha, head_sha]:
        shape = "merge"
    elif parents == [base_sha]:
        try:
            run(
                "git",
                "-C",
                str(directory),
                "merge-base",
                "--is-ancestor",
                base_sha,
                head_sha,
            )
        except RuntimeError as error:
            raise RuntimeError(
                "Squash-merged PR head does not contain its main parent"
            ) from error
        shape = "squash"
    else:
        raise RuntimeError("PR CI evidence does not match the merge commit parents")
    if git_tree(directory, head_sha) != git_tree(directory, merge_sha):
        raise RuntimeError("PR head tree differs from the exact main merge tree")
    return shape


def ensure_pr_head(directory: Path, head_sha: str, pull_number: int) -> None:
    """Fetch a merged PR's head when its branch is gone from this clone."""
    try:
        run("git", "-C", str(directory), "cat-file", "-e", f"{head_sha}^{{commit}}")
        return
    except RuntimeError:
        # diagnostic-expected: absent locally; fetched from the PR ref below.
        pass
    run(
        "git",
        "-C",
        str(directory),
        "fetch",
        "--no-tags",
        "origin",
        f"refs/pull/{pull_number}/head",
    )
    run("git", "-C", str(directory), "cat-file", "-e", f"{head_sha}^{{commit}}")


def squash_merged_pr_head(directory: Path, merge_sha: str) -> tuple[str, int]:
    """The head commit and number of the one PR squash-merged as ``merge_sha``."""
    pulls = github_json_value(f"commits/{merge_sha}/pulls")
    if not isinstance(pulls, list):
        raise RuntimeError("GitHub associated pull request listing is malformed")
    matches = [
        item
        for item in pulls
        if isinstance(item, dict)
        and item.get("state") == "closed"
        and item.get("merged_at")
        and item.get("merge_commit_sha") == merge_sha
        and isinstance(item.get("head"), dict)
        and SHA.fullmatch(str(item["head"].get("sha", "")))
        and isinstance(item.get("number"), int)
        and item["number"] > 0
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Squash commit is not bound to one exact merged pull request"
        )
    head_sha = matches[0]["head"]["sha"]
    ensure_pr_head(directory, head_sha, matches[0]["number"])
    return head_sha, matches[0]["number"]


def require_merged_pr_for_commit(
    *, head_sha: str, base_sha: str, merge_sha: str, directory: Path
) -> dict[str, Any]:
    pulls = github_json_value(f"commits/{head_sha}/pulls")
    if not isinstance(pulls, list):
        raise RuntimeError("GitHub associated pull request listing is malformed")
    squash = len(commit_parents(directory, merge_sha)) == 1
    matches = [
        item
        for item in pulls
        if isinstance(item, dict)
        and item.get("state") == "closed"
        and item.get("merged_at")
        and item.get("merge_commit_sha") == merge_sha
        and isinstance(item.get("base"), dict)
        # A squash PR's base may be an older main commit; the shape check
        # below proves its head contains the exact main parent instead.
        and (squash or item["base"].get("sha") == base_sha)
        and isinstance(item.get("head"), dict)
        and item["head"].get("sha") == head_sha
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "PR CI evidence is not bound to one exact merged pull request"
        )
    if not isinstance(matches[0].get("number"), int) or matches[0]["number"] < 1:
        raise RuntimeError("Merged pull request identity is invalid")
    require_pr_merge_shape(
        directory, merge_sha=merge_sha, base_sha=base_sha, head_sha=head_sha
    )
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


# GitHub shows at most this many characters of a job name.
GITHUB_JOB_NAME_LIMIT = 100


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

    def names(lane: str, entry: dict[str, Any]) -> set[str]:
        # GitHub names a matrix job "<lane> (<values in key order>)" and shows
        # a name over 100 characters as its first 97 plus "...".
        name = f"{lane} ({', '.join(str(entry[key]) for key in sorted(entry))})"
        if len(name) <= GITHUB_JOB_NAME_LIMIT:
            return {name}
        return {name, name[: GITHUB_JOB_NAME_LIMIT - 3] + "..."}

    expected = Counter(signature(entry) for entry in include)
    remaining = list(include)
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
        # Each successful job accounts for one selected entry whose display
        # name it carries. When truncation leaves several entries with the
        # same name, as many successful jobs as entries are still required.
        match = next(
            (
                index
                for index, entry in enumerate(remaining)
                if name in names(lane, entry)
            ),
            None,
        )
        if match is None:
            raise RuntimeError("GitHub Playwright job name lacks its matrix identity")
        actual[signature(remaining.pop(match))] += 1
    if actual != expected or remaining:
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
    base_sha: str | None,
    directory: Path,
    trusted_baseline: str,
    review_reason: str | None = None,
) -> tuple[dict[str, Any], str]:
    """The trusted selector's plan for a reviewed browser selection.

    A pull request run selects against its base; a run dispatched on an
    exact main commit has no baseline and carries its operator's reason.
    """
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
        reviewed_reason = (
            review_reason
            if review_reason is not None
            else f"{selection.get('reason', '')} Exclusions: {selection.get('exclusions', '')}"
        )
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
        if base_sha is not None:
            args.extend(("--baseline", base_sha))
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


def verify_commit_playwright_run(
    run_id: str, *, commit: str, directory: Path, trusted_baseline: str
) -> dict[str, Any]:
    """Accept a Playwright run dispatched on exactly ``commit``.

    For a squash merge whose pull request never ran its reviewed browser
    selection, a successful ``playwright-impact.yml`` dispatch on the merged
    commit itself is direct evidence. Its plan must equal the trusted
    selector's plan for the commit's committed selection, and every selected
    entry needs its own successful job.
    """
    if not CI_RUN_ID.fullmatch(run_id):
        raise RuntimeError(
            "Commit Playwright run id must be a positive decimal identifier"
        )
    run_info = github_json(f"actions/runs/{run_id}")
    repository = run_info.get("repository")
    if not isinstance(repository, dict):
        repository = {}
    if (
        run_info.get("head_sha") != commit
        or run_info.get("event") != "workflow_dispatch"
        or run_info.get("status") != "completed"
        or run_info.get("conclusion") != "success"
        or str(repository.get("full_name", "")).lower() != GITHUB_REPOSITORY
        or type(run_info.get("run_attempt")) is not int
        or run_info.get("run_attempt") < 1
        or run_info.get("html_url")
        != f"https://github.com/{GITHUB_REPOSITORY}/actions/runs/{run_id}"
    ):
        raise RuntimeError(
            "Commit Playwright run is not a successful dispatch on the exact commit"
        )
    workflow_id = run_info.get("workflow_id")
    if type(workflow_id) is not int or workflow_id < 1:
        raise RuntimeError("Commit Playwright run has no valid workflow identity")
    workflow = github_json(f"actions/workflows/{workflow_id}")
    if (
        workflow.get("path") != ".github/workflows/playwright-impact.yml"
        or workflow.get("state") != "active"
    ):
        raise RuntimeError(
            "Commit Playwright run did not use the pinned active workflow"
        )
    verify_ci_trust_inputs(directory, trusted_baseline=trusted_baseline, sha=commit)
    files, artifact, artifact_digest = supplemental_artifact_files(
        int(run_id),
        "playwright-impact-receipt",
        {"playwright-impact-plan.json", "playwright-impact-receipt.md"},
    )
    try:
        received_plan = json.loads(files["playwright-impact-plan.json"])
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "Commit Playwright artifact contains invalid JSON"
        ) from error
    if (
        not isinstance(received_plan, dict)
        or received_plan.get("candidate_sha") != commit
        or received_plan.get("baseline_sha") is not None
        or not isinstance(received_plan.get("review_reason"), str)
        or not received_plan["review_reason"].strip()
    ):
        raise RuntimeError("Commit Playwright plan is not bound to the exact commit")
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
    if not isinstance(selection.get("playwright"), list) or not selection["playwright"]:
        raise RuntimeError("Commit has no reviewed browser selection to prove")
    expected_plan, expected_receipt = expected_pr_playwright_plan(
        selection,
        head_sha=commit,
        base_sha=None,
        directory=directory,
        trusted_baseline=trusted_baseline,
        review_reason=received_plan["review_reason"],
    )
    if (
        expected_plan.get("coverage_review_required")
        or not expected_plan.get("include")
        or received_plan != expected_plan
        or files["playwright-impact-receipt.md"].decode("utf-8") != expected_receipt
    ):
        raise RuntimeError(
            "Commit Playwright artifact differs from the pinned selector result"
        )
    selected_jobs = validate_playwright_matrix_jobs(
        github_run_jobs(int(run_id)), expected_plan["include"]
    )
    if artifact.get("expired") is not False:
        raise RuntimeError("Commit Playwright artifact is expired")
    return {
        "source": "github-actions-commit-playwright",
        "run_id": int(run_id),
        "run_url": run_info["html_url"],
        "run_attempt": run_info["run_attempt"],
        "workflow_path": ".github/workflows/playwright-impact.yml",
        "commit": commit,
        "review_reason": received_plan["review_reason"],
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
    commit_playwright: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not CI_RUN_ID.fullmatch(run_id):
        raise RuntimeError("CI run id must be a positive decimal identifier")
    if supplemental_playwright is not None and commit_playwright is not None:
        raise RuntimeError("A commit's browser evidence has exactly one source")
    browser_proven = (
        supplemental_playwright is not None or commit_playwright is not None
    )
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
    if bool(selection.get("playwright")) != browser_proven:
        raise RuntimeError(
            "Selected Playwright checks require exact merged-PR or exact-commit evidence"
        )
    jobs = github_run_jobs(int(run_id))
    required_jobs = selected_ci_jobs(
        selection, supplemental_playwright_verified=browser_proven
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
    if commit_playwright is not None:
        receipt["commit_playwright_evidence"] = commit_playwright
    return receipt


class CiEvidenceUnavailable(RuntimeError):
    """A candidate commit has no usable CI evidence; prepare stops, nothing runs locally."""


def _newest_successful_pr_run(workflow: str, head_sha: str, label: str) -> str:
    listing = github_json(
        f"actions/workflows/{workflow}/runs?"
        f"head_sha={head_sha}&event=pull_request&per_page=50"
    )
    runs = [
        item
        for item in listing.get("workflow_runs", [])
        if isinstance(item, dict)
        and item.get("head_sha") == head_sha
        and item.get("event") == "pull_request"
    ]
    succeeded = [
        item
        for item in runs
        if item.get("status") == "completed" and item.get("conclusion") == "success"
    ]
    if not succeeded:
        states = sorted(
            {f"{item.get('status')}/{item.get('conclusion') or '-'}" for item in runs}
        )
        raise CiEvidenceUnavailable(
            f"{label} has no successful pull request {workflow} run on its head "
            f"{head_sha[:12]}"
            + (f" (runs: {', '.join(states)})" if states else " (no run)")
            + "; re-run it"
        )
    return str(max(succeeded, key=lambda item: int(item.get("id", 0)))["id"])


def _newest_successful_commit_playwright_run(commit: str) -> str | None:
    listing = github_json(
        "actions/workflows/playwright-impact.yml/runs?"
        f"head_sha={commit}&event=workflow_dispatch&per_page=50"
    )
    succeeded = [
        item
        for item in listing.get("workflow_runs", [])
        if isinstance(item, dict)
        and item.get("head_sha") == commit
        and item.get("event") == "workflow_dispatch"
        and item.get("status") == "completed"
        and item.get("conclusion") == "success"
    ]
    if not succeeded:
        return None
    return str(max(succeeded, key=lambda item: int(item.get("id", 0)))["id"])


def discover_pr_supplement_run_ids(
    directory: Path, *, previous: str, sha: str
) -> tuple[list[str], list[str], dict[str, str]]:
    """Find browser evidence for each Playwright-selected commit.

    Push CI does not run browser checks. They come from the PR whose head
    tested exactly the tree main received (a merge commit's second parent, or
    the head of the PR squash-merged as the commit), when both its CI and
    Playwright runs succeeded. Otherwise they come from a successful
    ``playwright-impact.yml`` run dispatched on the exact commit. Acceptance
    re-verifies shapes, trees, plans and every selected job.
    """

    pr_ci: list[str] = []
    pr_playwright: list[str] = []
    commit_runs: dict[str, str] = {}
    for commit in first_parent_commits(directory, previous=previous, sha=sha):
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
        if not committed.get("playwright"):
            continue
        parents = commit_parents(directory, commit)
        if len(parents) == 2:
            head_sha = parents[1]
        elif len(parents) == 1:
            head_sha, _pull = squash_merged_pr_head(directory, commit)
        else:
            raise CiEvidenceUnavailable(
                f"{commit[:12]} selects Playwright checks but is neither a merge "
                "nor a squash commit, so no pull request runs can prove them"
            )
        try:
            ci_run = _newest_successful_pr_run("ci.yml", head_sha, commit[:12])
            playwright_run = _newest_successful_pr_run(
                "playwright-impact.yml", head_sha, commit[:12]
            )
        except CiEvidenceUnavailable as missing:
            dispatched = _newest_successful_commit_playwright_run(commit)
            if dispatched is None:
                raise CiEvidenceUnavailable(
                    f"{missing}; nor is there a successful playwright-impact.yml "
                    f"run dispatched on {commit[:12]} itself"
                ) from missing
            commit_runs[commit] = dispatched
            continue
        pr_ci.append(ci_run)
        pr_playwright.append(playwright_run)
    return pr_ci, pr_playwright, commit_runs


def discover_ci_run_ids(directory: Path, *, previous: str, sha: str) -> list[str]:
    """Find the successful main push CI run for each first-parent commit.

    CI already ran each commit's reviewed selection before and after merge, so
    the deployer accepts that evidence instead of re-running tests on the Core
    host. A commit without a successful run stops preparation with the exact
    reason. A Playwright-selected commit still needs this run for its other
    layers; its browser evidence comes from discover_pr_supplement_run_ids.
    """

    run_ids: list[str] = []
    for commit in first_parent_commits(directory, previous=previous, sha=sha):
        listing = github_json(
            "actions/workflows/ci.yml/runs?"
            f"head_sha={commit}&branch=main&event=push&per_page=50"
        )
        runs = [
            item
            for item in listing.get("workflow_runs", [])
            if isinstance(item, dict)
            and item.get("head_sha") == commit
            and item.get("head_branch") == "main"
            and item.get("event") == "push"
        ]
        succeeded = [
            item
            for item in runs
            if item.get("status") == "completed" and item.get("conclusion") == "success"
        ]
        if not succeeded:
            states = sorted(
                {
                    f"{item.get('status')}/{item.get('conclusion') or '-'}"
                    for item in runs
                }
            )
            raise CiEvidenceUnavailable(
                f"{commit[:12]} has no successful main push CI run"
                + (f" (runs: {', '.join(states)})" if states else " (no run yet)")
                + "; wait for CI or re-run it"
            )
        newest = max(succeeded, key=lambda item: int(item.get("id", 0)))
        run_ids.append(str(newest["id"]))
    return run_ids


def verify_ci_acceptance_chain(
    run_ids: list[str],
    *,
    previous: str,
    sha: str,
    directory: Path,
    pr_ci_run_ids: list[str] | None = None,
    pr_playwright_run_ids: list[str] | None = None,
    commit_playwright_run_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    commits = first_parent_commits(directory, previous=previous, sha=sha)
    commit_playwright_run_ids = dict(commit_playwright_run_ids or {})
    if len(run_ids) != len(commits):
        raise RuntimeError(
            "CI acceptance requires exactly one run for each first-parent commit"
        )
    if len(set(run_ids)) != len(run_ids):
        raise RuntimeError("CI acceptance run IDs must be unique")

    pr_ci_run_ids = pr_ci_run_ids or []
    pr_playwright_run_ids = pr_playwright_run_ids or []
    dispatch_ids = list(commit_playwright_run_ids.values())
    every_id = (*run_ids, *pr_ci_run_ids, *pr_playwright_run_ids, *dispatch_ids)
    if len(set(every_id)) != len(every_id):
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
        if selected and commit not in commit_playwright_run_ids:
            playwright_commits.append(commit)
        elif not selected and commit in commit_playwright_run_ids:
            raise RuntimeError(
                "Exact-commit Playwright evidence names a commit without browser checks"
            )
    if set(commit_playwright_run_ids) - set(commits):
        raise RuntimeError("Exact-commit Playwright evidence names an unknown commit")
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
            if len(parents) == 2:
                base_sha, head_sha = parents
            elif len(parents) == 1:
                # A squash merge: its PR's runs count only for the identical tree.
                base_sha = parents[0]
                head_sha, _pull = squash_merged_pr_head(directory, commit)
                require_pr_merge_shape(
                    directory, merge_sha=commit, base_sha=base_sha, head_sha=head_sha
                )
            else:
                raise RuntimeError(
                    "Supplemental Playwright evidence requires a merge or squash commit"
                )
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
        commit_playwright = (
            verify_commit_playwright_run(
                commit_playwright_run_ids[commit],
                commit=commit,
                directory=directory,
                trusted_baseline=previous,
            )
            if commit in commit_playwright_run_ids
            else None
        )
        receipt = verify_ci_acceptance(
            run_id,
            previous=baseline,
            sha=commit,
            directory=directory,
            trusted_baseline=previous,
            supplemental_playwright=supplemental,
            commit_playwright=commit_playwright,
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


def expected_matrix_job_record(entries: list[Any]) -> dict[str, str]:
    """The job conclusions an accepted Playwright matrix record must hold."""
    expected_jobs = {"plan": "success", "gate": "success"}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("release CI Playwright record is invalid")
        signature = tuple(
            entry.get(field)
            for field in ("area", "grep", "project", "runtime", "shard")
        )
        if any(not isinstance(value, str) or not value for value in signature):
            raise RuntimeError("release CI Playwright record is invalid")
        label = json.dumps(signature, separators=(",", ":"))
        index = 1
        while f"playwright:{label}:{index}" in expected_jobs:
            index += 1
        expected_jobs[f"playwright:{label}:{index}"] = "success"
    return expected_jobs


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
            or pr_ci["selection_change_digest"] != playwright["selection_change_digest"]
            or pr_ci["selection_plan_sha256"] != playwright["selection_plan_sha256"]
        ):
            raise RuntimeError("release CI supplemental Playwright record is invalid")
        expected_jobs = expected_matrix_job_record(playwright["selected_entries"])
        if playwright.get("selected_jobs") != expected_jobs:
            raise RuntimeError(
                "release CI supplemental Playwright shard record is invalid"
            )
        if directory is not None:
            head = playwright["head_commit"]
            ensure_pr_head(directory, head, int(pull_number))
            try:
                require_pr_merge_shape(
                    directory, merge_sha=commit, base_sha=baseline, head_sha=head
                )
            except RuntimeError as error:
                raise RuntimeError(
                    "release CI supplemental merge parents are invalid"
                ) from error
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
        commit_evidence = item.get("commit_playwright_evidence")
        if supplemental is not None and commit_evidence is not None:
            raise RuntimeError("release CI browser evidence has two sources")
        if commit_evidence is not None:
            if (
                not isinstance(commit_evidence, dict)
                or commit_evidence.get("source") != "github-actions-commit-playwright"
                or commit_evidence.get("workflow_path")
                != ".github/workflows/playwright-impact.yml"
                or commit_evidence.get("commit") != commit
                or type(commit_evidence.get("run_id")) is not int
                or commit_evidence["run_id"] < 1
                or commit_evidence.get("run_url")
                != f"https://github.com/{GITHUB_REPOSITORY}/actions/runs/{commit_evidence['run_id']}"
                or not isinstance(commit_evidence.get("review_reason"), str)
                or not commit_evidence["review_reason"].strip()
                or not SHA256.fullmatch(
                    str(commit_evidence.get("playwright_plan_sha256", ""))
                )
                or not SHA256.fullmatch(str(commit_evidence.get("artifact_sha256", "")))
                or not isinstance(commit_evidence.get("selected_entries"), list)
                or not commit_evidence["selected_entries"]
                or commit_evidence.get("selected_jobs")
                != expected_matrix_job_record(commit_evidence["selected_entries"])
            ):
                raise RuntimeError("release exact-commit Playwright record is invalid")
            if commit_evidence["run_id"] in accepted_run_ids:
                raise RuntimeError("release CI acceptance run IDs are duplicated")
            accepted_run_ids.add(commit_evidence["run_id"])
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
            browser_proven = supplemental is not None or commit_evidence is not None
            if bool(selection.get("playwright")) != browser_proven:
                raise RuntimeError(
                    "release CI record does not cover its Playwright selection"
                )
            expected_main_jobs = {
                name: "success"
                for name in selected_ci_jobs(
                    selection, supplemental_playwright_verified=browser_proven
                )
            }
            if not recorded_ci_jobs_match(item["selected_jobs"], expected_main_jobs):
                raise RuntimeError("release CI selected-job record is incomplete")
            verify_ci_trust_inputs(directory, trusted_baseline=previous, sha=commit)
            if commit_evidence is not None:
                expected_plan, _ = expected_pr_playwright_plan(
                    selection,
                    head_sha=commit,
                    base_sha=None,
                    directory=directory,
                    trusted_baseline=previous,
                    review_reason=commit_evidence["review_reason"],
                )
                if commit_evidence["selected_entries"] != expected_plan.get("include"):
                    raise RuntimeError(
                        "release exact-commit Playwright entries differ from the trusted selector"
                    )
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
                if not recorded_ci_jobs_match(pr_ci["selected_jobs"], expected_pr_jobs):
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
    if (
        expected_commits is not None
        and [item["commit"] for item in ci["receipts"]] != expected_commits
    ):
        raise RuntimeError("release CI acceptance record is invalid")


def ci_acceptance_receipts(ci: dict[str, Any]) -> list[dict[str, Any]]:
    if ci.get("source") == "github-actions-ci":
        return [ci]
    receipts = ci.get("receipts")
    if ci.get("source") == "github-actions-ci-chain" and isinstance(receipts, list):
        return receipts
    raise RuntimeError("CI acceptance record has an unsupported source")


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
    require_ci_evidence: bool = False,
) -> dict[str, Any]:
    """Build and accept origin/main for this Core host.

    Every pull request merges only after its own CI passes, so by default the
    candidate is not re-checked against CI runs here: main push runs share a
    cancel-in-progress group, and back-to-back merges cancel each other's
    evidence. ``require_ci_evidence`` (or named run IDs) restores the check
    for every commit since the live release. Tests never run on this host; it
    builds, lints and verifies the release. Each step's duration is reported
    as it ends and kept in the receipt.
    """
    with StepTimings("prepare").run() as timings:
        return _prepare(
            ci_run_ids=ci_run_ids,
            pr_ci_run_ids=pr_ci_run_ids,
            pr_playwright_run_ids=pr_playwright_run_ids,
            require_ci_evidence=require_ci_evidence,
            timings=timings,
        )


def _prepare(
    *,
    ci_run_ids: list[str] | None,
    pr_ci_run_ids: list[str] | None,
    pr_playwright_run_ids: list[str] | None,
    require_ci_evidence: bool = False,
    timings: StepTimings,
) -> dict[str, Any]:
    check_ci = require_ci_evidence or ci_run_ids is not None
    if ci_run_ids is None and (
        pr_ci_run_ids is not None or pr_playwright_run_ids is not None
    ):
        raise RuntimeError(
            "Supplemental PR CI runs require the main CI acceptance chain"
        )
    timings.begin("fetch")
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
    current_state = state()
    journal = current_state.get("runtime_deployment")
    if (
        isinstance(journal, dict)
        and journal.get("state") != "complete"
        and journal.get("version") != sha
    ):
        raise RuntimeError(
            "A different deployment operation is still unresolved; reconcile it before replacing the pending candidate"
        )
    if (RELEASES / sha).is_dir():
        timings.begin("verify_prebuilt_release")
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
    timings.begin("checkout")
    directory = candidate(sha)
    ci_acceptance: dict[str, Any] | None = None
    if check_ci:
        timings.begin("ci_evidence")
        if ci_run_ids is None:
            ci_run_ids = discover_ci_run_ids(directory, previous=previous, sha=sha)
            (
                pr_ci_run_ids,
                pr_playwright_run_ids,
                commit_playwright_run_ids,
            ) = discover_pr_supplement_run_ids(directory, previous=previous, sha=sha)
        else:
            commit_playwright_run_ids = {}
        ci_acceptance = verify_ci_acceptance_chain(
            ci_run_ids,
            previous=previous,
            sha=sha,
            directory=directory,
            pr_ci_run_ids=pr_ci_run_ids,
            pr_playwright_run_ids=pr_playwright_run_ids,
            commit_playwright_run_ids=commit_playwright_run_ids,
        )
    plan = selected_plan(directory, previous)
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
    timings.begin("python_dependencies")
    run(POETRY, "check", cwd=directory)
    run(POETRY, "install", "--with", "dev", "--no-interaction", cwd=directory)
    timings.begin("ui_dependencies")
    run("npm", "--prefix", "ui", "ci", "--no-audit", "--no-fund", cwd=directory)
    timings.begin("ui_lint_and_audit")
    run("npm", "--prefix", "ui", "run", "lint", "--if-present", cwd=directory)
    run("npm", "--prefix", "ui", "run", "audit:diagnostics", cwd=directory)
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    build_env = {"NEBULA_BUILD_TIMESTAMP": timestamp}
    timings.begin("ui_build")
    run("npm", "--prefix", "ui", "run", "build", cwd=directory, env=build_env)
    timings.begin("core_build")
    run(
        POETRY,
        "run",
        "python",
        "-m",
        "scripts.build_nebula_core",
        cwd=directory,
        env=build_env,
    )
    timings.begin("verify_build")
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
    timings.begin("release_copy")
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
        # Counts of the tests CI ran for the reviewed selections.
        "python_tests": plan["expected_tests"]["python"],
        "frontend_tests": plan["expected_tests"]["frontend"],
        "browser_tests": plan["expected_tests"]["playwright"],
        "core_sha256": sha256(staging / "dist/nebula-core"),
        "web_manifest_sha256": sha256(staging / "ui/dist/web-build.json"),
        "test_selection_sha256": sha256(staging / ".github/test-selection.json"),
        "cumulative_selection_sha256": sha256(
            staging / "build/simulation-test-selection.json"
        ),
        "status": "accepted_candidate",
        "runtime_manifest_sha256": runtime_digest,
    }
    receipt["ci_acceptance"] = ci_acceptance
    if ci_acceptance is None:
        receipt["ci_acceptance_skipped"] = (
            "Pull requests merge only after their CI passes; the deployer does "
            "not re-check main CI runs unless --require-ci-evidence is given."
        )
    receipt["timings"] = timings.record()
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


def _read_deployment_operation(operation_id: str) -> dict[str, Any]:
    value = api().get(f"deployment/operations/{operation_id}")
    if not isinstance(value, dict) or value.get("operation_id") != operation_id:
        raise RuntimeError("Core returned an unrelated deployment operation receipt")
    return value


def _require_recorded_operation(
    journal: dict[str, Any], *, step: str, action: str, expected_state: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    steps = journal.get("steps")
    entry = steps.get(step) if isinstance(steps, dict) else None
    operation_id = f"{journal['id']}:{step.replace('/', ':')}"
    if (
        not isinstance(entry, dict)
        or entry.get("operation_id") != operation_id
        or entry.get("action") != action
        or not isinstance(entry.get("parameters"), dict)
        or not isinstance(entry.get("receipt"), dict)
    ):
        raise RuntimeError(f"Deployment journal has no exact {step} operation")
    receipt = _read_deployment_operation(operation_id)
    if (
        receipt.get("action") != action
        or receipt.get("state") != expected_state
        or receipt != entry["receipt"]
        or not isinstance(receipt.get("result"), dict)
    ):
        raise RuntimeError(f"Core {step} receipt differs from the deployment journal")
    return entry, receipt


def reconcile_runtime_deployment(deployment_id: str) -> dict[str, Any]:
    """Archive only a proven pre-spawn MemoryBudgetError from API preparation."""
    if not re.fullmatch(r"[0-9a-f]{32}", deployment_id):
        raise ValueError("deployment ID must be a 32-character journal identity")
    record = state()
    history = record.get("runtime_deployment_history", [])
    if not isinstance(history, list):
        raise RuntimeError("Deployment history archive is malformed")
    prior_archive = next(
        (
            item
            for item in history
            if isinstance(item, dict) and item.get("id") == deployment_id
        ),
        None,
    )
    if prior_archive is not None:
        if prior_archive.get("classification") != "pre_spawn_memory_budget_rejection":
            raise RuntimeError("Archived deployment reconciliation has another outcome")
        return {"status": "already_reconciled", "deployment": prior_archive}

    journal = record.get("runtime_deployment")
    if (
        not isinstance(journal, dict)
        or journal.get("id") != deployment_id
        or journal.get("action") != "promote"
        or journal.get("state") != "running"
    ):
        raise RuntimeError("No matching unresolved promotion journal exists")
    version, previous = journal.get("version"), journal.get("previous")
    pending, active = record.get("pending"), record.get("active")
    if (
        not isinstance(version, str)
        or not SHA.fullmatch(version)
        or not isinstance(previous, str)
        or not SHA.fullmatch(previous)
        or not isinstance(pending, str)
        or not SHA.fullmatch(pending)
        or pending == version
        or active != previous
    ):
        raise RuntimeError(
            "Deployment state does not match the pending-candidate recovery"
        )
    pending_release = verified_release(pending)
    pending_acceptance = json.loads(
        (pending_release / "simulation-acceptance.json").read_text()
    )
    if pending_acceptance.get("previous") != previous:
        raise RuntimeError(
            "Pending candidate was accepted against another active release"
        )
    steps = journal.get("steps")
    if not isinstance(steps, dict) or set(steps) != {"stage", "migrate", "api"}:
        raise RuntimeError(
            "Promotion journal contains additional or missing operations"
        )

    stage_entry, stage_receipt = _require_recorded_operation(
        journal, step="stage", action="stage", expected_state="succeeded"
    )
    staged_release, manifest_sha256 = runtime_release(version)
    if (
        stage_entry["parameters"].get("source") != str(staged_release)
        or stage_entry["parameters"].get("manifest_sha256") != manifest_sha256
        or stage_receipt["result"].get("version") != version
        or stage_receipt["result"].get("manifest_sha256") != manifest_sha256
    ):
        raise RuntimeError("Core stage receipt is not bound to the accepted release")

    migration_entry, migration_receipt = _require_recorded_operation(
        journal, step="migrate", action="migrate", expected_state="succeeded"
    )
    if (
        migration_entry["parameters"].get("version") != version
        or type(migration_entry["parameters"].get("expected_revision")) is not int
    ):
        raise RuntimeError("Migration journal is not bound to the selected release")

    api_entry = steps["api"]
    api_operation_id = f"{deployment_id}:api"
    if (
        not isinstance(api_entry, dict)
        or api_entry.get("operation_id") != api_operation_id
        or api_entry.get("action") != "prepare_api"
        or api_entry.get("parameters") != {"version": version}
        or not isinstance(api_entry.get("receipt"), dict)
    ):
        raise RuntimeError("Deployment journal has no exact API prepare operation")
    api_receipt = _read_deployment_operation(api_operation_id)
    api_result = api_receipt.get("result")
    if (
        api_receipt.get("action") != "prepare_api"
        or api_receipt.get("state") != "uncertain"
        or api_receipt != api_entry["receipt"]
        or not isinstance(api_result, dict)
        or api_result.get("error_type") != "MemoryBudgetError"
        or any(key in api_result for key in ("runtime_id", "pid", "process_birth"))
    ):
        raise RuntimeError(
            "API prepare is not the exact known pre-spawn MemoryBudgetError"
        )

    deployment_host = journal.get("host_activation")
    host = record.get("host_activation")
    legacy_top_level_activation = deployment_host is None
    if not isinstance(host, dict):
        raise RuntimeError("Deployment host activation is not durably complete")
    if legacy_top_level_activation:
        if (
            host.get("version") != version
            or host.get("previous_version") != previous
            or host.get("state") != "complete"
            or not isinstance(host.get("operation_id"), str)
            or not host.get("operation_id")
        ):
            raise RuntimeError(
                "Legacy top-level host activation does not match deployment"
            )
    elif (
        not isinstance(deployment_host, dict)
        or deployment_host.get("version") != version
        or deployment_host.get("state") != "complete"
        or not isinstance(deployment_host.get("operation_id"), str)
        or host.get("version") != version
        or host.get("previous_version") != previous
        or host.get("state") != "complete"
        or host.get("operation_id") != deployment_host.get("operation_id")
    ):
        raise RuntimeError("Deployment host activation is not durably complete")
    host_result = (
        deployment_host.get("result") if isinstance(deployment_host, dict) else None
    )
    host_readback = host.get("readback")
    if (
        (
            not legacy_top_level_activation
            and (
                not isinstance(host_result, dict)
                or host_result.get("status") != "host_activated"
                or host_result.get("version") != version
                or host_result.get("health_commit") != version
                or host_result.get("operation_id")
                != deployment_host.get("operation_id")
            )
        )
        or record.get("host_active") != version
        or not isinstance(host_readback, dict)
        or host_readback.get("health_commit") != version
        or host_readback.get("api_version") != previous
        or not isinstance(host_readback.get("resident_versions"), list)
    ):
        raise RuntimeError("Host activation lacks its exact version readback")
    if current_release_version() != version or unit_state(SERVICE) != "active":
        raise RuntimeError("Current host is not the selected healthy release")
    package_version = runtime_package_version(version)
    identity = host_connection(allow_stale=True)
    if identity is None or identity.get("package_version") != package_version:
        raise RuntimeError("Current runtime discovery does not match the selected host")
    require_runtime_process_in_service(supervisor_service_identity(), identity, version)
    health, status = read_supervisor_health()
    residents = status.get("resident_versions")
    if (
        health.get("status") != "ok"
        or health.get("database") != "ok"
        or health.get("commit") != version
        or health.get("recovery_version") != package_version
        or status.get("protocol") != "nebula.runtime/v1"
        or status.get("health") != "ready"
        or status.get("api_version") != previous
        or status.get("target_execution_version") != version
        or residents != host_readback["resident_versions"]
        or not isinstance(residents, list)
        or version in residents
    ):
        raise RuntimeError(
            "Live API, host, or resident state changed from the proven pre-spawn state"
        )

    if len(history) >= DEPLOYMENT_HISTORY_LIMIT:
        raise RuntimeError(
            "Deployment history archive is full; retain existing records"
        )
    reconciliation = {
        "id": deployment_id,
        "state": "reconciled_no_effect",
        "classification": "pre_spawn_memory_budget_rejection",
        "reconciled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "journal": deepcopy(journal),
        "core_receipts": {
            "stage": stage_receipt,
            "migrate": migration_receipt,
            "api": api_receipt,
        },
        "live_readback": {
            "current_release": version,
            "health_commit": health["commit"],
            "api_version": status["api_version"],
            "target_execution_version": status["target_execution_version"],
            "resident_versions": residents,
            "deployment_health": status["health"],
        },
        "pending_candidate_preserved": pending,
    }
    history.append(reconciliation)
    record["runtime_deployment_history"] = history
    record.pop("runtime_deployment", None)
    atomic_json(STATE, record)
    return {"status": "reconciled_no_effect", "deployment": reconciliation}


def _manual_record(prefix: str, name: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]{8}-[0-9a-f]{8}", prefix):
        raise ValueError("manual receipt prefix must be YYYYMMDD-<8 hex commit digits>")
    path = DEPLOYMENT_RECORDS / f"{prefix}-{name}.json"
    if path.is_symlink() or path.parent.resolve() != DEPLOYMENT_RECORDS.resolve():
        raise RuntimeError("Manual deployment receipt path is unsafe")
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError(f"Manual deployment receipt is missing or oversized: {name}")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError(
            f"Manual deployment receipt is unreadable: {name}"
        ) from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Manual deployment receipt has an invalid shape: {name}")
    return value


def _manual_runtime_package_version(version: str) -> str:
    """Read the package identity from a manually staged, hash-bound release."""
    release = RELEASES / version
    if (
        release.is_symlink()
        or release.resolve(strict=True).parent != RELEASES.resolve()
        or release.name != version
    ):
        raise RuntimeError("Manual runtime release path is unsafe")
    outer = json.loads((release / "nebula-runtime.json").read_text())
    relative = "dist/nebula-runtime/nebula-runtime.json"
    files = outer.get("files")
    expected = files.get(relative) if isinstance(files, dict) else None
    bundle_manifest = release / relative
    expected_bundle_path = (release / "dist/nebula-runtime").resolve(strict=True)
    if (
        outer.get("version") != version
        or not isinstance(expected, str)
        or not SHA256.fullmatch(expected)
        or (release / "dist").is_symlink()
        or (release / "dist/nebula-runtime").is_symlink()
        or bundle_manifest.is_symlink()
        or bundle_manifest.resolve(strict=True).parent != expected_bundle_path
        or sha256(bundle_manifest) != expected
    ):
        raise RuntimeError("Manual runtime manifest does not bind its package identity")
    value = json.loads(bundle_manifest.read_text()).get("version")
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(character) < 33 for character in value)
    ):
        raise RuntimeError("Manual runtime package version is invalid")
    return value


def _verify_manual_operation(
    request: Any,
    saved_receipt: Any,
    *,
    action: str,
    version: str,
    operation_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(request, dict) or not isinstance(saved_receipt, dict):
        raise RuntimeError(f"Manual {action} receipt is incomplete")
    request_id = request.get("operation_id")
    parameters = request.get("parameters")
    if (
        request.get("action") != action
        or not isinstance(request_id, str)
        or operation_id is not None
        and request_id != operation_id
        or not isinstance(parameters, dict)
    ):
        raise RuntimeError(f"Manual {action} request does not match the release")
    receipt = _read_deployment_operation(request_id)
    if (
        receipt.get("operation_id") != request_id
        or receipt.get("action") != action
        or receipt.get("state") != saved_receipt.get("state")
        or receipt.get("result") != saved_receipt.get("result")
        or saved_receipt.get("operation_id") != request_id
        or saved_receipt.get("action") != action
    ):
        raise RuntimeError(f"Core {action} receipt differs from the manual record")
    if action == "stage":
        source = parameters.get("source")
        if not isinstance(source, str):
            raise RuntimeError("Manual stage receipt has no exact source release")
        release = Path(source)
        if (
            release.is_symlink()
            or release.resolve(strict=True).parent != RELEASES.resolve()
            or release.name != version
        ):
            raise RuntimeError("Manual stage source is not the exact immutable release")
        manifest_sha256 = sha256(release / "nebula-runtime.json")
        if (
            parameters.get("manifest_sha256") != manifest_sha256
            or receipt.get("state") != "succeeded"
            or (receipt.get("result") or {}).get("version") != version
            or (receipt.get("result") or {}).get("manifest_sha256") != manifest_sha256
        ):
            raise RuntimeError("Manual stage receipt is not bound to the exact release")
    elif action == "migrate":
        result = receipt.get("result") or {}
        if (
            parameters.get("version") != version
            or receipt.get("state") != "succeeded"
            or result.get("version") != version
            or result.get("state") != "current"
            or not isinstance(result.get("target_head"), str)
            or result.get("current_head") != result.get("target_head")
        ):
            raise RuntimeError("Manual migration receipt is not exact and current")
    elif action == "prepare_api":
        result = receipt.get("result") or {}
        if (
            parameters.get("version") != version
            or receipt.get("state") != "succeeded"
            or result.get("version") != version
            or result.get("role") != "api"
            or not isinstance(result.get("runtime_id"), str)
        ):
            raise RuntimeError("Manual API preparation receipt is not exact")
    elif action == "promote_api":
        if not isinstance(parameters.get("runtime_id"), str) or receipt.get(
            "state"
        ) not in {"succeeded", "uncertain"}:
            raise RuntimeError("Manual API promotion receipt is not reconcilable")
    else:
        raise ValueError(f"Unsupported manual receipt action: {action}")
    return receipt


def reconcile_superseded_manual_deployment(
    deployment_id: str,
    *,
    manual_prefix: str,
    allow_degraded_baseline: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Archive a manual release that superseded a stale managed promotion.

    This records a later, authenticated host/API state and preserves the full
    older journal. It never replays or reclassifies an uncertain Core effect as
    no-effect. A degraded aggregate health state requires an explicit flag and
    exact host/API/database identity.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", deployment_id):
        raise ValueError("deployment ID must be a 32-character journal identity")
    record = state()
    history = record.get("runtime_deployment_history", [])
    if not isinstance(history, list):
        raise RuntimeError("Deployment history archive is malformed")
    prior_archive = next(
        (
            item
            for item in history
            if isinstance(item, dict) and item.get("id") == deployment_id
        ),
        None,
    )
    if prior_archive is not None:
        if prior_archive.get("classification") != "superseded_by_manual_deployment":
            raise RuntimeError("Archived deployment has another reconciliation outcome")
        return {"status": "already_reconciled", "deployment": prior_archive}

    journal = record.get("runtime_deployment")
    if (
        not isinstance(journal, dict)
        or journal.get("id") != deployment_id
        or journal.get("action") != "promote"
        or journal.get("state") != "running"
    ):
        raise RuntimeError("No matching unresolved promotion journal exists")
    if record.get("pending") is not None or record.get("promoting") is not None:
        raise RuntimeError(
            "A pending or active deployer candidate must be resolved first"
        )
    version = journal.get("version")
    steps = journal.get("steps")
    expected_steps = {"stage", "migrate", "api", "promote-api", "target"}
    if (
        not isinstance(version, str)
        or not SHA.fullmatch(version)
        or not isinstance(steps, dict)
        or set(steps) != expected_steps
    ):
        raise RuntimeError("The unresolved journal has an unsupported operation shape")

    core_receipts: dict[str, dict[str, Any]] = {}
    for step, action in (
        ("stage", "stage"),
        ("migrate", "migrate"),
        ("api", "prepare_api"),
        ("promote-api", "promote_api"),
        ("target", "target_execution"),
    ):
        entry = steps.get(step)
        operation_id = f"{deployment_id}:{step.replace('/', ':')}"
        if (
            not isinstance(entry, dict)
            or entry.get("operation_id") != operation_id
            or entry.get("action") != action
            or not isinstance(entry.get("parameters"), dict)
            or not isinstance(entry.get("receipt"), dict)
        ):
            raise RuntimeError(f"Journal step {step} is not exact")
        receipt = _read_deployment_operation(operation_id)
        saved = entry["receipt"]
        if (
            receipt.get("operation_id") != operation_id
            or receipt.get("action") != action
            or receipt.get("state") != saved.get("state")
            or receipt.get("result") != saved.get("result")
        ):
            raise RuntimeError(f"Core {step} receipt differs from the saved journal")
        if step in {"promote-api", "target"} and receipt.get("state") == "succeeded":
            # Hosts that return a bounded operation summary confirm these
            # effects directly; older hosts left them uncertain (below).
            pass
        elif step in {"promote-api", "target"}:
            settled = entry.get("settled")
            if (
                receipt.get("state") != "uncertain"
                or not isinstance(settled, dict)
                or settled.get("by") != "deployment_status"
                or (settled.get("evidence") or {}).get(
                    "api_version"
                    if step == "promote-api"
                    else "target_execution_version"
                )
                != version
                or (settled.get("result") or {}).get(
                    "api_version"
                    if step == "promote-api"
                    else "target_execution_version"
                )
                != version
            ):
                raise RuntimeError(
                    f"Uncertain {step} lacks its exact saved status settlement"
                )
        elif receipt.get("state") != "succeeded":
            raise RuntimeError(f"Journal step {step} is not confirmed successful")
        core_receipts[step] = receipt

    old_api_result = core_receipts["api"].get("result")
    old_api_runtime = (
        old_api_result.get("runtime_id") if isinstance(old_api_result, dict) else None
    )
    old_stage = steps["stage"]["parameters"]
    old_migration = steps["migrate"]["parameters"]
    old_prepare = steps["api"]["parameters"]
    old_promote = steps["promote-api"]["parameters"]
    old_target = steps["target"]["parameters"]
    old_release, old_manifest_sha256 = runtime_release(version)
    if (
        not isinstance(old_stage.get("source"), str)
        or old_stage.get("source") != str(old_release)
        or old_stage.get("manifest_sha256") != old_manifest_sha256
        or old_migration.get("version") != version
        or type(old_migration.get("expected_revision")) is not int
        or old_prepare != {"version": version}
        or not isinstance(old_api_runtime, str)
        or (old_api_result or {}).get("version") != version
        or (old_api_result or {}).get("role") != "api"
        or old_promote.get("runtime_id") != old_api_runtime
        or type(old_promote.get("expected_revision")) is not int
        or old_target != {"version": version}
    ):
        raise RuntimeError(
            "Old deployment operations are not bound to its exact release"
        )

    activation = journal.get("host_activation")
    activation_result = (
        activation.get("result") if isinstance(activation, dict) else None
    )
    if (
        not isinstance(activation, dict)
        or activation.get("version") != version
        or activation.get("state") != "complete"
        or not isinstance(activation.get("operation_id"), str)
        or not isinstance(activation_result, dict)
        or activation_result.get("operation_id") != activation.get("operation_id")
        or activation_result.get("status") != "host_activated"
        or activation_result.get("health_commit") != version
        or activation_result.get("version") != version
    ):
        raise RuntimeError("Old journal lacks its exact completed host activation")

    stage = _manual_record(manual_prefix, "stage")
    migration = _manual_record(manual_prefix, "migrate")
    host_switch = _manual_record(manual_prefix, "host-switch-manual")
    api_promotion = _manual_record(manual_prefix, "promote-api-manual")
    final_readback = _manual_record(manual_prefix, "final-readback")
    manual_version = host_switch.get("version")
    if (
        not isinstance(manual_version, str)
        or not SHA.fullmatch(manual_version)
        or not manual_prefix.endswith(manual_version[:8])
        or manual_version == version
        or api_promotion.get("version") != manual_version
        or final_readback.get("target_commit") != manual_version
        or (final_readback.get("deployment") or {}).get("api_version") != manual_version
    ):
        raise RuntimeError("Manual receipt bundle does not identify a later release")

    stage_receipt = _verify_manual_operation(
        stage.get("request"),
        stage.get("receipt"),
        action="stage",
        version=manual_version,
    )
    migration_receipt = _verify_manual_operation(
        migration.get("request"),
        migration.get("receipt"),
        action="migrate",
        version=manual_version,
    )
    manual_manifest = json.loads(
        (RELEASES / manual_version / "nebula-runtime.json").read_text()
    )
    if manual_manifest.get("version") != manual_version or migration_receipt[
        "result"
    ].get("target_head") != manual_manifest.get("migration_head"):
        raise RuntimeError(
            "Manual migration receipt differs from the exact runtime manifest"
        )
    host_readback = host_switch.get("readback")
    package_version = _manual_runtime_package_version(manual_version)
    if (
        host_switch.get("phase") != "complete"
        or not isinstance(host_switch.get("operation_id"), str)
        or host_switch.get("previous_version") == manual_version
        or not isinstance(host_readback, dict)
        or host_readback.get("health_commit") != manual_version
        or host_readback.get("api_version") != manual_version
        or host_readback.get("runtime_package_version") != package_version
        or host_readback.get("ui_commit") != manual_version
        or host_readback.get("owner_changes") != {}
    ):
        raise RuntimeError(
            "Manual host-switch receipt is not an exact completed readback"
        )
    api_steps = api_promotion.get("steps")
    prepare_step = api_steps.get("prepare-api") if isinstance(api_steps, dict) else None
    promote_step = api_steps.get("promote-api") if isinstance(api_steps, dict) else None
    if not isinstance(prepare_step, dict) or not isinstance(promote_step, dict):
        raise RuntimeError("Manual API promotion receipt is incomplete")
    prepare_receipt = _verify_manual_operation(
        prepare_step.get("request"),
        prepare_step.get("receipt"),
        action="prepare_api",
        version=manual_version,
    )
    promote_receipt = _verify_manual_operation(
        promote_step.get("request"),
        promote_step.get("receipt"),
        action="promote_api",
        version=manual_version,
    )
    prepared_runtime_id = (prepare_receipt.get("result") or {}).get("runtime_id")
    if (
        prepared_runtime_id != api_promotion.get("prepared_runtime_id")
        or (promote_step.get("request", {}).get("parameters") or {}).get("runtime_id")
        != prepared_runtime_id
        or (api_promotion.get("promoted_readback") or {}).get("api_version")
        != manual_version
    ):
        raise RuntimeError("Manual API promotion is not bound to its prepared runtime")
    final_health = final_readback.get("health")
    if (
        not isinstance(final_health, dict)
        or final_health.get("status") != "ok"
        or final_health.get("database") != "ok"
        or final_health.get("commit") != manual_version
        or (final_readback.get("deployment") or {}).get("health") != "ready"
    ):
        raise RuntimeError("Manual final readback did not record a ready release")

    if unit_state(SERVICE) != "active" or current_release_version() != manual_version:
        raise RuntimeError("Current supervisor host differs from the manual release")
    identity = host_connection()
    package_version = _manual_runtime_package_version(manual_version)
    service_identity = supervisor_service_identity()
    if identity is None or identity.get("package_version") != package_version:
        raise RuntimeError("Current supervisor process does not match the manual host")
    executable, executable_sha256 = require_manual_runtime_process_in_service(
        service_identity, identity, manual_version
    )
    health, status = read_supervisor_health()
    if not isinstance(health, dict) or not isinstance(status, dict):
        raise RuntimeError("Current authenticated Core health readback is malformed")
    health_status = status.get("health")
    if (
        health.get("status") != "ok"
        or health.get("database") != "ok"
        or health.get("commit") != manual_version
        or health.get("recovery_version") != package_version
        or status.get("protocol") != "nebula.runtime/v1"
        or status.get("api_version") != manual_version
        or health_status not in {"ready", "degraded"}
        or (health_status == "degraded" and not allow_degraded_baseline)
        or (health_status == "ready" and allow_degraded_baseline)
    ):
        raise RuntimeError(
            "Current authenticated Core health does not match the manual release"
        )
    target = status.get("target_execution_version")
    residents = status.get("resident_versions")
    projects = status.get("projects")
    processes = status.get("processes")
    old_package_version = runtime_package_version(version)
    superseded_identities = {version, old_package_version}
    if (
        not isinstance(target, str)
        or not SHA.fullmatch(target)
        or target in superseded_identities
        or not isinstance(residents, list)
        or any(
            not isinstance(resident, str) or resident in superseded_identities
            for resident in residents
        )
        or not isinstance(projects, list)
        or any(
            not isinstance(project, dict)
            or "runtime_version" not in project
            or project["runtime_version"] is not None
            and not isinstance(project["runtime_version"], str)
            or project.get("runtime_version") in superseded_identities
            for project in projects
        )
        or not isinstance(processes, list)
        or any(
            not isinstance(process, dict)
            or not isinstance(process.get("version"), str)
            or process.get("version") in superseded_identities
            or "runtime_version" in process
            and process.get("runtime_version") is not None
            and not isinstance(process.get("runtime_version"), str)
            or process.get("runtime_version") in superseded_identities
            for process in processes
        )
    ):
        raise RuntimeError("Live Core still owns or runs the superseded release")

    if len(history) >= DEPLOYMENT_HISTORY_LIMIT:
        raise RuntimeError(
            "Deployment history archive is full; retain existing records"
        )
    reconciliation = {
        "id": deployment_id,
        "state": "reconciled_superseded",
        "classification": "superseded_by_manual_deployment",
        "reconciled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "manual_receipt_prefix": manual_prefix,
        "manual_release": manual_version,
        "journal": deepcopy(journal),
        "core_receipts": core_receipts,
        "manual_receipts": {
            "stage": stage_receipt,
            "migrate": migration_receipt,
            "host_switch": host_switch,
            "prepare_api": prepare_receipt,
            "promote_api": promote_receipt,
            "final_readback": final_readback,
        },
        "live_readback": {
            "current_release": manual_version,
            "health": health,
            "deployment_status": status,
            "deployment_health": health_status,
            "degraded_baseline_accepted": health_status == "degraded",
            "host_process": identity,
            "supervisor_service": service_identity,
            "host_executable": {
                "path": str(executable),
                "sha256": executable_sha256,
                "identity_source": "current_manual_release_and_live_process_image",
            },
        },
        "uncertain_effects_preserved": [
            step
            for step, receipt in core_receipts.items()
            if receipt.get("state") == "uncertain"
        ],
    }
    if dry_run:
        return {
            "status": "dry_run_validated",
            "proposed": {
                "id": deployment_id,
                "classification": reconciliation["classification"],
                "manual_receipt_prefix": manual_prefix,
                "manual_release": manual_version,
                "uncertain_effects_preserved": reconciliation[
                    "uncertain_effects_preserved"
                ],
                "core_receipts": {
                    step: {
                        "operation_id": receipt["operation_id"],
                        "action": receipt["action"],
                        "state": receipt["state"],
                    }
                    for step, receipt in core_receipts.items()
                },
                "manual_receipt_operations": {
                    "stage": stage_receipt["operation_id"],
                    "migrate": migration_receipt["operation_id"],
                    "prepare_api": prepare_receipt["operation_id"],
                    "promote_api": promote_receipt["operation_id"],
                },
                "live_readback": reconciliation["live_readback"],
                "state_alignment": {
                    "active": manual_version,
                    "host_active": manual_version,
                    "canonical_runtime_version": target,
                    "remove_runtime_deployment": deployment_id,
                },
            },
        }
    history.append(reconciliation)
    record["runtime_deployment_history"] = history
    record.pop("runtime_deployment", None)
    record["deployment_mode"] = "supervisor"
    record["active"] = manual_version
    record["host_active"] = manual_version
    record["previous"] = host_switch["previous_version"]
    record["runtime_api_version"] = manual_version
    record["canonical_runtime_version"] = target
    record["last_health"] = {
        "status": health.get("status"),
        "database": health.get("database"),
        "commit": manual_version,
        "deployment_health": health_status,
        "degraded_baseline_accepted": health_status == "degraded",
    }
    atomic_json(STATE, record)
    return {"status": "reconciled_superseded", "deployment": reconciliation}


def reconcile_unactivated_supersession(
    deployment_id: str, *, dry_run: bool = False
) -> dict[str, Any]:
    """Archive an unactivated rollback of a promotion superseded by a manual host.

    Both recorded Core stages and the promotion migration remain real effects.
    This path only clears deployer intent after exact receipts and the current
    manual host/API/process inventory prove neither candidate was activated.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", deployment_id):
        raise ValueError("deployment ID must be a 32-character journal identity")
    record = state()
    history = record.get("runtime_deployment_history", [])
    if not isinstance(history, list):
        raise RuntimeError("Deployment history archive is malformed")
    prior = next(
        (
            item
            for item in history
            if isinstance(item, dict) and item.get("id") == deployment_id
        ),
        None,
    )
    if prior is not None:
        if prior.get("classification") != "superseded_unactivated_deployment":
            raise RuntimeError("Archived deployment has another reconciliation outcome")
        return {"status": "already_reconciled", "deployment": prior}

    rollback = record.get("runtime_deployment")
    if (
        not isinstance(rollback, dict)
        or rollback.get("id") != deployment_id
        or rollback.get("action") != "rollback"
        or rollback.get("state") != "running"
    ):
        raise RuntimeError("No matching unresolved rollback journal exists")
    promotion = rollback.get("superseded")
    active = record.get("active")
    old = rollback.get("version")
    candidate = promotion.get("version") if isinstance(promotion, dict) else None
    if (
        not all(
            isinstance(value, str) and SHA.fullmatch(value)
            for value in (active, old, candidate)
        )
        or len({active, old, candidate}) != 3
        or rollback.get("previous") != active
        or not isinstance(promotion, dict)
        or promotion.get("action") != "promote"
        or promotion.get("state") != "running"
        or promotion.get("previous") != old
        or not isinstance(promotion.get("id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", promotion["id"])
        or record.get("pending") != candidate
        or record.get("promoting") is not None
        or not isinstance(rollback.get("steps"), dict)
        or not isinstance(promotion.get("steps"), dict)
        or set(rollback.get("steps", {})) != {"stage"}
        or set(promotion.get("steps", {})) != {"stage", "migrate"}
    ):
        raise RuntimeError(
            "Deployment journals do not match an unactivated supersession"
        )
    # Accepted releases and reconciled manual releases have separate immutable
    # identity checks; runtime_release verifies the applicable one.
    runtime_release(active)

    for journal in (promotion, rollback):
        activation = journal.get("host_activation")
        operation_id = journal["id"] + ":host-activation"
        if not isinstance(activation, dict) or activation != {
            "operation_id": operation_id,
            "state": "requested",
            "version": journal["version"],
        }:
            raise RuntimeError("Candidate host activation is not only requested")
        top_level = record.get("host_activation")
        if (
            isinstance(top_level, dict)
            and top_level.get("operation_id") == operation_id
        ):
            raise RuntimeError("Candidate has a host activation operation in progress")

    receipts: dict[str, dict[str, Any]] = {}
    for name, journal in (("promotion", promotion), ("rollback", rollback)):
        version = journal["version"]
        entry, receipt = _require_recorded_operation(
            journal, step="stage", action="stage", expected_state="succeeded"
        )
        release, manifest_sha256 = runtime_release(version)
        if (
            entry["parameters"]
            != {"source": str(release), "manifest_sha256": manifest_sha256}
            or receipt["result"].get("version") != version
            or receipt["result"].get("manifest_sha256") != manifest_sha256
        ):
            raise RuntimeError(f"{name} stage is not bound to its exact release")
        receipts[name + ":stage"] = receipt
    migration_entry, migration_receipt = _require_recorded_operation(
        promotion, step="migrate", action="migrate", expected_state="succeeded"
    )
    migration = migration_status(candidate)
    if (
        migration_entry["parameters"].get("version") != candidate
        or type(migration_entry["parameters"].get("expected_revision")) is not int
        or migration_receipt["result"].get("version") != candidate
        or migration_receipt["result"].get("target_head") != migration["target_head"]
        or migration["current_head"] != migration["target_head"]
    ):
        raise RuntimeError("Promotion migration is not the exact current database head")
    receipts["promotion:migrate"] = migration_receipt

    if unit_state(SERVICE) != "active" or current_release_version() != active:
        raise RuntimeError(
            "Current supervisor host differs from the recorded active release"
        )
    identity = host_connection()
    service_identity = supervisor_service_identity()
    if identity is None or identity.get("package_version") != runtime_package_version(
        active
    ):
        raise RuntimeError("Current supervisor process differs from the active release")
    require_runtime_process_in_service(service_identity, identity, active)
    health, status = read_supervisor_health()
    forbidden = {
        old,
        candidate,
        runtime_package_version(old),
        runtime_package_version(candidate),
    }
    projects = status.get("projects") if isinstance(status, dict) else None
    processes = status.get("processes") if isinstance(status, dict) else None
    residents = status.get("resident_versions") if isinstance(status, dict) else None
    if (
        not isinstance(health, dict)
        or health.get("status") != "ok"
        or health.get("database") != "ok"
        or health.get("commit") != active
        or not isinstance(status, dict)
        or status.get("protocol") != "nebula.runtime/v1"
        or status.get("health") != "ready"
        or status.get("api_version") != active
        or not isinstance(status.get("target_execution_version"), str)
        or status.get("target_execution_version") in forbidden
        or not isinstance(residents, list)
        or any(
            not isinstance(version, str) or version in forbidden
            for version in residents
        )
        or not isinstance(projects, list)
        or any(
            not isinstance(row, dict)
            or "runtime_version" not in row
            or row["runtime_version"] is not None
            and not isinstance(row["runtime_version"], str)
            or row.get("runtime_version") in forbidden
            for row in projects
        )
        or not isinstance(processes, list)
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("version"), str)
            or row.get("version") in forbidden
            or row.get("runtime_version") is not None
            and not isinstance(row["runtime_version"], str)
            or row.get("runtime_version") in forbidden
            for row in processes
        )
    ):
        raise RuntimeError(
            "Live Core still runs a candidate or does not match the active release"
        )
    if len(history) >= DEPLOYMENT_HISTORY_LIMIT:
        raise RuntimeError("Deployment history archive is full")

    archived = {
        "id": deployment_id,
        "state": "reconciled_superseded",
        "classification": "superseded_unactivated_deployment",
        "reconciled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "journal": deepcopy(rollback),
        "core_receipts": receipts,
        "migration_status": migration,
        "live_readback": {
            "health": health,
            "deployment_status": status,
            "host_process": identity,
            "supervisor_service": service_identity,
        },
    }
    if dry_run:
        return {
            "status": "dry_run_validated",
            "proposed": {
                "classification": archived["classification"],
                "rollback_id": deployment_id,
                "promotion_id": promotion["id"],
                "active": active,
                "clear_pending": candidate,
                "core_receipts": {
                    key: {
                        "operation_id": value["operation_id"],
                        "state": value["state"],
                    }
                    for key, value in receipts.items()
                },
                "live_readback": {
                    "host_commit": health["commit"],
                    "api_version": status["api_version"],
                    "deployment_health": status["health"],
                    "revision": status.get("revision"),
                    "target_execution_version": status["target_execution_version"],
                    "resident_versions": residents,
                    "owned_projects": sum(
                        row.get("runtime_id") is not None for row in projects
                    ),
                    "migration_head": migration["current_head"],
                },
            },
        }
    # Re-read live identity before changing the journal; the CLI holds the
    # deploy lock, and an external host switch must fail this final check.
    latest_health, latest_status = read_supervisor_health()
    if (
        current_release_version() != active
        or unit_state(SERVICE) != "active"
        or not isinstance(latest_health, dict)
        or not isinstance(latest_status, dict)
        or latest_health.get("commit") != active
        or latest_status.get("api_version") != active
        or latest_status.get("revision") != status.get("revision")
        or supervisor_service_identity() != service_identity
    ):
        raise RuntimeError("Live Core changed during reconciliation")
    record["runtime_deployment_history"] = [*history, archived]
    record.pop("runtime_deployment", None)
    record["pending"] = None
    record["host_active"] = active
    record["runtime_api_version"] = active
    record["canonical_runtime_version"] = status["target_execution_version"]
    atomic_json(STATE, record)
    return {"status": "reconciled_superseded", "deployment": archived}


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
            expected_commits=first_parent_commits(REPO, previous=previous, sha=sha),
            directory=REPO,
        )
    return release


def api() -> CoreClient:
    return CoreClient("http://127.0.0.1:8000", service_token(TOKEN_FILE))


def service(action: str) -> None:
    if action not in {"start", "stop", "reset-failed"}:
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
    release, _ = runtime_release(version)
    accepted_executable = release / "dist/nebula-core"
    expected_digest = release_core_sha256(version)
    if (
        accepted_executable.is_symlink()
        or sha256(accepted_executable) != expected_digest
    ):
        raise RuntimeError("Accepted supervisor executable is not hash-bound")
    _require_process_identity_in_service(
        service_identity,
        runtime_identity,
        accepted_executable.resolve(strict=True),
        expected_digest,
        # A reconciled manual release has no build receipt; compare the live
        # image as its reconciliation did.
        compare_live_image=_manual_baseline_release(version) is not None,
    )


def require_manual_runtime_process_in_service(
    service_identity: dict[str, Any], runtime_identity: dict[str, Any], version: str
) -> tuple[Path, str]:
    """Bind manual Core to its exact current release and live process image.

    Manual releases do not have managed simulation-acceptance receipts. Their
    current executable is therefore checked against the immutable release path
    and the bytes exposed by each live /proc/<pid>/exe link.
    """
    release = RELEASES / version
    if (
        release.is_symlink()
        or release.name != version
        or release.resolve(strict=True).parent != RELEASES.resolve()
        or (release / "dist").is_symlink()
    ):
        raise RuntimeError("Manual supervisor release path is unsafe")
    executable = release / "dist/nebula-core"
    if (
        executable.is_symlink()
        or executable.resolve(strict=True).parent
        != (release / "dist").resolve(strict=True)
        or not executable.is_file()
    ):
        raise RuntimeError("Manual supervisor executable path is unsafe")
    expected_digest = sha256(executable)
    if not SHA256.fullmatch(expected_digest):
        raise RuntimeError("Manual supervisor executable digest is invalid")
    _require_process_identity_in_service(
        service_identity,
        runtime_identity,
        executable.resolve(strict=True),
        expected_digest,
        compare_live_image=True,
    )
    return executable.resolve(strict=True), expected_digest


def _require_process_identity_in_service(
    service_identity: dict[str, Any],
    runtime_identity: dict[str, Any],
    accepted_executable: Path,
    expected_digest: str,
    *,
    compare_live_image: bool,
) -> None:
    """Apply the common unit ancestry, process birth, UID, and image checks."""
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
                "Supervisor process does not match the expected executable"
            )
        if compare_live_image:
            _verify_live_process_executable(pid, accepted_executable, expected_digest)

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
    if compare_live_image:
        for pid in {runtime_pid, main_pid}:
            _verify_live_process_executable(pid, accepted_executable, expected_digest)
    latest_service = supervisor_service_identity()
    if (
        latest_service.get("main_pid") != main_pid
        or latest_service.get("main_process_birth") != main_birth
        or latest_service.get("exec_start") != service_identity.get("exec_start")
    ):
        raise RuntimeError("Supervisor unit identity changed during validation")


def _verify_live_process_executable(
    pid: int, expected_path: Path, expected_digest: str
) -> None:
    """Compare the live process image with the current release file by path and bytes."""
    live_image = PROC_ROOT / str(pid) / "exe"
    try:
        live_path = live_image.resolve(strict=True)
        live_stat = live_image.stat()
        release_stat = expected_path.stat()
        live_digest = sha256(live_image)
    except OSError as error:
        raise RuntimeError(
            "Manual supervisor live executable could not be verified"
        ) from error
    if (
        live_path != expected_path
        or live_stat.st_dev != release_stat.st_dev
        or live_stat.st_ino != release_stat.st_ino
        or live_digest != expected_digest
    ):
        raise RuntimeError(
            "Manual supervisor process image differs from its current release"
        )


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
            or allow_degraded
            and deployment_health == "degraded"
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
            or (
                runtime_id is not None
                and (not isinstance(runtime_id, str) or not runtime_id)
            )
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


class DeadOwnerRecoveryPending(RuntimeError):
    """The exact accepted dead owner has moved but recovery is not verified yet."""


def _exact_uuid(value: Any) -> bool:
    from uuid import UUID

    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _readonly_postgres_rows(query: str) -> list[str]:
    settings = configured_postgres(TOKEN_FILE)
    if settings is None:
        raise RuntimeError(
            "Dead-worker acceptance requires the configured PostgreSQL deployment"
        )
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("PG")
    }
    if "PGPASSFILE" in os.environ:
        environment["PGPASSFILE"] = os.environ["PGPASSFILE"]
    result = subprocess.run(
        [
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--set=ON_ERROR_STOP=1",
            "--command",
            "BEGIN READ ONLY; " + query + "; COMMIT;",
        ],
        env={**environment, **settings},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Dead-worker readback failed; inspect PostgreSQL access")
    return [
        line
        for line in result.stdout.splitlines()
        if line not in {"", "BEGIN", "COMMIT"}
    ]


def load_dead_owner_acceptance(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Dead-worker acceptance must be a regular file")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != {
        "project_id",
        "runtime_id",
        "fencing_token",
        "turn_ids",
        "failed_turn_ids",
        "effect_call_ids",
        "acceptance",
    }:
        raise RuntimeError("Dead-worker acceptance has an invalid shape")
    turns, failed, effects = (
        value["turn_ids"],
        value["failed_turn_ids"],
        value["effect_call_ids"],
    )
    if (
        not _exact_uuid(value["project_id"])
        or not _exact_uuid(value["runtime_id"])
        or type(value["fencing_token"]) is not int
        or value["fencing_token"] < 1
        or not isinstance(turns, list)
        or not turns
        or not all(_exact_uuid(item) for item in turns)
        or len(turns) != len(set(turns))
        or not isinstance(failed, list)
        or not failed
        or not all(_exact_uuid(item) for item in failed)
        or len(failed) != len(set(failed))
        or not isinstance(effects, list)
        or not effects
        or not all(_exact_uuid(item) for item in effects)
        or len(effects) != len(set(effects))
        or value["acceptance"] != "accept unconfirmed effects without replay"
    ):
        raise RuntimeError("Dead-worker acceptance has invalid exact identities")
    return value


def prove_dead_owner(accepted: dict[str, Any]) -> dict[str, Any]:
    runtime_id = accepted["runtime_id"]
    rows = _readonly_postgres_rows(
        "SELECT pid, process_birth, role, state FROM runtime_processes "
        f"WHERE runtime_id = '{runtime_id}'"
    )
    if len(rows) != 1:
        raise RuntimeError("The accepted worker has no exact process record")
    fields = rows[0].split("|")
    if len(fields) != 4 or fields[2] != "execution" or fields[3] == "retired":
        raise RuntimeError("The accepted worker is not a resident execution process")
    pid = int(fields[0])
    birth = fields[1]
    if not birth.startswith("linux:") or linux_process_birth(pid) == birth:
        raise RuntimeError("The accepted worker process has not proven to exit")
    failed = ",".join(f"'{item}'" for item in accepted["failed_turn_ids"])
    failed_turns = _readonly_postgres_rows(
        "SELECT id FROM entities "
        f"WHERE engagement_id = '{accepted['project_id']}' AND kind = 'chat_turns' "
        "AND payload->>'backend' = 'provider' AND payload->>'status' = 'failed' "
        "AND payload->>'execution_claim_id' IS NOT NULL"
    )
    if set(failed_turns) != set(accepted["failed_turn_ids"]):
        raise RuntimeError("The accepted failed turn claims changed")
    effects = _readonly_postgres_rows(
        "SELECT id, payload->>'status', payload->>'risk_class' FROM entities "
        f"WHERE engagement_id = '{accepted['project_id']}' AND kind = 'tool_calls' "
        f"AND payload->>'chat_turn_id' IN ({failed}) "
        "AND payload->>'status' NOT IN ('complete','denied','cancelled') "
        "AND NOT (payload->>'status' = 'failed' AND payload->>'risk_class' = 'local_read')"
    )
    observed = {
        parts[0]
        for row in effects
        if len(parts := row.split("|")) == 3
        and parts[1] == "failed"
        and parts[2] == "workspace_write"
    }
    if observed != set(accepted["effect_call_ids"]):
        raise RuntimeError("The accepted failed write-call identities changed")
    return {"pid": pid, "process_birth": birth}


def verified_dead_owner_recovery(
    accepted: dict[str, Any], current: dict[str, Any]
) -> bool:
    if not _exact_uuid(current["runtime_id"]):
        return False
    rows = _readonly_postgres_rows(
        "SELECT operation_id FROM runtime_recoveries WHERE "
        f"project_id = '{accepted['project_id']}' AND "
        f"source_runtime_id = '{accepted['runtime_id']}' AND "
        f"source_fencing_token = {accepted['fencing_token']} AND "
        f"target_runtime_id = '{current['runtime_id']}' AND "
        f"target_fencing_token = {current['fencing_token']} AND phase = 'verified'"
    )
    return len(rows) == 1


def require_preserved_workers(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    admitted_version: str | None = None,
    accepted_dead_owner: dict[str, Any] | None = None,
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
            accepted_transition = (
                accepted_dead_owner is not None
                and project_id == accepted_dead_owner["project_id"]
                and previous["runtime_id"] == accepted_dead_owner["runtime_id"]
                and previous["fencing_token"] == accepted_dead_owner["fencing_token"]
                and current["fencing_token"] > previous["fencing_token"]
                and current["runtime_version"] == admitted_version
            )
            if accepted_transition:
                if verified_dead_owner_recovery(accepted_dead_owner, current):
                    continue
                raise DeadOwnerRecoveryPending(
                    f"Accepted dead owner recovery is not verified for project {project_id}"
                )
            raise RuntimeError(
                f"Host restart changed the existing execution owner for project {project_id}"
            )
        if previous["runtime_id"] is not None:
            retained += 1
    previous_residents = set(before["resident_versions"])
    current_residents = set(after["resident_versions"])
    if (
        not previous_residents <= current_residents
        or current_residents
        - previous_residents
        - ({admitted_version} if admitted_version is not None else set())
    ):
        raise RuntimeError(
            "Host restart changed resident execution versions unexpectedly"
        )
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
    migration_readback = (
        (migrate.get("settled") or {}).get("result", {})
        if isinstance(migrate, dict)
        else {}
    )
    migration_confirmed = (
        migration_readback.get("version") == version
        and isinstance(migration_readback.get("target_head"), str)
        and migration_readback.get("target_head")
        == migration_readback.get("current_head")
    )
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
                or (
                    migrate["receipt"].get("state") != "succeeded"
                    and not migration_confirmed
                )
            )
        )
    ):
        raise RuntimeError(
            "Host activation requires verified stage and additive-migration receipts"
        )
    return {
        "deployment_id": journal.get("id"),
        "stage_operation_id": stage.get("operation_id"),
        "migration_operation_id": migrate.get("operation_id")
        if isinstance(migrate, dict)
        else None,
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


def confirm_api_read(client: CoreClient | None = None) -> None:
    """Read one authenticated page through the promoted API worker.

    Health alone can be served by the supervisor while the API worker is dead
    or hung; readiness requires the API itself to answer.
    """
    page = (client or api()).get("engagements", offset=0, limit=1)
    if not isinstance(page, list):
        raise ValueError("Core API read returned an invalid project page")


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
    restart_exited: bool = False,
    accepted_dead_owner: dict[str, Any] | None = None,
    timeout: int = 480,
) -> dict[str, Any]:
    expected_package_version = runtime_package_version(version)
    deadline = time.monotonic() + timeout
    last_problem = "supervisor did not publish its accepted host identity"
    starts = 1
    while time.monotonic() < deadline:
        try:
            unit = unit_state(SERVICE)
            if unit != "active":
                if restart_exited and unit in {"inactive", "failed"}:
                    if starts >= HOST_START_ATTEMPTS:
                        raise RuntimeError(
                            f"Supervisor host {version} exited before readiness after {starts} starts"
                        )
                    time.sleep(HOST_RESTART_DELAY_SECONDS)
                    if unit == "failed":
                        service("reset-failed")
                    service("start")
                    starts += 1
                    continue
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
            confirm_api_read()
            if (
                health.get("commit") != version
                or health.get("recovery_version") != expected_package_version
            ):
                raise RuntimeError(
                    "Host health does not report the accepted Core and recovery identities"
                )
            if status.get("health") == "degraded" and not (
                allow_degraded_workers and degraded_baseline
            ):
                # The replaced API worker reports degraded while it drains;
                # wait for ready within this bound instead of failing the run.
                last_problem = (
                    "deployment status is degraded: "
                    + str(status.get("detail", ""))[:300]
                )
                time.sleep(2)
                continue
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
                    prior_status,
                    snapshot,
                    admitted_version=version,
                    accepted_dead_owner=accepted_dead_owner,
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
                "host_starts": starts,
            }
        except (OSError, ValueError, HTTPError, DeadOwnerRecoveryPending) as error:
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


def chat_turns_at_restart_boundary(client: CoreClient) -> list[str]:
    """Return projects with a chat turn queued, admitting or running."""
    return list(_restart_boundary_activity(client))


def _restart_boundary_activity(client: CoreClient) -> dict[str, dict[str, Any]]:
    """Map each project with unfinished chat work to its name and turn IDs."""
    busy: dict[str, dict[str, Any]] = {}
    for project in client.all_entities("engagements"):
        project_id = project.get("id")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("Core returned a project without an ID")
        activity = client.get("chat/session-activity", engagement_id=project_id)
        if not isinstance(activity, list):
            raise ValueError("Core chat activity has an invalid shape")
        pending = [
            item
            for item in activity
            if not isinstance(item, dict) or item.get("state") != "idle"
        ]
        if pending:
            name = project.get("name")
            busy[project_id] = {
                "name": name if isinstance(name, str) else "",
                "turns": sorted(
                    {
                        item["turn_id"]
                        for item in pending
                        if isinstance(item, dict)
                        and isinstance(item.get("turn_id"), str)
                    }
                ),
                "unidentified_turns": sum(
                    1
                    for item in pending
                    if not isinstance(item, dict)
                    or not isinstance(item.get("turn_id"), str)
                    or not item.get("turn_id")
                ),
            }
    return busy


def _unavailable_turn_owners(
    client: CoreClient, busy: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Busy projects whose execution owner cannot advance their turns.

    Core recovers a project by itself only after its owner's process has
    exited. An owner whose lease lapsed while its process stays alive (frozen
    or wedged), or whose application failed while its process keeps renewing
    the lease, keeps its turns unfinished for as long as that lasts. Ownerless
    projects are not listed: admission starts a worker for their work.
    """
    status = client.get("deployment/status")
    if not isinstance(status, dict):
        raise ValueError("Core deployment status has an invalid shape")
    projects = status.get("projects")
    processes = status.get("processes")
    if not isinstance(projects, list) or not isinstance(processes, list):
        raise ValueError("Core deployment status has an invalid shape")
    by_project = {
        row.get("project_id"): row for row in projects if isinstance(row, dict)
    }
    by_runtime = {
        row.get("runtime_id"): row for row in processes if isinstance(row, dict)
    }
    stuck: dict[str, dict[str, Any]] = {}
    for project_id in busy:
        row = by_project.get(project_id)
        if row is None:
            continue
        runtime_id = row.get("runtime_id")
        if not isinstance(runtime_id, str) or not runtime_id:
            continue
        process = by_runtime.get(runtime_id)
        blockers = row.get("blockers")
        failures = [
            str(item)
            for item in (blockers if isinstance(blockers, list) else [])
            if str(item).startswith(f"Execution worker {runtime_id} failed")
        ]
        if failures:
            reason = failures[0]
        elif process is None:
            reason = "Core no longer lists this worker as a resident process"
        elif process.get("ready_verified") is not True:
            reason = (
                "its heartbeat lease lapsed while Core lists its process as "
                f"{process.get('state')}, so it is not answering; Core recovers "
                "the project only after that process exits"
            )
        else:
            continue
        stuck[project_id] = {
            "runtime_id": runtime_id,
            "version": row.get("runtime_version"),
            "reason": reason,
        }
    return stuck


def _unavailable_owner_blocker(
    busy: dict[str, dict[str, Any]], stuck: dict[str, dict[str, Any]]
) -> str:
    parts = []
    for project_id, owner in list(stuck.items())[:10]:
        name = busy[project_id]["name"]
        label = f'Project "{name}" ({project_id})' if name else f"Project {project_id}"
        turns = ", ".join(busy[project_id]["turns"][:5]) or "unknown"
        parts.append(
            f"{label}: chat turn(s) {turns} belong to execution worker "
            f"{owner['runtime_id']} (version {owner['version']}), which cannot "
            f"advance them: {owner['reason']}"
        )
    return (
        f"Chat turns in {len(stuck)} project(s) are held by an unavailable "
        "execution worker and cannot settle by waiting, so the supervisor was "
        "not stopped. "
        + "; ".join(parts)
        + ". End or recover that worker (deployment status names it), or stop "
        "the turns once the project is recovered, then rerun the deployment"
    )


def _includes_host_reconnect(version: str) -> bool:
    try:
        run(
            "git",
            "-C",
            str(REPO),
            "merge-base",
            "--is-ancestor",
            HOST_RECONNECT_COMMIT,
            version,
        )
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return False
    return True


def _versions_reconnecting_to_host(client: CoreClient) -> list[str] | None:
    """Every API and execution version, when each one reconnects to the host.

    Returns None, so the caller waits for chat work to settle, when any
    version is unknown, not an exact commit, or predates #1023.
    """
    status = client.get("deployment/status")
    if not isinstance(status, dict):
        return None
    versions: set[Any] = {status.get("api_version")}
    residents = status.get("resident_versions")
    projects = status.get("projects")
    if not isinstance(residents, list) or not isinstance(projects, list):
        return None
    versions.update(residents)
    for project in projects:
        if not isinstance(project, dict):
            return None
        if project.get("runtime_version") is not None:
            versions.add(project.get("runtime_version"))
    if not all(isinstance(item, str) and SHA.fullmatch(item) for item in versions):
        return None
    if not all(_includes_host_reconnect(item) for item in versions):
        return None
    return sorted(versions)


def core_write(path: str, payload: dict, *, idempotency_key: str) -> Any:
    """POST an ordinary Core write; the key makes a retried request exact."""
    client = api()
    request = Request(
        f"{client.origin}/api/v1/{path}",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {client.token}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def holds_state_path() -> Path:
    # Separate from deployer-state.json: promotion saves its own copy of that
    # record, which would overwrite a hold saved in between.
    return STATE.with_name("project-holds.json")


def _saved_holds() -> dict[str, Any]:
    path = holds_state_path()
    if not path.exists():
        return {}
    holds = json.loads(path.read_text())
    if not isinstance(holds, dict):
        raise RuntimeError(
            "Saved project holds are malformed; inspect them before releasing"
        )
    return holds


def _save_holds(holds: dict[str, Any]) -> None:
    if holds:
        atomic_json(holds_state_path(), holds)
    else:
        holds_state_path().unlink(missing_ok=True)


def _unrouted(error: HTTPError) -> bool:
    """Whether a 404 is the framework's unknown route, not an unknown project."""
    try:
        body = json.loads(error.read() or b"{}")
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("detail") == "Not Found"


def place_project_hold(
    project_id: str, *, deployment_id: str, reason: str, grace_seconds: int
) -> str | None:
    """Hold one project's chat work; None when Core predates project holds.

    The hold is saved in deployer state before this returns, so a later
    ``release-holds`` releases it even if this run stops.
    """
    try:
        view = core_write(
            f"engagements/{quote(project_id, safe='')}/hold",
            {
                "reason": reason,
                "deployment_id": deployment_id,
                "grace_seconds": grace_seconds,
            },
            idempotency_key=f"project-hold:{deployment_id}:{project_id}",
        )
    except HTTPError as error:
        if error.code == 405 or (error.code == 404 and _unrouted(error)):
            # The route itself is missing: this Core predates project holds.
            return None
        raise
    hold = view.get("hold") if isinstance(view, dict) else None
    if not isinstance(hold, dict) or not isinstance(hold.get("id"), str):
        raise RuntimeError(f"Core returned no hold for project {project_id}")
    saved = _saved_holds()
    saved[project_id] = {
        "hold_id": hold["id"],
        "deployment_id": deployment_id,
        "reason": reason,
        "placed_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_holds(saved)
    return hold["id"]


def release_project_holds(
    *, reason: str, deployment_prefix: str | None = None
) -> dict[str, Any]:
    """Release the holds this deployer placed and saved; report each.

    ``deployment_prefix`` limits release to holds whose deployment ID starts
    with it, so a host restart never releases an operator's stop holds.
    """
    saved = _saved_holds()
    released: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for project_id, entry in sorted(saved.items()):
        if deployment_prefix is not None and not (
            isinstance(entry, dict)
            and str(entry.get("deployment_id", "")).startswith(deployment_prefix)
        ):
            continue
        hold_id = entry.get("hold_id") if isinstance(entry, dict) else None
        if not isinstance(hold_id, str):
            failed[project_id] = "saved hold has no ID"
            continue
        try:
            view = core_write(
                f"engagements/{quote(project_id, safe='')}/hold/"
                f"{quote(hold_id, safe='')}/release",
                {"reason": reason},
                idempotency_key=f"project-hold-release:{hold_id}",
            )
        except (HTTPError, OSError, ValueError) as error:
            failed[project_id] = str(error)[:300]
            continue
        hold = view.get("hold") if isinstance(view, dict) else {}
        released[project_id] = {
            "hold_id": hold_id,
            "status": hold.get("status") if isinstance(hold, dict) else None,
            "restored_goals": hold.get("restored_goals", [])
            if isinstance(hold, dict)
            else [],
        }
    _save_holds(
        {
            project_id: entry
            for project_id, entry in _saved_holds().items()
            if project_id not in released
        }
    )
    return {"released": released, "failed": failed}


def hold_projects_until_quiet(
    *,
    deployment_id: str,
    reason: str,
    project_ids: list[str] | None = None,
    grace_seconds: int = PROJECT_HOLD_GRACE_SECONDS,
) -> dict[str, Any] | None:
    """Hold projects until none has chat work; None if Core has no holds.

    ``project_ids`` holds exactly those projects; without it every project
    with chat work is held, and any project that becomes busy while this
    waits is held too. Core stops what is still running after the grace.
    """
    started = time.monotonic()
    deadline = started + grace_seconds + PROJECT_HOLD_WAIT_SECONDS
    held: dict[str, str] = {}
    targets = list(project_ids or [])
    while True:
        busy = _restart_boundary_activity(api())
        for project_id in [*targets, *busy]:
            if project_id in held:
                continue
            hold_id = place_project_hold(
                project_id,
                deployment_id=deployment_id,
                reason=reason,
                grace_seconds=grace_seconds,
            )
            if hold_id is None:
                if held:
                    raise RuntimeError(
                        "Core accepted some project holds and refused another; "
                        "release them with release-holds"
                    )
                return None
            held[project_id] = hold_id
        if not busy:
            return {
                "quiet_after_seconds": round(time.monotonic() - started, 1),
                "holds": held,
            }
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Chat work in {len(busy)} held project(s) did not stop within "
                f"{grace_seconds + PROJECT_HOLD_WAIT_SECONDS}s, so nothing was "
                "stopped or restarted; the holds stay in place until release-holds: "
                + ", ".join(list(busy)[:10])
            )
        time.sleep(PROJECT_HOLD_POLL_SECONDS)


def hold_command(
    project_ids: list[str] | None, reason: str, grace_seconds: int
) -> dict[str, Any]:
    """Hold the named projects, or every live project, until no chat work runs.

    Without ``project_ids`` it holds each project that has an execution
    worker; a project without one runs nothing, and a hold request would only
    start a worker for it. Any project that becomes busy meanwhile is held too.
    """
    if project_ids:
        projects = list(project_ids)
    else:
        status = api().get("deployment/status")
        rows = status.get("projects") if isinstance(status, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("Core deployment status has an invalid shape")
        projects = sorted(
            row["project_id"]
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("project_id"), str)
            and not row["project_id"].startswith("__")
            and row.get("runtime_id")
        )
    held = hold_projects_until_quiet(
        deployment_id=f"operator-stop-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
        reason=reason,
        project_ids=projects,
        grace_seconds=grace_seconds,
    )
    if held is None:
        raise RuntimeError(
            "This Core predates project holds; coordinate the stop with Butler "
            "as AGENTS.md describes for older releases"
        )
    return {"status": "held", **held}


def wait_quiet_restart_boundary(
    accepted_dead_owner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wait, bounded, until no chat turn would cross the host restart.

    Supervisor workers outlive the host, but a worker without #1023 fails a
    turn that reads the host (admission pressure, credentials, the browser
    bridge) while no host is running. When every API and execution version
    includes #1023 there is nothing to protect, so this returns at once and
    running turns cross the restart. Otherwise it waits: the operator gate
    runs before staging and migration; this repeats its chat check at the
    moment of the stop. Reads fail closed. Nothing is stopped when chat work
    does not settle.

    A turn whose execution owner is unavailable does not settle while that
    owner stays unavailable. Once the same owner has stayed unavailable for
    HOST_UNAVAILABLE_OWNER_SECONDS this refuses at once, naming the project,
    its turns, the worker and why the worker cannot advance them.
    """
    started = time.monotonic()
    if accepted_dead_owner is None:
        reconnecting = _versions_reconnecting_to_host(api())
        if reconnecting is not None:
            return {
                "quiet_after_seconds": 0.0,
                "waited": False,
                "reason": (
                    "every API and execution worker version waits out a "
                    "supervisor replacement, so running turns cross it"
                ),
                "reconnecting_versions": reconnecting,
            }
        # Older workers need a quiet boundary. Core holds every busy project
        # instead of waiting for it to settle on its own; the caller releases
        # the holds once the new host is up. A Core without holds falls back
        # to the bounded wait below.
        pending = state().get("pending")
        holds = hold_projects_until_quiet(
            deployment_id=f"{HOST_RESTART_HOLD_PREFIX}{pending or 'unknown'}",
            reason="Core supervisor restart for a deployment",
        )
        if holds is not None:
            return {**holds, "waited": True}
    deadline = started + HOST_QUIET_WAIT_SECONDS
    unavailable_since: dict[tuple[str, str], float] = {}
    while True:
        client = api()
        busy = _restart_boundary_activity(client)
        if not busy:
            quiet_after = round(time.monotonic() - started, 1)
            print(
                f"activate-host: chat turns were quiet after {quiet_after}s",
                file=sys.stderr,
                flush=True,
            )
            return {"quiet_after_seconds": quiet_after}
        stuck = _unavailable_turn_owners(client, busy)
        if accepted_dead_owner is not None:
            project_id = accepted_dead_owner["project_id"]
            if (
                set(busy) == {project_id}
                and set(busy[project_id]["turns"])
                == set(accepted_dead_owner["turn_ids"])
                and busy[project_id].get("unidentified_turns", 0) == 0
                and project_id in stuck
                and stuck[project_id]["runtime_id"] == accepted_dead_owner["runtime_id"]
            ):
                status = client.get("deployment/status")
                owner = next(
                    (
                        item
                        for item in status["projects"]
                        if item.get("project_id") == project_id
                    ),
                    None,
                )
                if (
                    owner is not None
                    and owner.get("fencing_token")
                    == accepted_dead_owner["fencing_token"]
                ):
                    return {
                        "quiet_after_seconds": round(time.monotonic() - started, 1),
                        "accepted_dead_owner": accepted_dead_owner,
                        "dead_process": prove_dead_owner(accepted_dead_owner),
                    }
            raise RuntimeError(
                "Dead-worker acceptance no longer matches the exact busy turns and owner"
            )
        now = time.monotonic()
        unavailable_since = {
            (project_id, owner["runtime_id"]): unavailable_since.get(
                (project_id, owner["runtime_id"]), now
            )
            for project_id, owner in stuck.items()
        }
        lasting = {
            project_id: owner
            for project_id, owner in stuck.items()
            if now - unavailable_since[(project_id, owner["runtime_id"])]
            >= HOST_UNAVAILABLE_OWNER_SECONDS
        }
        if lasting:
            raise RuntimeError(_unavailable_owner_blocker(busy, lasting))
        if now >= deadline:
            if stuck:
                raise RuntimeError(_unavailable_owner_blocker(busy, stuck))
            raise RuntimeError(
                f"Chat turns stayed queued, admitting or running in {len(busy)} "
                "project(s) at the host restart boundary, so the supervisor was "
                "not stopped: " + ", ".join(list(busy)[:10])
            )
        time.sleep(HOST_QUIET_POLL_SECONDS)


def _process_alive(identity: dict[str, Any]) -> bool:
    return linux_process_birth(identity["pid"]) == identity["process_birth"]


def _kill_process(identity: dict[str, Any]) -> bool:
    """SIGKILL exactly this process identity; False when it already exited."""
    pidfd_open = getattr(os, "pidfd_open", None)
    if pidfd_open is None or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError(
            "Verified pidfd signaling is required to stop a hung supervisor"
        )
    try:
        handle = pidfd_open(identity["pid"])
    except ProcessLookupError:
        return False
    try:
        # The descriptor pins the process: a reused PID cannot be signalled.
        if linux_process_birth(identity["pid"]) != identity["process_birth"]:
            return False
        signal.pidfd_send_signal(handle, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(handle)


def stop_supervisor_service(identity: dict[str, Any] | None) -> dict[str, Any]:
    """Stop the foreground unit within a bound and prove its Core process exited.

    ``identity`` is the supervisor process named by private runtime discovery
    (a live pid with its birth), or None when none is running. After
    SUPERVISOR_STOP_GRACE_SECONDS that exact process is killed. This bounds the
    stop of releases that wait forever for open viewer streams, and it also
    ends a supervisor that a KillMode=process stop timeout left running after
    systemd killed only its onefile bootloader. Workers are never signalled. A
    unit left failed by the forced exit is reset so the next start is clean.
    """
    started = time.monotonic()
    outcome: dict[str, Any] = {
        "process": (
            {"pid": identity["pid"], "process_birth": identity["process_birth"]}
            if identity is not None
            else None
        ),
        "forced": False,
    }
    stopped = threading.Event()
    stop_errors: list[Exception] = []

    def enforce_grace() -> None:
        if identity is None:
            return
        deadline = started + SUPERVISOR_STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not _process_alive(identity):
                return
            stopped.wait(0.2)
        try:
            if _kill_process(identity):
                outcome["forced"] = True
                outcome["forced_after_seconds"] = round(time.monotonic() - started, 1)
        except Exception as error:
            stop_errors.append(error)

    watchdog = threading.Thread(
        target=enforce_grace, name="nebula-supervisor-stop-grace", daemon=True
    )
    watchdog.start()
    try:
        if unit_state(SERVICE) not in {"inactive", "failed"}:
            service("stop")
    finally:
        # The watchdog keeps its grace even after the unit stops: an orphaned
        # supervisor must still exit before another one may start.
        watchdog.join()
    if stop_errors:
        raise RuntimeError(
            "The exact supervisor could not be stopped safely"
        ) from stop_errors[0]
    if identity is not None:
        deadline = time.monotonic() + SUPERVISOR_EXIT_SECONDS
        while _process_alive(identity):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "The previous supervisor process did not exit after SIGKILL"
                )
            time.sleep(0.2)
    unit = unit_state(SERVICE)
    if unit == "failed":
        # A forced stop, or a host that crashed, leaves the unit failed. That
        # only records the past exit; the process proof above is what counts.
        service("reset-failed")
        outcome["reset_failed"] = True
    outcome["seconds"] = round(time.monotonic() - started, 1)
    return outcome


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
    persist()
    stop_started = time.monotonic()
    journal["service_stop"] = stop_supervisor_service(host_connection(allow_stale=True))
    require_stopped()
    if current_release_version() != version:
        switch_to(release)
    journal["state"] = "service_stopped_release_selected"
    journal["current_release"] = str(release)
    persist()
    service("start")
    start_issued = time.monotonic()
    journal["state"] = "service_started"
    persist()
    readback = wait_supervisor_host_ready(
        version,
        expected_api_version=expected_api_version,
        prior_status=prior_status,
        allow_degraded_workers=allow_degraded_workers,
        degraded_baseline=prior_status.get("health") == "degraded",
        restart_exited=True,
        accepted_dead_owner=journal.get("accepted_dead_owner"),
    )
    journal["timings"] = {
        "stop_and_switch_seconds": round(start_issued - stop_started, 1),
        "start_to_ready_seconds": round(time.monotonic() - start_issued, 1),
    }
    print(
        "activate-host: stop and switch took "
        f"{journal['timings']['stop_and_switch_seconds']}s, start to ready "
        f"{journal['timings']['start_to_ready_seconds']}s",
        file=sys.stderr,
        flush=True,
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
        allow_degraded=(allow_degraded_workers and prior.get("health") == "degraded"),
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
    accepted_dead_owner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Restart only the foreground supervisor from an accepted staged release.

    Projects held for the restart boundary are released afterwards, whether
    the activation succeeded or rolled back; a release Core does not confirm
    stays saved for ``release-holds``.
    """
    try:
        result = _activate_supervisor_host(
            record,
            version,
            allow_degraded_workers=allow_degraded_workers,
            accepted_dead_owner=accepted_dead_owner,
        )
    except BaseException:
        _release_restart_holds()
        raise
    released = _release_restart_holds()
    if released["released"] or released["failed"]:
        result = {**result, "restart_holds": released}
    return result


def _release_restart_holds() -> dict[str, Any]:
    try:
        return release_project_holds(
            reason="Core supervisor restart finished",
            deployment_prefix=HOST_RESTART_HOLD_PREFIX,
        )
    except Exception as error:  # the holds stay saved for release-holds
        return {"released": {}, "failed": {"*": str(error)[:300]}}


def _activate_supervisor_host(
    record: dict[str, Any],
    version: str,
    *,
    allow_degraded_workers: bool = False,
    accepted_dead_owner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if record.get("deployment_mode") != "supervisor":
        raise RuntimeError("Host activation requires supervisor deployment mode")
    deployment = record.get("runtime_deployment")
    action = deployment.get("action") if isinstance(deployment, dict) else None
    valid_pending = action == "promote" and record.get("pending") == version
    superseded = deployment.get("superseded") if action == "rollback" else None
    valid_rollback = (
        action == "rollback"
        and deployment.get("version") == version
        and (
            record.get("previous") == version
            or isinstance(superseded, dict)
            and superseded.get("action") == "promote"
            and superseded.get("previous") == version
        )
    )
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
        recorded_dead_owner = existing.get("accepted_dead_owner")
        if (
            accepted_dead_owner is not None
            and accepted_dead_owner != recorded_dead_owner
        ):
            raise RuntimeError("A retry cannot change the accepted dead owner")
        accepted_dead_owner = recorded_dead_owner
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
            # A completed activation may be replayed after this deployment's
            # later API promotion. Accept that change only when its successful
            # receipt identifies the API worker prepared for this release.
            steps = deployment.get("steps", {})
            prepared = steps.get("api", {})
            promoted = steps.get("promote-api", {})
            prepared_receipt = prepared.get("receipt") or {}
            promoted_receipt = promoted.get("receipt") or {}
            prepared_result = (
                (prepared.get("settled") or {}).get("result")
                or prepared_receipt.get("result")
                or {}
            )
            prepared_runtime = prepared_result.get("runtime_id")
            own_api_promoted = (
                prepared.get("action") == "prepare_api"
                and (prepared.get("parameters") or {}).get("version") == version
                and (
                    prepared_receipt.get("state") == "succeeded"
                    or prepared.get("settled")
                )
                and isinstance(prepared_runtime, str)
                and promoted.get("action") == "promote_api"
                and (promoted.get("parameters") or {}).get("runtime_id")
                == prepared_runtime
                and (
                    promoted_receipt.get("state") == "succeeded"
                    or promoted.get("settled")
                )
            )
            current = wait_supervisor_host_ready(
                version,
                expected_api_version=version
                if own_api_promoted
                else prior["api_version"],
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
                "current_readback": current,
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
                            accepted_dead_owner=accepted_dead_owner,
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
            existing["restart_boundary"] = wait_quiet_restart_boundary(
                accepted_dead_owner
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
    if valid_rollback and current_release_version() == version:
        # The rollback target is already the selected host: the promotion it
        # replaces never activated its own host, or that activation restored
        # this one. Verify the running host; nothing is restarted.
        _health, status = read_supervisor_health()
        current_api = status.get("api_version")
        if not isinstance(current_api, str):
            raise RuntimeError("A ready supervisor API status is required")
        readback = wait_supervisor_host_ready(
            version,
            expected_api_version=current_api,
            prior_status=None,
            allow_degraded_workers=allow_degraded_workers,
            degraded_baseline=status.get("health") == "degraded",
        )
        return {
            "operation_id": requested_operation_id or uuid4().hex,
            "status": "host_activated",
            "version": version,
            "previous_version": version,
            "already_active": True,
            **readback,
        }
    restart_boundary = wait_quiet_restart_boundary(accepted_dead_owner)
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
        or health.get("recovery_version") != runtime_package_version(previous_version)
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
    if version not in [
        *status.get("staged_versions", []),
        *status.get("resident_versions", []),
    ]:
        raise RuntimeError("Pending host release has not been staged by the supervisor")
    if action == "promote" and prior_status["api_version"] == version:
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
        "restart_boundary": restart_boundary,
        "accepted_dead_owner": accepted_dead_owner,
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
            # A failed start leaves discovery pointing at the stopped host.
            # Treat that stale identity as absent so restoration can proceed.
            current_identity = host_connection(allow_stale=True)
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


def activate_host(
    *,
    allow_degraded_workers: bool = False,
    accepted_dead_owner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = state()
    pending = record.get("pending")
    if not isinstance(pending, str):
        raise RuntimeError(
            "No accepted pending candidate is available for host activation"
        )
    return activate_supervisor_host(
        record,
        pending,
        allow_degraded_workers=allow_degraded_workers,
        accepted_dead_owner=accepted_dead_owner,
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
        or health.get("recovery_version") != runtime_package_version(current_version)
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
            confirm_api_read(client)
            request = Request("http://127.0.0.1:8000/web-build.json")
            with urlopen(request, timeout=10) as response:
                web = json.load(response)
            if web.get("commit") == commit and web.get("dirty") is False:
                return
        except (OSError, ValueError, TimeoutError):
            time.sleep(2)
    raise RuntimeError("Core did not serve its accepted build within 480 seconds")


def operator_database_backup(label: str) -> Path:
    """An operator-requested online PostgreSQL dump before a rolling promotion.

    Supervisor deployments keep live data in place and never restore it, so
    this is a recovery copy only; it is taken only when explicitly requested.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
        raise ValueError("backup label must be a single safe path component")
    postgres = configured_postgres(TOKEN_FILE)
    if postgres is None:
        raise RuntimeError("an online backup needs the configured PostgreSQL database")
    backup = BACKUPS / label
    if backup.exists() or backup.is_symlink():
        raise RuntimeError("operator backup already exists")
    backup.parent.mkdir(parents=True, exist_ok=True)
    staging = BACKUPS / f".{label}.{os.getpid()}.partial"
    staging.mkdir(mode=0o700)
    try:
        snapshot_postgres(staging, postgres)
        os.replace(staging, backup)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return backup


def _restore_previous_code(previous: str) -> None:
    """Code-only legacy rollback: no snapshot was requested, data stays as is."""

    old_release = RELEASES / previous
    if not old_release.is_dir():
        raise RuntimeError("previous immutable release is missing")
    if unit_state(SERVICE) != "inactive":
        service("stop")
        require_stopped()
    switch_to(old_release)
    service("start")
    wait_ready(previous)
    edge("start")


def legacy_promote(*, backup: bool = False) -> dict[str, Any]:
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
    snapshot_path: Path | None = None
    if backup:
        label = f"pre-{new}-{int(time.time())}"
        try:
            snapshot_path = snapshot(label)
        except Exception:
            service("start")
            wait_ready(previous)
            edge("start")
            raise
        record.update({"rollback_backup": str(snapshot_path)})
    else:
        # Without a requested snapshot, an older one must never be restored
        # over newer data, so it is no longer this promotion's rollback target.
        record.pop("rollback_backup", None)
    record.update({"previous": previous, "promoting": new})
    atomic_json(STATE, record)
    try:
        switch_to(release)
        service("start")
        wait_ready(new)
        edge("start")
    except Exception:
        try:
            if snapshot_path is not None:
                rollback(_legacy_attempt=attempt)
            else:
                _restore_previous_code(previous)
                record.update({"promoting": None})
                atomic_json(STATE, record)
        except Exception as recovery:
            raise RuntimeError(
                f"candidate failed; automatic rollback also failed: {recovery}"
            ) from recovery
        raise
    record.update({"active": new, "pending": None, "promoting": None})
    atomic_json(STATE, record)
    if snapshot_path is None:
        return {
            "status": "promoted",
            "commit": new,
            "previous": previous,
            "rollback_backup": None,
            "rollback_retention": {"status": "not_requested"},
        }
    retention = finish_rollback_retention(record, snapshot_path)
    return {
        "status": "promoted"
        if retention["status"] == "complete"
        else "promoted_cleanup_incomplete",
        "commit": new,
        "previous": previous,
        "rollback_backup": str(snapshot_path),
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


DEPLOYMENT_RETRY_ATTEMPTS = 30
DEPLOYMENT_RETRY_MAX_SECONDS = 10


def deployment_post(path: str, payload: dict) -> dict:
    """Submit a deployment operation, honouring the host's Retry-After.

    A stopping or busy supervisor answers 503 or 429 with Retry-After while
    the operation continues under its ID. Submitting the same operation ID
    again is idempotent and returns its saved receipt once one exists.

    A new operation (``deployment/operations`` with its journaled
    ``operation_id``) whose reply is lost, by a 500, 502, 503 or 504, a
    timeout, or a reset or refused connection, is re-sent under the same ID
    with bounded backoff. Core never dispatches a saved ID twice; it answers
    with that operation's receipt, so a lost reply is read back, not applied
    again. If that receipt still says ``running``, it is read until it
    settles, within the same bound. Other POSTs, such as reconciliation, are
    not re-sent.
    """
    operation_id = payload.get("operation_id")
    if path != "deployment/operations" or not isinstance(operation_id, str):
        return _deployment_post_once(path, payload)
    description = f"Deployment operation {operation_id} ({payload.get('action')})"
    lost = False

    def send() -> dict:
        nonlocal lost
        try:
            return _deployment_post_once(path, payload)
        except (OSError, HTTPException) as error:
            if transient_reason(error) is not None:
                lost = True
            raise

    receipt = tolerate_transient(send, description)
    if lost and isinstance(receipt, dict) and receipt.get("state") == "running":
        # The original request is still executing in Core; read its receipt
        # until it settles. A receipt still running past the bound is
        # returned as is, and the next run reads it again.
        receipt = _settled_receipt(operation_id, receipt)
    return receipt


def _settled_receipt(operation_id: str, receipt: dict) -> dict:
    started = time.monotonic()
    delay = TRANSIENT_FIRST_DELAY_SECONDS
    client = api()
    while receipt.get("state") == "running":
        if time.monotonic() - started + delay > TRANSIENT_BUDGET_SECONDS:
            break
        time.sleep(delay)
        delay = min(delay * 2, TRANSIENT_MAX_DELAY_SECONDS)
        observed = client.read("deployment/operations/" + operation_id)
        if not isinstance(observed, dict) or observed.get("operation_id") != (
            operation_id
        ):
            raise RuntimeError(
                "Deployment operation readback identifies another request"
            )
        receipt = observed
    return receipt


def _deployment_post_once(path: str, payload: dict) -> dict:
    """One submission, waiting out only answers that carry Retry-After."""
    client = api()
    body = json.dumps(payload, separators=(",", ":")).encode()
    for attempt in range(DEPLOYMENT_RETRY_ATTEMPTS):
        request = Request(
            f"{client.origin}/api/v1/{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {client.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            retry_after = error.headers.get("Retry-After") if error.headers else None
            if (
                error.code not in {429, 503}
                or retry_after is None
                or not retry_after.isdigit()
                or attempt + 1 == DEPLOYMENT_RETRY_ATTEMPTS
            ):
                raise
            time.sleep(min(int(retry_after), DEPLOYMENT_RETRY_MAX_SECONDS))
    raise RuntimeError("Deployment operation retry budget is exhausted")


def reconciled_manual_release(sha: str) -> dict[str, str] | None:
    """Return the identity a manual supersession reconciliation recorded for ``sha``.

    A manually installed release has no managed acceptance receipt. Once
    ``reconcile-superseded-manual-deployment`` verified it as the live host, its
    archive holds the manifest digest from the authenticated stage receipt and
    the executable digest read from the live process image. Later promotions
    and code rollbacks bind the release to exactly those digests.
    """
    if not SHA.fullmatch(sha):
        return None
    history = state().get("runtime_deployment_history", [])
    if not isinstance(history, list):
        return None
    for item in reversed(history):
        if (
            not isinstance(item, dict)
            or item.get("classification") != "superseded_by_manual_deployment"
            or item.get("state") != "reconciled_superseded"
            or item.get("manual_release") != sha
        ):
            continue
        stage = ((item.get("manual_receipts") or {}).get("stage") or {}).get(
            "result"
        ) or {}
        executable = (item.get("live_readback") or {}).get("host_executable") or {}
        manifest_sha256 = stage.get("manifest_sha256")
        core_sha256 = executable.get("sha256")
        path = executable.get("path")
        if (
            stage.get("version") != sha
            or not isinstance(manifest_sha256, str)
            or not SHA256.fullmatch(manifest_sha256)
            or not isinstance(core_sha256, str)
            or not SHA256.fullmatch(core_sha256)
            or not isinstance(path, str)
        ):
            raise RuntimeError(
                "Reconciled manual release record lacks its exact manifest and executable identity"
            )
        return {
            "manifest_sha256": manifest_sha256,
            "core_sha256": core_sha256,
            "executable": path,
            "deployment_id": str(item.get("id")),
        }
    return None


def _manual_baseline_release(sha: str) -> tuple[Path, dict[str, str]] | None:
    """Verify a reconciled manual release against its recorded digests."""
    release = RELEASES / sha
    if (release / "simulation-acceptance.json").exists() or (
        release / "simulation-acceptance.json"
    ).is_symlink():
        return None
    identity = reconciled_manual_release(sha)
    if identity is None:
        return None
    if (
        release.is_symlink()
        or release.resolve(strict=True).parent != RELEASES.resolve()
        or release.name != sha
        or (release / "dist").is_symlink()
        or (release / "nebula-runtime.json").is_symlink()
    ):
        raise RuntimeError("Reconciled manual release path is unsafe")
    executable = release / "dist/nebula-core"
    if (
        executable.is_symlink()
        or str(executable.resolve(strict=True)) != identity["executable"]
        or sha256(executable) != identity["core_sha256"]
    ):
        raise RuntimeError(
            "Reconciled manual release executable changed after reconciliation"
        )
    manifest_path = release / "nebula-runtime.json"
    if (
        sha256(manifest_path) != identity["manifest_sha256"]
        or json.loads(manifest_path.read_text()).get("version") != sha
    ):
        raise RuntimeError(
            "Reconciled manual release manifest changed after reconciliation"
        )
    return release, identity


def release_core_sha256(version: str) -> str:
    """The supervisor executable digest bound to a managed or reconciled release."""
    manual = _manual_baseline_release(version)
    if manual is not None:
        return manual[1]["core_sha256"]
    release = verified_release(version)
    receipt = json.loads((release / "simulation-acceptance.json").read_text())
    expected = receipt.get("core_sha256")
    if not isinstance(expected, str) or not SHA256.fullmatch(expected):
        raise RuntimeError("Accepted supervisor executable is not hash-bound")
    return expected


def runtime_release(sha: str) -> tuple[Path, str]:
    manual = _manual_baseline_release(sha)
    if manual is not None:
        return manual[0], manual[1]["manifest_sha256"]
    release = verified_release(sha)
    receipt = json.loads((release / "simulation-acceptance.json").read_text())
    digest = receipt.get("runtime_manifest_sha256")
    if not isinstance(digest, str) or sha256(release / "nebula-runtime.json") != digest:
        raise RuntimeError(
            "The accepted release has no verified supervisor runtime manifest"
        )
    return release, digest


def migration_status(version: str) -> dict[str, Any]:
    """Read the exact installed SQL head, including before host activation."""
    release, _digest = runtime_release(version)
    manifest = json.loads((release / "nebula-runtime.json").read_text())
    if manifest.get("version") != version or not isinstance(
        manifest.get("migration_head"), str
    ):
        raise RuntimeError(
            "Migration readback requires the exact accepted release manifest"
        )
    settings = configured_postgres(TOKEN_FILE)
    if settings is not None:
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("PG")
        }
        if "PGPASSFILE" in os.environ:
            environment["PGPASSFILE"] = os.environ["PGPASSFILE"]
        result = subprocess.run(
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--set=ON_ERROR_STOP=1",
                "--command",
                "BEGIN READ ONLY; SELECT version_num FROM alembic_version; COMMIT;",
            ],
            env={**environment, **settings},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                "Migration head readback failed; inspect PostgreSQL access"
            )
        heads = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() not in {"", "BEGIN", "COMMIT"}
        ]
    else:
        database = DATA / "nebula.db"
        with sqlite3.connect(
            f"file:{database.resolve()}?mode=ro", uri=True
        ) as connection:
            heads = [
                row[0]
                for row in connection.execute("SELECT version_num FROM alembic_version")
            ]
    if len(heads) != 1:
        raise RuntimeError("Migration readback requires one exact database head")
    return {
        "version": version,
        "current_head": heads[0],
        "target_head": manifest["migration_head"],
    }


def reconcile_operation(operation_id: str) -> dict[str, Any]:
    try:
        return deployment_post(
            "deployment/operations/" + operation_id + "/reconcile", {}
        )
    except HTTPError as error:
        if error.code == 409:
            # Core refuses to settle an operation it cannot prove either way
            # (a target change has no durable outcome record, for example).
            # Its unchanged receipt lets the caller settle the step from
            # deployment status, or keep holding it; nothing is replayed.
            receipt = api().read("deployment/operations/" + operation_id)
            if (
                not isinstance(receipt, dict)
                or receipt.get("operation_id") != operation_id
            ):
                raise RuntimeError(
                    "Deployment operation readback identifies another request"
                ) from error
            return receipt
        if error.code not in {404, 405}:
            raise
        raise RuntimeError(
            "The active host predates durable operation reconciliation; retain the "
            "unknown receipt and wait for its original completion. Do not edit the journal"
        ) from error


def staged_deployer(record: dict) -> StagedDeployer:
    # Promotion reads ride out a busy or restarting Core (``CoreClient.read``);
    # a read failing past its bound stops the run, which resumes from its journal.
    return StagedDeployer(
        record,
        get=api().read,
        post=deployment_post,
        save=lambda value: atomic_json(STATE, value),
        verify=runtime_release,
        activate_host=lambda version: activate_supervisor_host(record, version),
        migration_status=migration_status,
        reconcile_operation=reconcile_operation,
        handoff_grace_seconds=HANDOFF_GRACE_SECONDS,
        recovery_wait_seconds=HANDOFF_RECOVERY_WAIT_SECONDS,
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


def promote(*, backup: bool = False) -> dict[str, Any]:
    """Promote the pending candidate; take a backup only when the operator asks."""
    record = state()
    if record.get("deployment_mode") != "supervisor":
        return legacy_promote(backup=backup)
    pending = record.get("pending")
    if not isinstance(pending, str):
        return {
            "status": "no_pending_candidate",
            "deployment": api().get("deployment/status"),
        }
    operator_backup = None
    if backup:
        operator_backup = str(
            operator_database_backup(f"operator-pre-{pending}-{int(time.time())}")
        )
    result = staged_deployer(record).promote(pending)
    if operator_backup is not None:
        result = {**result, "operator_backup": operator_backup}
    return result


def rollback(*, _legacy_attempt: _LegacyAttempt | None = None) -> dict[str, Any]:
    record = state()
    if record.get("deployment_mode") != "supervisor":
        return offline_rollback(_legacy_attempt=_legacy_attempt)
    journal = record.get("runtime_deployment")
    running = (
        journal
        if isinstance(journal, dict) and journal.get("state") != "complete"
        else None
    )
    # An unfinished promotion is rolled back to the release it started from;
    # an unfinished rollback resumes toward its own target.
    previous = (
        running.get("previous")
        if running is not None and running.get("action") == "promote"
        else running.get("version")
        if running is not None and running.get("action") == "rollback"
        else record.get("previous")
    )
    if not isinstance(previous, str) or not SHA.fullmatch(previous):
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


def prebuild() -> dict[str, Any]:
    """Build and accept the newest main release before anyone promotes it.

    Meant for a timer: it does what ``prepare`` does, so a later ``promote``
    or ``cycle`` finds the immutable release already built and only verifies
    it. Waiting states are results, not failures: an unfinished deployment,
    or main CI evidence that is still running, missing or failed, is reported
    and retried on the next run. It never promotes, never runs local tests,
    and leaves Core untouched.
    """
    record = state()
    journal = record.get("runtime_deployment")
    if isinstance(journal, dict) and journal.get("state") != "complete":
        return {
            "status": "skipped",
            "reason": "deployment_in_progress",
            "deployment_state": journal.get("state"),
            "version": journal.get("version"),
        }
    if record.get("promoting") is not None:
        return {"status": "skipped", "reason": "promotion_in_progress"}
    try:
        prepared = prepare()
    except CiEvidenceUnavailable as error:
        return {"status": "waiting_for_ci", "reason": str(error)}
    if prepared.get("status") == "current":
        return {"status": "current", "commit": prepared.get("commit")}
    return {
        "status": "prebuilt",
        "commit": prepared.get("commit"),
        "previous": prepared.get("previous"),
        "timings": prepared.get("timings"),
    }


def _lock_holder(path: Path) -> str | None:
    try:
        holder = json.loads(path.read_text() or "null")
    except (OSError, ValueError):
        return None
    action = holder.get("action") if isinstance(holder, dict) else None
    return action if isinstance(action, str) else None


def acquire_deploy_lock(
    lock,
    path: Path,
    action: str,
    *,
    clock=time.monotonic,
    sleep=time.sleep,
) -> str | None:
    """Take the exclusive deploy lock for ``action``.

    Returns None once held. A prebuild returns the busy holder's action
    instead of waiting. A promote or cycle waits, bounded, for a prebuild,
    since that run is building the release it will promote; any other busy
    holder is refused at once, as before.
    """
    deadline = None
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            holder = _lock_holder(path)
            if action == "prebuild":
                return holder or "unknown"
            if action in {"promote", "cycle"} and holder == "prebuild":
                if deadline is None:
                    deadline = clock() + PREBUILD_LOCK_WAIT_SECONDS
                    print(
                        f"{action}: waiting for the running prebuild to finish",
                        file=sys.stderr,
                        flush=True,
                    )
                if clock() < deadline:
                    sleep(LOCK_POLL_SECONDS)
                    continue
            raise RuntimeError(
                f"Another deployer run ({holder or 'unknown action'}) holds the deploy lock"
            )
    lock.seek(0)
    lock.truncate()
    lock.write(json.dumps({"action": action, "pid": os.getpid()}))
    lock.flush()
    return None


def cycle(*, backup: bool = False) -> dict[str, Any]:
    prepared = prepare()
    return {"prepared": prepared, "promotion": promote(backup=backup)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "promote",
            "rollback",
            "reconcile-runtime-deployment",
            "reconcile-superseded-manual-deployment",
            "reconcile-unactivated-supersession",
            "activate-host",
            "rollback-host",
            "offline-rollback",
            "adopt-supervisor",
            "bootstrap",
            "cycle",
            "prebuild",
            "status",
            "hold",
            "release-holds",
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
        "--require-ci-evidence",
        action="store_true",
        help=(
            "prepare: also require a successful main CI run for every commit "
            "since the live release (off by default: pull requests merge only "
            "after their CI passes)"
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
        "--backup",
        action="store_true",
        help=(
            "operator request: take a backup before promoting (a coherent rollback "
            "snapshot in legacy mode, an online PostgreSQL dump in supervisor mode); "
            "without it no backup is taken"
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
    parser.add_argument(
        "--accepted-dead-owner-file",
        type=Path,
        help="exact operator acceptance of one proven-dead worker and its unfinished turns",
    )
    parser.add_argument(
        "--deployment-id",
        help="exact unresolved runtime_deployment journal ID for guarded reconciliation",
    )
    parser.add_argument(
        "--manual-receipt-prefix",
        help="exact YYYYMMDD-<8 hex commit digits> prefix for the later manual release receipts",
    )
    parser.add_argument(
        "--allow-degraded-baseline",
        action="store_true",
        help=(
            "explicitly accept a currently degraded deployment aggregate after the exact host, API, and database checks pass"
        ),
    )
    parser.add_argument(
        "--project",
        action="append",
        help=(
            "hold: hold this project (repeat); without it every project is held, "
            "as a whole-Core stop needs"
        ),
    )
    parser.add_argument(
        "--reason",
        help="hold or release-holds: operator-facing reason recorded on each hold",
    )
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=PROJECT_HOLD_GRACE_SECONDS,
        help="hold: seconds running turns may finish before Core stops them",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate a supported supersession reconciliation without changing deployer state",
    )
    args = parser.parse_args()
    checked_host()
    if args.ci_run_id is not None and args.action != "prepare":
        raise RuntimeError("--ci-run-id is supported only with prepare")
    if args.require_ci_evidence and args.action != "prepare":
        raise RuntimeError("--require-ci-evidence is supported only with prepare")
    if args.backup and args.action not in {"promote", "cycle"}:
        raise RuntimeError("--backup is supported only with promote or cycle")
    if (
        args.ci_pr_run_id is not None or args.ci_playwright_run_id is not None
    ) and args.action != "prepare":
        raise RuntimeError("Supplemental PR CI run IDs are supported only with prepare")
    if args.allow_degraded_workers and args.action != "activate-host":
        raise RuntimeError(
            "--allow-degraded-workers is supported only with activate-host"
        )
    if args.accepted_dead_owner_file is not None and args.action != "activate-host":
        raise RuntimeError(
            "--accepted-dead-owner-file is supported only with activate-host"
        )
    if args.deployment_id is not None and args.action not in {
        "reconcile-runtime-deployment",
        "reconcile-superseded-manual-deployment",
        "reconcile-unactivated-supersession",
    }:
        raise RuntimeError("--deployment-id is supported only with reconciliation")
    if (
        args.action
        in {
            "reconcile-runtime-deployment",
            "reconcile-superseded-manual-deployment",
            "reconcile-unactivated-supersession",
        }
        and args.deployment_id is None
    ):
        raise RuntimeError("reconciliation requires --deployment-id")
    if (
        args.manual_receipt_prefix is not None
        and args.action != "reconcile-superseded-manual-deployment"
    ):
        raise RuntimeError(
            "--manual-receipt-prefix is supported only for manual supersession reconciliation"
        )
    if (
        args.action == "reconcile-superseded-manual-deployment"
        and args.manual_receipt_prefix is None
    ):
        raise RuntimeError(
            "manual supersession reconciliation requires --manual-receipt-prefix"
        )
    if (
        args.allow_degraded_baseline
        and args.action != "reconcile-superseded-manual-deployment"
    ):
        raise RuntimeError(
            "--allow-degraded-baseline is supported only for manual supersession reconciliation"
        )
    if args.dry_run and args.action not in {
        "reconcile-superseded-manual-deployment",
        "reconcile-unactivated-supersession",
    }:
        raise RuntimeError(
            "--dry-run is supported only for manual supersession reconciliation"
        )
    if (args.project is not None or args.reason is not None) and args.action not in {
        "hold",
        "release-holds",
    }:
        raise RuntimeError("--project and --reason are supported only with hold")
    if args.action == "hold" and not args.reason:
        raise RuntimeError("hold requires --reason")
    if not 0 <= args.grace_seconds <= 3600:
        raise RuntimeError("--grace-seconds must be between 0 and 3600")
    lock_path = ROOT / ".deploy.lock"
    with lock_path.open("a+") as lock:
        busy = acquire_deploy_lock(lock, lock_path, args.action)
        result = (
            {"status": "skipped", "reason": "deploy_lock_busy", "holder": busy}
            if busy is not None
            else {
                "status": "state",
                **state(),
                "project_holds": _saved_holds(),
            }
            if args.action == "status"
            else hold_command(args.project, args.reason, args.grace_seconds)
            if args.action == "hold"
            else release_project_holds(reason=args.reason or "Released by the operator")
            if args.action == "release-holds"
            else prepare(
                ci_run_ids=args.ci_run_id,
                pr_ci_run_ids=args.ci_pr_run_id,
                pr_playwright_run_ids=args.ci_playwright_run_id,
                require_ci_evidence=args.require_ci_evidence,
            )
            if args.action == "prepare"
            else reconcile_runtime_deployment(args.deployment_id)
            if args.action == "reconcile-runtime-deployment"
            else reconcile_superseded_manual_deployment(
                args.deployment_id,
                manual_prefix=args.manual_receipt_prefix,
                allow_degraded_baseline=args.allow_degraded_baseline,
                dry_run=args.dry_run,
            )
            if args.action == "reconcile-superseded-manual-deployment"
            else reconcile_unactivated_supersession(
                args.deployment_id, dry_run=args.dry_run
            )
            if args.action == "reconcile-unactivated-supersession"
            else activate_host(
                allow_degraded_workers=args.allow_degraded_workers,
                accepted_dead_owner=(
                    load_dead_owner_acceptance(args.accepted_dead_owner_file)
                    if args.accepted_dead_owner_file is not None
                    else None
                ),
            )
            if args.action == "activate-host"
            else promote(backup=args.backup)
            if args.action == "promote"
            else cycle(backup=args.backup)
            if args.action == "cycle"
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
