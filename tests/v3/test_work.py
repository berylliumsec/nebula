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
    created = service.create(
        project.id,
        WorkCreate(title="Prepare release notes", request_id="create-1"),
        actor_id="operator",
    )
    assert (
        service.create(
            project.id,
            WorkCreate(title="Prepare release notes", request_id="create-1"),
            actor_id="operator",
        ).id
        == created.id
    )
    check_in = service.check_in(
        project.id,
        created.id,
        WorkCheckIn(
            summary="Outlined the changes",
            next_step="Review wording",
            status="in_progress",
            request_id="update-1",
        ),
        actor_kind="operator",
        actor_id="operator",
    )
    assert (
        service.check_in(
            project.id,
            created.id,
            WorkCheckIn(summary="Outlined the changes", request_id="update-1"),
            actor_kind="operator",
            actor_id="operator",
        ).id
        == check_in.id
    )
    reopened = WorkService(NebulaStore(path))
    assert reopened.enabled(project.id)
    assert reopened.get(project.id, created.id).status == "in_progress"
    assert [update.id for update in reopened.updates(project.id, created.id)] == [
        check_in.id
    ]
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
        service.check_in(
            second.id,
            item.id,
            WorkCheckIn(summary="Wrong project"),
            actor_kind="operator",
            actor_id="operator",
        )
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
    assert not service.update_due(
        project.id, "agent-1", now - timedelta(minutes=19), now=now
    )


def test_provider_work_tools_use_chat_scope_and_opt_in(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Release planning"))
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Plan release",
            model="test-model",
            provider_profile_id="provider-1",
        )
    )
    service = WorkService(store)
    components = work_components(service, project.id, tmp_path)

    async def call(name: str, arguments: dict):
        return await components.broker.execute(
            ToolInvocation(
                engagement_id=project.id,
                run_id="turn-1",
                chat_session_id=chat.id,
                tool_name=name,
                arguments=arguments,
                workspace=tmp_path,
            ),
            components.scope,
        )

    with pytest.raises(ValueError, match="off"):
        asyncio.run(call("work_list", {}))
    service.set_enabled(project.id, True)
    created = asyncio.run(
        call("work_create", {"title": "Prepare notes", "request_id": "create-1"})
    ).output["item"]
    assert created["assignee_session_id"] == chat.id
    assert created["source_id"] == chat.id
    updated = asyncio.run(
        call(
            "work_check_in",
            {
                "item_id": created["id"],
                "summary": "Draft ready",
                "request_id": "update-1",
            },
        )
    ).output["update"]
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
    assert (
        client.patch(f"{root}/setting", headers=auth, json={"enabled": True}).json()[
            "work_enabled"
        ]
        is True
    )
    response = client.post(
        root, headers=auth, json={"title": "Build the search page", "status": "ready"}
    )
    assert response.status_code == 200, response.text
    item_id = response.json()["id"]
    assert (
        client.get(
            f"/api/v1/engagements/{other.id}/work/{item_id}", headers=auth
        ).status_code
        == 404
    )
    update = client.post(
        f"{root}/{item_id}/updates",
        headers=auth,
        json={"summary": "Search layout is ready", "status": "review"},
    )
    assert update.status_code == 200, update.text
    assert client.get(f"{root}/{item_id}", headers=auth).json()["status"] == "review"
    reloaded = TestClient(create_app(NebulaStore(path), auth_token="test-token"))
    assert reloaded.get(root, headers=auth).json()[0]["id"] == item_id
    assert (
        reloaded.get(f"{root}/{item_id}/updates", headers=auth).json()[0]["summary"]
        == "Search layout is ready"
    )
    assert (
        reloaded.get("/api/v1/work/updates", headers=auth).json()[0]["item_id"]
        == item_id
    )


def test_work_import_api_creates_projects_items_and_updates_without_duplicate_or_overwrite(
    tmp_path,
):
    path = tmp_path / "core.db"
    store = NebulaStore(path)
    client = TestClient(create_app(store, auth_token="test-token"))
    auth = {"Authorization": "Bearer test-token"}
    workspace = tmp_path / "source-workspace"
    workspace.mkdir()
    body = {
        "source": "sample-tracker",
        "projects": [
            {
                "external_id": "release-plan",
                "name": "Release plan",
                "status": "active",
                "workspace_path": str(workspace),
                "items": [
                    {
                        "external_id": "task-1",
                        "title": "Review the checklist",
                        "status": "in_progress",
                        "priority": "high",
                        "update": {
                            "summary": "Draft checklist received",
                            "next_step": "Review entries",
                        },
                    }
                ],
            }
        ],
    }
    assert client.post("/api/v1/work/import", json=body).status_code == 401
    first = client.post("/api/v1/work/import", headers=auth, json=body)
    assert first.status_code == 200, first.text
    project = first.json()["projects"][0]
    project_id = project["engagement_id"]
    item_id = project["items"][0]["item_id"]
    assert store.get(Engagement, project_id).work_enabled is False
    assert store.get(Engagement, project_id).workspace_path == str(workspace.resolve())
    assert store.get(WorkItem, item_id).source_kind == "import"
    assert (
        client.get(f"/api/v1/engagements/{project_id}/work", headers=auth).json()[0][
            "id"
        ]
        == item_id
    )
    assert (
        len(
            client.get(
                f"/api/v1/engagements/{project_id}/work/{item_id}/updates", headers=auth
            ).json()
        )
        == 1
    )

    changed = client.post(
        f"/api/v1/engagements/{project_id}/work/{item_id}/updates",
        headers=auth,
        json={"summary": "Review complete", "status": "review"},
    )
    assert changed.status_code == 200, changed.text
    repeated = client.post("/api/v1/work/import", headers=auth, json=body)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == first.json()
    reloaded = TestClient(create_app(NebulaStore(path), auth_token="test-token"))
    assert (
        reloaded.get(
            f"/api/v1/engagements/{project_id}/work/{item_id}", headers=auth
        ).json()["status"]
        == "review"
    )
    assert (
        len(
            reloaded.get(
                f"/api/v1/engagements/{project_id}/work/{item_id}/updates", headers=auth
            ).json()
        )
        == 2
    )
    update_pages = [
        reloaded.get(
            f"/api/v1/work/updates?offset={offset}&limit=1", headers=auth
        ).json()
        for offset in (0, 1)
    ]
    assert update_pages[0][0]["id"] != update_pages[1][0]["id"]


def test_work_import_api_can_attach_to_existing_project_and_reject_duplicate_ids(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    existing = store.create(Engagement(name="Existing plan"))
    client = TestClient(create_app(store, auth_token="test-token"))
    auth = {"Authorization": "Bearer test-token"}
    project = {
        "external_id": "external-plan",
        "engagement_id": existing.id,
        "name": "External plan",
        "items": [
            {"external_id": "queued", "title": "Pick next task", "status": "ready"},
            {
                "external_id": "blocked",
                "title": "Resolve access",
                "status": "blocked",
                "update": {
                    "summary": "Access is pending",
                    "blocker": "Approval required",
                },
            },
        ],
    }
    invalid = client.post(
        "/api/v1/work/import",
        headers=auth,
        json={
            "source": "sample-tracker",
            "projects": [project, project],
        },
    )
    assert invalid.status_code == 422
    assert store.list_entities(WorkItem, engagement_id=existing.id) == []
    missing_workspace = client.post(
        "/api/v1/work/import",
        headers=auth,
        json={
            "source": "sample-tracker",
            "projects": [
                {
                    "external_id": "missing",
                    "name": "Missing folder",
                    "workspace_path": str(tmp_path / "missing"),
                }
            ],
        },
    )
    assert missing_workspace.status_code == 422
    saved = client.post(
        "/api/v1/work/import",
        headers=auth,
        json={
            "source": "sample-tracker",
            "projects": [project],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["projects"][0]["engagement_id"] == existing.id
    assert len(store.list_entities(WorkItem, engagement_id=existing.id)) == 2
    assert len(store.list_entities(WorkUpdate, engagement_id=existing.id)) == 1
    assert store.get(Engagement, existing.id).name == "Existing plan"
    first_page = client.get("/api/v1/work/items?offset=0&limit=1", headers=auth)
    second_page = client.get("/api/v1/work/items?offset=1&limit=1", headers=auth)
    assert first_page.status_code == second_page.status_code == 200
    assert first_page.json()[0]["id"] != second_page.json()[0]["id"]
