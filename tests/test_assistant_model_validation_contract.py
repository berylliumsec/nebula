import json
from pathlib import Path

from scripts.capture_assistant_model_validation import collect_model_validation


def test_model_validation_oracle_matches_current_python_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1]
            / "assistant-rs/compatibility/python-model-validation.json"
        ).read_text()
    )
    assert collect_model_validation() == saved
