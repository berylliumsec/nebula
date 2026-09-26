"""How a provider chat assembles its conversation once older messages compact.

The derived memory is history carried in the conversation, never the system
instructions; retrieved originals follow the current message; every canonical
message is sent or covered by the snapshot that is sent; the trigger counts
what the request adds beside the conversation; and the estimate is calibrated
by what the provider reported for the conversation's earlier requests.
"""

import asyncio
import json

import pytest

import nebula.v3.chat as chat_module
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.context import (
    calibrated_estimate,
    estimate_allowance,
    estimate_messages,
    estimate_model_request,
    estimate_tool_definitions,
    resolve_context_limits,
    updated_calibration,
)
from nebula.v3.domain import (
    ChatDecision,
    ChatGoal,
    ChatGoalStatus,
    ChatMessage,
    ChatRole,
    ChatSession,
    ContextSnapshot,
    Engagement,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
    RiskClass,
    ScopePolicy,
)
from nebula.v3.providers import ModelRequest, ModelResponse, ModelUsage
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_catalog import (
    catalog_components,
    catalog_instructions,
    deferrable_specs,
)
from nebula.v3.tools import ToolSpec
from tests.v3.test_chat import FakeProvider, _profile

MEMORY_HEADING = "EARLIER CONVERSATION, COMPACTED BY NEBULA"
EXCERPTS_HEADING = "RETRIEVED CANONICAL TRANSCRIPT EXCERPTS"
CODENAMES = ["amberfox", "bluejay", "copperwolf", "duskowl", "emberlynx", "frostelk"]


class ReportingProvider(FakeProvider):
    """Reports input tokens as a fixed share of Core's own estimate.

    ``summary`` is what the compactor answers with, so a test can make the
    memory as large as it needs.
    """

    def __init__(
        self,
        provider_id: str,
        *,
        ratio: float | None = None,
        summary: str = "Earlier conversation retained with provenance.",
    ) -> None:
        super().__init__(provider_id, local=True)
        self.ratio = ratio
        self.summary = summary

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation") == "context_compaction":
            self.requests.append(request)
            return ModelResponse(
                provider_id=self.config.id,
                model=request.model or "model-a",
                text=json.dumps({"summary": self.summary}),
                usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                finish_reason="stop",
            )
        response = await super().complete(request)
        if self.ratio is None or request.metadata.get("operation"):
            return response
        reported = round(estimate_model_request(request) * self.ratio)
        return response.model_copy(
            update={
                "usage": ModelUsage(
                    input_tokens=reported,
                    output_tokens=3,
                    total_tokens=reported + 3,
                    cached_input_tokens=reported // 2,
                )
            }
        )


def _chat(tmp_path, provider_factory, *, history: list[str], profile=None):
    store = NebulaStore(tmp_path / "assembly.db")
    engagement = store.create(Engagement(id="eng-assembly", name="Assembly"))
    profile = store.create(profile or _profile(local=True))
    session = store.create(
        ChatSession(
            id="session-assembly",
            engagement_id=engagement.id,
            title="Assembly",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    store.create_many(
        [
            ChatMessage(
                id=f"history-{sequence:02d}",
                engagement_id=engagement.id,
                session_id=session.id,
                sequence=sequence,
                role=ChatRole.USER if sequence % 2 else ChatRole.ASSISTANT,
                content=content,
            )
            for sequence, content in enumerate(history, start=1)
        ]
    )
    provider = provider_factory(profile.id)
    service = ChatService(store, provider_factory=lambda _: provider)
    return store, service, session, profile, provider


def _history(count: int, repeat: int = 180) -> list[str]:
    return [
        f"Historical evidence {index} about {CODENAMES[index % len(CODENAMES)]}: "
        + ("bounded context " * repeat)
        for index in range(1, count + 1)
    ]


def _send(service, session, profile, content, **fields):
    prepared = service.prepare(
        ChatCompletionRequest(
            session_id=session.id,
            provider_id=profile.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": content}],
            **fields,
        )
    )
    asyncio.run(service.complete(prepared))
    return prepared


def _turn_requests(provider) -> list[ModelRequest]:
    return [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]


def _compactions(provider) -> list[ModelRequest]:
    return [
        request
        for request in provider.requests
        if request.metadata.get("operation") == "context_compaction"
    ]


def test_post_compaction_turns_keep_a_byte_stable_request_prefix(tmp_path):
    store, service, session, profile, provider = _chat(
        tmp_path,
        lambda provider_id: ReportingProvider(provider_id),
        history=_history(40, repeat=40),
    )

    # Each turn asks about a different archived message, so each retrieves
    # different originals: they must not disturb anything before them.
    for codename in ("copperwolf", "duskowl", "emberlynx"):
        _send(service, session, profile, f"What did we find about {codename}?")

    assert len(_compactions(provider)) > 0
    first, second, third = _turn_requests(provider)
    snapshots = store.list_entities(ContextSnapshot, limit=10)
    assert len(snapshots) == 1
    for earlier, later in ((first, second), (second, third)):
        # The instructions are the system's own and do not carry the memory.
        assert later.instructions == earlier.instructions
        assert "DERIVED WORKING MEMORY" not in (later.instructions or "")
        # Everything up to the earlier request's current message is sent
        # again byte for byte; that message is replayed as stored, without
        # the originals retrieved for it.
        cut = len(earlier.messages) - 1
        assert later.messages[:cut] == earlier.messages[:cut]
        replayed = later.messages[cut].content
        assert earlier.messages[-1].content.startswith(replayed)
        assert EXCERPTS_HEADING not in replayed
        assert earlier.messages[-1].content != replayed
    for request, codename in zip(
        (first, second, third), ("copperwolf", "duskowl", "emberlynx"), strict=True
    ):
        assert request.messages[0].content.startswith(MEMORY_HEADING)
        current = request.messages[-1].content
        assert current.startswith(f"What did we find about {codename}?")
        excerpts = json.loads(current.split(EXCERPTS_HEADING, 1)[1].split("\n", 1)[1])
        assert codename in excerpts[0]["content"]


def test_messages_between_the_boundary_and_the_tail_are_never_silently_dropped(
    tmp_path,
):
    # Long saved operator context and a large memory leave the first recent
    # tail no room beside them. Core used to compact, then drop the oldest
    # tail messages to fit, so they were neither sent nor summarised.
    store, service, session, profile, provider = _chat(
        tmp_path,
        lambda provider_id: ReportingProvider(
            provider_id, summary="Long derived summary. " * 200
        ),
        history=_history(20, repeat=45),
    )
    for index in range(2):
        store.create(
            ChatDecision(
                id=f"decision-{index}",
                engagement_id=session.engagement_id,
                session_id=session.id,
                kind="question",
                text=f"Open question {index}: " + ("which scope applies? " * 180),
            )
        )
    canonical = service.session_messages(session.id)

    prepared = service.prepare(
        ChatCompletionRequest(
            session_id=session.id,
            provider_id=profile.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": "Where do we stand?"}],
        )
    )

    snapshot = prepared.context_snapshot
    assert snapshot is not None
    covered = {reference.source_id for reference in snapshot.source_references}
    sent = [str(message.content) for message in prepared.model_request.messages]
    for message in canonical:
        verbatim = any(message.content in content for content in sent)
        assert verbatim or message.id in covered, message.id
    # Nothing is both summarised and repeated either: the messages sent are
    # exactly those after the served snapshot's boundary, then the new one.
    kept = [m for m in canonical if m.sequence > snapshot.compacted_through]
    assert len(sent) == len(kept) + 1
    assert sent[-1].endswith("Where do we stand?")
    # The boundary moved past the first compaction's, which was compacted
    # again rather than cut.
    snapshots = sorted(
        store.list_entities(ContextSnapshot, limit=10), key=lambda item: item.version
    )
    assert len(snapshots) >= 2
    assert snapshots[-1].id == snapshot.id
    assert snapshot.compacted_through > snapshots[0].compacted_through
    limits = resolve_context_limits(profile)
    assert (
        estimate_messages(
            prepared.model_request.messages, prepared.model_request.instructions or ""
        )
        + chat_module.estimate_tokens(chat_module._NO_TOOL_PREFIX)
        <= limits.target_input_tokens
    )


def test_a_covered_message_edited_in_place_is_compacted_again(tmp_path):
    store, service, session, profile, provider = _chat(
        tmp_path,
        lambda provider_id: ReportingProvider(provider_id),
        history=_history(12),
    )
    first = _send(service, session, profile, "Summarise the evidence.")
    compactions = len(_compactions(provider))
    assert compactions > 0
    second = _send(service, session, profile, "And the next step?")
    assert len(_compactions(provider)) == compactions
    assert second.context_snapshot.id == first.context_snapshot.id

    covered = store.get(ChatMessage, "history-03")
    store.update(
        ChatMessage,
        covered.id,
        {"content": "Historical evidence 3 was wrong; the host is 10.0.0.9."},
        expected_revision=covered.revision,
    )
    third = _send(service, session, profile, "Which host again?")

    # Same message ids, different content: the memory no longer stands for
    # the transcript, so it is rebuilt from the edited original.
    assert len(_compactions(provider)) > compactions
    assert third.context_snapshot.id != first.context_snapshot.id
    assert third.context_snapshot.source_sha256 != first.context_snapshot.source_sha256
    assert "10.0.0.9" in json.dumps(
        [
            message.content
            for request in _compactions(provider)[compactions:]
            for message in request.messages
        ]
    )


def _tool_profile() -> ProviderProfile:
    payload = _profile(local=True).model_dump(mode="python")
    payload["capabilities"]["tool_calling"] = True
    payload["capability_verifications"] = {
        "model-a": ProviderCapabilityVerification(
            model="model-a", status=ProviderVerificationStatus.VERIFIED
        )
    }
    return ProviderProfile.model_validate(payload)


def test_tool_definitions_and_routing_instructions_count_toward_the_trigger(
    tmp_path,
):
    def tool_provider(provider_id: str) -> ReportingProvider:
        provider = ReportingProvider(provider_id)
        provider.config.capabilities.tools = True
        return provider

    # Measure what the agent-messaging tools add, then size a conversation
    # that fits the target without them and not with them.
    store, service, session, profile, provider = _chat(
        tmp_path, tool_provider, history=[], profile=_tool_profile()
    )
    probe = service.prepare(
        ChatCompletionRequest(
            session_id=session.id,
            provider_id=profile.id,
            include_knowledge=False,
            allow_agent_messaging=True,
            messages=[{"role": "user", "content": "probe"}],
        )
    )
    reserved = probe.context_reserved_tokens
    assert reserved is not None and reserved > 500
    service._fail_closed_turn(
        probe, status=chat_module.ChatTurnStatus.CANCELLED, error="probe"
    )
    limits = resolve_context_limits(profile)
    history = _history(12, repeat=1)
    per_message = (limits.target_input_tokens - reserved // 2) // len(history)
    history = [
        text + " padding" * max(0, (per_message * 3 - len(text)) // 8)
        for text in history
    ]
    store.create_many(
        [
            ChatMessage(
                id=f"sized-{sequence:02d}",
                engagement_id=session.engagement_id,
                session_id=session.id,
                sequence=sequence,
                role=ChatRole.USER if sequence % 2 else ChatRole.ASSISTANT,
                content=content,
            )
            for sequence, content in enumerate(history, start=1)
        ]
    )
    request = ChatCompletionRequest(
        session_id=session.id,
        provider_id=profile.id,
        include_knowledge=False,
        allow_agent_messaging=True,
        messages=[{"role": "user", "content": "Continue."}],
    )
    without_tools = service.prepare(
        request.model_copy(update={"allow_agent_messaging": False})
    )
    assert without_tools.context_snapshot is None

    prepared = service.prepare(request)
    assert prepared.context_snapshot is not None
    asyncio.run(service.complete(prepared))
    routing = next(request for request in _turn_requests(provider) if request.tools)
    # The request as sent, tools and routing instructions included, now
    # fits the target the trigger is meant to keep.
    assert estimate_model_request(routing) <= limits.target_input_tokens
    assert reserved >= (
        estimate_model_request(routing)
        - estimate_messages(routing.messages, prepared.model_request.instructions or "")
    )
    status = service.context_status(session.id)
    assert status.reserved_input_tokens == prepared.context_reserved_tokens
    assert status.estimated_input_tokens > estimate_messages(
        routing.messages, prepared.model_request.instructions or ""
    )


def test_calibration_is_learned_once_per_turn_from_reported_usage(tmp_path):
    store, service, session, profile, provider = _chat(
        tmp_path,
        lambda provider_id: ReportingProvider(provider_id, ratio=0.75),
        history=_history(4),
    )

    prepared = _send(service, session, profile, "First question")
    after = store.get(ChatSession, session.id)
    record = after.metadata["context_calibration"]
    assert record["runtimes"] == [
        {
            "provider_profile_id": profile.id,
            "model": "model-a",
            "factor": pytest.approx(0.75, abs=0.001),
            "samples": 1,
        }
    ]
    assert record["reserved_input_tokens"] == prepared.context_reserved_tokens
    message = service.session_messages(session.id)[-1]
    request = message.metadata["last_provider_request"]
    assert request["reported_input_tokens"] >= 1_000
    assert request["reported_cached_input_tokens"] == (
        request["reported_input_tokens"] // 2
    )

    provider.ratio = 0.4
    second = _send(service, session, profile, "Second question")
    latest = store.get(ChatSession, session.id)
    # The accounting rides the turn's own session write: none of its own.
    assert latest.revision == after.revision + 1
    runtimes = latest.metadata["context_calibration"]["runtimes"]
    # A sample below the floor counts as the floor; the factor moves halfway.
    assert runtimes[0]["factor"] == pytest.approx((0.75 + 0.6) / 2, abs=0.001)
    assert runtimes[0]["samples"] == 2
    assert second.estimate_calibration == pytest.approx(0.75, abs=0.001)

    status = service.context_status(session.id)
    assert status.estimate_calibration == pytest.approx(0.675, abs=0.001)


def test_small_reports_do_not_calibrate():
    assert updated_calibration(None, estimated=900, reported=500) is None
    assert updated_calibration(0.9, estimated=900, reported=999) == 0.9
    assert updated_calibration(None, estimated=1_000, reported=3_000) == 1.5
    assert updated_calibration(1.2, estimated=2_000, reported=1_000) == 0.9


def test_calibration_moves_the_trigger_and_the_meter_but_not_below_the_hard_floor(
    tmp_path,
):
    store, service, session, profile, provider = _chat(
        tmp_path, lambda provider_id: ReportingProvider(provider_id), history=[]
    )
    limits = resolve_context_limits(profile)
    # About 1.25 times the target by Core's raw estimate, and 0.75 of it as
    # the provider counts this conversation.
    history = _history(8, repeat=1)
    size = (limits.target_input_tokens * 5 // 4) // len(history)
    history = [text + " x" * max(0, (size * 3 - len(text)) // 2) for text in history]
    store.create_many(
        [
            ChatMessage(
                id=f"calibrated-{sequence:02d}",
                engagement_id=session.engagement_id,
                session_id=session.id,
                sequence=sequence,
                role=ChatRole.USER if sequence % 2 else ChatRole.ASSISTANT,
                content=content,
            )
            for sequence, content in enumerate(history, start=1)
        ]
    )
    uncalibrated = service.context_status(session.id)
    assert uncalibrated.estimate_calibration is None
    assert uncalibrated.estimated_input_tokens > limits.target_input_tokens
    assert uncalibrated.status == "stale"

    latest = store.get(ChatSession, session.id)
    store.update(
        ChatSession,
        session.id,
        {
            "metadata": {
                **latest.metadata,
                "context_calibration": {
                    "runtimes": [
                        {
                            "provider_profile_id": profile.id,
                            "model": "model-a",
                            "factor": 0.6,
                            "samples": 3,
                        }
                    ],
                    "reserved_input_tokens": 0,
                },
            }
        },
        expected_revision=latest.revision,
    )
    calibrated = service.context_status(session.id)
    assert calibrated.estimate_calibration == 0.6
    assert calibrated.estimated_input_tokens == calibrated_estimate(
        uncalibrated.estimated_input_tokens, 0.6
    )
    assert calibrated.status == "not_needed"

    prepared = service.prepare(
        ChatCompletionRequest(
            session_id=session.id,
            provider_id=profile.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": "Go on."}],
        )
    )
    # Counted as the provider counts it, the conversation fits: no compaction.
    assert prepared.context_snapshot is None
    assert _compactions(provider) == []
    assert prepared.estimate_calibration == 0.6

    # A capacity check never scales below 0.8 of the raw estimate.
    assert estimate_allowance(1_000, 0.6, hard=True) == 1_250
    assert estimate_allowance(1_000, 0.6) == 1_666
    assert calibrated_estimate(1_000, 0.6, hard=True) == 800
    assert calibrated_estimate(1_000, 1.2, hard=True) == 1_200
    oversized = prepared.model_request.model_copy(
        update={
            "messages": [
                chat_module.ModelMessage(
                    role="user", content="y" * (limits.input_capacity * 3 * 13 // 10)
                )
            ]
        }
    )
    assert not service._fits_request_capacity(profile, oversized, 0.6)
    assert service._fits_request_capacity(profile, oversized, None) is False
    assert service._fits_request_capacity(
        profile,
        oversized.model_copy(
            update={
                "messages": [
                    chat_module.ModelMessage(
                        role="user", content="y" * (limits.input_capacity * 3 * 6 // 5)
                    )
                ]
            }
        ),
        0.8,
    )


def test_compaction_is_guided_by_the_goal_not_the_latest_message(tmp_path):
    store, service, session, profile, provider = _chat(
        tmp_path,
        lambda provider_id: ReportingProvider(provider_id),
        history=_history(12),
    )

    _send(service, session, profile, "thanks")
    objective = json.loads(_compactions(provider)[0].messages[0].content)["objective"]
    assert objective is None

    goal = store.create(
        ChatGoal(
            id="goal-assembly",
            engagement_id=session.engagement_id,
            session_id=session.id,
            objective="Map every exposed admin interface",
            completion_criteria=["Each interface is listed"],
            status=ChatGoalStatus.RUNNING,
        )
    )
    # Edited archived messages make the next turn compact afresh, now for
    # the goal.
    for message in service.session_messages(session.id)[:4]:
        store.update(
            ChatMessage,
            message.id,
            {"content": message.content + " (revised)"},
            expected_revision=message.revision,
        )
    before = len(_compactions(provider))
    _send(service, session, profile, "ok", goal_id=goal.id, stream=True)
    compaction = _compactions(provider)[before]
    assert (
        json.loads(compaction.messages[0].content)["objective"]
        == "Map every exposed admin interface"
    )


def test_the_reserve_covers_the_largest_catalog_picks_routing_can_send(tmp_path):
    def spec(name: str, source: str | None, fields: int) -> ToolSpec:
        return ToolSpec(
            name=name,
            description=f"{name} does one bounded thing. " * 4,
            input_schema={
                "type": "object",
                "properties": {
                    f"field_{index}": {"type": "string", "description": "A value."}
                    for index in range(fields)
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            risk_class=RiskClass.LOCAL_READ,
            source_id=source,
        )

    specs = {
        item.name: item
        for item in (
            spec("safe_read", None, 2),
            *(
                spec(f"mcp.remote.tool_{index}", "mcp:remote", index)
                for index in range(8)
            ),
        )
    }
    components = RuntimeToolComponents(
        broker=None,
        scope=ScopePolicy(engagement_id="eng-assembly"),
        workspace=tmp_path,
        specs=specs,
    )
    deferred = deferrable_specs(specs)
    assert len(deferred) == 8

    reserve = ChatService._tool_request_reserve(components, deferred, (), None)

    # Whatever the ranker picks, routing adds no more than was reserved.
    catalog = catalog_components(components, deferred=deferred)
    assert catalog is not None
    sent = {
        **{name: item for name, item in specs.items() if name not in deferred},
        **catalog.specs,
    }
    tools = estimate_tool_definitions(ChatService._routing_tools(sent.values()))
    prefix = chat_module.estimate_tokens(
        chat_module._routing_instructions(sent, None) + "\n\n"
    )
    for picks in (
        {"preloaded": ["mcp.remote.tool_7", "mcp.remote.tool_6"], "suggested": []},
        {
            "preloaded": ["mcp.remote.tool_0"],
            "suggested": [f"mcp.remote.tool_{index}" for index in range(1, 6)],
        },
    ):
        added = catalog_instructions({"deferred": sorted(deferred), **picks}, specs)
        assert tools + prefix + chat_module.estimate_tokens(added) <= reserve
