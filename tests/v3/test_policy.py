from datetime import datetime, timedelta, timezone

import pytest

from nebula.v3.domain import MissionGrant, RiskClass, ScopePolicy
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
        risk_class=RiskClass.ACTIVE_SCAN,
        target="https://app.example.test/health",
        resolved_ips=["10.40.2.3"],
    )
    approved_target = engine.evaluate(_scope(), request, now=NOW)
    assert approved_target.effect == PolicyEffect.REQUIRE_APPROVAL
    assert approved_target.rule == "active_scan"
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
            risk_class=RiskClass.ACTIVE_SCAN,
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
                target=target,
                port=port,
            ),
            now=NOW,
        )

    assert passive("host.lab.example.test").effect == PolicyEffect.ALLOW
    assert passive("lab.example.test").rule == "target_scope"
    assert passive("app.example.test", 80).rule == "port_scope"
    assert passive("attacker.example").effect == PolicyEffect.DENY


def test_active_scan_mission_grants_are_time_tool_and_target_bounded():
    grant = MissionGrant(
        risk_classes=[RiskClass.ACTIVE_SCAN],
        tool_names=["scan.tcp"],
        targets=["10.40.2.3"],
        granted_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=5),
        granted_by="operator-1",
    )
    policy = _scope(grants=[grant])
    matching = PolicyRequest(
        tool_name="scan.tcp",
        risk_class=RiskClass.ACTIVE_SCAN,
        target="10.40.2.3",
        port=443,
    )

    decision = PolicyEngine().evaluate(policy, matching, now=NOW)
    assert decision.effect == PolicyEffect.ALLOW
    assert decision.rule == "mission_grant"
    assert decision.matched_grant_index == 0

    other_tool = PolicyEngine().evaluate(
        policy, matching.model_copy(update={"tool_name": "scan.udp"}), now=NOW
    )
    assert other_tool.effect == PolicyEffect.REQUIRE_APPROVAL
    expired = PolicyEngine().evaluate(policy, matching, now=NOW + timedelta(minutes=6))
    assert expired.effect == PolicyEffect.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    "risk",
    [
        RiskClass.CREDENTIAL_USE,
        RiskClass.EXPLOITATION,
        RiskClass.PERSISTENCE,
        RiskClass.DESTRUCTIVE,
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
