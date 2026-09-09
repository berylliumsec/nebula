#!/usr/bin/env python3
"""Validate a reviewed, diff-bound test selection before CI installs runtimes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

PLAN = ".github/test-selection.json"


def change_digest(baseline: str, candidate: str) -> str:
    revisions = [baseline] if candidate == "WORKTREE" else [baseline, candidate]
    diff = subprocess.check_output(
        [
            "git",
            "diff",
            "--binary",
            "--no-ext-diff",
            *revisions,
            "--",
            ".",
            f":(exclude){PLAN}",
        ]
    )
    return hashlib.sha256(diff).hexdigest()


def validate(plan: dict, digest: str) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("Coverage review required: plan must be an object.")
    if plan.get("change_digest") != digest:
        raise ValueError(
            "Coverage review required: missing/stale change_digest; regenerate the selection for this diff."
        )
    versions = plan.get("python_versions", ["3.12"])
    if (
        not isinstance(versions, list)
        or not versions
        or any(v not in ("3.11", "3.12", "3.13") for v in versions)
    ):
        raise ValueError(
            "Select supported Python versions explicitly; default is 3.12."
        )
    if type(plan.get("python_browser", False)) is not bool:
        raise ValueError("python_browser must be a boolean runtime requirement.")
    if type(plan.get("python_node", False)) is not bool:
        raise ValueError("python_node must be a boolean runtime requirement.")
    if not isinstance(plan.get("expected_tests"), dict):
        raise ValueError("Record expected_tests counts for each selected layer.")
    for field in ("reason", "reviewed_by", "exclusions"):
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ValueError(
                f"Coverage review required: {field} must explain this selection."
            )
    for kind in ("python", "frontend", "playwright", "native"):
        values = plan.get(kind)
        if not isinstance(values, list) or any(
            not isinstance(v, str) or not v for v in values
        ):
            raise ValueError(
                f"{kind} must be an explicit list (empty only with an exclusion rationale)."
            )
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate {kind} selections")
        count = plan.get("expected_tests", {}).get(kind)
        if type(count) is not int or count < 0 or bool(values) != bool(count):
            raise ValueError(
                f"Record a collected/estimated {kind} test count consistent with the selection."
            )
        if kind == "playwright":
            continue
        if kind == "native":
            if any(
                not re.fullmatch(
                    r"[A-Za-z_][A-Za-z_0-9]*(?:::[A-Za-z_][A-Za-z_0-9]*)+", v
                )
                for v in values
            ):
                raise ValueError(
                    "Native tests require exact module::test names, not broad filters."
                )
            continue
        for value in values:
            path = value.split("::", 1)[0]
            prefix = "tests/" if kind == "python" else "ui/src/"
            suffix = (
                path.endswith(".py")
                if kind == "python"
                else path.endswith((".test.ts", ".test.tsx"))
            )
            if (
                not path.startswith(prefix)
                or ".." in Path(path).parts
                or not suffix
                or not Path(path).is_file()
            ):
                raise ValueError(
                    f"Select existing individual {kind} test files, not suites/options/globs: {value}"
                )
        all_tests = (
            set(Path("tests/v3").rglob("test_*.py"))
            if kind == "python"
            else set(Path("ui/src").rglob("*.test.ts*"))
        )
        selected = {Path(v.split("::", 1)[0]) for v in values if "::" not in v}
        if all_tests and all_tests <= selected:
            raise ValueError("A routine selection must not enumerate the entire suite.")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--plan", type=Path, default=Path(PLAN))
    parser.add_argument(
        "--digest",
        action="store_true",
        help="Print digest only; stage new files before using WORKTREE",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        digest = change_digest(args.baseline, args.candidate)
        if args.digest:
            print(digest)
            return 0
        plan = validate(json.loads(args.plan.read_text()), digest)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        blocked = {
            "coverage_review_required": True,
            "reason": str(error),
            "python": [],
            "frontend": [],
            "native": [],
            "playwright": [],
        }
        if args.output:
            args.output.write_text(json.dumps(blocked, indent=2) + "\n")
        print(json.dumps(blocked))
        return 2
    if args.output:
        args.output.write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
