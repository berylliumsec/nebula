import json
from pathlib import Path

from scripts.capture_assistant_execution import collect_execution


def test_execution_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1]
            / "assistant-rs/compatibility/python-execution.json"
        ).read_text()
    )
    assert collect_execution() == saved
