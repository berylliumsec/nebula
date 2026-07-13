"""Opt-in acceptance for Qwen2.5-Coder served by vLLM's Hermes parser."""

import json
import os
from uuid import uuid4

import httpx
import pytest


@pytest.mark.skipif(
    not os.getenv("NEBULA_LIVE_QWEN_URL"),
    reason="set NEBULA_LIVE_QWEN_URL to run the live vLLM acceptance",
)
def test_live_qwen_required_probe_safe_call_and_final_response():
    base_url = os.environ["NEBULA_LIVE_QWEN_URL"].rstrip("/")
    model = os.getenv(
        "NEBULA_LIVE_QWEN_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"
    )
    nonce = uuid4().hex
    tool = {
        "type": "function",
        "function": {
            "name": "nebula_safe_echo",
            "description": "Echo a harmless verification nonce.",
            "parameters": {
                "type": "object",
                "properties": {"nonce": {"type": "string", "enum": [nonce]}},
                "required": ["nonce"],
                "additionalProperties": False,
            },
        },
    }
    with httpx.Client(base_url=base_url, timeout=120) as client:
        probe = client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": "Call the supplied function exactly once; no prose.",
                    }
                ],
                "tools": [tool],
                "tool_choice": "required",
                "parallel_tool_calls": False,
                "temperature": 0,
            },
        )
        probe.raise_for_status()
        message = probe.json()["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        assert len(calls) == 1
        call = calls[0]
        assert call["function"]["name"] == "nebula_safe_echo"
        assert json.loads(call["function"]["arguments"]) == {"nonce": nonce}

        # This is the harmless synthetic broker result. The final request uses
        # a valid assistant-call/tool-result pair and exposes no callable tools.
        safe_result = json.dumps({"echo": nonce, "status": "complete"})
        final = client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Summarize the safe synthetic result in one sentence.",
                    },
                    {
                        "role": "user",
                        "content": "Verify the local tool runtime.",
                    },
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": calls,
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": safe_result,
                    },
                ],
                "temperature": 0,
            },
        )
        final.raise_for_status()
        final_message = final.json()["choices"][0]["message"]
        assert final_message.get("content", "").strip()
        assert not final_message.get("tool_calls")
