from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from datetime import timedelta
from pathlib import Path

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
from nebula.v3.browser_tools import BrowserAutomationBroker, autonomous_browser_specs
from nebula.v3.domain import (
    AgentRun,
    BrowserAutomationLease,
    BrowserAutomationLeaseStatus,
    BrowserCommand,
    BrowserCommandStatus,
    BrowserIdentity,
    BrowserSession,
    Engagement,
    ScopePolicy,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import ToolInvocation
from tests.v3.row_horizon_fixture import seed_older_copies


async def _project(store: NebulaStore) -> tuple[Engagement, str]:
    project = Engagement(name="Automation lab")
    (await store.create(project))
    scope = ScopePolicy(
        engagement_id=project.id,
        allowed_domains=["app.example.test"],
        allowed_ports=[443],
    )
    (await store.create(scope))
    project = await store.update(
        Engagement,
        project.id,
        {"scope_policy_id": scope.id},
        expected_revision=project.revision,
    )
    return project, scope.id


async def _run(store: NebulaStore, project: Engagement) -> AgentRun:
    run = AgentRun(engagement_id=project.id, objective="test browser application")
    return await store.create(run)


async def test_browser_automation_lease_claim_and_finish_are_durable(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, _ = await _project(store)
    run = await _run(store, project)
    service = BrowserAutomationService(store)
    session = await service.store.list_entities(
        __import__("nebula.v3.domain", fromlist=["BrowserSession"]).BrowserSession,
        engagement_id=project.id,
        limit=1,
    )
    if not session:
        identity = __import__(
            "nebula.v3.domain", fromlist=["BrowserIdentity"]
        ).BrowserIdentity(engagement_id=project.id, name="Test identity")
        (await store.create(identity))
        session_entity = __import__(
            "nebula.v3.domain", fromlist=["BrowserSession"]
        ).BrowserSession(
            engagement_id=project.id,
            name="Test session",
            identity_id=identity.id,
            device_owner="desktop-1",
        )
        (await store.create(session_entity))
    else:
        session_entity = session[0]
    request = BrowserAutonomyRequestModel(
        session_id=session_entity.id,
        targets=["https://app.example.test/"],
        credential_refs=["vault:browser-test"],
    )
    lease = await service.create_lease(run.id, project.id, request, "operator")
    command = await service.enqueue_command(
        lease.id,
        BrowserCommandCreateRequest(
            tab_id="tab-1",
            kind="browser.navigate",
            arguments={"url": "https://app.example.test/account"},
        ),
        "agent",
    )
    claimed = await service.claim_command(
        command.id, BrowserCommandClaimRequest(device_id="desktop-1")
    )
    assert claimed.status == BrowserCommandStatus.CLAIMED
    finished = await service.finish_command(
        command.id,
        BrowserCommandResultRequest(
            device_id="desktop-1",
            claim_token=claimed.claim_token or "",
            state="complete",
            result={"url": "https://app.example.test/account", "page": "untrusted"},
        ),
    )
    assert finished.status == BrowserCommandStatus.COMPLETE
    assert (await service.status(project.id, run.id)).commands[0].result[
        "page"
    ] == "untrusted"


async def test_browser_runtime_exposes_backend_neutral_research_tools(tmp_path):
    async def scenario() -> None:
        store = NebulaStore(tmp_path / "nebula.db")
        project, scope_id = await _project(store)
        run = await _run(store, project)
        service = BrowserAutomationService(store)
        from nebula.v3.domain import BrowserCrawlJob, BrowserIdentity, BrowserSession

        identity = await store.create(
            BrowserIdentity(engagement_id=project.id, name="Test identity")
        )
        session = await store.create(
            BrowserSession(
                engagement_id=project.id,
                name="Test session",
                identity_id=identity.id,
                device_owner="desktop-1",
            )
        )
        (
            await service.create_lease(
                run.id,
                project.id,
                BrowserAutonomyRequestModel(
                    session_id=session.id,
                    targets=["https://app.example.test/"],
                ),
                "operator",
            )
        )
        broker = BrowserAutomationBroker(store, service)
        scope = await store.get(ScopePolicy, scope_id)
        assert {
            "target.crawl",
            "repeater.create",
            "intruder.create",
            "analysis.tokens",
        } <= set(autonomous_browser_specs())

        result = await broker.execute(
            ToolInvocation(
                engagement_id=project.id,
                run_id=run.id,
                tool_name="target.crawl",
                arguments={
                    "start_url": "https://app.example.test/docs",
                    "max_requests": 10,
                },
                workspace=Path(tmp_path),
                idempotency_key="crawl-1",
            ),
            scope,
        )
        assert result.output["status"] == "draft"
        assert (await store.count(BrowserCrawlJob)) == 1

        decoded = await broker.execute(
            ToolInvocation(
                engagement_id=project.id,
                run_id=run.id,
                tool_name="analysis.decode",
                arguments={"operation": "url_decode", "value": "a%2Fb"},
                workspace=Path(tmp_path),
                idempotency_key="decode-1",
            ),
            scope,
        )
        assert decoded.output["result"] == "a/b"

    (await scenario())


async def test_browser_automation_rejects_scope_expansion_and_secret_values(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, _ = await _project(store)
    run = await _run(store, project)
    service = BrowserAutomationService(store)
    from nebula.v3.domain import BrowserIdentity, BrowserSession

    identity = BrowserIdentity(engagement_id=project.id, name="Test identity")
    (await store.create(identity))
    session = BrowserSession(
        engagement_id=project.id,
        name="Test session",
        identity_id=identity.id,
        device_owner="desktop-1",
    )
    (await store.create(session))
    lease = await service.create_lease(
        run.id,
        project.id,
        BrowserAutonomyRequestModel(
            session_id=session.id, targets=["https://app.example.test/"]
        ),
        "operator",
    )
    with pytest.raises(BrowserAutomationRequestError, match="outside"):
        (
            await service.enqueue_command(
                lease.id,
                BrowserCommandCreateRequest(
                    tab_id="tab-1",
                    kind="browser.navigate",
                    arguments={"url": "https://outside.example/"},
                ),
                "agent",
            )
        )
    with pytest.raises(BrowserAutomationRequestError, match="credential_ref"):
        (
            await service.enqueue_command(
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
        )


async def test_proxy_rules_are_declarative_and_revoke_with_run(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, _ = await _project(store)
    run = await _run(store, project)
    service = BrowserAutomationService(store)
    from nebula.v3.domain import BrowserIdentity, BrowserSession

    identity = BrowserIdentity(engagement_id=project.id, name="Test identity")
    (await store.create(identity))
    session = BrowserSession(
        engagement_id=project.id,
        name="Test session",
        identity_id=identity.id,
        device_owner="desktop-1",
        proxy_enabled=True,
        proxy_trust_acknowledged=True,
    )
    (await store.create(session))
    lease = await service.create_lease(
        run.id,
        project.id,
        BrowserAutonomyRequestModel(
            session_id=session.id, targets=["https://app.example.test/"]
        ),
        "operator",
    )
    rule = await service.add_rule(
        lease.id,
        BrowserProxyRuleRequest(
            match={"host": "app.example.test", "path": "/api"},
            action={"type": "set_header", "name": "X-Test", "value": "1"},
        ),
        "agent",
    )
    assert rule.enabled is True
    (await service.revoke_run(run.id, "operator stop", "operator"))
    assert (await service.status(project.id, run.id)).leases[
        0
    ].status.value == "revoked"
    assert (await service.status(project.id, run.id)).rules[0].enabled is False
    assert (await store.get(type(session), session.id)).proxy_enabled is False


async def test_scope_revision_invalidates_queued_commands_and_rules(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, scope_id = await _project(store)
    run = await _run(store, project)
    service = BrowserAutomationService(store)
    from nebula.v3.domain import BrowserIdentity, BrowserSession

    identity = BrowserIdentity(engagement_id=project.id, name="Test identity")
    (await store.create(identity))
    session = BrowserSession(
        engagement_id=project.id,
        name="Test session",
        identity_id=identity.id,
        device_owner="desktop-1",
    )
    (await store.create(session))
    lease = await service.create_lease(
        run.id,
        project.id,
        BrowserAutonomyRequestModel(
            session_id=session.id, targets=["https://app.example.test/"]
        ),
        "operator",
    )
    command = await service.enqueue_command(
        lease.id,
        BrowserCommandCreateRequest(
            tab_id="tab-1",
            kind="browser.navigate",
            arguments={"url": "https://app.example.test/"},
        ),
        "agent",
    )
    rule = await service.add_rule(
        lease.id,
        BrowserProxyRuleRequest(
            action={"type": "block"},
        ),
        "agent",
    )
    current_scope = await store.get(ScopePolicy, scope_id)
    (
        await store.update(
            ScopePolicy,
            scope_id,
            {"allowed_domains": ["new.example.test"]},
            expected_revision=current_scope.revision,
        )
    )

    status = await service.status(project.id, run.id)
    assert status.leases[0].status.value == "revoked"
    assert (
        next(item for item in status.commands if item.id == command.id).status
        == BrowserCommandStatus.CANCELLED
    )
    assert next(item for item in status.rules if item.id == rule.id).enabled is False


async def test_credential_use_is_lease_bound_and_stays_as_a_reference(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, _ = await _project(store)
    run = await _run(store, project)
    service = BrowserAutomationService(store)
    from nebula.v3.domain import BrowserIdentity, BrowserSession, RiskClass

    identity = BrowserIdentity(engagement_id=project.id, name="Test identity")
    (await store.create(identity))
    session = BrowserSession(
        engagement_id=project.id,
        name="Test session",
        identity_id=identity.id,
        device_owner="desktop-1",
    )
    (await store.create(session))
    lease = await service.create_lease(
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
    command = await service.enqueue_command(
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
        (
            await service.enqueue_command(
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
        )


async def _leased_service(store: NebulaStore, tmp_path: Path):
    from nebula.v3.domain import BrowserIdentity, BrowserSession

    project, scope_id = await _project(store)
    run = await _run(store, project)
    service = BrowserAutomationService(store)
    identity = await store.create(
        BrowserIdentity(engagement_id=project.id, name="Identity")
    )
    session = await store.create(
        BrowserSession(
            engagement_id=project.id,
            name="Session",
            identity_id=identity.id,
            device_owner="desktop-1",
        )
    )
    lease = await service.create_lease(
        run.id,
        project.id,
        BrowserAutonomyRequestModel(
            session_id=session.id, targets=["https://app.example.test/"]
        ),
        "operator",
    )
    return project, run, service, lease, (await store.get(ScopePolicy, scope_id))


async def test_autonomous_browser_commands_are_enqueued_from_flat_tool_arguments(
    tmp_path, monkeypatch
):
    from nebula.v3.domain import BrowserCommand

    async def scenario() -> None:
        store = NebulaStore(tmp_path / "nebula.db")
        project, run, service, _lease, scope = await _leased_service(store, tmp_path)

        async def already_finished(command_id: str, _timeout: float):
            return await store.get(BrowserCommand, command_id)

        monkeypatch.setattr(service, "wait_for_command", already_finished)
        broker = BrowserAutomationBroker(store, service)

        result = await broker.execute(
            ToolInvocation(
                engagement_id=project.id,
                run_id=run.id,
                tool_name="browser.navigate",
                arguments={
                    "tab_id": "tab-1",
                    "url": "https://app.example.test/docs",
                    "page_url": "https://app.example.test/",
                },
                workspace=Path(tmp_path),
                idempotency_key="navigate-1",
            ),
            scope,
        )

        (command,) = await store.list_entities(BrowserCommand)
        assert result.output["command_id"] == command.id
        assert command.kind == "browser.navigate"
        assert command.tab_id == "tab-1"
        assert command.arguments == {"url": "https://app.example.test/docs"}
        assert command.expected_page_url == "https://app.example.test/"

    (await scenario())


async def test_expected_page_url_does_not_bypass_the_lease_target_check(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    _project_, _run_, service, lease, _scope = await _leased_service(store, tmp_path)

    with pytest.raises(BrowserAutomationRequestError, match="target"):
        (
            await service.enqueue_command(
                lease.id,
                BrowserCommandCreateRequest(
                    tab_id="tab-1",
                    kind="browser.navigate",
                    expected_page_url="https://app.example.test/",
                    arguments={"url": "https://other.example.test/"},
                ),
                "operator",
            )
        )


async def _session(store: NebulaStore, project: Engagement) -> BrowserSession:
    identity = await store.create(
        BrowserIdentity(engagement_id=project.id, name="Test identity")
    )
    # A long-lived Project holds a page of older identities.
    (await seed_older_copies(store, identity))
    return await store.create(
        BrowserSession(
            engagement_id=project.id,
            name="Test session",
            identity_id=identity.id,
            device_owner="desktop-1",
        )
    )


async def test_lease_lookups_reach_records_after_a_page_of_history(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, _ = await _project(store)
    run = await _run(store, project)
    service = BrowserAutomationService(store)
    session = await _session(store, project)
    lease = await service.create_lease(
        run.id,
        project.id,
        BrowserAutonomyRequestModel(
            session_id=session.id, targets=["https://app.example.test/"]
        ),
        "operator",
    )
    (
        await seed_older_copies(
            store,
            lease.model_copy(update={"status": BrowserAutomationLeaseStatus.EXPIRED}),
        )
    )
    assert (await service.active_lease_for_run(run.id)).id == lease.id

    (
        await seed_older_copies(
            store,
            BrowserCommand(
                engagement_id=project.id,
                run_id=run.id,
                lease_id=lease.id,
                session_id=session.id,
                tab_id="tab-1",
                kind="browser.navigate",
                status=BrowserCommandStatus.COMPLETE,
                expires_at=utc_now() + timedelta(minutes=5),
            ),
            vary=lambda index: {"idempotency_key": f"done-{index}"},
        )
    )
    request = BrowserCommandCreateRequest(
        tab_id="tab-1",
        kind="browser.navigate",
        arguments={"url": "https://app.example.test/account"},
        idempotency_key="navigate-account",
    )
    command = await service.enqueue_command(lease.id, request, "agent")
    assert (await service.enqueue_command(lease.id, request, "agent")).id == command.id
    assert (await service.status(project.id, run.id)).commands[-1].id == command.id

    (await service.revoke_run(run.id, "operator stop", "operator"))
    assert (
        await store.get(BrowserCommand, command.id)
    ).status == BrowserCommandStatus.CANCELLED
    assert (
        await store.get(BrowserAutomationLease, lease.id)
    ).status == BrowserAutomationLeaseStatus.REVOKED


async def test_scope_change_revokes_an_active_lease_after_a_page_of_history(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project, _ = await _project(store)
    run = await _run(store, project)
    service = BrowserAutomationService(store)
    session = await _session(store, project)
    lease = await service.create_lease(
        run.id,
        project.id,
        BrowserAutonomyRequestModel(
            session_id=session.id, targets=["https://app.example.test/"]
        ),
        "operator",
    )
    (
        await seed_older_copies(
            store,
            lease.model_copy(update={"status": BrowserAutomationLeaseStatus.REVOKED}),
        )
    )

    revoked = await service.invalidate_scope_revision(
        project.id, lease.scope_policy_revision + 1, "operator"
    )

    assert revoked == 1
    assert (
        await store.get(BrowserAutomationLease, lease.id)
    ).status == BrowserAutomationLeaseStatus.REVOKED


pytestmark = pytest.mark.anyio
