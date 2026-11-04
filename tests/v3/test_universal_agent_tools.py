"""Runtime-independent Core tools, immutable guards and mixed delegation."""

import asyncio
import json
from pathlib import Path

import pytest

from nebula.v3.chat import ChatCompletionRequest, ChatRequestMessage
from nebula.v3.chat_assistant_config import (
    ChatAssistantConfigPatch,
    update_chat_assistant_config,
)
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    HarnessSession,
    HarnessTurn,
    HarnessTurnStatus,
    NativeHookExecution,
    ToolCallOrigin,
    ProviderProfile,
)
from nebula.v3.tools import ToolInvocation
from nebula.v3.working_notes import read_working_notes
from tests.v3.test_harness_provider_subagents import _setup, _prepare, _payload, _until


def hook(
    root: Path,
    identity: str = "guard",
    *,
    fail: str | None = None,
    events: list[str] | None = None,
) -> None:
    directory = root / ".agents/hooks" / identity
    directory.mkdir(parents=True)
    command = directory / "run.py"
    command.write_text(
        "#!/usr/bin/env python3\nimport json, sys\nevent=json.load(sys.stdin)\n"
        + (
            f"sys.exit(1 if event['event']=={fail!r} else 0)\n"
            if fail
            else "sys.exit(0)\n"
        )
    )
    command.chmod(0o700)
    (directory / "hook.json").write_text(
        json.dumps(
            {
                "version": 1,
                "name": identity,
                "events": events or ["chat.turn.started", "chat.turn.completed"],
                "command": ["run.py"],
                "side_effects": "none",
                "failure_policy": "block",
            }
        )
    )


@pytest.mark.parametrize("parent_backend", ["provider", "harness"])
@pytest.mark.parametrize("child_backend", ["provider", "harness"])
def test_all_parent_child_combinations_preserve_guard_receipts(
    tmp_path, parent_backend, child_backend
):
    async def scenario():
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        chat.workspace_resolver = lambda _: tmp_path
        hook(tmp_path)
        setting = (
            {"harness_profile_id": harness.id, "model": "gpt-test"}
            if child_backend == "harness"
            else {"provider_profile_id": "provider", "model": "model-a"}
        )
        child_provider = chat.provider_factory(store.get(ProviderProfile, "provider"))
        child_provider.answers = ["Guarded work complete."]
        if parent_backend == "harness":
            parent, parent_turn, _ = _prepare(
                runtime, project, harness, "Delegate", setting=setting
            )
        else:
            parent = store.create(
                ChatSession(
                    engagement_id=project.id,
                    title="Provider parent",
                    provider_profile_id="provider",
                    model="model-a",
                )
            )
            parent_turn = store.create(
                ChatTurn(
                    engagement_id=project.id,
                    session_id=parent.id,
                    provider_profile_id="provider",
                    model="model-a",
                    tools_enabled=True,
                    request_snapshot={
                        "allow_subagents": True,
                        "provider_subagent": setting,
                    },
                )
            )
        invocation = ToolInvocation(
            engagement_id=project.id,
            run_id=parent_turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=parent.id,
            chat_turn_id=parent_turn.id,
            tool_name="start_subagent",
            arguments={},
            workspace=tmp_path,
            idempotency_key="guarded-child",
        )
        child = await chat.subagents.start(
            invocation,
            task="Run the guarded task",
            name="Guarded",
            context=None,
            capabilities={"hook_ids": ["guard"]},
        )
        await _until(
            lambda: (
                store.get(ChatSubagent, child.id).status != ChatSubagentStatus.RUNNING
            )
        )
        saved = store.get(ChatSubagent, child.id)
        assert saved.status == ChatSubagentStatus.COMPLETED, saved.error
        session = store.get(ChatSession, saved.child_session_id)
        assert session.backend.value == child_backend
        assert session.parent_session_id == parent.id
        executions = store.list_session_entities(NativeHookExecution, session.id)
        assert {(item.event_name, item.status) for item in executions} == {
            ("chat.turn.started", "complete"),
            ("chat.turn.completed", "complete"),
        }
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("failed_event", ["chat.turn.started", "chat.turn.completed"])
def test_harness_required_guard_failure_cannot_publish_success(tmp_path, failed_event):
    async def scenario():
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        hook(tmp_path, fail=failed_event)
        parent, _, turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="Guard this response",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            hook_ids=["guard"],
        )
        events = [event async for event in runtime.stream_turn(turn.id)]
        saved = store.get(HarnessTurn, turn.id)
        assert saved.status == HarnessTurnStatus.INTERRUPTED
        assert "guard" in saved.error
        assert not [
            item
            for item in store.list_session_entities(ChatMessage, parent.id)
            if item.role == ChatRole.ASSISTANT
        ]
        assert not [
            item for item in events if item.type in {"message_delta", "completed"}
        ]
        if failed_event == "chat.turn.started":
            assert adapter.opens == []
        assert any(
            item.status == "failed"
            for item in store.list_session_entities(NativeHookExecution, parent.id)
        )
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_core_tools_execute_and_notes_survive_next_turn(tmp_path, monkeypatch):
    async def scenario():
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        from nebula.v3.research_results import ResearchResultService

        monkeypatch.setattr(
            ResearchResultService,
            "query",
            lambda self, action, arguments: {
                "action": action,
                "handle": arguments["handle"],
                "results": [],
            },
        )

        async def script(connection, prompt):
            catalog = runtime._gateway_catalog(
                connection.request.session,
                turn=runtime._active_gateway_turn(connection.request.session.id),
            )["tools"]
            assert {
                "notes.write",
                "conversation.search",
                "research_result_catalog",
                "research_result_search",
                "research_result_read",
            } <= {item["name"] for item in catalog}
            note_result = await connection.call(
                "notes.write", content="Remember the guarded result."
            )
            assert not note_result["isError"], note_result
            result = await connection.call(
                "research_result_catalog", handle="sim:2026-11-04T120000Z-12345678"
            )
            assert result["structuredContent"]["action"] == "catalog", result
            return "Done"

        adapter.script = script
        parent, _, turn = _prepare(runtime, project, harness, "Inspect")
        await runtime.start_chat_turn(turn.id)
        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE, (
            store.get(HarnessTurn, turn.id).error
        )
        assert (
            read_working_notes(store, parent.id).content
            == "Remember the guarded result."
        )
        _, _, next_turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="Continue",
            chat_session_id=parent.id,
            harness_session_id=None,
            mcp_server_ids=[],
        )
        assert "Remember the guarded result." in next_turn.prompt
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_child_capabilities_are_paginated_and_launch_is_unambiguous(tmp_path):
    async def scenario():
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        chat.workspace_resolver = lambda _: tmp_path
        for index in range(13):
            hook(tmp_path, f"guard-{index:02}")

        async def script(connection, prompt):
            initial = _payload(await connection.call("subagent.capabilities"))
            assert initial["subagents"]["available"] is True
            assert initial["subagents"]["can_start"] is True
            page = _payload(
                await connection.call(
                    "subagent.capabilities", category="hooks", limit=2
                )
            )
            assert len(page["items"]) == 2
            assert page["total"] == 13
            cursor = page["continuation_cursor"]
            following = _payload(
                await connection.call(
                    "subagent.capabilities", category="hooks", cursor=cursor, limit=2
                )
            )
            assert {item["id"] for item in page["items"]}.isdisjoint(
                item["id"] for item in following["items"]
            )
            return "Catalog read"

        adapter.script = script
        _, _, turn = _prepare(
            runtime,
            project,
            harness,
            "Read catalog",
            setting={"harness_profile_id": harness.id, "model": "gpt-test"},
        )
        await runtime.start_chat_turn(turn.id)
        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE, (
            store.get(HarnessTurn, turn.id).error
        )
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_assistant_config_saves_hooks_and_provider_parent_runtime(tmp_path):
    store, project, harness, chat, adapter, runtime = _setup(tmp_path)
    hook(tmp_path)
    harness = store.update(
        type(harness),
        harness.id,
        {
            "capabilities": harness.capabilities.model_copy(
                update={"models": ["gpt-test"]}
            )
        },
        expected_revision=harness.revision,
    )
    parent, _, _ = _prepare(runtime, project, harness, "Saved settings")
    # This boundary used to reject a valid hook simply because of the backend.
    updated = update_chat_assistant_config(
        store,
        parent,
        ChatAssistantConfigPatch(hook_ids=["guard"]),
        expected_revision=parent.revision,
        workspace_resolver=lambda _: tmp_path,
    )
    assert updated.metadata["assistant_config"]["hook_ids"] == ["guard"]


@pytest.mark.parametrize("narrowed", [False, True])
def test_harness_discovers_and_calls_unselected_mcp_with_shared_policy(
    tmp_path, monkeypatch, narrowed
):
    async def scenario():
        from nebula.v3.domain import Engagement, McpServerProfile, utc_now
        from nebula.v3.runtime_platform import RuntimePlatform

        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        store.update(
            Engagement,
            project.id,
            {"workspace_path": str(tmp_path)},
            expected_revision=project.revision,
        )
        platform = RuntimePlatform(
            store=store,
            artifact_store=runtime.artifact_store,
            data_root=tmp_path / "runtime",
        )
        platform.bind_mcp_service(runtime.mcp_service)
        runtime.bind_tool_platform(platform)
        source = store.create(
            McpServerProfile(
                id="available-source",
                enabled=True,
                name="Synthetic-status",
                transport="streamable_http",
                url="https://fixture.invalid/mcp",
                capabilities={
                    "checked_at": utc_now(),
                    "tools": [
                        {
                            "name": "status",
                            "description": "Read synthetic status",
                            "read_only": True,
                            "destructive": False,
                            "idempotent": True,
                            "open_world": False,
                            "credentialed": False,
                            "annotations_complete": True,
                        }
                    ],
                },
            )
        )
        called = []

        async def remote(profile, **kwargs):
            called.append((profile.id, kwargs["tool_name"]))
            return {
                "content": [{"type": "text", "text": "synthetic ready"}],
                "isError": False,
            }

        monkeypatch.setattr(runtime.mcp_service, "call_tool", remote)

        async def script(connection, prompt):
            catalog = runtime._gateway_catalog(
                connection.request.session,
                turn=runtime._active_gateway_turn(connection.request.session.id),
            )["tools"]
            assert {
                "tool_catalog.search",
                "tool_catalog.load",
                "tool_catalog.call",
            } <= {item["name"] for item in catalog}
            matches = _payload(
                await connection.call("tool_catalog.search", query="synthetic status")
            )["matches"]
            name = matches[0]["name"]
            unloaded = await connection.call(
                "tool_catalog.call", name=name, arguments={}
            )
            assert unloaded["isError"]
            assert called == []
            assert not (await connection.call("tool_catalog.load", names=[name]))[
                "isError"
            ]
            result = await connection.call("tool_catalog.call", name=name, arguments={})
            assert not result["isError"], result
            assert result["structuredContent"]["schema"] == "nebula.tool-result/v2"
            return "Catalog worked"

        adapter.script = script
        from nebula.v3.mcp import mcp_tool_runtime_name

        _, _, turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="Discover",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            allowed_tool_names=[mcp_tool_runtime_name(source.id, "status")]
            if narrowed
            else None,
        )
        await runtime.start_chat_turn(turn.id)
        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE, (
            store.get(HarnessTurn, turn.id).error
        )
        assert called == [(source.id, "status")]
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_narrowed_tools_and_changed_guard_fail_before_task_work(tmp_path):
    async def scenario():
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        hook(tmp_path)
        _, _, turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="Guarded",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            hook_ids=["guard"],
            allowed_tool_names=[],
        )
        session = store.get(HarnessSession, turn.harness_session_id)
        names = {
            item["name"]
            for item in runtime._gateway_catalog(session, turn=turn)["tools"]
        }
        assert not {"workspace.read", "notes.write", "work.create"} & names
        command = tmp_path / ".agents/hooks/guard/run.py"
        command.write_text(command.read_text() + "\n# changed after selection\n")
        events = [item async for item in runtime.stream_turn(turn.id)]
        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.INTERRUPTED
        assert adapter.opens == []
        assert not any(item.type == "completed" for item in events)
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("child_backend", ["provider", "harness"])
def test_provider_preparation_retains_independent_child_runtime(
    tmp_path, child_backend
):
    async def scenario():
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        chat.workspace_resolver = lambda _: tmp_path
        prepared = await chat.prepare_async(
            ChatCompletionRequest(
                provider_id="provider",
                engagement_id=project.id,
                model="model-a",
                messages=[
                    ChatRequestMessage(role="user", content="Delegate independently")
                ],
                allow_subagents=True,
                subagent_provider_id="provider"
                if child_backend == "provider"
                else None,
                subagent_harness_profile_id=harness.id
                if child_backend == "harness"
                else None,
                subagent_model="model-a" if child_backend == "provider" else "gpt-test",
            )
        )
        setting = prepared.turn.request_snapshot["provider_subagent"]
        assert setting["model"] == (
            "model-a" if child_backend == "provider" else "gpt-test"
        )
        assert (
            store.get(ChatSession, prepared.turn.session_id).metadata[
                "provider_subagent"
            ]
            == setting
        )
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_goal_reports_and_completion_share_ownership_and_commit(tmp_path):
    async def scenario():
        from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
        from nebula.v3.domain import ButlerAgentReport

        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        parent, _, initial = _prepare(runtime, project, harness, "Create conversation")
        await runtime.start_chat_turn(initial.id)
        goals = ChatGoalService(store)
        goal = goals.create(
            parent.id,
            GoalCreate(
                objective="Finish synthetic work",
                completion_criteria=["Evidence recorded"],
            ),
        )
        goal = goals.write(
            parent.id, GoalWrite(expected_revision=goal.revision, action="start")
        )

        async def script(connection, prompt):
            report = await connection.call(
                "report_progress",
                what_changed="Synthetic work finished",
                evidence_gained=["Inert fixture returned its result"],
                criterion_advanced="Evidence recorded",
                next_step="Complete the goal",
            )
            assert not report["isError"], report
            completed = await connection.call(
                "goal.complete",
                completion_summary="Synthetic work finished",
                completion_evidence=["Inert fixture returned its result"],
            )
            assert not completed["isError"], completed
            assert goals.get(parent.id).status.value == "running"
            return "Goal finished with recorded evidence."

        adapter.script = script
        _, owner, turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="Complete synthetic work",
            chat_session_id=parent.id,
            goal_id=goal.id,
            harness_session_id=None,
            mcp_server_ids=[],
        )
        await runtime.start_chat_turn(turn.id)
        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE, (
            store.get(HarnessTurn, turn.id).error
        )
        saved = goals.get(parent.id)
        assert saved.status.value == "completed"
        assert saved.completion_evidence[0]["turn_id"] == owner.id
        reports = store.list_entities(ButlerAgentReport)
        assert len(reports) == 1 and reports[0].source == "harness_adapter"
        assert store.get(ChatTurn, owner.id).final_message_id
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("allowed", [[], ["work_get", "list_agents"]])
def test_narrowed_harness_preserves_aliases_delegation_and_clamps_native_tools(
    tmp_path, allowed
):
    from nebula.v3.domain import HarnessNativeCapabilities
    from nebula.v3.harnesses import _session_native_capabilities

    store, project, harness, chat, adapter, runtime = _setup(tmp_path)
    from nebula.v3.chat_agent_messages import AgentMessageService

    runtime.bind_agent_messages(AgentMessageService(store))
    harness = store.update(
        type(harness),
        harness.id,
        {
            "kind": "claude_agent_sdk",
            "native_capabilities": HarnessNativeCapabilities(
                web_search=True,
                web_fetch=True,
                skills=True,
                subagents=True,
            ),
        },
        expected_revision=harness.revision,
    )
    _, _, turn = runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=harness.id,
        model=None,
        prompt="Confined child",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        allowed_tool_names=allowed,
        allow_agent_messaging=True,
        provider_subagent={"provider_profile_id": "provider", "model": "model-a"},
    )
    session = store.get(HarnessSession, turn.harness_session_id)
    names = {
        item["name"] for item in runtime._gateway_catalog(session, turn=turn)["tools"]
    }
    assert {
        "subagent.capabilities",
        "subagent.start",
        "subagent.wait",
        "subagent.list",
        "subagent.message",
        "subagent.stop",
    } <= names
    assert ("work.get" in names) == ("work_get" in allowed)
    assert ("agent.list" in names) == ("list_agents" in allowed)
    assert not {"work.create", "agent.send", "notes.write"} & names
    assert _session_native_capabilities(session, harness) == HarnessNativeCapabilities()


def test_retry_keeps_original_child_selections_even_if_saved_defaults_widen(tmp_path):
    async def scenario():
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        skill = tmp_path / ".agents/skills/review/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("Retain this exact selected review instruction.")
        parent, owner, turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="Restricted work",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            allowed_tool_names=["work_get"],
            allow_mcp_catalog=False,
            ssh_environment_ids=[],
            skill_selections=[{"name": "review", "path": str(skill)}],
        )
        runtime._fail_turn(
            turn.id, HarnessTurnStatus.INTERRUPTED, "Synthetic disconnect"
        )
        parent = store.get(ChatSession, parent.id)
        store.update(
            ChatSession,
            parent.id,
            {"metadata": {**parent.metadata, "core_tool_selection": {}}},
            expected_revision=parent.revision,
        )
        replacement = await runtime.retry_turn(turn.id)
        await runtime.start_chat_turn(replacement.id)
        snapshot = store.get(ChatTurn, replacement.chat_turn_id).request_snapshot
        assert snapshot["allowed_tool_names"] == ["work_get"]
        assert snapshot["allow_mcp_catalog"] is False
        assert snapshot["ssh_environment_snapshot"] == []
        assert [item["path"] for item in snapshot["skill_snapshots"]] == [str(skill)]
        assert "Retain this exact selected review instruction." in replacement.prompt
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_selected_skill_instructions_and_resources_are_frozen(tmp_path):
    async def scenario():
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        skill = tmp_path / ".agents/skills/review/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "Follow the selected review procedure. Read [detail](detail.txt)."
        )
        detail = skill.parent / "detail.txt"
        detail.write_text("Immutable review detail")

        async def script(connection, prompt):
            assert "Follow the selected review procedure." in prompt
            result = await connection.call(
                "skill.read_resource", skill_path=str(skill), resource_path="detail.txt"
            )
            assert not result["isError"], result
            assert result["structuredContent"]["content"] == "Immutable review detail"
            detail.write_text("Changed after selection")
            changed = await connection.call(
                "skill.read_resource", skill_path=str(skill), resource_path="detail.txt"
            )
            assert changed["isError"]
            return "Read the selected skill"

        adapter.script = script
        _, _, turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="Use selected skill",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            allowed_tool_names=[],
            skill_selections=[{"name": "review", "path": str(skill)}],
        )
        await runtime.start_chat_turn(turn.id)
        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE, (
            store.get(HarnessTurn, turn.id).error
        )
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_rejects_duplicate_hooks_before_persisting_a_turn(tmp_path):
    from nebula.v3.harnesses import HarnessConfigurationError

    store, project, harness, chat, adapter, runtime = _setup(tmp_path)
    hook(tmp_path)
    with pytest.raises(HarnessConfigurationError, match="unique"):
        runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="No duplicate effects",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            hook_ids=["guard", "guard"],
        )
    assert store.list_entities(ChatTurn) == []
    assert store.list_entities(HarnessTurn) == []
    assert adapter.opens == []


@pytest.mark.parametrize(
    "exit_path", ["start_rejected", "connection_failed", "start_cancelled"]
)
def test_harness_early_exit_runs_terminal_hooks_and_finishes_observation(
    tmp_path, exit_path
):
    async def scenario():
        from nebula.v3.domain import WorkspaceProvenanceObservation

        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        hook(
            tmp_path,
            fail="chat.turn.started" if exit_path == "start_rejected" else None,
            events=[
                "chat.turn.started",
                "chat.turn.completed",
                "chat.turn.failed",
                "chat.turn.cancelled",
            ],
        )
        if exit_path == "connection_failed":

            async def broken_connection(request):
                raise OSError("Synthetic connection failure")

            adapter.open = broken_connection
        if exit_path == "start_cancelled":
            command = tmp_path / ".agents/hooks/guard/run.py"
            command.write_text(
                "#!/usr/bin/env python3\nimport json, sys, time\nfrom pathlib import Path\nevent=json.load(sys.stdin)\n"
                "if event['event']=='chat.turn.started':\n"
                " for _ in range(100):\n"
                "  if Path('release').exists(): break\n"
                "  time.sleep(.05)\n"
            )
        parent, _, turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="Finish observations",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            hook_ids=["guard"],
        )

        async def consume():
            return [item async for item in runtime.stream_turn(turn.id)]

        task = asyncio.create_task(consume())
        if exit_path == "start_cancelled":
            await _until(
                lambda: any(
                    item.status == "running"
                    for item in store.list_entities(NativeHookExecution)
                )
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            (tmp_path / ".agents/hooks/guard/release").touch()
        else:
            await task
        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.INTERRUPTED
        executions = store.list_session_entities(NativeHookExecution, parent.id)
        assert any(
            item.event_name == "chat.turn.failed" and item.status == "complete"
            for item in executions
        )
        assert not any(item.status == "running" for item in executions)
        observations = store.list_entities(WorkspaceProvenanceObservation)
        assert len(observations) == 1 and observations[0].status == "complete"
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_child_question_can_wait_for_its_harness_parent(tmp_path):
    async def scenario():
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)

        async def script(connection, prompt):
            if "You are a subagent." in prompt:
                reply = _payload(
                    await connection.call(
                        "message_parent",
                        message="Which synthetic dataset?",
                        wait_for_reply=True,
                    )
                )
                assert "Use fixture A." in json.dumps(reply)
                return "Used fixture A."
            started = _payload(
                await connection.call(
                    "subagent.start", task="Check the synthetic dataset"
                )
            )
            asked = _payload(await connection.call("subagent.wait"))
            assert asked["awaiting_your_reply"] == [started["subagent_id"]]
            await connection.call(
                "subagent.message",
                subagent_id=started["subagent_id"],
                message="Use fixture A.",
            )
            finished = _payload(await connection.call("subagent.wait"))
            assert finished["subagents"][0]["report"] == "Used fixture A."
            return "Answered the child"

        adapter.script = script
        _, _, turn = _prepare(
            runtime,
            project,
            harness,
            "Ask a child",
            setting={"harness_profile_id": harness.id, "model": "gpt-test"},
        )
        await asyncio.wait_for(runtime.start_chat_turn(turn.id), timeout=10)
        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE, (
            store.get(HarnessTurn, turn.id).error
        )
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("usage_reported", [False, True])
def test_completion_guard_failure_debits_known_usage_and_pauses_goal(
    tmp_path, usage_reported
):
    async def scenario():
        from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
        from nebula.v3.domain import ChatTokenUsage
        from nebula.v3.harnesses import HarnessEvent

        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        hook(tmp_path, fail="chat.turn.completed")
        harness = store.update(
            type(harness),
            harness.id,
            {
                "capabilities": harness.capabilities.model_copy(
                    update={"detailed_usage": True}
                ),
            },
            expected_revision=harness.revision,
        )
        parent, _, initial = _prepare(runtime, project, harness, "Create conversation")
        await runtime.start_chat_turn(initial.id)
        goals = ChatGoalService(store)
        goal = goals.create(
            parent.id,
            GoalCreate(
                objective="Synthetic guarded work",
                completion_criteria=["Guard passed"],
                token_budget=100,
            ),
        )
        goal = goals.write(
            parent.id, GoalWrite(expected_revision=goal.revision, action="start")
        )
        original_open = adapter.open

        async def open_connection(request):
            connection = await original_open(request)
            original_run = connection.run_turn

            async def run_with_usage(prompt, *, model, images=None):
                async for event in original_run(prompt, model=model, images=images):
                    if event.type == "completed" and usage_reported:
                        yield HarnessEvent(
                            type="usage",
                            usage=ChatTokenUsage(
                                input_tokens=20, output_tokens=10, total_tokens=30
                            ),
                        )
                    yield event

            connection.run_turn = run_with_usage
            return connection

        adapter.open = open_connection
        _, owner, turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="Reject the candidate",
            chat_session_id=parent.id,
            harness_session_id=None,
            goal_id=goal.id,
            mcp_server_ids=[],
            hook_ids=["guard"],
        )
        await runtime.start_chat_turn(turn.id)
        saved = goals.get(parent.id)
        assert saved.status.value == "paused"
        assert saved.usage.total_tokens == (30 if usage_reported else 0)
        assert bool(saved.metadata.get("usage_unknown")) is not usage_reported
        assert saved.execution_claim_id is None
        assert store.get(ChatTurn, owner.id).final_message_id is None
        assert (
            store.get(HarnessTurn, turn.id).usage.total_tokens
            == saved.usage.total_tokens
        )
        # Repeated cleanup never charges the same rejected response twice.
        runtime._account_terminal_chat_usage(
            turn, ChatTokenUsage(total_tokens=30), usage_reported=True
        )
        assert goals.get(parent.id).usage == saved.usage
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("resolution", ["operator", "recorded"])
def test_retry_does_not_replay_a_hook_with_an_unknown_external_effect(
    tmp_path, resolution
):
    from nebula.v3.domain import utc_now
    from nebula.v3.harnesses import HarnessConfigurationError

    store, project, harness, chat, adapter, runtime = _setup(tmp_path)
    hook(tmp_path)
    parent, owner, turn = runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=harness.id,
        model=None,
        prompt="Preserve unknown effects",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        hook_ids=["guard"],
    )
    runtime._fail_turn(turn.id, HarnessTurnStatus.INTERRUPTED, "Synthetic interruption")
    store.create(
        NativeHookExecution(
            engagement_id=project.id,
            chat_session_id=parent.id,
            chat_turn_id=owner.id,
            hook_id="guard",
            hook_snapshot=owner.request_snapshot["hook_snapshots"][0],
            event_name="chat.turn.started",
            status="interrupted",
            side_effects="external",
            started_at=utc_now(),
            completed_at=utc_now(),
        )
    )
    with pytest.raises(HarnessConfigurationError, match="unknown effect"):
        runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="Retry",
            chat_session_id=parent.id,
            harness_session_id=None,
            mcp_server_ids=[],
        )
    assert len(store.list_entities(HarnessTurn)) == 1
    assert adapter.opens == []
    # The shared operator API can reconcile the fenced receipt before retry.
    from nebula.v3.harness_hooks import finish_terminal_hooks

    asyncio.run(finish_terminal_hooks(runtime, turn.id))
    owner = store.get(ChatTurn, owner.id)
    unknown = owner.request_snapshot["recovery"]["unknown_hook_execution_ids"]
    assert len(unknown) == 1
    if resolution == "operator":
        chat.reconcile_interrupted_hook(
            owner.id,
            unknown[0],
            outcome="complete",
            detail="Verified synthetic effect",
            expected_revision=owner.revision,
        )
    else:
        from nebula.v3.domain import NativeHookLateOutcome

        execution = store.get(NativeHookExecution, unknown[0])
        store.update(
            NativeHookExecution,
            execution.id,
            {
                "late_outcome": NativeHookLateOutcome(
                    status="complete", exit_code=0, stdout="", stderr="", error=None
                ),
            },
            expected_revision=execution.revision,
        )
        chat.pending_turn(parent.id)
    assert (
        store.get(ChatTurn, owner.id).request_snapshot["recovery"]["required"] is False
    )
    _, _, retry = runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=harness.id,
        model=None,
        prompt="Retry after reconciliation",
        chat_session_id=parent.id,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    assert retry.id != turn.id


@pytest.mark.parametrize("confirmed", [False, True])
def test_remote_harness_does_not_receive_core_notes_or_skills_without_consent(
    tmp_path, confirmed
):
    from nebula.v3.harnesses import HarnessConfigurationError
    from nebula.v3.working_notes import write_working_notes

    store, project, harness, chat, adapter, runtime = _setup(tmp_path)
    harness = store.update(
        type(harness),
        harness.id,
        {
            "privacy": harness.privacy.model_copy(
                update={"local_only": False, "permits_sensitive_data": True}
            ),
        },
        expected_revision=harness.revision,
    )
    # Disable peer messaging to isolate Core data consent from its existing gate.
    parent, _, initial = runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=harness.id,
        model=None,
        prompt="Synthetic initial prompt",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        allow_agent_messaging=False,
    )
    runtime._fail_turn(initial.id, HarnessTurnStatus.INTERRUPTED, "Synthetic setup")
    write_working_notes(
        store,
        engagement_id=project.id,
        session_id=parent.id,
        content="Private retained Core note",
        turn_id=initial.chat_turn_id,
    )
    _, _, turn = runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=harness.id,
        model=None,
        prompt="Continue",
        chat_session_id=parent.id,
        harness_session_id=None,
        mcp_server_ids=[],
        allow_agent_messaging=False,
        allow_remote_mcp=confirmed,
    )
    assert ("Private retained Core note" in turn.prompt) is confirmed
    runtime._fail_turn(turn.id, HarnessTurnStatus.INTERRUPTED, "Synthetic setup")
    skill = tmp_path / ".agents/skills/review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Private selected Core skill")
    parameters = dict(
        engagement_id=project.id,
        profile_id=harness.id,
        model=None,
        prompt="Use selected skill",
        chat_session_id=parent.id,
        harness_session_id=None,
        mcp_server_ids=[],
        allow_agent_messaging=False,
        allow_remote_mcp=confirmed,
        skill_selections=[{"name": "review", "path": str(skill)}],
    )
    if confirmed:
        _, _, selected = runtime.prepare_chat(**parameters)
        assert "Private selected Core skill" in selected.prompt
    else:
        with pytest.raises(
            HarnessConfigurationError, match="explicit operator confirmation"
        ):
            runtime.prepare_chat(**parameters)
    assert adapter.opens == []
