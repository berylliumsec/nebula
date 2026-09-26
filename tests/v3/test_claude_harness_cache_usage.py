"""A Claude harness turn counts its whole prompt, cached parts included.

Claude Code reports the Messages API usage, whose ``input_tokens`` counts only
the uncached remainder beside ``cache_read_input_tokens`` and
``cache_creation_input_tokens``. Provider turns normalize that shape to the
whole prompt (``providers._anthropic_usage``); harness turns now do the same,
so chat usage, run spend and live usage mean the same thing for both.

The usage below was recorded on 2026-09-26 from Claude Code 2.1.277 running
``claude -p ... --output-format stream-json --verbose --model haiku`` on a turn
that read one file with the Read tool: two model calls, the first writing the
CLI's instructions to the cache and the second reading them back. Message
envelopes are trimmed to what the SDK parses (thinking blocks dropped); every
``usage`` and ``modelUsage`` value is verbatim. The CLI reported 18 input
tokens for a turn that sent 60,611.

The real ``claude_agent_sdk.ClaudeSDKClient`` runs over the scripted CLI
transport of the other Claude harness tests; only the subprocess is replaced.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from nebula.v3.domain import (
    AgentRun,
    ChatMessage,
    ChatTokenUsage,
    ChatTurn,
    HarnessDetailedUsage,
    HarnessTurn,
    RunBudget,
    RunStatus,
)
from nebula.v3.harnesses import _claude_detailed_usage
from nebula.v3.providers import _anthropic_usage
from tests.v3.test_claude_harness_turns import (
    SESSION,
    TURN_DEADLINE_SECONDS,
    Chat,
    ScriptedCli,
)

MODEL = "claude-haiku-4-5-20251001"
FIRST_CALL_USAGE = {
    "input_tokens": 10,
    "cache_creation_input_tokens": 8181,
    "cache_read_input_tokens": 21969,
    "cache_creation": {
        "ephemeral_5m_input_tokens": 0,
        "ephemeral_1h_input_tokens": 8181,
    },
    "output_tokens": 3,
    "service_tier": "standard",
    "inference_geo": "not_available",
}
SECOND_CALL_USAGE = {
    "input_tokens": 8,
    "cache_creation_input_tokens": 293,
    "cache_read_input_tokens": 30150,
    "cache_creation": {
        "ephemeral_5m_input_tokens": 0,
        "ephemeral_1h_input_tokens": 293,
    },
    "output_tokens": 3,
    "service_tier": "standard",
    "inference_geo": "not_available",
}
RESULT_USAGE = {
    "input_tokens": 18,
    "cache_creation_input_tokens": 8474,
    "cache_read_input_tokens": 52119,
    "output_tokens": 290,
    "output_tokens_details": {"thinking_tokens": 157},
    "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
    "service_tier": "standard",
    "cache_creation": {
        "ephemeral_1h_input_tokens": 8474,
        "ephemeral_5m_input_tokens": 0,
    },
    "inference_geo": "not_available",
    "iterations": [
        {
            "input_tokens": 8,
            "output_tokens": 57,
            "cache_read_input_tokens": 30150,
            "cache_creation_input_tokens": 293,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 293,
            },
            "type": "message",
        }
    ],
    "speed": "standard",
}
RESULT_MODEL_USAGE = {
    MODEL: {
        "inputTokens": 18,
        "outputTokens": 290,
        "cacheReadInputTokens": 52119,
        "cacheCreationInputTokens": 8474,
        "webSearchRequests": 0,
        "costUSD": 0.023627900000000004,
        "contextWindow": 200000,
        "maxOutputTokens": 32000,
        "thinkingTokens": 157,
        "canonicalModel": "claude-haiku-4-5",
        "provider": "firstParty",
        "costBasis": "list",
    }
}
TOTAL_COST_USD = 0.023627900000000004
ANSWER = "The launch code is 42."
TOOL_ID = "toolu_01LA5ZzcXSsqppAyLsbd7mi1"

# 18 uncached + 52,119 read + 8,474 written, and 290 out.
WHOLE_TURN = ChatTokenUsage(
    input_tokens=60_611,
    output_tokens=290,
    total_tokens=60_901,
    cached_input_tokens=52_119,
    cache_creation_input_tokens=8_474,
)


def _assistant(
    message_id: str, block: dict[str, Any], usage: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": f"asst-{message_id}-{block['type']}",
        "session_id": SESSION,
        "parent_tool_use_id": None,
        "message": {
            "model": MODEL,
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [block],
            "stop_reason": None,
            "usage": usage,
        },
    }


def recorded_turn() -> list[dict[str, Any]]:
    return [
        _assistant(
            "msg_011CfSGjJ7nYsZE385fR5W7R",
            {
                "type": "tool_use",
                "id": TOOL_ID,
                "name": "Read",
                "input": {"file_path": "note.txt"},
            },
            FIRST_CALL_USAGE,
        ),
        {
            "type": "user",
            "uuid": "user-tool-result",
            "session_id": SESSION,
            "parent_tool_use_id": None,
            "message": {
                "role": "user",
                "content": [
                    {
                        "tool_use_id": TOOL_ID,
                        "type": "tool_result",
                        "content": f"1\t{ANSWER}\n2\t",
                    }
                ],
            },
        },
        _assistant(
            "msg_011CfSGjXn4MSPcw5UMC7YTT",
            {"type": "text", "text": ANSWER},
            SECOND_CALL_USAGE,
        ),
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 5087,
            "duration_api_ms": 4239,
            "is_error": False,
            "num_turns": 2,
            "session_id": SESSION,
            "result": ANSWER,
            "stop_reason": "end_turn",
            "total_cost_usd": TOTAL_COST_USD,
            "usage": RESULT_USAGE,
            "modelUsage": RESULT_MODEL_USAGE,
            "permission_denials": [],
            "terminal_reason": "completed",
        },
    ]


def test_claude_usage_counts_the_whole_prompt_as_the_messages_adapter_does() -> None:
    detailed = _claude_detailed_usage(RESULT_USAGE)

    assert (detailed.input_tokens, detailed.output_tokens, detailed.total_tokens) == (
        60_611,
        290,
        60_901,
    )
    assert detailed.cached_input_tokens == detailed.cache_read_input_tokens == 52_119
    assert detailed.cache_creation_input_tokens == 8_474
    assert detailed.basic() == WHOLE_TURN
    # The same Messages usage means the same tokens on either runtime.
    provider = _anthropic_usage(RESULT_USAGE)
    assert detailed.basic() == ChatTokenUsage(**provider.model_dump())
    # The CLI's camelCase per-model totals read the same way.
    assert _claude_detailed_usage(RESULT_MODEL_USAGE[MODEL]).basic() == WHOLE_TURN


def test_claude_chat_turn_saves_the_whole_prompt(tmp_path: Path) -> None:
    async def scenario() -> tuple[Chat, HarnessTurn, str | None]:
        chat = Chat(tmp_path, lambda: ScriptedCli([recorded_turn()]))
        turn, answer = await chat.send("Read note.txt and reply with its contents.")
        await chat.service.shutdown()
        return chat, turn, answer

    chat, turn, answer = asyncio.run(scenario())

    assert answer == ANSWER
    owner = chat.store.get(ChatTurn, turn.chat_turn_id)
    message = chat.store.get(ChatMessage, owner.final_message_id)
    assert turn.usage == WHOLE_TURN
    assert owner.usage == WHOLE_TURN
    assert message.usage == WHOLE_TURN
    # Live usage follows each model call's whole prompt: the second call reads
    # more from the cache than the first sent uncached, and is still reported.
    ledger = chat.service.activity_events(turn.id).events
    live = [
        event.detailed_usage
        for event in ledger
        if event.type == "usage" and event.detailed_usage is not None
    ]
    assert [usage.input_tokens for usage in live] == [30_160, 30_451, 60_611]
    assert live[-1].cache_creation_input_tokens == 8_474


def test_claude_mission_spend_counts_the_whole_prompt(tmp_path: Path) -> None:
    async def scenario() -> AgentRun:
        chat = Chat(tmp_path, lambda: ScriptedCli([recorded_turn()]))
        run = await chat.service.start_mission(
            engagement_id=chat.engagement.id,
            name="Note review",
            objective="Read note.txt and report its contents",
            profile_id=chat.profile.id,
            model=MODEL,
            budget=RunBudget(max_duration_seconds=10),
        )
        await asyncio.wait_for(
            chat.service._mission_tasks[run.id], TURN_DEADLINE_SECONDS
        )
        finished = chat.store.get(AgentRun, run.id)
        await chat.service.shutdown()
        return finished

    finished = asyncio.run(scenario())

    assert finished.status == RunStatus.COMPLETE
    assert finished.metadata["input_tokens"] == 60_611
    assert finished.metadata["output_tokens"] == 290
    (entry,) = finished.metadata["harness_turn_usage"].values()
    assert entry["input_tokens"] == 60_611
    assert entry["total_tokens"] == 60_901


def test_usage_saved_before_normalization_stays_valid() -> None:
    """Rows saved before this change keep the uncached count they recorded."""

    chat_usage = ChatTokenUsage.model_validate(
        {"input_tokens": 18, "output_tokens": 290, "total_tokens": 308}
    )
    assert chat_usage.cache_creation_input_tokens == 0
    assert chat_usage.input_tokens == 18
    # Reads larger than the recorded input were normal for Claude Code rows.
    detailed = HarnessDetailedUsage.model_validate(
        {
            "input_tokens": 18,
            "output_tokens": 290,
            "total_tokens": 308,
            "cached_input_tokens": 52_119,
            "cache_creation_input_tokens": 8_474,
            "cache_read_input_tokens": 52_119,
        }
    )
    assert (detailed.input_tokens, detailed.cached_input_tokens) == (18, 52_119)
