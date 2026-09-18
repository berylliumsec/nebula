import asyncio
from pathlib import Path

from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.chat_subagents import MAX_ACTIVE_SUBAGENTS
from nebula.v3.domain import (
    ChatMessage,
    ChatSession,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
    ToolCall,
)
from nebula.v3.storage import ConflictError, NebulaStore

CHILD_MARKER = "You are a subagent."


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


class RoutedProvider(ModelProvider):
    """Serve parent and subagent requests from separate scripts."""

    def __init__(self, parent: list[ModelResponse], child: list[ModelResponse]) -> None:
        super().__init__(
            ProviderConfig(
                id="provider",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(streaming=True, tools=True, strict_tools=True),
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
            return self.child.pop(0)
        self.parent_requests.append(request)
        if not self.parent:
            raise AssertionError("parent script was exhausted")
        return self.parent.pop(0)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id="provider", healthy=True, models=["model-a"])


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


def _request(project: Engagement, *, content: str, session_id: str | None = None, **flags):
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
        (item for item in store.list_entities(ChatMessage, limit=1_000) if item.session_id == session_id),
        key=lambda item: item.sequence,
    )


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
            "stop_subagent",
        }
        assert opted_in.turn.request_snapshot["allow_subagents"] is True
        await chat.shutdown()

    asyncio.run(scenario())


def test_wait_resumes_parent_with_report_and_posts_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call("p1", "start_subagent", task="Count the route files.", name="Count routes", context=None),
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
        parent_turn_id = chat.start_provider_turn(prepared)
        events = await _drain(chat, parent_turn_id)
        assert "callback_required" in events
        paused = store.get(ChatTurn, parent_turn_id)
        assert paused.status == ChatTurnStatus.WAITING_CALLBACK
        assert paused.tool_history[-1]["subagent_wait"]["mode"] == "all"

        provider.child_gate.set()
        await _until(lambda: store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE)

        parent = store.get(ChatTurn, parent_turn_id)
        wait_entry = parent.tool_history[1]
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
        assert store.get(ChatTurn, record.child_turn_id).engagement_id == parent.engagement_id
        # Children never receive the delegation tools and are told they are subagents.
        for request in provider.child_requests:
            assert not any(tool.name.endswith("subagent") or tool.name == "wait_subagents" for tool in request.tools)
        await _until(lambda: store.get(ChatSubagent, record.id).result_message_id is not None)
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
                _call("p1", "start_subagent", task="Review the auth module.", name=None, context="Focus on session cookies."),
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
        assert "Focus on session cookies." in provider.child_requests[0].messages[-1].content

        provider.child_gate.set()
        await _until(lambda: store.get(ChatSubagent, record.id).result_message_id is not None)
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
                _call("p1", "start_subagent", task="Long task.", name="Slow", context=None),
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
        assert store.get(ChatTurn, stopped.child_turn_id).status == ChatTurnStatus.CANCELLED
        messages = _messages(store, store.get(ChatTurn, parent_turn_id).session_id)
        assert messages[-1].metadata["subagent_status"] == "stopped"
        await chat.shutdown()

    asyncio.run(scenario())


def test_concurrent_subagent_limit_is_reported_to_the_model(tmp_path: Path) -> None:
    async def scenario() -> None:
        starts = [
            _call(f"p{index}", "start_subagent", task=f"Task {index}.", name=None, context=None)
            for index in range(MAX_ACTIVE_SUBAGENTS + 1)
        ]
        provider = RoutedProvider(
            parent=[*starts, _finish("done"), _response(text="Limited.")],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Fan out.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        parent = store.get(ChatTurn, parent_turn_id)
        statuses = [entry["status"] for entry in parent.tool_history]
        assert statuses == ["complete"] * MAX_ACTIVE_SUBAGENTS + ["failed"]
        assert "already running" in parent.tool_history[-1]["provider_result"]
        assert len(store.list_entities(ChatSubagent)) == MAX_ACTIVE_SUBAGENTS
        for record in store.list_entities(ChatSubagent):
            await chat.subagents.stop(record.id)
        assert {item.status for item in store.list_entities(ChatSubagent)} == {
            ChatSubagentStatus.STOPPED
        }
        await chat.shutdown()

    asyncio.run(scenario())


def test_restart_interrupts_running_subagents_and_reports_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call("p1", "start_subagent", task="Long task.", name="Slow", context=None),
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
        # Simulate a crash: drop the in-memory worker without settling the child.
        for runtime in chat._active_provider_turns.values():
            if runtime.task is not None:
                runtime.task.cancel()
        chat._active_provider_turns.clear()
        store.update(
            ChatSubagent,
            record.id,
            {"status": ChatSubagentStatus.RUNNING, "finished_at": None, "result_message_id": None},
            expected_revision=store.get(ChatSubagent, record.id).revision,
        )
        child_turn = store.get(ChatTurn, record.child_turn_id)
        store.update(
            ChatTurn,
            child_turn.id,
            {"status": ChatTurnStatus.ROUTING, "error": None},
            expected_revision=child_turn.revision,
        )

        restarted = ChatService(store, provider_factory=lambda _: provider, worker_id="worker-2")
        await restarted.startup()

        interrupted = store.get(ChatSubagent, record.id)
        assert interrupted.status == ChatSubagentStatus.INTERRUPTED
        assert interrupted.result_message_id is not None
        await restarted.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_deleting_parent_removes_finished_subagent_conversations(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call("p1", "start_subagent", task="Quick task.", name="Quick", context=None),
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
        await _until(lambda: store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE)
        (record,) = store.list_entities(ChatSubagent)
        await _until(lambda: store.get(ChatSubagent, record.id).result_message_id is not None)
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
        ChatSession(id="parent", engagement_id=project.id, title="Parent", provider_profile_id="provider", model="model-a")
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


def test_core_shutdown_interrupts_rather_than_stops_subagents(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call("p1", "start_subagent", task="Long task.", name="Slow", context=None),
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
        assert record.status == ChatSubagentStatus.INTERRUPTED
        assert record.error == "Core shut down while this subagent was running."

    asyncio.run(scenario())
