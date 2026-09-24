"""Tiny STDIO MCP shim; all authority and state remain in Nebula Core.

A harness launches one shim per connection, and waits for it before its first
turn. Keep this module standard-library only, with no package-relative
imports: the shim runs as a plain script (``python -I mcp_gateway.py`` from a
checkout, or the frozen Core's bundled copy of this file) so that starting it
never imports Nebula Core itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_MCP_MESSAGE_BYTES = 4 * 1024 * 1024


def _encode_frame(message: dict[str, Any]) -> bytes:
    """Encode one newline-delimited gateway frame as UTF-8.

    Matches Core's ``encode_gateway_frame``: the frame limit measures the
    message itself rather than its ``\\uXXXX`` escapes.
    """

    text = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    try:
        return text.encode("utf-8") + b"\n"
    except UnicodeEncodeError:
        # diagnostic-expected: lone surrogates cannot be UTF-8; send them escaped.
        return json.dumps(message, separators=(",", ":")).encode() + b"\n"


class GatewayClient:
    def __init__(self, socket_path: Path, token: str) -> None:
        self.socket_path = socket_path
        self.token = token
        self.next_id = 1
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        if self.reader is not None or self.writer is not None:
            return
        reader, writer = await asyncio.open_unix_connection(
            str(self.socket_path), limit=MAX_MCP_MESSAGE_BYTES + 1
        )
        writer.write(
            json.dumps(
                {"id": 0, "method": "authenticate", "token": self.token},
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        await writer.drain()
        line = await reader.readline()
        if not line or len(line) > MAX_MCP_MESSAGE_BYTES:
            writer.close()
            await writer.wait_closed()
            raise RuntimeError("Nebula Core gateway authentication failed")
        response = json.loads(line)
        if (
            response.get("error")
            or response.get("result", {}).get("authenticated") is not True
        ):
            writer.close()
            await writer.wait_closed()
            raise RuntimeError("Nebula Core gateway authentication failed")
        self.reader = reader
        self.writer = writer
        # The bearer is single-use. Do not retain it in the long-lived shim.
        self.token = ""

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        async with self.lock:
            await self.start()
            assert self.reader is not None and self.writer is not None
            request_id = self.next_id
            self.next_id += 1
            payload = {
                "id": request_id,
                "method": method,
                **({"params": params} if params is not None else {}),
            }
            encoded = _encode_frame(payload)
            # Refuse before writing: a partial or oversized frame would cost
            # the single-use authenticated connection for the whole session.
            if len(encoded) > MAX_MCP_MESSAGE_BYTES:
                raise ValueError("MCP request exceeded 4 MiB")
            self.writer.write(encoded)
            await self.writer.drain()
            line = await self.reader.readline()
            if not line or len(line) > MAX_MCP_MESSAGE_BYTES:
                raise RuntimeError("Nebula Core gateway became unavailable")
            response = json.loads(line)
            if response.get("id") != request_id:
                raise RuntimeError("Nebula Core gateway response was uncorrelated")
            if response.get("error"):
                error = response["error"]
                raise RuntimeError(
                    str(error.get("message") if isinstance(error, dict) else error)
                )
            return response.get("result")

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
        self.reader = None
        self.writer = None


async def serve(socket_path: Path, token: str) -> int:
    client = GatewayClient(socket_path, token)
    await client.start()
    try:
        while line := await asyncio.to_thread(sys.stdin.buffer.readline):
            request_id: Any = None
            try:
                if len(line) > MAX_MCP_MESSAGE_BYTES:
                    raise ValueError("MCP request exceeded 4 MiB")
                message = json.loads(line)
                if isinstance(message, dict) and "id" not in message:
                    # A notification (initialized, cancelled, ...) takes no
                    # answer; a reply with a null id is a protocol error.
                    continue
                request_id = message.get("id")
                method = message.get("method")
                if method == "initialize":
                    result = {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "nebula", "version": "3"},
                        "instructions": (
                            "Action tools return receipts. Inspect evidence only with "
                            "bounded retrieval tools. knowledge.list returns bounded "
                            "source metadata and knowledge.search returns cited engagement "
                            "excerpts."
                        ),
                    }
                elif method == "notifications/initialized":
                    continue
                elif method == "tools/list":
                    params = message.get("params")
                    result = await client.request(
                        "tools/list", params if isinstance(params, dict) else {}
                    )
                elif method == "tools/call":
                    params = message.get("params")
                    if not isinstance(params, dict):
                        raise ValueError("tools/call params must be an object")
                    result = await client.request("tools/call", params)
                elif method == "ping":
                    result = {}
                else:
                    raise ValueError(f"unsupported MCP method: {method}")
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except (
                Exception
            ) as exc:  # diagnostic-expected: serialized as a bounded MCP protocol error
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": str(exc)[:1000]},
                }
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        return 0
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nebula-core mcp-gateway")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--token")
    options = parser.parse_args(argv)
    token = options.token or os.environ.pop("NEBULA_MCP_GATEWAY_TOKEN", None)
    if not token:
        parser.error("gateway token is required")
    return asyncio.run(serve(options.socket, token))


if __name__ == "__main__":
    raise SystemExit(main())
