import json
from pathlib import Path

from scripts.capture_assistant_scope_policy import collect_scope_policy


def test_scope_policy_oracle_matches_python_source():
    root = Path(__file__).parents[1]
    captured = collect_scope_policy()
    assert captured == json.loads(
        (root / "assistant-rs/compatibility/python-scope-policy.json").read_text()
    )
    assert captured["static_data"] == json.loads(
        (
            root / "assistant-rs/crates/domain/src/scope_policy/static_data.json"
        ).read_text()
    )
