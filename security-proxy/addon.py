"""mitmdump addon for one Core-owned, headless agent security session.

The addon has no Project policy of its own. Every outbound HTTP request,
redirect, and WebSocket handshake asks Core for a fresh scope decision. A lost
bridge fails closed. It sends bounded metadata to Core for durable capture.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

from mitmproxy import http


MAX_MESSAGE_BYTES = 256_000
MAX_HEADERS = 200


class CoreBridgeError(RuntimeError):
    pass


class AgentSecurityAddon:
    def __init__(self) -> None:
        self.address = os.environ["NEBULA_SECURITY_FLOW_BRIDGE"]
        self.token = os.environ["NEBULA_SECURITY_FLOW_TOKEN"]
        self.exchange_ids: dict[str, str] = {}

    async def _call(self, kind: str, value: dict) -> dict:
        host, port = self.address.rsplit(":", 1)
        if host != "127.0.0.1" or not port.isdigit():
            raise CoreBridgeError("invalid local Core bridge")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)), 5
        )
        try:
            payload = (
                json.dumps(
                    {"token": self.token, "kind": kind, "value": value},
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            if len(payload) > MAX_MESSAGE_BYTES:
                raise CoreBridgeError("proxy flow metadata exceeded its limit")
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), 5)
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line or len(line) > MAX_MESSAGE_BYTES:
                raise CoreBridgeError("Core flow bridge did not answer")
            reply = json.loads(line)
            if not reply.get("ok") or not isinstance(reply.get("result"), dict):
                raise CoreBridgeError("Core refused the security flow")
            return reply["result"]
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    def _headers(headers: http.Headers) -> dict[str, str]:
        return {
            name: value[:8192] for name, value in list(headers.items())[:MAX_HEADERS]
        }

    async def _wait_for_decision(self, intercept_id: str) -> dict | None:
        for _ in range(300):
            await asyncio.sleep(0.2)
            state = await self._call("intercept_poll", {"intercept_id": intercept_id})
            if state["state"] == "paused":
                continue
            if state["state"] != "forwarded":
                return None
            # Durable research records redact secrets. A redacted placeholder
            # must never be replayed as a real HTTP header value.
            if any(
                value.startswith("<redacted:") for _, value in state.get("headers", [])
            ):
                raise CoreBridgeError("redacted header edits cannot be forwarded")
            return state
        return None

    async def requestheaders(self, flow: http.HTTPFlow) -> None:
        try:
            decision = await self._call("authorize", {"url": flow.request.pretty_url})
            if decision.get("interception_enabled"):
                item = await self._call(
                    "intercept_start",
                    {
                        "transaction_id": f"{flow.id}:request",
                        "phase": "request",
                        "method": flow.request.method,
                        "url": flow.request.pretty_url,
                        "headers": list(flow.request.headers.items())[:MAX_HEADERS],
                        "timeout_seconds": 60,
                    },
                )
                state = await self._wait_for_decision(item["intercept_id"])
                if state is None:
                    flow.kill()
                    return
                if state.get("method"):
                    flow.request.method = state["method"]
                if state.get("url"):
                    flow.request.url = state["url"]
                if state.get("headers"):
                    flow.request.headers.clear()
                    for name, value in state["headers"]:
                        flow.request.headers.add(name, value)
                # An edit can change authority; recheck before forwarding.
                await self._call("authorize", {"url": flow.request.pretty_url})
        except (CoreBridgeError, OSError, ValueError, TimeoutError, KeyError):
            flow.kill()

    async def response(self, flow: http.HTTPFlow) -> None:
        if flow.response is None or flow.error is not None:
            return
        try:
            request_url = flow.request.pretty_url
            decision = await self._call("authorize", {"url": request_url})
            if decision.get("interception_enabled"):
                item = await self._call(
                    "intercept_start",
                    {
                        "transaction_id": f"{flow.id}:response",
                        "phase": "response",
                        "method": flow.request.method,
                        "url": request_url,
                        "status_code": flow.response.status_code,
                        "headers": list(flow.response.headers.items())[:MAX_HEADERS],
                        "timeout_seconds": 60,
                    },
                )
                state = await self._wait_for_decision(item["intercept_id"])
                if state is None:
                    flow.kill()
                    return
                if state.get("headers"):
                    flow.response.headers.clear()
                    for name, value in state["headers"]:
                        flow.response.headers.add(name, value)
            websocket = flow.response.status_code == 101 and (
                flow.request.headers.get("upgrade", "").lower() == "websocket"
            )
            version = flow.response.http_version
            protocol = (
                "h2"
                if version in {"HTTP/2", "HTTP/2.0"}
                else ("http/1.0" if version == "HTTP/1.0" else "http/1.1")
            )
            result = await self._call(
                "exchange",
                {
                    "method": flow.request.method,
                    "url": request_url,
                    "protocol": "websocket" if websocket else protocol,
                    "status_code": flow.response.status_code,
                    "request_headers": self._headers(flow.request.headers),
                    "response_headers": self._headers(flow.response.headers),
                    "request_bytes": len(flow.request.raw_content or b""),
                    "response_bytes": len(flow.response.raw_content or b""),
                    "duration_ms": max(
                        0,
                        int(
                            (flow.response.timestamp_end - flow.request.timestamp_start)
                            * 1000
                        ),
                    )
                    if flow.response.timestamp_end
                    else None,
                },
            )
            if websocket:
                self.exchange_ids[flow.id] = result["exchange_id"]
        except (CoreBridgeError, OSError, ValueError, TimeoutError, KeyError):
            flow.kill()

    async def websocket_message(self, flow: http.HTTPFlow) -> None:
        exchange_id = self.exchange_ids.get(flow.id)
        if exchange_id is None or flow.websocket is None or not flow.websocket.messages:
            flow.kill()
            return
        message = flow.websocket.messages[-1]
        content = message.content
        try:
            await self._call(
                "websocket_frame",
                {
                    "exchange_id": exchange_id,
                    "direction": "client" if message.from_client else "server",
                    "opcode": "text" if message.is_text else "binary",
                    "payload_preview": content[:2000].decode("utf-8", errors="replace"),
                    "payload_sha256": hashlib.sha256(content).hexdigest(),
                    "payload_bytes": len(content),
                    "truncated": len(content) > 2000,
                },
            )
        except (CoreBridgeError, OSError, ValueError, TimeoutError, KeyError):
            flow.kill()

    def websocket_end(self, flow: http.HTTPFlow) -> None:
        self.exchange_ids.pop(flow.id, None)


addons = [AgentSecurityAddon()]
