import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import (
    AgentRun,
    Asset,
    ChatSession,
    Engagement,
    Observation,
    ProviderProfile,
    Report,
    ReportStatus,
    RunStatus,
    Service,
    Task,
    utc_now,
)
from nebula.v3.storage import NebulaStore, NotFoundError


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_engagement_delete_rejects_orphaning_owned_entities(tmp_path):
    store = NebulaStore(tmp_path / "engagement-delete.db")
    engagement = store.create(Engagement(name="Keep history"))
    asset = store.create(Asset(engagement_id=engagement.id, name="Owned asset"))
    client = TestClient(create_app(store, auth_token="test-token"))

    response = client.delete(f"/api/v1/engagements/{engagement.id}", headers=_auth())

    assert response.status_code == 409
    assert "archive it instead" in response.json()["detail"]
    assert store.get(Engagement, engagement.id).id == engagement.id
    assert store.get(Asset, asset.id).engagement_id == engagement.id


def test_empty_engagement_can_still_be_deleted(tmp_path):
    store = NebulaStore(tmp_path / "empty-engagement-delete.db")
    engagement = store.create(Engagement(name="Empty"))
    client = TestClient(create_app(store, auth_token="test-token"))

    response = client.delete(f"/api/v1/engagements/{engagement.id}", headers=_auth())

    assert response.status_code == 204


def test_mission_delete_requires_terminal_state_and_removes_execution_history(tmp_path):
    store = NebulaStore(tmp_path / "mission-delete.db")
    engagement = store.create(Engagement(name="Mission cleanup"))
    run = store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Review the exposed service",
            status=RunStatus.RUNNING,
        )
    )
    task = store.create(
        Task(
            engagement_id=engagement.id,
            run_id=run.id,
            specialist_role="analyst",
            title="Inspect service",
        )
    )
    store.append_event(run.id, "run.started", {"status": "running"})
    client = TestClient(create_app(store, auth_token="test-token"))

    active = client.delete(f"/api/v1/runs/{run.id}", headers=_auth())
    assert active.status_code == 409
    assert "before deletion" in active.json()["detail"]

    cancelled = store.update(
        AgentRun,
        run.id,
        {"status": RunStatus.CANCELLED},
        expected_revision=run.revision,
    )
    deleted = client.delete(f"/api/v1/runs/{run.id}", headers=_auth())
    assert deleted.status_code == 204

    with pytest.raises(NotFoundError):
        store.get(AgentRun, cancelled.id)
    with pytest.raises(NotFoundError):
        store.get(Task, task.id)
    assert len(store.replay_events(run.id)) == 1
    assert (
        client.get(f"/api/v1/runs/{run.id}/events", headers=_auth()).status_code
        == 404
    )


def test_provider_delete_rejects_stranding_chat_or_run_history(tmp_path):
    store = NebulaStore(tmp_path / "provider-delete.db")
    engagement = store.create(Engagement(name="Provider history"))
    chat_provider = store.create(
        ProviderProfile(name="Chat", provider_type="vllm", is_local=True)
    )
    run_provider = store.create(
        ProviderProfile(name="Run", provider_type="vllm", is_local=True)
    )
    unused_provider = store.create(
        ProviderProfile(name="Unused", provider_type="vllm", is_local=True)
    )
    store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Durable chat",
            provider_profile_id=chat_provider.id,
            model="model-a",
        )
    )
    store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Durable run",
            supervisor_provider_id=run_provider.id,
            supervisor_model="model-a",
        )
    )
    client = TestClient(create_app(store, auth_token="test-token"))

    for provider in (chat_provider, run_provider):
        response = client.delete(f"/api/v1/providers/{provider.id}", headers=_auth())
        assert response.status_code == 409
        assert "durable chat or run history" in response.json()["detail"]
        assert store.get(ProviderProfile, provider.id).id == provider.id

    assert (
        client.delete(
            f"/api/v1/providers/{unused_provider.id}", headers=_auth()
        ).status_code
        == 204
    )


def test_generic_delete_rejects_referenced_graph_nodes(tmp_path):
    store = NebulaStore(tmp_path / "graph-delete.db")
    engagement = store.create(Engagement(name="Graph integrity"))
    asset = store.create(Asset(engagement_id=engagement.id, name="Referenced asset"))
    service = store.create(
        Service(
            engagement_id=engagement.id,
            asset_id=asset.id,
            port=443,
        )
    )
    unreferenced = store.create(
        Asset(engagement_id=engagement.id, name="Unreferenced asset")
    )
    client = TestClient(create_app(store, auth_token="test-token"))

    blocked = client.delete(f"/api/v1/assets/{asset.id}", headers=_auth())

    assert blocked.status_code == 409
    assert "services.asset_id" in blocked.json()["detail"]
    assert store.get(Service, service.id).asset_id == asset.id
    assert (
        client.delete(f"/api/v1/assets/{unreferenced.id}", headers=_auth()).status_code
        == 204
    )


@pytest.mark.parametrize(
    "report_status", [ReportStatus.DRAFT, ReportStatus.REVIEW, ReportStatus.FINAL]
)
def test_note_dependencies_name_the_referencing_report_and_protect_lineage(
    tmp_path, report_status
):
    store = NebulaStore(tmp_path / f"note-{report_status}.db")
    engagement = store.create(Engagement(name="Report lineage"))
    note = store.create(
        Observation(
            engagement_id=engagement.id,
            observation_type="note",
            title="Retained source note",
        )
    )
    report = store.create(
        Report(
            engagement_id=engagement.id,
            title=(
                "Immutable client report"
                if report_status == ReportStatus.FINAL
                else "Working client report"
            ),
            status=report_status,
            observation_ids=[note.id],
            signed_off_by="operator-1" if report_status == ReportStatus.FINAL else None,
            signed_off_at=utc_now() if report_status == ReportStatus.FINAL else None,
        )
    )
    client = TestClient(create_app(store, auth_token="test-token"))

    dependencies = client.get(
        f"/api/v1/observations/{note.id}/dependencies", headers=_auth()
    )
    assert dependencies.status_code == 200
    assert dependencies.json() == {
        "observation_id": note.id,
        "deletable": False,
        "reports": [
            {"id": report.id, "title": report.title, "status": report_status.value}
        ],
    }

    blocked = client.delete(
        f"/api/v1/observations/{note.id}",
        headers={**_auth(), "If-Match": str(note.revision)},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "note_referenced_by_report"
    assert blocked.json()["reason_code"] == "note_referenced_by_report"
    assert report.title in blocked.json()["detail"]
    if report_status == ReportStatus.FINAL:
        assert "immutable" in blocked.json()["detail"].lower()
    else:
        assert "remove this note" in blocked.json()["detail"].lower()
