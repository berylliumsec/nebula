"""The conversation snapshot can find Work saved in a child project."""

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import ChatSession, Engagement
from nebula.v3.storage import NebulaStore
from nebula.v3.work import WorkCheckIn, WorkCreate, WorkService


def test_recent_work_updates_filter_finds_parent_chat_in_child_project(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    parent = store.create(Engagement(name="Portfolio"))
    child = store.create(
        Engagement(name="Child project", parent_engagement_id=parent.id)
    )
    chat = store.create(
        ChatSession(
            engagement_id=parent.id,
            title="Progress review",
            model="fixture",
            provider_profile_id="fixture-provider",
        )
    )
    unrelated = store.create(
        ChatSession(
            engagement_id=parent.id,
            title="Other",
            model="fixture",
            provider_profile_id="fixture-provider",
        )
    )
    work = WorkService(store)
    item = work.create(
        child.id,
        WorkCreate(title="Complete project task", source_kind="import"),
        actor_id="fixture",
    )
    update = work.check_in(
        child.id,
        item.id,
        WorkCheckIn(summary="The current step is complete."),
        actor_kind="agent",
        actor_id=chat.id,
        source_session_id=chat.id,
        allow_parent_source_session=True,
    )
    client = TestClient(create_app(store, auth_token="test-token"))
    auth = {"Authorization": "Bearer test-token"}

    response = client.get(
        "/api/v1/work/updates",
        params={"source_session_id": chat.id, "limit": 1},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    assert [entry["id"] for entry in response.json()] == [update.id]
    assert response.json()[0]["engagement_id"] == child.id
    assert response.json()[0]["source_engagement_id"] == parent.id
    assert (
        client.get(
            "/api/v1/work/updates",
            params={"source_session_id": unrelated.id},
            headers=auth,
        ).json()
        == []
    )
