import json
from pathlib import Path

from scripts.capture_assistant_subagents import collect_subagents


def test_subagents_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1]
            / "assistant-rs/compatibility/python-subagents.json"
        ).read_text()
    )
    assert collect_subagents() == saved
