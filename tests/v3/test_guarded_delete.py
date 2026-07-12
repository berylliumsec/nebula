from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import (
    AgentRun,
    Asset,
    ChatSession,
    Engagement,
    ProviderProfile,
    Service,
)
from nebula.v3.storage import NebulaStore


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
