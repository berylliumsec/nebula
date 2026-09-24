import json
from pathlib import Path

from scripts.capture_assistant_decision_preparation import collect_decision_preparation


def test_decision_preparation_oracle_matches_python_source():
    root = Path(__file__).parents[1]
    assert collect_decision_preparation() == json.loads(
        (
            root / "assistant-rs/compatibility/python-decision-preparation.json"
        ).read_text()
    )
