import asyncio
import json

import pytest

from nebula.v3.mcp import MAX_MCP_MESSAGE_BYTES, McpGatewaySession
from nebula.v3.mcp_gateway import GatewayClient

# Three bytes as UTF-8, six bytes as a JSON \\uXXXX escape.
CJK = "漢"


def _text_that_only_fits_unescaped() -> str:
    """Non-ASCII text under the frame limit as UTF-8 but over it once escaped."""

    count = MAX_MCP_MESSAGE_BYTES // 6 + 4_096
    text = CJK * count
    assert len(text.encode("utf-8")) < MAX_MCP_MESSAGE_BYTES
    assert len(json.dumps(text)) > MAX_MCP_MESSAGE_BYTES
    return text


def _echo_gateway() -> McpGatewaySession:
    return McpGatewaySession(
        list_tools=lambda params: {"tools": [{"name": "echo"}]},
        call_tool=lambda name, arguments: {
            "content": [{"type": "text", "text": arguments["text"]}]
        },
    )


def test_gateway_frames_are_measured_as_utf8_not_escaped_json():
    async def scenario() -> None:
        text = _text_that_only_fits_unescaped()
        gateway = _echo_gateway()
        launch = await gateway.start()
        client = GatewayClient(launch.socket_path, launch.token)
        try:
            result = await client.request(
                "tools/call", {"name": "echo", "arguments": {"text": text}}
            )
            assert result["content"][0]["text"] == text
        finally:
            await client.close()
            await gateway.close()

    asyncio.run(scenario())


def test_gateway_client_refuses_oversized_requests_before_writing():
    async def scenario() -> None:
        gateway = _echo_gateway()
        launch = await gateway.start()
        client = GatewayClient(launch.socket_path, launch.token)
        try:
            with pytest.raises(ValueError, match="4 MiB"):
                await client.request(
                    "tools/call",
                    {
                        "name": "echo",
                        "arguments": {"text": "x" * (MAX_MCP_MESSAGE_BYTES + 1)},
                    },
                )
            # The single-use bearer means this connection must survive.
            assert await client.request("tools/list", {}) == {
                "tools": [{"name": "echo"}]
            }
        finally:
            await client.close()
            await gateway.close()

    asyncio.run(scenario())


def test_gateway_session_answers_oversized_frames_and_keeps_the_connection():
    async def scenario() -> None:
        gateway = _echo_gateway()
        launch = await gateway.start()
        reader, writer = await asyncio.open_unix_connection(
            str(launch.socket_path), limit=MAX_MCP_MESSAGE_BYTES + 1
        )
        try:
            writer.write(
                json.dumps(
                    {"id": 0, "method": "authenticate", "token": launch.token}
                ).encode()
                + b"\n"
            )
            await writer.drain()
            hello = json.loads(await reader.readline())
            assert hello["result"]["authenticated"] is True

            oversized = (
                b'{"id":7,"method":"tools/list","params":{"pad":"'
                + b"x" * (MAX_MCP_MESSAGE_BYTES + 16)
                + b'"}}\n'
            )
            writer.write(oversized)
            await writer.drain()
            reply = json.loads(await reader.readline())
            assert "4 MiB" in reply["error"]["message"]

            writer.write(b'{"id":8,"method":"tools/list","params":{}}\n')
            await writer.drain()
            reply = json.loads(await reader.readline())
            assert reply == {"id": 8, "result": {"tools": [{"name": "echo"}]}}
        finally:
            writer.close()
            await writer.wait_closed()
            await gateway.close()

    asyncio.run(scenario())
