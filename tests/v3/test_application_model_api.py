"""Real Core graph routes and the shared browser/harness tool contract."""

from fastapi.testclient import TestClient
from nebula.v3.api import create_app
from nebula.v3.storage import NebulaStore
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.application_model.tools import INPUTS


def test_real_core_graph_capture_and_project_scope(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(
        store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        auth_token="test-token",
    )
    auth = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        project = client.post(
            "/api/v1/engagements", headers=auth, json={"name": "Site A"}
        ).json()
        base = f"/api/v1/engagements/{project['id']}/application-model"
        assert client.get(base + "/graph").status_code == 401
        assert client.get(base + "/graph", headers=auth).json()["objects"] == []
        assert len(client.get(base + "/schema", headers=auth).json()["types"]) == 33
        browser = client.get(
            f"/api/v1/engagements/{project['id']}/browser-workspace", headers=auth
        ).json()["sessions"][0]
        sync = client.put(
            f"/api/v1/browser-sessions/{browser['id']}/tabs",
            headers=auth,
            json={
                "expected_revision": browser["revision"],
                "tabs": [
                    {
                        "id": "fixture",
                        "url": "https://example.test/",
                        "title": "Fixture",
                        "position": 0,
                    }
                ],
                "active_tab_id": "fixture",
                "device_owner": "fixture",
            },
        )
        assert sync.status_code == 200, sync.text
        capture = client.post(
            f"/api/v1/browser-sessions/{browser['id']}/traffic",
            headers=auth,
            json={
                "tab_id": "fixture",
                "method": "GET",
                "url": "https://example.test/",
                "blocked": True,
                "status_code": 403,
            },
        )
        assert capture.status_code == 201, capture.text
        evidence = client.get(base + "/evidence", headers=auth).json()["evidence"][0]
        assert evidence["facts"]["status_code"]["value"] == 403
        assert client.get(base + "/graph", headers=auth).json()["objects"] == []
        transaction = {
            "expected_revision": 0,
            "idempotency_key": "save",
            "operations": [
                {
                    "op": "put_object",
                    "id": "firewall",
                    "label": "Possible firewall",
                    "authentication_context": "anonymous",
                    "classification": {
                        "value": "AccessControl",
                        "status": "hypothesized",
                        "evidence": [
                            {
                                "kind": evidence["kind"],
                                "id": evidence["id"],
                                "revision": evidence["revision"],
                                "role": "supporting",
                            }
                        ],
                    },
                }
            ],
        }
        response = client.post(base + "/transactions", headers=auth, json=transaction)
        assert response.status_code == 200, response.text
        assert (
            client.post(base + "/transactions", headers=auth, json=transaction).json()
            == response.json()
        )
        assert (
            client.get(base + "/graph", headers=auth).json()["objects"][0][
                "classification"
            ]["status"]
            == "hypothesized"
        )
        other = client.post(
            "/api/v1/engagements", headers=auth, json={"name": "Other"}
        ).json()
        otherbase = f"/api/v1/engagements/{other['id']}/application-model"
        assert (
            client.get(
                otherbase + f"/evidence/{evidence['kind']}/{evidence['id']}",
                headers=auth,
            ).status_code
            == 404
        )
        assert client.post(base + "/sessions", headers=auth, json={}).status_code == 404
        assert not any(
            "queries" in path or "states" in path
            for path in app.openapi()["paths"]
            if "application-model" in path
        )


def test_browser_and_project_tools_expose_same_graph(tmp_path):
    import asyncio
    from nebula.v3.domain import (
        Engagement,
        BrowserSession,
        ChatSession,
        ChatTurn,
        ToolCallOrigin,
        ScopePolicy,
    )
    from nebula.v3.tools import ToolInvocation, InvalidToolArguments
    from nebula.v3.application_model.tools import components, project_components
    import pytest

    store = NebulaStore(tmp_path / "broker.db")
    project = store.create(Engagement(name="Shared graph"))
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Model",
            model="fixture",
            provider_profile_id="fixture",
        )
    )
    turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            model="fixture",
            provider_profile_id="fixture",
            tools_enabled=True,
        )
    )
    scope = ScopePolicy(engagement_id=project.id)
    browser = components(
        store,
        BrowserSession(engagement_id=project.id, name="Browser", identity_id="fixture"),
        scope,
        tmp_path,
    )
    harness = project_components(store, project.id, scope, tmp_path)
    assert set(browser.specs) == set(harness.specs) == set(INPUTS)

    async def invoke(runtime, name, arguments):
        return await runtime.broker.execute(
            ToolInvocation(
                engagement_id=project.id,
                run_id=turn.id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=chat.id,
                chat_turn_id=turn.id,
                tool_name=name,
                arguments=arguments,
                workspace=tmp_path,
            ),
            scope,
        )

    async def journey():
        discovered = await invoke(browser, "model.discover_schema", {})
        assert len(discovered.output["types"]) == 33
        tx = {
            "expected_revision": 0,
            "idempotency_key": "browser-edit",
            "operations": [
                {
                    "op": "put_object",
                    "id": "site",
                    "label": "Site",
                    "authentication_context": "anonymous",
                    "classification": {"value": "Application"},
                }
            ],
        }
        assert (await invoke(browser, "model.transact", tx)).output["revision"] == 1
        assert (await invoke(harness, "model.search", {})).output["objects"][0][
            "id"
        ] == "site"
        assert (await invoke(harness, "model.transact", tx)).output["revision"] == 1
        history = (await invoke(harness, "model.get_updates", {})).output["edits"]
        assert len(history) == 1 and history[0]["producer"] == "assistant"
        await invoke(
            browser,
            "model.transact",
            {
                "expected_revision": 1,
                "idempotency_key": "add-page",
                "operations": [
                    {
                        "op": "put_object",
                        "id": "page",
                        "label": "Page",
                        "authentication_context": "anonymous",
                        "classification": {"value": "Page"},
                    }
                ],
            },
        )
        bad_link = {
            "expected_revision": 2,
            "idempotency_key": "link-page",
            "operations": [
                {
                    "op": "put_relationship",
                    "id": "site-page",
                    "type": "exposes",
                    "source": "site",
                    "target": "page",
                    "claim": {"value": True},
                },
            ],
        }
        rejected = await invoke(browser, "model.transact", bad_link)
        assert rejected.exit_code == 1
        options = rejected.output["recovery"]["relationship_options"]["options"]
        assert any(
            o["type"] == "contains" and o["source"] == "site" and o["target"] == "page"
            for o in options
        )
        found = await invoke(
            harness,
            "model.relationship_options",
            {"source_id": "site", "target_id": "page"},
        )
        assert found.output["revision"] == 2
        bad_link["operations"][0]["type"] = "contains"
        corrected = await invoke(harness, "model.transact", bad_link)
        assert corrected.output["revision"] == 3
        neighborhood = await invoke(
            browser, "model.neighborhood", {"object_id": "site"}
        )
        assert len(neighborhood.output["relationships"]) == 1
        with pytest.raises(InvalidToolArguments):
            await harness.broker.execute(
                ToolInvocation(
                    engagement_id="another-project",
                    run_id=turn.id,
                    tool_name="model.search",
                    workspace=tmp_path,
                ),
                scope,
            )

    asyncio.run(journey())
