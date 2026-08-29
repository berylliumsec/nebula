import asyncio

import httpx
import pytest

from nebula.v3.browser_engine import (
    BrowserEngineAction,
    BrowserEngineRegistry,
    LocalBrowserdAdapter,
)
from nebula.v3.domain import BrowserEngineState


def test_browserd_adapter_requires_literal_loopback_and_opaque_authentication():
    with pytest.raises(ValueError, match="literal loopback"):
        LocalBrowserdAdapter("http://localhost:4711", "opaque-token")
    with pytest.raises(ValueError, match="literal loopback"):
        LocalBrowserdAdapter("https://example.test:4711", "opaque-token")
    with pytest.raises(ValueError, match="opaque"):
        LocalBrowserdAdapter("http://127.0.0.1:4711", "secret token")


def test_browserd_adapter_maps_authenticated_readiness_without_url_secrets():
    observed: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "adapter": "managed-chromium",
                "display_name": "Managed Chromium",
                "contract_version": "1",
                "state": "ready",
                "installed_version": "149.0.0",
                "digest": f"sha256:{'a' * 64}",
                "actions": ["navigate", "click", "snapshot", "trace"],
                "protocols": ["http", "https", "websocket"],
                "check_families": [],
                "desktop_only": True,
            },
        )

    token = "browserd-secret-token"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        capability = asyncio.run(
            LocalBrowserdAdapter(
                "http://127.0.0.1:4711", token, client=client
            ).readiness()
        )
    finally:
        asyncio.run(client.aclose())
    assert capability.state == BrowserEngineState.READY
    assert observed["authorization"] == f"Bearer {token}"
    assert token not in str(observed["url"])


def test_browserd_adapter_fails_closed_on_contract_drift():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "adapter": "managed-chromium",
                "display_name": "Managed Chromium",
                "contract_version": "2",
                "state": "ready",
                "actions": [],
                "protocols": [],
                "check_families": [],
                "desktop_only": True,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        capability = asyncio.run(
            LocalBrowserdAdapter(
                "http://127.0.0.1:4711", "token", client=client
            ).readiness()
        )
    finally:
        asyncio.run(client.aclose())
    assert capability.state == BrowserEngineState.UNAVAILABLE
    assert "incompatible" in (capability.unavailability_reason or "")


def test_browserd_lost_side_effect_receipt_is_ambiguous_and_never_auto_replayed():
    async def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("receipt connection closed")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    action = BrowserEngineAction(
        action_token="action-once",
        assessment_id="assessment-1",
        session_id="session-1",
        identity_id="identity-1",
        tab_id="tab-1",
        kind="click",
        locator={"role": "button", "name": "Submit"},
        side_effect="possible",
    )
    try:
        receipt = asyncio.run(
            LocalBrowserdAdapter(
                "http://127.0.0.1:4711", "token", client=client
            ).execute(action)
        )
    finally:
        asyncio.run(client.aclose())
    assert receipt.state == "ambiguous"
    assert receipt.action_token == action.action_token
    assert "will not replay" in (receipt.recovery_action or "")


def test_registry_discovers_configured_browserd_without_exposing_token(monkeypatch):
    monkeypatch.setenv("NEBULA_BROWSERD_URL", "http://127.0.0.1:4711")
    monkeypatch.setenv("NEBULA_BROWSERD_TOKEN", "configured-secret")
    registry = BrowserEngineRegistry()
    assert len(registry._adapters) == 1
    assert isinstance(registry._adapters[0], LocalBrowserdAdapter)
