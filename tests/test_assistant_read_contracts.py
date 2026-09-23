import json
from pathlib import Path

from scripts.capture_assistant_catalog import collect_catalog
from scripts.capture_assistant_catchup import collect_catchup


def test_catalog_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1] / "assistant-rs/compatibility/python-catalog.json"
        ).read_text()
    )
    assert collect_catalog() == saved


def test_catchup_oracle_matches_current_python_assistant_boundary():
    saved = json.loads(
        (
            Path(__file__).parents[1] / "assistant-rs/compatibility/python-catchup.json"
        ).read_text()
    )
    assert collect_catchup() == saved
