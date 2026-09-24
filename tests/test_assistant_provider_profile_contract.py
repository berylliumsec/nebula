import json
from pathlib import Path

from scripts.capture_assistant_provider_profile import collect_provider_profiles


def test_provider_profile_oracle_matches_python_source():
    root = Path(__file__).parents[1]
    assert collect_provider_profiles(root) == json.loads(
        (root / "assistant-rs/compatibility/python-provider-profile.json").read_text()
    )
