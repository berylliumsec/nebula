#!/usr/bin/env python3
"""Bind the one installed-package browser smoke to an immutable release artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

CASE = "release core smoke pairs, creates a project, and retains one answer"
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
TAG = re.compile(r"nebula-v3\.[0-9A-Za-z.-]+\Z")


def deb_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_one_passed_browser_case(report: Path) -> None:
    if report.is_symlink() or not report.is_file() or report.stat().st_size > 2_000_000:
        raise ValueError("Missing or invalid Playwright result")
    result = json.loads(report.read_text())
    stats = result.get("stats")
    if not isinstance(stats, dict) or any(
        stats.get(key) != value
        for key, value in {"expected": 1, "skipped": 0, "unexpected": 0, "flaky": 0}.items()
    ):
        raise ValueError("Playwright did not report exactly one passed case")
    specs: list[dict] = []

    def collect(suite: dict) -> None:
        specs.extend(suite.get("specs", []))
        for child in suite.get("suites", []):
            collect(child)

    collect(result)
    if len(specs) != 1 or specs[0].get("title") != CASE:
        raise ValueError("Playwright selected an unexpected case")
    tests = specs[0].get("tests", [])
    if len(tests) != 1 or tests[0].get("projectName") != "real-core" or tests[0].get("status") != "expected":
        raise ValueError("Playwright case did not pass in desktop real-core")
    executions = tests[0].get("results", [])
    if len(executions) != 1 or executions[0].get("status") != "passed" or executions[0].get("retry") != 0:
        raise ValueError("Playwright case was skipped, failed, or retried")


def write_receipt(path: Path, *, tag: str, commit: str, deb: Path, exit_code: int, report: Path) -> None:
    if not TAG.fullmatch(tag) or not COMMIT.fullmatch(commit) or not deb.is_file():
        raise ValueError("Invalid release identity or missing installed DEB")
    if exit_code == 0:
        require_one_passed_browser_case(report)
    payload = {
        "schema_version": 1,
        "tag": tag,
        "commit": commit,
        "case": CASE,
        "selected_test_count": 1,
        "retries": 0,
        "installed_deb_sha256": deb_sha256(deb),
        "result": "passed" if exit_code == 0 else "failed",
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def verify_payload(payload: object, *, tag: str, commit: str, deb_sha: str) -> None:
    expected = {
        "schema_version": 1,
        "tag": tag,
        "commit": commit,
        "case": CASE,
        "selected_test_count": 1,
        "retries": 0,
        "installed_deb_sha256": deb_sha,
        "result": "passed",
    }
    if not SHA.fullmatch(deb_sha) or payload != expected:
        raise ValueError("Release core smoke receipt failed identity, artifact, or result validation")


def verify_receipt(path: Path, *, tag: str, commit: str, deb: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("Missing or invalid release core smoke receipt")
    verify_payload(json.loads(path.read_text()), tag=tag, commit=commit, deb_sha=deb_sha256(deb))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("write", "verify"))
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--playwright-report", type=Path)
    args = parser.parse_args()
    if args.action == "write":
        if args.exit_code is None or args.playwright_report is None:
            parser.error("write requires --exit-code and --playwright-report")
        write_receipt(
            args.receipt, tag=args.tag, commit=args.commit, deb=args.deb,
            exit_code=args.exit_code, report=args.playwright_report,
        )
    else:
        verify_receipt(args.receipt, tag=args.tag, commit=args.commit, deb=args.deb)
        print("Release core smoke receipt matches tag, commit, and installed DEB")


if __name__ == "__main__":
    main()
