import asyncio
import json
import sys
import textwrap
from types import SimpleNamespace

import httpx
import pytest

from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    utc_now,
)
from nebula.v3.mcp import (
    McpProbeService,
    build_mcp_tool_plugins,
    mcp_tool_display_name,
    mcp_tool_runtime_name,
)
from nebula.v3.storage import NebulaStore

# A stdio server whose capabilities and per-method errors come from argv, and
# which logs every method it receives so tests can see what the probe asked.
SERVER = textwrap.dedent(
    """
    import json, sys

    capabilities = json.loads(sys.argv[1])
    errors = json.loads(sys.argv[2])
    log = open(sys.argv[3], "a")
    results = {
        "initialize": {"protocolVersion": "2025-06-18", "capabilities": capabilities},
        "tools/list": {"tools": [{"name": "scan", "inputSchema": {"type": "object"}}]},
        "resources/list": {"resources": [{"uri": "test://r", "name": "r"}]},
        "resources/templates/list": {"resourceTemplates": []},
        "prompts/list": {"prompts": [{"name": "p"}]},
    }
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        log.write(method + "\\n")
        log.flush()
        message = {"jsonrpc": "2.0", "id": request["id"]}
        if method in errors:
            code, text = errors[method]
            message["error"] = {"code": code, "message": text}
        else:
            message["result"] = results[method]
        print(json.dumps(message), flush=True)
    """
)


# A server that closes stdout first and only explains itself on stderr a
# moment later, the way a program that fails during setup often does.
LATE_STDERR_SERVER = textwrap.dedent(
    """
    import os, sys, time

    os.close(1)
    time.sleep(0.2)
    sys.stderr.write("boom: missing config\\n")
    sys.stderr.flush()
    sys.exit(1)
    """
)


def _stdio_service(tmp_path, capabilities, errors=None, *, source=SERVER):
    script = tmp_path / "server.py"
    script.write_text(source, encoding="utf-8")
    log = tmp_path / "methods.log"
    store = NebulaStore(tmp_path / "mcp.db")
    profile = store.create(
        McpServerProfile(
            name="probe",
            transport=McpTransport.STDIO,
            command=sys.executable,
            arguments=[
                str(script),
                json.dumps(capabilities),
                json.dumps(errors or {}),
                str(log),
            ],
            trusted_stdio=True,
        )
    )
    service = McpProbeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
    )
    return store, profile, service, log


def _probe(tmp_path, capabilities, errors=None):
    _, profile, service, log = _stdio_service(tmp_path, capabilities, errors)
    report = asyncio.run(service.probe(profile.id, engagement_id="eng"))
    methods = log.read_text().split() if log.exists() else []
    return report, methods


def _http_service(tmp_path, *, startup: float, tool: float):
    store = NebulaStore(tmp_path / "mcp.db")
    profile = store.create(
        McpServerProfile(
            name="remote",
            transport=McpTransport.STREAMABLE_HTTP,
            url="http://127.0.0.1:1/mcp",
            enabled=True,
            startup_timeout_seconds=startup,
            tool_timeout_seconds=tool,
            capabilities=McpCapabilitySnapshot(
                tools=[McpToolSnapshot(name="scan", input_schema={"type": "object"})],
                checked_at=utc_now(),
            ),
        )
    )
    service = McpProbeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
    )
    return profile, service


def _slow_http_server(monkeypatch, *, answers_after: float, seen: dict):
    """Route the MCP HTTP client to a server whose tools/call takes a while.

    httpx.MockTransport does not enforce timeouts, so the handler raises the
    ReadTimeout a real transport raises when the server outlasts the request's
    read timeout. ``seen`` collects the timeouts each method was sent with.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        method = message.get("method")
        timeouts = dict(request.extensions.get("timeout") or {})
        seen[method] = timeouts
        if "id" not in message:
            return httpx.Response(202)
        if method == "tools/call":
            read = timeouts.get("read")
            if read is not None and answers_after > read:
                raise httpx.ReadTimeout(
                    "server outlasted the read timeout", request=request
                )
            result = {"content": [{"type": "text", "text": "done"}]}
        else:
            result = {"protocolVersion": "2025-06-18", "capabilities": {}}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": message["id"], "result": result}
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_unadvertised_resources_and_prompts_are_not_requested(tmp_path):
    report, methods = _probe(
        tmp_path,
        {"tools": {}},
        {name: [-32600, "Unsupported"] for name in ("resources/list", "prompts/list")},
    )

    assert report.compatible is True, report.detail
    assert [tool.name for tool in report.capabilities.tools] == ["scan"]
    assert report.capabilities.resources is False
    assert report.capabilities.prompts is False
    assert methods == ["initialize", "tools/list"]


def test_tools_are_listed_even_when_the_capability_is_omitted(tmp_path):
    report, methods = _probe(tmp_path, {})

    assert report.compatible is True, report.detail
    assert [tool.name for tool in report.capabilities.tools] == ["scan"]
    assert methods == ["initialize", "tools/list"]


@pytest.mark.parametrize("message", ["Unsupported", "Unknown method", ""])
def test_method_not_found_code_is_optional_whatever_the_message(tmp_path, message):
    report, _ = _probe(
        tmp_path,
        {"tools": {}, "resources": {}, "prompts": {}},
        {
            "resources/list": [-32601, message],
            "resources/templates/list": [-32601, message],
            "prompts/list": [-32601, message],
        },
    )

    assert report.compatible is True, report.detail
    assert report.capabilities.resources is False
    assert report.capabilities.prompts is False


def test_other_errors_fail_the_probe_even_if_they_say_not_found(tmp_path):
    report, _ = _probe(
        tmp_path,
        {"tools": {}},
        {"tools/list": [-32603, "Config file not found"]},
    )

    assert report.compatible is False
    assert "Config file not found" in (report.detail or "")
    assert report.capabilities.tools == []


def test_advertised_resources_and_prompts_are_still_discovered(tmp_path):
    report, methods = _probe(tmp_path, {"tools": {}, "resources": {}, "prompts": {}})

    assert report.compatible is True, report.detail
    assert report.capabilities.resources is True
    assert report.capabilities.prompts is True
    assert methods == [
        "initialize",
        "tools/list",
        "resources/list",
        "resources/templates/list",
        "prompts/list",
    ]


def test_http_tool_call_waits_for_the_tool_timeout_not_the_startup_timeout(
    tmp_path, monkeypatch
):
    seen: dict = {}
    _slow_http_server(monkeypatch, answers_after=1.0, seen=seen)
    profile, service = _http_service(tmp_path, startup=0.2, tool=5.0)

    result = asyncio.run(
        service.call_tool(profile, engagement_id="eng", tool_name="scan", arguments={})
    )

    assert result["content"] == [{"type": "text", "text": "done"}]
    assert seen["initialize"]["read"] == pytest.approx(0.2)
    assert seen["tools/call"]["connect"] == pytest.approx(0.2)
    assert seen["tools/call"]["read"] == pytest.approx(5.0)


def test_http_tool_timeout_is_reported_as_timed_out(tmp_path, monkeypatch):
    _slow_http_server(monkeypatch, answers_after=1.0, seen={})
    profile, service = _http_service(tmp_path, startup=0.2, tool=0.5)
    [plugin] = build_mcp_tool_plugins(service, (profile,))

    result = asyncio.run(
        plugin.execute(SimpleNamespace(engagement_id="eng", arguments={}), None)
    )

    assert result.exit_code == 1
    assert result.execution["timed_out"] is True


def test_mcp_plugin_carries_a_readable_name_beside_its_runtime_name(tmp_path):
    """The runtime name routes the call; the display name is what a person reads."""

    profile, service = _http_service(tmp_path, startup=0.2, tool=0.5)
    [plugin] = build_mcp_tool_plugins(service, (profile,))

    assert plugin.spec.name == mcp_tool_runtime_name(profile.id, "scan")
    assert plugin.spec.name != plugin.spec.display_name
    assert plugin.spec.display_name == "remote · scan"


def test_mcp_display_name_stays_bounded_and_single_line(tmp_path):
    assert mcp_tool_display_name("a\n b", "c\td") == "a b · c d"
    assert mcp_tool_display_name("", "") == "MCP · tool"
    long = mcp_tool_display_name("s" * 200, "t" * 200)
    assert len(long) == 80 + 3 + 100


def test_probe_keeps_discovered_tools_when_the_profile_changes_meanwhile(
    tmp_path, monkeypatch
):
    store, profile, service, _ = _stdio_service(tmp_path, {"tools": {}})
    original = McpProbeService._optional_list

    async def edit_profile_during_listing(client, method):
        if method == "tools/list":
            latest = store.get(McpServerProfile, profile.id)
            store.update(
                McpServerProfile,
                profile.id,
                {"tool_timeout_seconds": 90},
                expected_revision=latest.revision,
            )
        return await original(client, method)

    monkeypatch.setattr(
        McpProbeService, "_optional_list", staticmethod(edit_profile_during_listing)
    )

    report = asyncio.run(service.probe(profile.id, engagement_id="eng"))

    assert report.compatible is True, report.detail
    stored = store.get(McpServerProfile, profile.id)
    assert [tool.name for tool in stored.capabilities.tools] == ["scan"]
    assert stored.tool_timeout_seconds == 90


def test_stdio_exit_message_waits_briefly_for_stderr(tmp_path):
    _, profile, service, _ = _stdio_service(tmp_path, {}, source=LATE_STDERR_SERVER)

    report = asyncio.run(service.probe(profile.id, engagement_id="eng"))

    assert report.compatible is False
    assert "exited during discovery" in (report.detail or "")
    assert "boom: missing config" in (report.detail or "")
