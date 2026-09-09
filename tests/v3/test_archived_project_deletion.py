"""Archived deletion: atomic ownership cleanup without any filesystem removal."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, func
from nebula.v3.api import create_app
from nebula.v3.domain import Engagement, Asset, AgentRun, ChatQueue, RunStatus
from nebula.v3.storage import NebulaStore, NotFoundError
from nebula.v3.database import EntityRow, SearchDocumentRow
from nebula.v3.application_model.persistence import graphs, edits


def setup(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    folder = tmp_path / "host"
    folder.mkdir()
    (folder / "keep.txt").write_text("host data")
    project = store.create(
        Engagement(name="Delete archive", status="archived", workspace_path=str(folder))
    )
    client = TestClient(create_app(store, auth_token="test-token"))
    return store, project, client, folder


def remove(client, project, revision=None):
    return client.delete(
        f"/api/v1/engagements/{project.id}",
        headers={
            "Authorization": "Bearer test-token",
            "If-Match": str(revision or project.revision),
        },
    )


def test_archive_deletion_cleans_owned_records_and_preserves_folder_and_other_project(
    tmp_path,
):
    store, project, client, folder = setup(tmp_path)
    asset = store.create(Asset(engagement_id=project.id, name="Owned"))
    other = store.create(Engagement(name="Unrelated", workspace_path=str(folder)))
    other_asset = store.create(Asset(engagement_id=other.id, name="Keep"))
    run = store.create(
        AgentRun(
            engagement_id=project.id,
            objective="Fixture only",
            status=RunStatus.COMPLETE,
        )
    )
    store.append_event(run.id, "run.completed", {})
    with store.database.session() as session:
        for owner in (project.id, other.id):
            session.execute(
                insert(graphs).values(project_id=owner, revision=1, payload={})
            )
            session.execute(
                insert(edits).values(
                    project_id=owner,
                    revision=1,
                    idempotency_key="fixture",
                    digest="fixture",
                    payload={},
                )
            )
    assert remove(client, project).status_code == 204
    for model, id in ((Engagement, project.id), (Asset, asset.id), (AgentRun, run.id)):
        with pytest.raises(NotFoundError):
            store.get(model, id)
    assert store.get(Asset, other_asset.id).name == "Keep"
    assert (folder / "keep.txt").read_text() == "host data"
    assert store.replay_events(run.id)
    with store.database.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(EntityRow)
                .where(EntityRow.engagement_id == project.id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(SearchDocumentRow)
                .where(SearchDocumentRow.project_id == project.id)
            )
            == 0
        )
        for table in (graphs, edits):
            assert list(session.scalars(select(table.c.project_id))) == [other.id]
    assert remove(client, project).status_code == 404


@pytest.mark.parametrize(
    "mode", ["revision", "restored", "missing_revision", "running", "queue"]
)
def test_archive_deletion_rejects_stale_or_busy_requests_without_partial_cleanup(
    tmp_path, mode
):
    store, project, client, folder = setup(tmp_path)
    asset = store.create(Asset(engagement_id=project.id, name="Keep until safe"))
    if mode == "restored":
        store.update(
            Engagement,
            project.id,
            {"status": "active"},
            expected_revision=project.revision,
        )
    if mode == "running":
        store.create(
            AgentRun(
                engagement_id=project.id,
                objective="Fixture only",
                status=RunStatus.RUNNING,
            )
        )
    if mode == "queue":
        store.create(
            ChatQueue(
                engagement_id=project.id,
                session_id="fixture",
                items=[{"status": "queued"}],
            )
        )
    response = (
        client.delete(
            f"/api/v1/engagements/{project.id}",
            headers={"Authorization": "Bearer test-token"},
        )
        if mode == "missing_revision"
        else remove(client, project, 99 if mode == "revision" else None)
    )
    assert response.status_code == 409
    assert store.get(Asset, asset.id).name == "Keep until safe"
    assert (folder / "keep.txt").read_text() == "host data"
