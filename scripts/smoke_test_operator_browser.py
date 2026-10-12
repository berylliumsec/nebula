"""Focused production UI journey through real Core, browserd, and Chromium.

Run with a staged runtime under a display, for example:
  PYTHONPATH=src xvfb-run -a python -m scripts.smoke_test_operator_browser \
    --runtime-root ui/src-tauri/resources/playwright-browsers \
    --origin-host 192.168.1.155 --evidence /tmp/operator-browser.png
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile

import httpx
from playwright.async_api import async_playwright, expect
import uvicorn

from scripts.smoke_test_browserd import _respond_as_bounded_proxy


async def _server(app, host: str = "127.0.0.1"):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, 0))
    listener.listen(128)
    listener.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            return server, task, listener.getsockname()[1]
        if task.done():
            await task
            raise RuntimeError("The test server exited before it was ready.")
        await asyncio.sleep(0.05)
    raise RuntimeError("The test server did not become ready.")


async def smoke(runtime_root: Path, origin_host: str, evidence: Path) -> None:
    from nebula.v3.api import create_app
    from nebula.v3.browserd import (
        BrowserdManager,
        BrowserdSettings,
        create_browserd_app,
    )
    from nebula.v3.domain import BrowserSession, Engagement, ScopePolicy
    from nebula.v3.storage import NebulaStore

    if (
        not Path(create_app.__code__.co_filename)
        .resolve()
        .is_relative_to(Path(__file__).resolve().parents[1] / "src")
    ):
        raise RuntimeError(
            "The smoke run imported another checkout. Run with PYTHONPATH=src."
        )

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime_root)
    proxy = await asyncio.start_server(_respond_as_bounded_proxy, "127.0.0.1", 0)
    proxy_port = proxy.sockets[0].getsockname()[1]
    browser_server = core_server = None
    browser_task = core_task = None
    old_url, old_token = (
        os.environ.get("NEBULA_BROWSERD_URL"),
        os.environ.get("NEBULA_BROWSERD_TOKEN"),
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="nebula-operator-browser-"
        ) as directory:
            root = Path(directory)
            settings = BrowserdSettings(
                token=secrets.token_urlsafe(32),
                profile_root=root / "profiles",
                policy_proxy_url=f"http://127.0.0.1:{proxy_port}",
                runtime_root=runtime_root,
            )
            manager = BrowserdManager(settings)
            browser_server, browser_task, browser_port = await _server(
                create_browserd_app(settings, manager=manager)
            )
            os.environ["NEBULA_BROWSERD_URL"] = f"http://127.0.0.1:{browser_port}"
            os.environ["NEBULA_BROWSERD_TOKEN"] = settings.token
            store = NebulaStore(root / "core.db")
            project = store.create(Engagement(name="Operator browser fixture"))
            scope = store.create(
                ScopePolicy(
                    engagement_id=project.id,
                    allowed_domains=["browserd-smoke.example.test"],
                )
            )
            store.update(Engagement, project.id, {"scope_policy_id": scope.id})
            token = secrets.token_urlsafe(32)
            dist = Path(__file__).resolve().parents[1] / "ui" / "dist"
            if not (dist / "index.html").is_file():
                raise RuntimeError(
                    "Build the production UI first: npm --prefix ui run build"
                )
            core_server, core_task, core_port = await _server(
                create_app(
                    store,
                    auth_token=token,
                    static_dir=dist,
                    allow_insecure_device_pairing=True,
                ),
                host="0.0.0.0",
            )
            origin = f"http://{origin_host}:{core_port}"
            local_core = f"http://127.0.0.1:{core_port}"
            target_url = "http://browserd-smoke.example.test/"
            # Pairings are intentionally issued only to loopback callers. The
            # paired UI itself loads from the requested LAN origin below.
            async with httpx.AsyncClient(base_url=local_core, timeout=30) as client:
                client.headers["Authorization"] = f"Bearer {token}"
                pairing = (
                    (
                        await client.post(
                            "/api/v1/auth/pairings",
                            json={"name": "Operator browser smoke"},
                        )
                    )
                    .raise_for_status()
                    .json()
                )
                prepared = await client.post(
                    f"/api/v1/engagements/{project.id}/browser-companion?mode=operator"
                )
                prepared.raise_for_status()
                probe = await client.post(
                    f"/api/v1/browser-companion/{prepared.json()['session_id']}/operator-operations",
                    json={"operation": "tabs"},
                )
                assert probe.raise_for_status().json()["tabs"]
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=False)
                    context = await browser.new_context(
                        viewport={"width": 1440, "height": 900}, reduced_motion="reduce"
                    )
                    page = await context.new_page()
                    page.set_default_timeout(30000)
                    page.on(
                        "response",
                        lambda response: (
                            print(
                                f"Browser API refusal: {response.status} {response.request.method} {response.url}",
                                flush=True,
                            )
                            if response.status >= 400
                            and "browser-companion" in response.url
                            else None
                        ),
                    )
                    try:
                        await page.goto(
                            f"{origin}/#pair={pairing['secret']}&code={pairing['confirmation_code']}"
                        )
                        await page.get_by_label("Device name").fill(
                            "Operator browser smoke"
                        )
                        await page.get_by_role(
                            "button", name="Pair device", exact=True
                        ).click()
                        await expect(
                            page.get_by_role(
                                "button",
                                name=re.compile("Nebula Core (ready|degraded)"),
                            )
                        ).to_be_visible(timeout=30000)
                        await page.goto(
                            f"{origin}/projects/{project.id}/workbench?view=browser&browserEngine=operator"
                        )
                        panel = page.get_by_role(
                            "region", name="Interactive browser", exact=True
                        )
                        image = panel.get_by_role(
                            "img",
                            name="Live Chromium page. Click or focus to interact.",
                        )
                        address = panel.get_by_role("textbox", name="Browser address")
                        await expect(image).to_be_visible(timeout=30000)
                        await address.fill(target_url)
                        await panel.get_by_role("button", name="Go", exact=True).click()
                        await expect(address).to_have_value(target_url)
                        opened = (
                            (
                                await client.post(
                                    f"/api/v1/engagements/{project.id}/browser-companion?mode=operator"
                                )
                            )
                            .raise_for_status()
                            .json()
                        )
                        session_id, tab_id = (
                            opened["session_id"],
                            opened["active_tab_id"],
                        )
                        base = f"/api/v1/browser-companion/{session_id}/operator-operations"

                        async def capture():
                            return (
                                (
                                    await client.post(
                                        base,
                                        json={"operation": "capture", "tab_id": tab_id},
                                    )
                                )
                                .raise_for_status()
                                .json()
                            )

                        assert "Ready" in (await capture())["text"]
                        identity_id = store.get(BrowserSession, session_id).identity_id
                        live_page = await manager.page_for_screencast(
                            identity_id, tab_id
                        )

                        async def click_host(selector: str):
                            box = await live_page.locator(selector).bounding_box()
                            assert box is not None
                            await image.scroll_into_view_if_needed()
                            await image.click(
                                position=await image.evaluate(
                                    "(img, box) => ({x:(box.x+box.width/2)*img.clientWidth/img.naturalWidth, y:(box.y+box.height/2)*img.clientHeight/img.naturalHeight})",
                                    box,
                                )
                            )

                        await click_host("button")
                        await expect(live_page.locator("button")).to_have_text("Saved")
                        await click_host("input[type=password]")
                        await image.press("f")
                        await expect(live_page.get_by_label("Password")).to_have_value(
                            "f"
                        )
                        bounds = await panel.evaluate(
                            "el => { const a=el.querySelector('input[aria-label=\"Browser address\"]').getBoundingClientRect(), i=el.querySelector('img').getBoundingClientRect(), s=el.querySelector('.operator-browser-screen').getBoundingClientRect(); return {addressBottom:a.bottom, imageTop:i.top, imageBottom:i.bottom, screenTop:s.top, screenBottom:s.bottom, imageLeft:i.left, imageRight:i.right, screenLeft:s.left, screenRight:s.right}; }"
                        )
                        assert bounds["addressBottom"] <= bounds["screenTop"]
                        assert (
                            bounds["screenTop"]
                            <= bounds["imageTop"]
                            <= bounds["imageBottom"]
                            <= bounds["screenBottom"]
                        )
                        assert (
                            bounds["screenLeft"]
                            <= bounds["imageLeft"]
                            <= bounds["imageRight"]
                            <= bounds["screenRight"]
                        )
                        await page.reload()
                        await expect(image).to_be_visible(timeout=30000)
                        resumed = (
                            (
                                await client.post(
                                    f"/api/v1/engagements/{project.id}/browser-companion?mode=operator"
                                )
                            )
                            .raise_for_status()
                            .json()
                        )
                        assert (
                            resumed["session_id"] == session_id
                            and resumed["active_tab_id"] == tab_id
                        )
                        await panel.get_by_role(
                            "button", name="Reconnect browser"
                        ).click()
                        await expect(image).to_be_visible(timeout=30000)
                        await expect(
                            panel.get_by_role("button", name="Go", exact=True)
                        ).to_be_enabled()
                        refused = await client.post(
                            base,
                            json={
                                "operation": "navigate",
                                "tab_id": tab_id,
                                "url": "https://outside.example/",
                            },
                        )
                        assert refused.status_code == 409, refused.text
                        await address.fill("outside.example")
                        await panel.get_by_role("button", name="Go", exact=True).click()
                        await expect(panel.get_by_role("alert")).to_contain_text(
                            "scope"
                        )
                        assert "Saved" in (await capture())["text"]
                        await page.get_by_role(
                            "button", name="Assistant", exact=True
                        ).click()
                        await expect(address).to_be_visible()
                        await expect(image).to_be_visible()
                        evidence.parent.mkdir(parents=True, exist_ok=True)
                        await page.screenshot(path=str(evidence))
                        print(
                            f"operator browser production journey passed: origin={origin} evidence={evidence}",
                            flush=True,
                        )
                    except Exception:
                        with suppress(Exception):
                            evidence.parent.mkdir(parents=True, exist_ok=True)
                            await page.screenshot(
                                path=str(
                                    evidence.with_name(evidence.stem + "-failure.png")
                                )
                            )
                        raise
                    finally:
                        await browser.close()
    finally:
        if core_server:
            core_server.should_exit = True
            await asyncio.wait_for(core_task, 15)
        if browser_server:
            browser_server.should_exit = True
            await asyncio.wait_for(browser_task, 15)
        proxy.close()
        await proxy.wait_closed()
        if old_url is None:
            os.environ.pop("NEBULA_BROWSERD_URL", None)
        else:
            os.environ["NEBULA_BROWSERD_URL"] = old_url
        if old_token is None:
            os.environ.pop("NEBULA_BROWSERD_TOKEN", None)
        else:
            os.environ["NEBULA_BROWSERD_TOKEN"] = old_token


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--origin-host", default="127.0.0.1")
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(
        smoke(
            arguments.runtime_root.resolve(), arguments.origin_host, arguments.evidence
        )
    )
