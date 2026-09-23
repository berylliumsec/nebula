import json
from pathlib import Path

from scripts.capture_assistant_navigation import collect_navigation


def test_navigation_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1]
            / "assistant-rs/compatibility/python-navigation.json"
        ).read_text()
    )
    assert collect_navigation() == saved
