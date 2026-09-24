import json
from pathlib import Path

from scripts.capture_assistant_execution_context import collect_execution_context


def test_execution_context_oracle_matches_python_source():
    root = Path(__file__).parents[1]
    captured = collect_execution_context()
    assert captured == json.loads(
        (root / "assistant-rs/compatibility/python-execution-context.json").read_text()
    )
    assert captured["static_data"] == json.loads(
        (
            root / "assistant-rs/crates/services/src/execution_context/static_data.json"
        ).read_text()
    )
