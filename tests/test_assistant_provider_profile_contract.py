import json
from pathlib import Path

from scripts.capture_assistant_provider_profile import collect_provider_profiles


def test_provider_profile_oracle_matches_python_source():
    root = Path(__file__).parents[1]
    captured = collect_provider_profiles(root)
    expected = json.loads(
        (root / "assistant-rs/compatibility/python-provider-profile.json").read_text()
    )
    # Retain the fixture's full interpreter provenance without treating a patch
    # release number as provider behavior. Every behavioral field still matches
    # exactly, including Unicode/locality tables and source hashes.
    captured_version = captured.pop("python_version")
    expected_version = expected.pop("python_version")
    assert captured_version.split(".")[:2] == expected_version.split(".")[:2] == [
        "3",
        "12",
    ]
    assert captured == expected
