"""GLM and DeepSeek native tool markup, and inline ``<think>`` reasoning.

A serving runtime that does not run a model's tool or reasoning parser
leaves the model's own markup in the Chat Completions ``content`` field:
vLLM and SGLang skip tool parsing for a request that declares no tools, and
local runtimes without a reasoning parser keep ``<think>`` in the reply.
Each shape is exercised whole (``complete``) and streamed (``stream``),
through the real OpenAI-compatible adapter and chat's answer checks.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nebula.v3.chat import (
    _final_answer_problem,
    _operator_answer_text,
    _StreamedAnswer,
)
from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.providers import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    StreamEventType,
    ToolCall,
)
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response

GLM = "z-ai/glm-5.3-flash"
DEEPSEEK = "deepseek/deepseek-v4.1-flash"
MODES = pytest.mark.parametrize("streamed", [False, True], ids=["complete", "stream"])


def _provider(model: str, response: httpx.Response) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderConfig(
            id="upstream",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.CUSTOM,
            base_url="https://provider.invalid/v1",
            default_model=model,
            capabilities=ModelCapabilities(tools=True, streaming=True),
            options={"retry_backoff_seconds": 0},
        ),
        transport=httpx.MockTransport(lambda _request: response),
    )


def _wire_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "call_1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


async def _collect(events: Any) -> list[ModelStreamEvent]:
    return [event async for event in events]


def _reply(
    model: str,
    content: list[str],
    *,
    streamed: bool,
    reasoning: str = "",
    tool_call: dict[str, Any] | None = None,
    finish: str = "stop",
) -> tuple[ModelResponse, list[ModelStreamEvent]]:
    """What the adapter makes of one reply, whole or streamed in these pieces."""

    if streamed:
        frames: list[dict[str, Any]] = []
        if reasoning:
            frames.append({"choices": [{"delta": {"reasoning_content": reasoning}}]})
        frames.extend({"choices": [{"delta": {"content": piece}}]} for piece in content)
        if tool_call is not None:
            frames.append(
                {"choices": [{"delta": {"tool_calls": [{"index": 0, **tool_call}]}}]}
            )
        frames.append({"choices": [{"delta": {}, "finish_reason": finish}]})
        body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        wire = httpx.Response(
            200,
            text=body + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
    else:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_call is not None:
            message["tool_calls"] = [tool_call]
        wire = httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": model,
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            },
        )
    provider = _provider(model, wire)
    request = ModelRequest(messages=[ModelMessage(role="user", content="value?")])
    if not streamed:
        return asyncio.run(provider.complete(request)), []
    events = asyncio.run(_collect(provider.stream(request)))
    assert events[-1].type == StreamEventType.COMPLETED, events[-1]
    assert events[-1].response is not None
    return events[-1].response, events


def _deltas(events: list[ModelStreamEvent], kind: StreamEventType) -> list[str]:
    return [event.delta or "" for event in events if event.type == kind]


def _calls(response: ModelResponse) -> list[tuple[str, dict[str, Any]]]:
    return [(call.name, call.arguments) for call in response.tool_calls]


# GLM-4.5's template puts a newline after the name and after each element.
GLM_NEWLINE = (
    "<tool_call>safe_read\n<arg_key>value</arg_key>\n<arg_value>a</arg_value>\n"
    "</tool_call>"
)
# GLM-4.7 and 5.x write the same call with nothing between the elements.
GLM_COMPACT = (
    "<tool_call>safe_read<arg_key>value</arg_key><arg_value>a</arg_value></tool_call>"
)
# DeepSeek V3 and R1: a type, then the name and a fenced JSON body.
DEEPSEEK_FENCED = (
    "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>safe_read\n"
    '```json\n{"value": "a"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>'
)
# DeepSeek V3.1: the name, then the JSON arguments bare.
DEEPSEEK_BARE = (
    "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>safe_read<｜tool▁sep｜>"
    '{"value": "a"}<｜tool▁call▁end｜><｜tool▁calls▁end｜>'
)
NATIVE_CALLS = pytest.mark.parametrize(
    ("model", "markup"),
    [
        (GLM, GLM_NEWLINE),
        (GLM, GLM_COMPACT),
        (DEEPSEEK, DEEPSEEK_FENCED),
        (DEEPSEEK, DEEPSEEK_BARE),
        # Routes that normalize text turn the full-width pipes into ASCII.
        (DEEPSEEK, DEEPSEEK_FENCED.replace("\uff5c", "|")),
    ],
    ids=[
        "glm-newline",
        "glm-compact",
        "deepseek-fenced",
        "deepseek-bare",
        "deepseek-ascii-pipes",
    ],
)


def _pieces(text: str, size: int = 5) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]


@NATIVE_CALLS
@MODES
def test_native_tool_markup_is_recovered_as_a_tool_call(model, markup, streamed):
    response, _ = _reply(model, _pieces(markup), streamed=streamed)

    assert _calls(response) == [("safe_read", {"value": "a"})]
    assert response.text == ""
    prefix = "glm-" if markup.startswith("<tool_call>") else "deepseek-"
    assert response.tool_calls[0].id.startswith(prefix)
    # A final answer that was only a call is a tool call, never an answer.
    assert _final_answer_problem(response) == "tool_call"


@NATIVE_CALLS
@MODES
def test_an_answer_before_native_markup_stands_and_the_call_is_recovered(
    model, markup, streamed
):
    response, _ = _reply(
        model, ["The value is a.", "\n\n", *_pieces(markup, 7)], streamed=streamed
    )

    assert response.text == "The value is a."
    assert _calls(response) == [("safe_read", {"value": "a"})]
    assert _final_answer_problem(response) is None


@MODES
def test_every_native_call_in_a_reply_is_recovered_with_its_values(streamed):
    glm = (
        "<tool_call>safe_read\n<arg_key>path</arg_key>\n<arg_value>notes.md</arg_value>\n"
        "<arg_key>limit</arg_key>\n<arg_value>20</arg_value>\n"
        '<arg_key>options</arg_key>\n<arg_value>{"raw": true}</arg_value>\n</tool_call>'
        "<tool_call>list_hosts\n</tool_call>"
    )
    deepseek = (
        "<｜tool▁calls▁begin｜>"
        "<｜tool▁call▁begin｜>function<｜tool▁sep｜>safe_read\n"
        '```json\n{"path": "a.md"}\n```<｜tool▁call▁end｜>\n'
        "<｜tool▁call▁begin｜>function<｜tool▁sep｜>list_hosts\n"
        "```json\n{}\n```<｜tool▁call▁end｜>"
        "<｜tool▁calls▁end｜>"
    )

    glm_response, _ = _reply(GLM, _pieces(glm), streamed=streamed)
    deepseek_response, _ = _reply(DEEPSEEK, _pieces(deepseek), streamed=streamed)

    assert _calls(glm_response) == [
        ("safe_read", {"path": "notes.md", "limit": 20, "options": {"raw": True}}),
        ("list_hosts", {}),
    ]
    assert _calls(deepseek_response) == [
        ("safe_read", {"path": "a.md"}),
        ("list_hosts", {}),
    ]
    assert glm_response.text == deepseek_response.text == ""


UNREADABLE = pytest.mark.parametrize(
    ("model", "frame"),
    [
        # The same argument twice: which value wins is a guess.
        (
            GLM,
            "<tool_call>safe_read<arg_key>value</arg_key><arg_value>a</arg_value>"
            "<arg_key>value</arg_key><arg_value>b</arg_value></tool_call>",
        ),
        # Prose among the arguments is an instruction Core cannot read.
        (
            GLM,
            "<tool_call>safe_read\nthe file I mentioned\n"
            "<arg_key>value</arg_key><arg_value>a</arg_value></tool_call>",
        ),
        # Cut off by the output limit.
        (GLM, "<tool_call>safe_read\n<arg_key>value</arg_key>\n<arg_value>a"),
        # Arguments that are not a JSON object.
        (
            DEEPSEEK,
            "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>safe_read<｜tool▁sep｜>"
            "value=a<｜tool▁call▁end｜><｜tool▁calls▁end｜>",
        ),
        # A call type other than function.
        (
            DEEPSEEK,
            "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>retrieval<｜tool▁sep｜>safe_read\n"
            '```json\n{"value": "a"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>',
        ),
        # Cut off before the frame closed.
        (
            DEEPSEEK,
            "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>safe_read\n"
            '```json\n{"value": "a"}\n```<｜tool▁call▁end｜>',
        ),
    ],
    ids=[
        "glm-repeated-argument",
        "glm-prose-inside",
        "glm-truncated",
        "deepseek-arguments-not-json",
        "deepseek-unknown-type",
        "deepseek-truncated",
    ],
)


@UNREADABLE
@MODES
def test_native_markup_core_cannot_read_is_quarantined_not_answered(
    model, frame, streamed
):
    response, _ = _reply(model, _pieces(frame), streamed=streamed)

    assert response.tool_calls == []
    assert _final_answer_problem(response) == "provider_control_frame"


@UNREADABLE
@MODES
def test_the_answer_before_unreadable_native_markup_is_all_that_is_kept(
    model, frame, streamed
):
    response, _ = _reply(
        model, ["The value is a.", "\n\n", *_pieces(frame)], streamed=streamed
    )

    assert response.tool_calls == []
    assert _operator_answer_text(response.text) == "The value is a."
    assert _final_answer_problem(response) is None


@NATIVE_CALLS
def test_native_markup_in_a_routing_step_runs_like_any_other_tool_call(
    tmp_path, model, markup
):
    del model
    broker = RecordingBroker()
    store, service, prepared, _provider = _prepared(
        tmp_path,
        [
            _response(text=markup),
            _response(
                calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
            ),
            _response(text="The safe tool returned a."),
        ],
        broker,
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "The safe tool returned a."
    # The broker saw an ordinary invocation with the arguments the model wrote.
    assert [(call.tool_name, call.arguments) for call in broker.calls] == [
        ("safe_read", {"value": "a"})
    ]
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE


@UNREADABLE
def test_final_synthesis_keeps_only_the_answer_before_unreadable_native_markup(
    tmp_path, model, frame
):
    del model
    answer = "The stored value is a."
    store, service, prepared, _provider = _prepared(
        tmp_path,
        [
            _response(
                calls=[
                    ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})
                ]
            ),
            _response(
                calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
            ),
            _response(text=f"{answer}\n\n{frame}"),
        ],
        RecordingBroker(),
    )

    async def scenario():
        return [event async for event in service.stream(prepared)]

    events = asyncio.run(scenario())

    visible = "".join(payload["delta"] for name, payload in events if name == "delta")
    assert visible == answer
    done = [payload for name, payload in events if name == "done"]
    assert done[-1]["message"]["content"] == answer
    serialized = json.dumps(events, ensure_ascii=False)
    assert "<tool_call>" not in serialized
    assert "tool\u2581" not in serialized
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE


@pytest.mark.parametrize(
    "frame",
    [
        GLM_NEWLINE,
        GLM_COMPACT,
        DEEPSEEK_FENCED,
        DEEPSEEK_BARE.replace("\uff5c", "|"),
        "<tool_call>safe_read\nthe file I mentioned\n",
    ],
    ids=["glm-newline", "glm-compact", "deepseek", "deepseek-ascii-pipes", "glm-prose"],
)
def test_a_streamed_answer_never_shows_native_markup(frame):
    for size in (1, 3, 7, 64):
        answer = _StreamedAnswer()
        shown = "".join(
            answer.push(piece) for piece in _pieces(f"The value is a.\n{frame}", size)
        )
        assert shown == "The value is a.\n", size
        assert answer.held_tail("The value is a.") == ""


@pytest.mark.parametrize(
    "text",
    [
        "GLM wraps each call in `<tool_call>` tags.",
        "Qwen writes <tool_call> and then a JSON object.",
        "A reply may close its thought with </think> before the answer.",
        "Compare a<b and c <tool_ids> here.",
    ],
)
def test_text_that_mentions_markup_is_an_answer(text):
    response = ModelResponse(provider_id="upstream", model=GLM, text=text)

    assert response.tool_calls == []
    assert _operator_answer_text(response.text) == text
    answer = _StreamedAnswer()
    shown = "".join(answer.push(piece) for piece in _pieces(text, 3))
    assert shown + answer.held_tail(text) == text


@pytest.mark.parametrize("size", [1, 3, 200])
@pytest.mark.parametrize("tag", ["think", "thinking"])
@MODES
def test_leading_think_block_is_reasoning_not_text(tag, size, streamed):
    content = f"  <{tag}>\nplan the answer\n</{tag}>\n\nThe host has 3 open ports."
    # Tags split across pieces are the case a stream has to wait out.
    response, events = _reply("local-model", _pieces(content, size), streamed=streamed)

    assert response.reasoning == "plan the answer"
    assert response.text == "The host has 3 open ports."
    if streamed:
        texts = _deltas(events, StreamEventType.TEXT_DELTA)
        assert "".join(texts) == "The host has 3 open ports."
        assert "".join(_deltas(events, StreamEventType.REASONING_DELTA)).strip() == (
            "plan the answer"
        )


@MODES
def test_a_think_block_beside_a_routing_tool_call_leaves_no_routing_prose(streamed):
    response, events = _reply(
        "local-model",
        [
            "<think>\nThe user wants the asset. ",
            "I'll call lookup_asset.\n</think>\n\n",
        ],
        streamed=streamed,
        tool_call=_wire_call("lookup_asset", {"address": "a"}),
        finish="tool_calls",
    )

    assert response.text == ""
    assert response.reasoning == "The user wants the asset. I'll call lookup_asset."
    assert _calls(response) == [("lookup_asset", {"address": "a"})]
    assert _deltas(events, StreamEventType.TEXT_DELTA) == []


@MODES
def test_an_unclosed_leading_think_block_is_all_reasoning(streamed):
    response, _ = _reply(
        "local-model",
        ["<think>I should check the ", "ports first and then"],
        streamed=streamed,
        finish="length",
    )

    assert response.text == ""
    assert response.reasoning == "I should check the ports first and then"
    assert _final_answer_problem(response) == "output_limit"


@pytest.mark.parametrize(
    "model",
    [
        "deepseek/deepseek-r1",
        DEEPSEEK,
        GLM,
        "zai-org/GLM-4.5-Air",
        "qwen/qwen3-235b-a22b-thinking-2507",
    ],
)
@pytest.mark.parametrize("size", [1, 4])
@MODES
def test_closing_only_think_is_reasoning_for_template_thinking_models(
    model, size, streamed
):
    """Their chat templates write the opening ``<think>`` into the prompt."""

    content = "The operator wants the value, I should answer briefly.\n</think>\n\nThe value is a."
    response, events = _reply(model, _pieces(content, size), streamed=streamed)

    assert (
        response.reasoning == "The operator wants the value, I should answer briefly."
    )
    assert response.text == "The value is a."
    if streamed:
        assert "".join(_deltas(events, StreamEventType.TEXT_DELTA)) == "The value is a."


@MODES
def test_closing_only_think_is_left_alone_for_other_models(streamed):
    content = "Close your thought with </think> and then answer."
    response, events = _reply(
        "meta-llama/llama-3.3-70b-instruct", _pieces(content, 4), streamed=streamed
    )

    assert response.text == content
    assert response.reasoning == ""
    if streamed:
        # Nothing waits for a close that only template-thinking models write.
        assert len(_deltas(events, StreamEventType.TEXT_DELTA)) > 1


@MODES
def test_closing_only_think_is_left_alone_when_the_route_sent_reasoning(streamed):
    """A route that parsed the thought never leaves the template's close."""

    content = "Close your thought with </think> and then answer."
    response, events = _reply(
        DEEPSEEK,
        _pieces(content, 4),
        streamed=streamed,
        reasoning="Answer the tag question.",
    )

    assert response.text == content
    assert response.reasoning == "Answer the tag question."
    if streamed:
        assert len(_deltas(events, StreamEventType.TEXT_DELTA)) > 1


@MODES
def test_think_tags_mentioned_in_an_answer_stay_in_it(streamed):
    content = "Models wrap thoughts as <think>…</think>; the answer follows."
    response, _ = _reply(DEEPSEEK, _pieces(content, 4), streamed=streamed)

    assert response.text == content
    assert response.reasoning == ""


@MODES
def test_a_think_block_then_native_markup_is_reasoning_and_a_call(streamed):
    """A route with neither parser leaks both; markup in the thought is not a call."""

    content = (
        "<think>I could write <tool_call>other<arg_key>x</arg_key>"
        "<arg_value>1</arg_value></tool_call> but safe_read fits.</think>\n"
        + GLM_COMPACT
    )
    response, events = _reply(GLM, _pieces(content, 6), streamed=streamed)

    assert _calls(response) == [("safe_read", {"value": "a"})]
    assert response.text == ""
    assert "safe_read fits." in response.reasoning
    if streamed:
        assert not any(
            "think" in delta for delta in _deltas(events, StreamEventType.TEXT_DELTA)
        )
