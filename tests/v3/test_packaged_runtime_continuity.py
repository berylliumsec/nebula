"""Exactly one explicit opt-in journey against real, prebuilt native artifacts."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.test_packaged_runtime_continuity import run


def test_real_packaged_windows_preserve_held_turn_and_draft_through_code_rollback(
    tmp_path,
):
    path = os.getenv("NEBULA_TEST_PACKAGED_CONTINUITY_INPUTS")
    if not path:
        pytest.skip(
            "requires explicit owned native GUI fixture and prebuilt artifact inputs"
        )
    values = json.loads(Path(path).read_text())
    selected = {
        name: Path(value).resolve(strict=True) for name, value in values.items()
    }
    selected.setdefault(
        "desktop_fixture",
        Path(__file__).with_name("packaged_continuity_desktop_fixture.py"),
    )
    selected.setdefault("public_model_cache", None)
    receipt = run(SimpleNamespace(**selected, output=tmp_path / "packaged-continuity"))
    assert receipt["result"] == "passed"
    assert receipt["provider_executions"] == 1
    assert receipt["shutdown_observed_state"] == "stopped"
