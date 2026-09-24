import json
from pathlib import Path

from scripts.capture_assistant_operator_help import collect_operator_help


def test_operator_help_oracle_matches_python_source():
    root = Path(__file__).parents[1]
    captured = collect_operator_help()
    assert captured == json.loads(
        (root / "assistant-rs/compatibility/python-operator-help.json").read_text()
    )
    assert (root / "src/nebula/v3/operator_help.md").read_bytes() == (
        root / "assistant-rs/crates/services/src/operator_help/corpus.md"
    ).read_bytes()
