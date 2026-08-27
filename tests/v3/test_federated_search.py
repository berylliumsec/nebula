from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from nebula.v3.api import create_app
from nebula.v3.database import EntityRow, SearchDocumentRow
from nebula.v3.domain import (
    Asset,
    BrowserSession,
    BrowserTabState,
    CommandExecution,
    Engagement,
    Observation,
)
from nebula.v3.storage import NebulaStore


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_search_is_active_project_first_and_paginates(tmp_path):
    store = NebulaStore(tmp_path / "search.db")
    first = Engagement(name="Alpha")
    second = Engagement(name="Beta")
    store.create_many([first, second])
    store.create(
        Asset(engagement_id=first.id, name="Shared gateway", hostname="alpha.test")
    )
    store.create(
        Asset(engagement_id=second.id, name="Shared gateway", hostname="beta.test")
    )
    client = TestClient(create_app(store, auth_token="test-token"))

    active = client.get(
        "/api/v1/search",
        params={"query": "gateway", "active_project": first.id, "limit": 1},
        headers=_auth(),
    )
    assert active.status_code == 200
    assert len(active.json()["items"]) == 1
    assert active.json()["items"][0]["ref"]["project_id"] == first.id
    all_projects = client.get(
        "/api/v1/search",
        params={
            "query": "gateway",
            "active_project": first.id,
            "scope": "all",
            "limit": 1,
        },
        headers=_auth(),
    ).json()
    assert all_projects["items"][0]["project"] == "Alpha"
    assert all_projects["next_cursor"]
    second_page = client.get(
        "/api/v1/search",
        params={
            "query": "gateway",
            "active_project": first.id,
            "scope": "all",
            "limit": 1,
            "cursor": all_projects["next_cursor"],
        },
        headers=_auth(),
    ).json()
    assert second_page["items"][0]["project"] == "Beta"
    assert any(action["id"] == "open" for action in all_projects["items"][0]["actions"])


def test_search_projection_excludes_secrets_outputs_and_url_queries(tmp_path):
    store = NebulaStore(tmp_path / "privacy.db")
    project = Engagement(name="Privacy")
    store.create(project)
    command = CommandExecution(
        engagement_id=project.id,
        session_id="session",
        process_id="process",
        command="curl example.test",
        command_sha256="a" * 64,
        runtime_digest="sha256:" + "b" * 64,
        policy_revision=1,
        error="SECRET_OUTPUT_MUST_NOT_BE_INDEXED",
    )
    browser = BrowserSession(
        engagement_id=project.id,
        name="Browser",
        identity_id="identity",
        tabs=[
            BrowserTabState(
                id="tab",
                title="Portal",
                url="https://example.test/path?token=SECRET_QUERY#private",
            )
        ],
    )
    note = Observation(
        engagement_id=project.id,
        observation_type="note",
        title="Explicit note",
        body="operator saved durable note",
    )
    store.create_many([command, browser, note])
    with store.database.session() as session:
        documents = {row.id: row for row in session.scalars(select(SearchDocumentRow))}
    command_text = f"{documents[command.id].label} {documents[command.id].description} {documents[command.id].content}"
    browser_text = f"{documents[browser.id].label} {documents[browser.id].description} {documents[browser.id].content}"
    assert "SECRET_OUTPUT" not in command_text
    assert "SECRET_QUERY" not in browser_text
    assert "token=" not in browser_text
    browser_tab = next(
        row for row in documents.values() if row.resource_kind == "browser_tab"
    )
    assert browser_tab.resource_id == f"{browser.id}/tab"
    assert browser_tab.description == "https://example.test/path"
    assert "operator saved durable note" in documents[note.id].description


def test_search_repairs_a_stale_projection(tmp_path):
    store = NebulaStore(tmp_path / "stale.db")
    project = Engagement(name="Before")
    store.create(project)
    with store.database.session() as session:
        session.execute(
            update(EntityRow)
            .where(EntityRow.id == project.id)
            .values(
                revision=2,
                payload={
                    **project.model_dump(mode="json"),
                    "name": "After",
                    "revision": 2,
                },
            )
        )
    client = TestClient(create_app(store, auth_token="test-token"))
    response = client.get(
        "/api/v1/search",
        params={"query": "After", "active_project": project.id},
        headers=_auth(),
    )
    assert response.status_code == 200
    assert response.json()["partial_index"] is True
    assert response.json()["items"][0]["label"] == "After"
