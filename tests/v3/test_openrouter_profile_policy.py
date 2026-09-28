"""Opt-in OpenRouter profile routing stays scoped to the saved profile."""

import asyncio

import httpx
import pytest

from nebula.v3.api import _invalidate_provider_verification
from nebula.v3.domain import ProviderProfile
from nebula.v3.providers import (
    ModelMessage,
    ModelRequest,
    OpenAICompatibleProvider,
    ProviderError,
    ProviderFlavor,
    config_from_catalog,
)


def _provider(*, zdr=False, allowed=(), handler=None):
    return OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="policy-test",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
            default_model="author/model",
            options={"openrouter_zdr": zdr, "openrouter_providers": list(allowed)},
        ),
        **({"transport": httpx.MockTransport(handler)} if handler else {}),
    )


def _request():
    return ModelRequest(messages=[ModelMessage(role="user", content="Hello")])


def _endpoint(tag):
    return {
        "provider_name": tag.split("/", 1)[0],
        "tag": tag,
        "context_length": 128_000,
        "max_prompt_tokens": 120_000,
        "max_completion_tokens": 8_000,
        "supported_parameters": ["tools"],
        "status": 0,
    }


def _zdr_rows(*routes):
    return {"data": [{"model_id": model, "tag": tag} for model, tag in routes]}


def test_zdr_and_upstream_allowlist_are_independent_per_profile():
    assert _provider()._payload(_request(), "author/model").get("provider") is None
    assert _provider(zdr=True)._payload(_request(), "author/model")["provider"] == {
        "zdr": True
    }
    assert _provider(allowed=("relace",))._payload(_request(), "author/model")[
        "provider"
    ] == {"only": ["relace"]}
    assert _provider(zdr=True, allowed=("relace",))._payload(
        _request(), "author/model"
    )["provider"] == {"only": ["relace"], "zdr": True}


def test_zdr_catalog_intersects_account_models_with_exact_allowed_endpoints():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        path = request.url.path
        if path == "/api/v1/key":
            return httpx.Response(200, json={"data": {}})
        if path == "/api/v1/models/user":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "author/model"},
                        {"id": "author/other"},
                        {"id": "private/account-only"},
                    ]
                },
            )
        if path == "/api/v1/providers":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"name": "Relace", "slug": "relace"},
                        {"name": "Wafer", "slug": "wafer"},
                    ]
                },
            )
        if path == "/api/v1/endpoints/zdr":
            return httpx.Response(
                200,
                json=_zdr_rows(
                    ("author/model", "wafer/fp8"),
                    ("author/other", "relace/fp8"),
                    ("outside/account", "relace/fp8"),
                ),
            )
        raise AssertionError(path)

    health = asyncio.run(
        _provider(zdr=True, allowed=("relace",), handler=handler).health()
    )

    assert health.healthy is True
    assert health.models == ["author/other"]
    assert "/api/v1/models" not in seen


def test_zdr_catalog_fails_closed_when_endpoint_directory_fails():
    def handler(request):
        path = request.url.path
        if path == "/api/v1/key":
            return httpx.Response(200, json={"data": {}})
        if path == "/api/v1/models/user":
            return httpx.Response(200, json={"data": [{"id": "author/model"}]})
        if path == "/api/v1/providers":
            return httpx.Response(200, json={"data": []})
        if path == "/api/v1/endpoints/zdr":
            return httpx.Response(503, json={})
        raise AssertionError(path)

    health = asyncio.run(_provider(zdr=True, handler=handler).health())

    assert health.healthy is False
    assert health.models == []
    assert "ZDR endpoint discovery failed" in health.detail


def test_route_limits_intersect_exact_zdr_tags_and_allowed_upstreams():
    def handler(request):
        if request.url.path == "/api/v1/endpoints/zdr":
            return httpx.Response(
                200,
                json=_zdr_rows(
                    ("author/model", "relace/zdr"),
                    ("author/model", "wafer/zdr"),
                    ("author/other", "relace/other"),
                ),
            )
        assert request.url.path.endswith("/models/author/model/endpoints")
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "author/model",
                    "endpoints": [
                        _endpoint("relace/zdr"),
                        _endpoint("relace/ordinary"),
                        _endpoint("wafer/zdr"),
                    ],
                }
            },
        )

    routes = asyncio.run(
        _provider(
            zdr=True, allowed=("relace",), handler=handler
        ).openrouter_route_limits("author/model")
    )

    assert [(route.provider_slug, route.context_window) for route in routes] == [
        ("relace", 128_000)
    ]


def test_route_limits_reject_when_allowed_upstream_has_no_zdr_endpoint():
    def handler(request):
        if request.url.path == "/api/v1/endpoints/zdr":
            return httpx.Response(200, json=_zdr_rows(("author/model", "wafer/zdr")))
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "author/model",
                    "endpoints": [_endpoint("relace/ordinary")],
                }
            },
        )

    with pytest.raises(ProviderError, match="No ZDR OpenRouter endpoints"):
        asyncio.run(
            _provider(
                zdr=True, allowed=("relace",), handler=handler
            ).openrouter_route_limits("author/model")
        )


def test_policy_change_discards_old_catalog_and_verification():
    current = ProviderProfile(
        name="OpenRouter",
        provider_type="openrouter",
        metadata={
            "options": {"openrouter_providers": ["wafer"]},
            "model_descriptors": [
                {"id": "author/model", "route_limits": [{"provider_slug": "wafer"}]}
            ],
            "model_catalog_revision": "old",
            "route_catalog_revision": "old",
        },
    )
    candidate = current.model_copy(
        update={
            "metadata": {
                **current.metadata,
                "options": {
                    "openrouter_providers": ["relace"],
                    "openrouter_zdr": True,
                },
            }
        }
    )

    updated = _invalidate_provider_verification(current, candidate)

    assert updated.metadata["options"]["openrouter_zdr"] is True
    assert "model_descriptors" not in updated.metadata
    assert "model_catalog_revision" not in updated.metadata
    assert "route_catalog_revision" not in updated.metadata


def test_zdr_option_requires_a_boolean():
    with pytest.raises(ValueError, match="openrouter_zdr must be a boolean"):
        ProviderProfile(
            name="OpenRouter",
            provider_type="openrouter",
            metadata={
                "options": {"openrouter_zdr": "true"},
            },
        )
