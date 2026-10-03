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
from nebula.v3.work import WorkCreate


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


def test_parent_gateway_checks_in_to_linked_project_only(tmp_path):
    async def scenario() -> None:
        store, parent, profile, runtime = _runtime(tmp_path)
        child = store.create(
            Engagement(name="linked_child", parent_engagement_id=parent.id)
        )
        unrelated = store.create(Engagement(name="Other project"))
        child_item = runtime.work.create(
            child.id,
            WorkCreate(title="Research status", status="in_progress"),
            actor_id="import",
        )
        other_item = runtime.work.create(
            unrelated.id, WorkCreate(title="Other status"), actor_id="import"
        )
        _chat, _chat_turn, turn = runtime.prepare_chat(
            engagement_id=parent.id,
            profile_id=profile.id,
            model=None,
            prompt="Review linked project",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
        )
        session = store.get(HarnessSession, turn.harness_session_id)
        runtime._active[session.id] = SimpleNamespace(
            turn_id=turn.id, connection=None, task=None
        )
        listed = await runtime._gateway_call(
            session, "work.list", {"project_name": child.name}
        )
        assert [item["id"] for item in listed["structuredContent"]["items"]] == [
            child_item.id
        ]
        assert (
            await runtime._gateway_call(
                session, "work.list", {"project_name": unrelated.name}
            )
        )["isError"] is True
        updated = await runtime._gateway_call(
            session,
            "work.check_in",
            {
                "item_id": child_item.id,
                "summary": "Reviewed the current task",
                "next_step": "Resolve the open lane",
                "request_id": "linked-review-1",
            },
        )
        assert updated["isError"] is False
        check_in = runtime.work.updates(child.id, child_item.id)[0]
        assert check_in.source_session_id == turn.chat_session_id
        assert check_in.source_engagement_id == parent.id
        assert (
            await runtime._gateway_call(
                session, "work.check_in", {"item_id": other_item.id, "summary": "No"}
            )
        )["isError"] is True
        runtime.work.set_enabled(child.id, False)
        assert (
            await runtime._gateway_call(
                session, "work.check_in", {"item_id": child_item.id, "summary": "No"}
            )
        )["isError"] is True
        assert len(runtime.work.updates(child.id, child_item.id)) == 1
        runtime._active.pop(session.id)

    asyncio.run(scenario())
