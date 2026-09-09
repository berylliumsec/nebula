"""Prevent accidental unscoped local pytest runs; collection is always safe."""

import os
from pathlib import Path

import pytest


def pytest_configure(config):
    if config.option.collectonly:
        return
    approved = os.environ.get("NEBULA_FULL_SUITE_APPROVAL") == "RUN_FULL_SUITE"
    if approved and os.environ.get("NEBULA_FULL_SUITE_REASON", "").strip():
        return
    targets = [str(arg).split("::", 1)[0] for arg in config.args]
    if not targets or any(not target.endswith(".py") for target in targets):
        raise pytest.UsageError(
            "Focused test files required. Full suites almost never run and require "
            "explicit user approval plus NEBULA_FULL_SUITE_APPROVAL=RUN_FULL_SUITE "
            "and NEBULA_FULL_SUITE_REASON for this one command."
        )
    all_files = {
        path.resolve()
        for path in (Path(__file__).parent / "tests/v3").glob("test_*.py")
    }
    whole_files = {
        Path(str(arg)).resolve() for arg in config.args if "::" not in str(arg)
    }
    if all_files and all_files <= whole_files:
        raise pytest.UsageError(
            "Enumerating every test file does not bypass the full-suite approval gate."
        )
