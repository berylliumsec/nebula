"""Focused Work persistence, scope, and API contracts."""

import asyncio
import json

from fastapi.testclient import TestClient
import pytest

import nebula.v3.chat as chat_module
from nebula.v3.api import create_app
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.domain import (
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    WorkItem,
    WorkUpdate,
)
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError
from nebula.v3.tool_results import MAX_EXCERPT_BYTES, serialize_model_result
from nebula.v3.tools import ToolInvocation, ToolNotPermitted
from nebula.v3.providers import ModelRequest, ModelResponse
from nebula.v3.work import (
    WorkCheckIn,
    WorkCreate,
    WorkPatch,
    WorkService,
    work_components,
    work_roster_components,
)
from tests.v3.test_chat import FakeProvider


def test_work_default_on_and_check_ins_are_durable_and_idempotent(tmp_path):
    path = tmp_path / "core.db"
    store = NebulaStore(path)
    project = store.create(Engagement(name="Release planning"))
    service = WorkService(store)
    assert service.enabled(project.id)
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
    assert not WorkService(NebulaStore(path)).enabled(project.id)


def test_work_item_revision_checks_preserve_edits_and_idempotent_check_ins(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Revision checks"))
    service = WorkService(store)
    item = service.create(
        project.id, WorkCreate(title="Review plan"), actor_id="operator"
    )
    changed = service.patch(
        project.id,
        item.id,
        WorkPatch(priority="high", expected_revision=item.revision),
    )
    with pytest.raises(ConflictError, match="work item changed"):
        service.patch(
            project.id,
            item.id,
            WorkPatch(title="Stale title", expected_revision=item.revision),
        )
    with pytest.raises(ConflictError, match="work item changed"):
        service.check_in(
            project.id,
            item.id,
            WorkCheckIn(summary="Stale status", expected_revision=item.revision),
            actor_kind="operator",
            actor_id="operator",
        )
    assert service.get(project.id, item.id).title == "Review plan"
    assert service.updates(project.id, item.id) == []
    request = WorkCheckIn(
        summary="Ready to review",
        status="review",
        request_id="check-1",
        expected_revision=changed.revision,
    )
    first = service.check_in(
        project.id, item.id, request, actor_kind="operator", actor_id="operator"
    )
    repeated = service.check_in(
        project.id, item.id, request, actor_kind="operator", actor_id="operator"
    )
    assert repeated.id == first.id
    assert service.get(project.id, item.id).status == "review"
    assert len(service.updates(project.id, item.id)) == 1


def test_work_project_batch_links_existing_projects_and_prevents_cycles(tmp_path):
    path = tmp_path / "core.db"
    store = NebulaStore(path)
    parent = store.create(Engagement(name="Portfolio", work_enabled=False))
    children = [
        store.create(Engagement(name=f"Plan {index}", work_enabled=False))
        for index in range(2)
    ]
    client = TestClient(create_app(store, auth_token="test-token"))
    auth = {"Authorization": "Bearer test-token"}
    body = {
        "project_ids": [child.id for child in children],
        "parent_engagement_id": parent.id,
        "work_enabled": True,
    }
    assert client.patch("/api/v1/work/projects", json=body).status_code == 401
    response = client.patch("/api/v1/work/projects", headers=auth, json=body)
    assert response.status_code == 200, response.text
    assert response.json() == {"updated": 2}
    assert client.patch("/api/v1/work/projects", headers=auth, json=body).json() == {
        "updated": 0
    }
    reloaded = NebulaStore(path)
    assert all(
        reloaded.get(Engagement, child.id).parent_engagement_id == parent.id
        for child in children
    )
    assert all(reloaded.get(Engagement, child.id).work_enabled for child in children)
    assert not reloaded.get(Engagement, parent.id).work_enabled
    cycle = client.patch(
        "/api/v1/work/projects",
        headers=auth,
        json={"project_ids": [parent.id], "parent_engagement_id": children[0].id},
    )
    assert cycle.status_code == 409
    assert reloaded.get(Engagement, parent.id).parent_engagement_id is None
    assert (
        client.patch(
            "/api/v1/work/projects",
            headers=auth,
            json={
                "project_ids": [children[0].id],
                "parent_engagement_id": children[0].id,
            },
        ).status_code
        == 409
    )
    assert (
        client.patch(
            "/api/v1/work/projects",
            headers=auth,
            json={"project_ids": ["missing"], "parent_engagement_id": parent.id},
        ).status_code
        == 404
    )
    assert (
        client.patch(
            "/api/v1/work/projects",
            headers=auth,
            json={
                "project_ids": [children[0].id, children[0].id],
                "parent_engagement_id": parent.id,
            },
        ).status_code
        == 422
    )


def test_work_import_can_create_child_under_existing_project(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    parent = store.create(Engagement(name="Portfolio"))
    client = TestClient(create_app(store, auth_token="test-token"))
    auth = {"Authorization": "Bearer test-token"}
    body = {
        "source": "sample-tracker",
        "projects": [
            {
                "external_id": "child-1",
                "name": "Research plan",
                "parent_engagement_id": parent.id,
            }
        ],
    }
    response = client.post("/api/v1/work/import", headers=auth, json=body)
    assert response.status_code == 200, response.text
    child_id = response.json()["projects"][0]["engagement_id"]
    assert store.get(Engagement, child_id).parent_engagement_id == parent.id
    assert (
        client.post("/api/v1/work/import", headers=auth, json=body).json()
        == response.json()
    )
    invalid = client.post(
        "/api/v1/work/import",
        headers=auth,
        json={
            "source": "sample-tracker",
            "projects": [
                {
                    "external_id": "child-2",
                    "name": "Missing parent",
                    "parent_engagement_id": "missing",
                }
            ],
        },
    )
    assert invalid.status_code == 404
    assert (
        client.post(
            "/api/v1/work/import",
            headers=auth,
            json={
                "source": "sample-tracker",
                "projects": [
                    {
                        "external_id": "child-3",
                        "name": "Ambiguous",
                        "engagement_id": parent.id,
                        "parent_engagement_id": parent.id,
                    }
                ],
            },
        ).status_code
        == 422
    )


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


def test_provider_work_tools_use_chat_scope_and_opt_out(tmp_path):
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

    assert asyncio.run(call("work_list", {})).output == {
        "items": [],
        "offset": 0,
        "next_offset": None,
    }
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
    service.set_enabled(project.id, False)
    with pytest.raises(ValueError, match="off"):
        asyncio.run(call("work_list", {}))


def test_provider_work_tools_reach_only_linked_child_project(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    parent = store.create(Engagement(name="Parent project"))
    child = store.create(
        Engagement(name="linked_child", parent_engagement_id=parent.id)
    )
    unrelated = store.create(Engagement(name="Unrelated"))
    chat = store.create(
        ChatSession(
            engagement_id=parent.id,
            title="Research",
            model="test-model",
            provider_profile_id="provider-1",
        )
    )
    service = WorkService(store)
    item = service.create(
        child.id, WorkCreate(title="Research status"), actor_id="import"
    )
    other = service.create(unrelated.id, WorkCreate(title="Other"), actor_id="import")
    components = work_components(service, parent.id, tmp_path)

    async def call(name: str, arguments: dict):
        return await components.broker.execute(
            ToolInvocation(
                engagement_id=parent.id,
                run_id="turn-1",
                chat_session_id=chat.id,
                tool_name=name,
                arguments=arguments,
                workspace=tmp_path,
            ),
            components.scope,
        )

    assert [
        row["id"]
        for row in asyncio.run(call("work_list", {"project_name": child.name})).output[
            "items"
        ]
    ] == [item.id]
    assert (
        asyncio.run(call("work_get", {"item_id": item.id})).output["item"]["id"]
        == item.id
    )
    update = asyncio.run(
        call(
            "work_check_in",
            {
                "item_id": item.id,
                "summary": "Reviewed",
                "request_id": "review-1",
            },
        )
    ).output["update"]
    assert update["source_session_id"] == chat.id
    assert update["source_engagement_id"] == parent.id
    with pytest.raises(NotFoundError):
        asyncio.run(call("work_list", {"project_name": unrelated.name}))
    with pytest.raises(NotFoundError):
        asyncio.run(call("work_get", {"item_id": other.id}))
    with pytest.raises(NotFoundError):
        asyncio.run(call("work_check_in", {"item_id": other.id, "summary": "No"}))


def test_provider_work_pages_and_reads_full_long_item(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Large Work project"))
    service = WorkService(store)
    long_description = "🙂" * 20_000
    original = store.create(
        WorkItem(
            engagement_id=project.id,
            title="Long description",
            description=long_description,
        )
    )
    for index in range(75):
        store.create(
            WorkItem(
                engagement_id=project.id,
                title=f"Task {index:03d}",
                description="Details " * 125,
            )
        )
    components = work_components(service, project.id, tmp_path)
    assert "next_offset" in components.specs["work_list"].description
    assert "next_description_offset" in components.specs["work_get"].description

    async def call(name: str, arguments: dict):
        return await components.broker.execute(
            ToolInvocation(
                engagement_id=project.id,
                run_id="turn-1",
                tool_name=name,
                arguments=arguments,
                workspace=tmp_path,
            ),
            components.scope,
        )

    offset = None
    seen: list[str] = []
    while True:
        arguments = {} if offset is None else {"offset": offset}
        page = asyncio.run(call("work_list", arguments)).output
        delivered = serialize_model_result(page)
        assert len(delivered.encode("utf-8")) <= MAX_EXCERPT_BYTES
        assert "nebula.bounded-result/v1" not in delivered
        assert page["items"]
        assert all("description_preview" in item for item in page["items"])
        assert all("description" not in item for item in page["items"])
        seen.extend(item["id"] for item in page["items"])
        offset = page["next_offset"]
        if offset is None:
            break
    assert len(seen) == len(set(seen)) == 76
    assert set(seen) == {item.id for item in service.list(project.id)}
    assert original.id in seen

    description_offset = None
    chunks: list[str] = []
    while True:
        arguments = {"item_id": original.id}
        if description_offset is not None:
            arguments["description_offset"] = description_offset
        result = asyncio.run(call("work_get", arguments)).output
        delivered = serialize_model_result(result)
        assert len(delivered.encode("utf-8")) <= MAX_EXCERPT_BYTES
        assert "nebula.bounded-result/v1" not in delivered
        chunks.append(result["item"]["description"])
        description_offset = result["next_description_offset"]
        if description_offset is None:
            break
    assert "".join(chunks) == long_description


def test_provider_work_list_reaches_items_after_previous_500_cap(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Imported tasks"))
    for index in range(505):
        store.create(WorkItem(engagement_id=project.id, title=f"Task {index}"))
    service = WorkService(store)
    offset = 0
    seen: list[str] = []
    while True:
        page = service.agent_list_page(project.id, offset=offset, limit=100)
        assert len(serialize_model_result(page).encode("utf-8")) <= MAX_EXCERPT_BYTES
        seen.extend(item["id"] for item in page["items"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 505


def test_provider_chat_can_follow_work_list_pages(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Agent pagination"))
    profile = store.create(
        ProviderProfile(
            name="Local provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"tool_calling": True},
            capability_verifications={
                "model-a": {"model": "model-a", "status": "verified"}
            },
        )
    )
    for index in range(45):
        store.create(
            WorkItem(
                engagement_id=project.id,
                title=f"Task {index}",
                description="Context " * 125,
            )
        )

    class PagingProvider(FakeProvider):
        def __init__(self):
            super().__init__(profile.id, local=True)
            self.config.capabilities.tools = True
            self.config.capabilities.strict_tools = True
            self.seen: list[str] = []
            self.pages = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("operation"):
                return await super().complete(request)
            self.requests.append(request)
            assert "next_offset" in (request.instructions or "")
            assert {"work_list", "work_get"} <= {tool.name for tool in request.tools}
            if request.tool_results:
                last = request.tool_results[-1].output
                page = json.loads(last) if isinstance(last, str) else last
                assert page.get("schema") != "nebula.bounded-result/v1"
                self.seen.extend(item["id"] for item in page["items"])
                offset = page["next_offset"]
                if offset is None:
                    return ModelResponse(
                        provider_id=self.config.id,
                        model="model-a",
                        text=f"Read {len(self.seen)} tasks.",
                        finish_reason="stop",
                    )
                arguments = {"offset": offset}
            else:
                arguments = {}
            self.pages += 1
            return ModelResponse(
                provider_id=self.config.id,
                model="model-a",
                tool_calls=[
                    {
                        "id": f"page-{self.pages}",
                        "name": "work_list",
                        "arguments": arguments,
                    }
                ],
                finish_reason="tool_calls",
            )

    provider = PagingProvider()
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    chat = ChatService(store, workspace_resolver=lambda _: tmp_path)
    prepared = chat.prepare(
        ChatCompletionRequest(
            engagement_id=project.id,
            provider_id=profile.id,
            model="model-a",
            messages=[{"role": "user", "content": "List all tasks"}],
            include_knowledge=False,
        )
    )
    response = asyncio.run(chat.complete(prepared))
    assert response.message.content == "Read 45 tasks."
    assert provider.pages > 1
    assert len(provider.seen) == len(set(provider.seen)) == 45


def test_new_project_advertises_work_on_provider_turn_without_timed_prompt(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Project"))
    profile = store.create(
        ProviderProfile(
            name="Local provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"tool_calling": True},
            capability_verifications={
                "model-a": {"model": "model-a", "status": "verified"}
            },
        )
    )
    provider = FakeProvider(profile.id, local=True)
    provider.config.capabilities.tools = True
    provider.config.capabilities.strict_tools = True
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    chat = ChatService(store, workspace_resolver=lambda _: tmp_path)
    prepared = chat.prepare(
        ChatCompletionRequest(
            engagement_id=project.id,
            provider_id=profile.id,
            model="model-a",
            messages=[{"role": "user", "content": "Plan the next step"}],
            include_knowledge=False,
            stream=True,
        )
    )
    assert "Project Work is available" in prepared.model_request.instructions
    assert "minutes" not in prepared.model_request.instructions
    assert {"work_list", "work_get", "work_create", "work_check_in"} <= set(
        prepared.tool_components.specs
    )
    assert "work_agents" in prepared.tool_components.specs


def test_provider_roster_is_default_even_when_work_and_peer_messages_are_off(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Project", work_enabled=False))
    profile = store.create(
        ProviderProfile(
            name="Local provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"tool_calling": True},
            capability_verifications={
                "model-a": {"model": "model-a", "status": "verified"}
            },
        )
    )
    provider = FakeProvider(profile.id, local=True)
    provider.config.capabilities.tools = True
    provider.config.capabilities.strict_tools = True
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    chat = ChatService(store, workspace_resolver=lambda _: tmp_path)
    prepared = chat.prepare(
        ChatCompletionRequest(
            engagement_id=project.id,
            provider_id=profile.id,
            model="model-a",
            messages=[{"role": "user", "content": "Who is working on this project?"}],
            allow_agent_messaging=False,
            include_knowledge=False,
        )
    )
    assert prepared.tool_components is not None
    assert "work_agents" in prepared.tool_components.specs
    assert "work_list" not in prepared.tool_components.specs
    assert "list_agents" not in prepared.tool_components.specs
    assert prepared.turn.request_snapshot["work_roster_enabled"] is True
    assert "work_agents" in prepared.model_request.instructions


def test_agent_roster_matches_dashboard_scope_and_refreshes_links(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Active project", work_enabled=False))
    foreign = store.create(Engagement(name="Foreign project"))

    def session(engagement_id, title, **metadata):
        return store.create(
            ChatSession(
                engagement_id=engagement_id,
                title=title,
                provider_profile_id="provider-1",
                model="model-a",
                metadata=metadata,
            )
        )

    worker = session(project.id, "Parser review", allow_agent_messaging=False)
    waiting = session(project.id, "Release review")
    child = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Decoder subagent",
            provider_profile_id="provider-1",
            model="model-a",
            parent_session_id=worker.id,
            metadata={"subagent_id": "decoder-child"},
        )
    )
    hidden = session(project.id, "Side chat", side_chat=True)
    other = session(foreign.id, "Foreign work")

    def turn(chat, status):
        return store.create(
            ChatTurn(
                engagement_id=chat.engagement_id,
                session_id=chat.id,
                provider_profile_id="provider-1",
                model="model-a",
                status=status,
            )
        )

    worker_turn = turn(worker, ChatTurnStatus.ROUTING)
    waiting_turn = turn(waiting, ChatTurnStatus.WAITING_APPROVAL)
    child_turn = turn(child, ChatTurnStatus.ROUTING)
    turn(hidden, ChatTurnStatus.ROUTING)
    turn(other, ChatTurnStatus.ROUTING)
    store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=other.id,
            provider_profile_id="provider-1",
            model="model-a",
            status=ChatTurnStatus.ROUTING,
        )
    )
    service = WorkService(store)
    item = service.create(
        project.id,
        WorkCreate(
            title="Inspect decoder",
            status="in_progress",
            assignee_session_id=worker.id,
        ),
        actor_id="operator",
    )
    service.create(
        project.id,
        WorkCreate(title="Old task", status="done", assignee_session_id=waiting.id),
        actor_id="operator",
    )

    components = work_roster_components(service, project.id, tmp_path)

    async def roster():
        result = await components.broker.execute(
            ToolInvocation(
                engagement_id=project.id,
                run_id=worker_turn.id,
                chat_session_id=worker.id,
                tool_name="work_agents",
                arguments={},
                workspace=tmp_path,
            ),
            components.scope,
        )
        return result.output["agents"]

    entries = {row["session_id"]: row for row in asyncio.run(roster())}
    assert set(entries) == {worker.id, waiting.id, child.id}
    assert entries[worker.id]["state"] == "working"
    assert entries[worker.id]["work_item"] == {
        "id": item.id,
        "title": item.title,
        "status": "in_progress",
        "last_update_at": None,
    }
    assert entries[waiting.id]["state"] == "waiting"
    assert entries[waiting.id]["work_item"] is None
    assert entries[child.id]["state"] == "working"
    assert entries[child.id]["work_item"] is None
    with pytest.raises(ToolNotPermitted, match="limited to this project"):
        asyncio.run(
            components.broker.execute(
                ToolInvocation(
                    engagement_id=foreign.id,
                    run_id=worker_turn.id,
                    chat_session_id=other.id,
                    tool_name="work_agents",
                    arguments={},
                    workspace=tmp_path,
                ),
                components.scope,
            )
        )
    client = TestClient(create_app(store, auth_token="test-token"))
    dashboard = client.get(
        "/api/v1/work/agents", headers={"Authorization": "Bearer test-token"}
    )
    assert dashboard.status_code == 200
    assert {
        row["session_id"]: row["state"]
        for row in dashboard.json()
        if row["engagement_id"] == project.id
    } == {session_id: row["state"] for session_id, row in entries.items()}

    service.patch(project.id, item.id, WorkPatch(assignee_session_id=waiting.id))
    store.update(ChatTurn, worker_turn.id, {"status": ChatTurnStatus.COMPLETE})
    refreshed = {row["session_id"]: row for row in asyncio.run(roster())}
    assert set(refreshed) == {waiting.id, child.id}
    assert refreshed[waiting.id]["work_item"]["id"] == item.id
    store.update(ChatTurn, waiting_turn.id, {"status": ChatTurnStatus.COMPLETE})
    store.update(ChatTurn, child_turn.id, {"status": ChatTurnStatus.COMPLETE})
    assert asyncio.run(roster()) == []


def test_work_agents_api_lists_active_sessions_across_projects(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    for index in range(30):
        store.create(Engagement(name=f"Empty project {index}"))
    project = store.create(Engagement(name="Active project"))
    session = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Review release plan",
            provider_profile_id="provider-1",
            model="model-a",
        )
    )
    turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id="provider-1",
            model="model-a",
            status=ChatTurnStatus.ROUTING,
        )
    )
    client = TestClient(create_app(store, auth_token="test-token"))
    assert client.get("/api/v1/work/agents").status_code == 401
    response = client.get(
        "/api/v1/work/agents", headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "session_id": session.id,
            "engagement_id": project.id,
            "title": session.title,
            "state": "working",
            "turn_id": turn.id,
        }
    ]


def test_work_change_feed_signals_saved_updates_across_services(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Project"))
    session = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Plan release",
            provider_profile_id="provider-1",
            model="model-a",
        )
    )
    writer = WorkService(store)
    reader = WorkService(store)

    async def scenario():
        queue = reader.changes.subscribe()
        try:
            item = await asyncio.to_thread(
                writer.create,
                project.id,
                WorkCreate(title="Review the plan"),
                actor_id="operator",
            )
            assert await asyncio.wait_for(queue.get(), 1) == "work"
            await asyncio.to_thread(
                writer.check_in,
                project.id,
                item.id,
                WorkCheckIn(summary="Review ready"),
                actor_kind="agent",
                actor_id="session-1",
            )
            assert await asyncio.wait_for(queue.get(), 1) == "work"
            await asyncio.to_thread(writer.set_enabled, project.id, False)
            assert await asyncio.wait_for(queue.get(), 1) == "projects"
            turn = await asyncio.to_thread(
                store.create,
                ChatTurn(
                    engagement_id=project.id,
                    session_id=session.id,
                    provider_profile_id="provider-1",
                    model="model-a",
                ),
            )
            assert await asyncio.wait_for(queue.get(), 1) == "work"
            await asyncio.to_thread(
                store.update,
                ChatTurn,
                turn.id,
                {"status": ChatTurnStatus.COMPLETE},
                expected_revision=turn.revision,
            )
            assert await asyncio.wait_for(queue.get(), 1) == "work"
        finally:
            reader.changes.unsubscribe(queue)

    asyncio.run(scenario())


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
    assert store.get(Engagement, project_id).work_enabled is True
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
