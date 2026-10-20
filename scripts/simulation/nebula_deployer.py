#!/usr/bin/env python3
"""Prepare exact Nebula commits and promote only while the live Core is idle.

Install beside nebula_deploy_gate.py on simulation. The initial same-build
cutover provisions current and its user service separately; this deployer
handles later main updates. It never changes the development checkout.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
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
import time
from typing import Any
from urllib.request import Request, urlopen

from nebula_deploy_gate import CoreClient, blockers, service_token


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


def prepare() -> dict[str, Any]:
    git_source()
    previous = baseline()
    run("git", "-C", str(REPO), "fetch", "--no-tags", "origin", "main")
    sha = run("git", "-C", str(REPO), "rev-parse", "origin/main", capture=True)
    if sha == previous:
        return {"status": "current", "commit": sha}
    if not SHA.fullmatch(sha):
        raise RuntimeError("origin/main is not an exact commit")
    run("git", "-C", str(REPO), "merge-base", "--is-ancestor", previous, sha)
    if (RELEASES / sha).is_dir():
        receipt = json.loads(
            (verified_release(sha) / "simulation-acceptance.json").read_text()
        )
        if receipt.get("previous") != previous:
            raise RuntimeError(
                "accepted candidate was reviewed against a different live commit"
            )
        current_state = state()
        current_state["pending"] = sha
        atomic_json(STATE, current_state)
        return receipt
    directory = candidate(sha)
    plan, entries = selected_plan(directory, previous)
    run(POETRY, "check", cwd=directory)
    run(POETRY, "install", "--with", "dev", "--no-interaction", cwd=directory)
    run("npm", "--prefix", "ui", "ci", "--no-audit", "--no-fund", cwd=directory)
    collected = 0
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
        raise RuntimeError("Python collection disagrees with the reviewed test count")
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
    if frontend_count != plan["expected_tests"]["frontend"]:
        raise RuntimeError("frontend collection disagrees with the reviewed test count")
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
    if plan["native"]:
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
        raise RuntimeError("browser collection disagrees with the reviewed test count")
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
    }
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
    ):
        if sha256(release / relative) != receipt.get(field):
            raise RuntimeError(f"release {relative} changed after acceptance")
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
    database_paths = {DATA / relative for relative in SQLITE_DATABASES}
    excluded = database_paths | {
        Path(f"{database}-{suffix}")
        for database in database_paths
        for suffix in ("wal", "shm")
    }

    def ignore_sidecars(path: str, names: list[str]) -> list[str]:
        directory = Path(path)
        return [name for name in names if directory / name in excluded]

    try:
        shutil.copytree(DATA, staging, symlinks=True, ignore=ignore_sidecars)
        for relative in SQLITE_DATABASES:
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
    database = keep / "nebula.db"
    if database.is_symlink() or not database.is_file():
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


def promote() -> dict[str, Any]:
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
            rollback()
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


def rollback() -> dict[str, Any]:
    record = state()
    failed_commit = record.get("promoting") or record.get("active")
    previous = record.get("previous")
    backup = Path(str(record.get("rollback_backup", "")))
    if not isinstance(previous, str) or backup.parent != BACKUPS or not backup.is_dir():
        raise RuntimeError("a coherent rollback backup is unavailable")
    old_release = RELEASES / previous
    if not old_release.is_dir():
        raise RuntimeError("previous immutable release is missing")
    edge("stop")
    service("stop")
    require_stopped()
    preserved = BACKUPS / f"post-{time.time_ns()}"
    os.replace(DATA, preserved)
    try:
        shutil.copytree(backup, DATA, symlinks=True)
        switch_to(old_release)
        service("start")
        wait_ready(previous)
        edge("start")
    except Exception:
        service("stop")
        raise
    record.update(
        {
            "active": previous,
            "pending": failed_commit if failed_commit != previous else None,
            "promoting": None,
            "post_state": str(preserved),
        }
    )
    atomic_json(STATE, record)
    return {
        "status": "rolled_back",
        "commit": previous,
        "preserved_post_state": str(preserved),
    }


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
        choices=("prepare", "promote", "rollback", "bootstrap", "cycle", "status"),
    )
    args = parser.parse_args()
    checked_host()
    with (ROOT / ".deploy.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = (
            {"status": "state", **state()}
            if args.action == "status"
            else globals()[args.action]()
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
        print(f"simulation Nebula deployer: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
