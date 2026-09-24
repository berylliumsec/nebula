import json
from pathlib import Path

from scripts.capture_assistant_turn_validation import collect_turn_validation


def test_turn_validation_oracle_matches_current_python_models():
    saved = json.loads(
        (
            Path(__file__).parents[1]
            / "assistant-rs/compatibility/python-turn-validation.json"
        ).read_text()
    )
    assert collect_turn_validation() == saved
