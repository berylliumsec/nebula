"""Explicit opt-in acceptance against already built frozen artifacts."""

import os
from pathlib import Path

import pytest

from scripts.verify_frozen_core_runtime import run


def test_actual_frozen_core_reconnects_and_uses_private_worker_ipc(tmp_path):
    core = os.getenv("NEBULA_TEST_FROZEN_CORE")
    bundle = os.getenv("NEBULA_TEST_FROZEN_BUNDLE")
    if not core or not bundle:
        pytest.skip(
            "requires explicitly supplied, already built frozen Core and bundle"
        )
    candidate = os.getenv("NEBULA_TEST_FROZEN_CANDIDATE")
    receipt = run(
        Path(core),
        Path(bundle),
        tmp_path / "frozen-acceptance",
        Path(candidate) if candidate else None,
        diagnostic_declared_budget=os.getenv("NEBULA_TEST_DIAGNOSTIC_DB_BUDGET") == "1",
    )
    assert receipt["result"] == "passed"
