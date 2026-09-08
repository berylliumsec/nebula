"""Core-owned lazy browserd lifecycle for desktop and paired web viewers."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from collections.abc import Iterator
import os
import secrets
import socket
import sys
from pathlib import Path

import uvicorn

from .browser_engine import LocalBrowserdAdapter
from .browser_host_proxy import BrowserHostProxy
from .browser_security import BrowserSecurityService
from .browserd import BrowserdManager, BrowserdSettings, create_browserd_app
from .domain import BrowserIdentity, RiskClass
from .storage import NebulaStore, NotFoundError


class _PrivateBrowserServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # Core owns process signals; a lazy in-process service must not replace them.
        yield


class ManagedBrowserHost:
    def __init__(self, store: NebulaStore, root: Path) -> None:
        self.store = store
        self.root = root
        self._lock = asyncio.Lock()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._socket: socket.socket | None = None
        self._adapter: LocalBrowserdAdapter | None = None
        self._manager: BrowserdManager | None = None
        self._proxies: dict[str, BrowserHostProxy] = {}

    async def adapter(self) -> LocalBrowserdAdapter:
        async with self._lock:
            if (
                self._task is not None
                and not self._task.done()
                and self._adapter is not None
            ):
                return self._adapter
            await self._stop()
            location = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            if not location:
                raise ValueError(
                    "The packaged Chromium runtime is unavailable on the Nebula host. Repair the host application, then retry here. Your saved conversations remain available."
                )
            runtime = Path(location).expanduser().resolve()
            if not (runtime / "nebula-playwright-runtime.json").is_file():
                raise ValueError(
                    "The host Chromium bundle is missing its verification manifest. Repair the host application, then retry here. Your saved conversations remain available."
                )
            if (
                os.name != "nt"
                and sys.platform != "darwin"
                and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            ):
                raise ValueError(
                    "The Nebula host has no desktop display for managed Chromium. Start Nebula in the host desktop session, then retry here. Your saved conversations remain available."
                )

            try:

                def deny_unbound(_: str) -> None:
                    raise ValueError("A browser identity is required.")

                fallback = BrowserHostProxy(deny_unbound)
                self._proxies[""] = fallback
                fallback_config = await fallback.start()
                listener = socket.socket()
                self._socket = listener
                listener.bind(("127.0.0.1", 0))
                listener.listen(128)
                listener.setblocking(False)
                token = secrets.token_urlsafe(32)
                settings = BrowserdSettings(
                    token=token,
                    profile_root=self.root / "profiles",
                    policy_proxy_url=fallback_config["server"],
                    runtime_root=runtime,
                    # Chromium 149 stalls authenticated proxy navigation with HTTP/2.
                    # Keep this compatibility restriction local to the built-in proxy.
                    http1_only=True,
                )
                manager = BrowserdManager(
                    settings, proxy_for_identity=self._identity_proxy
                )
                self._manager = manager
                server = _PrivateBrowserServer(
                    uvicorn.Config(
                        create_browserd_app(settings, manager=manager),
                        log_level="error",
                    )
                )
                self._server = server
                # diagnostic-expected: Core owns, monitors and drains this task in _stop.
                self._task = asyncio.create_task(server.serve(sockets=[listener]))
                for _ in range(200):
                    if self._task.done():
                        await self._task
                        raise ValueError(
                            "The managed browser service stopped during startup. Retry here; conversations are saved."
                        )
                    if server.started:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise ValueError(
                        "The managed browser service did not become ready. Retry here; conversations are saved."
                    )
                adapter = LocalBrowserdAdapter(
                    f"http://127.0.0.1:{listener.getsockname()[1]}", token
                )
                receipt = await adapter.readiness()
                if receipt.state.value != "ready":
                    raise ValueError(
                        "The packaged Chromium bundle failed runtime verification. Repair the host application and retry. Conversations remain saved."
                    )
                self._adapter = adapter
                return adapter
            except BaseException:
                # diagnostic-expected: failed startup releases its private listeners.
                await self._stop()
                raise

    async def _identity_proxy(self, identity_id: str) -> dict[str, str]:
        security = BrowserSecurityService(self.store)

        def authorize(destination: str) -> None:
            try:
                identity = self.store.get(BrowserIdentity, identity_id)
                if identity.revoked_at:
                    raise ValueError("The browser identity is revoked.")
                scope = security._scope(identity.engagement_id)
            except NotFoundError as exc:
                # diagnostic-expected: deletion revokes an existing egress grant.
                raise ValueError(
                    "The browser identity or scope is unavailable."
                ) from exc
            security._require_in_scope(
                scope, destination, "browser.navigate", RiskClass.PASSIVE
            )

        if identity_id not in self._proxies:
            if len(self._proxies) >= 65:
                raise ValueError(
                    "The host browser identity limit is reached. Restart the managed browser before opening more identities."
                )
            # Validate ownership and scope before allocating any listener.
            identity = self.store.get(BrowserIdentity, identity_id)
            if identity.revoked_at:
                raise ValueError("The browser identity is revoked.")
            security._scope(identity.engagement_id)
            self._proxies[identity_id] = BrowserHostProxy(authorize)
        return await self._proxies[identity_id].start()

    async def close(self) -> None:
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._manager is not None:
            await self._manager.close()
        if self._task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.gather(self._task, return_exceptions=True), 10
                )
            except (TimeoutError, asyncio.CancelledError):
                # diagnostic-expected: bounded shutdown of a disconnected ASGI service.
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for proxy in self._proxies.values():
            await proxy.close()
        self._proxies.clear()
        if self._socket is not None:
            self._socket.close()
        self._server = None
        self._task = None
        self._socket = None
        self._adapter = None
        self._manager = None
