import json
from pathlib import Path

from scripts.capture_assistant_state import collect_state


def test_state_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1] / "assistant-rs/compatibility/python-state.json"
        ).read_text()
    )
    assert collect_state() == saved
