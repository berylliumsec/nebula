"""MCP text output reaches the model inline, and abandoned calls are cancelled.

An MCP server's text result was stored only as an artifact, so the model had
to call tool_output.read for output a command would have shown inline. A
timed-out call killed a stdio server without notifications/cancelled, so a
server that frees what a call holds only on a graceful cancel kept it.
"""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import base64
import json
import sys
import textwrap
import time

import httpx

from nebula.v3 import mcp
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    utc_now,
)
from nebula.v3.mcp import McpProbeService, build_mcp_tool_plugins
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_results import MAX_EXCERPT_BYTES, MAX_INLINE_OUTPUT_BYTES
from nebula.v3.tools import ToolExecutionResult
from tests.v3.test_storage_waste import _record

SECRET = "AWS_SECRET_ACCESS_KEY=abcdEFGHijklMNOPqrstUVWXyz0123456789abcd"


def _mcp_result(*blocks: dict) -> ToolExecutionResult:
    return ToolExecutionResult(
        output={},
        exit_code=0,
        mcp_content_blocks=list(blocks),
        execution={"runtime": "mcp"},
    )


async def test_mcp_text_is_previewed_inline_and_redacted(tmp_path):
    text = f"uid=501(operator)\n{SECRET}\n"
    recorded, kinds = await _record(
        tmp_path,
        _mcp_result({"type": "text", "text": text}),
        name="mcp.fixture.run_shell",
    )
    receipt = recorded.receipt

    assert kinds == ["mcp_content", "receipt"]
    [preview] = receipt.output_preview
    assert preview.kind == "mcp_text"
    assert preview.partial is False
    assert preview.artifact_id == receipt.artifacts[0].artifact_id
    assert preview.text.startswith("uid=501(operator)\n")
    assert "abcdEFGHijkl" not in preview.text
    assert "AWS_SECRET_ACCESS_KEY=[REDACTED]" in preview.text
    # A complete preview needs no follow-up read.
    assert receipt.next_actions == []
    assert "output_preview" in receipt.as_model_result()


async def test_large_mcp_text_gets_a_bounded_partial_preview(tmp_path):
    lines = "".join(f"line {index:05d} of the listing\n" for index in range(2_000))
    recorded, _ = await _record(
        tmp_path,
        _mcp_result({"type": "text", "text": lines}),
        name="mcp.fixture.run_shell",
    )
    receipt = recorded.receipt

    [preview] = receipt.output_preview
    assert preview.kind == "mcp_text"
    assert preview.partial is True
    assert preview.text.startswith("line 00000 of the listing\n")
    assert preview.text.endswith("line 01999 of the listing\n")
    assert "tool_output.read" in preview.text
    assert receipt.next_actions == ["tool_output.search", "tool_output.read"]
    rendered = json.dumps(
        receipt.as_model_result(), separators=(",", ":"), ensure_ascii=False
    )
    assert len(rendered.encode()) <= MAX_EXCERPT_BYTES


async def test_an_image_block_is_never_previewed(tmp_path):
    # Text-like bytes under a text media type: still an image block, never inline.
    image = base64.b64encode(b"plain looking image payload\n").decode()
    recorded, kinds = await _record(
        tmp_path,
        _mcp_result({"type": "image", "data": image, "mimeType": "text/plain"}),
        name="mcp.fixture.screenshot",
    )

    assert kinds == ["mcp_content", "receipt"]
    assert recorded.receipt.output_preview == []
    assert "output_preview" not in recorded.receipt.as_model_result()


async def test_an_image_beside_text_previews_only_the_text(tmp_path):
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    recorded, _ = await _record(
        tmp_path,
        _mcp_result(
            {"type": "image", "data": image, "mimeType": "image/png"},
            {"type": "text", "text": "captured the window\n"},
        ),
        name="mcp.fixture.screenshot",
    )

    [preview] = recorded.receipt.output_preview
    assert preview.text == "captured the window\n"
    assert preview.artifact_id == recorded.receipt.artifacts[1].artifact_id


async def test_structured_stdout_is_previewed_as_its_stream(tmp_path):
    stdout = "".join(f"file-{index:05d}.log\n" for index in range(600))
    structured = {"exit_code": 0, "stdout": stdout, "stderr": f"warn: {SECRET}\n"}
    recorded, kinds = await _record(
        tmp_path,
        _mcp_result(
            {"type": "text", "text": json.dumps(structured)},
            {"type": "structured_content", "value": structured},
        ),
        name="mcp.fixture.run_shell",
    )
    receipt = recorded.receipt

    # The JSON is too large to show whole, so each stream gets its own text
    # artifact and preview instead of an escaped JSON excerpt.
    assert kinds == ["mcp_content", "mcp_content", "mcp_content", "receipt"]
    assert [item.kind for item in receipt.output_preview] == ["stdout", "stderr"]
    stdout_preview, stderr_preview = receipt.output_preview
    assert stdout_preview.partial is True
    assert stdout_preview.text.startswith("file-00000.log\n")
    assert stdout_preview.text.endswith("file-00599.log\n")
    assert stderr_preview.partial is False
    assert "abcdEFGHijkl" not in stderr_preview.text
    names = {ref.artifact_id: ref.filename for ref in receipt.artifacts if ref.filename}
    assert names[stdout_preview.artifact_id].endswith("-mcp-stdout.txt")
    assert names[stderr_preview.artifact_id].endswith("-mcp-stderr.txt")


async def test_structured_output_field_without_text_is_previewed(tmp_path):
    recorded, kinds = await _record(
        tmp_path,
        _mcp_result(
            {"type": "structured_content", "value": {"output": "lease granted\n"}}
        ),
        name="mcp.fixture.lease",
    )

    assert kinds == ["mcp_content", "mcp_content", "receipt"]
    [preview] = recorded.receipt.output_preview
    assert preview.kind == "mcp_text"
    assert preview.text == "lease granted\n"


async def test_small_structured_json_text_is_shown_whole(tmp_path):
    structured = {"exit_code": 3, "stdout": "", "stderr": "no such file\n"}
    recorded, kinds = await _record(
        tmp_path,
        _mcp_result(
            {"type": "text", "text": json.dumps(structured)},
            {"type": "structured_content", "value": structured},
        ),
        name="mcp.fixture.run_shell",
    )

    # The whole JSON fits, exit code included; no extra stream artifacts.
    assert kinds == ["mcp_content", "receipt"]
    [preview] = recorded.receipt.output_preview
    assert preview.kind == "mcp_text"
    assert json.loads(preview.text) == structured
    assert len(preview.text.encode()) <= MAX_INLINE_OUTPUT_BYTES


async def test_mcp_tool_description_names_the_inline_preview(tmp_path):
    store = NebulaStore(tmp_path / "mcp.db")
    profile = await store.create(
        McpServerProfile(
            name="remote",
            transport=McpTransport.STREAMABLE_HTTP,
            url="http://127.0.0.1:1/mcp",
            enabled=True,
            capabilities=McpCapabilitySnapshot(
                tools=[McpToolSnapshot(name="scan", input_schema={"type": "object"})],
                checked_at=utc_now(),
            ),
        )
    )
    service = McpProbeService(
        store, credential_store=CredentialStore(), workspace_resolver=lambda _: tmp_path
    )
    [plugin] = await build_mcp_tool_plugins(service, (profile,))

    assert "output_preview" in plugin.spec.description


# A stdio server whose ``slow`` tool never answers. It logs every message it
# receives, and how it exits, so a test can see a cancel arrive before close.
SLOW_SERVER = textwrap.dedent(
    """
    import json, sys

    log = open(sys.argv[1], "a")
    def note(value):
        log.write(json.dumps(value) + "\\n")
        log.flush()

    for line in sys.stdin:
        message = json.loads(line)
        note(message)
        if "id" not in message:
            continue
        if message["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        else:
            continue  # tools/call: hold the call until cancelled
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}),
              flush=True)
    note("eof")
    """
)


async def _slow_stdio_profile(tmp_path, *, timeout: float):
    script = tmp_path / "slow_server.py"
    script.write_text(SLOW_SERVER, encoding="utf-8")
    log = tmp_path / "server.log"
    store = NebulaStore(tmp_path / "mcp.db")
    profile = await store.create(
        McpServerProfile(
            name="slow",
            transport=McpTransport.STDIO,
            command=sys.executable,
            arguments=[str(script), str(log)],
            trusted_stdio=True,
            enabled=True,
            tool_timeout_seconds=timeout,
            capabilities=McpCapabilitySnapshot(
                tools=[McpToolSnapshot(name="slow", input_schema={"type": "object"})],
                checked_at=utc_now(),
            ),
        )
    )
    service = McpProbeService(
        store, credential_store=CredentialStore(), workspace_resolver=lambda _: tmp_path
    )
    return profile, service, log


def _logged(log) -> list:
    return [json.loads(line) for line in log.read_text().splitlines()]


async def test_a_timed_out_stdio_call_is_cancelled_before_close(tmp_path):
    profile, service, log = await _slow_stdio_profile(tmp_path, timeout=0.5)
    [plugin] = await build_mcp_tool_plugins(service, (profile,))

    started = time.monotonic()
    result = await plugin.execute(
        type("Invocation", (), {"engagement_id": "eng", "arguments": {}})(), None
    )
    elapsed = time.monotonic() - started

    assert result.execution["timed_out"] is True
    messages = _logged(log)
    call = next(
        m for m in messages if isinstance(m, dict) and m.get("method") == "tools/call"
    )
    cancel = next(
        m
        for m in messages
        if isinstance(m, dict) and m.get("method") == "notifications/cancelled"
    )
    assert cancel["params"]["requestId"] == call["id"]
    assert "timed out" in cancel["params"]["reason"]
    # The server saw the cancel, then a graceful end of input, not a kill.
    assert messages.index(cancel) < messages.index("eof")
    # Bounded: the tool timeout plus at most the cancel grace (and spawn time).
    assert elapsed < 0.5 + mcp.MCP_CANCEL_GRACE_SECONDS + 3


async def test_a_cancelled_stdio_call_tells_the_server(tmp_path):
    profile, service, log = await _slow_stdio_profile(tmp_path, timeout=60)

    task = asyncio.create_task(
        service.call_tool(profile, engagement_id="eng", tool_name="slow", arguments={})
    )
    for _ in range(200):
        if log.exists() and "tools/call" in log.read_text():
            break
        await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - the call never answers
        raise AssertionError("the call should have been cancelled")

    messages = _logged(log)
    cancel = next(
        m
        for m in messages
        if isinstance(m, dict) and m.get("method") == "notifications/cancelled"
    )
    assert cancel["params"] == {
        "requestId": 2,
        "reason": "Nebula cancelled the tool call",
    }


async def test_a_stdio_server_that_ignores_the_cancel_is_still_closed(
    tmp_path, monkeypatch
):
    # A server that never reads stdin again cannot take the cancel or EOF;
    # close() still terminates it once the bounded grace runs out.
    monkeypatch.setattr(mcp, "MCP_CANCEL_GRACE_SECONDS", 0.2)
    stubborn = SLOW_SERVER.replace(
        "continue  # tools/call: hold the call until cancelled",
        "import time; time.sleep(3600)",
    )
    profile, service, log = await _slow_stdio_profile(tmp_path, timeout=0.3)
    (tmp_path / "slow_server.py").write_text(stubborn, encoding="utf-8")

    started = time.monotonic()
    try:
        await service.call_tool(
            profile, engagement_id="eng", tool_name="slow", arguments={}
        )
    except TimeoutError:
        pass
    else:  # pragma: no cover - the call never answers
        raise AssertionError("the call should have timed out")

    assert time.monotonic() - started < 0.3 + 0.2 + 3 + 2
    assert "eof" not in log.read_text()


async def test_a_timed_out_http_call_is_cancelled(tmp_path, monkeypatch):
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        seen.append(message)
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "tools/call":
            raise httpx.ReadTimeout(
                "server outlasted the read timeout", request=request
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    store = NebulaStore(tmp_path / "mcp.db")
    profile = await store.create(
        McpServerProfile(
            name="remote",
            transport=McpTransport.STREAMABLE_HTTP,
            url="http://127.0.0.1:1/mcp",
            enabled=True,
            tool_timeout_seconds=0.5,
            capabilities=McpCapabilitySnapshot(
                tools=[McpToolSnapshot(name="scan", input_schema={"type": "object"})],
                checked_at=utc_now(),
            ),
        )
    )
    service = McpProbeService(
        store, credential_store=CredentialStore(), workspace_resolver=lambda _: tmp_path
    )

    try:
        await service.call_tool(
            profile, engagement_id="eng", tool_name="scan", arguments={}
        )
    except httpx.ReadTimeout:
        pass
    else:  # pragma: no cover - the call never answers
        raise AssertionError("the call should have timed out")

    call = next(m for m in seen if m.get("method") == "tools/call")
    cancel = next(m for m in seen if m.get("method") == "notifications/cancelled")
    assert cancel["params"]["requestId"] == call["id"]
    assert seen.index(cancel) > seen.index(call)
