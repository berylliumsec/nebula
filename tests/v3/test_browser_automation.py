import pytest

from nebula.v3.browser_automation import (
    BrowserAutomationRequestError,
    BrowserAutomationService,
    BrowserAutonomyRequestModel,
    BrowserCommandClaimRequest,
    BrowserCommandCreateRequest,
    BrowserCommandResultRequest,
    BrowserProxyRuleRequest,
)
from nebula.v3.domain import AgentRun, BrowserCommandStatus, Engagement, ScopePolicy
from nebula.v3.storage import NebulaStore


def _project(store: NebulaStore) -> tuple[Engagement, str]:
    project = Engagement(name="Automation lab")
    store.create(project)
    scope = ScopePolicy(
        engagement_id=project.id,
        allowed_domains=["app.example.test"],
        allowed_ports=[443],
    )
    store.create(scope)
    project = store.update(
        Engagement,
        project.id,
        {"scope_policy_id": scope.id},
        expected_revision=project.revision,
    )
    return project, scope.id


def _run(store: NebulaStore, project: Engagement) -> AgentRun:
    run = AgentRun(engagement_id=project.id, objective="test browser application")
    return store.create(run)


def test_browser_automation_lease_claim_and_finish_are_durable(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, _ = _project(store)
    run = _run(store, project)
    service = BrowserAutomationService(store)
    session = service.store.list_entities(
        __import__("nebula.v3.domain", fromlist=["BrowserSession"]).BrowserSession,
        engagement_id=project.id,
        limit=1,
    )
    if not session:
        identity = __import__(
            "nebula.v3.domain", fromlist=["BrowserIdentity"]
        ).BrowserIdentity(engagement_id=project.id, name="Test identity")
        store.create(identity)
        session_entity = __import__(
            "nebula.v3.domain", fromlist=["BrowserSession"]
        ).BrowserSession(
            engagement_id=project.id,
            name="Test session",
            identity_id=identity.id,
            device_owner="desktop-1",
        )
        store.create(session_entity)
    else:
        session_entity = session[0]
    request = BrowserAutonomyRequestModel(
        session_id=session_entity.id,
        targets=["https://app.example.test/"],
        credential_refs=["vault:browser-test"],
    )
    lease = service.create_lease(run.id, project.id, request, "operator")
    command = service.enqueue_command(
        lease.id,
        BrowserCommandCreateRequest(
            tab_id="tab-1",
            kind="browser.navigate",
            arguments={"url": "https://app.example.test/account"},
        ),
        "agent",
    )
    claimed = service.claim_command(
        command.id, BrowserCommandClaimRequest(device_id="desktop-1")
    )
    assert claimed.status == BrowserCommandStatus.CLAIMED
    finished = service.finish_command(
        command.id,
        BrowserCommandResultRequest(
            device_id="desktop-1",
            claim_token=claimed.claim_token or "",
            state="complete",
            result={"url": "https://app.example.test/account", "page": "untrusted"},
        ),
    )
    assert finished.status == BrowserCommandStatus.COMPLETE
    assert service.status(project.id, run.id).commands[0].result["page"] == "untrusted"


def test_browser_automation_rejects_scope_expansion_and_secret_values(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, _ = _project(store)
    run = _run(store, project)
    service = BrowserAutomationService(store)
    from nebula.v3.domain import BrowserIdentity, BrowserSession

    identity = BrowserIdentity(engagement_id=project.id, name="Test identity")
    store.create(identity)
    session = BrowserSession(
        engagement_id=project.id,
        name="Test session",
        identity_id=identity.id,
        device_owner="desktop-1",
    )
    store.create(session)
    lease = service.create_lease(
        run.id,
        project.id,
        BrowserAutonomyRequestModel(
            session_id=session.id, targets=["https://app.example.test/"]
        ),
        "operator",
    )
    with pytest.raises(BrowserAutomationRequestError, match="outside"):
        service.enqueue_command(
            lease.id,
            BrowserCommandCreateRequest(
                tab_id="tab-1",
                kind="browser.navigate",
                arguments={"url": "https://outside.example/"},
            ),
            "agent",
        )
    with pytest.raises(BrowserAutomationRequestError, match="credential_ref"):
        service.enqueue_command(
            lease.id,
            BrowserCommandCreateRequest(
                tab_id="tab-1",
                kind="browser.replay",
                arguments={
                    "url": "https://app.example.test/",
                    "method": "POST",
                    "password": "plaintext",
                },
            ),
            "agent",
        )


def test_proxy_rules_are_declarative_and_revoke_with_run(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, _ = _project(store)
    run = _run(store, project)
    service = BrowserAutomationService(store)
    from nebula.v3.domain import BrowserIdentity, BrowserSession

    identity = BrowserIdentity(engagement_id=project.id, name="Test identity")
    store.create(identity)
    session = BrowserSession(
        engagement_id=project.id,
        name="Test session",
        identity_id=identity.id,
        device_owner="desktop-1",
        proxy_enabled=True,
        proxy_trust_acknowledged=True,
    )
    store.create(session)
    lease = service.create_lease(
        run.id,
        project.id,
        BrowserAutonomyRequestModel(
            session_id=session.id, targets=["https://app.example.test/"]
        ),
        "operator",
    )
    rule = service.add_rule(
        lease.id,
        BrowserProxyRuleRequest(
            match={"host": "app.example.test", "path": "/api"},
            action={"type": "set_header", "name": "X-Test", "value": "1"},
        ),
        "agent",
    )
    assert rule.enabled is True
    service.revoke_run(run.id, "operator stop", "operator")
    assert service.status(project.id, run.id).leases[0].status.value == "revoked"
    assert service.status(project.id, run.id).rules[0].enabled is False
    assert store.get(type(session), session.id).proxy_enabled is False


def test_scope_revision_invalidates_queued_commands_and_rules(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, scope_id = _project(store)
    run = _run(store, project)
    service = BrowserAutomationService(store)
    from nebula.v3.domain import BrowserIdentity, BrowserSession

    identity = BrowserIdentity(engagement_id=project.id, name="Test identity")
    store.create(identity)
    session = BrowserSession(
        engagement_id=project.id,
        name="Test session",
        identity_id=identity.id,
        device_owner="desktop-1",
    )
    store.create(session)
    lease = service.create_lease(
        run.id,
        project.id,
        BrowserAutonomyRequestModel(
            session_id=session.id, targets=["https://app.example.test/"]
        ),
        "operator",
    )
    command = service.enqueue_command(
        lease.id,
        BrowserCommandCreateRequest(
            tab_id="tab-1",
            kind="browser.navigate",
            arguments={"url": "https://app.example.test/"},
        ),
        "agent",
    )
    rule = service.add_rule(
        lease.id,
        BrowserProxyRuleRequest(
            action={"type": "block"},
        ),
        "agent",
    )
    current_scope = store.get(ScopePolicy, scope_id)
    store.update(
        ScopePolicy,
        scope_id,
        {"allowed_domains": ["new.example.test"]},
        expected_revision=current_scope.revision,
    )

    status = service.status(project.id, run.id)
    assert status.leases[0].status.value == "revoked"
    assert (
        next(item for item in status.commands if item.id == command.id).status
        == BrowserCommandStatus.CANCELLED
    )
    assert next(item for item in status.rules if item.id == rule.id).enabled is False


def test_credential_use_is_lease_bound_and_stays_as_a_reference(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, _ = _project(store)
    run = _run(store, project)
    service = BrowserAutomationService(store)
    from nebula.v3.domain import BrowserIdentity, BrowserSession, RiskClass

    identity = BrowserIdentity(engagement_id=project.id, name="Test identity")
    store.create(identity)
    session = BrowserSession(
        engagement_id=project.id,
        name="Test session",
        identity_id=identity.id,
        device_owner="desktop-1",
    )
    store.create(session)
    lease = service.create_lease(
        run.id,
        project.id,
        BrowserAutonomyRequestModel(
            session_id=session.id,
            targets=["https://app.example.test/"],
            allowed_risk_classes=[
                RiskClass.PASSIVE,
                RiskClass.ACTIVE_SCAN,
                RiskClass.CREDENTIAL_USE,
            ],
            credential_refs=["vault:credential-1"],
        ),
        "operator",
    )
    command = service.enqueue_command(
        lease.id,
        BrowserCommandCreateRequest(
            tab_id="tab-1",
            kind="browser.interact",
            arguments={
                "operation": "fill",
                "credential_ref": "vault:credential-1",
                "page_url": "https://app.example.test/login",
                "non_secret_text": "operator@example.test",
            },
        ),
        "agent",
    )
    assert command.arguments["credential_ref"] == "vault:credential-1"
    assert command.arguments["non_secret_text"] == "operator@example.test"
    with pytest.raises(BrowserAutomationRequestError, match="not authorized"):
        service.enqueue_command(
            lease.id,
            BrowserCommandCreateRequest(
                tab_id="tab-1",
                kind="browser.interact",
                arguments={
                    "operation": "fill",
                    "credential_ref": "vault:other",
                    "page_url": "https://app.example.test/login",
                },
            ),
            "agent",
        )
