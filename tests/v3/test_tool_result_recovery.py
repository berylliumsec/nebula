"""Large chat tool results retain an authorized retrieval path."""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from pathlib import Path

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatService
from nebula.v3.chat_subagents import subagent_specs
from nebula.v3.domain import (
    ChatSession,
    ChatTurn,
    Engagement,
    ProviderProfile,
    ToolCallOrigin,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_results import (
    MAX_EXCERPT_BYTES,
    ToolOutputAccessError,
    ToolOutputService,
    model_result_bytes,
    sanitize_model_history_result,
)
from nebula.v3.tools import ToolExecutionResult, ToolInvocation


async def _delivery(tmp_path: Path) -> tuple[ChatService, ToolInvocation, NebulaStore]:
    store = NebulaStore(tmp_path / "core.db")
    engagement = await store.create(Engagement(name="Delivery"))
    profile = await store.create(
        ProviderProfile(name="Local", provider_type="vllm", is_local=True)
    )
    session = await store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Manifest",
            provider_profile_id=profile.id,
            model="test-model",
        )
    )
    turn = await store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="test-model",
        )
    )
    chat = ChatService(store, artifact_store=ArtifactStore(tmp_path / "artifacts"))
    invocation = ToolInvocation(
        engagement_id=engagement.id,
        run_id=turn.id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=session.id,
        chat_turn_id=turn.id,
        tool_name="list_subagent_capabilities",
        workspace=tmp_path,
        requested_by="chat-assistant",
    )
    return chat, invocation, store


def _manifest() -> dict:
    result = {
        "tool_names": [f"capability.{i:04d}" for i in range(700)],
        "padding": "",
    }
    result["padding"] = "x" * (15_772 - model_result_bytes(result))
    assert model_result_bytes(result) == 15_772
    return result


async def test_oversized_capability_result_has_authorized_artifact_and_replays(
    tmp_path: Path,
) -> None:
    chat, invocation, store = await _delivery(tmp_path)
    manifest = _manifest()
    fields, waiting = await chat._tool_result_entry(
        ToolExecutionResult(output=manifest),
        invocation=invocation,
        spec=subagent_specs()["list_subagent_capabilities"],
        call_id="manifest-call",
    )
    delivered = json.loads(fields["provider_result"])
    assert waiting is False
    assert fields["status"] == "incomplete"
    assert len(fields["provider_result"].encode()) <= MAX_EXCERPT_BYTES
    assert delivered["artifact_id"] == fields["result_artifact_id"]
    assert delivered["next_actions"] == ["tool_output.read"]

    replayed = sanitize_model_history_result(
        fields["provider_result"],
        tool_call_id="manifest-call",
        tool_name=invocation.tool_name,
        trusted_result=True,
    )
    assert replayed == delivered
    service = ToolOutputService(store, chat.artifact_store)
    chunks = []
    cursor = None
    while True:
        page = await service.read(
            engagement_id=invocation.engagement_id,
            owner_id=invocation.run_id,
            artifact_id=delivered["artifact_id"],
            continuation_cursor=cursor,
        )
        assert model_result_bytes(page) <= MAX_EXCERPT_BYTES
        chunks.extend(item["text"] for item in page["lines"])
        cursor = page["continuation_cursor"]
        if cursor is None:
            break
    assert json.loads("".join(chunks)) == manifest
    with pytest.raises(ToolOutputAccessError):
        (
            await service.read(
                engagement_id=invocation.engagement_id,
                owner_id="other-turn",
                artifact_id=delivered["artifact_id"],
            )
        )


async def test_failed_large_result_artifact_write_is_explicitly_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, invocation, _ = await _delivery(tmp_path)

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic write failure")

    assert chat.artifact_store is not None
    monkeypatch.setattr(chat.artifact_store, "put_bytes_with_status", fail_write)
    fields, _ = await chat._tool_result_entry(
        ToolExecutionResult(output=_manifest()),
        invocation=invocation,
        spec=subagent_specs()["list_subagent_capabilities"],
        call_id="failed-manifest",
    )
    delivered = json.loads(fields["provider_result"])
    assert fields["status"] == "incomplete"
    assert fields["result_artifact_id"] is None
    assert delivered["status"] == "incomplete"
    assert delivered.get("artifact_id") is None
    assert "persistence failed" in delivered["warning"]


pytestmark = pytest.mark.anyio
