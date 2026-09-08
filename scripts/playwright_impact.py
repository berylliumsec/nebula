#!/usr/bin/env python3
"""Generate a fail-closed Playwright matrix from a git diff."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


def matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def changed_paths(name_status: str) -> list[str]:
    """Return both sides of renames and the named path for other changes."""
    paths: list[str] = []
    for raw in name_status.splitlines():
        if not raw.strip():
            continue
        fields = raw.split("\t")
        status = fields[0]
        expected = 3 if status.startswith(("R", "C")) else 2
        if len(fields) < expected:
            raise ValueError(f"invalid git name-status line: {raw!r}")
        paths.extend(fields[1:expected])
    return sorted(set(paths))


def stable_entries(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for entry in entries:
        key = tuple(str(entry.get(field, "")) for field in (
            "area", "project", "test_match", "grep", "runtime", "shard"
        ))
        unique[key] = entry
    return [unique[key] for key in sorted(unique)]


def select_plan(
    manifest: dict[str, Any], baseline: str | None, candidate: str,
    paths: list[str], scope: str = "impacted",
) -> dict[str, Any]:
    matched_rules: list[str] = []
    fallbacks: list[str] = []
    exclusions: list[str] = []

    if scope == "full":
        include = stable_entries(manifest["full_include"])
        reason = "manual_full_scope"
    elif not baseline:
        include = stable_entries(manifest["full_include"])
        reason = "full_fallback"
        fallbacks.append("no_valid_baseline")
    else:
        full_matches = [
            path for path in paths if matches(path, manifest["full_patterns"])
        ]
        if full_matches:
            include = stable_entries(manifest["full_include"])
            reason = "full_fallback"
            fallbacks.extend(f"global:{path}" for path in full_matches)
        else:
            selected_areas: set[str] = set()
            covered: set[str] = set()
            ordered_rules = sorted(manifest["rules"], key=lambda rule: rule.get("fallback", False))
            for rule in ordered_rules:
                hits = [
                    path for path in paths
                    if matches(path, rule["patterns"])
                    and (not rule.get("fallback") or path not in covered)
                ]
                if hits:
                    matched_rules.append(rule["name"])
                    covered.update(hits)
                    selected_areas.update(rule["areas"])

            unknown = sorted(
                path for path in paths
                if path not in covered
                and any(path.startswith(prefix) for prefix in manifest["watched_prefixes"])
                and not matches(path, manifest.get("watched_exclusions", []))
            )
            if unknown:
                include = stable_entries(manifest["full_include"])
                reason = "full_fallback"
                fallbacks.extend(f"unmapped:{path}" for path in unknown)
            else:
                include = stable_entries(
                    entry
                    for area in sorted(selected_areas)
                    for entry in manifest["areas"][area]
                )
                reason = "impacted" if include else "no_playwright_impact"
                exclusions.extend(path for path in paths if path not in covered)

    return {
        "baseline_sha": baseline,
        "candidate_sha": candidate,
        "changed_files": paths,
        "reason": reason,
        "include": include,
        "matched_rules": sorted(set(matched_rules)),
        "fallbacks": fallbacks,
        "exclusions": exclusions,
    }


def render_receipt(plan: dict[str, Any]) -> str:
    def bullets(values: Iterable[str], empty: str = "None") -> str:
        values = list(values)
        return "\n".join(f"- `{value}`" for value in values) if values else f"- {empty}"

    jobs = []
    for entry in plan["include"]:
        command = f"{entry['project']} :: {entry['test_match']}"
        if entry.get("grep"):
            command += f" :: grep={entry['grep']}"
        command += f" :: {entry['runtime']} :: shard={entry['shard']}"
        jobs.append(command)
    return f"""# Playwright impact receipt

- Baseline: `{plan['baseline_sha'] or 'NONE'}`
- Candidate: `{plan['candidate_sha']}`
- Decision: `{plan['reason']}`
- Matrix jobs: `{len(plan['include'])}`

## Changed files

{bullets(plan['changed_files'])}

## Matched rules

{bullets(plan['matched_rules'])}

## Selected matrix

{bullets(jobs)}

## Fail-closed fallbacks

{bullets(plan['fallbacks'])}

## Excluded changes

{bullets(plan['exclusions'])}
"""


def successful_release_baseline(runs: dict[str, Any], candidate: str) -> dict[str, str] | None:
    for run in runs.get("workflow_runs", []):
        tag = str(run.get("head_branch", ""))
        sha = str(run.get("head_sha", ""))
        if (
            run.get("conclusion") == "success"
            and tag.startswith("nebula-v3.")
            and sha
            and sha != candidate
        ):
            return {"tag": tag, "sha": sha, "run_id": str(run.get("id", ""))}
    return None


def git_diff(baseline: str, candidate: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-status", "-M", baseline, candidate],
        check=True, text=True, capture_output=True,
    )
    return changed_paths(proc.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    select = subparsers.add_parser("select")
    select.add_argument("--manifest", type=Path, required=True)
    select.add_argument("--baseline")
    select.add_argument("--candidate", required=True)
    select.add_argument("--scope", choices=("impacted", "full"), default="impacted")
    select.add_argument("--name-status", type=Path)
    select.add_argument("--output", type=Path, required=True)
    select.add_argument("--receipt", type=Path, required=True)

    baseline = subparsers.add_parser("release-baseline")
    baseline.add_argument("--runs-json", type=Path, required=True)
    baseline.add_argument("--candidate", required=True)

    args = parser.parse_args(argv)
    if args.command == "release-baseline":
        result = successful_release_baseline(
            json.loads(args.runs_json.read_text()), args.candidate
        )
        print(json.dumps(result or {}, sort_keys=True))
        return 0

    manifest = json.loads(args.manifest.read_text())
    baseline_sha = args.baseline or None
    if args.name_status:
        paths = changed_paths(args.name_status.read_text())
    elif baseline_sha:
        try:
            paths = git_diff(baseline_sha, args.candidate)
        except subprocess.CalledProcessError as error:
            print(error.stderr, file=sys.stderr)
            baseline_sha = None
            paths = []
    else:
        paths = []
    plan = select_plan(manifest, baseline_sha, args.candidate, paths, args.scope)
    args.output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    args.receipt.write_text(render_receipt(plan))
    print(json.dumps({"include": plan["include"]}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
