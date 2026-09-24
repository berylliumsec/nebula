import json
from pathlib import Path

from scripts.capture_assistant_forks import collect_forks


def test_forks_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1] / "assistant-rs/compatibility/python-forks.json"
        ).read_text()
    )
    assert collect_forks() == saved
