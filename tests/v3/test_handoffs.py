from __future__ import annotations
import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
import time
from datetime import timedelta

from tests.v3.async_test_support import AsyncTestClient as TestClient
from sqlalchemy import select

from nebula.v3.api import create_app
from nebula.v3.database import EntityRow, OperationEventRow
from nebula.v3.domain import (
    Engagement,
    Evidence,
    HandoffEnvelope,
    HandoffStatus,
    ResourceKind,
    ResourceRef,
)
from nebula.v3.handoffs import (
    HandoffCancelRequest,
    HandoffConsumeRequest,
    HandoffCreateRequest,
    HandoffService,
)
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError


async def _fixture(tmp_path):
    store = NebulaStore(tmp_path / "handoffs.db")
    project = await store.create(Engagement(name="Handoffs"))
    evidence = await store.create(
        Evidence(
            engagement_id=project.id,
            evidence_type="capture",
            title="Request capture",
            sha256="a" * 64,
        )
    )
    ref = ResourceRef(
        project_id=project.id,
        kind=ResourceKind.EVIDENCE,
        id=evidence.id,
        revision=evidence.revision,
    )
    return store, project, evidence, ref


async def test_reference_only_handoff_survives_refresh_and_consumes_idempotently(
    tmp_path,
):
    store, project, _, ref = await _fixture(tmp_path)
    service = HandoffService(store)
    envelope = await service.create(
        HandoffCreateRequest(
            project_id=project.id,
            source_refs=[ref],
            action_id="ask_nebula",
            origin_device_id="mac",
            source_hashes={f"evidence:{ref.id}": "a" * 64},
            source_labels={f"evidence:{ref.id}": "Request capture"},
        ),
        actor_id="operator",
    )

    refreshed = await HandoffService(NebulaStore(store.database)).resolve(
        envelope.id, current_device_id="linux"
    )
    assert refreshed.recovery == "ready"
    assert refreshed.sources[0].state == "available"
    consumed = await service.consume(
        envelope.id,
        HandoffConsumeRequest(
            expected_revision=envelope.revision,
            device_id="linux",
            idempotency_key="consume-once",
        ),
    )
    assert consumed.status == HandoffStatus.CONSUMED
    assert (
        await service.consume(
            envelope.id,
            HandoffConsumeRequest(
                expected_revision=envelope.revision,
                device_id="linux",
                idempotency_key="consume-once",
            ),
        )
    ).revision == consumed.revision
    async with store.database.session() as session:
        events = list(
            (
                await session.scalars(
                    select(OperationEventRow).where(
                        OperationEventRow.operation_id == envelope.id
                    )
                )
            )
        )
    assert [event.event_type for event in events] == [
        "handoff.created",
        "handoff.consumed",
    ]


async def test_transient_handoff_requires_origin_or_recapture_and_never_stores_bytes(
    tmp_path,
):
    store, project, _, ref = await _fixture(tmp_path)
    secret_selection = "UNSENT_SELECTED_BYTES_MUST_STAY_IN_MEMORY"
    service = HandoffService(store)
    envelope = await service.create(
        HandoffCreateRequest(
            project_id=project.id,
            source_refs=[ref],
            action_id="take_note",
            origin_device_id="mac",
            source_hashes={f"evidence:{ref.id}": "b" * 64},
            source_labels={f"evidence:{ref.id}": "Selected request text"},
            transient=True,
        ),
        actor_id="operator",
    )
    assert (
        await service.resolve(envelope.id, current_device_id="linux")
    ).recovery == "resume_origin"
    assert (
        await service.resolve(envelope.id, current_device_id="mac")
    ).recovery == "preserve_or_recapture"
    async with store.database.session() as session:
        serialized = json.dumps(
            [row.payload for row in (await session.scalars(select(EntityRow)))],
            sort_keys=True,
        )
    assert secret_selection not in serialized
    assert (
        set(HandoffEnvelope.model_fields) & {"text", "bytes", "content", "raw_text"}
        == set()
    )


async def test_handoff_revision_guards_and_cancel_are_fail_closed(tmp_path):
    store, project, evidence, ref = await _fixture(tmp_path)
    service = HandoffService(store)
    (
        await store.update(
            Evidence, evidence.id, {"description": "changed"}, expected_revision=1
        )
    )
    try:
        (
            await service.create(
                HandoffCreateRequest(
                    project_id=project.id,
                    source_refs=[ref],
                    action_id="add_to_report",
                    origin_device_id="mac",
                ),
                actor_id="operator",
            )
        )
    except ConflictError as exc:
        assert "revision conflict" in str(exc)
    else:
        raise AssertionError("stale handoff source was accepted")

    current_ref = ref.model_copy(update={"revision": 2})
    envelope = await service.create(
        HandoffCreateRequest(
            project_id=project.id,
            source_refs=[current_ref],
            action_id="add_to_report",
            origin_device_id="mac",
        ),
        actor_id="operator",
    )
    cancelled = await service.cancel(
        envelope.id,
        HandoffCancelRequest(expected_revision=envelope.revision),
        actor_id="operator",
    )
    assert cancelled.status == HandoffStatus.CANCELLED


async def test_handoff_api_rejects_raw_selected_text(tmp_path):
    store, project, _, ref = await _fixture(tmp_path)
    client = TestClient(create_app(store, auth_token="test-token"))
    response = await client.post(
        "/api/v1/handoffs",
        headers={"Authorization": "Bearer test-token"},
        json={
            "project_id": project.id,
            "source_refs": [ref.model_dump(mode="json")],
            "action_id": "ask_nebula",
            "origin_device_id": "mac",
            "transient": True,
            "raw_text": "must be rejected",
        },
    )
    assert response.status_code == 422


async def test_handoff_rejects_missing_project_and_unbounded_labels(tmp_path):
    store, project, _, ref = await _fixture(tmp_path)
    service = HandoffService(store)
    invalid_project = ref.model_copy(update={"project_id": "missing"})
    try:
        (
            await service.create(
                HandoffCreateRequest(
                    project_id="missing",
                    source_refs=[invalid_project],
                    action_id="ask_nebula",
                    origin_device_id="mac",
                ),
                actor_id="operator",
            )
        )
    except NotFoundError as exc:
        assert "not found" in str(exc).lower()
    else:
        raise AssertionError("handoff accepted a missing project")

    try:
        (
            await service.create(
                HandoffCreateRequest(
                    project_id=project.id,
                    source_refs=[ref],
                    action_id="ask_nebula",
                    origin_device_id="mac",
                    source_labels={f"evidence:{ref.id}": "x" * 301},
                ),
                actor_id="operator",
            )
        )
    except ValueError as exc:
        assert "300 characters" in str(exc)
    else:
        raise AssertionError("handoff accepted an unbounded label")


async def test_handoff_list_pages_newest_first(tmp_path):
    store, project, _, ref = await _fixture(tmp_path)
    service = HandoffService(store)
    created = []
    for index in range(3):
        created.append(
            (
                await service.create(
                    HandoffCreateRequest(
                        project_id=project.id,
                        source_refs=[ref],
                        action_id=f"action-{index}",
                        origin_device_id="mac",
                    ),
                    actor_id="operator",
                )
            )
        )
        time.sleep(0.002)
    newest_first = [item.id for item in reversed(created)]
    assert [item.id for item in (await service.list(project.id))] == newest_first
    assert [
        item.id for item in (await service.list(project.id, limit=2))
    ] == newest_first[:2]
    assert [
        item.id for item in (await service.list(project.id, offset=2, limit=2))
    ] == newest_first[2:]
    client = TestClient(create_app(store, auth_token="test-token"))
    response = await client.get(
        "/api/v1/handoffs",
        params={"project_id": project.id, "offset": 1, "limit": 1},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == newest_first[1:2]


async def test_expire_on_read_survives_a_concurrent_expiry(tmp_path):
    store, project, _, ref = await _fixture(tmp_path)
    service = HandoffService(store)
    envelope = await service.create(
        HandoffCreateRequest(
            project_id=project.id,
            source_refs=[ref],
            action_id="ask_nebula",
            origin_device_id="mac",
        ),
        actor_id="operator",
    )
    (
        await store.update(
            HandoffEnvelope,
            envelope.id,
            {"expires_at": envelope.created_at + timedelta(microseconds=1)},
            expected_revision=envelope.revision,
        )
    )
    rival = HandoffService(NebulaStore(store.database))
    real_update = store.update_with_operation_event

    async def update_after_rival_expires(*args, **kwargs):
        store.update_with_operation_event = real_update
        assert (await rival.get(envelope.id)).status == HandoffStatus.EXPIRED
        return await real_update(*args, **kwargs)

    store.update_with_operation_event = update_after_rival_expires
    resolved = await service.resolve(envelope.id, current_device_id="linux")
    assert resolved.envelope.status == HandoffStatus.EXPIRED
    assert resolved.envelope.revision == 3
    async with store.database.session() as session:
        events = list(
            (
                await session.scalars(
                    select(OperationEventRow).where(
                        OperationEventRow.operation_id == envelope.id
                    )
                )
            )
        )
    assert [event.event_type for event in events] == [
        "handoff.created",
        "handoff.expired",
    ]


pytestmark = pytest.mark.anyio
