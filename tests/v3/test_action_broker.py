from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import timedelta

import pytest

from nebula.v3 import action_broker as action_broker_module
from nebula.v3.action_broker import (
    INTENT_EXPIRY_MINUTES,
    ActionBroker,
    ActionIntentClaimRequest,
    ActionIntentCommitRequest,
    ActionIntentCreateRequest,
    ActionIntentPrepareRequest,
    ActionIntentResultRequest,
)
from nebula.v3.domain import (
    ActionIntentStatus,
    DeviceCapabilitySnapshot,
    Engagement,
    KnowledgeSource,
    PairedDeviceSession,
    ResourceKind,
    ResourceRef,
    utc_now,
)
from nebula.v3.storage import ConflictError, NebulaStore


async def _device(store: NebulaStore, name: str = "Mac") -> PairedDeviceSession:
    now = utc_now()
    device = await store.create(
        PairedDeviceSession(
            name=name,
            token_sha256="a" * 64,
            csrf_sha256="b" * 64,
            idle_expires_at=now + timedelta(days=30),
            absolute_expires_at=now + timedelta(days=90),
        )
    )
    return device


async def _source(store: NebulaStore):
    project = await store.create(Engagement(name="Broker"))
    source = await store.create(
        KnowledgeSource(
            engagement_id=project.id,
            name="Portal",
            source_type="url",
        )
    )
    return project, source


async def test_broker_routes_to_owner_and_requires_prepare_before_commit(tmp_path):
    store = NebulaStore(tmp_path / "broker.db")
    project, source = await _source(store)
    device = await _device(store)
    broker = ActionBroker(store)
    ref = ResourceRef(
        project_id=project.id,
        kind=ResourceKind.SOURCE,
        id=source.id,
        revision=source.revision,
    )
    device = await broker.heartbeat(
        device.id,
        DeviceCapabilitySnapshot(
            platform="macos",
            app_version="3.0.0",
            capabilities=["browser.navigate"],
            ownership_claims=[ref],
            expected_revision=device.revision,
        ),
    )
    intent = await broker.create(
        ActionIntentCreateRequest(
            project_id=project.id,
            resources=[ref],
            action_id="navigate",
            requester="operator-1",
            idempotency_key="navigate-1",
        )
    )
    assert intent.selected_device_id == device.id
    assert intent.status == ActionIntentStatus.QUEUED
    with pytest.raises(ConflictError, match="prepared"):
        (
            await broker.commit(
                intent.id, ActionIntentCommitRequest(expected_revision=intent.revision)
            )
        )

    intent = await broker.claim(
        intent.id,
        ActionIntentClaimRequest(
            device_id=device.id, expected_revision=intent.revision
        ),
    )
    intent = await broker.prepare(
        intent.id,
        ActionIntentPrepareRequest(
            device_id=device.id,
            expected_revision=intent.revision,
            preflight_succeeded=True,
        ),
    )
    intent = await broker.commit(
        intent.id, ActionIntentCommitRequest(expected_revision=intent.revision)
    )
    intent = await broker.result(
        intent.id,
        ActionIntentResultRequest(
            device_id=device.id,
            expected_revision=intent.revision,
            succeeded=True,
            receipt={"native_revision": intent.revision},
        ),
    )
    assert intent.status == ActionIntentStatus.SUCCEEDED
    assert intent.receipt == {"native_revision": intent.revision - 1}
    events = await store.list_operation_events(project.id, limit=100)
    assert [event.event_type for event in events] == [
        "action_intent.queued",
        "action_intent.claimed",
        "action_intent.prepared",
        "action_intent.committed",
        "action_intent.succeeded",
    ]


async def test_broker_fails_without_capability_and_idempotency_is_stable(tmp_path):
    store = NebulaStore(tmp_path / "broker-routing.db")
    project, source = await _source(store)
    device = await _device(store)
    broker = ActionBroker(store)
    (
        await broker.heartbeat(
            device.id,
            DeviceCapabilitySnapshot(
                platform="linux",
                app_version="3.0.0",
                capabilities=["clipboard.write"],
                expected_revision=device.revision,
            ),
        )
    )
    request = ActionIntentCreateRequest(
        project_id=project.id,
        resources=[
            ResourceRef(project_id=project.id, kind=ResourceKind.SOURCE, id=source.id)
        ],
        action_id="navigate",
        requester="operator-1",
        idempotency_key="navigate-missing",
    )
    with pytest.raises(ConflictError, match="no healthy paired device"):
        (await broker.create(request))

    updated = await store.get(PairedDeviceSession, device.id)
    (
        await broker.heartbeat(
            device.id,
            DeviceCapabilitySnapshot(
                platform="linux",
                app_version="3.0.0",
                capabilities=["browser.navigate"],
                expected_revision=updated.revision,
            ),
        )
    )
    first = await broker.create(request)
    assert (await broker.create(request)).id == first.id
    with pytest.raises(ConflictError, match="another action intent"):
        (await broker.create(request.model_copy(update={"action_id": "copy"})))


async def test_native_failure_compensates_or_requires_reconciliation(tmp_path):
    store = NebulaStore(tmp_path / "broker-compensation.db")
    project, source = await _source(store)
    device = await _device(store)
    broker = ActionBroker(store)
    ref = ResourceRef(project_id=project.id, kind=ResourceKind.SOURCE, id=source.id)
    device = await broker.heartbeat(
        device.id,
        DeviceCapabilitySnapshot(
            platform="macos",
            app_version="3.0.0",
            capabilities=["browser.navigate"],
            expected_revision=device.revision,
        ),
    )
    intent = await broker.create(
        ActionIntentCreateRequest(
            project_id=project.id,
            resources=[ref],
            action_id="navigate",
            requester="operator-1",
            preferred_device_id=device.id,
            idempotency_key="compensate-1",
        )
    )
    intent = await broker.claim(
        intent.id,
        ActionIntentClaimRequest(
            device_id=device.id, expected_revision=intent.revision
        ),
    )
    intent = await broker.prepare(
        intent.id,
        ActionIntentPrepareRequest(
            device_id=device.id,
            expected_revision=intent.revision,
            preflight_succeeded=True,
        ),
    )
    intent = await broker.commit(
        intent.id,
        ActionIntentCommitRequest(
            expected_revision=intent.revision, core_mutation_committed=True
        ),
    )
    intent = await broker.result(
        intent.id,
        ActionIntentResultRequest(
            device_id=device.id,
            expected_revision=intent.revision,
            succeeded=False,
            error="native apply failed",
            compensation_succeeded=False,
        ),
    )
    assert intent.status == ActionIntentStatus.RECONCILE_REQUIRED
    assert intent.error == "native apply failed"


async def _committed_intent(store, broker, *, core_mutation_committed: bool, key: str):
    project, source = await _source(store)
    device = await _device(store, name=f"Mac {key}")
    ref = ResourceRef(project_id=project.id, kind=ResourceKind.SOURCE, id=source.id)
    device = broker.heartbeat(
        device.id,
        DeviceCapabilitySnapshot(
            platform="macos",
            app_version="3.0.0",
            capabilities=["browser.navigate"],
            expected_revision=device.revision,
        ),
    )
    intent = broker.create(
        ActionIntentCreateRequest(
            project_id=project.id,
            resources=[ref],
            action_id="navigate",
            requester="operator-1",
            preferred_device_id=device.id,
            idempotency_key=key,
        )
    )
    intent = broker.claim(
        intent.id,
        ActionIntentClaimRequest(
            device_id=device.id, expected_revision=intent.revision
        ),
    )
    intent = broker.prepare(
        intent.id,
        ActionIntentPrepareRequest(
            device_id=device.id,
            expected_revision=intent.revision,
            preflight_succeeded=True,
        ),
    )
    intent = broker.commit(
        intent.id,
        ActionIntentCommitRequest(
            expected_revision=intent.revision,
            core_mutation_committed=core_mutation_committed,
        ),
    )
    assert intent.status == ActionIntentStatus.COMMITTED
    return intent


async def test_committed_core_mutation_without_device_result_requires_reconciliation(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "broker-silent-device.db")
    broker = ActionBroker(store)

    committed = await _committed_intent(
        store, broker, core_mutation_committed=True, key="silent-committed"
    )
    plain = await _committed_intent(
        store, broker, core_mutation_committed=False, key="silent-plain"
    )
    # The device never reports: both the lease and the intent expiry lapse.
    later = utc_now() + timedelta(minutes=INTENT_EXPIRY_MINUTES + 1)
    monkeypatch.setattr(action_broker_module, "utc_now", lambda: later)

    expired = await broker.get(committed.id)
    assert expired.status == ActionIntentStatus.RECONCILE_REQUIRED
    assert expired.lease_expires_at is None
    assert "reconcil" in str(expired.error)
    events = await store.list_operation_events(committed.engagement_id, limit=100)
    assert events[-1].event_type == "action_intent.reconcile_required"
    # Terminal state is stable on later reads.
    assert (
        await broker.get(committed.id)
    ).status == ActionIntentStatus.RECONCILE_REQUIRED

    # Without a committed Core mutation there is nothing to reconcile.
    assert (await broker.get(plain.id)).status == ActionIntentStatus.EXPIRED


async def test_actions_resolve_for_an_existing_browser_exchange(tmp_path):
    # The endpoint map named a kind no entity declares, so every action on an
    # exchange was disabled with "no longer exists" while the row was present.
    from nebula.v3.domain import ActionResolutionRequest, BrowserTrafficExchange

    store = NebulaStore(tmp_path / "exchange-actions.db")
    project = await store.create(Engagement(name="Exchange actions"))
    exchange = await store.create(
        BrowserTrafficExchange(
            engagement_id=project.id,
            session_id="session-1",
            tab_id="tab-1",
            identity_id="identity-1",
            method="GET",
            url="https://target.example/login",
            scope_state="in_scope",
            scope_policy_id="scope-1",
            scope_policy_revision=1,
        )
    )
    broker = ActionBroker(store)

    resolved = await broker.registry.resolve(
        ActionResolutionRequest(
            resources=[
                ResourceRef(
                    project_id=project.id,
                    kind=ResourceKind.BROWSER_EXCHANGE,
                    id=exchange.id,
                    revision=exchange.revision,
                )
            ]
        )
    )

    preserve = next(item for item in resolved if item.id == "preserve_as_evidence")
    assert preserve.available is True, preserve.disabled_reason


async def test_heartbeat_stamps_server_time_and_keeps_the_client_clock_as_metadata(
    tmp_path,
):
    # A phone whose clock ran slow could never be healthy, and a fast clock
    # kept an offline phone healthy: the client's timestamp was the liveness.
    store = NebulaStore(tmp_path / "heartbeat.db")
    device = await _device(store)
    client_clock = utc_now() - timedelta(minutes=10)
    broker = ActionBroker(store)

    before = utc_now()
    updated = await broker.heartbeat(
        device.id,
        DeviceCapabilitySnapshot(
            platform="ios",
            app_version="3.0.0",
            capabilities=["share.sheet"],
            heartbeat_at=client_clock,
            expected_revision=device.revision,
        ),
    )

    assert updated.heartbeat_at is not None
    assert updated.heartbeat_at >= before
    assert updated.last_used_at >= before
    assert broker.healthy(updated) is True
    assert updated.metadata["client_heartbeat_at"] == client_clock.isoformat()


async def test_device_routing_sees_live_devices_past_a_thousand_revoked_pairings(
    tmp_path,
):
    store = NebulaStore(tmp_path / "broker-revoked.db")
    project, source = await _source(store)
    now = utc_now()
    expiry = now + timedelta(days=1)
    (
        await store.create_many(
            [
                PairedDeviceSession(
                    name="Revoked phone",
                    token_sha256="c" * 64,
                    csrf_sha256="d" * 64,
                    idle_expires_at=expiry,
                    absolute_expires_at=expiry,
                    revoked_at=now,
                    created_at=now - timedelta(minutes=1),
                )
                for _ in range(1_000)
            ]
        )
    )
    device = await _device(store)
    broker = ActionBroker(store)
    ref = ResourceRef(
        project_id=project.id,
        kind=ResourceKind.SOURCE,
        id=source.id,
        revision=source.revision,
    )
    (
        await broker.heartbeat(
            device.id,
            DeviceCapabilitySnapshot(
                platform="macos",
                app_version="3.0.0",
                capabilities=["browser.navigate"],
                ownership_claims=[ref],
                expected_revision=device.revision,
            ),
        )
    )

    intent = await broker.create(
        ActionIntentCreateRequest(
            project_id=project.id,
            resources=[ref],
            action_id="navigate",
            requester="operator-1",
            idempotency_key="navigate-live",
        )
    )

    assert intent.eligible_device_ids == [device.id]


async def test_idempotent_create_finds_the_prior_intent_past_a_thousand_older_ones(
    tmp_path,
):
    from nebula.v3.domain import ActionIntent

    store = NebulaStore(tmp_path / "broker-crowded.db")
    project, source = await _source(store)
    device = await _device(store)
    broker = ActionBroker(store)
    ref = ResourceRef(
        project_id=project.id,
        kind=ResourceKind.SOURCE,
        id=source.id,
        revision=source.revision,
    )
    (
        await broker.heartbeat(
            device.id,
            DeviceCapabilitySnapshot(
                platform="macos",
                app_version="3.0.0",
                capabilities=["browser.navigate"],
                ownership_claims=[ref],
                expected_revision=device.revision,
            ),
        )
    )
    now = utc_now()
    (
        await store.create_many(
            [
                ActionIntent(
                    engagement_id=project.id,
                    resources=[ref],
                    action_id="navigate",
                    requester="operator-1",
                    idempotency_key=f"older-{index}",
                    logical_lease_key=f"lease-{index}",
                    expires_at=now + timedelta(days=1),
                    created_at=now - timedelta(minutes=1),
                )
                for index in range(1_000)
            ]
        )
    )
    request = ActionIntentCreateRequest(
        project_id=project.id,
        resources=[ref],
        action_id="navigate",
        requester="operator-1",
        idempotency_key="navigate-once",
    )

    first = await broker.create(request)

    assert (await broker.create(request)).id == first.id
    assert (await store.count(ActionIntent, engagement_id=project.id)) == 1_001


pytestmark = pytest.mark.anyio
