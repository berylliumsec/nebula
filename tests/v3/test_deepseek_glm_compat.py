"""DeepSeek and Zhipu GLM (Z.ai) dialect of the Chat Completions adapter.

The operator runs ``deepseek/deepseek-v4.1-flash`` and ``z-ai/glm-5.3-flash``
through OpenRouter, and the same families directly on DeepSeek, Z.ai and
BigModel. Each case below is a shape those APIs send or require:

- structured output is ``json_object`` only (no ``json_schema``), so the schema
  travels in the instructions, and context compaction survives a rejected
  ``response_format``;
- vendor finish reasons (``insufficient_system_resource``, ``network_error``,
  ``sensitive``, ``model_context_window_exceeded``) end a reply as a failure,
  not as a complete answer;
- ``strict`` is an OpenAI contract, sent only to OpenAI routes;
- Z.ai's versioned base path (``/api/paas/v4``) takes no extra ``/v1``;
- an OpenRouter endpoint with no separate completion cap does not discard the
  verified limits of every other endpoint.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from nebula.v3.context import (
    ContextCompactionError,
    ContextCompactor,
    ContextSource,
    memory_prompt_schema,
)
from nebula.v3.domain import ChatTokenUsage, ContextMemory, ContextSourceReference
from nebula.v3.model_catalog import openrouter_model_routes
from nebula.v3.providers import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderContextLengthError,
    ProviderFlavor,
    ProviderKind,
    ProviderOverloadedError,
    ProviderRefusalError,
    StreamEventType,
    ToolChoice,
    ToolDefinition,
)
from nebula.v3.storage import NebulaStore

STRICT_TOOL = ToolDefinition(
    name="safe_read",
    description="Read one value",
    input_schema={
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

DEEPSEEK_REJECTS_FORMAT = {
    "error": {
        "message": "This response_format type is unavailable now",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_request_error",
    }
}

_BASES = {
    ProviderFlavor.OPENROUTER: "https://openrouter.ai/api/v1",
    ProviderFlavor.DEEPSEEK: "https://api.deepseek.com/v1",
    ProviderFlavor.OPENAI: "https://api.openai.com/v1",
    ProviderFlavor.AZURE_OPENAI: "https://example.openai.azure.com/openai/v1",
    ProviderFlavor.MICROSOFT_FOUNDRY: "https://example.services.ai.azure.com/openai/v1",
    ProviderFlavor.CUSTOM: "https://api.z.ai/api/paas/v4",
    ProviderFlavor.VLLM: "http://127.0.0.1:8000/v1",
    ProviderFlavor.OLLAMA: "http://127.0.0.1:11434/v1",
    ProviderFlavor.MISTRAL: "https://api.mistral.ai/v1",
}


def _provider(
    flavor: ProviderFlavor,
    model: str,
    *,
    base_url: str | None = None,
    model_parameters: dict[str, list[str]] | None = None,
    handler=None,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderConfig(
            id=f"{flavor.value}-profile",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=flavor,
            base_url=base_url or _BASES[flavor],
            default_model=model,
            local=flavor in {ProviderFlavor.VLLM, ProviderFlavor.OLLAMA},
            capabilities=ModelCapabilities(
                streaming=True, tools=True, strict_tools=True, structured_output=True
            ),
            model_parameters=model_parameters or {},
            # Zero backoff keeps the retry contract under test without waiting.
            options={"retry_backoff_seconds": 0},
        ),
        **({"transport": httpx.MockTransport(handler)} if handler else {}),
    )


def _payload(
    flavor: ProviderFlavor,
    model: str,
    *,
    base_url: str | None = None,
    model_parameters: dict[str, list[str]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    provider = _provider(
        flavor, model, base_url=base_url, model_parameters=model_parameters
    )
    request = ModelRequest(
        model=model,
        messages=[ModelMessage(role="user", content="Summarize the scan")],
        **fields,
    )
    return provider._payload(request, provider.require(request))


def _system(payload: dict[str, Any]) -> str:
    first = payload["messages"][0]
    assert first["role"] == "system"
    return first["content"]


def _sse(*frames: Any) -> httpx.Response:
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    return httpx.Response(
        200,
        text=body + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "gen-1",
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _completion(content: str, finish: str | None) -> dict[str, Any]:
    return {
        "id": "gen-1",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish,
            }
        ],
    }


def _scripted(*responses: Any):
    """A MockTransport handler that answers each request with the next item."""

    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # The last item repeats, as a provider that keeps failing would.
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, httpx.Response):
            return httpx.Response(
                item.status_code, headers=item.headers, content=item.content
            )
        return httpx.Response(200, json=item)

    return handler, seen


def _stream_events(provider: OpenAICompatibleProvider) -> list[Any]:
    async def collect():
        request = ModelRequest(messages=[ModelMessage(role="user", content="hi")])
        return [event async for event in provider.stream(request)]

    return asyncio.run(collect())


def _complete(provider: OpenAICompatibleProvider):
    return asyncio.run(
        provider.complete(
            ModelRequest(messages=[ModelMessage(role="user", content="hi")])
        )
    )


# --- MODEL-4: json_object structured output ----------------------------------


@pytest.mark.parametrize(
    ("flavor", "model", "base_url", "parameters"),
    [
        (ProviderFlavor.DEEPSEEK, "deepseek-v4-flash", None, None),
        # A Custom profile pointed at DeepSeek's own API.
        (
            ProviderFlavor.CUSTOM,
            "deepseek-v4-flash",
            "https://api.deepseek.com",
            None,
        ),
        (ProviderFlavor.CUSTOM, "glm-5.3-flash", "https://api.z.ai/api/paas/v4", None),
        (
            ProviderFlavor.CUSTOM,
            "glm-5.3-flash",
            "https://api.z.ai/api/coding/paas/v4",
            None,
        ),
        (
            ProviderFlavor.CUSTOM,
            "glm-5.3-flash",
            "https://open.bigmodel.cn/api/paas/v4",
            None,
        ),
        (
            ProviderFlavor.OPENROUTER,
            "deepseek/deepseek-v4.1-flash",
            None,
            ["tools", "tool_choice", "response_format", "max_tokens"],
        ),
        (
            ProviderFlavor.OPENROUTER,
            "z-ai/glm-5.3-flash",
            None,
            ["tools", "tool_choice", "reasoning", "max_tokens"],
        ),
    ],
)
def test_deepseek_and_zai_structured_output_uses_json_object(
    flavor, model, base_url, parameters
):
    payload = _payload(
        flavor,
        model,
        base_url=base_url,
        model_parameters={model: parameters} if parameters else None,
        instructions="Return structured working memory.",
        response_schema=RESPONSE_SCHEMA,
    )

    # DeepSeek and Z.ai accept text and json_object only; json_schema is
    # rejected ("This response_format type is unavailable now").
    assert payload["response_format"] == {"type": "json_object"}
    system = _system(payload)
    assert system.startswith("Return structured working memory.")
    assert "Return JSON matching this schema: " in system
    assert json.dumps(RESPONSE_SCHEMA, separators=(",", ":")) in system


def test_json_object_schema_travels_as_the_only_instruction_when_none_is_set():
    payload = _payload(
        ProviderFlavor.DEEPSEEK, "deepseek-v4-flash", response_schema=RESPONSE_SCHEMA
    )

    assert payload["response_format"] == {"type": "json_object"}
    assert _system(payload).startswith("Return JSON matching this schema: ")
    assert payload["messages"][1] == {"role": "user", "content": "Summarize the scan"}


@pytest.mark.parametrize(
    ("flavor", "model", "parameters"),
    [
        # Advertised structured outputs keep the schema on the wire.
        (
            ProviderFlavor.OPENROUTER,
            "deepseek/deepseek-v4.1-flash",
            ["response_format", "structured_outputs"],
        ),
        # No catalog entry: nothing is known to be unsupported.
        (ProviderFlavor.OPENROUTER, "z-ai/glm-5.3-flash", None),
        (ProviderFlavor.OPENAI, "gpt-4.1", None),
        (ProviderFlavor.VLLM, "zai-org/GLM-4.5-Air", None),
    ],
)
def test_structured_output_keeps_json_schema_where_it_is_supported(
    flavor, model, parameters
):
    payload = _payload(
        flavor,
        model,
        model_parameters={model: parameters} if parameters else None,
        response_schema=RESPONSE_SCHEMA,
    )

    assert payload["response_format"]["type"] == "json_schema"
    assert payload["messages"][0]["role"] == "user"


def test_openrouter_json_object_under_require_parameters_needs_only_response_format():
    model = "deepseek/deepseek-v4.1-flash"
    common = {
        "tools": [STRICT_TOOL],
        "tool_choice": ToolChoice.REQUIRED,
        "response_schema": RESPONSE_SCHEMA,
    }

    json_mode = _payload(
        ProviderFlavor.OPENROUTER,
        model,
        model_parameters={model: ["tools", "tool_choice", "response_format"]},
        **common,
    )
    no_json_mode = _payload(
        ProviderFlavor.OPENROUTER,
        model,
        model_parameters={model: ["tools", "tool_choice"]},
        **common,
    )

    assert json_mode["provider"] == {"require_parameters": True}
    assert json_mode["response_format"] == {"type": "json_object"}
    # A route without response_format would match no endpoint; the schema
    # still reaches the model through the instructions.
    assert "response_format" not in no_json_mode
    assert "Return JSON matching this schema: " in _system(no_json_mode)


def _memory_json() -> str:
    return json.dumps({"summary": "Three hosts are up in 10.0.0.0/24."})


def _request_memory(provider: OpenAICompatibleProvider, model: str, tmp_path: Path):
    compactor = ContextCompactor(NebulaStore(tmp_path / "context.db"))
    source = ContextSource(
        reference=ContextSourceReference(
            source_kind="chat_message", source_id="m1", sequence=1
        ),
        content="Operator: scan 10.0.0.0/24. Assistant: 3 hosts up.",
    )
    return asyncio.run(
        compactor._request_memory(
            provider,
            model,
            [source],
            None,
            2048,
            input_capacity=100_000,
            prior_usage=ChatTokenUsage(),
            budget=None,
        )
    )


@pytest.mark.parametrize(
    ("flavor", "model", "base_url"),
    [
        # A gateway in front of DeepSeek: Core cannot tell it only takes
        # json_object until the provider says so.
        (ProviderFlavor.CUSTOM, "deepseek-v4-flash", "https://llm-gateway.invalid/v1"),
        # DeepSeek itself, if a deployment rejects json_object too.
        (ProviderFlavor.DEEPSEEK, "deepseek-v4-flash", None),
    ],
)
def test_compaction_falls_back_when_response_format_rejected(
    tmp_path, flavor, model, base_url
):
    handler, seen = _scripted(
        httpx.Response(400, json=DEEPSEEK_REJECTS_FORMAT),
        _completion(_memory_json(), "stop"),
    )
    provider = _provider(flavor, model, base_url=base_url, handler=handler)

    result = _request_memory(provider, model, tmp_path)

    assert result.memory.summary == "Three hosts are up in 10.0.0.0/24."
    assert len(seen) == 2
    first, retry = (json.loads(request.content) for request in seen)
    assert "response_format" in first
    # The retry asks for the same memory with the schema in the instructions
    # and no response_format at all, which the provider just refused.
    assert "response_format" not in retry
    system = _system(retry)
    assert system.count("Return JSON matching this schema: ") == 1
    assert (
        json.dumps(memory_prompt_schema(), ensure_ascii=False, separators=(",", ":"))
        in system
    )


def test_compaction_does_not_retry_an_unrelated_provider_error(tmp_path):
    handler, seen = _scripted(
        httpx.Response(400, json={"error": {"message": "model not found"}})
    )
    provider = _provider(
        ProviderFlavor.CUSTOM,
        "deepseek-v4-flash",
        base_url="https://llm-gateway.invalid/v1",
        handler=handler,
    )

    with pytest.raises(ContextCompactionError, match="provider request failed"):
        _request_memory(provider, "deepseek-v4-flash", tmp_path)

    assert len(seen) == 1


def test_compaction_falls_back_only_once(tmp_path):
    handler, seen = _scripted(httpx.Response(400, json=DEEPSEEK_REJECTS_FORMAT))
    provider = _provider(
        ProviderFlavor.CUSTOM,
        "deepseek-v4-flash",
        base_url="https://llm-gateway.invalid/v1",
        handler=handler,
    )

    with pytest.raises(ContextCompactionError, match="provider request failed"):
        _request_memory(provider, "deepseek-v4-flash", tmp_path)

    assert len(seen) == 2


# --- MODEL-6: vendor finish reasons ------------------------------------------


@pytest.mark.parametrize(
    ("reason", "error_type", "wording"),
    [
        ("insufficient_system_resource", ProviderOverloadedError, "with an error"),
        ("network_error", ProviderOverloadedError, "with an error"),
        # Not a vendor value Core names, but plainly an error family.
        ("server_error", ProviderOverloadedError, "with an error"),
        # Reported as a refusal (#489), not an answer to finish.
        ("sensitive", ProviderRefusalError, "content filter"),
        ("model_context_window_exceeded", ProviderContextLengthError, "context"),
    ],
)
def test_vendor_finish_reasons_are_not_completed_answers(reason, error_type, wording):
    handler, seen = _scripted(
        _sse(
            _chunk({"role": "assistant"}),
            _chunk({"content": "The scan found 3 open po"}),
            _chunk({}, reason),
        )
    )
    events = _stream_events(_provider(ProviderFlavor.DEEPSEEK, "m", handler=handler))

    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.ERROR,
    ]
    assert wording in (events[-1].error or "")
    assert reason in (events[-1].error or "")
    # Output was already shown, so the stream is never silently replayed.
    assert len(seen) == 1
    assert events[-1].retryable is (error_type is ProviderOverloadedError)
    assert events[-1].context_length_exceeded is (
        error_type is ProviderContextLengthError
    )

    handler, seen = _scripted(_completion("The scan found 3 open po", reason))
    with pytest.raises(error_type) as failure:
        _complete(_provider(ProviderFlavor.CUSTOM, "m", handler=handler))

    assert wording in str(failure.value)
    assert reason in str(failure.value)
    # A transient finish is resent like an HTTP 503; the others are final.
    assert len(seen) == (3 if error_type is ProviderOverloadedError else 1)


@pytest.mark.parametrize("reason", ["insufficient_system_resource", "network_error"])
def test_transient_vendor_finish_before_output_is_replayed(reason):
    handler, seen = _scripted(
        _sse(_chunk({"role": "assistant"}), _chunk({"content": ""}, reason)),
        _sse(_chunk({"content": "3 hosts are up."}, "stop")),
    )

    events = _stream_events(_provider(ProviderFlavor.DEEPSEEK, "m", handler=handler))

    assert len(seen) == 2
    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[-1].response.text == "3 hosts are up."

    handler, seen = _scripted(
        _completion("", reason), _completion("3 hosts are up.", "stop")
    )
    response = _complete(_provider(ProviderFlavor.CUSTOM, "m", handler=handler))

    assert len(seen) == 2
    assert response.text == "3 hosts are up."


@pytest.mark.parametrize(
    "reason",
    ["stop", "length", "tool_calls", "function_call", "end", "eos", "end_turn", None],
)
def test_ordinary_finish_reasons_still_complete(reason):
    handler, _seen = _scripted(
        _sse(_chunk({"content": "3 hosts are up."}), _chunk({}, reason))
    )
    events = _stream_events(_provider(ProviderFlavor.DEEPSEEK, "m", handler=handler))

    assert events[-1].type == StreamEventType.COMPLETED
    assert events[-1].response.text == "3 hosts are up."

    handler, _seen = _scripted(_completion("3 hosts are up.", reason))
    response = _complete(_provider(ProviderFlavor.CUSTOM, "m", handler=handler))

    assert response.text == "3 hosts are up."
    assert response.finish_reason == reason


# --- Strict scope --------------------------------------------------------------


@pytest.mark.parametrize(
    ("flavor", "model"),
    [
        (ProviderFlavor.DEEPSEEK, "deepseek-v4-flash"),
        (ProviderFlavor.OPENROUTER, "deepseek/deepseek-v4.1-flash"),
        (ProviderFlavor.OPENROUTER, "z-ai/glm-5.3-flash"),
        (ProviderFlavor.OPENROUTER, "anthropic/claude-sonnet-4.5"),
        (ProviderFlavor.CUSTOM, "glm-5.3-flash"),
        (ProviderFlavor.VLLM, "zai-org/GLM-4.5-Air"),
        (ProviderFlavor.MISTRAL, "mistral-large-latest"),
        (ProviderFlavor.OLLAMA, "llama3.1"),
    ],
)
def test_strict_is_not_sent_outside_openai_routes(flavor, model):
    tools = _payload(
        flavor,
        model,
        tools=[STRICT_TOOL],
        tool_choice=ToolChoice.REQUIRED,
    )
    # A route that advertises structured outputs still gets no strict flag.
    structured = _payload(
        flavor,
        model,
        model_parameters={model: ["response_format", "structured_outputs"]},
        response_schema=RESPONSE_SCHEMA,
    )

    # OpenRouter's own SDK never sends strict on tools; DeepSeek strict mode is
    # beta-only and needs every tool strict, which tool_catalog.call is not.
    assert "strict" not in tools["tools"][0]["function"]
    assert tools["tools"][0]["function"]["name"] == "safe_read"
    if structured["response_format"]["type"] == "json_schema":
        assert "strict" not in structured["response_format"]["json_schema"]


@pytest.mark.parametrize(
    ("flavor", "model"),
    [
        (ProviderFlavor.OPENAI, "gpt-4.1"),
        (ProviderFlavor.AZURE_OPENAI, "gpt-4.1"),
        (ProviderFlavor.MICROSOFT_FOUNDRY, "gpt-5-mini"),
        (ProviderFlavor.OPENROUTER, "openai/gpt-5-mini"),
    ],
)
def test_strict_is_kept_for_openai_routes(flavor, model):
    tools = _payload(flavor, model, tools=[STRICT_TOOL])
    structured = _payload(flavor, model, response_schema=RESPONSE_SCHEMA)
    memory = _payload(flavor, model, response_schema=ContextMemory.model_json_schema())

    assert tools["tools"][0]["function"]["strict"] is True
    assert structured["response_format"]["json_schema"]["strict"] is True
    # #484's guard still applies: a schema strict mode rejects goes without it.
    assert memory["response_format"]["json_schema"]["strict"] is False


# --- MODEL-5: versioned base paths ---------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.z.ai/api/paas/v4",
        "https://api.z.ai/api/coding/paas/v4",
        "https://open.bigmodel.cn/api/paas/v4/",
    ],
)
def test_versioned_base_paths_are_not_given_v1(base_url):
    handler, seen = _scripted(
        _completion("3 hosts are up.", "stop"),
        {"object": "list", "data": [{"id": "glm-5.3-flash"}]},
    )
    provider = _provider(
        ProviderFlavor.CUSTOM, "glm-5.3-flash", base_url=base_url, handler=handler
    )

    response = _complete(provider)
    health = asyncio.run(provider.health())

    prefix = base_url.rstrip("/")
    assert str(seen[0].url) == f"{prefix}/chat/completions"
    assert str(seen[1].url) == f"{prefix}/models"
    assert response.text == "3 hosts are up."
    assert health.healthy is True


@pytest.mark.parametrize(
    ("base_url", "path", "expected"),
    [
        ("https://api.deepseek.com/v1", "/v1/chat/completions", "/chat/completions"),
        ("https://openrouter.ai/api/v1", "/v1/models", "/models"),
        ("https://api.deepseek.com", "/v1/chat/completions", "/v1/chat/completions"),
        (
            "https://generativelanguage.googleapis.com/v1beta",
            "/v1beta/models",
            "/models",
        ),
        (
            "https://generativelanguage.googleapis.com",
            "/v1beta/models",
            "/v1beta/models",
        ),
        ("https://api.z.ai/api/anthropic", "/v1/messages", "/v1/messages"),
        (
            "https://gateway.invalid/openai/v2",
            "/v1/chat/completions",
            "/chat/completions",
        ),
        # A path segment that only starts with "v" is not a version.
        ("https://gateway.invalid/dev", "/v1/chat/completions", "/v1/chat/completions"),
    ],
)
def test_existing_base_path_rules_are_kept(base_url, path, expected):
    provider = _provider(ProviderFlavor.CUSTOM, "m", base_url=base_url)

    assert provider._path(path) == expected


# --- MODEL-8: OpenRouter endpoints without a completion cap ----------------------


def _endpoint(name: str, tag: str, max_completion: Any, **extra: Any) -> dict:
    return {
        "provider_name": name,
        "tag": tag,
        "context_length": 1_048_576,
        "max_prompt_tokens": None,
        "max_completion_tokens": max_completion,
        "supported_parameters": ["tools", "tool_choice", "max_tokens"],
        "status": 0,
        **extra,
    }


def test_endpoint_with_null_completion_limit_is_not_fatal():
    model = "z-ai/glm-5.3-flash"
    routes = openrouter_model_routes(
        {
            "data": {
                "id": model,
                "endpoints": [
                    _endpoint("Z.AI", "z-ai", 131_072),
                    _endpoint(
                        "Upstream B", "upstream-b/fp8", None, context_length=202_752
                    ),
                    {
                        key: value
                        for key, value in _endpoint(
                            "Upstream C", "upstream-c", 1
                        ).items()
                        if key != "max_completion_tokens"
                    },
                ],
            }
        },
        model=model,
    )

    assert [(route.provider_name, route.max_output_tokens) for route in routes] == [
        ("Z.AI", 131_072),
        # No separate completion cap: the endpoint's own window bounds output,
        # and the endpoint still bounds sizing, since OpenRouter may pick it.
        ("Upstream B", 202_752),
        ("Upstream C", 1_048_576),
    ]
    assert routes[1].context_window == 202_752


@pytest.mark.parametrize("max_completion", [0, -1, "4096", 1.5])
def test_endpoint_with_an_invalid_completion_limit_is_still_rejected(max_completion):
    with pytest.raises(ValueError, match="Incomplete OpenRouter endpoint limits"):
        openrouter_model_routes(
            {
                "data": {
                    "id": "author/model",
                    "endpoints": [_endpoint("A", "a", max_completion)],
                }
            },
            model="author/model",
        )


def test_openrouter_route_limits_keep_every_endpoint_with_a_null_cap():
    model = "deepseek/deepseek-v4.1-flash"
    handler, seen = _scripted(
        {
            "data": {
                "id": model,
                "endpoints": [
                    _endpoint("DeepSeek", "deepseek", 384_000),
                    _endpoint("Upstream B", "upstream-b", None),
                ],
            }
        }
    )
    provider = _provider(ProviderFlavor.OPENROUTER, model, handler=handler)

    routes = asyncio.run(provider.openrouter_route_limits(model))

    assert seen[0].url.path == "/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints"
    assert [route.max_output_tokens for route in routes] == [384_000, 1_048_576]
