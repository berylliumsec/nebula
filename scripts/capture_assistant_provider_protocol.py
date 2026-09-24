#!/usr/bin/env python3
"""Capture provider-only protocol contracts with an in-process HTTPX transport.

No server, Core lifespan, real provider, tool, filesystem workspace or credentials
are used. Response bytes are harmless synthetic data supplied by this file.
"""
from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import httpx
from nebula.v3 import providers as p
from nebula.v3.inline_reasoning import split_reply, ReplySplitter

ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG = {
    "id": "fixture-provider", "kind": "openai_compatible", "flavor": "custom",
    "base_url": "https://fixture.invalid", "default_model": "fixture-model",
    "capabilities": {"tools": True, "strict_tools": True, "structured_output": True},
    "options": {"retry_attempts": 1},
}
BASE_REQUEST = {"messages": [{"role": "user", "content": "Hello"}], "model": "fixture-model"}


def normalized(value):
    return value.model_dump(mode="json")


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def failure(exc):
    event = p.ModelStreamEvent.failure(exc)
    return {"class": type(exc).__name__, "event": normalized(event)}


async def capture():
    payloads, completions, streams, framing, reasoning = [], [], [], [], []

    def payload(name, request=None, config=None):
        c = p.ProviderConfig.model_validate(BASE_CONFIG | (config or {}))
        r = p.ModelRequest.model_validate(BASE_REQUEST | (request or {}))
        provider = p.OpenAICompatibleProvider(c)
        try:
            expected = {"accepted": True, "payload": provider._payload(r, provider.require(r))}
        except Exception as exc:
            expected = {"accepted": False, **failure(exc)}
        payloads.append({"name": name, "config": normalized(c), "request": normalized(r), "expected": expected})

    payload("ordinary")
    payload("instructions", {"instructions": "  Preserve spacing  ", "temperature": 0.5, "max_output_tokens": 31})
    payload("o-series", {"model": "o3-mini", "temperature": 0.5, "max_output_tokens": 31, "reasoning_effort": "high"})
    payload("gpt5", {"model": "gpt-5.1", "temperature": 0.5, "max_output_tokens": 31})
    for flavor, model in [("deepseek", "deepseek-v3.2"), ("vllm", "Qwen3-32B"), ("sglang", "glm-4.5"), ("ollama", "deepseek-v3.1")]:
        for effort in ["none", "minimal", "medium", "xhigh"]:
            payload(f"reasoning-{flavor}-{effort}", {"model": model, "reasoning_effort": effort}, {"flavor": flavor})
    payload("openrouter", {"metadata": {"chat_session_id": "session"}, "reasoning_effort": "high", "reasoning_max_tokens": 100}, {"flavor": "openrouter"})
    payload("openrouter-unsupported-reasoning", {"reasoning_effort": "high"}, {"flavor": "openrouter", "model_parameters": {"fixture-model": ["temperature"]}})
    payload("openrouter-mandatory", {"reasoning_effort": "none"}, {"flavor": "openrouter", "reasoning_mandatory_models": ["fixture-model"]})
    payload("parts", {"messages": [{"role": "user", "content": [{"type": "text", "text": "Image"}, {"type": "image", "media_type": "image/png", "data": "ZmFrZQ=="}, {"type": "unknown"}]}]})
    tool = {"name": "fixture.echo", "description": "Harmless protocol declaration", "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, "strict": True}
    payload("inert-tools", {"tools": [tool], "tool_choice": "required", "parallel_tool_calls": True})
    payload("strict-tools", {"tools": [tool]}, {"flavor": "openai"})
    payload("vllm-grammar", {"response_schema": {"type": "array", "uniqueItems": True, "items": {"type": "string"}}}, {"flavor": "vllm"})
    payload("disabled", config={"enabled": False})
    payload("model-denied", config={"model_allowlist": ["other"]})

    replies = [
        ("plain", {"id": "req-1", "model": "reported", "choices": [{"message": {"content": " Hello "}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}),
        ("usage-big", {"choices": [{"message": {"content": "Done"}}], "usage": {"prompt_tokens": 10**100, "completion_tokens": 3, "total_tokens": None}}),
        ("usage-types", {"choices": [{"message": {"content": "Done"}}], "usage": {"prompt_tokens": True, "completion_tokens": 3.9, "total_tokens": 0}}),
        ("reasoning-precedence", {"choices": [{"message": {"content": "Answer", "reasoning_content": "First", "reasoning": "Duplicate", "reasoning_details": [{"text": "Duplicate"}]}}]}),
        ("reasoning-details", {"choices": [{"message": {"content": "Answer", "reasoning_details": [{"type": "reasoning.encrypted", "text": "hidden"}, {"summary": "First"}, "Second"]}}]}),
        ("inline", {"choices": [{"message": {"content": "<think>Reason</think> Answer"}}]}),
        ("refusal-content", {"choices": [{"message": {"content": None, "refusal": "I cannot do that."}, "finish_reason": "stop"}]}),
        ("null-lists", {"choices": [{"message": {"content": "Done", "tool_calls": None}}], "usage": None}),
        ("nonobject-choice", {"choices": [None]}),
        ("missing-choice", {}),
        ("inert-call", {"choices": [{"message": {"content": None, "tool_calls": [{"id": "call-1", "function": {"name": "fixture.echo", "arguments": '{"value":1}'}}]}, "finish_reason": "tool_calls"}]}),
        ("repeated-arguments", {"choices": [{"message": {"tool_calls": [{"id": "call-1", "function": {"name": "fixture.echo", "arguments": '{"value":1}{"value":1}'}}]}}]}),
        ("bad-arguments", {"choices": [{"message": {"tool_calls": [{"id": "call-1", "function": {"name": "fixture.echo", "arguments": '{"value":'}}]}}]}),
    ]
    for field in ["model", "id", "finish_reason"]:
        for label, value in [("null", None), ("empty", ""), ("zero", 0), ("integer", 123), ("object", {"field": True})]:
            data = {"choices": [{"message": {"content": "Done"}}]}
            if field == "finish_reason":
                data["choices"][0][field] = value
            else:
                data[field] = value
            replies.append((f"field-{field}-{label}", data))
    for name, data in replies:
        request = p.ModelRequest.model_validate(BASE_REQUEST)
        config = p.ProviderConfig.model_validate(BASE_CONFIG)
        async def handler(_request, data=data):
            return httpx.Response(200, json=data)
        provider = p.OpenAICompatibleProvider(config, transport=httpx.MockTransport(handler))
        try:
            result = await provider.complete(request)
            expected = {"accepted": True, "response": normalized(result)}
        except Exception as exc:
            expected = {"accepted": False, **failure(exc)}
        completions.append({"name": name, "body": json.dumps(data, ensure_ascii=False), "expected": expected})

    frame_cases = {
        "crlf": b'data: {"a":1}\r\n\r\n',
        "cr": b'data: {"a":1}\r\r',
        "lf": b'data: {"a":1}\n\n',
        "unicode-lines": 'data: {"text":"a\u2028b\u2029c\u0085d☃"}\n\n'.encode(),
        "multiline": b'data: {\ndata: "a":1}\n\n',
        "gateway-lines": b'data: {"a":1}\ndata: {"b":2}\ndata: [DONE]\n\n',
        "comments": b': hello\nid: 99\nretry: 1\ndata: ping\n\n',
        "error-text": b'event: error\ndata: busy\n\n',
        "error-object": b'event: error\ndata: {"code":503,"message":"busy"}\n\n',
        "empty-data": b'data:\n\n',
        "eof-tail": b'data: {"a":1}',
        "malformed-utf8": b'data: {"text":"\xff"}\n\n',
    }
    for name, body in frame_cases.items():
        for size in [1, 7, len(body) or 1]:
            chunks = [body[i:i+size] for i in range(0, len(body), size)]
            response = httpx.Response(200, stream=Chunks(chunks))
            frames = [frame async for frame in p._sse_data_frames(response)]
            framing.append({"name": f"{name}-{size}", "chunks_hex": [c.hex() for c in chunks], "frames": frames})

    def chunk(delta=None, finish=None, **fields):
        return {"id": "req-1", "model": "fixture-model", "choices": [{"delta": delta or {}, "finish_reason": finish}], **fields}
    stream_cases = {
        "text": [chunk({"role": "assistant"}), chunk({"content": " Hello"}), chunk({"content": " world "}, "stop"), "[DONE]"],
        "finish-only": [chunk({"content": "Done"}, "stop")],
        "done-only": ["[DONE]"],
        "incomplete": [chunk({"content": "Partial"})],
        "reasoning": [chunk({"reasoning_content": "Reason", "reasoning": "Duplicate"}), chunk({"content": "Answer"}, "stop"), "[DONE]"],
        "inline": [chunk({"content": "<thi"}), chunk({"content": "nk>Reason</th"}), chunk({"content": "ink> Answer"}, "stop"), "[DONE]"],
        "late-usage": [chunk({"content": "Done"}, "stop"), {"id": None, "model": None, "choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}, "[DONE]"],
        "keepalive": ["ping", 4, None, chunk({"content": "Done"}, "stop"), "[DONE]"],
        "malformed-object": ['{invalid'],
        "error": [{"error": {"code": 503, "message": "busy"}}],
        "context": [{"error": {"code": "context_length_exceeded", "message": "context length exceeded"}}],
        "filter": [chunk({}, "content_filter")],
        "unknown-finish": [chunk({"content": "Done"}, "end_turn")],
        "inert-fragments": [chunk({"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "fixture.echo", "arguments": '{"value":'}}]}), chunk({"tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]}, "tool_calls"), "[DONE]"],
    }
    for field in ["model", "id", "finish_reason"]:
        for label, value in [("null", None), ("empty", ""), ("zero", 0), ("integer", 123), ("object", {"field": True})]:
            data = chunk({"content": "Done"})
            if field == "finish_reason":
                data["choices"][0][field] = value
            else:
                data[field] = value
            stream_cases[f"field-{field}-{label}"] = [data, "[DONE]"]
    stream_cases["invalid-fields-overwritten"] = [{"id": 3, "model": 4, "choices": [{"finish_reason": 7}]}, chunk({"content":"Done"}, "stop"), "[DONE]"]
    for label, error in [
        ("generic", {"message":"unknown"}),
        ("quota-status", {"code":429,"type":"insufficient_quota"}),
        ("quota-no-status", {"code":"insufficient_quota"}),
        ("context-code", {"code":"max_tokens_exceeded"}),
        ("transient-numeric-other", {"code":418,"type":"overloaded"}),
        ("upstream-context", {"code":503,"message":"Provider returned error","metadata":{"raw":"maximum context reached"}}),
    ]:
        stream_cases[f"error-{label}"] = [{"error":error}]
    for name, frames in stream_cases.items():
        body = "".join("data: " + (f if isinstance(f, str) else json.dumps(f)) + "\n\n" for f in frames).encode()
        chunks = [body[i:i+13] for i in range(0, len(body), 13)]
        async def handler(_request, chunks=chunks):
            return httpx.Response(200, stream=Chunks(chunks))
        provider = p.OpenAICompatibleProvider(p.ProviderConfig.model_validate(BASE_CONFIG), transport=httpx.MockTransport(handler))
        events = [normalized(e) async for e in provider.stream(p.ModelRequest.model_validate(BASE_REQUEST))]
        streams.append({"name": name, "chunks_hex": [c.hex() for c in chunks], "events": events})

    for model, text in [("fixture-model", "  <think>Reason</think> Answer"), ("fixture-model", "prefix <think>quoted</think>"), ("deepseek-r1", "Reason</think>Answer"), ("fixture-model", "<thinking>unfinished"), ("qwen3-thinking", "No closing tag")]:
        splitter = ReplySplitter(template_opened=p.template_opens_thinking(model))
        pieces = []
        for char in text:
            pieces.extend(splitter.push(char))
        pieces.extend(splitter.finish())
        reasoning.append({"model": model, "text": text, "split": list(split_reply(text, template_opened=p.template_opens_thinking(model))), "pieces": pieces})
    return {
        "schema": "nebula.assistant.provider-protocol.v1",
        "source_sha256": {str(path): sha256((ROOT/path).read_bytes()).hexdigest() for path in [Path("src/nebula/v3/providers.py"), Path("src/nebula/v3/inline_reasoning.py"), Path("src/nebula/v3/tool_markup.py")]},
        "config": BASE_CONFIG, "request": normalized(p.ModelRequest.model_validate(BASE_REQUEST)),
        "payloads": payloads, "completions": completions, "framing": framing, "streams": streams, "reasoning": reasoning,
        "unsupported_contracts": ["signed tool-result replay", "JSON-object instruction rendering", "full serialized tool markup recovery", "vendor raw error-detail wording", "arbitrary provider charset decoding", "native non-OpenAI-compatible adapters"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with patch.object(p.uuid, "uuid4", return_value=UUID("00000000-0000-4000-8000-000000000001")):
        result = asyncio.run(capture())
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
