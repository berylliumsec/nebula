"""Gemini call ids Core makes up never repeat across responses."""

import asyncio
import json

import httpx

from nebula.v3.providers import (
    GeminiProvider,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelToolResult,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    ToolDefinition,
)

TOOL = ToolDefinition(
    name="safe_read",
    description="Return one bounded value.",
    input_schema={
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
)


def _gemini(bodies: list[dict], seen: list[dict]) -> GeminiProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=bodies.pop(0))

    return GeminiProvider(
        ProviderConfig(
            id="gemini",
            kind=ProviderKind.GEMINI,
            flavor=ProviderFlavor.GEMINI,
            base_url="https://generativelanguage.googleapis.com",
            default_model="model-a",
            model_allowlist=["model-a"],
            api_key_value="fake-test-value",
            capabilities=ModelCapabilities(tools=True, strict_tools=True),
        ),
        transport=httpx.MockTransport(handler),
    )


def _call_body(value: str, **extra) -> dict:
    return {
        **extra,
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "safe_read",
                                "args": {"value": value},
                            }
                        }
                    ]
                },
            }
        ],
    }


def test_gemini_calls_without_a_response_id_get_distinct_ids_per_response():
    seen: list[dict] = []
    provider = _gemini([_call_body("a"), _call_body("b")], seen)
    request = ModelRequest(
        messages=[ModelMessage(role="user", content="read a then b")], tools=[TOOL]
    )

    first = asyncio.run(provider.complete(request))
    second = asyncio.run(
        provider.complete(
            request.model_copy(
                update={
                    "tool_results": [
                        ModelToolResult(
                            call_id=first.tool_calls[0].id,
                            name="safe_read",
                            arguments={"value": "a"},
                            output='{"value": "a"}',
                        )
                    ]
                }
            )
        )
    )

    # Two routing steps' first calls must not share an id, or the second
    # looks like a replay of the first.
    assert first.tool_calls[0].id != second.tool_calls[0].id
    # A made-up id is Core's own and is never echoed back to Gemini.
    replayed = json.dumps(seen[1]["contents"])
    assert first.tool_calls[0].id not in replayed


def test_gemini_calls_keep_the_response_id_scope_when_one_is_sent():
    seen: list[dict] = []
    provider = _gemini([_call_body("a", responseId="gemini_1")], seen)

    response = asyncio.run(
        provider.complete(
            ModelRequest(
                messages=[ModelMessage(role="user", content="read a")], tools=[TOOL]
            )
        )
    )

    assert response.tool_calls[0].id == "nebula-gemini-call:gemini_1:0"
