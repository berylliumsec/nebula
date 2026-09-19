import asyncio
import json
import sys
import textwrap

import pytest

from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import McpServerProfile, McpTransport
from nebula.v3.mcp import McpProbeService
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


def _probe(tmp_path, capabilities, errors=None):
    script = tmp_path / "server.py"
    script.write_text(SERVER, encoding="utf-8")
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
    report = asyncio.run(service.probe(profile.id, engagement_id="eng"))
    methods = log.read_text().split() if log.exists() else []
    return report, methods


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
