"""Contract for the inert packaged-app peer; never invoke a real tool/runtime."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("decision", ["allow", "deny", "cancel"])
def test_inert_native_peer_records_only_decisions_and_bounds_output(tmp_path, decision):
    fixture = tmp_path / "stabilization_acp.py"
    shutil.copyfile(
        Path(__file__).parents[1] / "scripts/fixtures/stabilization_acp.py", fixture
    )
    requests = [
        {"id": 1, "method": "initialize"},
        {"id": 2, "method": "session/prompt"},
        {"method": "session/cancel"}
        if decision == "cancel"
        else {
            "id": "inert-permission-1",
            "result": {"outcome": {"optionId": decision}},
        },
    ]
    result = subprocess.run(
        [sys.executable, str(fixture)],
        input="\n".join(json.dumps(item) for item in requests) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert responses[1]["method"] == "session/request_permission"
    receipt = fixture.with_suffix(".receipts.jsonl")
    if decision == "cancel":
        assert not receipt.exists()
        assert responses[-1]["result"]["stopReason"] == "cancelled"
        assert not any(item.get("method") == "session/update" for item in responses)
    else:
        records = [json.loads(line) for line in receipt.read_text().splitlines()]
        assert records == [
            {"request_id": "inert-permission-1", "allowed": decision == "allow"}
        ]
        text = responses[-2]["params"]["update"]["content"]["text"]
        assert len(text) < 5000
        assert text.startswith(
            "NATIVE_APPROVAL_ACCEPTED_ONCE"
            if decision == "allow"
            else "NATIVE_APPROVAL_DECLINED"
        )
        assert text.count("Local fixture paragraph") == (
            60 if decision == "allow" else 0
        )
        assert responses[-1]["result"]["stopReason"] == "end_turn"
