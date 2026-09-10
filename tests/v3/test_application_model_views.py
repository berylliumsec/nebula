"""Bounded browser reads across large graphs and project boundaries."""

from sqlalchemy import insert
from nebula.v3.application_model.persistence import graphs
from nebula.v3.application_model.service import ApplicationModelService, empty
from nebula.v3.domain import Engagement
from nebula.v3.storage import NebulaStore


def test_large_graph_view_bounds_pages_neighborhood_and_deep_links(tmp_path):
    store = NebulaStore(tmp_path / "model.db")
    project = store.create(Engagement(name="Large model"))
    other = store.create(Engagement(name="Other"))
    graph = empty(project.id)
    graph["revision"] = 1
    for i in range(2000):
        graph["objects"][f"page-{i}"] = {
            "id": f"page-{i}",
            "label": f"Page {i}",
            "classification": {"value": "Page"},
            "properties": {},
        }
        if i:
            graph["relationships"][f"edge-{i}"] = {
                "id": f"edge-{i}",
                "type": "links_to",
                "source": "page-0",
                "target": f"page-{i}",
                "claim": {"value": True},
            }
    with store.database.engine.begin() as connection:
        connection.execute(
            insert(graphs).values(project_id=project.id, revision=1, payload=graph)
        )
    service = ApplicationModelService(store)
    overview = service.view(project.id)
    assert overview["object_total"] == 2000
    assert overview["category_counts"]["Structure"] == 2000
    assert overview["outline_objects"] == []
    assert len(overview["relationships"]) == 25
    first = service.view(project.id, category="Structure")
    second = service.view(project.id, category="Structure", offset=20)
    assert len(first["outline_objects"]) == len(second["outline_objects"]) == 20
    assert not (
        {o["id"] for o in first["outline_objects"]}
        & {o["id"] for o in second["outline_objects"]}
    )
    focused = service.view(
        project.id, object_id="page-1999", depth=3, relationship_offset=25
    )
    assert focused["outline_offset"] == 1980
    assert any(o["id"] == "page-1999" for o in focused["outline_objects"])
    assert len(focused["map_objects"]) <= 100
    assert len(focused["map_relationships"]) <= 100
    assert len(focused["objects"]) <= 170
    assert focused["map_truncated"]
    selected = service.view(project.id, relationship_id="edge-1999")
    assert any(r["id"] == "edge-1999" for r in selected["relationships"])
    assert {"page-0", "page-1999"} <= {o["id"] for o in selected["objects"]}
    assert service.view(other.id, object_id="page-1999")["objects"] == []
    match = service.view(project.id, query="Page 1999")
    assert match["outline_total"] == 1
    assert [o["id"] for o in match["outline_objects"]] == ["page-1999"]


def test_view_api_validates_bounds_and_requires_auth(tmp_path):
    from fastapi.testclient import TestClient
    from nebula.v3.api import create_app
    from nebula.v3.artifacts import ArtifactStore

    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Bounded API"))
    app = create_app(
        store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        auth_token="test-token",
    )
    with TestClient(app) as client:
        path = f"/api/v1/engagements/{project.id}/application-model/view"
        assert client.get(path).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        for suffix in [
            "?offset=-1",
            "?relationship_offset=-1",
            "?depth=4",
            "?category=missing",
        ]:
            assert client.get(path + suffix, headers=headers).status_code == 422
        assert client.get(path, headers=headers).json()["object_total"] == 0
