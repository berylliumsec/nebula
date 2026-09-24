#!/usr/bin/env python3
"""Collect and run diff-bound exact Rust assistant tests; never expand an empty list."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import tomllib

from scripts.test_selection import assistant_rust_target, change_digest, validate

ROOT = Path(__file__).resolve().parents[1]


def cargo_command(selection: str, toolchain: str, *, listing: bool) -> list[str]:
    package, target, name = assistant_rust_target(selection)
    command = [
        "cargo",
        f"+{toolchain}",
        "test",
        "--locked",
        "--manifest-path",
        "assistant-rs/Cargo.toml",
        "-p",
        package,
        *(["--lib"] if target == "lib" else ["--test", target]),
        name,
        "--",
        "--exact",
    ]
    if listing:
        command.append("--list")
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument(
        "--plan", type=Path, default=Path(".github/test-selection.json")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    receipt = {"format": "nebula.assistant-rust-tests/v1", "passed": False, "tests": []}
    try:
        plan = validate(
            json.loads(args.plan.read_text()),
            change_digest(args.baseline, args.candidate),
        )
        receipt["change_digest"] = plan["change_digest"]
        selections = plan.get("assistant_rust", [])
        toolchain = tomllib.loads(
            (ROOT / "assistant-rs/rust-toolchain.toml").read_text()
        )["toolchain"]["channel"]
        receipt["toolchain"] = toolchain
        for selection in selections:
            started = time.monotonic()
            name = assistant_rust_target(selection)[2]
            collected = subprocess.run(
                cargo_command(selection, toolchain, listing=True),
                check=True,
                text=True,
                capture_output=True,
                timeout=600,
            )
            found = [
                line
                for line in collected.stdout.splitlines()
                if line.endswith(": test")
            ]
            if found != [f"{name}: test"]:
                raise ValueError(
                    f"Expected exactly one collected test for {selection}, got {found}"
                )
            if not args.list:
                result = subprocess.run(
                    cargo_command(selection, toolchain, listing=False),
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=120,
                )
                if (
                    "test result: ok. 1 passed; 0 failed; 0 ignored;"
                    not in result.stdout
                ):
                    raise ValueError(
                        f"Selected test did not execute once: {selection}\n{result.stdout}"
                    )
            receipt["tests"].append(
                {
                    "selection": selection,
                    "collected": 1,
                    "executed": not args.list,
                    "seconds": time.monotonic() - started,
                }
            )
            print(f"{'collected' if args.list else 'passed'} {selection}", flush=True)
        receipt["passed"] = True
        receipt["selected_count"] = len(selections)
        receipt["executed_count"] = 0 if args.list else len(selections)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        receipt["error"] = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            receipt["stdout"] = error.stdout
            receipt["stderr"] = error.stderr
        print(json.dumps(receipt), flush=True)
        return 2
    finally:
        if args.output:
            args.output.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
