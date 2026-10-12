"""Focused real mitmdump/Core boundary test; opt in with a staged sidecar."""

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from nebula.v3.agent_proxy import AgentMitmProxy
from nebula.v3.browser_research import BrowserResearchService, InterceptDecisionRequest
from nebula.v3.browser_security import BrowserSecurityService
from nebula.v3.domain import (
    BrowserIdentity,
    BrowserInterceptItem,
    BrowserSession,
    BrowserTabState,
    Engagement,
    RiskClass,
    ScopePolicy,
)
from nebula.v3.storage import NebulaStore


@pytest.mark.skipif(
    not os.environ.get("NEBULA_MITMDUMP_EXECUTABLE"),
    reason="the separate security-proxy sidecar is not staged",
)
def test_agent_proxy_scopes_captures_and_intercepts_without_visible_browser(
    tmp_path, monkeypatch
):
    addon = Path(__file__).parents[2] / "security-proxy" / "addon.py"
    assert addon.is_file()
    monkeypatch.setenv("NEBULA_SECURITY_PROXY_ADDON", str(addon))
    store = NebulaStore(tmp_path / "nebula.db")
    project = store.create(Engagement(name="Proxy fixture"))
    hits = []

    async def fixture(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        hits.append(1)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nfixture"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def run():
        server = await asyncio.start_server(fixture, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        scope = store.create(
            ScopePolicy(
                engagement_id=project.id,
                allowed_cidrs=["127.0.0.1/32"],
                allowed_ports=[port],
            )
        )
        store.update(Engagement, project.id, {"scope_policy_id": scope.id})
        identity = store.create(
            BrowserIdentity(
                engagement_id=project.id,
                name="Headless",
                metadata={"runtime_kind": "agent_security"},
            )
        )
        session = store.create(
            BrowserSession(
                engagement_id=project.id,
                identity_id=identity.id,
                runtime_kind="agent_security",
                name="Headless",
                proxy_enabled=True,
                proxy_trust_acknowledged=True,
                tabs=[BrowserTabState(id="headless-tab")],
                active_tab_id="headless-tab",
            )
        )
        security = BrowserSecurityService(store)
        research = BrowserResearchService(store, security)

        def authorize(url):
            security._require_in_scope(
                security._scope(project.id),
                url,
                "agent_security.proxy",
                RiskClass.ACTIVE_SCAN,
                native_scope_authority=True,
            )

        proxy = AgentMitmProxy(
            store, session.id, identity.id, tmp_path / "proxies", authorize
        )
        try:
            config = await proxy.start()
            proxy_url = config["server"].replace(
                "http://", f"http://nebula:{config['password']}@"
            )
            async with httpx.AsyncClient(proxy=proxy_url, timeout=10) as client:
                response = await client.get(f"http://127.0.0.1:{port}/one")
                assert response.status_code == 200
                assert response.text == "fixture"
                assert len(security.workspace(project.id).traffic) == 1

                try:
                    outside = await client.get(f"http://127.0.0.1:{port + 1}/outside")
                    assert outside.status_code >= 400
                except httpx.HTTPError:
                    # mitmdump may close a denied flow before writing an HTTP response.
                    pass
                assert len(hits) == 1

                session = store.get(BrowserSession, session.id)
                store.update(
                    BrowserSession,
                    session.id,
                    {
                        "interception_enabled": True,
                    },
                    expected_revision=session.revision,
                )
                waiting = asyncio.create_task(
                    client.get(f"http://127.0.0.1:{port}/two")
                )

                async def wait_for_phase(phase):
                    for _ in range(100):
                        await asyncio.sleep(0.05)
                        items = store.find_entities(
                            BrowserInterceptItem,
                            {"state": "paused", "phase": phase},
                            engagement_id=project.id,
                        )
                        if items:
                            return items[0]
                    return None

                paused = await wait_for_phase("request")
                assert paused is not None
                assert not waiting.done()
                research.decide_intercept(
                    paused.id,
                    InterceptDecisionRequest(
                        expected_revision=paused.revision,
                        decision="forward",
                        operator_id="test-operator",
                    ),
                )
                response_pause = await wait_for_phase("response")
                assert response_pause is not None
                assert not waiting.done()
                research.decide_intercept(
                    response_pause.id,
                    InterceptDecisionRequest(
                        expected_revision=response_pause.revision,
                        decision="forward",
                        operator_id="test-operator",
                    ),
                )
                forwarded = await waiting
                assert forwarded.status_code == 200
                assert len(hits) == 2
                assert len(security.workspace(project.id).traffic) == 2
        finally:
            await proxy.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())
