from __future__ import annotations

import asyncio

import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import BUTLER_TOOL_NAMES, ButlerCapabilities
from nebula.v3.domain import ChatSession, Engagement, ProviderProfile
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore


def _capabilities(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    app = create_app(
        store, auth_token="test-token", artifact_store=artifacts, tool_platform=runtime
    )
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    provider = store.create(
        ProviderProfile(
            name="Local", provider_type="ollama", is_local=True,
            model_allowlist=["model-a"],
        )
    )
    own_chat = store.create(
        ChatSession(
            engagement_id=selected.id, title="Research agent",
            provider_profile_id=provider.id, model="model-a",
        )
    )
    foreign_chat = store.create(
        ChatSession(
            engagement_id=other.id, title="Foreign agent",
            provider_profile_id=provider.id, model="model-a",
        )
    )
    capabilities = ButlerCapabilities(
        store, artifacts, selected.id, work=app.state.control_assistant_work
    )
    return store, selected, own_chat, foreign_chat, capabilities


def test_butler_discovers_chat_control_without_advertising_every_schema(tmp_path):
    _, _, own_chat, _, capabilities = _capabilities(tmp_path)
    assert {plugin.spec.name for plugin in capabilities.plugins()} == BUTLER_TOOL_NAMES

    async def scenario():
        found = await capabilities.search({"query": "send a message to a chat"})
        assert "nebula.work.message_chat" in {
            item["operation"] for item in found["matches"]
        }
        for query, operation in (
            ("fork a chat", "nebula.work.fork_chat"),
            ("delete a chat", "nebula.work.delete_chat"),
            ("retry failed chat turn", "nebula.work.retry_chat_turn"),
        ):
            matches = await capabilities.search({"query": query})
            assert operation in {item["operation"] for item in matches["matches"]}
            exact = await capabilities.describe({"operation": operation})
            assert exact["invoke_with"] == "nebula.capabilities.change"
        approvals = await capabilities.search({"query": "approve an agent request"})
        assert approvals["matches"][0]["operation"] == "nebula.work.decide_approval"
        assert approvals["next_offset"] is not None
        next_page = await capabilities.search({
            "query": "approve an agent request",
            "offset": approvals["next_offset"],
        })
        assert next_page["matches"]
        assert next_page["matches"][0] != approvals["matches"][0]
        described = await capabilities.describe(
            {"operation": "nebula.work.message_chat"}
        )
        assert described["invoke_with"] == "nebula.capabilities.change"
        assert "session_id" in described["input_schema"]["properties"]
        chat = await capabilities.invoke(
            {"operation": "nebula.work.chat", "arguments": {"session_id": own_chat.id}},
            change=False,
        )
        assert chat["title"] == "Research agent"
        assert chat["kind"] == "main"

    asyncio.run(scenario())


def test_butler_change_routes_validate_schema_revision_and_project(tmp_path):
    store, _, own_chat, foreign_chat, capabilities = _capabilities(tmp_path)

    async def scenario():
        operation = "nebula.work.update_chat"
        payload = {
            "operation": operation,
            "arguments": {
                "session_id": own_chat.id,
                "expected_revision": own_chat.revision,
                "title": "Sources review",
            },
        }
        with pytest.raises(ValueError, match="capabilities.change"):
            await capabilities.invoke(payload, change=False)
        with pytest.raises(ValueError, match="exact schema"):
            await capabilities.invoke(
                {"operation": operation, "arguments": {"session_id": own_chat.id}},
                change=True,
            )
        with pytest.raises(ValueError, match="project's work roster"):
            await capabilities.invoke(
                {"operation": "nebula.work.chat", "arguments": {"session_id": foreign_chat.id}},
                change=False,
            )
        saved = await capabilities.invoke(payload, change=True)
        assert saved["title"] == "Sources review"
        assert saved["butler_operation"] == operation
        assert saved["butler_project_id"] == own_chat.engagement_id
        assert store.get(ChatSession, own_chat.id).title == "Sources review"
        with pytest.raises(ValueError, match="current revision"):
            await capabilities.invoke(payload, change=True)

    asyncio.run(scenario())


def test_butler_browser_action_is_not_reported_as_saved_state(tmp_path):
    _, _, _, _, capabilities = _capabilities(tmp_path)

    async def scenario():
        described = await capabilities.describe(
            {"operation": "nebula.settings.set_device_theme"}
        )
        assert described["invoke_with"] == "nebula.capabilities.change"
        result = await capabilities.invoke(
            {
                "operation": "nebula.settings.set_device_theme",
                "arguments": {"theme": "light"},
            },
            change=True,
        )
        assert result["state"] == "pending_browser_apply"
        assert result["butler_operation"] == "nebula.settings.set_device_theme"

    asyncio.run(scenario())
