"""Real Core graph routes and the shared browser/harness tool contract."""

import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from tests.v3.async_test_support import AsyncTestClient as TestClient
from nebula.v3.api import create_app
from nebula.v3.storage import NebulaStore
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.application_model.tools import INPUTS


async def test_real_core_graph_capture_and_project_scope(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(
        store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        auth_token="test-token",
    )
    auth = {"Authorization": "Bearer test-token"}
    async with TestClient(app) as client:
        project = (
            await client.post(
                "/api/v1/engagements", headers=auth, json={"name": "Site A"}
            )
        ).json()
        base = f"/api/v1/engagements/{project['id']}/application-model"
        assert (await client.get(base + "/graph")).status_code == 401
        assert (await client.get(base + "/graph", headers=auth)).json()["objects"] == []
        assert (
            len((await client.get(base + "/schema", headers=auth)).json()["types"])
            == 33
        )
        browser = (
            await client.get(
                f"/api/v1/engagements/{project['id']}/browser-workspace", headers=auth
            )
        ).json()["sessions"][0]
        sync = await client.put(
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
        capture = await client.post(
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
        evidence = (await client.get(base + "/evidence", headers=auth)).json()[
            "evidence"
        ][0]
        assert evidence["facts"]["status_code"]["value"] == 403
        assert (await client.get(base + "/graph", headers=auth)).json()["objects"] == []
        transaction = {
            "expected_revision": 0,
            "idempotency_key": "save",
            "operations": [
                {
                    "op": "put_object",
                    "id": "firewall",
                    "properties": {
                        "purpose": {
                            "value": "Track why this request was denied; enforcement mechanism remains uncertain."
                        }
                    },
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
        missing_purpose = {
            **transaction,
            "operations": [{**transaction["operations"][0], "properties": {}}],
        }
        rejected = await client.post(
            base + "/transactions", headers=auth, json=missing_purpose
        )
        assert rejected.status_code == 422
        assert "properties.purpose" in rejected.text
        assert (await client.get(base + "/graph", headers=auth)).json()["objects"] == []
        response = await client.post(
            base + "/transactions", headers=auth, json=transaction
        )
        assert response.status_code == 200, response.text
        assert (
            await client.post(base + "/transactions", headers=auth, json=transaction)
        ).json() == response.json()
        assert (await client.get(base + "/graph", headers=auth)).json()["objects"][0][
            "classification"
        ]["status"] == "hypothesized"
        other = (
            await client.post(
                "/api/v1/engagements", headers=auth, json={"name": "Other"}
            )
        ).json()
        otherbase = f"/api/v1/engagements/{other['id']}/application-model"
        assert (
            await client.get(
                otherbase + f"/evidence/{evidence['kind']}/{evidence['id']}",
                headers=auth,
            )
        ).status_code == 404
        assert (
            await client.post(base + "/sessions", headers=auth, json={})
        ).status_code == 404
        assert not any(
            "queries" in path or "states" in path
            for path in app.openapi()["paths"]
            if "application-model" in path
        )


async def test_browser_and_project_tools_expose_same_graph(tmp_path):
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
    project = await store.create(Engagement(name="Shared graph"))
    chat = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Model",
            model="fixture",
            provider_profile_id="fixture",
        )
    )
    turn = await store.create(
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
                    "properties": {
                        "purpose": {
                            "value": "Define the application boundary for its authentication workflow."
                        }
                    },
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
                        "properties": {
                            "purpose": {
                                "value": "Identify the user entry point into the authentication workflow."
                            }
                        },
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

    (await journey())


async def test_a_chat_model_learns_why_its_model_edit_was_refused(tmp_path):
    """A refused model edit reaches the chat model as Core's guided failure."""
    from nebula.v3.application_model.tools import project_components
    from nebula.v3.chat import ChatService
    from nebula.v3.domain import (
        ChatSession,
        ChatTurn,
        Engagement,
        ScopePolicy,
        ToolCallOrigin,
    )
    from nebula.v3.tools import ToolInvocation
    import json

    store = NebulaStore(tmp_path / "refusal.db")
    project = await store.create(Engagement(name="Refusals"))
    chat = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Model",
            model="fixture",
            provider_profile_id="fixture",
        )
    )
    turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            model="fixture",
            provider_profile_id="fixture",
            tools_enabled=True,
        )
    )
    scope = ScopePolicy(engagement_id=project.id)
    runtime = project_components(store, project.id, scope, tmp_path)
    service = ChatService(store)

    async def model_sees(name, arguments, call_id):
        invocation = ToolInvocation(
            engagement_id=project.id,
            run_id=turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=chat.id,
            chat_turn_id=turn.id,
            tool_name=name,
            arguments=arguments,
            workspace=tmp_path,
        )
        result = await runtime.broker.execute(invocation, scope)
        assert result.exit_code == 1 and result.output["recovery"]
        entry, waiting = await service._tool_result_entry(
            result,
            invocation=invocation,
            spec=runtime.specs[name],
            arguments=arguments,
            call_id=call_id,
        )
        assert not waiting and entry["status"] == "failed"
        # What the provider replays to the model for this call.
        return json.loads(entry["provider_result"])

    unknown = await model_sees(
        "model.transact",
        {
            "expected_revision": 0,
            "idempotency_key": "unknown-type",
            "operations": [
                {
                    "op": "put_object",
                    "id": "entry",
                    "label": "Entry",
                    "authentication_context": "anonymous",
                    "properties": {
                        "purpose": {"value": "Where an anonymous visitor signs in."}
                    },
                    "classification": {"value": "not-a-schema-type"},
                }
            ],
        },
        "call-1",
    )
    # Before, every refusal read "The tool could not complete." with no way
    # to tell a schema mistake from a stale revision.
    assert unknown["problem"] != "The tool could not complete."
    assert unknown["category"] == "invalid_arguments"
    assert unknown["side_effects"] == "none"
    assert "Nothing was saved" in unknown["problem"]
    assert "not-a-schema-type" not in str(unknown)
    assert "model.discover_schema" in unknown["next_action"]

    unlinked = await model_sees(
        "model.transact",
        {
            "expected_revision": 0,
            "idempotency_key": "unlinked",
            "operations": [
                {
                    "op": "put_relationship",
                    "id": "a-b",
                    "type": "contains",
                    "source": "absent-a",
                    "target": "absent-b",
                    "claim": {"value": True},
                }
            ],
        },
        "call-4",
    )
    # Core's own fixed sentence is shown as written.
    assert unlinked["problem"].startswith("Both endpoints must exist in this project")
    assert "model.relationship_options" in unlinked["next_action"]

    stale = await model_sees(
        "model.transact",
        {
            "expected_revision": 7,
            "idempotency_key": "stale",
            "operations": [
                {
                    "op": "put_object",
                    "id": "page",
                    "label": "Page",
                    "authentication_context": "anonymous",
                    "properties": {
                        "purpose": {"value": "Where an anonymous visitor signs in."}
                    },
                    "classification": {"value": "Page"},
                }
            ],
        },
        "call-2",
    )
    assert stale["category"] == "conflict"
    assert "revision 0" in stale["next_action"]
    assert stale["error_code"] == "application_model.conflict"

    missing = await model_sees(
        "model.neighborhood", {"object_id": "missing-object"}, "call-3"
    )
    assert missing["category"] == "unavailable_resource"
    assert "Nothing changed" in missing["problem"]
    await service.shutdown()


pytestmark = pytest.mark.anyio
