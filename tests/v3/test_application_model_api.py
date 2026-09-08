"""Real Core routes, browser capture, durable queries and project isolation."""

import base64
import json
import time
from fastapi.testclient import TestClient
from nebula.v3.api import create_app
from nebula.v3.storage import NebulaStore
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import Engagement, ScopePolicy, BrowserSession


def test_browser_capture_to_state_to_query_survives_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBULA_APPLICATION_MODEL", "1")
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(
        store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        auth_token="test-token",
    )
    auth = {"Authorization": "Bearer test-token"}
    assert not any(path.startswith("/api/v1/application-model-") for path in app.openapi()["paths"])
    with TestClient(app) as client:
        project = client.post(
            "/api/v1/engagements", headers=auth, json={"name": "Model fixture"}
        ).json()
        scope = store.get(
            ScopePolicy, store.get(Engagement, project["id"]).scope_policy_id
        )
        store.update(
            ScopePolicy,
            scope.id,
            {"allowed_domains": ["app.example.test"], "allowed_ports": [443]},
            expected_revision=scope.revision,
        )
        workspace = client.get(
            f"/api/v1/engagements/{project['id']}/browser-workspace", headers=auth
        ).json()
        browser = workspace["sessions"][0]
        response = client.put(
            f"/api/v1/browser-sessions/{browser['id']}/tabs",
            headers=auth,
            json={
                "expected_revision": browser["revision"],
                "tabs": [
                    {
                        "id": "fixture",
                        "url": "https://app.example.test/records/1",
                        "title": "Fixture",
                        "position": 0,
                    }
                ],
                "active_tab_id": "fixture",
                "device_owner": "desktop-fixture",
            },
        )
        assert response.status_code == 200, response.text
        current = store.get(BrowserSession, browser["id"])
        store.update(
            BrowserSession,
            current.id,
            {"capture_mode": "bodies"},
            expected_revision=current.revision,
        )
        base = f"/api/v1/engagements/{project['id']}/application-model"
        response = client.post(
            base + "/sessions", headers=auth, json={"browser_session_id": browser["id"]}
        )
        assert response.status_code == 201, response.text
        collection = response.json()["id"]
        for state in ["draft", "submitted"]:
            response = client.post(
                f"/api/v1/browser-sessions/{browser['id']}/body-artifacts",
                headers=auth,
                json={
                    "direction": "response",
                    "media_type": "application/json",
                    "content_base64": base64.b64encode(
                        json.dumps(
                            {"id": 1, "status": state, "token": "private"}
                        ).encode()
                    ).decode(),
                },
            )
            assert response.status_code == 201, response.text
            artifact = response.json()
            response = client.post(
                f"/api/v1/browser-sessions/{browser['id']}/traffic",
                headers=auth,
                json={
                    "tab_id": "fixture",
                    "url": "https://app.example.test/records/1",
                    "method": "GET",
                    "status_code": 200,
                    "response_body_artifact_id": artifact["id"],
                },
            )
            assert response.status_code == 201, response.text
        app.state.application_model.process_batch()
        data = client.get(
            base + f"/sessions/{collection}/workspace", headers=auth
        ).json()
        assert len(data["states"]) == 2
        assert len(data["objects"]) == 1
        assert "private" not in json.dumps(data)
        final = data["states"][-1]
        fields = client.get(
            base + f"/sessions/{collection}/states/{final['id']}/fields", headers=auth
        ).json()
        field = next(key for key in fields if key.endswith("response.status"))
        assert fields[field]["value"] == "submitted"
        response = client.post(
            base + f"/sessions/{collection}/queries",
            headers=auth,
            json={
                "state_id": final["id"],
                "formula": {
                    "op": "eq",
                    "args": [
                        {"op": "field", "field": field},
                        {"op": "literal", "value": "submitted"},
                    ],
                },
            },
        )
        assert response.status_code == 202, response.text
        query = response.json()["id"]
        for _ in range(100):
            result = client.get(
                base + f"/sessions/{collection}/queries/{query}", headers=auth
            ).json()
            if result["status"] not in {"running", "queued"}:
                break
            time.sleep(0.05)
        assert result["result"] == "SAT", result
        fork_response = client.post(
            base + f"/sessions/{collection}/states/{final['id']}/fork",
            headers=auth,
            json={"assertion_ids": final["assertion_ids"]},
        )
        assert fork_response.status_code == 200, fork_response.text
        fork = fork_response.json()
        assert fork["id"] != final["id"]
        assert fork["parent_state_ids"] == [final["id"]]
        assert fork["semantic_hash"] == final["semantic_hash"]
        assert fork["interpretation"] is True
        original = client.get(
            base + f"/sessions/{collection}/states/{final['id']}", headers=auth
        ).json()
        assert original == final
        assert client.get(base + f"/sessions/{collection}/workspace").status_code == 401
    # A fresh service and Core instance reconstruct the same saved records.
    app2 = create_app(
        store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        auth_token="test-token",
    )
    with TestClient(app2) as client:
        reloaded = client.get(
            base + f"/sessions/{collection}/queries/{query}", headers=auth
        )
        assert reloaded.json()["result"] == "SAT"
