"""Drive the extracted production desktop with native WebDriver in an isolated profile.

Run under xvfb-run when there is no host display. No package installation occurs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import shlex
import sqlite3
import tempfile

import httpx

from smoke_test_browserd import _respond_as_bounded_proxy


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def smoke(
    package_root: Path,
    driver: Path,
    native_driver: Path,
    evidence_root: Path,
    harness_source_db: Path | None = None,
    codex_home: Path | None = None,
    width: int = 1440,
    height: int = 900,
    stabilization: bool = False,
    research_suite: bool = False,
    resume_recovery_only: bool = False,
) -> dict[str, object]:
    evidence_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nebula-packaged-profile-") as temporary:
        profile = Path(temporary)
        environment = {
            **os.environ,
            "APPDIR": str(package_root),
            "XDG_DATA_HOME": str(profile / "data"),
            "XDG_CONFIG_HOME": str(profile / "config"),
            "XDG_CACHE_HOME": str(profile / "cache"),
        }
        port, native_port = free_port(), free_port()
        with (evidence_root / "packaged-webdriver.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                str(driver),
                "--port",
                str(port),
                "--native-port",
                str(native_port),
                "--native-driver",
                str(native_driver),
                env=environment,
                stdout=log,
                stderr=log,
            )
            session_id = None
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=120
            ) as webdriver:
                try:
                    for _ in range(100):
                        if process.returncode is not None:
                            raise RuntimeError(
                                "The isolated desktop driver exited before startup."
                            )
                        try:
                            await webdriver.get("/status")
                            break
                        except httpx.ConnectError:
                            await asyncio.sleep(0.1)
                    else:
                        raise RuntimeError("The isolated desktop driver did not start.")
                    result = await webdriver.post(
                        "/session",
                        json={
                            "capabilities": {
                                "alwaysMatch": {
                                    "tauri:options": {
                                        "application": str(
                                            package_root / "usr/bin/nebula-ui"
                                        )
                                    }
                                }
                            }
                        },
                    )
                    result.raise_for_status()
                    value = result.json()["value"]
                    if "sessionId" not in value:
                        raise RuntimeError(f"Desktop session creation failed: {value}")
                    session_id = value["sessionId"]
                    prefix = f"/session/{session_id}"
                    print(
                        "Packaged desktop: native WebDriver session opened", flush=True
                    )
                    for _ in range(60):
                        ready = await webdriver.post(
                            prefix + "/execute/sync",
                            json={
                                "script": "return Boolean(window.__TAURI_INTERNALS__ && document.querySelector('main'));",
                                "args": [],
                            },
                        )
                        ready.raise_for_status()
                        if ready.json().get("value"):
                            break
                        await asyncio.sleep(0.5)
                    else:
                        raise RuntimeError(
                            "The packaged desktop interface did not render."
                        )
                    requested_rect = {"x": 0, "y": 0, "width": width, "height": height}
                    for _ in range(3):
                        resized = await webdriver.post(
                            prefix + "/window/rect", json=requested_rect
                        )
                        resized.raise_for_status()
                        actual = await webdriver.post(
                            prefix + "/execute/sync",
                            json={
                                "script": "return {width: innerWidth, height: innerHeight};",
                                "args": [],
                            },
                        )
                        actual.raise_for_status()
                        viewport = actual.json()["value"]
                        if viewport == {"width": width, "height": height}:
                            break
                        requested_rect["width"] += width - viewport["width"]
                        requested_rect["height"] += height - viewport["height"]
                    else:
                        raise RuntimeError(
                            f"Packaged viewport did not reach {width}x{height}: {viewport}"
                        )
                    backend_response = await webdriver.post(
                        prefix + "/execute/async",
                        json={
                            "script": "const done = arguments[arguments.length - 1]; window.__TAURI_INTERNALS__.invoke('resolve_backend_connection').then(done).catch(error => done({error: String(error)}));",
                            "args": [],
                        },
                    )
                    backend_response.raise_for_status()
                    backend = backend_response.json()["value"]
                    if "error" in backend:
                        raise RuntimeError(
                            "The packaged Core could not start: " + backend["error"]
                        )
                    assert backend["source"] == "local"
                    async with httpx.AsyncClient(
                        base_url=backend["endpoint"].rstrip("/") + "/",
                        headers={"Authorization": "Bearer " + backend["token"]},
                    ) as core:
                        health = await core.get("health")
                        health.raise_for_status()
                        project_response = await core.post(
                            "engagements", json={"name": "Packaged browser validation"}
                        )
                        project_response.raise_for_status()
                        project = project_response.json()
                        scope_response = await core.patch(
                            "scope-policies/" + project["scope_policy_id"],
                            json={"changes": {"allowed_cidrs": ["127.0.0.1/32"]}},
                        )
                        scope_response.raise_for_status()
                        conversation_id = None
                        if harness_source_db is not None:
                            from nebula.v3.domain import (
                                HarnessProfile,
                                HarnessNativeCapabilities,
                            )

                            with sqlite3.connect(
                                f"file:{harness_source_db}?mode=ro", uri=True
                            ) as source:
                                harness_payload = next(
                                    json.loads(row[0])
                                    for row in source.execute(
                                        "SELECT payload FROM entities WHERE kind='harnesses'"
                                    )
                                    if json.loads(row[0]).get("kind")
                                    == "codex_app_server"
                                )
                                harness = HarnessProfile.model_validate(
                                    {
                                        key: value
                                        for key, value in harness_payload.items()
                                        if key in HarnessProfile.model_fields
                                    }
                                )
                            harness.native_capabilities = HarnessNativeCapabilities()
                            if codex_home is not None:
                                wrapper = profile / "codex-validation"
                                wrapper.write_text(
                                    "#!/bin/sh\nexec env "
                                    + shlex.quote(
                                        "CODEX_HOME=" + str(codex_home.resolve())
                                    )
                                    + " "
                                    + shlex.quote(harness.executable or "")
                                    + ' "$@"\n'
                                )
                                wrapper.chmod(0o700)
                                harness.executable = str(wrapper)
                            created = await core.post(
                                "harnesses",
                                json=harness.model_dump(
                                    mode="json",
                                    exclude={
                                        "id",
                                        "created_at",
                                        "updated_at",
                                        "revision",
                                    },
                                ),
                            )
                            created.raise_for_status()
                            harness_id = created.json()["id"]
                            (
                                await core.post(
                                    f"harnesses/{harness_id}/health", timeout=90
                                )
                            ).raise_for_status()
                            discovered = await core.get(f"harnesses/{harness_id}")
                            discovered.raise_for_status()
                            model = next(
                                item["model"]
                                for item in discovered.json()["capabilities"][
                                    "model_options"
                                ]
                                if item["image_input"]
                            )
                            seeded = await core.post(
                                "chat/completions",
                                timeout=180,
                                json={
                                    "backend": "harness",
                                    "engagement_id": project["id"],
                                    "harness_profile_id": harness_id,
                                    "model": model,
                                    "include_knowledge": False,
                                    "tools_enabled": True,
                                    "allow_cloud_tool_results": True,
                                    "messages": [
                                        {
                                            "role": "user",
                                            "content": "Reply Ready. Do not use tools.",
                                        }
                                    ],
                                },
                            )
                            seeded.raise_for_status()
                            conversation_id = seeded.json()["session_id"]
                    fixture = await asyncio.start_server(
                        _respond_as_bounded_proxy, "127.0.0.1", 0
                    )
                    try:
                        target_url = (
                            f"http://127.0.0.1:{fixture.sockets[0].getsockname()[1]}/"
                        )

                        async def execute(script: str, *args: object) -> object:
                            response = await webdriver.post(
                                prefix + "/execute/sync",
                                json={"script": script, "args": list(args)},
                            )
                            response.raise_for_status()
                            value = response.json()["value"]
                            if isinstance(value, dict) and "error" in value:
                                raise RuntimeError(str(value))
                            return value

                        async def wait_for(script: str, timeout: int = 60) -> None:
                            deadline = asyncio.get_running_loop().time() + timeout
                            while asyncio.get_running_loop().time() < deadline:
                                if await execute(script):
                                    return
                                await asyncio.sleep(0.5)
                            screenshot = await webdriver.get(prefix + "/screenshot")
                            if screenshot.is_success:
                                (
                                    evidence_root / "packaged-desktop-failure.png"
                                ).write_bytes(
                                    base64.b64decode(screenshot.json()["value"])
                                )
                            details = await execute(
                                "return document.querySelector('main')?.innerText;"
                            )
                            raise RuntimeError(
                                "Packaged UI condition timed out: "
                                + script
                                + " Visible state: "
                                + str(details)
                            )

                        async def element(selector: str) -> str:
                            response = await webdriver.post(
                                prefix + "/element",
                                json={"using": "css selector", "value": selector},
                            )
                            response.raise_for_status()
                            return response.json()["value"][
                                "element-6066-11e4-a52e-4f735466cecf"
                            ]

                        async def click(selector: str) -> None:
                            await execute(
                                "const e=document.querySelector(arguments[0]);e?.scrollIntoView({block:'nearest',inline:'center'});return Boolean(e);",
                                selector,
                            )
                            scroll = await execute(
                                "const e=document.querySelector(arguments[0]),p=e?.closest('.chat-composer');if(!p||getComputedStyle(p).overflowY!=='auto')return null;const r=e.getBoundingClientRect(),b=p.getBoundingClientRect();if(r.bottom<=b.bottom&&r.top>=b.top)return null;return {x:Math.round(b.right-8),y:Math.round((b.top+b.bottom)/2),deltaX:0,deltaY:Math.round(r.bottom>b.bottom?r.bottom-b.bottom+8:r.top-b.top-8)};",
                                selector,
                            )
                            if scroll:
                                scrolled = await webdriver.post(
                                    prefix + "/actions",
                                    json={
                                        "actions": [
                                            {
                                                "type": "wheel",
                                                "id": "composer-scroll",
                                                "actions": [
                                                    {
                                                        "type": "scroll",
                                                        "origin": "viewport",
                                                        "duration": 150,
                                                        **scroll,
                                                    }
                                                ],
                                            }
                                        ]
                                    },
                                )
                                if scrolled.is_error:
                                    raise RuntimeError(
                                        "Native composer scroll failed: "
                                        + scrolled.text
                                    )
                            identifier = await element(selector)
                            response = await webdriver.post(
                                prefix + f"/element/{identifier}/click", json={}
                            )
                            if response.is_error:
                                if response.json().get("value", {}).get("error") == "element not interactable":
                                    fallback = await execute(
                                        "const e=document.querySelector(arguments[0]);if(!e||!e.getClientRects().length)return false;e.click();return true;",
                                        selector,
                                    )
                                    if fallback:
                                        print(
                                            "Packaged desktop: WebKit coordinate fallback clicked "
                                            + selector,
                                            flush=True,
                                        )
                                        return
                                geometry = await execute(
                                    "let e=document.querySelector(arguments[0]);const out=[];while(e){const r=e.getBoundingClientRect(),s=getComputedStyle(e);out.push({tag:e.tagName,class:e.className,top:r.top,bottom:r.bottom,height:r.height,clientHeight:e.clientHeight,scrollHeight:e.scrollHeight,scrollTop:e.scrollTop,overflowY:s.overflowY});e=e.parentElement;}return out;",
                                    selector,
                                )
                                (
                                    evidence_root / "packaged-click-geometry.json"
                                ).write_text(json.dumps(geometry, indent=2))
                                screenshot = await webdriver.get(prefix + "/screenshot")
                                if screenshot.is_success:
                                    (
                                        evidence_root / "packaged-click-failure.png"
                                    ).write_bytes(
                                        base64.b64decode(screenshot.json()["value"])
                                    )
                                raise RuntimeError(
                                    f"Native click failed for {selector}: {response.text}"
                                )
                            response.raise_for_status()

                        await execute(
                            "location.href = '/projects/' + encodeURIComponent(arguments[0]) + '/workbench?view=browser' + (arguments[1] ? '&session=' + encodeURIComponent(arguments[1]) : ''); return true;",
                            project["id"],
                            conversation_id,
                        )
                        if research_suite:
                            await wait_for(
                                "return Boolean(document.querySelector('select[aria-label=\"Browser engine\"]'));",
                                120,
                            )
                            await execute(
                                "const e=document.querySelector('select[aria-label=\"Browser engine\"]');e.value='native';e.dispatchEvent(new Event('change',{bubbles:true}));return e.value;"
                            )
                            await wait_for(
                                "return Boolean(document.querySelector('.browser-start input[aria-label=\"Start browsing\"], .browser-toolbar #browser-address'));",
                                120,
                            )
                            for _ in range(3):
                                resized = await webdriver.post(
                                    prefix + "/window/rect", json=requested_rect
                                )
                                resized.raise_for_status()
                                actual = await webdriver.post(
                                    prefix + "/execute/sync",
                                    json={
                                        "script": "return {width: innerWidth, height: innerHeight};",
                                        "args": [],
                                    },
                                )
                                actual.raise_for_status()
                                viewport = actual.json()["value"]
                                if viewport == {"width": width, "height": height}:
                                    break
                                requested_rect["width"] += width - viewport["width"]
                                requested_rect["height"] += height - viewport["height"]
                            else:
                                raise RuntimeError(
                                    "Native browser viewport did not retain "
                                    f"{width}x{height}: {viewport}"
                                )
                            print(
                                "Packaged desktop: native security browser is visible",
                                flush=True,
                            )
                        else:
                            await wait_for(
                                "return Boolean(document.querySelector('.managed-browser-screen img'));",
                                120,
                            )
                            print(
                                "Packaged desktop: managed Chromium is visible",
                                flush=True,
                            )
                            assert await execute(
                                "return document.querySelector('[aria-label=\"Browser address\"]')?.readOnly && !document.querySelector('[aria-label=\"Go\"]');"
                            )
                        if stabilization:
                            from native_stabilization_journeys import exercise

                            async def relaunch():
                                nonlocal prefix, session_id, backend
                                (await webdriver.delete(prefix)).raise_for_status()
                                session_id = None
                                launched = await webdriver.post(
                                    "/session",
                                    json={
                                        "capabilities": {
                                            "alwaysMatch": {
                                                "tauri:options": {
                                                    "application": str(
                                                        package_root
                                                        / "usr/bin/nebula-ui"
                                                    )
                                                }
                                            }
                                        }
                                    },
                                )
                                launched.raise_for_status()
                                session_id = launched.json()["value"]["sessionId"]
                                prefix = f"/session/{session_id}"
                                await wait_for(
                                    "return Boolean(window.__TAURI_INTERNALS__ && document.querySelector('main'));"
                                )
                                (
                                    await webdriver.post(
                                        prefix + "/window/rect", json=requested_rect
                                    )
                                ).raise_for_status()
                                await wait_for(
                                    f"return innerWidth==={width} && innerHeight==={height};"
                                )
                                resolved = await webdriver.post(
                                    prefix + "/execute/async",
                                    json={
                                        "script": "const done=arguments[arguments.length-1];window.__TAURI_INTERNALS__.invoke('resolve_backend_connection').then(done).catch(e=>done({error:String(e)}));",
                                        "args": [],
                                    },
                                )
                                resolved.raise_for_status()
                                backend = resolved.json()["value"]
                                assert (
                                    "error" not in backend
                                    and backend["source"] == "local"
                                ), (
                                    "Isolated packaged Core did not reconnect after relaunch"
                                )
                                return prefix, backend

                            await exercise(
                                webdriver=webdriver,
                                prefix=prefix,
                                backend=backend,
                                profile=profile,
                                project=project,
                                evidence_root=evidence_root,
                                execute=execute,
                                wait_for=wait_for,
                                element=element,
                                click=click,
                                relaunch=relaunch,
                            )
                        if conversation_id:
                            if not await execute(
                                "return Boolean(document.querySelector('#browser-assistant-panel #analyst-message'));"
                            ):
                                await execute(
                                    "const button=[...document.querySelectorAll('button[aria-label=\"Assistant\"]')].find(item=>item.getClientRects().length);if(button)button.setAttribute('data-validation-assistant','true');return Boolean(button);"
                                )
                                await click("[data-validation-assistant]")
                                await wait_for(
                                    "return Boolean(document.querySelector('#browser-assistant-panel #analyst-message'));"
                                )

                            async def send_question(text: str) -> None:
                                composer = await element(
                                    "#browser-assistant-panel #analyst-message"
                                )
                                (
                                    await webdriver.post(
                                        prefix + f"/element/{composer}/value",
                                        json={"text": text},
                                    )
                                ).raise_for_status()
                                await click(
                                    '#browser-assistant-panel button[aria-label="Send message"]'
                                )
                                await wait_for(
                                    "return Boolean(document.querySelector('#browser-assistant-panel button[aria-label=\"Stop response\"]'));",
                                    30,
                                )

                            # Native element clicks target the same visible operator controls.
                            await execute(
                                "const button = document.querySelector('.managed-browser-toolbar button[aria-label=\"Resume assistant control\"]'); if (button) button.setAttribute('data-validation-resume','true'); return true;"
                            )
                            if await execute(
                                "return Boolean(document.querySelector('[data-validation-resume]'));"
                            ):
                                await click("[data-validation-resume]")
                            await send_question(
                                f"Use browser.companion only to open this local fixture {target_url}, capture fresh context, click Ready once, wait for my approval, and report its new label."
                            )
                            await wait_for(
                                "return Boolean(document.querySelector('[aria-label=\"Browser action approval\"]'));",
                                120,
                            )
                            await click('[aria-label="Browser action approval"] button')
                            await wait_for(
                                "return !document.querySelector('#browser-assistant-panel button[aria-label=\"Stop response\"]') && [...document.querySelectorAll('#browser-assistant-panel .chat-message.assistant')].at(-1)?.textContent.includes('Saved');",
                                180,
                            )
                            await execute(
                                "document.querySelector('.managed-browser-toolbar button[aria-label=\"Ask about page\"]').setAttribute('data-validation-capture','true'); return true;"
                            )
                            await click("[data-validation-capture]")
                            await wait_for(
                                "return document.querySelector('.managed-browser-capture pre')?.textContent === 'Saved';"
                            )
                            await click(".managed-browser-capture pre")
                            await click(
                                ".managed-browser-capture button.button.primary"
                            )
                            await wait_for(
                                "return Boolean(document.querySelector('[aria-label=\"Selected context pack\"]'));"
                            )
                            print(
                                "Packaged desktop: live answer, approved page action, and context attachment passed",
                                flush=True,
                            )
                        if research_suite:
                            async def fill(selector: str, value: str) -> None:
                                filled = await execute(
                                    "const e=document.querySelector(arguments[0]);if(!e)return false;const setter=Object.getOwnPropertyDescriptor(e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set;setter.call(e,arguments[1]);e.dispatchEvent(new Event('input',{bubbles:true}));return e.value===arguments[1];",
                                    selector,
                                    value,
                                )
                                if not filled:
                                    raise RuntimeError(
                                        f"Visible control could not be filled: {selector}"
                                    )

                            async def tag_button(
                                label: str, marker: str, *, exact: bool = True
                            ) -> str:
                                tagged = await execute(
                                    "const [label,marker,exact]=arguments;const e=[...document.querySelectorAll('button')].find(b=>b.getClientRects().length && (exact?b.textContent.trim()===label:b.textContent.includes(label)));if(!e)return false;e.setAttribute(marker,'true');return true;",
                                    label,
                                    marker,
                                    exact,
                                )
                                if not tagged:
                                    raise RuntimeError(
                                        f"Visible button was not found: {label}"
                                    )
                                return f"[{marker}]"

                            async def click_button(
                                label: str, marker: str, *, exact: bool = True
                            ) -> None:
                                await click(
                                    await tag_button(label, marker, exact=exact)
                                )

                            async def submit_address(url: str) -> None:
                                selector = (
                                    '.browser-start input[aria-label="Start browsing"]'
                                    if await execute(
                                        "return Boolean(document.querySelector('.browser-start input[aria-label=\"Start browsing\"]'));"
                                    )
                                    else ".browser-toolbar #browser-address"
                                )
                                await fill(selector, url)
                                submitted = await execute(
                                    "const e=document.querySelector(arguments[0]);if(!e?.form)return false;e.form.requestSubmit();return true;",
                                    selector,
                                )
                                if not submitted:
                                    raise RuntimeError("Browser address form was unavailable.")

                            await submit_address(target_url + "initial")
                            await wait_for(
                                "return document.querySelector('.browser-surface')?.classList.contains('is-live') && !document.querySelector('.browser-toolbar button[aria-label=\"Reload\"]')?.disabled;",
                                120,
                            )
                            # WebKitWebDriver reports the requested window height while a
                            # native child webview halves its CSS interaction coordinates
                            # under Xvfb. Keep every control visible and WebDriver-clicked by
                            # applying an explicit operator-visible browser zoom.
                            await execute(
                                "document.documentElement.style.zoom='0.5';return getComputedStyle(document.documentElement).zoom;"
                            )
                            await click('button[aria-label="Security research workbench"]')
                            await wait_for(
                                "return Boolean(document.querySelector('nav[aria-label=\"Security Browser tools\"]'));"
                            )
                            await click_button(
                                "Session", "data-validation-session", exact=False
                            )
                            await wait_for(
                                "return Boolean(document.querySelector('.browser-session-workbench'));"
                            )
                            await click_button(
                                "I trust the CA · enable capture proxy",
                                "data-validation-enable-proxy",
                            )
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('[role=\"dialog\"] button')].find(b=>b.textContent.includes('CA is trusted')));"
                            )
                            await click_button(
                                "CA is trusted · enable",
                                "data-validation-confirm-proxy",
                            )
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Enable interception'));",
                                120,
                            )
                            if await execute(
                                "return Boolean(document.querySelector('.browser-start input[aria-label=\"Start browsing\"]'));"
                            ):
                                await submit_address(target_url + "proxied")
                                await wait_for(
                                    "return document.querySelector('.browser-surface')?.classList.contains('is-live');",
                                    120,
                                )
                            await click_button(
                                "Enable interception",
                                "data-validation-enable-interception",
                            )
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('[role=\"dialog\"] button')].find(b=>b.textContent.trim()==='Enable interception'));"
                            )
                            await execute(
                                "const b=[...document.querySelectorAll('[role=\"dialog\"] button')].find(b=>b.textContent.trim()==='Enable interception');b.setAttribute('data-validation-confirm-interception','true');return true;"
                            )
                            await click("[data-validation-confirm-interception]")
                            await wait_for(
                                "return document.body.innerText.includes('Interception enabled. In-scope requests now pause durably');",
                                120,
                            )
                            await click_button(
                                "Intercept", "data-validation-intercept"
                            )
                            if resume_recovery_only:
                                await submit_address(target_url + "resume-pending")
                                await wait_for(
                                    "return document.querySelector('#browser-address')?.value.includes('/resume-pending');",
                                    120,
                                )
                                await click_button(
                                    "Resume requests",
                                    "data-validation-disable-interception",
                                )
                                await wait_for(
                                    "return document.body.innerText.includes('Interception disabled. New requests pass through');",
                                    120,
                                )
                                await wait_for(
                                    "return document.body.innerText.includes('Paused requests (0)') && document.querySelector('#browser-address')?.value.includes('/resume-pending') && !document.querySelector('.browser-toolbar button[aria-label=\"Reload\"]')?.disabled;",
                                    120,
                                )
                                screenshot = await webdriver.get(prefix + "/screenshot")
                                screenshot.raise_for_status()
                                (
                                    evidence_root
                                    / "packaged-resume-pending-recovery.png"
                                ).write_bytes(
                                    base64.b64decode(screenshot.json()["value"])
                                )
                                print(
                                    "Packaged desktop: Resume forwarded the active native request",
                                    flush=True,
                                )
                                return {
                                    "state": "passed",
                                    "package_root": str(package_root),
                                    "profile_isolated": True,
                                    "viewport": viewport,
                                    "native_security_browser_visible": True,
                                    "native_resume_pending_recovery": True,
                                    "workflow_limit": "Focused regression gate; remaining research controls are separate selected journeys.",
                                }
                            await submit_address(target_url + "forward-original")
                            await wait_for(
                                "return document.body.innerText.includes('Paused requests (1)');",
                                120,
                            )
                            await click_button(
                                "Copy to Repeater",
                                "data-validation-copy-repeater",
                            )
                            await click_button(
                                "Intercept", "data-validation-intercept-return"
                            )
                            await execute(
                                "const s=[...document.querySelectorAll('summary')].find(e=>e.textContent.includes('Edit request before forwarding'));s.parentElement.open=true;return true;"
                            )
                            await fill(
                                '.browser-suite-form input[name="url"]',
                                target_url + "forward-edited",
                            )
                            await click_button(
                                "Forward edited request",
                                "data-validation-forward-edited",
                            )
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('.browser-suite-list li')].find(e=>e.textContent.includes('response') && e.textContent.includes('paused')));",
                                120,
                            )
                            await click_button(
                                "Forward response",
                                "data-validation-forward-response",
                            )
                            await submit_address(target_url + "drop")
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('.browser-suite-list li')].find(e=>e.textContent.includes('/drop') && e.textContent.includes('paused')));",
                                120,
                            )
                            await click_button("Drop", "data-validation-drop")
                            await wait_for(
                                "return document.body.innerText.includes('Paused requests (0)');",
                                120,
                            )
                            await submit_address(target_url + "resume-pending")
                            await wait_for(
                                "return document.body.innerText.includes('Paused requests (1)');",
                                120,
                            )
                            await click_button(
                                "Resume requests",
                                "data-validation-disable-interception",
                            )
                            await wait_for(
                                "return document.body.innerText.includes('Interception disabled. New requests pass through');",
                                120,
                            )
                            await wait_for(
                                "return document.body.innerText.includes('Paused requests (0)') && document.querySelector('#browser-address')?.value.includes('/resume-pending') && !document.querySelector('.browser-toolbar button[aria-label=\"Reload\"]')?.disabled;",
                                120,
                            )
                            await click_button(
                                "Repeater", "data-validation-repeater"
                            )
                            await wait_for(
                                "return Boolean(document.querySelector('#browser-repeater-heading'));"
                            )
                            await click_button("Send", "data-validation-repeater-send")
                            await wait_for(
                                "return document.body.innerText.includes('Result history (1)') && Boolean(document.querySelector('.repeater-response-meta'));",
                                120,
                            )
                            if await execute(
                                "return Boolean([...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Preview redacted body'));"
                            ):
                                await click_button(
                                    "Preview redacted body",
                                    "data-validation-preview-body",
                                )
                                await wait_for(
                                    "return Boolean(document.querySelector('.browser-result-body pre'));"
                                )
                            await click_button(
                                "Automate Intruder",
                                "data-validation-intruder",
                                exact=False,
                            )
                            await wait_for(
                                "return Boolean(document.querySelector('#browser-intruder-heading'));"
                            )
                            await fill(
                                '.browser-suite-form label:nth-of-type(1) input',
                                "Live bounded attack",
                            )
                            await fill(
                                '.browser-suite-form label:nth-of-type(4) input', "id"
                            )
                            await fill(
                                '.browser-suite-form label:nth-of-type(5) input',
                                target_url + "intruder?id=§id§",
                            )
                            await fill(
                                '.browser-suite-form label:nth-of-type(8) textarea',
                                "one\ntwo\nthree\nfour\nfive\nsix",
                            )
                            await click_button(
                                "Save attack draft",
                                "data-validation-save-attack",
                            )
                            await wait_for(
                                "return document.body.innerText.includes('Intruder attack saved as a draft');"
                            )
                            await click_button(
                                "Queue on desktop", "data-validation-queue-attack"
                            )
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Pause'));",
                                120,
                            )
                            await click_button("Pause", "data-validation-pause-attack")
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Resume'));",
                                120,
                            )
                            await click_button("Resume", "data-validation-resume-attack")
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Cancel'));",
                                120,
                            )
                            await click_button("Cancel", "data-validation-cancel-attack")
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Retry remaining'));",
                                120,
                            )
                            await click_button(
                                "Retry remaining", "data-validation-retry-attack"
                            )
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('.browser-action-status')].find(e=>e.textContent.trim()==='complete')) && document.body.innerText.includes('Results (6)');",
                                180,
                            )
                            await click_button(
                                "Pause requests",
                                "data-validation-enable-before-reload",
                            )
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('[role=\"dialog\"] button')].find(b=>b.textContent.trim()==='Enable interception'));"
                            )
                            await execute(
                                "const b=[...document.querySelectorAll('[role=\"dialog\"] button')].find(b=>b.textContent.trim()==='Enable interception');b.setAttribute('data-validation-confirm-before-reload','true');return true;"
                            )
                            await click("[data-validation-confirm-before-reload]")
                            await wait_for(
                                "return document.body.innerText.includes('Interception enabled. In-scope requests now pause durably');",
                                120,
                            )
                            await execute("location.reload();return true;")
                            await wait_for(
                                "return Boolean(window.__TAURI_INTERNALS__ && document.querySelector('main'));",
                                120,
                            )
                            await wait_for(
                                "return Boolean(document.querySelector('select[aria-label=\"Browser engine\"]'));",
                                120,
                            )
                            await execute(
                                "const e=document.querySelector('select[aria-label=\"Browser engine\"]');e.value='native';e.dispatchEvent(new Event('change',{bubbles:true}));return true;"
                            )
                            await wait_for(
                                "return Boolean(document.querySelector('button[aria-label=\"Security research workbench\"]'));",
                                120,
                            )
                            await execute(
                                "document.documentElement.style.zoom='0.5';return true;"
                            )
                            await submit_address(target_url + "reconnect")
                            await click('button[aria-label="Security research workbench"]')
                            await click_button(
                                "Intercept", "data-validation-intercept-reload"
                            )
                            await wait_for(
                                "return document.body.innerText.includes('Paused requests (1)');",
                                120,
                            )
                            await click_button(
                                "Forward request",
                                "data-validation-forward-reconnect-request",
                            )
                            await wait_for(
                                "return Boolean([...document.querySelectorAll('.browser-suite-list li')].find(e=>e.textContent.includes('response') && e.textContent.includes('paused')));",
                                120,
                            )
                            await click_button(
                                "Forward response",
                                "data-validation-forward-reconnect-response",
                            )
                            await click_button(
                                "Repeater", "data-validation-repeater-reload"
                            )
                            await execute(
                                "const b=[...document.querySelectorAll('button')].find(e=>e.textContent.includes('/forward-original'));if(!b)return false;b.setAttribute('data-validation-select-repeater-reload','true');return true;"
                            )
                            await click("[data-validation-select-repeater-reload]")
                            await wait_for(
                                "return document.body.innerText.includes('forward-original') && document.body.innerText.includes('Result history (1)');",
                                120,
                            )
                            await click_button(
                                "Automate Intruder",
                                "data-validation-intruder-reload",
                                exact=False,
                            )
                            await wait_for(
                                "return document.body.innerText.includes('Live bounded attack') && document.body.innerText.includes('Results (6)');",
                                120,
                            )
                            print(
                                "Packaged desktop: live Intercept, Repeater, Intruder, and durable reload controls passed",
                                flush=True,
                            )
                    finally:
                        fixture.close()
                        await fixture.wait_closed()
                    screenshot = await webdriver.get(prefix + "/screenshot")
                    screenshot.raise_for_status()
                    (evidence_root / "packaged-desktop-boot.png").write_bytes(
                        base64.b64decode(screenshot.json()["value"])
                    )
                    return {
                        "state": "passed",
                        "packaged_desktop_boot": True,
                        "supervised_packaged_core": True,
                        "package_root": str(package_root),
                        "profile_isolated": True,
                        "viewport": viewport,
                        "managed_chromium_visible": not research_suite,
                        "native_security_browser_visible": research_suite,
                        "read_only_page_observation_and_context_attachment": bool(
                            harness_source_db
                        ),
                        "live_codex_inline_approval": bool(conversation_id),
                        "inert_packaged_approval_journeys": stabilization,
                        "live_security_research_controls": research_suite,
                        "workflow_limit": "Physical input and full lifecycle matrix remain separate gates.",
                    }
                finally:
                    if session_id is not None:
                        with suppress(httpx.HTTPError):
                            await webdriver.delete(f"/session/{session_id}")
                    if process.returncode is None:
                        process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 10)
                        except TimeoutError:
                            process.kill()
                            await process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--native-driver", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--harness-source-db", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument(
        "--stabilization",
        action="store_true",
        help="Exercise approvals using an inert local ACP peer, no model calls or tools",
    )
    parser.add_argument(
        "--research-suite",
        action="store_true",
        help="Exercise live native Intercept, Repeater, Intruder, and reload controls",
    )
    parser.add_argument(
        "--resume-recovery-only",
        action="store_true",
        help="Stop after proving Resume forwards an actively paused native request",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                smoke(
                    args.package_root,
                    args.driver,
                    args.native_driver,
                    args.evidence_root,
                    args.harness_source_db,
                    args.codex_home,
                    args.width,
                    args.height,
                    args.stabilization,
                    args.research_suite,
                    args.resume_recovery_only,
                )
            ),
            sort_keys=True,
        )
    )
