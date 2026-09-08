"""Read-only public-page navigation through an isolated authenticated Core host.

Run with PYTHONPATH=src, PLAYWRIGHT_BROWSERS_PATH, and an available display.
No existing profile, project, credentials or page state is used.
"""

import asyncio
import base64
import json
import secrets
import socket
import tempfile
from pathlib import Path

import httpx
import uvicorn
from websockets.asyncio.client import connect

from nebula.v3.api import create_app
from nebula.v3.domain import Engagement, ScopePolicy
from nebula.v3.storage import NebulaStore


async def main():
    with tempfile.TemporaryDirectory(prefix="nebula-navigation-") as directory:
        store = NebulaStore(Path(directory) / "core.db")
        project = store.create(Engagement(name="Isolated public navigation"))
        scope = store.create(
            ScopePolicy(
                engagement_id=project.id,
                allowed_domains=[
                    "google.com",
                    "*.google.com",
                    "gstatic.com",
                    "*.gstatic.com",
                ],
            )
        )
        store.update(Engagement, project.id, {"scope_policy_id": scope.id})
        token = secrets.token_urlsafe(32)
        app = create_app(store, auth_token=token)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    await asyncio.sleep(0.05)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                timeout=40,
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                opened = await client.post(
                    f"/api/v1/engagements/{project.id}/browser-companion"
                )
                opened.raise_for_status()
                session = opened.json()
                endpoint = f"/api/v1/browser-companion/{session['session_id']}"
                result = await client.post(
                    endpoint + "/operations",
                    json={
                        "operation": "navigate",
                        "tab_id": session["active_tab_id"],
                        "url": "https://google.com/",
                    },
                )
                result.raise_for_status()
                assert result.json()["page_revision"]
                encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
                url = f"ws://127.0.0.1:{port}{endpoint}/tabs/{session['active_tab_id']}/stream"
                for _ in range(2):
                    async with connect(
                        url,
                        subprotocols=["nebula.browser.v1", "nebula.auth." + encoded],
                        max_size=8 * 1024 * 1024,
                    ) as stream:
                        async with asyncio.timeout(15):
                            frame = json.loads(await stream.recv())
                            assert (
                                frame.get("data")
                                or frame.get("image")
                                or frame.get("type")
                            )
                print(
                    json.dumps(
                        {
                            "state": "passed",
                            "navigation": "https://google.com/",
                            "stream_and_reconnect": True,
                        }
                    )
                )
        finally:
            server.should_exit = True
            await task
            listener.close()


if __name__ == "__main__":
    asyncio.run(main())
