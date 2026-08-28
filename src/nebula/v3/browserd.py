"""Desktop-owned, loopback-authenticated managed Chromium service.

This process owns live Playwright objects, local profiles, traces, and raw page
state. Core receives only normalized readiness and action receipts.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.websockets import WebSocketDisconnect
from playwright.async_api import async_playwright
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from .browser_engine import (
    BROWSER_ENGINE_CONTRACT_VERSION,
    BrowserEngineAction,
    BrowserEngineReceipt,
)
from .domain import BrowserEngineCapability, BrowserEngineState


_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


def _loopback_http_url(value: str) -> str:
    import ipaddress

    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL must use a literal loopback address") from exc
    if (
        parsed.scheme != "http"
        or not address.is_loopback
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "URL must be credential-free HTTP on a literal loopback address and explicit port"
        )
    return value.rstrip("/")


def _safe_page_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))


def _network_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("browser navigation requires an absolute HTTP(S) URL")
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("browser navigation contains an invalid port") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(
            "browser navigation requires a credential-free HTTP(S) URL without a fragment"
        )
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class BrowserdSettings:
    token: str
    profile_root: Path
    policy_proxy_url: str
    runtime_root: Path
    upload_root: Path | None = None
    headless: bool = False

    def __post_init__(self) -> None:
        if not self.token or any(character.isspace() for character in self.token):
            raise ValueError("browserd token must be opaque")
        object.__setattr__(
            self, "policy_proxy_url", _loopback_http_url(self.policy_proxy_url)
        )
        root = self.profile_root.expanduser().resolve(strict=False)
        runtime = self.runtime_root.expanduser().resolve(strict=False)
        object.__setattr__(self, "profile_root", root)
        object.__setattr__(self, "runtime_root", runtime)
        if self.upload_root is not None:
            object.__setattr__(
                self,
                "upload_root",
                self.upload_root.expanduser().resolve(strict=False),
            )


class BrowserdIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity_id: str = Field(pattern=_OPAQUE_ID.pattern)


class BrowserdIdentityReceipt(BaseModel):
    identity_id: str
    tab_ids: list[str]
    state: str = "ready"


class BrowserdLifecycleReceipt(BaseModel):
    assessment_id: str
    state: str


class ActionLedger:
    """Local action-token ledger storing hashes and redacted receipts only."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        self.path = path
        self._lock = asyncio.Lock()
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS browser_actions (
                    action_token TEXT PRIMARY KEY,
                    payload_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    receipt_json TEXT
                )
                """
            )
        os.chmod(path, 0o600)

    @staticmethod
    def payload_digest(action: BrowserEngineAction) -> str:
        payload = json.dumps(
            action.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def claim(self, action: BrowserEngineAction) -> BrowserEngineReceipt | None:
        digest = self.payload_digest(action)
        async with self._lock:
            with sqlite3.connect(self.path) as connection:
                row = connection.execute(
                    "SELECT payload_sha256, state, receipt_json FROM browser_actions WHERE action_token = ?",
                    (action.action_token,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO browser_actions(action_token, payload_sha256, state) VALUES (?, ?, 'executing')",
                        (action.action_token, digest),
                    )
                    return None
                if not hmac.compare_digest(row[0], digest):
                    raise ValueError("action token was reused with different arguments")
                if row[2]:
                    return BrowserEngineReceipt.model_validate_json(row[2])
                return BrowserEngineReceipt(
                    action_token=action.action_token,
                    state="ambiguous",
                    pre_fingerprint=action.pre_fingerprint,
                    failure_code="browserd_interrupted_action",
                    operator_message=(
                        "browserd restarted while this action was executing, so completion is ambiguous."
                    ),
                    recovery_action=(
                        "Inspect the live page and trace; do not replay this action automatically."
                    ),
                )

    async def finish(self, receipt: BrowserEngineReceipt) -> BrowserEngineReceipt:
        async with self._lock:
            with sqlite3.connect(self.path) as connection:
                result = connection.execute(
                    "UPDATE browser_actions SET state = ?, receipt_json = ? WHERE action_token = ?",
                    (
                        receipt.state,
                        receipt.model_dump_json(),
                        receipt.action_token,
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError("action token was not claimed")
        return receipt


class BrowserdManager:
    """Own Playwright, project identities, tabs, CDP sessions, and trace files."""

    def __init__(
        self,
        settings: BrowserdSettings,
        *,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self._playwright_factory = playwright_factory
        self._playwright: Any | None = None
        self._contexts: dict[str, Any] = {}
        self._tabs: dict[tuple[str, str], Any] = {}
        self._assessment_states: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._ledger = ActionLedger(settings.profile_root / "browserd-actions.sqlite3")
        self._capability: BrowserEngineCapability | None = None

    async def start(self) -> None:
        if self.settings.profile_root.is_symlink():
            raise RuntimeError("browserd profile root cannot be a symbolic link")
        self.settings.profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.settings.profile_root, 0o700)
        try:
            if self._playwright_factory is None:
                self._playwright_factory = async_playwright
            self._playwright = await self._playwright_factory().start()
            self._capability = self._verify_runtime()
        except Exception:
            self._capability = BrowserEngineCapability(
                adapter="managed-chromium",
                display_name="Managed Chromium",
                state=BrowserEngineState.UNAVAILABLE,
                unavailability_reason=(
                    "The verified full-Chromium Playwright runtime could not start."
                ),
                recovery_action="Prepare the managed browser runtime and restart browserd.",
            )

    def _verify_runtime(self) -> BrowserEngineCapability:
        playwright = self._playwright
        if playwright is None:
            raise RuntimeError("Playwright did not finish starting")
        manifest_path = self.settings.runtime_root / "nebula-playwright-runtime.json"
        if not manifest_path.is_file():
            raise RuntimeError("verified browser runtime manifest is missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("browser") != "chromium"
            or not isinstance(manifest.get("executables"), list)
            or not isinstance(manifest.get("executable_sha256"), dict)
            or not manifest.get("sbom")
            or not manifest.get("provenance")
        ):
            raise RuntimeError("browser runtime manifest is incomplete")
        executable = Path(playwright.chromium.executable_path).resolve()
        try:
            executable.relative_to(self.settings.runtime_root)
        except ValueError as exc:
            raise RuntimeError(
                "Playwright resolved Chromium outside the verified runtime"
            ) from exc
        relative = executable.relative_to(self.settings.runtime_root).as_posix()
        expected = manifest["executable_sha256"].get(relative)
        actual = _sha256_file(executable)
        if not expected or not hmac.compare_digest(expected, actual):
            raise RuntimeError("Chromium executable digest does not match the manifest")
        state = (
            BrowserEngineState.DEGRADED
            if self.settings.headless
            else BrowserEngineState.READY
        )
        return BrowserEngineCapability(
            adapter="managed-chromium",
            display_name="Managed Chromium",
            contract_version=BROWSER_ENGINE_CONTRACT_VERSION,
            state=state,
            installed_version=str(manifest.get("playwright_version") or "unknown"),
            digest=f"sha256:{actual}",
            actions=[
                "navigate",
                "click",
                "fill",
                "select",
                "press",
                "wait",
                "snapshot",
                "screenshot",
                "trace",
                "takeover",
                *(["upload"] if self.settings.upload_root is not None else []),
            ],
            protocols=["http", "https", "websocket", "cdp-screencast"],
            unavailability_reason=(
                "Headless mode is test-only and cannot satisfy packaged desktop acceptance."
                if self.settings.headless
                else None
            ),
            recovery_action=(
                "Restart browserd in headed desktop mode for production qualification."
                if self.settings.headless
                else None
            ),
        )

    async def close(self) -> None:
        for context in list(self._contexts.values()):
            try:
                await context.close()
            except Exception:
                pass
        self._contexts.clear()
        self._tabs.clear()
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass

    async def readiness(self) -> BrowserEngineCapability:
        return self._capability or BrowserEngineCapability(
            adapter="managed-chromium",
            display_name="Managed Chromium",
            state=BrowserEngineState.UNAVAILABLE,
            unavailability_reason="browserd has not completed startup verification.",
            recovery_action="Wait for browserd startup or restart the managed runtime.",
        )

    def _identity_path(self, identity_id: str) -> Path:
        if not _OPAQUE_ID.fullmatch(identity_id):
            raise ValueError("identity id is invalid")
        directory = (
            self.settings.profile_root
            / hashlib.sha256(identity_id.encode("utf-8")).hexdigest()
        )
        resolved = directory.resolve(strict=False)
        try:
            resolved.relative_to(self.settings.profile_root)
        except ValueError as exc:
            raise ValueError("identity profile escapes the profile root") from exc
        return resolved

    async def ensure_identity(self, identity_id: str) -> BrowserdIdentityReceipt:
        capability = await self.readiness()
        if capability.state == BrowserEngineState.UNAVAILABLE:
            raise RuntimeError("managed Chromium is unavailable")
        async with self._lock:
            context = self._contexts.get(identity_id)
            if context is None:
                playwright = self._playwright
                if playwright is None:
                    raise RuntimeError("managed Chromium is unavailable")
                profile = self._identity_path(identity_id)
                profile.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(profile, 0o700)
                context = await playwright.chromium.launch_persistent_context(
                    str(profile),
                    headless=self.settings.headless,
                    executable_path=playwright.chromium.executable_path,
                    proxy={"server": self.settings.policy_proxy_url},
                    args=["--proxy-bypass-list=<-loopback>"],
                    accept_downloads=True,
                )
                await context.tracing.start(
                    screenshots=True, snapshots=True, sources=False
                )
                self._contexts[identity_id] = context
                pages = list(context.pages) or [await context.new_page()]
                for page in pages:
                    tab_id = str(uuid4())
                    self._tabs[(identity_id, tab_id)] = page
            tab_ids = [tab_id for (owner, tab_id) in self._tabs if owner == identity_id]
        return BrowserdIdentityReceipt(identity_id=identity_id, tab_ids=tab_ids)

    async def _page(self, action: BrowserEngineAction) -> Any:
        await self.ensure_identity(action.identity_id)
        page = self._tabs.get((action.identity_id, action.tab_id))
        if page is not None:
            return page
        context = self._contexts[action.identity_id]
        page = list(context.pages)[0] if context.pages else await context.new_page()
        self._tabs[(action.identity_id, action.tab_id)] = page
        return page

    @staticmethod
    async def _fingerprint(page: Any) -> str:
        title, markup = await asyncio.gather(page.title(), page.content())
        digest = hashlib.sha256()
        digest.update(page.url.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(title.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(markup.encode("utf-8", errors="replace"))
        return f"sha256:{digest.hexdigest()}"

    @staticmethod
    def _locator(page: Any, values: dict[str, str]) -> Any:
        frame = values.get("frame")
        root = page.frame_locator(frame) if frame else page
        if role := values.get("role"):
            return root.get_by_role(role, name=values.get("name"))
        if label := values.get("label"):
            return root.get_by_label(label)
        if test_id := values.get("test_id"):
            return root.get_by_test_id(test_id)
        if text := values.get("text"):
            return root.get_by_text(text, exact=values.get("exact") == "true")
        if css := values.get("css"):
            return root.locator(css)
        raise ValueError("action requires a role, label, test_id, text, or CSS locator")

    @staticmethod
    def _credential(reference: str) -> str:
        if not reference or any(character.isspace() for character in reference):
            raise ValueError("credential reference is invalid")
        import keyring

        value = keyring.get_password("nebula.browser.credentials", reference)
        if value is None:
            raise ValueError("credential reference is unavailable in the OS vault")
        return value

    def _upload_path(self, value: object) -> Path:
        if self.settings.upload_root is None:
            raise ValueError("host uploads are not configured for browserd")
        if not isinstance(value, str) or not value:
            raise ValueError("upload requires one enumerated host file")
        candidate = Path(value).expanduser().resolve(strict=True)
        try:
            candidate.relative_to(self.settings.upload_root)
        except ValueError as exc:
            raise ValueError(
                "upload file is outside the configured host browser"
            ) from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError("upload target must be a regular non-symlink file")
        return candidate

    async def execute(self, action: BrowserEngineAction) -> BrowserEngineReceipt:
        prior = await self._ledger.claim(action)
        if prior is not None:
            return prior
        if self._assessment_states.get(action.assessment_id) in {"paused", "stopped"}:
            return await self._ledger.finish(
                BrowserEngineReceipt(
                    action_token=action.action_token,
                    state="cancelled",
                    failure_code="assessment_not_running",
                    operator_message="The assessment is paused or stopped.",
                    recovery_action="Resume the assessment or create a new action.",
                )
            )
        page: Any | None = None
        pre: str | None = None
        trace_id = str(uuid4())
        trace_started = False
        dispatched = False
        try:
            page = await self._page(action)
            if action.expected_page_url and _safe_page_url(page.url) != _safe_page_url(
                action.expected_page_url
            ):
                raise ValueError("page changed before the action could execute")
            pre = await self._fingerprint(page)
            if action.pre_fingerprint and not hmac.compare_digest(
                action.pre_fingerprint, pre
            ):
                raise ValueError("page fingerprint changed before action execution")
            context = self._contexts[action.identity_id]
            trace_dir = self._identity_path(action.identity_id) / "traces"
            trace_dir.mkdir(mode=0o700, exist_ok=True)
            await context.tracing.start_chunk(title=action.action_token)
            trace_started = True
            locator = None
            if action.kind == "navigate":
                target = _network_url(action.arguments.get("url"))
                dispatched = True
                await page.goto(
                    target,
                    wait_until=str(
                        action.arguments.get("wait_until", "domcontentloaded")
                    ),
                )
            elif action.kind == "wait":
                locator = self._locator(page, action.locator)
                await locator.wait_for(
                    state=str(action.arguments.get("state", "visible"))
                )
            elif action.kind == "snapshot":
                await page.locator("html").aria_snapshot()
            elif action.kind == "screenshot":
                screenshot_dir = self._identity_path(action.identity_id) / "screenshots"
                screenshot_dir.mkdir(mode=0o700, exist_ok=True)
                await page.screenshot(
                    path=str(screenshot_dir / f"{trace_id}.png"), full_page=True
                )
            else:
                locator = self._locator(page, action.locator)
                if action.kind == "click":
                    dispatched = True
                    await locator.click()
                elif action.kind == "fill":
                    credential_ref = action.arguments.get("credential_ref")
                    input_type = await locator.get_attribute("type")
                    if input_type == "password" and credential_ref is None:
                        raise ValueError(
                            "password fields require an OS-vault credential reference"
                        )
                    value = (
                        self._credential(str(credential_ref))
                        if credential_ref is not None
                        else str(action.arguments.get("value", ""))
                    )
                    dispatched = True
                    await locator.fill(value)
                elif action.kind == "select":
                    dispatched = True
                    await locator.select_option(action.arguments.get("value"))
                elif action.kind == "press":
                    dispatched = True
                    await locator.press(str(action.arguments["key"]))
                elif action.kind == "upload":
                    upload = self._upload_path(action.arguments.get("path"))
                    dispatched = True
                    await locator.set_input_files(str(upload))
            await context.tracing.stop_chunk(path=str(trace_dir / f"{trace_id}.zip"))
            trace_started = False
            post = await self._fingerprint(page)
            receipt = BrowserEngineReceipt(
                action_token=action.action_token,
                state="complete",
                page_url=_safe_page_url(page.url),
                pre_fingerprint=pre,
                post_fingerprint=post,
                trace_ids=[f"browserd-trace:{trace_id}"],
                evidence_ids=(
                    [f"browserd-screenshot:{trace_id}"]
                    if action.kind == "screenshot"
                    else []
                ),
            )
        except Exception:
            if trace_started and action.identity_id in self._contexts:
                try:
                    trace_dir = self._identity_path(action.identity_id) / "traces"
                    await self._contexts[action.identity_id].tracing.stop_chunk(
                        path=str(trace_dir / f"{trace_id}.zip")
                    )
                except Exception:
                    pass
            ambiguous = action.side_effect != "none" and dispatched
            receipt = BrowserEngineReceipt(
                action_token=action.action_token,
                state="ambiguous" if ambiguous else "failed",
                page_url=_safe_page_url(page.url) if page is not None else None,
                pre_fingerprint=pre or action.pre_fingerprint,
                trace_ids=([f"browserd-trace:{trace_id}"] if pre is not None else []),
                failure_code=(
                    "action_completion_ambiguous"
                    if ambiguous
                    else "action_precondition_failed"
                ),
                operator_message=(
                    "The page may have changed before browserd observed completion."
                    if ambiguous
                    else "The browser action failed before a side effect was observed."
                ),
                recovery_action=(
                    "Inspect the live page and trace; do not replay automatically."
                    if ambiguous
                    else "Refresh the page snapshot and issue a new action token."
                ),
            )
        return await self._ledger.finish(receipt)

    async def lifecycle(
        self, assessment_id: str, state: str
    ) -> BrowserdLifecycleReceipt:
        if not _OPAQUE_ID.fullmatch(assessment_id):
            raise ValueError("assessment id is invalid")
        self._assessment_states[assessment_id] = state
        return BrowserdLifecycleReceipt(assessment_id=assessment_id, state=state)

    async def page_for_screencast(self, identity_id: str, tab_id: str) -> Any:
        receipt = await self.ensure_identity(identity_id)
        if tab_id not in receipt.tab_ids:
            raise ValueError("tab does not belong to the selected identity")
        return self._tabs[(identity_id, tab_id)]


def create_browserd_app(
    settings: BrowserdSettings,
    *,
    manager: BrowserdManager | None = None,
) -> FastAPI:
    runtime = manager or BrowserdManager(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(title="nebula-browserd", version="1", lifespan=lifespan)

    async def require_auth(
        authorization: str = Header(default="", alias="Authorization"),
    ) -> None:
        supplied = (
            authorization[7:] if authorization.lower().startswith("bearer ") else ""
        )
        if not supplied or not hmac.compare_digest(supplied, settings.token):
            raise HTTPException(
                status_code=401, detail="valid browserd bearer token required"
            )

    @app.get(
        "/v1/readiness",
        response_model=BrowserEngineCapability,
        dependencies=[Depends(require_auth)],
    )
    async def readiness() -> BrowserEngineCapability:
        return await runtime.readiness()

    @app.post(
        "/v1/identities/ensure",
        response_model=BrowserdIdentityReceipt,
        dependencies=[Depends(require_auth)],
    )
    async def ensure_identity(
        request: BrowserdIdentityRequest,
    ) -> BrowserdIdentityReceipt:
        return await runtime.ensure_identity(request.identity_id)

    @app.post(
        "/v1/actions",
        response_model=BrowserEngineReceipt,
        dependencies=[Depends(require_auth)],
    )
    async def execute(action: BrowserEngineAction) -> BrowserEngineReceipt:
        return await runtime.execute(action)

    @app.post(
        "/v1/assessments/{assessment_id}/{action}",
        response_model=BrowserdLifecycleReceipt,
        dependencies=[Depends(require_auth)],
    )
    async def lifecycle(assessment_id: str, action: str) -> BrowserdLifecycleReceipt:
        if action not in {"pause", "resume", "stop"}:
            raise HTTPException(status_code=404, detail="unknown lifecycle action")
        states = {"pause": "paused", "resume": "running", "stop": "stopped"}
        return await runtime.lifecycle(assessment_id, states[action])

    @app.websocket("/v1/identities/{identity_id}/tabs/{tab_id}/screencast")
    async def screencast(websocket: WebSocket, identity_id: str, tab_id: str) -> None:
        protocols = [
            value.strip()
            for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if value.strip()
        ]
        supplied = ""
        for protocol in protocols:
            if protocol.startswith("nebula.auth."):
                encoded = protocol.removeprefix("nebula.auth.")
                try:
                    supplied = base64.urlsafe_b64decode(
                        encoded + "=" * (-len(encoded) % 4)
                    ).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    supplied = ""
                break
        if not supplied or not hmac.compare_digest(supplied, settings.token):
            await websocket.close(
                code=4401, reason="valid browserd bearer token required"
            )
            return
        try:
            page = await runtime.page_for_screencast(identity_id, tab_id)
        except Exception:
            await websocket.close(code=4404, reason="managed browser tab not found")
            return
        await websocket.accept(
            subprotocol="nebula.browserd.v1"
            if "nebula.browserd.v1" in protocols
            else None
        )
        cdp = await page.context.new_cdp_session(page)
        frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2)

        def on_frame(event: dict[str, Any]) -> None:
            if frames.full():
                try:
                    frames.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            frames.put_nowait(event)

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 75, "maxWidth": 1920, "maxHeight": 1080},
        )

        async def send_frames() -> None:
            while True:
                frame = await frames.get()
                await websocket.send_json(
                    {
                        "kind": "frame",
                        "data": frame["data"],
                        "metadata": frame.get("metadata", {}),
                    }
                )
                await cdp.send(
                    "Page.screencastFrameAck", {"sessionId": frame["sessionId"]}
                )

        async def receive_input() -> None:
            while True:
                event = await websocket.receive_json()
                kind = event.get("kind")
                if kind == "mouse":
                    await cdp.send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": event.get("type", "mouseMoved"),
                            "x": float(event["x"]),
                            "y": float(event["y"]),
                            "button": event.get("button", "none"),
                            "clickCount": int(event.get("clickCount", 0)),
                        },
                    )
                elif kind == "key":
                    await cdp.send(
                        "Input.dispatchKeyEvent",
                        {
                            "type": event.get("type", "keyDown"),
                            "key": str(event.get("key", "")),
                            "code": str(event.get("code", "")),
                            "modifiers": int(event.get("modifiers", 0)),
                        },
                    )
                elif kind == "text":
                    await cdp.send(
                        "Input.insertText", {"text": str(event.get("text", ""))}
                    )

        sender = asyncio.create_task(send_frames())
        receiver = asyncio.create_task(receive_input())
        try:
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            sender.cancel()
            receiver.cancel()
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
            await cdp.detach()

    return app


def settings_from_environment() -> BrowserdSettings:
    token = os.environ.get("NEBULA_BROWSERD_TOKEN", "")
    proxy = os.environ.get("NEBULA_BROWSERD_POLICY_PROXY_URL", "")
    profiles = os.environ.get("NEBULA_BROWSERD_PROFILE_ROOT", "")
    runtime = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    upload_root = os.environ.get("NEBULA_BROWSERD_UPLOAD_ROOT")
    if not all((token, proxy, profiles, runtime)):
        raise RuntimeError(
            "browserd requires token, policy proxy, profile root, and Playwright runtime environment"
        )
    return BrowserdSettings(
        token=token,
        policy_proxy_url=proxy,
        profile_root=Path(profiles),
        runtime_root=Path(runtime),
        upload_root=Path(upload_root) if upload_root else None,
        headless=os.environ.get("NEBULA_BROWSERD_HEADLESS") == "1",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Nebula managed Chromium sidecar")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("NEBULA_BROWSERD_PORT", "4711"))
    )
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        raise SystemExit("browserd port must be between 1 and 65535")
    settings = settings_from_environment()
    uvicorn.run(
        create_browserd_app(settings),
        host="127.0.0.1",
        port=arguments.port,
        log_level="warning",
        access_log=False,
    )


__all__ = [
    "ActionLedger",
    "BrowserdManager",
    "BrowserdSettings",
    "create_browserd_app",
    "main",
]
