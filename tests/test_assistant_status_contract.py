import json
from pathlib import Path

from scripts.capture_assistant_status import collect_status


def test_status_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1] / "assistant-rs/compatibility/python-status.json"
        ).read_text()
    )
    assert collect_status() == saved
