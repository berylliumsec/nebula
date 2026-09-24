import json
from pathlib import Path

from scripts.capture_assistant_settings import collect_settings


def test_settings_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1]
            / "assistant-rs/compatibility/python-settings.json"
        ).read_text()
    )
    assert collect_settings() == saved
