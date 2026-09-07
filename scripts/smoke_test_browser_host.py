"""Exercise automatic Core-owned browser startup against a controlled local page."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile

import httpx

from smoke_test_browserd import _respond_as_bounded_proxy


async def smoke(runtime_root: Path) -> dict[str, object]:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime_root)
    os.environ.pop("NEBULA_BROWSERD_URL", None)
    os.environ.pop("NEBULA_BROWSERD_TOKEN", None)
    from nebula.v3.api import create_app
    from nebula.v3.browser_companion import (
        BrowserCompanion,
        CompanionRequest,
        CompanionAction,
    )
    from nebula.v3.browser_companion_tools import CompanionBroker
    from nebula.v3.domain import BrowserSession, ChatSession, Engagement, ScopePolicy
    from nebula.v3.storage import NebulaStore

    observed_requests: list[bytes] = []

    async def serve_fixture(reader, writer):
        await _respond_as_bounded_proxy(
            reader, writer, observed_requests=observed_requests
        )

    target = await asyncio.start_server(serve_fixture, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{target.sockets[0].getsockname()[1]}/"
    try:
        with tempfile.TemporaryDirectory(prefix="nebula-host-smoke-") as temporary:
            store = NebulaStore(Path(temporary) / "core.db")
            project = store.create(Engagement(name="Automatic host validation"))
            scope = store.create(
                ScopePolicy(engagement_id=project.id, allowed_cidrs=["127.0.0.1/32"])
            )
            store.update(Engagement, project.id, {"scope_policy_id": scope.id})
            app = create_app(store, auth_token="isolated-host-smoke-token")
            host = BrowserCompanion._store_hosts[store]()
            assert host is not None and host._task is None
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app),
                    base_url="http://core.test",
                    headers={"Authorization": "Bearer isolated-host-smoke-token"},
                    timeout=40,
                ) as client:
                    path = f"/api/v1/engagements/{project.id}/browser-companion"
                    first, second = await asyncio.gather(
                        client.post(path), client.post(path)
                    )
                    first.raise_for_status()
                    second.raise_for_status()
                    session = first.json()
                    assert second.json()["session_id"] == session["session_id"]
                    endpoint = f"/api/v1/browser-companion/{session['session_id']}"
                    result = await client.post(
                        endpoint + "/operations",
                        json={
                            "operation": "navigate",
                            "tab_id": session["active_tab_id"],
                            "url": url,
                        },
                    )
                    result.raise_for_status()
                    assert result.json()["text"] == "Ready", result.text
                    identity_id = store.get(
                        BrowserSession, session["session_id"]
                    ).identity_id
                    live_page = await host._manager.page_for_screencast(
                        identity_id, session["active_tab_id"]
                    )
                    policy_proxy = host._proxies[identity_id]
                    authorize = policy_proxy.authorize
                    denied_destinations = []

                    def observe_denial(destination):
                        try:
                            authorize(destination)
                        except ValueError:
                            denied_destinations.append(destination)
                            raise

                    policy_proxy.authorize = observe_denial
                    blocked_url = url.replace("127.0.0.1", "localhost")
                    async with httpx.AsyncClient() as reachable_client:
                        assert (
                            await reachable_client.get(blocked_url)
                        ).status_code == 200
                    observed_requests.clear()
                    await live_page.evaluate(
                        "url => fetch(url, {mode:'no-cors'}).then(() => true, () => false)",
                        blocked_url,
                    )
                    assert blocked_url in denied_destinations
                    assert not any(
                        b"localhost:" in headers for headers in observed_requests
                    )
                    # Harness/provider brokers must reach this same host without
                    # environment-variable registration or a second Chromium.
                    broker = CompanionBroker(store, session["session_id"])
                    assert await broker.service.adapter() is host._adapter
                    async with httpx.AsyncClient() as private_client:
                        denied = await private_client.get(
                            host._adapter.base_url + "/v1/readiness"
                        )
                        assert denied.status_code == 401
                    chat = store.create(
                        ChatSession(
                            engagement_id=project.id,
                            title="Saved host conversation",
                            model="fixture",
                            provider_profile_id="fixture",
                        )
                    )
                    (
                        await client.put(
                            endpoint + "/conversation",
                            json={"conversation_id": chat.id},
                        )
                    ).raise_for_status()
                    context = host._manager._contexts[identity_id]
                    (
                        await client.put(endpoint + "/control?paused=false")
                    ).raise_for_status()
                    pending = broker.service.propose(
                        session["session_id"],
                        CompanionRequest(
                            operation="click",
                            tab_id=session["active_tab_id"],
                            page_revision=result.json()["page_revision"],
                            element_id="0",
                        ),
                    )
                    await context.close()
                    # A viewer's background tab refresh can win the race with
                    # its explicit reconnect; it must not erase the loss notice.
                    refreshed = await client.post(
                        endpoint + "/operations", json={"operation": "tabs"}
                    )
                    refreshed.raise_for_status()
                    reopened = await client.post(path)
                    reopened.raise_for_status()
                    restored = reopened.json()
                    assert restored["session_id"] == session["session_id"]
                    assert restored["conversation_id"] == chat.id
                    assert restored["page_state_reset"] is True
                    assert restored["active_tab_id"] != session["active_tab_id"]
                    assert restored["tabs"][0]["url"] == "about:blank"
                    assert (await client.get(endpoint + "/control")).json()[
                        "paused"
                    ] is True
                    assert store.get(CompanionAction, pending.id).status == "revoked"
                    stale = await client.post(
                        endpoint + "/operations",
                        json={
                            "operation": "click",
                            "tab_id": session["active_tab_id"],
                            "page_revision": result.json()["page_revision"],
                            "element_id": "0",
                        },
                    )
                    assert stale.status_code == 409
                    navigated = await client.post(
                        endpoint + "/operations",
                        json={
                            "operation": "navigate",
                            "tab_id": restored["active_tab_id"],
                            "url": url,
                        },
                    )
                    navigated.raise_for_status()
                    assert (await client.post(path)).json()["page_state_reset"] is False
                    private_endpoint = host._adapter.base_url
            assert host._task is None and not host._proxies
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    await client.get(private_endpoint + "/v1/readiness")
            except httpx.ConnectError:
                pass
            else:
                raise AssertionError("Core shutdown left browserd reachable")
            return {
                "state": "passed",
                "automatic_host_startup": True,
                "concurrent_open_single_session": True,
                "real_headed_chromium": True,
                "authenticated_policy_proxy": True,
                "live_browser_subrequest_scope_denial": True,
                "broker_shared_host": True,
                "private_endpoint_requires_auth": True,
                "chromium_exit_recovery": True,
                "saved_conversation_lost_tabs_distinguished": True,
                "shutdown_releases_host": True,
                "core_transport": "ASGI",
                "page": "controlled loopback HTTP fixture",
            }
    finally:
        target.close()
        await target.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(smoke(arguments.runtime_root)), sort_keys=True))
