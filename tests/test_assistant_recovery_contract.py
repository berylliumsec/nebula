import json
from pathlib import Path

from scripts.capture_assistant_recovery import collect_recovery


def test_recovery_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1]
            / "assistant-rs/compatibility/python-recovery.json"
        ).read_text()
    )
    assert collect_recovery() == saved
