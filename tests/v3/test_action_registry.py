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
