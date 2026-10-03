"""Focused Work persistence, scope, and API contracts."""

import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient
import pytest

from nebula.v3.api import create_app
from nebula.v3.domain import ChatSession, Engagement, WorkItem, WorkUpdate, utc_now
from nebula.v3.storage import NebulaStore, NotFoundError
from nebula.v3.tools import ToolInvocation
from nebula.v3.work import WorkCheckIn, WorkCreate, WorkService, work_components


def test_work_opt_in_and_check_ins_are_durable_and_idempotent(tmp_path):
    path = tmp_path / "core.db"
    store = NebulaStore(path)
    project = store.create(Engagement(name="Release planning"))
    service = WorkService(store)
    assert not service.enabled(project.id)
    service.set_enabled(project.id, True)
    created = service.create(project.id, WorkCreate(title="Prepare release notes", request_id="create-1"), actor_id="operator")
    assert service.create(project.id, WorkCreate(title="Prepare release notes", request_id="create-1"), actor_id="operator").id == created.id
    check_in = service.check_in(
        project.id, created.id,
        WorkCheckIn(summary="Outlined the changes", next_step="Review wording", status="in_progress", request_id="update-1"),
        actor_kind="operator", actor_id="operator",
    )
    assert service.check_in(project.id, created.id, WorkCheckIn(summary="Outlined the changes", request_id="update-1"), actor_kind="operator", actor_id="operator").id == check_in.id
    reopened = WorkService(NebulaStore(path))
    assert reopened.enabled(project.id)
    assert reopened.get(project.id, created.id).status == "in_progress"
    assert [update.id for update in reopened.updates(project.id, created.id)] == [check_in.id]
    reopened.set_enabled(project.id, False)
    assert reopened.get(project.id, created.id).status == "in_progress"


def test_work_rejects_cross_project_items_and_assignees(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    first = store.create(Engagement(name="First"))
    second = store.create(Engagement(name="Second"))
    service = WorkService(store)
    item = service.create(first.id, WorkCreate(title="First task"), actor_id="operator")
    with pytest.raises(NotFoundError):
        service.get(second.id, item.id)
    with pytest.raises(NotFoundError):
        service.check_in(second.id, item.id, WorkCheckIn(summary="Wrong project"), actor_kind="operator", actor_id="operator")
    assert not store.list_entities(WorkUpdate, engagement_id=second.id)


def test_work_prompt_is_due_only_during_enabled_stale_work(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Sample"))
    service = WorkService(store)
    now = utc_now()
    started = now - timedelta(minutes=21)
    assert not service.update_due(project.id, "agent-1", started, now=now)
    service.set_enabled(project.id, True)
    assert service.update_due(project.id, "agent-1", started, now=now)
    assert not service.update_due(project.id, "agent-1", now - timedelta(minutes=19), now=now)


def test_provider_work_tools_use_chat_scope_and_opt_in(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Release planning"))
    chat = store.create(ChatSession(engagement_id=project.id, title="Plan release", model="test-model", provider_profile_id="provider-1"))
    service = WorkService(store)
    components = work_components(service, project.id, tmp_path)

    async def call(name: str, arguments: dict):
        return await components.broker.execute(ToolInvocation(
            engagement_id=project.id, run_id="turn-1", chat_session_id=chat.id,
            tool_name=name, arguments=arguments, workspace=tmp_path,
        ), components.scope)

    with pytest.raises(ValueError, match="off"):
        asyncio.run(call("work_list", {}))
    service.set_enabled(project.id, True)
    created = asyncio.run(call("work_create", {"title": "Prepare notes", "request_id": "create-1"})).output["item"]
    assert created["assignee_session_id"] == chat.id
    assert created["source_id"] == chat.id
    updated = asyncio.run(call("work_check_in", {"item_id": created["id"], "summary": "Draft ready", "request_id": "update-1"})).output["update"]
    assert updated["source_session_id"] == chat.id
    assert len(service.updates(project.id, created["id"])) == 1


def test_work_api_auth_scope_and_refresh(tmp_path):
    path = tmp_path / "core.db"
    store = NebulaStore(path)
    project = store.create(Engagement(name="Documentation portal"))
    other = store.create(Engagement(name="Another project"))
    client = TestClient(create_app(store, auth_token="test-token"))
    root = f"/api/v1/engagements/{project.id}/work"
    auth = {"Authorization": "Bearer test-token"}
    assert client.get(root).status_code == 401
    assert client.patch(f"{root}/setting", headers=auth, json={"enabled": True}).json()["work_enabled"] is True
    response = client.post(root, headers=auth, json={"title": "Build the search page", "status": "ready"})
    assert response.status_code == 200, response.text
    item_id = response.json()["id"]
    assert client.get(f"/api/v1/engagements/{other.id}/work/{item_id}", headers=auth).status_code == 404
    update = client.post(f"{root}/{item_id}/updates", headers=auth, json={"summary": "Search layout is ready", "status": "review"})
    assert update.status_code == 200, update.text
    assert client.get(f"{root}/{item_id}", headers=auth).json()["status"] == "review"
    reloaded = TestClient(create_app(NebulaStore(path), auth_token="test-token"))
    assert reloaded.get(root, headers=auth).json()[0]["id"] == item_id
    assert reloaded.get(f"{root}/{item_id}/updates", headers=auth).json()[0]["summary"] == "Search layout is ready"
    assert reloaded.get("/api/v1/work/updates", headers=auth).json()[0]["item_id"] == item_id
