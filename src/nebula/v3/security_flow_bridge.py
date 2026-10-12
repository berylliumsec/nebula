"""Private, authenticated flow ingress for the isolated agent proxy process."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from typing import Any

from .browser_research import BrowserResearchService, InterceptCreateRequest
from .browser_security import (
    BrowserSecurityService,
    BrowserTrafficRecordRequest,
    BrowserWebSocketFrameRecordRequest,
)
from .domain import (
    BrowserInterceptItem,
    BrowserSession,
    BrowserSessionStatus,
    RiskClass,
    utc_now,
)
from .storage import NebulaStore


MAX_MESSAGE_BYTES = 256_000


class SecurityFlowBridge:
    """One proxy instance may write only one active agent security session."""

    def __init__(self, store: NebulaStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.token = secrets.token_urlsafe(32)
        self._server: asyncio.Server | None = None
        self.security = BrowserSecurityService(store)
        self.research = BrowserResearchService(store, self.security)

    @property
    def address(self) -> str:
        if self._server is None:
            raise RuntimeError("security flow bridge is not listening")
        return f"127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def start(self) -> None:
        if self._server is None:
            self._server = await asyncio.start_server(
                self._handle, "127.0.0.1", 0, limit=MAX_MESSAGE_BYTES + 1
            )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _session(self) -> BrowserSession:
        session = self.store.get(BrowserSession, self.session_id)
        if (
            session.runtime_kind != "agent_security"
            or session.status != BrowserSessionStatus.ACTIVE
            or session.active_tab_id is None
        ):
            raise ValueError("the agent security session is stopped or unavailable")
        return session

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line or len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                raise ValueError("invalid proxy message")
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("invalid proxy message")
            supplied = message.get("token")
            if not isinstance(supplied, str) or not hmac.compare_digest(
                supplied, self.token
            ):
                raise ValueError("proxy bridge authentication failed")
            result = self._dispatch(message)
            payload = {"ok": True, "result": result}
        except Exception as exc:
            # The sidecar fails closed. Detailed errors never include bearer or
            # raw response bodies; Core's diagnostics preserve the exception.
            payload = {"ok": False, "error": type(exc).__name__}
        try:
            writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def _dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        session = self._session()
        kind = message.get("kind")
        value = message.get("value")
        if not isinstance(value, dict):
            raise ValueError("invalid proxy flow payload")
        if kind == "authorize":
            url = value.get("url")
            if not isinstance(url, str):
                raise ValueError("proxy flow has no URL")
            self.security._require_in_scope(
                self.security._scope(session.engagement_id),
                url,
                "agent_security.proxy.request",
                RiskClass.ACTIVE_SCAN,
                native_scope_authority=True,
            )
            return {"interception_enabled": session.interception_enabled}
        if kind == "exchange":
            traffic_request = BrowserTrafficRecordRequest.model_validate(
                {
                    **value,
                    "tab_id": session.active_tab_id,
                }
            )
            exchange = self.security.record_traffic(
                session.id, traffic_request, "agent-security-proxy"
            )
            return {"exchange_id": exchange.id}
        if kind == "websocket_frame":
            frame_request = BrowserWebSocketFrameRecordRequest.model_validate(value)
            frame = self.security.record_websocket_frame(
                session.id, frame_request, "agent-security-proxy"
            )
            return {"frame_id": frame.id}
        if kind == "intercept_start":
            intercept_request = InterceptCreateRequest.model_validate(
                {
                    **value,
                    "tab_id": session.active_tab_id,
                }
            )
            item = self.research.pause_intercept(
                session.id, intercept_request, "agent-security-proxy"
            )
            return {"intercept_id": item.id, "expires_at": item.expires_at.isoformat()}
        if kind == "intercept_poll":
            item = self.store.get(
                BrowserInterceptItem, str(value.get("intercept_id", ""))
            )
            if item.session_id != session.id:
                raise ValueError("intercept belongs to another session")
            if item.state == "paused" and utc_now() >= item.expires_at:
                self.research.interrupt_stale_intercepts(session.engagement_id)
                item = self.store.get(BrowserInterceptItem, item.id)
            return {
                "state": item.state,
                "method": item.edited_method,
                "url": item.edited_url,
                "headers": item.edited_headers,
            }
        raise ValueError("unknown proxy flow message")
