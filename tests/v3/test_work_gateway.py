"""Focused built-in Work gateway boundary."""

import asyncio
from types import SimpleNamespace

from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    Engagement,
    HarnessKind,
    HarnessNativeCapabilities,
    HarnessProfile,
    HarnessSession,
    WorkUpdate,
)
from nebula.v3.harnesses import HarnessRuntimeService, _harness_developer_instructions
from nebula.v3.storage import NebulaStore


def _runtime(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    engagement = store.create(Engagement(name="Release planning"))
    profile = store.create(
        HarnessProfile(
            name="Test harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="test-model",
            privacy={"local_only": True, "permits_sensitive_data": False},
        )
    )
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        adapter_factory=lambda _: None,
    )
    return store, engagement, profile, runtime


def test_builtin_work_gateway_is_default_on_scoped_and_retains_updates(tmp_path):
    async def scenario() -> None:
        store, engagement, profile, runtime = _runtime(tmp_path)
        _chat, _chat_turn, turn = runtime.prepare_chat(
            engagement_id=engagement.id,
            profile_id=profile.id,
            model=None,
            prompt="Prepare release notes",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
        )
        session = store.get(HarnessSession, turn.harness_session_id)
        assert {
            tool["name"] for tool in runtime._gateway_catalog(session)["tools"]
        } >= {
            "work.list",
            "work.create",
            "work.check_in",
        }
        instructions = _harness_developer_instructions(
            session,
            HarnessNativeCapabilities(),
            vendor="Codex",
            gateway_tools=tuple(runtime._gateway_catalog(session)["tools"]),
        )
        assert "Project Work is available" in instructions
        assert "minutes" not in instructions
        runtime._active[session.id] = SimpleNamespace(
            turn_id=turn.id, connection=None, task=None
        )
        created = await runtime._gateway_call(
            session,
            "work.create",
            {
                "title": "Prepare release notes",
                "status": "in_progress",
                "request_id": "work-1",
            },
        )
        assert created["isError"] is False, created
        item_id = created["structuredContent"]["item"]["id"]
        updated = await runtime._gateway_call(
            session,
            "work.check_in",
            {
                "item_id": item_id,
                "summary": "Outlined the changes",
                "next_step": "Review wording",
                "request_id": "check-1",
            },
        )
        assert updated["isError"] is False, updated
        assert (
            store.find_entities(
                WorkUpdate, {"item_id": item_id}, engagement_id=engagement.id
            )[0].source_session_id
            == turn.chat_session_id
        )
        runtime.work.set_enabled(engagement.id, False)
        assert not any(
            tool["name"].startswith("work.")
            for tool in runtime._gateway_catalog(session)["tools"]
        )
        denied = await runtime._gateway_call(
            session,
            "work.check_in",
            {
                "item_id": item_id,
                "summary": "Should be refused",
            },
        )
        assert denied["isError"] is True
        assert (
            len(
                store.find_entities(
                    WorkUpdate, {"item_id": item_id}, engagement_id=engagement.id
                )
            )
            == 1
        )
        runtime._active.pop(session.id)

    asyncio.run(scenario())
