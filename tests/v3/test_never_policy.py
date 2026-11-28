"""Project never policy reaches Core and its native gateway without persisting grants."""

import pytest
from tests.v3.async_test_support import (
    close_database_handles as close_database_handles,
)

from nebula.v3.domain import (
    AutomationApprovalPolicy,
    AutomationProjectPolicy,
    Engagement,
    RiskClass,
    ScopePolicy,
    ToolCallOrigin,
)
from nebula.v3.execution_policy import effective_scope
from nebula.v3.harnesses import _codex_mcp_overrides, _codex_thread_config
from nebula.v3.policy import PolicyEffect, PolicyEngine, PolicyRequest

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class PolicyStore:
    def __init__(self, approval=AutomationApprovalPolicy.NEVER, *, bypass=False):
        self.scope = ScopePolicy(
            id="scope",
            engagement_id="project",
            allowed_cidrs=["192.0.2.0/24"],
            prohibited_actions=["prohibited.command"],
            bypass_permissions=bypass,
        )
        self.project = Engagement(
            id="project", name="Policy fixture", scope_policy_id=self.scope.id
        )
        self.policy = (
            AutomationProjectPolicy(
                id="command-policy", engagement_id="project", approval_policy=approval
            )
            if approval is not None
            else None
        )

    async def get(self, model, identifier):
        if model is Engagement and identifier == self.project.id:
            return self.project
        if model is ScopePolicy and identifier == self.scope.id:
            return self.scope
        raise AssertionError(f"Unexpected lookup: {model} {identifier}")

    async def list_entities(self, model, *, engagement_id, limit):
        assert model is AutomationProjectPolicy
        assert engagement_id == self.project.id
        assert limit == 1
        return [self.policy] if self.policy is not None else []


@pytest.mark.parametrize(
    "tool_request",
    [
        PolicyRequest(
            tool_name="prohibited.command", risk_class=RiskClass.WORKSPACE_WRITE
        ),
        PolicyRequest(
            tool_name="ssh.remote.run_command",
            risk_class=RiskClass.NETWORK_ACTION,
            network_access=True,
            target="203.0.113.1",
        ),
        PolicyRequest(
            tool_name="high-risk.command", risk_class=RiskClass.DESTRUCTIVE_CHANGE
        ),
    ],
    ids=["explicit-denial", "outside-scope", "mandatory-approval"],
)
async def test_never_bypasses_denials_scope_and_approval(tool_request):
    store = PolicyStore()
    assert (
        PolicyEngine().evaluate(store.scope, tool_request).effect != PolicyEffect.ALLOW
    )
    scope = await effective_scope(store, "project")
    decision = PolicyEngine().evaluate(scope, tool_request)
    assert decision.effect == PolicyEffect.ALLOW
    assert decision.rule == "permission_bypass"


@pytest.mark.parametrize(
    "approval",
    [AutomationApprovalPolicy.ALWAYS, AutomationApprovalPolicy.ON_BOUNDARY, None],
    ids=["always", "on-boundary", "missing-policy"],
)
async def test_other_policies_preserve_explicit_denials(approval):
    scope = await effective_scope(PolicyStore(approval), "project")
    assert not scope.bypass_permissions
    decision = PolicyEngine().evaluate(
        scope,
        PolicyRequest(
            tool_name="prohibited.command", risk_class=RiskClass.WORKSPACE_WRITE
        ),
    )
    assert decision.effect == PolicyEffect.DENY
    assert decision.rule == "prohibited_action"


async def test_derived_bypass_does_not_mutate_saved_scope():
    store = PolicyStore()
    original = store.scope.model_dump(mode="json")
    scope = await effective_scope(store, "project")
    assert scope.bypass_permissions
    assert store.scope.model_dump(mode="json") == original


async def test_policy_change_revokes_derived_bypass_on_fresh_resolution():
    store = PolicyStore()
    assert (await effective_scope(store, "project")).bypass_permissions
    store.policy = store.policy.model_copy(
        update={"approval_policy": AutomationApprovalPolicy.ON_BOUNDARY}
    )
    assert not (await effective_scope(store, "project")).bypass_permissions


async def test_explicit_scope_bypass_survives_non_never_policy():
    store = PolicyStore(AutomationApprovalPolicy.ON_BOUNDARY, bypass=True)
    assert (await effective_scope(store, "project")).bypass_permissions


async def test_never_works_without_a_saved_scope():
    store = PolicyStore()
    store.project = store.project.model_copy(update={"scope_policy_id": None})
    scope = await effective_scope(store, "project")
    assert scope.engagement_id == "project"
    assert scope.bypass_permissions
    assert scope == await effective_scope(store, "project")


async def test_caller_supplied_scope_preserves_original_permission_fields():
    store = PolicyStore()
    snapshot = store.scope.model_copy(update={"allowed_ports": [443]})
    scope = await effective_scope(store, "project", snapshot)
    assert scope.bypass_permissions
    assert scope.allowed_ports == [443]
    assert not snapshot.bypass_permissions


def gateway_config():
    return {
        name: {
            "transport": "streamable_http",
            "url": f"http://127.0.0.1/{name}",
            "required": True,
            "startup_timeout_seconds": 10,
            "tool_timeout_seconds": 60,
            "enabled_tools": [],
            "disabled_tools": [],
            "headers": {},
        }
        for name in ("nebula", "external")
    }


@pytest.mark.parametrize("bypass", [False, True], ids=["ordinary", "bypassed"])
def test_native_thread_approval_defers_only_core_gateway_when_bypassed(bypass):
    config = _codex_thread_config(gateway_config(), bypass_permissions=bypass)
    servers = config["mcp_servers"]
    assert servers["nebula"]["default_tools_approval_mode"] == (
        "approve" if bypass else "auto"
    )
    assert servers["external"]["default_tools_approval_mode"] == "auto"


@pytest.mark.parametrize("bypass", [False, True], ids=["ordinary", "bypassed"])
def test_native_process_approval_defers_only_core_gateway_when_bypassed(bypass):
    overrides = _codex_mcp_overrides(gateway_config(), {}, bypass_permissions=bypass)
    values = dict(
        item.replace('"nebula"', "nebula")
        .replace('"external"', "external")
        .split("=", 1)
        for item in overrides
        if item != "-c"
    )
    assert values["mcp_servers.nebula.default_tools_approval_mode"] == (
        '"approve"' if bypass else '"auto"'
    )
    assert values["mcp_servers.external.default_tools_approval_mode"] == '"prompt"'


async def test_runtime_platform_broker_obeys_never_and_policy_revocation(
    tmp_path, monkeypatch
):
    from nebula.v3 import runtime_platform
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.storage import NebulaStore
    from nebula.v3.tools import (
        PolicyDenied,
        ToolExecutionResult,
        ToolInvocation,
        ToolPlugin,
        ToolSpec,
    )

    calls = []

    class DeniedCommand(ToolPlugin):
        spec = ToolSpec(
            name="ssh.fixture.run_command",
            description="Synthetic command behind the production tool broker",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
            risk_class=RiskClass.WORKSPACE_WRITE,
            requires_approval=True,
            policy_deny_reason="fixture explicit command denial",
        )

        async def execute(self, invocation, runner):
            calls.append(invocation.id)
            return ToolExecutionResult(output={}, stdout="fixture cwd", exit_code=0)

    monkeypatch.setattr(
        runtime_platform,
        "build_ssh_tool_plugins",
        lambda *_args, **_kwargs: [DeniedCommand()],
    )
    store = NebulaStore(tmp_path / "runtime.db")
    scope = await store.create(ScopePolicy(id="scope", engagement_id="project"))
    await store.create(
        Engagement(id="project", name="Policy fixture", scope_policy_id=scope.id)
    )
    from nebula.v3.domain import ChatSession, ChatTurn

    await store.create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Fixture",
            model="fixture",
            provider_profile_id="fixture-provider",
        )
    )
    await store.create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id="chat",
            model="fixture",
            provider_profile_id="fixture-provider",
        )
    )
    policy = await store.create(
        AutomationProjectPolicy(
            engagement_id="project", approval_policy=AutomationApprovalPolicy.NEVER
        )
    )
    platform = runtime_platform.RuntimePlatform(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core",
    )

    async def components():
        return await platform.chat_components(
            engagement_id="project",
            turn_id="turn",
            provider=None,
            model="fixture",
            allow_empty=True,
        )

    def invocation(selected):
        return ToolInvocation(
            engagement_id="project",
            run_id="turn",
            origin=ToolCallOrigin.CHAT,
            tool_name="ssh.fixture.run_command",
            workspace=selected.workspace,
        )

    permitted = await components()
    assert permitted.scope.bypass_permissions
    result = await permitted.broker.execute(invocation(permitted), permitted.scope)
    assert result.exit_code == 0
    assert result.receipt is not None
    assert any(item.text == "fixture cwd" for item in result.receipt.output_preview)
    assert len(calls) == 1
    assert not (await store.get(ScopePolicy, scope.id)).bypass_permissions

    await store.update(
        AutomationProjectPolicy,
        policy.id,
        {"approval_policy": AutomationApprovalPolicy.ON_BOUNDARY},
        expected_revision=policy.revision,
    )
    restricted = await components()
    assert not restricted.scope.bypass_permissions
    with pytest.raises(PolicyDenied, match="fixture explicit command denial"):
        await restricted.broker.execute(invocation(restricted), restricted.scope)
    assert len(calls) == 1


async def test_autonomous_browser_scope_tracks_never_and_revocation(tmp_path):
    from nebula.v3.browser_automation import BrowserAutomationService
    from nebula.v3.browser_tools import BrowserAutomationToolPlatform
    from nebula.v3.storage import NebulaStore
    from tests.v3.test_browser_automation import _project

    store = NebulaStore(tmp_path / "browser.db")
    project, scope_id = await _project(store)
    policy = await store.create(
        AutomationProjectPolicy(engagement_id=project.id, approval_policy="never")
    )
    service = BrowserAutomationService(store)
    assert (await service._scope(project.id)).bypass_permissions
    platform = BrowserAutomationToolPlatform(store, service)
    assert (await platform.runtime_components(project.id)).scope.bypass_permissions
    await store.update(
        AutomationProjectPolicy,
        policy.id,
        {"approval_policy": "on_boundary"},
        expected_revision=policy.revision,
    )
    assert not (await service._scope(project.id)).bypass_permissions
    assert not (await platform.runtime_components(project.id)).scope.bypass_permissions
    assert not (await store.get(ScopePolicy, scope_id)).bypass_permissions


async def test_harness_existing_connection_refreshes_when_project_policy_changes(
    tmp_path,
):
    from nebula.v3.domain import HarnessSession
    from tests.v3.test_harnesses import _runtime

    store, project, profile, _mcp, adapter, runtime = await _runtime(tmp_path)
    scope = await store.create(
        ScopePolicy(id="scope", engagement_id=project.id, prohibited_actions=["deny"])
    )
    await store.update(Engagement, project.id, {"scope_policy_id": scope.id})
    policy = await store.create(
        AutomationProjectPolicy(
            engagement_id=project.id, approval_policy=AutomationApprovalPolicy.NEVER
        )
    )
    _chat, _chat_turn, turn = await runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model=None,
        prompt="Synthetic permission transition",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    session = await store.get(HarnessSession, turn.harness_session_id)
    assert session.metadata["approval_policy"] == "never"
    assert session.metadata["scope_snapshot"]["bypass_permissions"] is True
    first = await runtime._connection(session, turn)
    assert adapter.opens[-1].session.metadata["scope_snapshot"]["bypass_permissions"]

    await store.update(
        AutomationProjectPolicy,
        policy.id,
        {"approval_policy": AutomationApprovalPolicy.ON_BOUNDARY},
        expected_revision=policy.revision,
    )
    second = await runtime._connection(session, turn)
    refreshed = await store.get(HarnessSession, session.id)
    assert second is not first
    assert first.closed
    assert refreshed.metadata["approval_policy"] == "prompt"
    assert refreshed.metadata["scope_snapshot"]["bypass_permissions"] is False
    assert not adapter.opens[-1].session.metadata["scope_snapshot"][
        "bypass_permissions"
    ]
    assert not (await store.get(ScopePolicy, scope.id)).bypass_permissions


async def test_harness_connection_reopens_when_profile_permission_mode_changes(
    tmp_path,
):
    from nebula.v3.domain import HarnessProfile, HarnessSession
    from tests.v3.test_harnesses import _runtime

    store, project, profile, _mcp, adapter, runtime = await _runtime(tmp_path)
    _chat, _chat_turn, turn = await runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model=None,
        prompt="Synthetic profile permission transition",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    session = await store.get(HarnessSession, turn.harness_session_id)
    assert session.metadata["harness_permission_mode"] == "managed"
    first = await runtime._connection(session, turn)
    assert await runtime._connection(session, turn) is first

    profile = await store.update(
        HarnessProfile,
        profile.id,
        {"permission_mode": "unrestricted"},
        expected_revision=profile.revision,
    )
    second = await runtime._connection(session, turn)
    assert second is not first
    assert first.closed
    assert (await store.get(HarnessSession, session.id)).metadata[
        "harness_permission_mode"
    ] == "unrestricted"
    assert adapter.opens[-1].session.metadata["harness_permission_mode"] == (
        "unrestricted"
    )

    await store.update(
        HarnessProfile,
        profile.id,
        {"permission_mode": "managed"},
        expected_revision=profile.revision,
    )
    third = await runtime._connection(session, turn)
    assert third is not second
    assert adapter.opens[-1].session.metadata["harness_permission_mode"] == "managed"


async def test_butler_session_keeps_its_permission_mode_when_profile_changes(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("NEBULA_BUTLER_SUPERVISOR_UNRESTRICTED", raising=False)
    from nebula.v3.domain import HarnessProfile, HarnessSession
    from tests.v3.test_harnesses import _runtime

    store, _project, profile, _mcp, _adapter, runtime = await _runtime(tmp_path)
    butler = HarnessSession(
        engagement_id=_project.id,
        harness_profile_id=profile.id,
        model="m",
        metadata={"control_assistant": True, "harness_permission_mode": "managed"},
    )
    await store.update(
        HarnessProfile,
        profile.id,
        {"permission_mode": "unrestricted"},
        expected_revision=profile.revision,
    )
    assert await runtime._profile_permission_mode(butler) is None


async def test_butler_session_follows_profile_with_operator_unrestricted_switch(
    tmp_path, monkeypatch
):
    from nebula.v3.domain import HarnessProfile, HarnessSession
    from tests.v3.test_harnesses import _runtime

    monkeypatch.setenv("NEBULA_BUTLER_SUPERVISOR_UNRESTRICTED", "1")
    store, _project, profile, _mcp, _adapter, runtime = await _runtime(tmp_path)
    butler = HarnessSession(
        engagement_id=_project.id,
        harness_profile_id=profile.id,
        model="m",
        metadata={"control_assistant": True, "harness_permission_mode": "managed"},
    )
    await store.update(
        HarnessProfile,
        profile.id,
        {"permission_mode": "unrestricted"},
        expected_revision=profile.revision,
    )
    assert await runtime._profile_permission_mode(butler) == "unrestricted"
