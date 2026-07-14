#!/usr/bin/env python3
"""Deterministic newline-delimited MCP server used by Core tests."""

from __future__ import annotations

import json
import sys


def reply(request: dict, result: dict) -> None:
    print(
        json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}),
        flush=True,
    )


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "initialize":
        reply(
            request,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {},
                    "prompts": {},
                },
                "serverInfo": {"name": "nebula-test-mcp", "version": "1.0"},
            },
        )
    elif method == "tools/list":
        # Discovery must tolerate notifications interleaved with a response.
        print(
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            ),
            flush=True,
        )
        schema = (
            {"type": "not-a-real-json-schema-type"}
            if "--bad-schema" in sys.argv
            else {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            }
        )
        reply(
            request,
            {
                "tools": [
                    {
                        "name": "read_file",
                        "description": "Read one workspace file",
                        "inputSchema": schema,
                        "annotations": {
                            "readOnlyHint": True,
                            "destructiveHint": False,
                            "idempotentHint": True,
                            "openWorldHint": False,
                        },
                        "_meta": {"nebula/credentialed": False},
                    }
                ]
            },
        )
    elif method == "resources/list":
        reply(request, {"resources": [{"uri": "test://resource", "name": "test"}]})
    elif method == "resources/templates/list":
        reply(request, {"resourceTemplates": []})
    elif method == "prompts/list":
        reply(request, {"prompts": [{"name": "summarize"}]})
    else:
        print(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32601, "message": "method not found"},
                }
            ),
            flush=True,
        )
