"""A diagnostic Test turn records the vendor's own failure classification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import HarnessKind, HarnessProfile
from nebula.v3.harness_diagnostics import run_harness_test_turn
from nebula.v3.harnesses import HarnessTurnFailedError
from nebula.v3.storage import NebulaStore


class _SignedOutConnection:
    external_session_id = None

    async def run_turn(self, _prompt: str, *, model: str) -> Any:
        del model
        raise HarnessTurnFailedError(
            "Claude turn failed: Failed to authenticate: OAuth session expired",
            reason_code="authentication_failed",
        )
        yield  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        return None


async def test_claude_sign_in_failure_marks_authentication_failed(
    tmp_path: Path,
) -> None:
    store = NebulaStore(tmp_path / "nebula.db")
    profile = await store.create(
        HarnessProfile(
            name="Claude",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            default_model="sonnet",
        )
    )

    async def open_connection(_adapter: Any, _request: Any) -> _SignedOutConnection:
        return _SignedOutConnection()

    health = await run_harness_test_turn(
        store,
        profile.id,
        credentials=CredentialStore(),
        adapter_factory=lambda _kind: object(),
        open_connection=open_connection,
    )

    assert not health.healthy
    saved = await store.get(HarnessProfile, profile.id)
    assert saved.capabilities.last_turn_failure_reason == "authentication_failed"
    assert saved.capabilities.authentication_state == "failed"
    assert saved.capabilities.turn_state == "failed"


pytestmark = pytest.mark.anyio
