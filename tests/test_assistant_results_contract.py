import json
from pathlib import Path

from scripts.capture_assistant_results import collect_results


def test_results_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1] / "assistant-rs/compatibility/python-results.json"
        ).read_text()
    )
    assert collect_results() == saved
