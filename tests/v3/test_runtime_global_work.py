"""Contextless Work reads use the global SQL app without an execution owner."""

import httpx
import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select

from nebula.v3.database import ProjectOwnershipRow, RuntimeCommandRow
from nebula.v3.domain import (
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    WorkItem,
    WorkUpdate,
    PairedDeviceSession,
    utc_now,
)
from nebula.v3.harnesses import harness_catalog
from tests.v3.test_runtime_global_reads import reads as reads
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
AUTH = {"Authorization": "Bearer isolated-global-read-token"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_global_work_reads_authenticate_without_a_fake_project_or_owner(reads):
    for path in ("work/items", "work/updates", "work/agents", "harness-catalog"):
        assert (await reads.client.get("/api/v1/" + path)).status_code == 401
        response = await reads.client.get("/api/v1/" + path, headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == (
            jsonable_encoder(harness_catalog()) if path == "harness-catalog" else []
        )
    assert await reads.store.count(Engagement) == 0
    async with reads.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 0
        )
    for method, path in (
        ("POST", "/api/v1/work/import"),
        ("PATCH", "/api/v1/work/projects"),
    ):
        assert not reads.global_api.supports(
            {"type": "http", "method": method, "path": path}
        )


async def test_global_work_reads_preserve_paired_device_auth_and_revocation(reads):
    offer = (
        await reads.client.post(
            "/api/v1/auth/pairings", headers=AUTH, json={"name": "Work viewer"}
        )
    ).json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reads.app), base_url="https://synthetic.test"
    ) as device:
        paired = await device.post(
            "/api/v1/auth/pairings/redeem",
            json={
                "secret": offer["secret"],
                "confirmation_code": offer["confirmation_code"],
                "name": "Work device",
            },
        )
        assert paired.status_code == 200, paired.text
        for path in ("work/items", "work/updates", "work/agents", "harness-catalog"):
            assert (await device.get("/api/v1/" + path)).status_code == 200
        await reads.store.update(
            PairedDeviceSession,
            paired.json()["device"]["id"],
            {"revoked_at": utc_now()},
        )
        assert (await device.get("/api/v1/work/items")).status_code == 401


async def test_global_work_items_keep_canonical_paging_and_project_identity(reads):
    projects = [
        await reads.store.create(Engagement(name=name)) for name in ("First", "Second")
    ]
    items = [
        await reads.store.create(WorkItem(engagement_id=project.id, title=project.name))
        for project in projects
    ]
    first = await reads.client.get("/api/v1/work/items?limit=1", headers=AUTH)
    second = await reads.client.get("/api/v1/work/items?offset=1&limit=1", headers=AUTH)
    assert first.status_code == second.status_code == 200
    assert first.json()[0]["id"] == items[1].id
    assert second.json()[0]["id"] == items[0].id
    assert {first.json()[0]["engagement_id"], second.json()[0]["engagement_id"]} == {
        project.id for project in projects
    }
    for query in ("limit=501", "limit=0", "offset=-1"):
        assert (
            await reads.client.get("/api/v1/work/items?" + query, headers=AUTH)
        ).status_code == 422
    selected = projects[0].id
    filtered = await reads.client.get(
        "/api/v1/work/items", params={"engagement_id": selected}, headers=AUTH
    )
    assert [row["id"] for row in filtered.json()] == [items[0].id]
    for path in ("work/items", "work/updates", "work/agents"):
        repeated = await reads.client.get(
            "/api/v1/" + path,
            params=[("engagement_id", selected), ("project_id", selected)],
            headers=AUTH,
        )
        assert repeated.status_code == 200, repeated.text
        for query in (
            [("engagement_id", selected), ("engagement_id", projects[1].id)],
            [("engagement_id", selected), ("project_id", projects[1].id)],
            [("project_id", "missing")],
        ):
            assert (
                await reads.client.get("/api/v1/" + path, params=query, headers=AUTH)
            ).status_code == 409


async def test_work_update_filter_keeps_parent_chat_attribution_in_child_project(reads):
    parent = await reads.store.create(Engagement(name="Parent"))
    child = await reads.store.create(
        Engagement(name="Child", parent_engagement_id=parent.id)
    )
    chat = await reads.store.create(
        ChatSession(
            engagement_id=parent.id,
            title="Review child work",
            provider_profile_id="inert",
            model="synthetic",
        )
    )
    item = await reads.store.create(
        WorkItem(engagement_id=child.id, title="Child task")
    )
    update = await reads.store.create(
        WorkUpdate(
            engagement_id=child.id,
            item_id=item.id,
            summary="Saved progress",
            status="in_progress",
            actor_kind="agent",
            actor_id=chat.id,
            source_session_id=chat.id,
            source_engagement_id=parent.id,
        )
    )
    response = await reads.client.get(
        "/api/v1/work/updates",
        params={"source_session_id": chat.id, "limit": 1},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert [value["id"] for value in response.json()] == [update.id]
    assert response.json()[0]["engagement_id"] == child.id
    assert response.json()[0]["source_engagement_id"] == parent.id
    assert (
        await reads.client.get(
            "/api/v1/work/updates?source_session_id=unrelated", headers=AUTH
        )
    ).json() == []
    filtered = await reads.client.get(
        "/api/v1/work/updates",
        params={"source_session_id": chat.id, "project_id": child.id},
        headers=AUTH,
    )
    assert [row["id"] for row in filtered.json()] == [update.id]
    assert (
        await reads.client.get(
            "/api/v1/work/updates",
            params={"source_session_id": chat.id, "project_id": parent.id},
            headers=AUTH,
        )
    ).json() == []


async def test_global_work_agents_read_saved_status_and_hide_archived_or_temporary_chats(
    reads,
):
    project = await reads.store.create(Engagement(name="Project"))
    archived = await reads.store.create(Engagement(name="Archived", status="archived"))
    for identity, owner, metadata, status in (
        ("working", project, {}, ChatTurnStatus.ROUTING),
        ("waiting", project, {}, ChatTurnStatus.WAITING_APPROVAL),
        ("temporary", project, {"temporary_assistant": True}, ChatTurnStatus.ROUTING),
        ("archived", archived, {}, ChatTurnStatus.ROUTING),
    ):
        chat = await reads.store.create(
            ChatSession(
                id=identity,
                engagement_id=owner.id,
                title=identity,
                provider_profile_id="inert",
                model="synthetic",
                metadata=metadata,
            )
        )
        await reads.store.create(
            ChatTurn(
                id=identity + "-turn",
                engagement_id=owner.id,
                session_id=chat.id,
                provider_profile_id="inert",
                model="synthetic",
                status=status,
            )
        )
    response = await reads.client.get("/api/v1/work/agents", headers=AUTH)
    assert response.status_code == 200, response.text
    assert {row["session_id"]: row["state"] for row in response.json()} == {
        "working": "working",
        "waiting": "waiting",
    }
    assert {row["engagement_id"] for row in response.json()} == {project.id}
