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
                                harness = next(
                                    HarnessProfile.model_validate_json(row[0])
                                    for row in source.execute(
                                        "SELECT payload FROM entities WHERE kind='harnesses'"
                                    )
                                    if json.loads(row[0])["kind"] == "codex_app_server"
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
                                "return document.querySelector('.managed-assistant-browser')?.innerText;"
                            )
                            raise RuntimeError(
                                "Packaged UI condition timed out: "
                                + script
                                + " Browser state: "
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
                            identifier = await element(selector)
                            response = await webdriver.post(
                                prefix + f"/element/{identifier}/click", json={}
                            )
                            response.raise_for_status()

                        await execute(
                            "location.href = '/projects/' + encodeURIComponent(arguments[0]) + '/workbench?view=browser' + (arguments[1] ? '&session=' + encodeURIComponent(arguments[1]) : ''); return true;",
                            project["id"],
                            conversation_id,
                        )
                        await wait_for(
                            "return Boolean(document.querySelector('.managed-browser-screen img'));",
                            120,
                        )
                        print(
                            "Packaged desktop: managed Chromium is visible", flush=True
                        )
                        address = await element('[aria-label="Browser address"]')
                        (
                            await webdriver.post(
                                prefix + f"/element/{address}/clear", json={}
                            )
                        ).raise_for_status()
                        (
                            await webdriver.post(
                                prefix + f"/element/{address}/value",
                                json={"text": target_url},
                            )
                        ).raise_for_status()
                        await click(
                            '.managed-browser-toolbar button[type="submit"], .managed-browser-toolbar button.button.primary'
                        )
                        await wait_for(
                            "return document.querySelector('.managed-browser-capture pre')?.textContent === 'Ready';"
                        )
                        await click(".managed-browser-capture button.button.primary")
                        await wait_for(
                            "return Boolean(document.querySelector('[aria-label=\"Selected context pack\"]'));"
                        )
                        assert await execute(
                            "return Boolean(document.querySelector('[aria-label=\"Browser Assistant\"] #analyst-message'));"
                        )
                        print(
                            "Packaged desktop: page context attached beside the live page",
                            flush=True,
                        )
                        if conversation_id:

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

                            await send_question(
                                "Read the attached page context. What is the button label? Reply only with that label; do not use tools."
                            )
                            await wait_for(
                                "return !document.querySelector('#browser-assistant-panel button[aria-label=\"Stop response\"]') && [...document.querySelectorAll('#browser-assistant-panel .chat-message.assistant')].at(-1)?.textContent.includes('Ready');",
                                180,
                            )
                            # Native element clicks target the same visible operator controls.
                            await execute(
                                "const button = [...document.querySelectorAll('.managed-browser-toolbar button')].find(b => b.textContent === 'Resume assistant control'); if (button) button.setAttribute('data-validation-resume','true'); return true;"
                            )
                            if await execute(
                                "return Boolean(document.querySelector('[data-validation-resume]'));"
                            ):
                                await click("[data-validation-resume]")
                            await send_question(
                                "Use browser.companion only to capture fresh context, click Ready once, wait for my approval, and report its new label."
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
                                "[...document.querySelectorAll('.managed-browser-toolbar button')].find(b => b.textContent === 'Ask about page').setAttribute('data-validation-capture','true'); return true;"
                            )
                            await click("[data-validation-capture]")
                            await wait_for(
                                "return document.querySelector('.managed-browser-capture pre')?.textContent === 'Saved';"
                            )
                            await click(".managed-browser-capture pre")
                            print(
                                "Packaged desktop: live answer and approved page action passed",
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
                        "managed_chromium_visible": True,
                        "page_navigation_and_context_attachment": True,
                        "live_codex_inline_approval": bool(conversation_id),
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
                )
            ),
            sort_keys=True,
        )
    )
