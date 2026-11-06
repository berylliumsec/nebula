"""Global MCP discovery must not decode Notebook history or block Core's loop."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from nebula.v3 import chat_subagents, storage
from nebula.v3.chat_subagents import SubagentBroker
from nebula.v3.database import EntityRow
from nebula.v3.domain import (
    ChatSubagent,
    HarnessSession,
    McpApprovalMode,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    ProviderProfile,
    ScopePolicy,
    ToolCall,
    utc_now,
)
from nebula.v3.mcp import McpGatewaySession, catalog_mcp_profiles
from nebula.v3.mcp_gateway import GatewayClient
from nebula.v3.notebook_mcp import NotebookMcpTools
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import InvalidToolArguments, ToolInvocation
from tests.v3.test_harness_provider_subagents import _payload, _prepare, _setup


def _profile(identity, **fields):
    return McpServerProfile(
        id=identity,
        name=fields.pop("name", identity),
        transport="streamable_http",
        url="https://fixture.invalid/mcp",
        enabled=fields.pop("enabled", True),
        capabilities=fields.pop(
            "capabilities",
            McpCapabilitySnapshot(
                checked_at=utc_now(), tools=[McpToolSnapshot(name="read")]
            ),
        ),
        **fields,
    )


async def test_catalog_filters_before_hydration_and_reaches_past_notebook_pages(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "catalog.db")
    notebook = McpServerProfile(
        id="nebula-notebook-fixture",
        name="Notebook",
        transport="builtin",
        enabled=True,
        capabilities=NotebookMcpTools.capabilities(),
        metadata={"built_in": "notebook_v1", "engagement_id": "fixture"},
    )
    (
        await store.create_many(
            [
                notebook.model_copy(
                    update={
                        "id": f"nebula-notebook-{i}",
                        "metadata": {
                            "built_in": "notebook_v1",
                            "engagement_id": str(i),
                        },
                    }
                )
                for i in range(1001)
            ]
        )
    )
    expected = [
        _profile("global"),
        _profile("null-built-in", metadata={"built_in": None}),
        _profile("owned", metadata={"engagement_id": "project"}),
        _profile("empty-owner", metadata={"engagement_id": ""}),
        _profile("null-owner", metadata={"engagement_id": None}),
        _profile("other-built-in", metadata={"built_in": "fixture_v1"}),
    ]
    (
        await store.create_many(
            [
                *expected,
                _profile("selected"),
                _profile("disabled", enabled=False),
                _profile("unprobed", capabilities=McpCapabilitySnapshot()),
                _profile("foreign", metadata={"engagement_id": "other"}),
            ]
        )
    )
    # Even an unreadable excluded Notebook must never be decoded or validated.
    payload = notebook.model_dump(mode="json")
    payload["capabilities"] = "unreadable"
    async with store.database.engine.begin() as connection:
        (
            await connection.execute(
                update(EntityRow)
                .where(EntityRow.id == "nebula-notebook-0")
                .values(payload=payload)
            )
        )
    hydrated = []
    original = storage._row_to_entity

    def counted(row, model):
        hydrated.append(row.id)
        return original(row, model)

    monkeypatch.setattr(storage, "_row_to_entity", counted)
    result = await catalog_mcp_profiles(
        store, engagement_id="project", exclude={"selected"}
    )
    assert [profile.id for profile in result] == sorted(p.id for p in expected)
    assert set(hydrated) == {p.id for p in expected}
    assert len(hydrated) == len(expected)


async def test_catalog_has_no_horizon_even_when_more_than_1000_servers_are_usable(
    tmp_path,
):
    store = NebulaStore(tmp_path / "catalog.db")
    (await store.create_many([_profile(f"server-{i:04d}") for i in range(1003)]))
    assert [p.id for p in (await catalog_mcp_profiles(store))] == [
        f"server-{i:04d}" for i in range(1003)
    ]


async def test_catalog_reads_fresh_permissions_revisions_and_deletions(tmp_path):
    store = NebulaStore(tmp_path / "catalog.db")
    profile = await store.create(_profile("server"))
    assert (await catalog_mcp_profiles(store)) == (profile,)
    denied = await store.update(
        McpServerProfile,
        profile.id,
        {"tool_overrides": {"read": McpApprovalMode.DENY}},
        expected_revision=profile.revision,
    )
    assert (await catalog_mcp_profiles(store)) == ()
    assert (await catalog_mcp_profiles(store, bypass_permissions=True)) == (denied,)
    disabled = await store.update(
        McpServerProfile,
        profile.id,
        {"enabled": False},
        expected_revision=denied.revision,
    )
    assert (await catalog_mcp_profiles(store, bypass_permissions=True)) == ()
    (
        await store.delete(
            McpServerProfile, profile.id, expected_revision=disabled.revision
        )
    )
    assert (await catalog_mcp_profiles(store)) == ()


@pytest.mark.parametrize("entry", ["harness", "provider"])
async def test_capability_entry_points_keep_storage_and_sessions_off_the_loop(
    tmp_path, monkeypatch, entry
):
    async def scenario():
        store, project, harness, chat, _, runtime = await _setup(tmp_path)
        _, parent, turn = _prepare(runtime, project, harness, "Inspect capabilities")
        session = await store.get(HarnessSession, turn.harness_session_id)
        runtime._active[session.id] = SimpleNamespace(turn_id=turn.id, task=None)
        loop_thread = threading.get_ident()
        entered, release = threading.Event(), threading.Event()
        original = chat.subagents.available_capabilities

        async def delayed(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            entered.set()
            assert release.wait(5), "storage blocked the event loop"
            return await original(*args, **kwargs)

        monkeypatch.setattr(chat.subagents, "available_capabilities", delayed)
        measuring = True
        for method in ("get", "update", "reserve_tool_call"):
            selected = getattr(store, method)

            async def checked(*args, original=selected, **kwargs):
                if measuring:
                    assert threading.get_ident() != loop_thread
                return await original(*args, **kwargs)

            monkeypatch.setattr(store, method, checked)
        gateway = McpGatewaySession(
            list_tools=lambda _: {"tools": []},
            call_tool=lambda name, arguments: runtime._gateway_call(
                session, name, arguments
            ),
        )
        launch = await gateway.start()
        client = GatewayClient(launch.socket_path, launch.token)
        invocation = ToolInvocation(
            engagement_id=project.id,
            run_id=parent.id,
            chat_session_id=parent.session_id,
            chat_turn_id=parent.id,
            tool_name="list_subagent_capabilities",
            workspace=tmp_path,
        )
        task = asyncio.create_task(
            (
                await client.request(
                    "tools/call", {"name": "subagent.capabilities", "arguments": {}}
                )
            )
            if entry == "harness"
            else (
                await SubagentBroker(chat.subagents).execute(
                    invocation, ScopePolicy(engagement_id=project.id)
                )
            )
        )
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            assert not task.done()
            release.set()
            result = await task
            value = _payload(result) if entry == "harness" else result.output
            assert value["subagents"]["selected"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            measuring = False
            await client.close()
            await gateway.close()
            runtime._active.clear()
            await runtime.shutdown()
            await chat.shutdown()

    (await scenario())


@pytest.mark.parametrize(
    "state", ["disabled", "analysis", "planner", "invalid_arguments", "plan_required"]
)
async def test_gateway_capability_read_preserves_offer_and_planning_guards(
    tmp_path, state
):
    async def scenario():
        store, project, harness, chat, _, runtime = await _setup(tmp_path)
        _, parent, turn = _prepare(runtime, project, harness, "Inspect capabilities")
        session = await store.get(HarnessSession, turn.harness_session_id)
        runtime._active[session.id] = SimpleNamespace(turn_id=turn.id, task=None)
        metadata = dict(session.metadata)
        if state == "disabled":
            metadata.pop("provider_subagent", None)
        elif state == "analysis":
            metadata["analysis_only"] = True
        elif state == "planner":
            metadata["planning_target_turn_id"] = parent.id
        elif state == "plan_required":
            (
                await store.update(
                    type(parent),
                    parent.id,
                    {
                        "request_snapshot": {
                            **parent.request_snapshot,
                            "planning_enabled": True,
                            "plan_behavior": "enforced",
                        }
                    },
                    expected_revision=parent.revision,
                )
            )
        (
            await store.update(
                HarnessSession,
                session.id,
                {"metadata": metadata},
                expected_revision=session.revision,
            )
        )
        try:
            response = await runtime._gateway_call(
                session,
                "subagent.capabilities",
                {"unexpected": True} if state == "invalid_arguments" else {},
            )
            assert response["isError"] is True
            assert response["structuredContent"]["schema"] == "nebula.tool-failure/v1"
            assert not (await store.list_entities(ToolCall))
        finally:
            runtime._active.clear()
            await runtime.shutdown()
            await chat.shutdown()

    (await scenario())


@pytest.mark.parametrize("capabilities", [None, {}, {"mcp_server_ids": []}])
async def test_child_start_skips_catalog_when_no_mcp_override_needs_validation(
    tmp_path, monkeypatch, capabilities
):
    async def scenario():
        store, project, harness, chat, adapter, runtime = await _setup(tmp_path)
        chat.provider_factory(
            (await store.get(ProviderProfile, "provider"))
        ).answers = ["Done."]

        def unexpected(*args, **kwargs):
            raise AssertionError(
                "inherited or empty selection must not read the catalog"
            )

        monkeypatch.setattr(chat_subagents, "catalog_mcp_profiles", unexpected)

        async def script(connection, prompt):
            started = _payload(
                await connection.call(
                    "subagent.start", task="Synthetic child", capabilities=capabilities
                )
            )
            assert started["subagent_id"]
            result = _payload(await connection.call("subagent.wait"))
            assert result["subagents"][0]["report"] == "Done."
            return "Finished."

        adapter.script = script
        _, _, turn = _prepare(runtime, project, harness, "Delegate")
        try:
            await runtime.start_chat_turn(turn.id)
            assert (await store.count(ChatSubagent)) == 1
            assert (await store.list_entities(ChatSubagent))[
                0
            ].status.value == "completed"
        finally:
            await runtime.shutdown()
            await chat.shutdown()

    (await scenario())


async def test_explicit_child_mcp_selection_yields_and_rejects_foreign_profile(
    tmp_path, monkeypatch
):
    async def scenario():
        store, project, harness, chat, _, runtime = await _setup(tmp_path)
        _, parent, _ = _prepare(runtime, project, harness, "Delegate")
        (await store.create(_profile("owned", metadata={"engagement_id": project.id})))
        (await store.create(_profile("foreign", metadata={"engagement_id": "other"})))
        assert [
            p["id"]
            for p in (await chat.subagents.available_capabilities(parent.id))[
                "mcp_servers"
            ]
        ] == ["owned"]
        loop_thread = threading.get_ident()
        original = chat_subagents.catalog_mcp_profiles
        observed = []

        def checked(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            observed.append(kwargs["engagement_id"])
            return original(*args, **kwargs)

        monkeypatch.setattr(chat_subagents, "catalog_mcp_profiles", checked)
        invocation = ToolInvocation(
            engagement_id=project.id,
            run_id=parent.id,
            chat_session_id=parent.session_id,
            chat_turn_id=parent.id,
            tool_name="start_subagent",
            workspace=tmp_path,
        )
        try:
            with pytest.raises(InvalidToolArguments, match="not enabled and probed"):
                await chat.subagents.start(
                    invocation,
                    task="Synthetic child",
                    name=None,
                    context=None,
                    capabilities={"mcp_server_ids": ["foreign"]},
                )
            assert observed == [project.id]
            assert (await store.count(ChatSubagent)) == 0
        finally:
            await runtime.shutdown()
            await chat.shutdown()

    (await scenario())


async def test_concurrent_explicit_selections_recheck_idempotency_after_worker_read(
    tmp_path, monkeypatch
):
    async def scenario():
        store, project, harness, chat, _, runtime = await _setup(tmp_path)
        _, parent, _ = _prepare(
            runtime,
            project,
            harness,
            "Delegate",
            setting={"harness_profile_id": harness.id, "model": "gpt-test"},
        )
        (await store.create(_profile("external")))
        entered, guard, release = [], threading.Lock(), threading.Event()
        original = chat_subagents.catalog_mcp_profiles

        def held(*args, **kwargs):
            with guard:
                entered.append(True)
            assert release.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr(chat_subagents, "catalog_mcp_profiles", held)
        invocation = ToolInvocation(
            engagement_id=project.id,
            run_id=parent.id,
            chat_session_id=parent.session_id,
            chat_turn_id=parent.id,
            tool_name="start_subagent",
            workspace=tmp_path,
            idempotency_key="same-parent-call",
        )
        tasks = [
            asyncio.create_task(
                chat.subagents.start(
                    invocation,
                    task="Synthetic child",
                    name=None,
                    context=None,
                    capabilities={"mcp_server_ids": ["external"]},
                )
            )
            for _ in range(2)
        ]
        try:
            async with asyncio.timeout(2):
                while len(entered) < 2:
                    await asyncio.sleep(0.001)
            release.set()
            first, second = await asyncio.gather(*tasks)
            assert first.id == second.id
            assert (await store.count(ChatSubagent)) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await runtime.shutdown()
            await chat.shutdown()

    (await scenario())


pytestmark = pytest.mark.anyio
