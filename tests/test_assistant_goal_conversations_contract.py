import json
from pathlib import Path

from scripts.capture_assistant_goal_conversations import collect_goal_conversations


def test_goal_conversations_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1]
            / "assistant-rs/compatibility/python-goal-conversations.json"
        ).read_text()
    )
    assert collect_goal_conversations() == saved
