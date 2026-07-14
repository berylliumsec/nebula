from fastapi.testclient import TestClient

import nebula.v3.api as api_module
from nebula.v3.api import create_app
from nebula.v3.domain import ProviderProfile, ProviderVerificationStatus
from nebula.v3.providers import ModelResponse, ToolCall
from nebula.v3.storage import NebulaStore


AUTH = {"Authorization": "Bearer test-token"}


class ProbeProvider:
    def __init__(self, *, valid: bool = True):
        self.valid = valid

    async def complete(self, request):
        assert request.tool_choice.value == "required"
        nonce = request.tools[0].input_schema["properties"]["nonce"]["enum"][0]
        return ModelResponse(
            provider_id="provider-a",
            model=request.model,
            text="" if self.valid else "<tool>not structured</tool>",
            tool_calls=(
                [
                    ToolCall(
                        id="probe-call",
                        name="nebula_capability_probe",
                        arguments={"nonce": nonce},
                    )
                ]
                if self.valid
                else []
            ),
            finish_reason="tool_calls" if self.valid else "stop",
        )


def _profile(store: NebulaStore) -> ProviderProfile:
    return store.create(
        ProviderProfile(
            id="provider-a",
            name="Local model",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8001/v1",
            is_local=True,
            model_allowlist=["model-a", "model-b"],
            metadata={"default_model": "model-a"},
        )
    )


def test_exact_model_probe_is_persisted_and_isolated(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "verification.db")
    profile = _profile(store)
    monkeypatch.setattr(
        api_module, "provider_from_profile", lambda _: ProbeProvider(valid=True)
    )
    app = create_app(store, auth_token="test-token")

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/providers/{profile.id}/capabilities/verify",
            headers=AUTH,
            json={"model": "model-a", "expected_revision": profile.revision},
        )

    assert response.status_code == 200
    stored = store.get(ProviderProfile, profile.id)
    assert stored.tools_verified_for("model-a") is True
    assert stored.tools_verified_for("model-b") is False
    assert stored.capabilities.tool_calling is True


def test_health_discovered_model_is_persisted_when_verified(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "discovered-verification.db")
    profile = store.create(
        ProviderProfile(
            id="provider-a",
            name="Discovered local model",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8001/v1",
            is_local=True,
        )
    )
    monkeypatch.setattr(
        api_module, "provider_from_profile", lambda _: ProbeProvider(valid=True)
    )
    app = create_app(store, auth_token="test-token")

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/providers/{profile.id}/capabilities/verify",
            headers=AUTH,
            json={"model": "discovered-model", "expected_revision": profile.revision},
        )

    assert response.status_code == 200
    stored = store.get(ProviderProfile, profile.id)
    assert stored.model_allowlist == ["discovered-model"]
    assert stored.tools_verified_for("discovered-model") is True
    assert stored.capabilities.tool_calling is True


def test_probe_fails_closed_and_revision_conflicts(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "verification-failure.db")
    profile = _profile(store)
    monkeypatch.setattr(
        api_module, "provider_from_profile", lambda _: ProbeProvider(valid=False)
    )
    app = create_app(store, auth_token="test-token")

    with TestClient(app) as client:
        failed = client.post(
            f"/api/v1/providers/{profile.id}/capabilities/verify",
            headers=AUTH,
            json={"model": "model-a", "expected_revision": profile.revision},
        )
        conflict = client.post(
            f"/api/v1/providers/{profile.id}/capabilities/verify",
            headers=AUTH,
            json={"model": "model-b", "expected_revision": profile.revision},
        )

    assert failed.status_code == 200
    verification = store.get(ProviderProfile, profile.id).capability_verifications[
        "model-a"
    ]
    assert verification.status == ProviderVerificationStatus.FAILED
    assert "prose" in (verification.failure_detail or "")
    assert conflict.status_code == 409


def test_compatibility_edit_invalidates_then_reverifies(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "verification-invalidation.db")
    profile = _profile(store)
    monkeypatch.setattr(
        api_module, "provider_from_profile", lambda _: ProbeProvider(valid=True)
    )
    app = create_app(store, auth_token="test-token")

    with TestClient(app) as client:
        verified = client.post(
            f"/api/v1/providers/{profile.id}/capabilities/verify",
            headers=AUTH,
            json={"model": "model-a", "expected_revision": profile.revision},
        ).json()
        updated = client.patch(
            f"/api/v1/providers/{profile.id}",
            headers=AUTH,
            json={
                "changes": {"endpoint": "http://127.0.0.1:8002/v1"},
                "expected_revision": verified["provider_revision"],
            },
        )

    assert updated.status_code == 200
    payload = updated.json()
    assert payload["capability_verifications"]["model-a"]["status"] == "verified"
    assert (
        payload["capability_verifications"]["model-a"]["checked_at"]
        != verified["verification"]["checked_at"]
    )
    assert payload["capabilities"]["tool_calling"] is True
