#!/usr/bin/env python3
"""Check an Apple notarization submission using credentials from 1Password."""

from __future__ import annotations

import argparse
import platform
import re
import shutil
import subprocess
import sys


SUBMISSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description=(
            "Query an Apple notarytool submission using APPLE_ID, "
            "APPLE_PASSWORD, and APPLE_TEAM_ID from 1Password."
        )
    )
    argument_parser.add_argument("submission_id", help="Apple submission UUID")
    argument_parser.add_argument(
        "--vault",
        help=(
            "1Password vault containing NEBULA_APPLE_ID, "
            "NEBULA_NOTARY_PASSWORD, and NEBULA_APPLE_TEAM_ID items"
        ),
    )
    argument_parser.add_argument("--apple-id-ref", help="APPLE_ID op:// reference")
    argument_parser.add_argument(
        "--apple-password-ref", help="APPLE_PASSWORD op:// reference"
    )
    argument_parser.add_argument("--team-id-ref", help="APPLE_TEAM_ID op:// reference")
    return argument_parser


def fail(message: str) -> None:
    raise SystemExit(message)


def read_secret(reference: str, label: str) -> str:
    try:
        result = subprocess.run(
            ["op", "read", reference],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or "1Password CLI returned an error"
        fail(f"could not read {label} from 1Password: {detail}")
    value = result.stdout.strip()
    if not value:
        fail(f"1Password returned an empty {label}")
    return value


def references(arguments: argparse.Namespace) -> tuple[str, str, str]:
    explicit = (
        arguments.apple_id_ref,
        arguments.apple_password_ref,
        arguments.team_id_ref,
    )

    if arguments.vault:
        if any(explicit):
            fail("use either --vault or explicit references, not both")
        return (
            f"op://{arguments.vault}/NEBULA_APPLE_ID/password",
            f"op://{arguments.vault}/NEBULA_NOTARY_PASSWORD/password",
            f"op://{arguments.vault}/NEBULA_APPLE_TEAM_ID/password",
        )

    if not all(explicit):
        fail("provide --vault or all three explicit 1Password references")
    return explicit


def main() -> int:
    arguments = parser().parse_args()

    if not SUBMISSION_ID.fullmatch(arguments.submission_id):
        fail(f"invalid Apple notarization submission ID: {arguments.submission_id}")
    if platform.system() != "Darwin":
        fail("Apple notarytool requires macOS with Xcode installed")
    for command in ("op", "xcrun"):
        if shutil.which(command) is None:
            fail(f"required command is unavailable: {command}")

    apple_id_ref, apple_password_ref, team_id_ref = references(arguments)
    apple_id = read_secret(apple_id_ref, "NEBULA_APPLE_ID")
    apple_password = read_secret(apple_password_ref, "NEBULA_NOTARY_PASSWORD")
    team_id = read_secret(team_id_ref, "NEBULA_APPLE_TEAM_ID")

    if not TEAM_ID.fullmatch(team_id):
        fail("APPLE_TEAM_ID must be exactly 10 uppercase letters or digits")

    completed = subprocess.run(
        [
            "xcrun",
            "notarytool",
            "info",
            arguments.submission_id,
            "--apple-id",
            apple_id,
            "--password",
            apple_password,
            "--team-id",
            team_id,
        ],
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
