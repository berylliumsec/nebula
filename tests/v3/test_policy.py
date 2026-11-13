from datetime import datetime, timedelta, timezone

import pytest

from nebula.v3.domain import RiskClass, ScopePolicy
from nebula.v3.policy import (
    PolicyEffect,
    PolicyEngine,
    PolicyRequest,
    path_is_within_workspace,
)


NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)


def _scope(**changes):
    values = {
        "engagement_id": "eng-1",
        "allowed_cidrs": ["10.40.0.0/16"],
        "allowed_domains": ["app.example.test", "*.lab.example.test"],
        "allowed_ports": [443],
    }
    values.update(changes)
    return ScopePolicy(**values)


def test_local_analysis_is_allowed_without_a_network_target():
    decision = PolicyEngine().evaluate(
        _scope(),
        PolicyRequest(tool_name="parse.nmap", risk_class=RiskClass.LOCAL_READ),
        now=NOW,
    )
    assert decision.effect == PolicyEffect.ALLOW
    assert decision.rule == "low_risk"


def test_active_hostname_requires_pinned_dns_and_rejects_mixed_answers():
    engine = PolicyEngine()
    request = PolicyRequest(
        tool_name="scan.tcp",
        risk_class=RiskClass.NETWORK_ACTION,
        network_access=True,
        target="https://app.example.test/health",
        resolved_ips=["10.40.2.3"],
    )
    approved_target = engine.evaluate(_scope(), request, now=NOW)
    assert approved_target.effect == PolicyEffect.ALLOW
    assert approved_target.rule == "in_scope_network_action"
    assert approved_target.normalized_target == "app.example.test"

    missing_dns = engine.evaluate(
        _scope(), request.model_copy(update={"resolved_ips": []}), now=NOW
    )
    assert missing_dns.effect == PolicyEffect.DENY
    assert missing_dns.rule == "dns_resolution_required"

    rebinding = engine.evaluate(
        _scope(),
        request.model_copy(update={"resolved_ips": ["10.40.2.3", "203.0.113.8"]}),
        now=NOW,
    )
    assert rebinding.effect == PolicyEffect.DENY
    assert rebinding.rule == "dns_rebinding"


def test_an_approval_reason_cannot_override_an_out_of_scope_target():
    decision = PolicyEngine().evaluate(
        _scope(),
        PolicyRequest(
            tool_name="scan.and.export",
            risk_class=RiskClass.NETWORK_ACTION,
            network_access=True,
            target="203.0.113.80",
            port=443,
            writes_outside_workspace=True,
        ),
        now=NOW,
    )
    assert decision.effect == PolicyEffect.DENY
    assert decision.rule == "target_scope"


def test_scope_enforces_ports_domains_and_wildcard_apex_boundaries():
    engine = PolicyEngine()

    def passive(target, port=443):
        return engine.evaluate(
            _scope(),
            PolicyRequest(
                tool_name="recon.headers",
                risk_class=RiskClass.PASSIVE,
                network_access=True,
                target=target,
                port=port,
            ),
            now=NOW,
        )

    assert passive("host.lab.example.test").effect == PolicyEffect.ALLOW
    assert passive("lab.example.test").rule == "target_scope"
    assert passive("app.example.test", 80).rule == "port_scope"
    assert passive("attacker.example").effect == PolicyEffect.DENY


def test_explicit_all_target_mode_bypasses_only_target_and_port_boundaries():
    engine = PolicyEngine()
    policy = _scope(allow_all_targets=True, prohibited_actions=["delete.production"])
    outside = engine.evaluate(
        policy,
        PolicyRequest(
            tool_name="recon.headers",
            risk_class=RiskClass.PASSIVE,
            network_access=True,
            target="https://outside.example:9443/",
            port=9443,
        ),
        now=NOW,
    )
    prohibited = engine.evaluate(
        policy,
        PolicyRequest(
            tool_name="delete.production",
            risk_class=RiskClass.PASSIVE,
            network_access=True,
            target="https://outside.example/",
        ),
        now=NOW,
    )
    assert outside.effect == PolicyEffect.ALLOW
    assert prohibited.effect == PolicyEffect.DENY
    assert prohibited.rule == "prohibited_action"


def test_active_scans_run_without_grants_but_cannot_expand_scope():
    policy = _scope()
    matching = PolicyRequest(
        tool_name="scan.tcp",
        risk_class=RiskClass.NETWORK_ACTION,
        network_access=True,
        target="10.40.2.3",
        port=443,
    )

    decision = PolicyEngine().evaluate(policy, matching, now=NOW)
    assert decision.effect == PolicyEffect.ALLOW
    assert decision.rule == "in_scope_network_action"
    assert decision.matched_grant_index is None

    other_tool = PolicyEngine().evaluate(
        policy, matching.model_copy(update={"tool_name": "scan.udp"}), now=NOW
    )
    assert other_tool.effect == PolicyEffect.ALLOW
    outside = PolicyEngine().evaluate(
        policy, matching.model_copy(update={"target": "203.0.113.7"}), now=NOW
    )
    assert outside.effect == PolicyEffect.DENY
    assert outside.rule == "target_scope"


@pytest.mark.parametrize(
    "risk",
    [
        RiskClass.CREDENTIAL_USE,
        RiskClass.HIGH_IMPACT_ACTION,
        RiskClass.PERSISTENT_CHANGE,
        RiskClass.DESTRUCTIVE_CHANGE,
        RiskClass.SCOPE_CHANGE,
    ],
)
def test_high_risk_actions_always_pause_for_approval(risk):
    request = PolicyRequest(tool_name="dangerous.action", risk_class=risk)
    if risk != RiskClass.SCOPE_CHANGE:
        request = request.model_copy(update={"target": "10.40.2.3", "port": 443})
    decision = PolicyEngine().evaluate(_scope(), request, now=NOW)
    assert decision.effect == PolicyEffect.REQUIRE_APPROVAL
    assert decision.rule == "high_risk"


def test_network_effect_not_risk_class_requires_a_target_in_bounded_mode():
    engine = PolicyEngine()
    opaque_network = PolicyRequest(
        tool_name="mcp.remote_lookup",
        risk_class=RiskClass.LOCAL_READ,
        network_access=True,
    )
    bounded = engine.evaluate(_scope(), opaque_network, now=NOW)
    assert bounded.effect == PolicyEffect.DENY
    assert bounded.rule == "target_required"
    assert "all destinations" in bounded.reason

    all_destinations = engine.evaluate(
        _scope(allow_all_targets=True), opaque_network, now=NOW
    )
    assert all_destinations.effect == PolicyEffect.ALLOW
    assert (
        engine.evaluate(
            _scope(allow_all_targets=True),
            opaque_network.model_copy(update={"risk_class": RiskClass.CREDENTIAL_USE}),
            now=NOW,
        ).effect
        == PolicyEffect.REQUIRE_APPROVAL
    )
    assert (
        engine.evaluate(
            _scope(),
            PolicyRequest(
                tool_name="local.credentials", risk_class=RiskClass.CREDENTIAL_USE
            ),
            now=NOW,
        ).effect
        == PolicyEffect.REQUIRE_APPROVAL
    )


def test_bypass_overrides_nebula_scope_and_approval_but_not_local_only_validation():
    engine = PolicyEngine()
    scope = _scope(
        bypass_permissions=True,
        allow_all_targets=False,
        not_after=NOW - timedelta(hours=1),
        prohibited_actions=["mcp.remote_lookup"],
    )
    decision = engine.evaluate(
        scope,
        PolicyRequest(
            tool_name="mcp.remote_lookup",
            risk_class=RiskClass.CREDENTIAL_USE,
            network_access=True,
            writes_outside_workspace=True,
        ),
        now=NOW,
    )
    assert decision.effect == PolicyEffect.ALLOW
    assert decision.rule == "permission_bypass"
    with pytest.raises(ValueError, match="Local only"):
        _scope(bypass_permissions=True, local_only=True)


def test_allowed_public_domain_dns_does_not_require_a_matching_cidr():
    request = PolicyRequest(
        tool_name="scan.tcp",
        risk_class=RiskClass.NETWORK_ACTION,
        network_access=True,
        target="app.example.test",
        port=443,
        resolved_ips=["8.8.8.8"],
    )
    assert (
        PolicyEngine().evaluate(_scope(), request, now=NOW).effect == PolicyEffect.ALLOW
    )
    private = request.model_copy(update={"resolved_ips": ["192.168.1.2"]})
    assert PolicyEngine().evaluate(_scope(), private, now=NOW).rule == "dns_rebinding"


def test_time_local_only_prohibited_action_and_workspace_boundaries(tmp_path):
    engine = PolicyEngine()
    policy = _scope(
        not_before=NOW - timedelta(hours=1),
        not_after=NOW + timedelta(hours=1),
        local_only=True,
        prohibited_actions=["persist"],
    )
    cloud = engine.evaluate(
        policy,
        PolicyRequest(
            tool_name="summarize",
            risk_class=RiskClass.LOCAL_READ,
            cloud_transfer=True,
        ),
        now=NOW,
    )
    assert cloud.rule == "local_only"
    prohibited = engine.evaluate(
        policy,
        PolicyRequest(
            tool_name="other",
            action="PERSIST",
            risk_class=RiskClass.LOCAL_READ,
        ),
        now=NOW,
    )
    assert prohibited.rule == "prohibited_action"
    ended = engine.evaluate(
        policy,
        PolicyRequest(tool_name="parse", risk_class=RiskClass.LOCAL_READ),
        now=NOW + timedelta(hours=2),
    )
    assert ended.rule == "time_window"

    workspace = tmp_path / "engagement"
    workspace.mkdir()
    assert path_is_within_workspace(workspace / "evidence" / "scan.txt", workspace)
    assert not path_is_within_workspace(workspace / ".." / "secrets", workspace)


@pytest.mark.parametrize(
    "legacy", ["active_scan", "exploitation", "persistence", "destructive"]
)
def test_legacy_risk_names_are_rejected(legacy):
    with pytest.raises(ValueError):
        RiskClass(legacy)


@pytest.mark.parametrize(
    "risk",
    [
        RiskClass.HIGH_IMPACT_ACTION,
        RiskClass.PERSISTENT_CHANGE,
        RiskClass.DESTRUCTIVE_CHANGE,
    ],
)
def test_neutral_high_risk_names_keep_approval_and_scope(risk):
    policy = ScopePolicy(engagement_id="project", allowed_cidrs=["10.0.0.0/24"])
    engine = PolicyEngine()
    allowed = engine.evaluate(
        policy,
        PolicyRequest(
            tool_name="custom.action",
            risk_class=risk,
            network_access=True,
            target="http://10.0.0.10/",
        ),
    )
    assert allowed.effect == PolicyEffect.REQUIRE_APPROVAL
    denied = engine.evaluate(
        policy,
        PolicyRequest(
            tool_name="custom.action",
            risk_class=risk,
            network_access=True,
            target="http://192.0.2.10/",
        ),
    )
    assert denied.effect == PolicyEffect.DENY


def test_external_tool_routing_uses_declared_effects():
    from nebula.v3.agent_tooling import role_for_tool
    from nebula.v3.orchestration import SpecialistRole
    from nebula.v3.tools import ToolSpec

    for name in ("custom.inspect", "nuclei.inspect", "searchsploit.inspect"):
        spec = ToolSpec(
            name=name,
            description="Operator supplied tool",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            risk_class=RiskClass.LOCAL_READ,
            filesystem_access="read",
        )
        assert role_for_tool(name, spec) == SpecialistRole.CODE_ANALYSIS
