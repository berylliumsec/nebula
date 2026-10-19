"""Focused durable task planning and pre-use gate contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nebula.v3.chat_planning import (
    ChatPlanService,
    PlanSubmission,
    PlanningBlocked,
)
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
    HarnessTurnOrigin,
    HarnessTurnStatus,
    ProviderProfile,
    RiskClass,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    ClaudeAgentSdkAdapter,
    HarnessConfigurationError,
    HarnessPermissionRequest,
    _session_native_capabilities,
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


def _plan_call(*tools: str) -> ToolCall:
    return ToolCall(
        id="submit-plan", name="plan.submit", arguments=_submission(*tools).model_dump()
    )


def _enable(store: NebulaStore, prepared):
    prepared.turn = store.update(
        ChatTurn,
        prepared.turn.id,
        {"request_snapshot": {"planning_enabled": True}},
        expected_revision=prepared.turn.revision,
    )


def test_plan_only_mode_rejects_enforced_planning():
    for mode in ("plan", "planning"):
        with pytest.raises(ValueError, match="cannot be combined"):
            ChatCompletionRequest(
                backend=ChatBackend.HARNESS,
                harness_profile_id="harness",
                messages=[{"role": "user", "content": "Inspect the project."}],
                harness_mode=mode,
                planning_enabled=True,
            )


def test_claude_planner_fails_closed_without_preuse_hook(tmp_path, monkeypatch):
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
        asyncio.run(
            ClaudeAgentSdkAdapter().open(
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


def test_provider_discovery_cannot_execute_and_saves_plan_before_tool(tmp_path):
    class GatedBroker(RecordingBroker):
        store: NebulaStore

        async def execute(self, invocation, scope, *, approval=None):
            plan = ChatPlanService(self.store).for_turn(invocation.chat_turn_id)
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
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)
    broker.store = store
    _enable(store, prepared)

    result = asyncio.run(service.complete(prepared))

    assert result.message.content == "Done."
    assert [item.arguments["value"] for item in broker.calls] == ["saved"]
    assert ChatPlanService(store).for_turn("turn").tool_names == ["safe_read"]
    assert {tool.name for tool in provider.requests[0].tools} == {"plan.submit"}
    assert provider.requests[1].tool_results[0].is_error is True
    assert "Core-validated task plan" in provider.requests[2].instructions
    assert store.get(ChatTurn, "turn").usage.total_tokens == 12


def test_provider_direct_answer_finishes_without_a_plan(tmp_path):
    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(
        tmp_path, [_response(text="No tools are needed.")], broker
    )
    _enable(store, prepared)

    result = asyncio.run(service.complete(prepared))

    assert result.message.content == "No tools are needed."
    assert broker.calls == []
    assert ChatPlanService(store).for_turn("turn").phase == "answered_without_tools"
    assert store.get(ChatTurn, "turn").usage.total_tokens == 3


def test_undeclared_provider_tool_replans_before_dispatch(tmp_path):
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
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)
    _enable(store, prepared)

    asyncio.run(service.complete(prepared))

    assert [item.arguments["value"] for item in broker.calls] == ["allowed"]
    plan = ChatPlanService(store).for_turn("turn")
    assert plan.phase == "execution" and plan.plan_version == 2


def test_parallel_batch_is_fully_checked_before_any_tool_runs(tmp_path):
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
    store, service, prepared, _ = _prepared(
        tmp_path, responses, broker, extra_specs=[another], parallel_safe=True
    )
    _enable(store, prepared)

    asyncio.run(service.complete(prepared))

    assert [item.arguments["value"] for item in broker.calls] == [
        "allowed-1",
        "allowed-2",
    ]
    assert ChatPlanService(store).for_turn("turn").plan_version == 2


def test_restart_reloads_main_gate_and_skips_existing_child_planning_snapshot(tmp_path):
    database = tmp_path / "plans.db"
    store = NebulaStore(database)
    project = store.create(Engagement(name="Planning"))
    provider = store.create(
        ProviderProfile(
            name="Local", provider_type="ollama", is_local=True, model_allowlist=["m"]
        )
    )
    parent = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Parent",
            provider_profile_id=provider.id,
            model="m",
        )
    )
    child = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Child",
            parent_session_id=parent.id,
            provider_profile_id=provider.id,
            model="m",
            metadata={"subagent_id": "child-1"},
        )
    )
    parent_turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=parent.id,
            provider_profile_id=provider.id,
            model="m",
            tools_enabled=True,
            request_snapshot={"planning_enabled": True},
        )
    )
    child_turn = store.create(
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
    first = plans.ensure(parent_turn, parent)
    assert first is not None
    second = plans.ensure(child_turn, child)
    assert second is None
    assert plans.required(child_turn, child) is False

    reopened = ChatPlanService(NebulaStore(database))
    with pytest.raises(PlanningBlocked, match="pending a validated plan"):
        reopened.guard(parent_turn.id, "workspace.read")
    reopened.submit(
        first,
        _submission("workspace.read"),
        available_tools={"workspace.read"},
        native_categories=set(),
        workspace=tmp_path,
    )
    reopened.guard(parent_turn.id, "workspace.read")
    assert reopened.required(child_turn, child) is False


def test_filename_discovery_is_bounded_and_paths_are_verified(tmp_path):
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
    project = store.create(Engagement(name="Paths"))
    session = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Paths",
            provider_profile_id="provider",
            model="m",
        )
    )
    turn = store.create(
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
    plan = plans.ensure(turn, session)
    for path in ("../outside", "outside-link", "/tmp/absolute"):
        with pytest.raises(ValueError):
            plans.submit(
                plan,
                _submission("workspace.read", files=[path]),
                available_tools={"workspace.read"},
                native_categories=set(),
                workspace=tmp_path,
            )
    saved = plans.submit(
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


def test_butler_force_is_revisioned_one_task_and_read_back(tmp_path):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    provider = store.create(
        ProviderProfile(
            name="Local", provider_type="ollama", is_local=True, model_allowlist=["m"]
        )
    )
    session = store.create(
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
    session = store.get(ChatSession, session.id)
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
    assert store.get(ChatPlanDirective, directive_id).status == "pending"
    turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=provider.id,
            model="m",
            tools_enabled=True,
        )
    )
    plan = ChatPlanService(store).ensure(turn, session)
    assert plan.directive_id == directive_id
    assert store.get(ChatPlanDirective, directive_id).status == "applied"
    assert ChatPlanService(store).pending_directive(turn, session) is None


def test_active_harness_force_waits_for_next_turn(tmp_path):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
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
    store.update(
        HarnessTurn,
        harness_turn.id,
        {"status": HarnessTurnStatus.RUNNING},
        expected_revision=harness_turn.revision,
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
    directive = store.get(ChatPlanDirective, receipt["directive"]["id"])
    assert (
        receipt["timing"] == "next_turn"
        and directive.deferred_after_turn_id == active.id
    )
    assert ChatPlanService(store).required(active, session) is False
    next_turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            backend=ChatBackend.HARNESS,
            model="m",
            tools_enabled=True,
            status=ChatTurnStatus.QUEUED,
        )
    )
    assert ChatPlanService(store).required(next_turn, session) is True


def test_queued_harness_force_applies_before_that_turn_starts(tmp_path):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
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
    directive = store.get(ChatPlanDirective, receipt["directive"]["id"])
    assert receipt["timing"] == "before_turn"
    assert directive.deferred_after_turn_id is None
    assert ChatPlanService(store).ensure(queued, session).directive_id == directive.id


def test_unenforceable_harness_adapter_blocks_before_connection(tmp_path):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
        HarnessProfile(
            name="Harness",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
            default_model="m",
            capabilities=HarnessCapabilities(models=["m"], mcp=True),
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
    session = store.get(HarnessSession, harness_turn.harness_session_id)
    with pytest.raises(PlanningBlocked, match="cannot enforce restricted discovery"):
        asyncio.run(harness._ensure_harness_plan(harness_turn, session))
    assert ChatPlanService(store).for_turn(harness_turn.chat_turn_id).phase == "blocked"
    assert session.id not in harness._connections


@pytest.mark.parametrize("permission_mode", ["managed", "unrestricted"])
# Keep this test ID available to the cumulative selection from PR #861.
def test_managed_codex_submits_formal_plan_through_restricted_core_gateway(
    tmp_path, monkeypatch, permission_mode
):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
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
    session = store.get(HarnessSession, harness_turn.harness_session_id)
    if permission_mode == "unrestricted":
        session = store.update(
            HarnessSession,
            session.id,
            {
                "metadata": {
                    **session.metadata,
                    "execution_mode": "host",
                    "harness_permission_mode": "unrestricted",
                    "scope_snapshot": {"bypass_permissions": True},
                    "approval_policy": "never",
                }
            },
            expected_revision=session.revision,
        )
    observed = {}

    async def planner(**kwargs):
        observed.update(kwargs)
        planner_session = store.create(
            HarnessSession(
                engagement_id=project.id,
                harness_profile_id=profile.id,
                model="codex-test",
                metadata={
                    "analysis_only": True,
                    "planning_target_turn_id": chat_turn.id,
                    "planning_tool_names": kwargs["planning_tool_names"],
                    "planning_native_categories": kwargs["planning_native_categories"],
                },
            )
        )
        assert {
            item["name"] for item in harness._gateway_catalog(planner_session)["tools"]
        } == {"workspace.find", "workspace.search", "workspace.read", "plan.submit"}
        monkeypatch.setattr(harness, "_active_gateway_turn", lambda _: harness_turn)
        await harness._gateway_call_unwrapped(
            planner_session,
            "plan.submit",
            _submission("workspace.read").model_dump(),
        )

    monkeypatch.setattr(harness, "analyze_structured", planner)
    planned_turn = asyncio.run(harness._ensure_harness_plan(harness_turn, session))

    plan = ChatPlanService(store).for_turn(chat_turn.id)
    assert plan is not None and plan.phase == "execution"
    assert plan.tool_names == ["workspace.read"]
    assert plan.native_categories == []
    assert observed["planning_native_categories"] == []
    assert "Core-validated task plan" in planned_turn.prompt
    session = store.get(HarnessSession, session.id)
    assert session.metadata["planning_enforced"] is True
    assert _session_native_capabilities(session, profile) == HarnessNativeCapabilities()

    ordinary_chat_turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat_turn.session_id,
            backend=ChatBackend.HARNESS,
            model="codex-test",
            tools_enabled=True,
            request_snapshot={"planning_enabled": False},
        )
    )
    ordinary_harness_turn = store.create(
        HarnessTurn(
            engagement_id=project.id,
            harness_session_id=session.id,
            origin=HarnessTurnOrigin.CHAT,
            chat_session_id=chat_turn.session_id,
            chat_turn_id=ordinary_chat_turn.id,
            prompt="ordinary follow-up",
        )
    )
    asyncio.run(harness._ensure_harness_plan(ordinary_harness_turn, session))
    session = store.get(HarnessSession, session.id)
    assert session.metadata["planning_enforced"] is False
    assert _session_native_capabilities(session, profile).web_search is True


def test_legacy_harness_subagent_cannot_start_a_second_planning_gate(tmp_path):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
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
    child = store.create(
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
    saved_child = store.get(ChatSession, child.id)
    assert saved_child.metadata["planning_enabled"] is False
    assert ChatPlanService(store).required(chat_turn, saved_child) is False


def test_linked_retry_of_blocked_plan_uses_saved_harness_choice(tmp_path, monkeypatch):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    unsupported = store.create(
        HarnessProfile(
            name="Unsupported",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
            default_model="grok-model",
            capabilities=HarnessCapabilities(models=["grok-model"]),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    managed = store.create(
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
    original_plan = plans.ensure(first_turn, chat)
    plans.block(first_turn.id, "This adapter cannot enforce restricted discovery.")
    store.update(
        ChatTurn,
        first_turn.id,
        {"status": ChatTurnStatus.FAILED},
        expected_revision=first_turn.revision,
    )
    store.update(
        HarnessTurn,
        failed.id,
        {"status": HarnessTurnStatus.FAILED},
        expected_revision=failed.revision,
    )
    chat = update_chat_assistant_config(
        store,
        store.get(ChatSession, chat.id),
        ChatAssistantConfigPatch(
            backend=ChatBackend.HARNESS,
            profile_id=managed.id,
            model="claude-model",
            planning_enabled=True,
        ),
        expected_revision=store.get(ChatSession, chat.id).revision,
    )
    started: list[str] = []
    monkeypatch.setattr(harness, "start_chat_turn", started.append)

    replacement = asyncio.run(harness.retry_turn(failed.id))

    assert started == [replacement.id]
    assert replacement.metadata["retry_of_turn_id"] == failed.id
    replacement_session = store.get(HarnessSession, replacement.harness_session_id)
    assert replacement_session.harness_profile_id == managed.id
    assert replacement_session.model == "claude-model"
    replacement_chat_turn = store.get(ChatTurn, replacement.chat_turn_id)
    assert replacement_chat_turn.request_snapshot["planning_enabled"] is True
    assert replacement_chat_turn.id != first_turn.id
    assert plans.for_turn(first_turn.id).id == original_plan.id
    assert plans.for_turn(first_turn.id).phase == "blocked"
    assert plans.for_turn(replacement_chat_turn.id) is None


def test_unrestricted_claude_configuration_blocks_before_connection(tmp_path):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
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
    session = store.get(HarnessSession, harness_turn.harness_session_id)
    session = store.update(
        HarnessSession,
        session.id,
        {"metadata": {**session.metadata, "harness_permission_mode": "unrestricted"}},
        expected_revision=session.revision,
    )
    with pytest.raises(PlanningBlocked, match="cannot enforce restricted discovery"):
        asyncio.run(harness._ensure_harness_plan(harness_turn, session))
    assert session.id not in harness._connections


def test_managed_claude_uses_same_model_restricted_gateway_and_saved_plan(
    tmp_path, monkeypatch
):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
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
    session = store.get(HarnessSession, harness_turn.harness_session_id)
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
    planned_turn = asyncio.run(harness._ensure_harness_plan(harness_turn, session))
    assert observed["model"] == "claude-test"
    assert observed["nebula_tools"] is False
    assert observed["planning_target_turn_id"] == harness_turn.chat_turn_id
    assert "Core-validated task plan" in planned_turn.prompt
    planner_session = store.create(
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


def test_harness_gateway_and_native_permission_replan_before_effect(
    tmp_path, monkeypatch
):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
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
    session = store.get(HarnessSession, harness_turn.harness_session_id)
    plans = ChatPlanService(store)
    plan = plans.ensure(chat_turn, store.get(ChatSession, chat_turn.session_id))
    plans.submit(
        plan,
        _submission("workspace.read"),
        available_tools={"workspace.read", "goal.block"},
        native_categories={"shell"},
        workspace=tmp_path,
    )
    monkeypatch.setattr(harness, "_active_gateway_turn", lambda _: harness_turn)

    with pytest.raises(PlanningBlocked, match="goal.block was not declared"):
        asyncio.run(
            harness._gateway_call_unwrapped(
                session, "goal.block", {"reason": "blocked"}
            )
        )
    assert plans.for_turn(chat_turn.id).phase == "replanning"

    plans.submit(
        plan,
        _submission("workspace.read"),
        available_tools={"workspace.read"},
        native_categories={"shell"},
        workspace=tmp_path,
    )
    with pytest.raises(
        PlanningBlocked, match="native capability shell was not declared"
    ):
        asyncio.run(
            harness._request_permission(
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
    assert plans.for_turn(chat_turn.id).phase == "replanning"
