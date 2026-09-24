import json
from pathlib import Path

from scripts.capture_assistant_goal_drafts import collect_goal_drafts


def test_goal_drafts_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1]
            / "assistant-rs/compatibility/python-goal-drafts.json"
        ).read_text()
    )
    assert collect_goal_drafts() == saved
