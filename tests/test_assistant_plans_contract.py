import json
from pathlib import Path

from scripts.capture_assistant_plans import collect_plans


def test_plans_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1] / "assistant-rs/compatibility/python-plans.json"
        ).read_text()
    )
    assert collect_plans() == saved
