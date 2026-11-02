from __future__ import annotations

from nebula.v3.action_registry import ActionRegistry
from nebula.v3.domain import (
    ActionAuthority,
    ActionResolutionRequest,
    Asset,
    Engagement,
    Evidence,
    ResourceKind,
    ResourceRef,
)
from nebula.v3.storage import NebulaStore


def test_registry_resolves_shared_verbs_and_device_capabilities(tmp_path):
    store = NebulaStore(tmp_path / "actions.db")
    project = store.create(Engagement(name="Actions"))
    evidence = store.create(
        Evidence(
            engagement_id=project.id,
            evidence_type="test",
            title="Exchange",
        )
    )
    request = ActionResolutionRequest(
        resources=[
            ResourceRef(
                project_id=project.id,
                kind=ResourceKind.EVIDENCE,
                id=evidence.id,
                revision=evidence.revision,
            )
        ]
    )

    actions = {item.id: item for item in ActionRegistry(store).resolve(request)}
    assert actions["open"].available is True
    assert actions["ask_nebula"].authority == ActionAuthority.UI
    assert actions["draft_finding"].available is True
    assert actions["copy"].available is False
    assert actions["copy"].disabled_reason == (
        "No connected device currently provides clipboard.write."
    )

    capable = request.model_copy(
        update={
            "device_id": "mac-1",
            "device_capabilities": ["clipboard.write"],
        }
    )
    actions = {item.id: item for item in ActionRegistry(store).resolve(capable)}
    assert actions["copy"].available is True
    assert actions["copy"].disabled_reason is None


def test_registry_fails_closed_for_stale_deleted_and_cross_project_refs(tmp_path):
    store = NebulaStore(tmp_path / "action-guards.db")
    first = store.create(Engagement(name="First"))
    second = store.create(Engagement(name="Second"))
    asset = store.create(Asset(engagement_id=first.id, name="Gateway"))
    registry = ActionRegistry(store)

    stale = registry.resolve(
        ActionResolutionRequest(
            resources=[
                ResourceRef(
                    project_id=first.id,
                    kind=ResourceKind.ASSET,
                    id=asset.id,
                    revision=asset.revision + 1,
                )
            ]
        )
    )
    assert stale and all(not item.available for item in stale)
    assert all("changed" in (item.disabled_reason or "") for item in stale)

    wrong_project = registry.resolve(
        ActionResolutionRequest(
            resources=[
                ResourceRef(project_id=second.id, kind=ResourceKind.ASSET, id=asset.id)
            ]
        )
    )
    assert all(not item.available for item in wrong_project)
    assert all(
        "different project" in (item.disabled_reason or "") for item in wrong_project
    )

    deleted = registry.resolve(
        ActionResolutionRequest(
            resources=[
                ResourceRef(project_id=first.id, kind=ResourceKind.ASSET, id="missing")
            ]
        )
    )
    assert all(not item.available for item in deleted)
    assert all("no longer exists" in (item.disabled_reason or "") for item in deleted)


def test_batched_resource_actions_keep_independent_guards_and_fresh_revisions(tmp_path):
    store = NebulaStore(tmp_path / "batched-actions.db")
    project = store.create(Engagement(name="Actions"))
    other = store.create(Engagement(name="Other"))
    asset = store.create(Asset(engagement_id=project.id, name="Gateway"))
    ref = ResourceRef(
        project_id=project.id, kind=ResourceKind.ASSET, id=asset.id, revision=1
    )
    requests = [
        ActionResolutionRequest(
            resources=[ref], device_capabilities=["clipboard.write"]
        ),
        ActionResolutionRequest(resources=[ref.model_copy(update={"revision": 2})]),
        ActionResolutionRequest(
            resources=[ref.model_copy(update={"project_id": other.id})]
        ),
        ActionResolutionRequest(resources=[ref.model_copy(update={"id": "missing"})]),
        ActionResolutionRequest(
            resources=[ref.model_copy(update={"kind": ResourceKind.NOTE})]
        ),
        ActionResolutionRequest(
            resources=[ref, ref.model_copy(update={"project_id": other.id})]
        ),
    ]
    registry = ActionRegistry(store)
    results = registry.resolve_many(requests)
    assert all(action.available for action in results[0])
    for actions, reason in zip(
        results[1:],
        (
            "changed",
            "different project",
            "no longer exists",
            "no longer exists",
            "different projects",
        ),
        strict=True,
    ):
        assert actions and all(not action.available for action in actions)
        assert all(reason in action.disabled_reason for action in actions)
    saved = store.update(Asset, asset.id, {"name": "Updated"}, expected_revision=1)
    refreshed = registry.resolve_many(
        [
            requests[0],
            requests[0].model_copy(
                update={
                    "resources": [ref.model_copy(update={"revision": saved.revision})]
                }
            ),
        ]
    )
    assert all(not action.available for action in refreshed[0])
    assert all(action.available for action in refreshed[1])
    assert registry.resolve_many([]) == []
