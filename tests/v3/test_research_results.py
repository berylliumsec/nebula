from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from nebula.v3.automation_tools import command_specs
from nebula.v3.research_results import ResearchResultService
from nebula.v3.tools import RETRIEVAL_TOOL_NAMES


HANDLE = "sim:20260928T120000Z-deadbeef"


def test_research_result_tools_are_always_available_to_callback_turns() -> None:
    names = {"research_result_catalog", "research_result_search", "research_result_read"}
    assert names <= command_specs().keys()
    assert names <= RETRIEVAL_TOOL_NAMES


def test_adapter_passes_only_opaque_handle_and_typed_search_args(tmp_path: Path) -> None:
    service = ResearchResultService(tmp_path / "python")
    payload = {"handle": HANDLE, "matches": [], "cursor": None}
    completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
    with patch("nebula.v3.research_results.subprocess.run", return_value=completed) as run:
        assert service.query("search", {"handle": HANDLE, "text": "0x1000",
                                        "filters": {"address": "0x1000"}, "limit": 2}) == payload
    argv = run.call_args.args[0]
    assert argv[:4] == [str(tmp_path / "python"), "-m", "apple_rnd.workflows.result_catalog", "search"]
    assert "--handle" in argv
    assert not any(item.startswith("/") and item != str(tmp_path / "python") for item in argv)


@pytest.mark.parametrize("arguments", [
    {"handle": "mac:/etc/passwd"},
    {"handle": HANDLE, "filters": {"path": "/etc/passwd"}},
    {"handle": HANDLE, "text": ""},
])
def test_adapter_rejects_invalid_input_before_starting_process(arguments: dict) -> None:
    with patch("nebula.v3.research_results.subprocess.run") as run:
        with pytest.raises(ValueError):
            ResearchResultService().query("search", arguments)
    run.assert_not_called()
