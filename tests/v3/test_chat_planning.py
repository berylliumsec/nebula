"""Focused advisory planning and legacy pre-use gate contracts."""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from pathlib import Path
from types import SimpleNamespace

import pytest

from nebula.v3.chat_planning import (
    ChatPlanService,
    PlanSubmission,
    PlanningBlocked,
    advisory_plan_components,
)
from nebula.v3.browser_tools import combine_tool_components
from nebula.v3.chat import ChatCompletionRequest
from nebula.v3.chat_assistant_config import (
    ChatAssistantConfigPatch,
    update_chat_assistant_config,
)
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatBackend,
    ChatPlanDirective,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessNativeCapabilities,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnStatus,
    ProviderProfile,
    RiskClass,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    ClaudeAgentSdkAdapter,
    HarnessConfigurationError,
    HarnessPermissionRequest,
)
from nebula.v3.providers import ToolCall
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_results import WorkspaceOutputService
from nebula.v3.tools import ParallelismPolicy, ToolSpec
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response
from tests.v3.test_control_assistant_work import _app


def _submission(*tools: str, files: list[str] | None = None) -> PlanSubmission:
    return PlanSubmission(
        steps=[
            {
                "action": "Inspect the selected task",
                "expected_outcome": "Report the result",
            }
        ],
        tool_names=list(tools),
        files=files or [],
        no_file_work=not files,
    )


def _plan_call(*tools: str, call_id: str = "submit-plan") -> ToolCall:
    return ToolCall(
        id=call_id, name="plan.submit", arguments=_submission(*tools).model_dump()
    )


async def _enable(store: NebulaStore, prepared):
    prepared.turn = await store.update(
        ChatTurn,
        prepared.turn.id,
        {"request_snapshot": {"planning_enabled": True}},
        expected_revision=prepared.turn.revision,
    )


async def _enable_advisory(store: NebulaStore, service, prepared):
    prepared.turn = await store.update(
        ChatTurn,
        prepared.turn.id,
        {
            "request_snapshot": {
                "planning_enabled": True,
                "plan_behavior": "advisory",
            }
        },
        expected_revision=prepared.turn.revision,
    )
    components = prepared.tool_components
    prepared.tool_components = combine_tool_components(
        components,
        advisory_plan_components(
            service.plans, components.scope, Path(components.workspace)
        ),
    )


async def _mark_historical_enforced_turn(store: NebulaStore, harness_turn: HarnessTurn):
    chat_turn = await store.get(ChatTurn, harness_turn.chat_turn_id)
    snapshot = dict(chat_turn.request_snapshot)
    snapshot.pop("plan_behavior", None)
    return await store.update(
        ChatTurn,
        chat_turn.id,
        {"request_snapshot": snapshot},
        expected_revision=chat_turn.revision,
    )


def test_plan_only_mode_allows_advisory_planning():
    for mode in ("plan", "planning"):
        request = ChatCompletionRequest(
            backend=ChatBackend.HARNESS,
            harness_profile_id="harness",
            messages=[{"role": "user", "content": "Inspect the project."}],
            harness_mode=mode,
            planning_enabled=True,
        )
        assert request.planning_enabled is True


async def test_advisory_provider_uses_tools_without_sharing_a_plan(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[ToolCall(id="work", name="safe_read", arguments={"value": "early"})]
        ),
        _response(text="Done."),
    ]
    store, service, prepared, provider = await _prepared(tmp_path, responses, broker)
    (await _enable_advisory(store, service, prepared))

    result = await service.complete(prepared)

    assert result.message.content == "Done."
    assert [item.arguments["value"] for item in broker.calls] == ["early"]
    assert (await ChatPlanService(store).for_turn("turn")).phase == "discovery"
    assert {tool.name for tool in provider.requests[0].tools} >= {
        "safe_read",
        "plan.submit",
    }


async def test_advisory_provider_shares_plan_after_tool_and_continues(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[ToolCall(id="early", name="safe_read", arguments={"value": "early"})]
        ),
        _response(calls=[_plan_call("safe_read")]),
        _response(
            calls=[ToolCall(id="later", name="safe_read", arguments={"value": "later"})]
        ),
        _response(calls=[_plan_call("safe_read", call_id="revised-plan")]),
        _response(text="Done."),
    ]
    store, service, prepared, _ = await _prepared(tmp_path, responses, broker)
    (await _enable_advisory(store, service, prepared))

    result = await service.complete(prepared)

    assert result.message.content == "Done."
    assert [item.arguments["value"] for item in broker.calls] == ["early", "later"]
    plan = await ChatPlanService(store).for_turn("turn")
    assert plan is not None and plan.phase == "execution"
    assert plan.plan_version == 2
    assert plan.steps[0]["action"] == "Inspect the selected task"


async def test_claude_planner_fails_closed_without_preuse_hook(tmp_path, monkeypatch):
    sdk = SimpleNamespace(__version__="0.2.118")
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(lambda: sdk))
    profile = HarnessProfile(
        name="Claude",
        kind=HarnessKind.CLAUDE_AGENT_SDK,
        default_model="m",
    )
    session = HarnessSession(
        engagement_id="project",
        harness_profile_id=profile.id,
        model="m",
        metadata={"planning_target_turn_id": "task"},
    )

    async def unexpected_permission(_request):
        raise AssertionError("planning never reached a tool permission boundary")

    with pytest.raises(HarnessConfigurationError, match="pre-use hook"):
        (
            await ClaudeAgentSdkAdapter().open(
                AdapterOpenRequest(
                    profile=profile,
                    session=session,
                    workspace=tmp_path,
                    mcp_profiles=(),
                    credential_store=CredentialStore(),
                    permission_handler=unexpected_permission,
                )
            )
        )


async def test_provider_discovery_cannot_execute_and_saves_plan_before_tool(tmp_path):
    class GatedBroker(RecordingBroker):
        store: NebulaStore

        async def execute(self, invocation, scope, *, approval=None):
            plan = await ChatPlanService(self.store).for_turn(invocation.chat_turn_id)
            assert plan is not None and plan.phase == "execution"
            return await super().execute(invocation, scope, approval=approval)

    broker = GatedBroker()
    responses = [
        _response(
            calls=[ToolCall(id="early", name="safe_read", arguments={"value": "early"})]
        ),
        _response(calls=[_plan_call("safe_read")]),
        _response(
            calls=[ToolCall(id="work", name="safe_read", arguments={"value": "saved"})]
        ),
        _response(text="Done."),
    ]
    store, service, prepared, provider = await _prepared(tmp_path, responses, broker)
    broker.store = store
    (await _enable(store, prepared))

    result = await service.complete(prepared)

    assert result.message.content == "Done."
    assert [item.arguments["value"] for item in broker.calls] == ["saved"]
    assert (await ChatPlanService(store).for_turn("turn")).tool_names == ["safe_read"]
    assert {tool.name for tool in provider.requests[0].tools} == {"plan.submit"}
    assert provider.requests[1].tool_results[0].is_error is True
    assert "Core-validated task plan" in provider.requests[2].instructions
    assert (await store.get(ChatTurn, "turn")).usage.total_tokens == 12


async def test_provider_direct_answer_finishes_without_a_plan(tmp_path):
    broker = RecordingBroker()
    store, service, prepared, _ = await _prepared(
        tmp_path, [_response(text="No tools are needed.")], broker
    )
    (await _enable(store, prepared))

    result = await service.complete(prepared)

    assert result.message.content == "No tools are needed."
    assert broker.calls == []
    assert (
        await ChatPlanService(store).for_turn("turn")
    ).phase == "answered_without_tools"
    assert (await store.get(ChatTurn, "turn")).usage.total_tokens == 3


async def test_undeclared_provider_tool_replans_before_dispatch(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(calls=[_plan_call()]),
        _response(
            calls=[
                ToolCall(
                    id="undeclared", name="safe_read", arguments={"value": "blocked"}
                )
            ]
        ),
        _response(calls=[_plan_call("safe_read")]),
        _response(
            calls=[
                ToolCall(id="allowed", name="safe_read", arguments={"value": "allowed"})
            ]
        ),
        _response(text="Done."),
    ]
    store, service, prepared, _ = await _prepared(tmp_path, responses, broker)
    (await _enable(store, prepared))

    (await service.complete(prepared))

    assert [item.arguments["value"] for item in broker.calls] == ["allowed"]
    plan = await ChatPlanService(store).for_turn("turn")
    assert plan.phase == "execution" and plan.plan_version == 2


async def test_parallel_batch_is_fully_checked_before_any_tool_runs(tmp_path):
    broker = RecordingBroker()
    another = ToolSpec(
        name="safe_read2",
        description="Return another bounded value.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        parallelism=ParallelismPolicy.SAFE_READ,
    )
    responses = [
        _response(calls=[_plan_call("safe_read")]),
        _response(
            calls=[
                ToolCall(
                    id="early-allowed", name="safe_read", arguments={"value": "early"}
                ),
                ToolCall(
                    id="early-undeclared",
                    name="safe_read2",
                    arguments={"value": "early"},
                ),
            ]
        ),
        _response(calls=[_plan_call("safe_read", "safe_read2")]),
        _response(
            calls=[
                ToolCall(
                    id="allowed-1", name="safe_read", arguments={"value": "allowed-1"}
                ),
                ToolCall(
                    id="allowed-2", name="safe_read2", arguments={"value": "allowed-2"}
                ),
            ]
        ),
        _response(text="Done."),
    ]
    store, service, prepared, _ = await _prepared(
        tmp_path, responses, broker, extra_specs=[another], parallel_safe=True
    )
    (await _enable(store, prepared))

    (await service.complete(prepared))

    assert [item.arguments["value"] for item in broker.calls] == [
        "allowed-1",
        "allowed-2",
    ]
    assert (await ChatPlanService(store).for_turn("turn")).plan_version == 2


async def test_restart_reloads_main_gate_and_skips_existing_child_planning_snapshot(
    tmp_path,
):
    database = tmp_path / "plans.db"
    store = NebulaStore(database)
    project = await store.create(Engagement(name="Planning"))
    provider = await store.create(
        ProviderProfile(
            name="Local", provider_type="ollama", is_local=True, model_allowlist=["m"]
        )
    )
    parent = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Parent",
            provider_profile_id=provider.id,
            model="m",
        )
    )
    child = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Child",
            parent_session_id=parent.id,
            provider_profile_id=provider.id,
            model="m",
            metadata={"subagent_id": "child-1"},
        )
    )
    parent_turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=parent.id,
            provider_profile_id=provider.id,
            model="m",
            tools_enabled=True,
            request_snapshot={"planning_enabled": True},
        )
    )
    child_turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=child.id,
            provider_profile_id=provider.id,
            model="m",
            tools_enabled=True,
            request_snapshot={"planning_enabled": True},
        )
    )
    plans = ChatPlanService(store)
    first = await plans.ensure(parent_turn, parent)
    assert first is not None
    second = await plans.ensure(child_turn, child)
    assert second is None
    assert (await plans.required(child_turn, child)) is False

    reopened = ChatPlanService(NebulaStore(database))
    with pytest.raises(PlanningBlocked, match="pending a validated plan"):
        (await reopened.guard(parent_turn.id, "workspace.read"))
    (
        await reopened.submit(
            first,
            _submission("workspace.read"),
            available_tools={"workspace.read"},
            native_categories=set(),
            workspace=tmp_path,
        )
    )
    (await reopened.guard(parent_turn.id, "workspace.read"))
    assert (await reopened.required(child_turn, child)) is False


async def test_filename_discovery_is_bounded_and_paths_are_verified(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "match.txt").write_text("one")
    (tmp_path / "a.match.txt").write_text("between")
    (tmp_path / "b-match.txt").write_text("two")
    (tmp_path / "outside-link").symlink_to(tmp_path / "a" / "match.txt")
    finder = WorkspaceOutputService(tmp_path, max_files=1)
    assert finder.find(query="match")["incomplete"] is True
    first = WorkspaceOutputService(tmp_path).find(query="match", limit=1)
    second = WorkspaceOutputService(tmp_path).find(
        query="match", limit=1, after=first["after"]
    )
    third = WorkspaceOutputService(tmp_path).find(
        query="match", limit=1, after=second["after"]
    )
    assert first["matches"] == ["a.match.txt"]
    assert second["matches"] == ["a/match.txt"]
    assert third["matches"] == ["b-match.txt"]

    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Paths"))
    session = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Paths",
            provider_profile_id="provider",
            model="m",
        )
    )
    turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id="provider",
            model="m",
            tools_enabled=True,
            request_snapshot={"planning_enabled": True},
        )
    )
    plans = ChatPlanService(store)
    plan = await plans.ensure(turn, session)
    for path in ("../outside", "outside-link", "/tmp/absolute"):
        with pytest.raises(ValueError):
            (
                await plans.submit(
                    plan,
                    _submission("workspace.read", files=[path]),
                    available_tools={"workspace.read"},
                    native_categories=set(),
                    workspace=tmp_path,
                )
            )
    saved = await plans.submit(
        plan,
        _submission("workspace.read", files=["a/match.txt"]),
        available_tools={"workspace.read"},
        native_categories=set(),
        workspace=tmp_path,
    )
    assert saved.files == ["a/match.txt"]


def test_filename_discovery_reports_unreadable_subtree_as_incomplete(
    tmp_path, monkeypatch
):
    unreadable = tmp_path / "a"
    unreadable.mkdir()
    (unreadable / "match.txt").write_text("hidden from this scan")
    (tmp_path / "b-match.txt").write_text("visible")
    original_iterdir = Path.iterdir

    def iterdir(path):
        if path == unreadable:
            raise PermissionError("directory became unreadable")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    result = WorkspaceOutputService(tmp_path).find(query="match")
    assert result["matches"] == ["b-match.txt"]
    assert result["incomplete"] is True
    assert result["after"] is None


async def test_butler_force_is_revisioned_one_task_and_read_back(tmp_path):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    provider = await store.create(
        ProviderProfile(
            name="Local", provider_type="ollama", is_local=True, model_allowlist=["m"]
        )
    )
    session = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Agent",
            provider_profile_id=provider.id,
            model="m",
        )
    )
    work = app.state.control_assistant_work
    settings = work.chat_settings(project.id, session.id)
    assert settings["settings"]["planning_enabled"] is False
    saved = work.set_chat_settings(
        project.id,
        {
            "session_id": session.id,
            "expected_revision": settings["revision"],
            "settings": {"planning_enabled": True},
        },
    )
    assert (
        work.chat_settings(project.id, session.id)["settings"]["planning_enabled"]
        is True
    )
    session = await store.get(ChatSession, session.id)
    with pytest.raises(ValueError, match="target changed"):
        work.require_plan(
            project.id,
            {
                "session_id": session.id,
                "target_kind": "agent",
                "target_id": session.id,
                "expected_revision": saved["revision"] - 1,
                "reason": "Current Core evidence requires a fresh task plan.",
            },
        )
    receipt = work.require_plan(
        project.id,
        {
            "session_id": session.id,
            "target_kind": "agent",
            "target_id": session.id,
            "expected_revision": session.revision,
            "reason": "Current Core evidence requires a fresh task plan.",
        },
    )
    directive_id = receipt["directive"]["id"]
    assert (await store.get(ChatPlanDirective, directive_id)).status == "pending"
    turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=provider.id,
            model="m",
            tools_enabled=True,
            request_snapshot={"plan_behavior": "advisory"},
        )
    )
    plan = await ChatPlanService(store).ensure(turn, session)
    assert plan.directive_id == directive_id
    assert (await ChatPlanService(store).required(turn, session)) is False
    assert (await store.get(ChatPlanDirective, directive_id)).status == "applied"
    assert (await ChatPlanService(store).pending_directive(turn, session)) is None


async def test_active_harness_force_waits_for_next_turn(tmp_path):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        HarnessProfile(
            name="Harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="m",
            capabilities=HarnessCapabilities(models=["m"]),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    harness = app.state.harness_runtime_service
    session, active, harness_turn = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model="m",
        prompt="inspect",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    (
        await store.update(
            HarnessTurn,
            harness_turn.id,
            {"status": HarnessTurnStatus.RUNNING},
            expected_revision=harness_turn.revision,
        )
    )
    work = app.state.control_assistant_work
    receipt = work.require_plan(
        project.id,
        {
            "session_id": session.id,
            "target_kind": "agent",
            "target_id": session.id,
            "expected_revision": session.revision,
            "reason": "Current Core evidence requires a fresh task plan.",
        },
    )
    directive = await store.get(ChatPlanDirective, receipt["directive"]["id"])
    assert (
        receipt["timing"] == "next_turn"
        and directive.deferred_after_turn_id == active.id
    )
    assert (await ChatPlanService(store).required(active, session)) is False
    next_turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            backend=ChatBackend.HARNESS,
            model="m",
            tools_enabled=True,
            status=ChatTurnStatus.QUEUED,
            request_snapshot={"plan_behavior": "advisory"},
        )
    )
    assert (await ChatPlanService(store).requested(next_turn, session)) is True
    assert (await ChatPlanService(store).required(next_turn, session)) is False


async def test_queued_harness_force_applies_before_that_turn_starts(tmp_path):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        HarnessProfile(
            name="Harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="m",
            capabilities=HarnessCapabilities(models=["m"]),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    harness = app.state.harness_runtime_service
    session, queued, harness_turn = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model="m",
        prompt="inspect",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    assert harness_turn.status == HarnessTurnStatus.QUEUED
    receipt = app.state.control_assistant_work.require_plan(
        project.id,
        {
            "session_id": session.id,
            "target_kind": "turn",
            "target_id": queued.id,
            "expected_revision": queued.revision,
            "reason": "The queued turn needs a fresh tool inventory before starting.",
        },
    )
    directive = await store.get(ChatPlanDirective, receipt["directive"]["id"])
    assert receipt["timing"] == "before_turn"
    assert directive.deferred_after_turn_id is None
    assert (
        await ChatPlanService(store).ensure(queued, session)
    ).directive_id == directive.id


async def test_advisory_harness_planning_does_not_require_mcp_or_block(tmp_path):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        HarnessProfile(
            name="Harness",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
            default_model="m",
            capabilities=HarnessCapabilities(models=["m"], mcp=False),
            native_capabilities=HarnessNativeCapabilities(shell=True),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    harness = app.state.harness_runtime_service
    _, _, harness_turn = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model="m",
        prompt="inspect files",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        planning_enabled=True,
    )
    session = await store.get(HarnessSession, harness_turn.harness_session_id)
    planned = await harness._ensure_harness_plan(harness_turn, session)
    assert planned.id == harness_turn.id
    assert (
        await ChatPlanService(store).for_turn(harness_turn.chat_turn_id)
    ).phase == "discovery"
    assert session.metadata.get("core_plan_isolated") is not True
    assert session.id not in harness._connections


async def test_existing_grok_chat_rebinds_out_of_old_contained_session(tmp_path):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        HarnessProfile(
            name="Grok",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
            default_model="grok-test",
            capabilities=HarnessCapabilities(models=["grok-test"], mcp=True),
            native_capabilities=HarnessNativeCapabilities(shell=True),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    old_session = app.state.harness_runtime_service.create_session(
        engagement_id=project.id,
        profile_id=profile.id,
        model="grok-test",
        core_plan_isolated=True,
    )
    chat = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Research",
            backend=ChatBackend.HARNESS,
            harness_profile_id=profile.id,
            harness_session_id=old_session.id,
            model="grok-test",
            metadata={"planning_enabled": True},
        )
    )
    saved, _, turn = app.state.harness_runtime_service.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model="grok-test",
        prompt="Read AGENTS.md and plan the task",
        chat_session_id=chat.id,
        harness_session_id=old_session.id,
        mcp_server_ids=[],
        planning_enabled=True,
    )
    current = await store.get(HarnessSession, turn.harness_session_id)
    assert saved.id == chat.id
    assert current.id != old_session.id
    assert current.metadata.get("core_plan_isolated") is not True
    assert (await store.get(ChatTurn, turn.chat_turn_id)).request_snapshot[
        "plan_behavior"
    ] == "advisory"


async def test_advisory_grok_shares_plan_without_restricting_normal_tools(
    tmp_path, monkeypatch
):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        HarnessProfile(
            name="Grok planning fixture",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
            permission_mode="unrestricted",
            default_model="grok-test",
            capabilities=HarnessCapabilities(models=["grok-test"], mcp=True),
            native_capabilities=HarnessNativeCapabilities(shell=True),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    harness = app.state.harness_runtime_service
    _, chat_turn, harness_turn = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model="grok-test",
        prompt="Inspect files before acting",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        planning_enabled=True,
    )
    session = await store.get(HarnessSession, harness_turn.harness_session_id)
    assert session.metadata.get("core_plan_isolated") is not True
    assert "Planning is requested" in harness_turn.prompt
    planned = await harness._ensure_harness_plan(harness_turn, session)
    assert planned.id == harness_turn.id
    assert harness.plans.for_turn(chat_turn.id).phase == "discovery"
    monkeypatch.setattr(harness, "_active_gateway_turn", lambda _: harness_turn)
    offered = {
        item["name"]
        for item in harness._gateway_catalog(session, turn=harness_turn)["tools"]
    }
    assert {"plan.submit", "workspace.read"} <= offered
    submitted = await harness._gateway_call_unwrapped(
        session,
        "plan.submit",
        _submission("workspace.read").model_dump(),
    )
    assert submitted["structuredContent"]["phase"] == "execution"
    assert (
        harness.plans.for_turn(chat_turn.id).steps[0]["action"]
        == "Inspect the selected task"
    )
    assert session.metadata.get("planning_enforced") is not True


@pytest.mark.parametrize("permission_mode", ["managed", "unrestricted"])
async def test_codex_advisory_plan_shares_steps_with_normal_tools(
    tmp_path, monkeypatch, permission_mode
):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        HarnessProfile(
            name="Codex planning fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            permission_mode=permission_mode,
            default_model="codex-test",
            capabilities=HarnessCapabilities(models=["codex-test"], mcp=True),
            native_capabilities=HarnessNativeCapabilities(shell=True, web_search=True),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    harness = app.state.harness_runtime_service
    _, chat_turn, harness_turn = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model="codex-test",
        prompt="inspect files",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        planning_enabled=True,
    )
    session = await store.get(HarnessSession, harness_turn.harness_session_id)
    planned = await harness._ensure_harness_plan(harness_turn, session)
    assert planned.id == harness_turn.id
    assert harness.plans.for_turn(chat_turn.id).phase == "discovery"
    assert session.metadata.get("planning_enforced") is not True
    monkeypatch.setattr(harness, "_active_gateway_turn", lambda _: harness_turn)
    offered = {
        item["name"]
        for item in harness._gateway_catalog(session, turn=harness_turn)["tools"]
    }
    assert {"plan.submit", "workspace.read"} <= offered
    submitted = await harness._gateway_call_unwrapped(
        session, "plan.submit", _submission().model_dump()
    )
    assert submitted["structuredContent"]["phase"] == "execution"
    assert harness.plans.for_turn(chat_turn.id).phase == "execution"


async def test_harness_subagent_inherits_advisory_planning_without_a_gate(tmp_path):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        HarnessProfile(
            name="Harness",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
            default_model="m",
            capabilities=HarnessCapabilities(models=["m"], mcp=True),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    harness = app.state.harness_runtime_service
    vendor_session = harness.create_session(
        engagement_id=project.id, profile_id=profile.id, model="m", mcp_server_ids=[]
    )
    child = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Delegated child",
            backend=ChatBackend.HARNESS,
            harness_profile_id=profile.id,
            harness_session_id=vendor_session.id,
            model="m",
            metadata={"subagent_id": "child-1", "planning_enabled": True},
        )
    )
    _, chat_turn, _ = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model="m",
        prompt="Continue the delegated plan",
        chat_session_id=child.id,
        harness_session_id=vendor_session.id,
        mcp_server_ids=None,
        planning_enabled=True,
    )
    saved_child = await store.get(ChatSession, child.id)
    assert saved_child.metadata["planning_enabled"] is True
    assert (await ChatPlanService(store).required(chat_turn, saved_child)) is False
    assert (await ChatPlanService(store).requested(chat_turn, saved_child)) is True


async def test_linked_retry_of_blocked_plan_uses_saved_harness_choice(
    tmp_path, monkeypatch
):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    unsupported = await store.create(
        HarnessProfile(
            name="Unsupported",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
            default_model="grok-model",
            capabilities=HarnessCapabilities(models=["grok-model"]),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    managed = await store.create(
        HarnessProfile(
            name="Managed",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            executable="/bin/true",
            default_model="claude-model",
            capabilities=HarnessCapabilities(models=["claude-model"]),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    harness = app.state.harness_runtime_service
    chat, first_turn, failed = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=unsupported.id,
        model="grok-model",
        prompt="Inspect a file",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        planning_enabled=True,
    )
    plans = ChatPlanService(store)
    original_plan = await plans.ensure(first_turn, chat)
    (
        await plans.block(
            first_turn.id, "This adapter cannot enforce restricted discovery."
        )
    )
    (
        await store.update(
            ChatTurn,
            first_turn.id,
            {"status": ChatTurnStatus.FAILED},
            expected_revision=first_turn.revision,
        )
    )
    (
        await store.update(
            HarnessTurn,
            failed.id,
            {"status": HarnessTurnStatus.FAILED},
            expected_revision=failed.revision,
        )
    )
    chat = await update_chat_assistant_config(
        store,
        (await store.get(ChatSession, chat.id)),
        ChatAssistantConfigPatch(
            backend=ChatBackend.HARNESS,
            profile_id=managed.id,
            model="claude-model",
            planning_enabled=True,
        ),
        expected_revision=(await store.get(ChatSession, chat.id)).revision,
    )
    started: list[str] = []
    monkeypatch.setattr(harness, "start_chat_turn", started.append)

    replacement = await harness.retry_turn(failed.id)

    assert started == [replacement.id]
    assert replacement.metadata["retry_of_turn_id"] == failed.id
    replacement_session = await store.get(
        HarnessSession, replacement.harness_session_id
    )
    assert replacement_session.harness_profile_id == managed.id
    assert replacement_session.model == "claude-model"
    replacement_chat_turn = await store.get(ChatTurn, replacement.chat_turn_id)
    assert replacement_chat_turn.request_snapshot["planning_enabled"] is True
    assert replacement_chat_turn.id != first_turn.id
    assert (await plans.for_turn(first_turn.id)).id == original_plan.id
    assert (await plans.for_turn(first_turn.id)).phase == "blocked"
    assert (await plans.for_turn(replacement_chat_turn.id)) is None


async def test_unrestricted_claude_configuration_blocks_before_connection(tmp_path):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        HarnessProfile(
            name="Claude unrestricted",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            executable="/bin/true",
            default_model="m",
            capabilities=HarnessCapabilities(models=["m"]),
            native_capabilities=HarnessNativeCapabilities(shell=True),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    harness = app.state.harness_runtime_service
    _, _, harness_turn = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model="m",
        prompt="inspect",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        planning_enabled=True,
    )
    (await _mark_historical_enforced_turn(store, harness_turn))
    session = await store.get(HarnessSession, harness_turn.harness_session_id)
    session = await store.update(
        HarnessSession,
        session.id,
        {"metadata": {**session.metadata, "harness_permission_mode": "unrestricted"}},
        expected_revision=session.revision,
    )
    with pytest.raises(PlanningBlocked, match="cannot enforce restricted discovery"):
        (await harness._ensure_harness_plan(harness_turn, session))
    assert session.id not in harness._connections


async def test_managed_claude_uses_same_model_restricted_gateway_and_saved_plan(
    tmp_path, monkeypatch
):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        HarnessProfile(
            name="Claude",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            executable="/bin/true",
            default_model="claude-test",
            capabilities=HarnessCapabilities(models=["claude-test"], mcp=True),
            native_capabilities=HarnessNativeCapabilities(shell=True),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    harness = app.state.harness_runtime_service
    _, _, harness_turn = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model="claude-test",
        prompt="inspect files",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        planning_enabled=True,
    )
    (await _mark_historical_enforced_turn(store, harness_turn))
    session = await store.get(HarnessSession, harness_turn.harness_session_id)
    observed = {}

    async def planner(**kwargs):
        observed.update(kwargs)
        plan = harness.plans.for_turn(kwargs["planning_target_turn_id"])
        harness.plans.submit(
            plan,
            _submission(),
            available_tools=set(kwargs["planning_tool_names"]),
            native_categories=set(kwargs["planning_native_categories"]),
            workspace=tmp_path,
        )

    monkeypatch.setattr(harness, "analyze_structured", planner)
    planned_turn = await harness._ensure_harness_plan(harness_turn, session)
    assert observed["model"] == "claude-test"
    assert observed["nebula_tools"] is False
    assert observed["planning_target_turn_id"] == harness_turn.chat_turn_id
    assert "Core-validated task plan" in planned_turn.prompt
    planner_session = await store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id=profile.id,
            model="claude-test",
            metadata={
                "analysis_only": True,
                "planning_target_turn_id": harness_turn.chat_turn_id,
            },
        )
    )
    catalog = harness._gateway_catalog(planner_session)
    assert {item["name"] for item in catalog["tools"]} == {
        "workspace.find",
        "workspace.search",
        "workspace.read",
        "plan.submit",
    }


async def test_harness_gateway_and_native_permission_replan_before_effect(
    tmp_path, monkeypatch
):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        HarnessProfile(
            name="Claude",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            executable="/bin/true",
            default_model="m",
            capabilities=HarnessCapabilities(models=["m"], mcp=True),
            native_capabilities=HarnessNativeCapabilities(shell=True),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    harness = app.state.harness_runtime_service
    _, chat_turn, harness_turn = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model="m",
        prompt="inspect files",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        planning_enabled=True,
    )
    chat_turn = await _mark_historical_enforced_turn(store, harness_turn)
    session = await store.get(HarnessSession, harness_turn.harness_session_id)
    plans = ChatPlanService(store)
    plan = await plans.ensure(
        chat_turn, (await store.get(ChatSession, chat_turn.session_id))
    )
    (
        await plans.submit(
            plan,
            _submission("workspace.read"),
            available_tools={"workspace.read", "goal.block"},
            native_categories={"shell"},
            workspace=tmp_path,
        )
    )
    monkeypatch.setattr(harness, "_active_gateway_turn", lambda _: harness_turn)

    with pytest.raises(PlanningBlocked, match="goal.block was not declared"):
        (
            await harness._gateway_call_unwrapped(
                session, "goal.block", {"reason": "blocked"}
            )
        )
    assert (await plans.for_turn(chat_turn.id)).phase == "replanning"

    (
        await plans.submit(
            plan,
            _submission("workspace.read"),
            available_tools={"workspace.read"},
            native_categories={"shell"},
            workspace=tmp_path,
        )
    )
    with pytest.raises(
        PlanningBlocked, match="native capability shell was not declared"
    ):
        (
            await harness._request_permission(
                harness_turn.id,
                HarnessPermissionRequest(
                    vendor_request_id="native-1",
                    category="command",
                    vendor_name="Bash",
                    tool_name="Bash",
                    arguments={"command": "never executed"},
                ),
            )
        )
    assert (await plans.for_turn(chat_turn.id)).phase == "replanning"


pytestmark = pytest.mark.anyio
