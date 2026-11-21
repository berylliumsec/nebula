"""Project-defined Work stages: settings, item stages, resyncing imports, tools."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from pydantic import ValidationError
import pytest

from tests.v3.async_test_support import AsyncTestClient as TestClient
from nebula.v3.api import create_app
from nebula.v3.domain import Engagement, ScopePolicy, WorkItem, WorkStage
from nebula.v3.storage import ConflictError, NebulaStore
from nebula.v3.tools import ToolInvocation
from nebula.v3.work import (
    WorkCheckIn,
    WorkCreate,
    WorkImportBatch,
    WorkPatch,
    WorkService,
    WorkStagesPatch,
    work_components,
)

STAGES = [
    {"key": "draft", "label": "Draft", "flag_when_empty": True},
    {"key": "review", "label": "Review"},
    {"key": "ship", "label": "Ship"},
]


async def test_stage_settings_are_revision_checked_and_validated(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Website refresh"))
    service = WorkService(store)
    saved = await service.set_stages(
        project.id,
        [WorkStage.model_validate(stage) for stage in STAGES],
        expected_revision=project.revision,
    )
    assert [stage.key for stage in saved.work_stages] == ["draft", "review", "ship"]
    assert saved.work_stages[0].flag_when_empty is True
    with pytest.raises(ConflictError):
        await service.set_stages(project.id, [], expected_revision=project.revision)
    with pytest.raises(ValidationError):
        WorkStagesPatch.model_validate(
            {"stages": [{"key": "a", "label": "A"}, {"key": "a", "label": "B"}]}
        )
    with pytest.raises(ValidationError):
        WorkStage.model_validate({"key": "Has Spaces", "label": "Bad"})
    with pytest.raises(ValidationError):
        WorkStage.model_validate({"key": "ok", "label": "   "})
    reloaded = await NebulaStore(tmp_path / "core.db").get(Engagement, project.id)
    assert [stage.label for stage in reloaded.work_stages] == [
        "Draft",
        "Review",
        "Ship",
    ]


async def test_items_take_a_stage_on_create_patch_and_check_in(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Website refresh"))
    service = WorkService(store)
    item = await service.create(
        project.id, WorkCreate(title="Home page copy", stage="draft"), actor_id="op"
    )
    assert item.stage == "draft"
    moved = await service.check_in(
        project.id,
        item.id,
        WorkCheckIn(summary="Copy approved", status="ready", stage="review"),
        actor_kind="operator",
        actor_id="op",
    )
    assert moved.stage == "review"
    current = await service.get(project.id, item.id)
    assert (current.stage, current.status) == ("review", "ready")
    kept = await service.check_in(
        project.id,
        item.id,
        WorkCheckIn(summary="Small fix"),
        actor_kind="operator",
        actor_id="op",
    )
    assert kept.stage is None
    assert (await service.get(project.id, item.id)).stage == "review"
    cleared = await service.patch(
        project.id, item.id, WorkPatch.model_validate({"stage": None})
    )
    assert cleared.stage is None
    unchanged = await service.patch(project.id, item.id, WorkPatch(title="Home copy"))
    assert unchanged.stage is None and unchanged.title == "Home copy"


async def test_reimport_moves_stage_and_posts_new_updates_by_update_id(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    service = WorkService(store)

    def batch(stage, update_id, status):
        payload = {
            "source": "sample-tracker",
            "projects": [
                {
                    "external_id": "site",
                    "name": "Website refresh",
                    "items": [
                        {
                            "external_id": "task-1",
                            "title": "Home page copy",
                            "status": "in_progress",
                            "update": {
                                "summary": f"Moved to {stage}",
                                "status": status,
                                **({"update_id": update_id} if update_id else {}),
                            },
                            **({"stage": stage} if stage is not None else {}),
                        }
                    ],
                }
            ],
        }
        return WorkImportBatch.model_validate(payload)

    first = await service.import_batch(batch("draft", "u1", "in_progress"))
    project_id = first.projects[0].engagement_id
    item_id = first.projects[0].items[0].item_id
    assert (await service.get(project_id, item_id)).stage == "draft"

    second = await service.import_batch(batch("review", "u2", "review"))
    assert second.projects[0].items[0].item_id == item_id
    assert second.projects[0].items[0].update_id != first.projects[0].items[0].update_id
    item = await service.get(project_id, item_id)
    assert (item.stage, item.status) == ("review", "review")

    # Same update_id replays; omitting stage leaves the item's stage alone.
    replay = await service.import_batch(batch(None, "u2", "done"))
    assert (
        replay.projects[0].items[0].update_id == second.projects[0].items[0].update_id
    )
    item = await service.get(project_id, item_id)
    assert (item.stage, item.status) == ("review", "review")
    assert len(await service.updates(project_id, item_id)) == 2


async def test_stage_api_and_agent_tools(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Website refresh"))
    client = TestClient(create_app(store, auth_token="test-token"))
    auth = {"Authorization": "Bearer test-token"}
    root = f"/api/v1/engagements/{project.id}/work"
    saved = await client.patch(
        f"{root}/stages",
        headers=auth,
        json={"stages": STAGES, "expected_revision": project.revision},
    )
    assert saved.status_code == 200, saved.text
    assert [stage["key"] for stage in saved.json()["work_stages"]] == [
        "draft",
        "review",
        "ship",
    ]
    stale = await client.patch(
        f"{root}/stages",
        headers=auth,
        json={"stages": [], "expected_revision": project.revision},
    )
    assert stale.status_code == 409, stale.text
    created = await client.post(
        root, headers=auth, json={"title": "Pricing page", "stage": "draft"}
    )
    assert created.status_code == 200, created.text
    assert created.json()["stage"] == "draft"
    bad = await client.post(root, headers=auth, json={"title": "X", "stage": "Not OK"})
    assert bad.status_code == 422

    service = WorkService(store)
    components = work_components(
        service,
        project.id,
        tmp_path,
        ScopePolicy(id=f"scope:{project.id}", engagement_id=project.id),
    )
    scope = components.scope
    assert "stage" in components.specs["work_create"].input_schema["properties"]
    assert "stage" in components.specs["work_check_in"].input_schema["properties"]
    listed = await components.broker.execute(
        ToolInvocation(
            engagement_id=project.id,
            tool_name="work_list",
            arguments={},
            run_id="run-1",
            workspace=tmp_path,
        ),
        scope,
    )
    assert [stage["key"] for stage in listed.output["stages"]] == [
        "draft",
        "review",
        "ship",
    ]
    assert listed.output["items"][0]["stage"] == "draft"
    item_id = created.json()["id"]
    await components.broker.execute(
        ToolInvocation(
            engagement_id=project.id,
            tool_name="work_check_in",
            arguments={
                "item_id": item_id,
                "summary": "Ready",
                "stage": "ship",
                "request_id": "move-1",
            },
            run_id="run-1",
            workspace=tmp_path,
        ),
        scope,
    )
    assert (await store.get(WorkItem, item_id)).stage == "ship"


async def test_subprojects_use_their_nearest_parents_stages(tmp_path):
    from nebula.v3.control_assistant_work import ControlAssistantWork

    store = NebulaStore(tmp_path / "core.db")
    service = WorkService(store)
    parent = await store.create(Engagement(name="Portfolio"))
    child = await store.create(Engagement(name="Team", parent_engagement_id=parent.id))
    leaf = await store.create(Engagement(name="Task", parent_engagement_id=child.id))
    assert await service.effective_stages(leaf) == ([], None)
    parent = await service.set_stages(
        parent.id,
        [WorkStage.model_validate(stage) for stage in STAGES],
        expected_revision=parent.revision,
    )
    stages, source = await service.effective_stages(leaf)
    assert [stage.key for stage in stages] == ["draft", "review", "ship"]
    assert source is not None and source.id == parent.id

    # Agents in a subproject see the inherited list; Butler names its source.
    components = work_components(
        service,
        leaf.id,
        tmp_path,
        ScopePolicy(id=f"scope:{leaf.id}", engagement_id=leaf.id),
    )
    listed = await components.broker.execute(
        ToolInvocation(
            engagement_id=leaf.id,
            tool_name="work_list",
            arguments={},
            run_id="run-1",
            workspace=tmp_path,
        ),
        components.scope,
    )
    assert [stage["key"] for stage in listed.output["stages"]] == [
        "draft",
        "review",
        "ship",
    ]
    butler = object.__new__(ControlAssistantWork)
    butler.store, butler.work_service = store, service
    read = await butler.work_stages(leaf.id)
    assert read["stages"] == []
    assert [stage["key"] for stage in read["effective_stages"]] == [
        "draft",
        "review",
        "ship",
    ]
    assert read["inherited_from"] == {"project_id": parent.id, "name": "Portfolio"}

    # A subproject's own stages win, and its children use those instead.
    child = await service.set_stages(
        child.id,
        [WorkStage.model_validate({"key": "build", "label": "Build"})],
        expected_revision=child.revision,
    )
    stages, source = await service.effective_stages(leaf)
    assert [stage.key for stage in stages] == ["build"]
    assert source is not None and source.id == child.id
    own = await butler.work_stages(child.id)
    assert own["inherited_from"] is None
    assert [stage["key"] for stage in own["effective_stages"]] == ["build"]
