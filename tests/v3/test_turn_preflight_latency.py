"""Work before a turn's first provider call, and how long a routing step may wait.

Tool ranking (Jev or the local index) reads only the operator's words, the
selected skills and the catalog, so it overlaps knowledge planning and context
compaction instead of queueing behind them. A covering context snapshot keeps
serving while everything after its boundary still fits, rather than the whole
archive being summarised again whenever the recent tail moves. Knowledge
planning and resent generations are bounded, and every routing step records
when its provider request left and answered.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

import nebula.v3.chat as chat_module
from nebula.v3.chat import ChatCompletionRequest, ChatPrivacyError, ChatService
from nebula.v3.database import ChatTurnStepEventRow
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatSession,
    Engagement,
    KnowledgeSource,
)
from nebula.v3.providers import (
    ModelMessage,
    ModelRequest,
    ProviderOverloadedError,
    ToolCall,
    build_provider,
    provider_from_profile,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider, _profile
from tests.v3.test_chat_tool_loop import RecordingBroker, ScriptedProvider
from tests.v3.test_chat_tool_loop import _prepared as _tool_loop
from tests.v3.test_chat_tool_loop import _response
from tests.v3.test_inference_timeouts_probe_budget import KEY_ENV
from tests.v3.test_inference_timeouts_probe_budget import _profile as _inference
from tests.v3.test_tool_suggestions import (
    MCP_TOOL,
    _client,
    _jev_answers,
    _mcp_service,
)


class _Timeline:
    def __init__(self) -> None:
        self.marks: dict[str, float] = {}

    def mark(self, name: str) -> None:
        self.marks[name] = time.perf_counter()


class _PlanningProvider(FakeProvider):
    """Plans retrieval slowly and answers routing without a tool."""

    def __init__(self, timeline: _Timeline, *, plan_seconds: float) -> None:
        super().__init__("provider-a", local=True)
        self.config.capabilities.tools = True
        self.config.capabilities.strict_tools = True
        self.timeline = timeline
        self.plan_seconds = plan_seconds

    async def complete(self, request):
        if request.metadata.get("operation") == "agentic_knowledge_retrieval":
            self.timeline.mark("plan_started")
            await asyncio.sleep(self.plan_seconds)
            self.timeline.mark("plan_finished")
        return await super().complete(request)


def _knowledge_service(tmp_path, monkeypatch, jev, *, plan_seconds=0.2):
    timeline = _Timeline()
    service, request = _mcp_service(tmp_path, monkeypatch, lambda: _client(jev))
    provider = _PlanningProvider(timeline, plan_seconds=plan_seconds)
    service.provider_factory = lambda _: provider
    service.store.create(
        KnowledgeSource(
            id="source-a",
            engagement_id=request.engagement_id,
            name="notes.txt",
            source_type="text/plain",
            artifact_id="artifact-a",
            citation="Uploaded notes",
            metadata={
                "chunks": [{"id": "chunk-a", "text": "login bug notes", "page": 1}]
            },
        )
    )
    return service, request.model_copy(update={"include_knowledge": True}), timeline


def test_tool_ranking_overlaps_knowledge_planning(tmp_path, monkeypatch):
    timeline = None

    async def jev(_request):
        timeline.mark("jev_started")
        await asyncio.sleep(0.2)
        timeline.mark("jev_finished")
        return httpx.Response(200, json=_jev_answers({MCP_TOOL: 0.8}))

    service, request, timeline = _knowledge_service(tmp_path, monkeypatch, jev)

    prepared = service.prepare(request)

    marks = timeline.marks
    # Both round trips were in flight at once; the turn used to wait for the
    # plan, then for the ranking.
    assert marks["jev_started"] < marks["plan_finished"]
    assert marks["plan_started"] < marks["jev_finished"]
    snapshot = prepared.turn.request_snapshot
    assert snapshot["tool_suggestions"]["preloaded"] == [MCP_TOOL]
    assert snapshot["tool_catalog"]["ranker"] == "jev"
    assert snapshot["preparation"]["duration_ms"] >= 200
    assert snapshot["preparation"]["started_at"] < prepared.turn.queued_at.isoformat()


def test_a_failed_preparation_cancels_its_pending_ranking(tmp_path, monkeypatch):
    cancelled = asyncio.Event()

    async def jev(_request):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("the ranking outlived its turn")

    service, request, _ = _knowledge_service(tmp_path, monkeypatch, jev)

    def refuse(*_args, **_kwargs):
        raise ChatPrivacyError("selected knowledge is local-only")

    service._retrieve = refuse

    async def prepare():
        with pytest.raises(ChatPrivacyError):
            await service.prepare_async(request)
        # Nothing of the failed turn is left running.
        assert cancelled.is_set()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    started = time.perf_counter()
    asyncio.run(prepare())
    assert time.perf_counter() - started < 5


def test_slow_knowledge_planning_falls_back_to_the_operators_words(
    tmp_path, monkeypatch
):
    async def jev(_request):
        return httpx.Response(200, json=_jev_answers({MCP_TOOL: 0.8}))

    service, request, timeline = _knowledge_service(
        tmp_path, monkeypatch, jev, plan_seconds=30
    )
    monkeypatch.setattr(chat_module, "_RETRIEVAL_PLAN_TIMEOUT_SECONDS", 0.1)
    searched = []
    original = service._retrieve

    def retrieve(engagement_id, queries, **kwargs):
        searched.append(queries)
        return original(engagement_id, queries, **kwargs)

    service._retrieve = retrieve

    started = time.perf_counter()
    prepared = service.prepare(request)

    # The turn exists without waiting out the provider's request timeout.
    assert time.perf_counter() - started < 5
    assert "plan_finished" not in timeline.marks
    assert searched == [["find the login bug"]]
    assert prepared.turn is not None


def test_jev_sees_each_operator_message_once_when_a_client_replays_history(
    tmp_path, monkeypatch
):
    states = []

    def jev(request):
        states.append(json.loads(request.content)["state"])
        return httpx.Response(200, json=_jev_answers({MCP_TOOL: 0.8}))

    service, request = _mcp_service(tmp_path, monkeypatch, lambda: _client(jev))
    store = service.store
    history = [
        {"role": "user", "content": "list the open issues"},
        {"role": "assistant", "content": "There are two."},
    ]
    for session_id in ("session-replayed", "session-appended"):
        store.create(
            ChatSession(
                id=session_id,
                engagement_id=request.engagement_id,
                title="Same history",
                provider_profile_id=request.provider_id,
                model="model-a",
            )
        )
        store.create_many(
            [
                ChatMessage(
                    engagement_id=request.engagement_id,
                    session_id=session_id,
                    sequence=index + 1,
                    role=ChatRole(item["role"]),
                    content=item["content"],
                )
                for index, item in enumerate(history)
            ]
        )
    asked = {"role": "user", "content": "find the login bug"}

    def ask(session_id, messages):
        return service.prepare(
            ChatCompletionRequest(
                provider_id=request.provider_id,
                engagement_id=request.engagement_id,
                session_id=session_id,
                mcp_server_ids=request.mcp_server_ids,
                messages=messages,
                include_knowledge=False,
                stream=True,
            )
        )

    # One client replays the transcript, another sends only the new message.
    ask("session-replayed", [*history, asked])
    appended = ask("session-appended", [asked])

    # Replaying used to repeat every earlier operator message, so Jev read a
    # different request and the identical one missed its cache.
    assert states == [
        {
            "operator_request": "find the login bug",
            "earlier_operator_messages": ["list the open issues"],
        }
    ]
    assert appended.turn.request_snapshot["tool_suggestions"]["cached"] is True


def test_routing_steps_record_their_provider_request_timing(tmp_path):
    class SlowRouting(ScriptedProvider):
        async def complete(self, request):
            if request.metadata.get("operation") != "conversation_naming":
                await asyncio.sleep(0.05)
            return await super().complete(request)

    responses = [
        _response(
            calls=[
                ToolCall(id="call-1", name="safe_read", arguments={"value": "a"}),
                ToolCall(id="call-2", name="safe_read", arguments={"value": "b"}),
            ]
        ),
        _response(
            calls=[ToolCall(id="call-3", name="safe_read", arguments={"value": "c"})]
        ),
        _response(text="Read all three."),
    ]
    store, service, prepared, _ = _tool_loop(tmp_path, responses, RecordingBroker())
    prepared.provider = SlowRouting(responses)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Read all three."
    with store.database.session() as session:
        rows = list(
            session.scalars(
                select(ChatTurnStepEventRow)
                .where(ChatTurnStepEventRow.turn_id == "turn")
                .order_by(ChatTurnStepEventRow.sequence)
            )
        )
    # Every row of a step carries the routing response that issued it: the
    # batched pair shares the first, the third call the second.
    assert {(row.step, row.provider_group) for row in rows} == {
        (0, 1),
        (1, 1),
        (2, 2),
    }
    for row in rows:
        payload = row.payload
        requested = _at(payload["provider_requested_at"])
        responded = _at(payload["provider_responded_at"])
        assert requested < responded <= _at(row.occurred_at)
        assert payload["provider_latency_ms"] >= 50
    started = {row.step: row for row in rows if row.event_type == "started"}
    completed = {row.step: row for row in rows if row.event_type == "complete"}
    # Core's own time between one step finishing and the next request leaving
    # is now measurable from the ledger alone.
    assert _at(completed[1].occurred_at) <= _at(
        started[2].payload["provider_requested_at"]
    )


def _at(value: str | datetime) -> datetime:
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _stalling(delays: list[float], failure: type[httpx.HTTPError]):
    """A transport whose attempts wait ``delays`` seconds and then fail."""

    requests: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(time.perf_counter())
        await asyncio.sleep(delays[min(len(requests), len(delays)) - 1])
        raise failure("upstream stalled", request=request)

    return requests, httpx.MockTransport(handler)


def _generation(transport, **options):
    profile = _inference("vllm", **options)
    return build_provider(provider_from_profile(profile).config, transport=transport)


def _hello() -> ModelRequest:
    return ModelRequest(
        model="m", messages=[ModelMessage(role="user", content="hello")]
    )


def test_a_stalled_generation_is_not_resent_once_its_allowance_is_spent(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")
    # The read timeout fires when the allowance runs out.
    requests, transport = _stalling([0.3], httpx.ReadTimeout)
    provider = _generation(transport, request_timeout_seconds=0.3)

    started = time.perf_counter()
    with pytest.raises(ProviderOverloadedError, match="timed out"):
        asyncio.run(provider.complete(_hello()))

    # Three attempts used to wait three full allowances (3 x 600 s by default).
    assert len(requests) == 1
    assert time.perf_counter() - started < 0.6


def test_a_resend_gets_only_what_is_left_of_the_allowance(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")
    # The connection tears early, and the resend hangs.
    requests, transport = _stalling([0.05, 30], httpx.RemoteProtocolError)
    provider = _generation(transport, request_timeout_seconds=0.4)

    started = time.perf_counter()
    with pytest.raises(ProviderOverloadedError, match="0.4 s request allowance"):
        asyncio.run(provider.complete(_hello()))

    assert len(requests) == 2
    assert time.perf_counter() - started < 2


def test_a_quick_transient_failure_is_still_resent(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.RemoteProtocolError("torn", request=request)
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": "m",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    provider = _generation(httpx.MockTransport(handler), request_timeout_seconds=5)

    assert asyncio.run(provider.complete(_hello())).text == "ok"
    assert len(attempts) == 2


def _long_chat(tmp_path: Path):
    store = NebulaStore(tmp_path / "long-chat.db")
    engagement = store.create(Engagement(id="eng-long", name="Long chat"))
    profile = store.create(_profile(local=True))
    session = store.create(
        ChatSession(
            id="session-long",
            engagement_id=engagement.id,
            title="Long chat",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    store.create_many(
        [
            ChatMessage(
                id=f"long-{sequence:02d}",
                engagement_id=engagement.id,
                session_id=session.id,
                sequence=sequence,
                role=ChatRole.USER if sequence % 2 else ChatRole.ASSISTANT,
                content=f"Historical evidence {sequence}: "
                + ("bounded context " * 180),
            )
            for sequence in range(1, 13)
        ]
    )
    provider = FakeProvider(profile.id, local=True)
    service = ChatService(store, provider_factory=lambda _: provider)
    return store, service, session, profile, provider


def _turn(service, session, profile, provider, content):
    before = len(provider.requests)
    prepared = service.prepare(
        ChatCompletionRequest(
            session_id=session.id,
            provider_id=profile.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": content}],
        )
    )
    asyncio.run(service.complete(prepared))
    compactions = [
        request
        for request in provider.requests[before:]
        if request.metadata.get("operation") == "context_compaction"
    ]
    return prepared, len(compactions)


FOLLOW_UP = "more detail " * 150


def test_a_covering_snapshot_serves_turns_until_the_tail_outgrows_it(tmp_path):
    store, service, session, profile, provider = _long_chat(tmp_path)
    limits = chat_module.resolve_context_limits(profile)

    first, compacted = _turn(service, session, profile, provider, FOLLOW_UP)
    assert compacted > 0
    snapshot = first.context_snapshot

    calls = []
    for index in range(4):
        prepared, compacted = _turn(
            service, session, profile, provider, f"Follow-up {index}: {FOLLOW_UP}"
        )
        calls.append(compacted)
        # Every message after the snapshot's boundary is sent as it is, the
        # first led by the unchanged memory, and the whole input still fits.
        assert prepared.context_snapshot.id == snapshot.id
        assert prepared.context_usage.total_tokens == 0
        after = [
            message.content
            for message in service.session_messages(session.id)
            if message.sequence > snapshot.compacted_through
        ]
        sent = prepared.model_request.messages
        assert [message.content for message in sent] == [
            chat_module._compacted_memory_text(snapshot.memory, after[0]),
            *after[1 : len(sent)],
        ]
        assert sent[-1].content.startswith(f"Follow-up {index}")
        assert (
            chat_module.estimate_messages(sent, prepared.model_request.instructions)
            <= limits.target_input_tokens
        )
        assert service.context_status(session.id).status == "ready"
    # The tail moved past the snapshot's boundary on these turns, and each
    # used to summarise the whole archive again; now the history kept word
    # for word outgrows the recent-tail share before anything compacts anew.
    assert calls == [0, 0, 0, 0]
    assert (
        chat_module.estimate_messages(sent[:-1], "")
        > limits.target_input_tokens * 2 // 5
    )

    # Then a later boundary serves: compacted by the turn that needed it, or
    # in the background after the turn before.
    for _ in range(20):
        prepared, compacted = _turn(
            service, session, profile, provider, f"Later: {FOLLOW_UP}"
        )
        if prepared.context_snapshot.id != snapshot.id:
            break
    assert prepared.context_snapshot.compacted_through > snapshot.compacted_through


def test_a_snapshot_over_a_retracted_message_is_not_reused(tmp_path):
    store, service, session, profile, provider = _long_chat(tmp_path)
    first, _ = _turn(service, session, profile, provider, FOLLOW_UP)
    for index in range(2):
        _, compacted = _turn(service, session, profile, provider, FOLLOW_UP)
        assert compacted == 0
    covered = store.get(ChatMessage, "long-03")
    store.update(
        ChatMessage,
        covered.id,
        {"metadata": {**covered.metadata, "retracted_at": "2026-09-24T00:00:00Z"}},
        expected_revision=covered.revision,
    )

    prepared, compacted = _turn(service, session, profile, provider, FOLLOW_UP)

    # The memory still summarises the retracted message, so it is rebuilt.
    assert compacted > 0
    assert prepared.context_snapshot.id != first.context_snapshot.id
    assert "long-03" not in {
        reference.source_id for reference in prepared.context_snapshot.source_references
    }
