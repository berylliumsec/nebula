import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable

from nebula.v3.artifacts import ArtifactStore
import pytest

from nebula.v3.chat import (
    ChatCompletionRequest,
    ChatService,
    ChatToolResultConsentRequired,
)
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.chat_turn_ledger import turn_history
from nebula.v3.domain import (
    Approval,
    ChatGoalUsageCharge,
    ChatMessage,
    ChatSession,
    ChatSubagent,
    ChatSubagentMessage,
    ChatSubagentMessageStatus,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
    RiskClass,
    utc_now,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
    ToolCall,
    ToolChoice,
    StreamEventType,
)
from nebula.v3.storage import ConflictError, NebulaStore

CHILD_MARKER = "You are a subagent."


def test_graceful_core_update_resumes_safe_child_before_reporting_to_parent(tmp_path):
    class WaitingProvider(RoutedProvider):
        async def stream(self, request: ModelRequest):
            del request
            yield ModelStreamEvent(type=StreamEventType.STARTED)
            await asyncio.Event().wait()

    async def scenario() -> None:
        store = NebulaStore(tmp_path / "child-core-update.db")
        engagement = store.create(Engagement(name="Child recovery"))
        profile = store.create(
            ProviderProfile(
                name="Local provider",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a"],
                metadata={"default_model": "model-a"},
            )
        )
        parent_session = store.create(
            ChatSession(
                engagement_id=engagement.id,
                title="Supervisor",
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
        parent = store.create(
            ChatTurn(
                engagement_id=engagement.id,
                session_id=parent_session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.WAITING_CALLBACK,
            )
        )
        child_session = store.create(
            ChatSession(
                engagement_id=engagement.id,
                title="Subagent",
                provider_profile_id=profile.id,
                model="model-a",
                parent_session_id=parent_session.id,
            )
        )
        reason = "Core stopped before this response completed. Review and resume it."
        child = store.create(
            ChatTurn(
                engagement_id=engagement.id,
                session_id=child_session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.INTERRUPTED,
                error=reason,
                reasoning="I should check the assigned item.",
                request_snapshot={
                    "subagent_child": True,
                    "model_request": ModelRequest(
                        model="model-a",
                        messages=[{"role": "user", "content": "Investigate."}],
                    ).model_dump(mode="json"),
                    "context_usage": {},
                    "recovery": {
                        "required": True,
                        "unknown_tool_call_ids": [],
                        "unknown_hook_execution_ids": [],
                    },
                },
            )
        )
        record = store.create(
            ChatSubagent(
                engagement_id=engagement.id,
                parent_session_id=parent_session.id,
                parent_turn_id=parent.id,
                child_session_id=child_session.id,
                child_turn_id=child.id,
                provider_profile_id=profile.id,
                model="model-a",
                name="Investigator",
                task="Investigate the assigned item.",
            )
        )
        provider = WaitingProvider([], [])
        service = ChatService(store, provider_factory=lambda _: provider)
        await service.startup()
        assert store.get(ChatSubagent, record.id).status == ChatSubagentStatus.RUNNING
        assert service.resume_turns_stopped_by_core() == [child.id]
        await service.subagents.reconcile_after_restart()
        assert store.get(ChatSubagent, record.id).status == ChatSubagentStatus.RUNNING
        assert store.get(ChatTurn, parent.id).status == ChatTurnStatus.WAITING_CALLBACK
        assert service.has_active_provider_turn(child.id)
        await service.shutdown()

    asyncio.run(scenario())


def _response(*, calls: list[ToolCall] | None = None, text: str = "") -> ModelResponse:
    return ModelResponse(
        provider_id="provider",
        model="model-a",
        text=text,
        tool_calls=calls or [],
        usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        finish_reason="tool_calls" if calls else "stop",
    )


def _call(call_id: str, tool: str, **arguments) -> ModelResponse:
    return _response(calls=[ToolCall(id=call_id, name=tool, arguments=arguments)])


def _finish(call_id: str) -> ModelResponse:
    return _call(call_id, "finish_response")


Scripted = ModelResponse | Callable[[ModelRequest], Awaitable[ModelResponse]]


class RoutedProvider(ModelProvider):
    """Serve parent and subagent requests from separate scripts.

    A script entry is a response, or an async function of the request that
    returns one, for a step that must wait on or inspect what happened.
    """

    def __init__(self, parent: list[Scripted], child: list[Scripted]) -> None:
        super().__init__(
            ProviderConfig(
                id="provider",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(
                    streaming=True, tools=True, strict_tools=True
                ),
            )
        )
        self.parent = list(parent)
        self.child = list(child)
        self.parent_requests: list[ModelRequest] = []
        self.child_requests: list[ModelRequest] = []
        self.child_gate: asyncio.Event | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation") == "conversation_naming":
            return _response(text="Named")
        if CHILD_MARKER in (request.instructions or ""):
            self.child_requests.append(request)
            if self.child_gate is not None:
                await self.child_gate.wait()
            if not self.child:
                raise AssertionError("child script was exhausted")
            upcoming = self.child[0]
            if (
                request.tool_choice != ToolChoice.NONE
                and isinstance(upcoming, ModelResponse)
                and not upcoming.tool_calls
            ):
                # Every subagent routes tools, so a scripted answer first
                # finishes routing. The synthesis keeps its tools declared
                # with calling off.
                return _finish(f"child-finish-{len(self.child_requests)}")
            return await _play(self.child.pop(0), request)
        self.parent_requests.append(request)
        if not self.parent:
            raise AssertionError("parent script was exhausted")
        return await _play(self.parent.pop(0), request)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id="provider", healthy=True, models=["model-a"])


async def _play(entry: Scripted, request: ModelRequest) -> ModelResponse:
    return entry if isinstance(entry, ModelResponse) else await entry(request)


def _setup(tmp_path: Path, provider: RoutedProvider):
    store = NebulaStore(tmp_path / "subagents.db")
    project = store.create(Engagement(id="project", name="Subagents"))
    profile = store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"streaming": True, "tool_calling": True},
            capability_verifications={
                "model-a": ProviderCapabilityVerification(
                    model="model-a", status=ProviderVerificationStatus.VERIFIED
                )
            },
        )
    )
    chat = ChatService(store, provider_factory=lambda _: provider, worker_id="worker")
    return store, project, profile, chat


def _request(
    project: Engagement, *, content: str, session_id: str | None = None, **flags
):
    return ChatCompletionRequest(
        provider_id="provider",
        engagement_id=project.id,
        session_id=session_id,
        model="model-a",
        messages=[{"role": "user", "content": content}],
        include_knowledge=False,
        stream=True,
        **flags,
    )


async def _drain(chat: ChatService, turn_id: str) -> list[str]:
    return [event async for event, _ in chat.follow_provider_turn(turn_id)]


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.01)


def _messages(store: NebulaStore, session_id: str) -> list[ChatMessage]:
    return sorted(
        (
            item
            for item in store.list_entities(ChatMessage, limit=1_000)
            if item.session_id == session_id
        ),
        key=lambda item: item.sequence,
    )


def test_child_approval_is_delivered_to_its_active_supervisor(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider([], [])
        store, project, profile, chat = _setup(tmp_path, provider)
        parent_session = store.create(
            ChatSession(
                engagement_id=project.id,
                title="Supervisor",
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
        parent_turn = store.create(
            ChatTurn(
                engagement_id=project.id,
                session_id=parent_session.id,
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
        child_session = store.create(
            ChatSession(
                engagement_id=project.id,
                title="Subagent",
                provider_profile_id=profile.id,
                model="model-a",
                parent_session_id=parent_session.id,
                metadata={"subagent_id": "child-record"},
            )
        )
        approval = store.create(
            Approval(
                engagement_id=project.id,
                run_id="child-turn",
                risk_class=RiskClass.LOCAL_READ,
                exact_request={
                    "tool_name": "safe_read",
                    "arguments": {"path": "/tmp/item"},
                },
                policy_rationale="approval boundary",
                requested_by="chat-assistant",
            )
        )
        child_turn = store.create(
            ChatTurn(
                id="child-turn",
                engagement_id=project.id,
                session_id=child_session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.WAITING_APPROVAL,
                approval_id=approval.id,
                tool_history=[
                    {
                        "name": "safe_read",
                        "arguments": {"path": "/tmp/item"},
                        "status": "waiting_approval",
                    }
                ],
            )
        )
        record = store.create(
            ChatSubagent(
                id="child-record",
                engagement_id=project.id,
                parent_session_id=parent_session.id,
                parent_turn_id=parent_turn.id,
                child_session_id=child_session.id,
                child_turn_id=child_turn.id,
                provider_profile_id=profile.id,
                model="model-a",
                name="Reader",
                task="Read the item.",
            )
        )

        await chat.subagents.turn_settled(child_turn.id)

        (notice,) = store.list_entities(ChatSubagentMessage)
        assert notice.subagent_id == record.id
        assert "You own the child lifecycle" in notice.content
        assert "stop_subagent" in notice.content
        assert (
            store.get(ChatTurn, child_turn.id).status == ChatTurnStatus.WAITING_APPROVAL
        )
        assert store.get(ChatSubagent, record.id).status == ChatSubagentStatus.RUNNING

        active_parent = store.get(ChatTurn, parent_turn.id)
        store.update(
            ChatTurn,
            active_parent.id,
            {"status": ChatTurnStatus.COMPLETE},
            expected_revision=active_parent.revision,
        )
        await chat.subagents.turn_settled(child_turn.id)

        assert store.get(ChatTurn, child_turn.id).status == ChatTurnStatus.CANCELLED
        assert store.get(ChatSubagent, record.id).status == ChatSubagentStatus.STOPPED
        posted = _messages(store, parent_session.id)
        assert any("You own the child lifecycle" in item.content for item in posted)
        assert any(item.metadata.get("subagent_status") == "stopped" for item in posted)
        await chat.shutdown()

    asyncio.run(scenario())


def test_subagent_tools_require_opt_in(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider([_response(text="plain")], [])
        store, project, _, chat = _setup(tmp_path, provider)
        prepared = await chat.prepare_async(_request(project, content="Hello"))
        assert prepared.tools_enabled is False
        opted_in = await chat.prepare_async(
            _request(project, content="Hello", allow_subagents=True)
        )
        assert opted_in.tools_enabled is True
        assert set(opted_in.tool_components.specs) == {
            "start_subagent",
            "wait_subagents",
            "list_subagents",
            "message_subagent",
            "stop_subagent",
        }
        assert opted_in.turn.request_snapshot["allow_subagents"] is True
        await chat.shutdown()

    asyncio.run(scenario())


def test_cloud_subagent_turn_asks_for_tool_result_consent_before_acceptance(
    tmp_path: Path,
) -> None:
    """Subagent tools send their results to the model, so a cloud turn asks.

    The composer used to leave Subagents out of its consent decision; Core then
    refused the send. The refusal is typed and comes before acceptance, so the
    client can ask the operator and send the same request again.
    """

    async def scenario() -> None:
        provider = RoutedProvider([_response(text="plain")], [])
        provider.config = provider.config.model_copy(update={"local": False})
        store = NebulaStore(tmp_path / "cloud-subagents.db")
        project = store.create(Engagement(id="project", name="Subagents"))
        store.create(
            ProviderProfile(
                id="provider",
                name="Cloud provider",
                provider_type="custom",
                endpoint="https://provider.invalid/v1",
                is_local=False,
                model_allowlist=["model-a"],
                capabilities={"streaming": True, "tool_calling": True},
                capability_verifications={
                    "model-a": ProviderCapabilityVerification(
                        model="model-a", status=ProviderVerificationStatus.VERIFIED
                    )
                },
                privacy={"permits_sensitive_data": True},
            )
        )
        chat = ChatService(store, provider_factory=lambda _: provider)

        with pytest.raises(ChatToolResultConsentRequired) as refused:
            await chat.prepare_async(
                _request(project, content="Split the work.", allow_subagents=True)
            )
        assert refused.value.code == "tool_result_consent_required"
        assert refused.value.families == ("subagents",)
        assert "Cloud provider" in refused.value._nebula_diagnostic_operator_detail
        assert store.list_entities(ChatSession, include_temporary=True) == []
        assert store.list_entities(ChatTurn) == []

        prepared = await chat.prepare_async(
            _request(
                project,
                content="Split the work.",
                allow_subagents=True,
                allow_cloud_tool_results=True,
            )
        )
        assert prepared.tools_enabled is True
        assert prepared.tool_components is not None
        assert "start_subagent" in prepared.tool_components.specs
        # Without Subagents the same cloud chat carries no tools and needs no consent.
        plain = await chat.prepare_async(_request(project, content="Just answer."))
        assert plain.tools_enabled is False
        await chat.shutdown()

    asyncio.run(scenario())


def test_wait_resumes_parent_with_report_and_posts_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Count the route files.",
                    name="Count routes",
                    context=None,
                ),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
                _finish("p3"),
                _response(text="There are three route files."),
            ],
            child=[_response(text="Found 3 route files.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        # Hold the child until the parent is paused so the wait path is exercised.
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Split the work.", allow_subagents=True)
        )
        assert prepared.tool_components is not None
        assert (
            prepared.tool_components.specs["wait_subagents"].display_name
            == "Collect delegated reports"
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        events = await _drain(chat, parent_turn_id)
        assert "callback_required" in events
        paused = store.get(ChatTurn, parent_turn_id)
        assert paused.status == ChatTurnStatus.WAITING_CALLBACK
        assert _history(store, paused)[-1]["subagent_wait"]["mode"] == "all"
        assert (
            _history(store, paused)[-1]["result_summary"]
            == "Waiting for delegated work."
        )

        provider.child_gate.set()
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE
            )
        )

        parent = store.get(ChatTurn, parent_turn_id)
        wait_entry = _history(store, parent)[1]
        assert wait_entry["name"] == "wait_subagents"
        assert wait_entry["status"] == "complete"
        assert "Found 3 route files." in wait_entry["provider_result"]
        (record,) = store.list_entities(ChatSubagent)
        assert record.status == ChatSubagentStatus.COMPLETED
        assert record.result == "Found 3 route files."
        assert record.name == "Count routes"
        child_session = store.get(ChatSession, record.child_session_id)
        assert child_session.parent_session_id == parent.session_id
        # Approval mode is the project's automation policy; the child runs in the
        # parent's project, so it inherits always/on-boundary/never unchanged.
        assert child_session.engagement_id == parent.engagement_id
        assert (
            store.get(ChatTurn, record.child_turn_id).engagement_id
            == parent.engagement_id
        )
        # Children never receive the delegation tools and are told they are subagents.
        for request in provider.child_requests:
            assert not any(
                tool.name.endswith("subagent") or tool.name == "wait_subagents"
                for tool in request.tools
            )
        await _until(
            lambda: store.get(ChatSubagent, record.id).result_message_id is not None
        )
        messages = _messages(store, parent.session_id)
        assert [item.metadata.get("kind") for item in messages][-1] == "subagent_result"
        assert messages[-1].content.startswith("Subagent finished: Count routes")
        assert messages[-2].content == "There are three route files."
        await chat.shutdown()

    asyncio.run(scenario())


def test_late_report_is_posted_after_parent_reply(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Review the auth module.",
                    name=None,
                    context="Focus on session cookies.",
                ),
                _finish("p2"),
                _response(text="Started a review; it will report back."),
            ],
            child=[_response(text="Cookies lack SameSite.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Review auth.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        parent = store.get(ChatTurn, parent_turn_id)
        assert parent.status == ChatTurnStatus.COMPLETE
        (record,) = store.list_entities(ChatSubagent)
        assert record.status == ChatSubagentStatus.RUNNING
        await _until(lambda: bool(provider.child_requests))
        assert (
            "Focus on session cookies."
            in provider.child_requests[0].messages[-1].content
        )

        provider.child_gate.set()
        await _until(
            lambda: store.get(ChatSubagent, record.id).result_message_id is not None
        )
        messages = _messages(store, parent.session_id)
        assert messages[-2].content == "Started a review; it will report back."
        assert messages[-1].metadata["subagent_status"] == "completed"
        assert "Cookies lack SameSite." in messages[-1].content
        session = store.get(ChatSession, parent.session_id)
        assert session.metadata["last_sequence"] == messages[-1].sequence
        await chat.shutdown()

    asyncio.run(scenario())


def test_stopping_parent_stops_its_subagents(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1", "start_subagent", task="Long task.", name="Slow", context=None
                ),
                _call("p2", "wait_subagents", subagent_ids=None, mode="any"),
            ],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Go.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        (record,) = store.list_entities(ChatSubagent)
        await _until(lambda: bool(provider.child_requests))

        await chat.stop_provider_turn(parent_turn_id)

        assert store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.CANCELLED
        stopped = store.get(ChatSubagent, record.id)
        assert stopped.status == ChatSubagentStatus.STOPPED
        assert (
            store.get(ChatTurn, stopped.child_turn_id).status
            == ChatTurnStatus.CANCELLED
        )
        messages = _messages(store, store.get(ChatTurn, parent_turn_id).session_id)
        assert messages[-1].metadata["subagent_status"] == "stopped"
        await chat.shutdown()

    asyncio.run(scenario())


def _fan_out(
    tmp_path: Path, count: int, **flags
) -> tuple[NebulaStore, ChatTurn, RoutedProvider]:
    """Start ``count`` subagents in one response while every child is held."""

    async def scenario() -> tuple[NebulaStore, ChatTurn, RoutedProvider]:
        starts = [
            _call(
                f"p{index}",
                "start_subagent",
                task=f"Task {index}.",
                name=None,
                context=None,
            )
            for index in range(count)
        ]
        provider = RoutedProvider(
            parent=[*starts, _finish("done"), _response(text="Fanned out.")],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Fan out.", allow_subagents=True, **flags)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        parent = store.get(ChatTurn, parent_turn_id)
        for record in store.list_entities(ChatSubagent):
            await chat.subagents.stop(record.id)
        assert {item.status for item in store.list_entities(ChatSubagent)} == {
            ChatSubagentStatus.STOPPED
        }
        await chat.shutdown()
        return store, parent, provider

    return asyncio.run(scenario())


def test_subagents_are_unlimited_unless_the_operator_sets_a_limit(
    tmp_path: Path,
) -> None:
    # More than the three-at-once and six-per-response caps that used to apply.
    store, parent, provider = _fan_out(tmp_path, 8)

    assert [entry["status"] for entry in _history(store, parent)] == ["complete"] * 8
    assert len(store.list_entities(ChatSubagent)) == 8
    assert parent.request_snapshot["max_active_subagents"] is None
    assert "running at once" not in (provider.parent_requests[0].instructions or "")


def test_operator_subagent_limit_is_reported_to_the_model(tmp_path: Path) -> None:
    store, parent, provider = _fan_out(tmp_path, 3, max_active_subagents=2)

    statuses = [entry["status"] for entry in _history(store, parent)]
    assert statuses == ["complete", "complete", "failed"]
    # A tool failure carries no exception text (docs/TOOL_FAILURE_CONTRACT.md):
    # a capacity refusal carries the limit as Core's numbers, and the
    # instructions state it too, asserted below.
    refused = json.loads(_history(store, parent)[-1]["provider_result"])
    assert refused["schema"] == "nebula.tool-failure/v1"
    assert refused["tool"] == "start_subagent"
    assert refused["category"] == "capacity_reached"
    assert refused["side_effects"] == "none"
    assert refused["retry_safe"] is True
    assert refused["limit"] == {
        "resource": "running_subagents",
        "maximum": 2,
        "current": 2,
    }
    assert len(store.list_entities(ChatSubagent)) == 2
    assert parent.request_snapshot["max_active_subagents"] == 2
    assert "at most 2 running at once" in (
        provider.parent_requests[0].instructions or ""
    )
    session = store.get(ChatSession, parent.session_id)
    assert session.metadata["max_active_subagents"] == 2


def test_restart_keeps_recoverable_subagent_round_live(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1", "start_subagent", task="Long task.", name="Slow", context=None
                ),
                _finish("p2"),
                _response(text="Delegated."),
            ],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Go.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        (record,) = store.list_entities(ChatSubagent)
        await _until(lambda: bool(provider.child_requests))

        restarted = ChatService(
            store, provider_factory=lambda _: provider, worker_id="worker-2"
        )
        # The replacement Core parks the child while the previous worker is
        # still alive. Its later unwind must not close the recoverable round.
        await restarted.startup()
        old_tasks = [
            runtime.task
            for runtime in chat._active_provider_turns.values()
            if runtime.task is not None
        ]
        for task in old_tasks:
            task.cancel()
        await asyncio.gather(*old_tasks, return_exceptions=True)

        interrupted = store.get(ChatSubagent, record.id)
        assert interrupted.status == ChatSubagentStatus.RUNNING
        assert interrupted.result_message_id is None
        child = store.get(ChatTurn, record.child_turn_id)
        assert child.status == ChatTurnStatus.INTERRUPTED
        assert child.request_snapshot["recovery"]["required"] is True
        assert restarted.subagents.view(interrupted)["status"] == "recovering"
        assert restarted.resume_turns_stopped_by_core() == [child.id]
        assert restarted.has_active_provider_turn(child.id)
        assert (
            store.get(ChatTurn, child.id).request_snapshot["recovery"]["required"]
            is False
        )
        assert restarted.subagents.view(interrupted)["status"] == "running"
        await restarted.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_restart_repairs_a_late_terminal_child_write_from_the_old_core(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1", "start_subagent", task="Long task.", name="Slow", context=None
                ),
                _finish("p2"),
                _response(text="Delegated."),
            ],
            child=[_response(text="Recovered report.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Go.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        (record,) = store.list_entities(ChatSubagent)
        await _until(lambda: bool(provider.child_requests))

        restarted = ChatService(
            store, provider_factory=lambda _: provider, worker_id="worker-2"
        )
        await restarted.startup()
        old_tasks = [
            runtime.task
            for runtime in chat._active_provider_turns.values()
            if runtime.task is not None
        ]
        for task in old_tasks:
            task.cancel()
        await asyncio.gather(*old_tasks, return_exceptions=True)

        # Reproduce the write made by a previous binary after the replacement
        # Core had already marked the child turn recoverable.
        stale = store.get(ChatSubagent, record.id)
        stale = store.update(
            ChatSubagent,
            stale.id,
            {
                "status": ChatSubagentStatus.INTERRUPTED,
                "finished_at": utc_now(),
                "error": "Core shut down while this subagent was running.",
            },
            expected_revision=stale.revision,
        )
        await chat.subagents._deliver(stale)
        stale = store.get(ChatSubagent, record.id)
        false_result_id = stale.result_message_id
        assert false_result_id is not None
        assert restarted.subagents.view(stale)["status"] == "recovering"
        assert restarted.subagents.view(stale)["finished_at"] is None

        child = store.get(ChatTurn, record.child_turn_id)
        assert restarted.resume_turns_stopped_by_core() == [child.id]
        fenced = store.get(ChatSubagent, record.id)
        assert fenced.status == ChatSubagentStatus.RUNNING
        assert fenced.finished_at is None
        assert fenced.result_message_id is None
        marker = fenced.parent_request["_core_restart_recovery"]
        assert marker["generation"] == 1
        assert marker["superseded_result_message_ids"] == [false_result_id]

        provider.child_gate.set()
        await _drain(restarted, child.id)
        completed = store.get(ChatSubagent, record.id)
        assert completed.status == ChatSubagentStatus.COMPLETED
        assert completed.result == "Recovered report."
        assert completed.result_message_id != false_result_id
        result_messages = [
            message
            for message in _messages(store, record.parent_session_id)
            if message.metadata.get("kind") == "subagent_result"
        ]
        assert [message.metadata["subagent_status"] for message in result_messages] == [
            "interrupted",
            "completed",
        ]
        assert result_messages[-1].metadata["recovered_after_core_restart"] is True
        assert result_messages[-1].content.startswith("Subagent recovered and finished")
        await restarted.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_deleting_parent_removes_finished_subagent_conversations(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Quick task.",
                    name="Quick",
                    context=None,
                ),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
                _finish("p3"),
                _response(text="Done."),
            ],
            child=[_response(text="Quick report.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        prepared = await chat.prepare_async(
            _request(project, content="Go.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE
            )
        )
        (record,) = store.list_entities(ChatSubagent)
        await _until(
            lambda: store.get(ChatSubagent, record.id).result_message_id is not None
        )
        parent_session_id = store.get(ChatTurn, parent_turn_id).session_id

        store.delete_chat_session(parent_session_id)

        assert store.list_entities(ChatSubagent) == []
        assert store.list_entities(ChatSession) == []
        assert store.list_entities(ChatTurn) == []
        await chat.shutdown()

    asyncio.run(scenario())


def test_parent_with_running_subagent_cannot_be_deleted(tmp_path: Path) -> None:
    store = NebulaStore(tmp_path / "subagents.db")
    project = store.create(Engagement(id="project", name="Subagents"))
    parent = store.create(
        ChatSession(
            id="parent",
            engagement_id=project.id,
            title="Parent",
            provider_profile_id="provider",
            model="model-a",
        )
    )
    child = store.create(
        ChatSession(
            id="child",
            engagement_id=project.id,
            title="Child",
            provider_profile_id="provider",
            model="model-a",
            parent_session_id=parent.id,
            metadata={"subagent_id": "sub"},
        )
    )
    store.create(
        ChatSubagent(
            id="sub",
            engagement_id=project.id,
            parent_session_id=parent.id,
            parent_turn_id="turn",
            child_session_id=child.id,
            name="Running",
            task="Work.",
        )
    )
    try:
        store.delete_chat_session(parent.id)
    except ConflictError as exc:
        assert "subagent is running" in str(exc)
    else:
        raise AssertionError("deleting a parent with a running subagent must fail")


def test_core_shutdown_preserves_safely_interrupted_subagents_for_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1", "start_subagent", task="Long task.", name="Slow", context=None
                ),
                _finish("p2"),
                _response(text="Delegated."),
            ],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Go.", allow_subagents=True)
        )
        await _drain(chat, chat.start_provider_turn(prepared))
        await _until(lambda: bool(provider.child_requests))

        await chat.shutdown()

        (record,) = store.list_entities(ChatSubagent)
        assert record.status == ChatSubagentStatus.RUNNING
        assert (
            store.get(ChatTurn, record.child_turn_id).status
            == ChatTurnStatus.INTERRUPTED
        )

    asyncio.run(scenario())


def test_restart_resumes_a_parent_waiting_on_an_interrupted_subagent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Count the route files.",
                    name="Count routes",
                    context=None,
                ),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
                _finish("p3"),
                _response(text="The subagent was interrupted by a restart."),
            ],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Split the work.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        events = await _drain(chat, parent_turn_id)
        assert "callback_required" in events
        assert (
            store.get(ChatTurn, parent_turn_id).status
            == ChatTurnStatus.WAITING_CALLBACK
        )
        (record,) = store.list_entities(ChatSubagent)
        await _until(lambda: bool(provider.child_requests))
        # Simulate a crash: drop the in-memory worker without settling the child.
        for runtime in chat._active_provider_turns.values():
            if runtime.task is not None:
                runtime.task.cancel()
        chat._active_provider_turns.clear()
        store.update(
            ChatSubagent,
            record.id,
            {
                "status": ChatSubagentStatus.RUNNING,
                "finished_at": None,
                "result_message_id": None,
            },
            expected_revision=store.get(ChatSubagent, record.id).revision,
        )
        child_turn = store.get(ChatTurn, record.child_turn_id)
        store.update(
            ChatTurn,
            child_turn.id,
            {"status": ChatTurnStatus.ROUTING, "error": None},
            expected_revision=child_turn.revision,
        )

        restarted = ChatService(
            store, provider_factory=lambda _: provider, worker_id="worker-2"
        )
        await restarted.startup()

        assert store.get(ChatSubagent, record.id).status == ChatSubagentStatus.RUNNING
        # The parent keeps waiting for the same recoverable child instead of
        # receiving a contradictory terminal interruption report.
        assert (
            store.get(ChatTurn, parent_turn_id).status
            == ChatTurnStatus.WAITING_CALLBACK
        )
        await restarted.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_failed_parent_resume_fails_the_turn_and_posts_reports(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Count the route files.",
                    name="Count routes",
                    context=None,
                ),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
            ],
            child=[_response(text="Found 3 route files.")],
        )
        store, project, profile, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Split the work.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        assert (
            store.get(ChatTurn, parent_turn_id).status
            == ChatTurnStatus.WAITING_CALLBACK
        )
        (record,) = store.list_entities(ChatSubagent)
        await _until(lambda: bool(provider.child_requests))
        # The provider is disabled while the parent waits, so the automatic
        # resume cannot rebuild the turn when the child reports back.
        latest_profile = store.get(ProviderProfile, profile.id)
        store.update(
            ProviderProfile,
            latest_profile.id,
            {"enabled": False},
            expected_revision=latest_profile.revision,
        )
        provider.child_gate.set()

        await _until(
            lambda: store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.FAILED
        )

        parent = store.get(ChatTurn, parent_turn_id)
        assert "no longer enabled" in (parent.error or "")
        assert parent.execution_claim_id is None
        # The conversation is free again and the report was not withheld.
        assert chat.pending_turn(parent.session_id) is None
        finished = store.get(ChatSubagent, record.id)
        assert finished.status == ChatSubagentStatus.COMPLETED
        assert finished.result_message_id is not None
        messages = _messages(store, parent.session_id)
        assert messages[-1].metadata.get("kind") == "subagent_result"
        assert "Found 3 route files." in messages[-1].content
        await chat.shutdown()

    asyncio.run(scenario())


def test_stopping_an_interrupted_parent_posts_finished_reports(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1", "start_subagent", task="Long task.", name="Slow", context=None
                ),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
            ],
            child=[_response(text="Child report.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Go.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        (record,) = store.list_entities(ChatSubagent)
        await _until(lambda: bool(provider.child_requests))
        # A restart interrupted the waiting parent (recovery pending), so the
        # finishing child cannot resume it and its report stays undelivered.
        parent = store.get(ChatTurn, parent_turn_id)
        store.update(
            ChatTurn,
            parent.id,
            {
                "status": ChatTurnStatus.INTERRUPTED,
                "error": "Core restarted before this response completed.",
                "request_snapshot": {
                    **parent.request_snapshot,
                    "recovery": {
                        "required": True,
                        "unknown_tool_call_ids": [],
                        "unknown_hook_execution_ids": [],
                    },
                },
            },
            expected_revision=parent.revision,
        )
        provider.child_gate.set()
        await _until(
            lambda: (
                store.get(ChatSubagent, record.id).status
                == ChatSubagentStatus.COMPLETED
            )
        )
        assert store.get(ChatSubagent, record.id).result_message_id is None

        await chat.stop_provider_turn(parent_turn_id)

        assert store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.CANCELLED
        delivered = store.get(ChatSubagent, record.id)
        assert delivered.result_message_id is not None
        messages = _messages(store, parent.session_id)
        assert messages[-1].metadata.get("kind") == "subagent_result"
        assert messages[-1].metadata["subagent_status"] == "completed"
        assert "Child report." in messages[-1].content
        await chat.shutdown()

    asyncio.run(scenario())


def test_core_shutdown_interrupts_an_inflight_parent_tool_turn(tmp_path: Path) -> None:
    class GatedParentProvider(RoutedProvider):
        """Hold the parent's second routing step so Core stops mid tool loop."""

        def __init__(self, parent, child) -> None:
            super().__init__(parent, child)
            self.parent_gate = asyncio.Event()
            self.parent_blocked = asyncio.Event()

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if (
                CHILD_MARKER not in (request.instructions or "")
                and request.metadata.get("operation") != "conversation_naming"
                and self.parent_requests
            ):
                self.parent_blocked.set()
                await self.parent_gate.wait()
            return await super().complete(request)

    async def scenario() -> None:
        provider = GatedParentProvider(
            parent=[
                _call(
                    "p1", "start_subagent", task="Long task.", name="Slow", context=None
                )
            ],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Go.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await asyncio.wait_for(provider.parent_blocked.wait(), 5)
        (record,) = store.list_entities(ChatSubagent)
        await _until(lambda: bool(provider.child_requests))

        await chat.shutdown()

        parent = store.get(ChatTurn, parent_turn_id)
        assert parent.status == ChatTurnStatus.INTERRUPTED
        assert parent.error == (
            "Core stopped before this response completed. Core will resume it automatically."
        )
        assert parent.request_snapshot["recovery"]["required"] is True
        assert parent.execution_claim_id is None
        assert _history(store, parent)[0]["name"] == "start_subagent"
        interrupted = store.get(ChatSubagent, record.id)
        assert interrupted.status == ChatSubagentStatus.RUNNING
        assert interrupted.error is None
        assert (
            store.get(ChatTurn, record.child_turn_id).status
            == ChatTurnStatus.INTERRUPTED
        )
        # The next boot offers the parent for resume instead of a dead end.
        restarted = ChatService(
            store, provider_factory=lambda _: provider, worker_id="worker-2"
        )
        await restarted.startup()
        assert store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.INTERRUPTED
        pending = restarted.pending_turn(parent.session_id)
        assert pending is not None and pending.id == parent_turn_id
        resumed = restarted.resume_turns_stopped_by_core()
        assert set(resumed) == {parent_turn_id, record.child_turn_id}
        assert restarted.has_active_provider_turn(parent_turn_id)
        assert restarted.has_active_provider_turn(record.child_turn_id)
        assert store.get(ChatSubagent, record.id).status == ChatSubagentStatus.RUNNING
        await restarted.shutdown()

    asyncio.run(scenario())


def test_subagent_start_failure_leaves_no_child_conversation_or_duplicate_report(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Review auth.",
                    name="Auth review",
                    context=None,
                ),
                _finish("p2"),
                _response(text="Delegation failed; reviewing inline."),
            ],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        prepared = await chat.prepare_async(
            _request(project, content="Review auth.", allow_subagents=True)
        )
        parent_session_id = prepared.session.id
        original = chat.prepare_async

        async def child_prepare_fails(request):
            if request.session_id != parent_session_id:
                raise RuntimeError("child provider offline")
            return await original(request)

        chat.prepare_async = child_prepare_fails
        parent_turn_id = chat.start_provider_turn(prepared)
        producer = chat._active_provider_turns[parent_turn_id].task
        await _drain(chat, parent_turn_id)
        await producer

        parent = store.get(ChatTurn, parent_turn_id)
        assert parent.status == ChatTurnStatus.COMPLETE
        step = _history(store, parent)[0]
        assert step["name"] == "start_subagent"
        assert step["status"] == "failed"
        # The model gets the failure contract's envelope, never the exception.
        # Core could not start it: no argument to correct.
        failure = json.loads(step["provider_result"])
        assert failure["schema"] == "nebula.tool-failure/v1"
        assert failure["tool"] == "start_subagent"
        assert failure["category"] == "execution_failed"
        assert "child provider offline" not in json.dumps(step)
        # No phantom child conversation, no failed record to re-report.
        assert store.list_entities(ChatSubagent) == []
        assert [
            item.id for item in store.list_entities(ChatSession, include_temporary=True)
        ] == [parent_session_id]
        messages = _messages(store, parent_session_id)
        assert [item for item in messages if item.metadata.get("kind")] == []
        assert messages[-1].content == "Delegation failed; reviewing inline."
        await chat.shutdown()

    asyncio.run(scenario())


def test_subagents_default_to_low_unless_the_supervisor_selects_an_effort(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Map the API routes.",
                    name="Routes",
                    context=None,
                    reasoning_effort=None,
                ),
                _call(
                    "p2",
                    "start_subagent",
                    task="List the config files.",
                    name="Config",
                    context=None,
                    reasoning_effort="high",
                ),
                _call(
                    "p3",
                    "start_subagent",
                    task="Guess.",
                    name="Bad level",
                    context=None,
                    reasoning_effort="extreme",
                ),
                _call("p4", "wait_subagents", subagent_ids=None, mode=None),
                _finish("p5"),
                _response(text="Both reported."),
            ],
            child=[_response(text="Done."), _response(text="Done.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        prepared = await chat.prepare_async(
            _request(
                project,
                content="Split the work.",
                allow_subagents=True,
                reasoning_effort="high",
            )
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE
            )
        )

        child_efforts = {
            request.messages[-1].content: request.reasoning_effort
            for request in provider.child_requests
        }
        assert child_efforts == {
            "Map the API routes.": "low",
            "List the config files.": "high",
        }
        records = {item.name: item for item in store.list_entities(ChatSubagent)}
        assert {name: item.reasoning_effort for name, item in records.items()} == {
            "Routes": "low",
            "Config": "high",
        }
        assert chat.subagents.view(records["Routes"])["reasoning_effort"] == "low"
        parent = store.get(ChatTurn, parent_turn_id)
        assert [entry["status"] for entry in _history(store, parent)[:3]] == [
            "complete",
            "complete",
            "failed",
        ]
        # The delegating model is told which level each child got.
        started = json.loads(_history(store, parent)[0]["provider_result"])
        assert started["reasoning_effort"] == "low"
        assert "reasoning_effort on start_subagent" in (
            provider.parent_requests[0].instructions or ""
        )
        await chat.shutdown()

    asyncio.run(scenario())


def test_goal_picking_up_late_reports_keeps_the_conversation_reasoning_level(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Review the auth module.",
                    name="Auth",
                    context=None,
                ),
                _finish("p2"),
                _response(text="Started a review."),
                _finish("p3"),
                _response(text="Folded the review into the goal."),
            ],
            child=[_response(text="Cookies lack SameSite.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        # Goal turns with tools publish a dashboard.
        chat.artifact_store = ArtifactStore(tmp_path / "artifacts")
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Review auth.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        (record,) = store.list_entities(ChatSubagent)
        assert record.status == ChatSubagentStatus.RUNNING

        # The reply belonged to a goal that is still running, and the operator
        # raised the conversation's level after it.
        goals = ChatGoalService(store)
        draft = goals.create(
            prepared.session.id,
            GoalCreate(
                objective="Review auth",
                completion_criteria=["Findings are reported"],
                step_budget=1,
            ),
        )
        goal = goals.write(
            prepared.session.id,
            GoalWrite(expected_revision=draft.revision, action="start"),
        )
        parent = store.get(ChatTurn, parent_turn_id)
        store.update(
            ChatTurn,
            parent.id,
            {"goal_id": goal.id},
            expected_revision=parent.revision,
        )
        session = store.get(ChatSession, prepared.session.id)
        store.update(
            ChatSession,
            session.id,
            {"metadata": {**session.metadata, "reasoning_effort": "high"}},
            expected_revision=session.revision,
        )

        provider.child_gate.set()
        await _until(
            lambda: any(
                "Subagent reports are ready" in str(request.messages[-1].content)
                for request in provider.parent_requests
            )
        )
        continued = next(
            request
            for request in provider.parent_requests
            if "Subagent reports are ready" in str(request.messages[-1].content)
        )
        assert continued.reasoning_effort == "high"
        await _until(
            lambda: (
                store.get(ChatSubagent, record.id).status
                == ChatSubagentStatus.COMPLETED
            )
        )
        await _until(lambda: len(store.list_entities(ChatGoalUsageCharge)) == 1)
        charged_usage = store.get(type(goal), goal.id).usage

        # Startup may revisit terminal children after either the settlement,
        # charge, or delivery write. The deterministic charge prevents a
        # second debit while the delivery repair remains safe to repeat.
        await chat.subagents.reconcile_after_restart()
        await chat.subagents.reconcile_after_restart()
        assert len(store.list_entities(ChatGoalUsageCharge)) == 1
        assert store.get(type(goal), goal.id).usage == charged_usage
        await chat.shutdown()

    asyncio.run(scenario())


def _sent(store: NebulaStore, direction: str) -> list[ChatSubagentMessage]:
    return [
        item
        for item in store.list_entities(ChatSubagentMessage, limit=100)
        if item.direction == direction
    ]


def _history(store: NebulaStore, turn: ChatTurn) -> list[dict]:
    """The turn's provider-visible steps, folded from its ledger."""

    return turn_history(store.database, turn)


def _entries(store: NebulaStore, turn: ChatTurn, name: str) -> list[dict]:
    return [item for item in _history(store, turn) if item["name"] == name]


def _result(entry: dict) -> dict:
    return json.loads(entry["provider_result"])


def test_parent_and_subagent_message_each_other_while_both_work(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store: NebulaStore

        async def instruct_child(request: ModelRequest) -> ModelResponse:
            await _until(lambda: bool(_sent(store, "to_parent")))
            (record,) = store.list_entities(ChatSubagent)
            return _call(
                "p2",
                "message_subagent",
                subagent_id=record.id,
                message="Also check port 8443.",
            )

        async def wait_for_report(request: ModelRequest) -> ModelResponse:
            await _until(
                lambda: (
                    store.list_entities(ChatSubagent)[0].status
                    == ChatSubagentStatus.COMPLETED
                )
            )
            return _finish("p3")

        async def note_progress(request: ModelRequest) -> ModelResponse:
            await _until(lambda: bool(_sent(store, "to_child")))
            return _call(
                "c2", "message_parent", message="Still scanning.", wait_for_reply=None
            )

        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Scan the host.",
                    name="Scan",
                    context=None,
                ),
                instruct_child,
                wait_for_report,
                _response(text="The admin panel is exposed on 8443."),
            ],
            child=[
                _call(
                    "c1",
                    "message_parent",
                    message="Found an open admin panel.",
                    wait_for_reply=None,
                ),
                note_progress,
                _response(text="Port 8443 serves the admin panel."),
            ],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        prepared = await chat.prepare_async(
            _request(project, content="Scan it.", allow_subagents=True)
        )
        # Every subagent can message its parent, even with no other tools.
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        (record,) = store.list_entities(ChatSubagent)
        await _until(
            lambda: store.get(ChatSubagent, record.id).result_message_id is not None
        )

        # The parent heard from the child before a later step, without asking.
        parent = store.get(ChatTurn, parent_turn_id)
        delivered = [
            item
            for item in _entries(store, parent, "list_subagents")
            if item.get("delivered_by_core")
        ]
        assert delivered
        assert "Found an open admin panel." in delivered[0]["provider_result"]
        # Core's step spends no tool budget.
        assert parent.artifact_queries == len(_history(store, parent)) - len(delivered)

        # The child read the parent's message before its next step.
        child = store.get(ChatTurn, record.child_turn_id)
        inbox = [
            item
            for item in _entries(store, child, "read_parent_messages")
            if item.get("delivered_by_core")
        ]
        assert "Also check port 8443." in inbox[0]["provider_result"]
        assert {tool.name for tool in provider.child_requests[0].tools} >= {
            "message_parent",
            "read_parent_messages",
        }
        await _until(
            lambda: all(
                item.status == ChatSubagentMessageStatus.DELIVERED
                for item in store.list_entities(ChatSubagentMessage)
            )
        )
        assert store.get(ChatSubagent, record.id).reported_at is not None
        await chat.shutdown()

    asyncio.run(scenario())


def test_subagent_question_pauses_it_until_the_waiting_parent_answers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store: NebulaStore
        seen: dict = {}

        async def answer(request: ModelRequest) -> ModelResponse:
            waited = request.tool_results[-1]
            seen["waited"] = waited
            (record,) = store.list_entities(ChatSubagent)
            return _call(
                "p3", "message_subagent", subagent_id=record.id, message="Use staging."
            )

        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Check the deploy.",
                    name="Deploy",
                    context=None,
                ),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
                answer,
                _call("p4", "wait_subagents", subagent_ids=None, mode=None),
                _finish("p5"),
                _response(text="Staging is healthy."),
            ],
            child=[
                _call(
                    "c1",
                    "message_parent",
                    message="Staging or production?",
                    wait_for_reply=True,
                ),
                _response(text="Checked staging: healthy."),
            ],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        prepared = await chat.prepare_async(
            _request(project, content="Check the deploy.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE
            )
        )
        (record,) = store.list_entities(ChatSubagent)

        # The wait ended on the question, which says the child is paused.
        waited = seen["waited"]
        assert waited.name == "wait_subagents"
        assert record.id in str(waited.output)
        assert "awaiting_your_reply" in str(waited.output)
        assert "Staging or production?" in str(waited.output)

        parent = store.get(ChatTurn, parent_turn_id)
        answered = _result(_entries(store, parent, "message_subagent")[0])
        assert answered["delivery"] == "answered"
        final_wait = _entries(store, parent, "wait_subagents")[-1]
        assert "Checked staging: healthy." in final_wait["provider_result"]

        # The child resumed with the answer as its tool result.
        child = store.get(ChatTurn, record.child_turn_id)
        (asked,) = _entries(store, child, "message_parent")
        assert asked["status"] == "complete"
        assert "Use staging." in asked["provider_result"]
        (question,) = _sent(store, "to_parent")
        assert question.expects_reply and not question.awaiting_reply
        assert question.status == ChatSubagentMessageStatus.DELIVERED
        await chat.shutdown()

    asyncio.run(scenario())


def test_idle_parent_releases_a_question_and_it_is_posted(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Rotate the keys.",
                    name="Keys",
                    context=None,
                ),
                _finish("p2"),
                _response(text="Started a key rotation."),
            ],
            child=[
                _call(
                    "c1",
                    "message_parent",
                    message="Which account should I use?",
                    wait_for_reply=True,
                ),
                _response(text="Rotated with the default account."),
            ],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Rotate keys.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        assert store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE

        provider.child_gate.set()
        (record,) = store.list_entities(ChatSubagent)
        await _until(
            lambda: store.get(ChatSubagent, record.id).result_message_id is not None
        )
        # Nobody was working to answer, so the child went on and said so.
        child = store.get(ChatTurn, record.child_turn_id)
        (asked,) = _entries(store, child, "message_parent")
        assert "not working right now" in asked["provider_result"]
        (question,) = _sent(store, "to_parent")
        assert not question.awaiting_reply
        # The question is in the parent conversation for its next turn.
        messages = _messages(store, prepared.session.id)
        posted = [
            item for item in messages if item.metadata.get("kind") == "subagent_message"
        ]
        assert posted[0].content.startswith("Question from subagent Keys:")
        assert "Which account should I use?" in posted[0].content
        assert "continued without your answer" in posted[0].content
        assert question.status == ChatSubagentMessageStatus.DELIVERED
        assert messages[-1].metadata["kind"] == "subagent_result"
        await chat.shutdown()

    asyncio.run(scenario())


def test_message_to_a_finished_subagent_starts_another_round(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Check the certificates.",
                    name="Certs",
                    context=None,
                ),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
                _finish("p3"),
                _response(text="Certificates are valid."),
            ],
            child=[
                _response(text="All certificates are valid."),
                _response(text="Backups are encrypted."),
            ],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        prepared = await chat.prepare_async(
            _request(project, content="Check certs.", allow_subagents=True)
        )
        first_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, first_turn_id)
        (record,) = store.list_entities(ChatSubagent)
        await _until(
            lambda: store.get(ChatSubagent, record.id).result_message_id is not None
        )
        first_post = store.get(ChatSubagent, record.id).result_message_id

        provider.parent.extend(
            [
                _call(
                    "p5",
                    "message_subagent",
                    subagent_id=record.id,
                    message="Now check the backups.",
                ),
                _call("p6", "wait_subagents", subagent_ids=[record.id], mode=None),
                _finish("p7"),
                _response(text="Backups are encrypted too."),
            ]
        )
        follow_up = await chat.prepare_async(
            _request(
                project,
                content="And the backups?",
                session_id=prepared.session.id,
                allow_subagents=True,
            )
        )
        second_turn_id = chat.start_provider_turn(follow_up)
        await _until(
            lambda: (
                store.get(ChatTurn, second_turn_id).status == ChatTurnStatus.COMPLETE
            )
        )
        second = store.get(ChatTurn, second_turn_id)
        sent = _result(_entries(store, second, "message_subagent")[0])
        assert sent["delivery"] == "new_round"
        assert sent["round"] == 2
        report = _result(_entries(store, second, "wait_subagents")[0])["subagents"][0]
        assert report["report"] == "Backups are encrypted."
        assert report["round"] == 2

        record = store.get(ChatSubagent, record.id)
        assert record.rounds == 2
        # The new round follows the parent's current turn.
        assert record.parent_turn_id == second_turn_id
        child_messages = _messages(store, record.child_session_id)
        assert "Now check the backups." in child_messages[-2].content
        await _until(
            lambda: (
                store.get(ChatSubagent, record.id).result_message_id
                not in {None, first_post}
            )
        )
        await chat.shutdown()

    asyncio.run(scenario())


def test_failed_subagent_reports_its_error_and_failed_steps(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def upstream_down(request: ModelRequest) -> ModelResponse:
            raise RuntimeError("upstream returned 503")

        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Fetch the logs.",
                    name="Logs",
                    context=None,
                ),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
                _finish("p3"),
                _response(text="The subagent failed."),
            ],
            child=[
                _call("c1", "message_parent", message="   ", wait_for_reply=None),
                upstream_down,
            ],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        prepared = await chat.prepare_async(
            _request(project, content="Get the logs.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE
            )
        )
        parent = store.get(ChatTurn, parent_turn_id)
        (report,) = _result(_entries(store, parent, "wait_subagents")[0])["subagents"]
        assert report["status"] == "failed"
        assert "upstream returned 503" in report["error"]
        (failure,) = report["tool_failures"]
        assert failure["tool"] == "message_parent"
        assert failure["status"] == "failed"
        # The failure contract's summary, not the handler's exception text.
        assert failure["error"] == "Invalid input."
        assert report["last_step"]["tool"] == "message_parent"

        (record,) = store.list_entities(ChatSubagent)
        await _until(
            lambda: store.get(ChatSubagent, record.id).result_message_id is not None
        )
        posted = _messages(store, prepared.session.id)[-1]
        assert posted.content.startswith("Subagent failed: Logs")
        assert "upstream returned 503" in posted.content
        assert "Failed step 0: message_parent" in posted.content
        await chat.shutdown()

    asyncio.run(scenario())


def test_core_bookkeeping_failure_fails_the_subagent_with_its_cause(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Map the routes.",
                    name="Routes",
                    context=None,
                ),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
                _finish("p3"),
                _response(text="Recording the routes failed."),
            ],
            child=[_response(text="Found 3 routes.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        settle = chat.subagents._child_settled

        async def broken(record: ChatSubagent, turn: ChatTurn) -> None:
            if turn.status == ChatTurnStatus.COMPLETE:
                raise RuntimeError("store unavailable")
            await settle(record, turn)

        chat.subagents._child_settled = broken  # type: ignore[method-assign]
        prepared = await chat.prepare_async(
            _request(project, content="Map routes.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE
            )
        )
        (record,) = store.list_entities(ChatSubagent)
        assert record.status == ChatSubagentStatus.FAILED
        assert record.error == (
            "Nebula could not record this subagent's result (RuntimeError): "
            "store unavailable"
        )
        parent = store.get(ChatTurn, parent_turn_id)
        assert (
            "store unavailable"
            in _entries(store, parent, "wait_subagents")[0]["provider_result"]
        )
        await chat.shutdown()

    asyncio.run(scenario())


def test_stopped_subagent_reports_messages_it_never_read(tmp_path: Path) -> None:
    async def scenario() -> None:
        store: NebulaStore
        provider: RoutedProvider

        async def instruct(request: ModelRequest) -> ModelResponse:
            await _until(lambda: bool(provider.child_requests))
            (record,) = store.list_entities(ChatSubagent)
            return _call(
                "p2",
                "message_subagent",
                subagent_id=record.id,
                message="Check the logs too.",
            )

        async def stop(request: ModelRequest) -> ModelResponse:
            (record,) = store.list_entities(ChatSubagent)
            return _call("p3", "stop_subagent", subagent_id=record.id)

        async def wait(request: ModelRequest) -> ModelResponse:
            (record,) = store.list_entities(ChatSubagent)
            return _call("p4", "wait_subagents", subagent_ids=[record.id], mode=None)

        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Audit the host.",
                    name="Audit",
                    context=None,
                ),
                instruct,
                stop,
                wait,
                _finish("p5"),
                _response(text="Stopped the audit."),
            ],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Audit it.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE
            )
        )
        parent = store.get(ChatTurn, parent_turn_id)
        queued = _result(_entries(store, parent, "message_subagent")[0])
        assert queued["delivery"] == "queued"
        # The report reaches the parent once, in whichever subagent result
        # comes first after the stop (here the update Core adds before the
        # next step); the wait that follows does not send it again.
        (report,) = [
            view
            for entry in _history(store, parent)
            if entry["name"] in {"list_subagents", "wait_subagents"}
            and entry.get("provider_result")
            for view in _result(entry).get("subagents", [])
            if "report" in view
        ]
        assert report["status"] == "stopped"
        (unread,) = report["undelivered_messages"]
        assert unread["content"] == "Check the logs too."
        assert unread["note"] == "The subagent stopped before reading it."
        (waited,) = _result(_entries(store, parent, "wait_subagents")[0])["subagents"]
        assert waited["status"] == "stopped"
        assert waited["report_received_earlier"] is True
        assert "report" not in waited
        (message,) = _sent(store, "to_child")
        assert message.status == ChatSubagentMessageStatus.UNDELIVERED
        await chat.shutdown()

    asyncio.run(scenario())
