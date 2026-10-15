"""OpenRouter account credit checks are separate from inference key limits."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import ProviderProfile
from nebula.v3.provider_credits import read_openrouter_credits
from nebula.v3.storage import NebulaStore


def _profile(**changes: object) -> ProviderProfile:
    return ProviderProfile(
        id="openrouter-account",
        name="OpenRouter",
        provider_type="openrouter",
        endpoint="https://example.invalid/api/v1",
        secret_ref="env:NEBULA_INFERENCE_TEST_KEY",
        **changes,
    )


def test_missing_management_key_does_not_use_inference_key(monkeypatch):
    monkeypatch.setenv("NEBULA_INFERENCE_TEST_KEY", "inference-only")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"data": {"total_credits": 100, "total_usage": 75}}
        )

    snapshot = asyncio.run(
        read_openrouter_credits(
            _profile(), CredentialStore(), transport=httpx.MockTransport(handler)
        )
    )
    assert snapshot.status == "unavailable"
    assert snapshot.reason_code == "management_credential_missing"
    assert snapshot.remaining_usd is None
    assert requests == []
    with pytest.raises(ValueError, match="must be separate"):
        _profile(openrouter_management_secret_ref="env:NEBULA_INFERENCE_TEST_KEY")


def test_account_credit_read_uses_distinct_key_and_fixed_origin(monkeypatch):
    monkeypatch.setenv("NEBULA_INFERENCE_TEST_KEY", "inference-only")
    monkeypatch.setenv("NEBULA_MANAGEMENT_TEST_KEY", "management-only")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"data": {"total_credits": 100.50, "total_usage": 80.50}}
        )

    snapshot = asyncio.run(
        read_openrouter_credits(
            _profile(openrouter_management_secret_ref="env:NEBULA_MANAGEMENT_TEST_KEY"),
            CredentialStore(),
            transport=httpx.MockTransport(handler),
        )
    )

    assert snapshot.status == "observed"
    assert snapshot.total_credits_usd == 100.5
    assert snapshot.total_usage_usd == 80.5
    assert snapshot.remaining_usd == 20.0
    assert snapshot.observed_at is not None
    assert snapshot.reason_code is None
    assert len(requests) == 1
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/credits"
    assert requests[0].headers["authorization"] == "Bearer management-only"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            httpx.Response(403, json={"error": "no management access"}),
            "management_credential_rejected",
        ),
        (httpx.Response(500, text="upstream failed"), "upstream_unavailable"),
        (
            httpx.Response(200, json={"data": {"total_credits": 100}}),
            "invalid_upstream_response",
        ),
        (
            httpx.Response(
                200, json={"data": {"total_credits": "NaN", "total_usage": 0}}
            ),
            "invalid_upstream_response",
        ),
    ],
)
def test_account_credit_failure_is_explicit_and_has_no_amounts(
    monkeypatch, response, reason
):
    monkeypatch.setenv("NEBULA_MANAGEMENT_TEST_KEY", "management-only")
    snapshot = asyncio.run(
        read_openrouter_credits(
            _profile(openrouter_management_secret_ref="env:NEBULA_MANAGEMENT_TEST_KEY"),
            CredentialStore(),
            transport=httpx.MockTransport(lambda request: response),
        )
    )
    assert snapshot.status == "unavailable"
    assert snapshot.reason_code == reason
    assert snapshot.remaining_usd is None
    assert snapshot.observed_at is None


def test_credit_refresh_route_requires_auth_and_openrouter_profile(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NEBULA_MANAGEMENT_TEST_KEY", "management-only")
    store = NebulaStore(tmp_path / "core.sqlite")
    store.create(
        _profile(openrouter_management_secret_ref="env:NEBULA_MANAGEMENT_TEST_KEY")
    )
    store.create(
        ProviderProfile(id="other-provider", name="Other", provider_type="openai")
    )
    app = create_app(
        store=store,
        auth_token="test-token",
        openrouter_credit_transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"data": {"total_credits": 35.25, "total_usage": 15.30}},
            )
        ),
    )
    with TestClient(app) as client:
        path = "/api/v1/providers/openrouter-account/openrouter/credits/refresh"
        assert client.post(path).status_code == 401
        response = client.post(path, headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200, response.text
        assert response.json()["remaining_usd"] == 19.95
        unsupported = client.post(
            "/api/v1/providers/other-provider/openrouter/credits/refresh",
            headers={"Authorization": "Bearer test-token"},
        )
        assert unsupported.status_code == 422


def test_management_reference_round_trips_through_provider_create_and_patch(tmp_path):
    store = NebulaStore(tmp_path / "core.sqlite")
    app = create_app(store=store, auth_token="test-token")
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/providers",
            headers=headers,
            json={
                "name": "OpenRouter",
                "provider_type": "openrouter",
                "secret_ref": "env:NEBULA_INFERENCE_TEST_KEY",
                "openrouter_management_secret_ref": "env:NEBULA_MANAGEMENT_TEST_KEY",
            },
        )
        assert created.status_code == 201, created.text
        profile = created.json()
        assert profile["openrouter_management_secret_ref"] == (
            "env:NEBULA_MANAGEMENT_TEST_KEY"
        )
        changed = client.patch(
            f"/api/v1/providers/{profile['id']}",
            headers=headers,
            json={
                "changes": {
                    "openrouter_management_secret_ref": "env:NEBULA_NEXT_MANAGEMENT_KEY"
                },
                "expected_revision": profile["revision"],
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["openrouter_management_secret_ref"] == (
            "env:NEBULA_NEXT_MANAGEMENT_KEY"
        )
        saved = store.get(ProviderProfile, profile["id"])
        assert saved.openrouter_management_secret_ref == (
            "env:NEBULA_NEXT_MANAGEMENT_KEY"
        )
