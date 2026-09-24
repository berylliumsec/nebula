"""Diff-bound provider protocol oracle; no network or Core lifespan."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from scripts.capture_assistant_provider_protocol import capture
from nebula.v3 import providers


def test_committed_assistant_provider_protocol_matches_python_source():
    root = Path(__file__).resolve().parents[2]
    expected = json.loads(
        (root / "assistant-rs/compatibility/python-provider-protocol.json").read_text()
    )
    with patch.object(
        providers.uuid,
        "uuid4",
        return_value=UUID("00000000-0000-4000-8000-000000000001"),
    ):
        # The fixture records the JSON protocol representation, including
        # splitter tuples serialized as arrays.
        actual = json.loads(json.dumps(asyncio.run(capture())))
    assert actual == expected
