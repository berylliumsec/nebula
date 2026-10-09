"""Publish a verified stable Linux release for new commits on main.

The protected admin credential creates tags and dispatches gated workflows.
Publication uses GITHUB_TOKEN so the driver owns the updater dispatch. A failed
gate leaves its immutable tag or draft for a release manager to inspect.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
MAIN_SHA = os.environ.get("GITHUB_SHA", "")
SOURCE_MARKER = re.compile(r"<!-- nebula-source-sha: ([0-9a-f]{40}) -->")
WAIT_SECONDS = 3 * 60 * 60
CONSERVATIVE_AREAS = ("desktop-interface", "mobile-layout", "core-api")


def run(*args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, text=True, capture_output=True, check=False, env=env)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def gh_json(endpoint: str) -> object:
    return json.loads(run("gh", "api", endpoint))


def require_release_admin() -> None:
    """Validate the authenticated principal, not the schedule's displayed actor."""
    if not os.environ.get("GH_TOKEN") or not os.environ.get("NEBULA_PUBLISH_TOKEN"):
        raise RuntimeError(
            "Protected release and GITHUB_TOKEN publication credentials are required"
        )
    try:
        actor = gh_json("user")["login"]
        permission = gh_json(f"repos/{REPOSITORY}/collaborators/{actor}/permission")
    except (RuntimeError, KeyError, TypeError) as error:
        raise RuntimeError(
            "Cannot verify the protected release credential's repository-admin identity"
        ) from error
    if permission.get("permission") != "admin":
        raise RuntimeError(
            "The protected release credential must belong to a repository admin"
        )
    print(f"Release credential: verified repository admin {actor}", flush=True)


def publish_release(tag: str) -> None:
    """Suppress a second release-event updater run; explicitly dispatch it below."""
    token = os.environ.get("NEBULA_PUBLISH_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN publication credential is required")
    run(
        "gh",
        "release",
        "edit",
        tag,
        "--repo",
        REPOSITORY,
        "--draft=false",
        "--prerelease=false",
        env={**os.environ, "GH_TOKEN": token},
    )


def published_release() -> dict:
    releases = gh_json(f"repos/{REPOSITORY}/releases?per_page=100")
    matches = [
        item
        for item in releases
        if item["tag_name"].startswith("nebula-v3.")
        and not item["draft"]
        and item.get("published_at")
    ]
    if not matches:
        raise RuntimeError("No published Nebula 3 release; bootstrap requires review")
    return max(matches, key=lambda item: item["published_at"])


def next_stable_version(tag: str) -> str:
    match = re.fullmatch(r"nebula-v(3)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", tag)
    if match is None:
        raise RuntimeError(f"Cannot derive a stable successor from {tag}")
    major, minor, patch, prerelease = match.groups()
    return f"{major}.{minor}.{int(patch) + (0 if prerelease else 1)}"


def green_main() -> None:
    runs = gh_json(
        f"repos/{REPOSITORY}/actions/workflows/ci.yml/runs"
        f"?head_sha={MAIN_SHA}&per_page=30"
    )["workflow_runs"]
    if not any(
        item["event"] == "push"
        and item["conclusion"] == "success"
        and item["head_sha"] == MAIN_SHA
        for item in runs
    ):
        raise RuntimeError(f"CI has not passed for main {MAIN_SHA}")


def conservative_coverage(plan: dict) -> tuple[str, str]:
    """Retain matched journeys and add bounded cross-interface/Core coverage."""
    manifest = json.loads(Path("ui/playwright-impact.json").read_text())
    areas = set(CONSERVATIVE_AREAS)
    for rule in manifest["rules"]:
        if any(
            fnmatch.fnmatchcase(path, pattern)
            for path in plan["changed_files"]
            for pattern in rule["patterns"]
        ):
            areas.update(rule["areas"])
    selection = ",".join(f"area:{area}" for area in sorted(areas))
    reason = (
        "Daily stable release: automatic conservative catalog coverage for "
        + ", ".join(plan["fallbacks"])
        + ". Includes desktop/compact interface, small/wide mobile Chromium and "
        "WebKit, real-Core contracts, and every matched feature area. Other "
        "full-matrix profiles are excluded; CI, packaging and smoke checks remain required."
    )
    return selection, reason


def preflight_impact(baseline: str, candidate: str) -> tuple[str, str]:
    """Resolve and validate daily coverage before consuming an immutable tag."""
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory, "plan.json")
        args = [
            sys.executable,
            "scripts/playwright_impact.py",
            "select",
            "--manifest",
            "ui/playwright-impact.json",
            "--baseline",
            baseline,
            "--candidate",
            candidate,
            "--scope",
            "impacted",
            "--output",
            str(output),
            "--receipt",
            str(Path(directory, "receipt.md")),
        ]
        result = subprocess.run(
            args,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return "", "Daily stable release: automatic impacted selection."
        plan = json.loads(output.read_text()) if output.exists() else {}
        blockers = plan.get("fallbacks", [])
        if (
            result.returncode != 2
            or not plan.get("coverage_review_required")
            or plan.get("baseline_sha") != baseline
            or plan.get("candidate_sha") != candidate
            or not blockers
            or any(not item.startswith(("global:", "unmapped:")) for item in blockers)
        ):
            raise RuntimeError(
                "Cannot resolve daily coverage before tagging: "
                + (", ".join(blockers) or result.stderr.strip())
            )
        selection, review_reason = conservative_coverage(plan)
        args.extend(("--selection", selection, "--review-reason", review_reason))
        result = subprocess.run(args, text=True, capture_output=True, check=False)
        accepted = json.loads(output.read_text())
        if (
            result.returncode
            or accepted.get("coverage_review_required", True)
            or accepted.get("baseline_sha") != baseline
            or accepted.get("candidate_sha") != candidate
            or not accepted.get("include")
        ):
            raise RuntimeError(
                "Conservative daily coverage failed validation before tagging: "
                + result.stderr.strip()
            )
        print(Path(directory, "receipt.md").read_text(), flush=True)
        return selection, review_reason


def wait_for_workflow(name: str, ref: str, commit: str, since: datetime) -> int:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        runs = gh_json(
            f"repos/{REPOSITORY}/actions/workflows/{name}/runs"
            "?event=workflow_dispatch&per_page=50"
        )["workflow_runs"]
        matching = [
            item
            for item in runs
            if item["head_branch"] == ref
            and item["head_sha"] == commit
            and item["event"] == "workflow_dispatch"
            and datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            >= since
        ]
        if matching:
            latest = max(matching, key=lambda item: item["id"])
            if latest["status"] == "completed":
                if latest["conclusion"] != "success":
                    raise RuntimeError(
                        f"{name} run {latest['id']} ended {latest['conclusion']}"
                    )
                return latest["id"]
        time.sleep(30)
    raise RuntimeError(f"Timed out waiting for {name} on {ref}")


def verify_impact_receipt(
    run_id: int, tag: str, baseline: str, candidate: str, selection: str = ""
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        run(
            "gh",
            "run",
            "download",
            str(run_id),
            "--repo",
            REPOSITORY,
            "--name",
            f"release-playwright-impact-{tag}",
            "--dir",
            directory,
        )
        plan = json.loads(Path(directory, "playwright-impact-plan.json").read_text())
        if (
            plan["coverage_review_required"]
            or plan["baseline_sha"] != baseline
            or plan["candidate_sha"] != candidate
            or (
                selection
                and (
                    plan.get("requested_selection") != selection.split(",")
                    or not plan.get("include")
                )
            )
        ):
            raise RuntimeError(
                "Release impact receipt lacks an accepted baseline or coverage"
            )
        print(
            f"Impact receipt: {len(plan['include'])} selected matrix entries",
            flush=True,
        )


def verify_assets(run_id: int, version: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        run(
            "gh",
            "run",
            "download",
            str(run_id),
            "--repo",
            REPOSITORY,
            "--name",
            "nebula3-linux-x64",
            "--dir",
            directory,
        )
        root = Path(directory)
        checksum = root / "SHA256SUMS-linux-x64.txt"
        required = [
            root / f"Nebula-{version}-linux-x86_64.deb",
            root / f"Nebula-{version}-linux-x86_64.AppImage",
        ]
        if not checksum.is_file() or any(not path.is_file() for path in required):
            raise RuntimeError("Missing required Linux artifact or checksum manifest")
        subprocess.run(["sha256sum", "-c", checksum.name], cwd=root, check=True)
        for path in required:
            run("gh", "attestation", "verify", str(path), "--repo", REPOSITORY)


def main() -> None:
    if not REPOSITORY or not re.fullmatch(r"[0-9a-f]{40}", MAIN_SHA):
        raise RuntimeError("GitHub repository and main commit are required")
    if run("git", "rev-parse", "HEAD") != MAIN_SHA:
        raise RuntimeError("Checkout is not the scheduled main commit")
    run("git", "fetch", "origin", "main", "--tags")
    if run("git", "rev-parse", "origin/main") != MAIN_SHA:
        raise RuntimeError("Main advanced during release selection; retry tomorrow")

    previous = published_release()
    previous_tag = previous["tag_name"]
    previous_version = previous_tag.removeprefix("nebula-v")
    tagged_notes = run(
        "git",
        "show",
        f"{previous_tag}:docs/releases/{previous_version}.md",
        check=False,
    )
    marker = SOURCE_MARKER.search(tagged_notes)
    previous_source = (
        marker.group(1)
        if marker
        else run("git", "rev-parse", f"{previous_tag}^{{commit}}")
    )
    run("git", "merge-base", "--is-ancestor", previous_source, previous_tag)
    if previous_source == MAIN_SHA:
        print(f"No new changes since {previous_tag}; skipping release")
        return
    run("git", "merge-base", "--is-ancestor", previous_source, MAIN_SHA)
    green_main()
    require_release_admin()
    selection, review_reason = preflight_impact(previous_source, MAIN_SHA)
    run("gh", "attestation", "verify", "--help")

    version = next_stable_version(previous_tag)
    tag = f"nebula-v{version}"
    if run("git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
        raise RuntimeError(f"Existing tag {tag} needs release manager recovery")
    if run("gh", "release", "view", tag, "--repo", REPOSITORY, check=False):
        raise RuntimeError(f"Existing release {tag} needs release manager recovery")

    run("git", "config", "user.name", "github-actions[bot]")
    run(
        "git",
        "config",
        "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com",
    )
    run("git", "checkout", "-B", "daily-release", MAIN_SHA)
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", previous_tag, "HEAD"], check=False
    ).returncode:
        # Retain previous release ancestry while taking the exact current main tree.
        run(
            "git",
            "merge",
            "-s",
            "ours",
            "--no-ff",
            previous_tag,
            "-m",
            f"Record {previous_tag} release lineage",
        )

    subjects = run(
        "git", "log", "--no-merges", "--format=%h %s", f"{previous_source}..{MAIN_SHA}"
    )
    if not subjects:
        subjects = run("git", "log", "--format=%h %s", f"{previous_source}..{MAIN_SHA}")
    notes = Path("docs/releases") / f"{version}.md"
    if notes.exists():
        raise RuntimeError(f"Existing release notes {notes} need review")
    notes.write_text(
        f"# Nebula {version}\n\nChanges since {previous_tag}:\n\n"
        + "\n".join(f"- {line}" for line in subjects.splitlines())
        + f"\n\n<!-- nebula-source-sha: {MAIN_SHA} -->\n"
    )

    run(sys.executable, "scripts/nebula3_version.py", "set", version)
    run(
        "git",
        "add",
        "NEBULA3_VERSION",
        "pyproject.toml",
        "src/nebula/v3/version.py",
        "ui/src-tauri/tauri.conf.json",
        "ui/src-tauri/Cargo.toml",
        "ui/src-tauri/Cargo.lock",
        "ui/package.json",
        "ui/package-lock.json",
        str(notes),
    )
    # This release-only commit never changes main. Its admin-authenticated push
    # must not launch a second preparation without the validated selection.
    # workflow_dispatch is unaffected; all release gates still run below.
    run("git", "commit", "-m", f"Prepare Nebula {version} stable release [skip ci]")
    release_commit = run("git", "rev-parse", "HEAD")
    run("git", "tag", "-a", tag, "-m", f"Nebula {version}")
    run("git", "push", "origin", f"refs/tags/{tag}")
    print(f"Created {tag} at {release_commit} from main {MAIN_SHA}", flush=True)

    preparation_started = datetime.now(timezone.utc).replace(microsecond=0)
    dispatch = [
        "gh",
        "workflow",
        "run",
        "nebula3-release.yml",
        "--repo",
        REPOSITORY,
        "--ref",
        tag,
        "-f",
        f"release_tag={tag}",
        "-f",
        "scope=impacted",
        "-f",
        f"review_reason={review_reason}",
    ]
    if selection:
        dispatch.extend(("-f", f"selection={selection}"))
    run(*dispatch)
    preparation_id = wait_for_workflow(
        "nebula3-release.yml", tag, release_commit, preparation_started
    )
    verify_impact_receipt(preparation_id, tag, previous_source, MAIN_SHA, selection)
    verify_assets(preparation_id, version)
    finalize_started = datetime.now(timezone.utc).replace(microsecond=0)
    run(
        "gh",
        "workflow",
        "run",
        "nebula3-release-finalize.yml",
        "--repo",
        REPOSITORY,
        "--ref",
        tag,
        "-f",
        f"release_tag={tag}",
        "-f",
        f"preparation_run_id={preparation_id}",
        "-f",
        "create_draft=true",
    )
    wait_for_workflow(
        "nebula3-release-finalize.yml", tag, release_commit, finalize_started
    )
    release = json.loads(
        run(
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            REPOSITORY,
            "--json",
            "isDraft,isPrerelease,tagName",
        )
    )
    if release != {"isDraft": True, "isPrerelease": False, "tagName": tag}:
        raise RuntimeError("Draft metadata is not the expected stable release")
    if published_release()["tag_name"] != previous_tag:
        raise RuntimeError("Another Nebula release was published during preparation")
    updater_started = datetime.now(timezone.utc).replace(microsecond=0)
    publish_release(tag)
    updater_commit = gh_json(f"repos/{REPOSITORY}/commits/main")["sha"]
    run(
        "gh",
        "workflow",
        "run",
        "publish-updater-manifest.yml",
        "--repo",
        REPOSITORY,
        "--ref",
        "main",
        "-f",
        f"release_tag={tag}",
    )
    wait_for_workflow(
        "publish-updater-manifest.yml", "main", updater_commit, updater_started
    )
    print(f"Published stable release {tag} and updater metadata", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        subprocess.CalledProcessError,
        OSError,
        KeyError,
        ValueError,
    ) as error:
        print(f"Daily stable release stopped: {error}", file=sys.stderr)
        sys.exit(1)
