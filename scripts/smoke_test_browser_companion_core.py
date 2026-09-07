"""Exercise authenticated Core routes against isolated headed browserd.

Run under an available display (or xvfb-run). The bounded proxy serves only a
controlled fixture. This does not qualify a packaged application or a provider.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import tempfile

import httpx
import uvicorn
from websockets.asyncio.client import connect

from smoke_test_browserd import _respond_as_bounded_proxy


async def smoke(runtime_root: Path) -> dict[str, object]:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime_root)
    from nebula.v3.api import create_app
    from nebula.v3.browserd import BrowserdSettings, create_browserd_app
    from nebula.v3.domain import Engagement, ScopePolicy, ChatSession
    from nebula.v3.storage import NebulaStore

    proxy = await asyncio.start_server(_respond_as_bounded_proxy, "127.0.0.1", 0)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    with tempfile.TemporaryDirectory(prefix="nebula-companion-core-") as directory:
        root = Path(directory)
        settings = BrowserdSettings(
            token="isolated-browserd-token",
            profile_root=root / "profiles",
            policy_proxy_url=f"http://127.0.0.1:{proxy.sockets[0].getsockname()[1]}",
            runtime_root=runtime_root,
        )
        server = uvicorn.Server(
            uvicorn.Config(create_browserd_app(settings), log_level="error")
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        core_server = None
        core_task = None
        core_listener = None
        try:
            for _ in range(200):
                if task.done():
                    await task
                    raise RuntimeError("browserd exited before readiness")
                if server.started:
                    break
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("browserd did not start")
            os.environ["NEBULA_BROWSERD_URL"] = (
                f"http://127.0.0.1:{listener.getsockname()[1]}"
            )
            os.environ["NEBULA_BROWSERD_TOKEN"] = settings.token
            store = NebulaStore(root / "core.db")
            project = store.create(Engagement(name="Isolated browser journey"))
            scope = store.create(
                ScopePolicy(
                    engagement_id=project.id,
                    allowed_domains=["browserd-smoke.example.test"],
                )
            )
            store.update(Engagement, project.id, {"scope_policy_id": scope.id})
            app = create_app(store, auth_token="isolated-core-token")
            core_listener = socket.socket()
            core_listener.bind(("127.0.0.1", 0))
            core_listener.listen(128)
            core_listener.setblocking(False)
            core_server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            core_task = asyncio.create_task(core_server.serve(sockets=[core_listener]))
            for _ in range(200):
                if core_server.started:
                    break
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Core did not start")
            core_origin = f"http://127.0.0.1:{core_listener.getsockname()[1]}"
            async with httpx.AsyncClient(base_url=core_origin) as client:
                path = f"/api/v1/engagements/{project.id}/browser-companion"
                assert (await client.post(path)).status_code == 401
                client.headers["Authorization"] = "Bearer isolated-core-token"
                opened = await client.post(path)
                if opened.is_error:
                    raise RuntimeError(f"Core browser open failed: {opened.text}")
                opened.raise_for_status()
                session = opened.json()
                endpoint = f"/api/v1/browser-companion/{session['session_id']}"
                tab = session["active_tab_id"]
                navigation = await client.post(
                    endpoint + "/operations",
                    json={
                        "operation": "navigate",
                        "tab_id": tab,
                        "url": "http://browserd-smoke.example.test/",
                    },
                )
                if navigation.is_error:
                    raise RuntimeError(f"Core navigation failed: {navigation.text}")
                navigation.raise_for_status()
                capture = navigation.json()
                assert capture["text"] == "Ready"
                changed = await client.post(
                    endpoint + "/operations",
                    json={
                        "operation": "click",
                        "tab_id": tab,
                        "page_revision": capture["page_revision"],
                        "element_id": "0",
                    },
                )
                changed.raise_for_status()
                assert changed.json()["text"] == "Saved"
                chat = store.create(
                    ChatSession(
                        engagement_id=project.id,
                        title="Saved conversation",
                        model="fixture",
                        provider_profile_id="fixture",
                    )
                )
                bound = await client.put(
                    endpoint + "/conversation", json={"conversation_id": chat.id}
                )
                bound.raise_for_status()
                reopened = await client.post(path)
                reopened.raise_for_status()
                assert reopened.json()["conversation_id"] == chat.id
                assert reopened.json()["active_tab_id"] == tab
                assert reopened.json()["session_id"] == session["session_id"]
                secret = (
                    base64.urlsafe_b64encode(b"isolated-core-token")
                    .decode()
                    .rstrip("=")
                )
                stream_url = (
                    core_origin.replace("http://", "ws://")
                    + endpoint
                    + f"/tabs/{tab}/stream"
                )
                async with connect(
                    stream_url,
                    subprotocols=["nebula.browser.v1", f"nebula.auth.{secret}"],
                ) as stream:
                    frame = json.loads(await asyncio.wait_for(stream.recv(), 10))
                    assert frame["kind"] == "frame" and len(frame["data"]) > 100
                    resumed = await client.put(endpoint + "/control?paused=false")
                    resumed.raise_for_status()
                    await stream.send(
                        json.dumps({"kind": "resize", "width": 390, "height": 844})
                    )
                    for _ in range(100):
                        control = await client.get(endpoint + "/control")
                        control.raise_for_status()
                        if control.json()["paused"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        raise RuntimeError("manual stream input did not take control")
                reconnected = await client.post(path)
                reconnected.raise_for_status()
                assert reconnected.json()["conversation_id"] == chat.id
                return {
                    "state": "passed",
                    "authenticated_core": True,
                    "headed_browserd": True,
                    "visible_change": True,
                    "durable_binding_reopen": True,
                    "transport": "Core loopback HTTP and WebSocket to browserd loopback HTTP and WebSocket",
                    "frame_relay_and_takeover": True,
                    "page": "controlled proxy fixture",
                }
        finally:
            if core_server is not None:
                core_server.should_exit = True
            if core_task is not None:
                await core_task
            if core_listener is not None:
                core_listener.close()
            server.should_exit = True
            await task
            listener.close()
            proxy.close()
            await proxy.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(smoke(args.runtime_root.resolve())), sort_keys=True))
