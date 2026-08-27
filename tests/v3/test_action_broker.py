from __future__ import annotations

from datetime import timedelta

import pytest

from nebula.v3.action_broker import (
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


def _device(store: NebulaStore, name: str = "Mac") -> PairedDeviceSession:
    now = utc_now()
    device = store.create(
        PairedDeviceSession(
            name=name,
            token_sha256="a" * 64,
            csrf_sha256="b" * 64,
            idle_expires_at=now + timedelta(days=30),
            absolute_expires_at=now + timedelta(days=90),
        )
    )
    return device


def _source(store: NebulaStore):
    project = store.create(Engagement(name="Broker"))
    source = store.create(
        KnowledgeSource(
            engagement_id=project.id,
            name="Portal",
            source_type="url",
        )
    )
    return project, source


def test_broker_routes_to_owner_and_requires_prepare_before_commit(tmp_path):
    store = NebulaStore(tmp_path / "broker.db")
    project, source = _source(store)
    device = _device(store)
    broker = ActionBroker(store)
    ref = ResourceRef(
        project_id=project.id,
        kind=ResourceKind.SOURCE,
        id=source.id,
        revision=source.revision,
    )
    device = broker.heartbeat(
        device.id,
        DeviceCapabilitySnapshot(
            platform="macos",
            app_version="3.0.0",
            capabilities=["browser.navigate"],
            ownership_claims=[ref],
            expected_revision=device.revision,
        ),
    )
    intent = broker.create(
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
        broker.commit(
            intent.id, ActionIntentCommitRequest(expected_revision=intent.revision)
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
        intent.id, ActionIntentCommitRequest(expected_revision=intent.revision)
    )
    intent = broker.result(
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
    events = store.list_operation_events(project.id, limit=100)
    assert [event.event_type for event in events] == [
        "action_intent.queued",
        "action_intent.claimed",
        "action_intent.prepared",
        "action_intent.committed",
        "action_intent.succeeded",
    ]


def test_broker_fails_without_capability_and_idempotency_is_stable(tmp_path):
    store = NebulaStore(tmp_path / "broker-routing.db")
    project, source = _source(store)
    device = _device(store)
    broker = ActionBroker(store)
    broker.heartbeat(
        device.id,
        DeviceCapabilitySnapshot(
            platform="linux",
            app_version="3.0.0",
            capabilities=["clipboard.write"],
            expected_revision=device.revision,
        ),
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
        broker.create(request)

    updated = store.get(PairedDeviceSession, device.id)
    broker.heartbeat(
        device.id,
        DeviceCapabilitySnapshot(
            platform="linux",
            app_version="3.0.0",
            capabilities=["browser.navigate"],
            expected_revision=updated.revision,
        ),
    )
    first = broker.create(request)
    assert broker.create(request).id == first.id
    with pytest.raises(ConflictError, match="another action intent"):
        broker.create(request.model_copy(update={"action_id": "copy"}))


def test_native_failure_compensates_or_requires_reconciliation(tmp_path):
    store = NebulaStore(tmp_path / "broker-compensation.db")
    project, source = _source(store)
    device = _device(store)
    broker = ActionBroker(store)
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
            idempotency_key="compensate-1",
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
            expected_revision=intent.revision, core_mutation_committed=True
        ),
    )
    intent = broker.result(
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
