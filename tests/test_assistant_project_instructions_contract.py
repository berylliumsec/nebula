import json
from pathlib import Path

from scripts.capture_assistant_project_instructions import collect_project_instructions


def test_project_instructions_oracle_matches_python_source():
    root = Path(__file__).parents[1]
    assert collect_project_instructions() == json.loads(
        (
            root / "assistant-rs/compatibility/python-project-instructions.json"
        ).read_text()
    )
