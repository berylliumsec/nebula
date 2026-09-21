"""Harness chats delegating to a Nebula provider model as a subagent."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from nebula.v3.chat import ChatConfigurationError, ChatPrivacyError, ChatService
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatBackend,
    ChatMessage,
    ChatSession,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    Engagement,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnStatus,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
    ScopePolicy,
    ToolCall,
    ToolCallStatus,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    HarnessAdapter,
    HarnessConnection,
    HarnessEvent,
    HarnessRuntimeService,
    _gateway_subagent_tools,
    _harness_developer_instructions,
    _portable_gateway_tool_name,
    _session_native_capabilities,
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
)
from nebula.v3.storage import NebulaStore

CHILD_MARKER = "You are a subagent."
SUBAGENT_TOOLS = {"subagent.start", "subagent.wait", "subagent.list", "subagent.stop"}

Script = Callable[["ScriptedConnection", str], Awaitable[str]]


class ChildProvider(ModelProvider):
    """Answer subagent turns from a script, optionally held behind a gate."""

    def __init__(self, provider_id: str = "provider", *, local: bool = True) -> None:
        super().__init__(
            ProviderConfig(
                id=provider_id,
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1"
                if local
                else "https://provider.invalid/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=local,
                capabilities=ModelCapabilities(
                    streaming=True, tools=True, strict_tools=True
                ),
            )
        )
        self.answers: list[str] = []
        self.requests: list[ModelRequest] = []
        self.gate: asyncio.Event | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation") == "conversation_naming":
            return self._response("Named")
        if CHILD_MARKER not in (request.instructions or ""):
            raise AssertionError("only subagent turns reach this provider")
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if not self.answers:
            raise AssertionError("child script was exhausted")
        return self._response(self.answers.pop(0))

    def _response(self, text: str) -> ModelResponse:
        return ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            text=text,
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            finish_reason="stop",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.config.id, healthy=True, models=["model-a"]
        )


class ScriptedConnection(HarnessConnection):
    adapter_version = "test/scripted"

    def __init__(
        self, request: AdapterOpenRequest, runtime: HarnessRuntimeService
    ) -> None:
        self.request = request
        self.runtime = runtime
        self.external_session_id = request.session.external_session_id
        self.prompts: list[str] = []
        self.script: Script | None = None
        self.interrupted = False

    async def call(self, tool: str, /, **arguments) -> dict:
        return await self.runtime._gateway_call(self.request.session, tool, arguments)

    async def run_turn(
        self, prompt: str, *, model: str, images=None
    ) -> AsyncIterator[HarnessEvent]:
        del model, images
        self.prompts.append(prompt)
        self.external_session_id = self.external_session_id or "vendor-thread"
        yield HarnessEvent(
            type="started",
            external_session_id=self.external_session_id,
            external_turn_id=f"vendor-turn-{len(self.prompts)}",
        )
        answer = await self.script(self, prompt) if self.script else "Done."
        yield HarnessEvent(type="message_delta", delta=answer)
        yield HarnessEvent(type="completed", message=answer)

    async def steer(self, text: str) -> None:
        del text

    async def interrupt(self) -> None:
        self.interrupted = True

    async def close(self) -> None:
        pass


class ScriptedAdapter(HarnessAdapter):
    kind = HarnessKind.CODEX_APP_SERVER

    def __init__(self) -> None:
        self.runtime: HarnessRuntimeService | None = None
        self.opens: list[AdapterOpenRequest] = []
        self.connections: list[ScriptedConnection] = []
        self.script: Script | None = None

    async def probe(self, profile, credential_store):  # pragma: no cover - unused
        raise AssertionError("probe is not used")

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        assert self.runtime is not None
        self.opens.append(request)
        connection = ScriptedConnection(request, self.runtime)
        connection.script = self.script
        self.connections.append(connection)
        return connection


def _setup(
    tmp_path: Path,
    *,
    providers: dict[str, ChildProvider] | None = None,
    local_only_project: bool = False,
):
    store = NebulaStore(tmp_path / "nebula.db")
    project = store.create(Engagement(id="project", name="Provider subagents"))
    if local_only_project:
        policy = store.create(
            ScopePolicy(id="policy", engagement_id=project.id, local_only=True)
        )
        project = store.update(
            Engagement,
            project.id,
            {"scope_policy_id": policy.id},
            expected_revision=project.revision,
        )
    harness = store.create(
        HarnessProfile(
            id="codex",
            name="Codex fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-test",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    providers = providers or {"provider": ChildProvider()}
    for provider_id, provider in providers.items():
        store.create(
            ProviderProfile(
                id=provider_id,
                name=provider_id.title(),
                provider_type="vllm" if provider.config.local else "openrouter",
                is_local=provider.config.local,
                model_allowlist=["model-a", "model-unverified"],
                capabilities={"streaming": True, "tool_calling": True},
                capability_verifications={
                    "model-a": ProviderCapabilityVerification(
                        model="model-a", status=ProviderVerificationStatus.VERIFIED
                    )
                },
                privacy={"permits_sensitive_data": provider_id != "closed"},
            )
        )
    chat = ChatService(
        store,
        provider_factory=lambda profile: providers[profile.id],
        worker_id="worker",
    )
    adapter = ScriptedAdapter()
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        adapter_factory=lambda _: adapter,
    )
    adapter.runtime = runtime
    runtime.bind_provider_subagents(chat.subagents)
    return store, project, harness, chat, adapter, runtime


SETTING = {"provider_profile_id": "provider", "model": "model-a"}


def _prepare(runtime, project, harness, prompt, *, chat_id=None, setting=SETTING):
    return runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=harness.id,
        model=None,
        prompt=prompt,
        chat_session_id=chat_id,
        harness_session_id=None,
        mcp_server_ids=[],
        provider_subagent=setting,
    )


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.01)


def _payload(response: dict) -> dict:
    assert response["isError"] is False, response
    return json.loads(response["content"][0]["text"])


def _messages(store: NebulaStore, session_id: str) -> list[ChatMessage]:
    return sorted(
        (
            item
            for item in store.list_entities(ChatMessage, limit=1_000)
            if item.session_id == session_id
        ),
        key=lambda item: item.sequence,
    )


def test_harness_delegates_to_provider_model_and_waits_for_report(tmp_path):
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        child = chat.provider_factory(store.get(ProviderProfile, "provider"))
        child.answers = ["Found 3 route files."]
        seen: dict = {}

        async def script(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            started = _payload(
                await connection.call(
                    "subagent.start",
                    task="Count the route files.",
                    name="Count routes",
                )
            )
            seen["started"] = started
            waited = _payload(await connection.call("subagent.wait"))
            seen["waited"] = waited
            listed = _payload(await connection.call("subagent.list"))
            seen["listed"] = listed
            return "There are three route files."

        adapter.script = script
        parent_chat, chat_turn, turn = _prepare(
            runtime, project, harness, "Split the work."
        )
        await runtime.start_chat_turn(turn.id)

        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE
        assert seen["started"]["model"] == "model-a"
        report = seen["waited"]["subagents"][0]
        assert report["report"] == "Found 3 route files."
        assert seen["waited"]["still_running"] == []
        assert seen["listed"]["subagents"][0]["status"] == "completed"

        (record,) = store.list_entities(ChatSubagent)
        assert record.parent_backend == ChatBackend.HARNESS
        assert record.parent_session_id == parent_chat.id
        assert record.parent_turn_id == chat_turn.id
        assert (record.provider_profile_id, record.model) == ("provider", "model-a")
        assert record.status == ChatSubagentStatus.COMPLETED
        # The harness read the report through subagent.wait.
        assert record.reported_at is not None
        child_session = store.get(ChatSession, record.child_session_id)
        assert child_session.backend == ChatBackend.PROVIDER
        assert child_session.provider_profile_id == "provider"
        assert child_session.model == "model-a"
        assert child_session.parent_session_id == parent_chat.id
        # Children never get delegation tools of their own.
        assert not any(
            "subagent" in tool.name for tool in child.requests[0].tools or []
        )

        # Each gateway call is a durable tool call on the harness turn.
        calls = [
            item
            for item in store.list_entities(ToolCall, limit=100)
            if item.chat_turn_id == chat_turn.id
        ]
        assert sorted(item.tool_name for item in calls) == [
            "subagent.list",
            "subagent.start",
            "subagent.wait",
        ]
        assert {item.status for item in calls} == {ToolCallStatus.COMPLETE}

        # Once the harness turn ends the report is posted for the operator.
        await _until(
            lambda: store.get(ChatSubagent, record.id).result_message_id is not None
        )
        messages = _messages(store, parent_chat.id)
        assert messages[-2].content == "There are three route files."
        assert messages[-1].metadata["kind"] == "subagent_result"
        assert messages[-1].content.startswith("Subagent finished: Count routes")
        view = chat.subagents.view(store.get(ChatSubagent, record.id))
        assert view["model"] == "model-a"
        assert view["parent_backend"] == "harness"
        await chat.shutdown()

    asyncio.run(scenario())


def test_late_report_reaches_the_harness_at_its_next_turn(tmp_path):
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        child = chat.provider_factory(store.get(ProviderProfile, "provider"))
        child.answers = ["Cookies lack SameSite."]
        child.gate = asyncio.Event()

        async def delegate(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            _payload(
                await connection.call(
                    "subagent.start",
                    task="Review the auth module.",
                    context="Focus on session cookies.",
                )
            )
            return "Started a review."

        adapter.script = delegate
        parent_chat, _, turn = _prepare(runtime, project, harness, "Review auth.")
        await runtime.start_chat_turn(turn.id)
        (record,) = store.list_entities(ChatSubagent)
        assert record.status == ChatSubagentStatus.RUNNING

        child.gate.set()
        await _until(
            lambda: store.get(ChatSubagent, record.id).result_message_id is not None
        )
        assert store.get(ChatSubagent, record.id).reported_at is None
        assert "Focus on session cookies." in child.requests[0].messages[-1].content

        async def follow_up(connection: ScriptedConnection, prompt: str) -> str:
            del connection, prompt
            return "Noted."

        adapter.script = follow_up
        adapter.connections[0].script = follow_up
        _, _, second = _prepare(
            runtime, project, harness, "What did it find?", chat_id=parent_chat.id
        )
        assert "Cookies lack SameSite." in second.prompt
        assert "arrived after your last turn" in second.prompt
        assert store.get(ChatSubagent, record.id).reported_at is not None
        await runtime.start_chat_turn(second.id)
        assert "Cookies lack SameSite." in adapter.connections[0].prompts[-1]

        # A report is handed over once.
        _, _, third = _prepare(
            runtime, project, harness, "Anything else?", chat_id=parent_chat.id
        )
        assert "Cookies lack SameSite." not in third.prompt
        await runtime.start_chat_turn(third.id)
        await chat.shutdown()

    asyncio.run(scenario())


def test_wait_is_bounded_and_reports_what_is_still_running(tmp_path):
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        child = chat.provider_factory(store.get(ProviderProfile, "provider"))
        child.answers = ["Late answer."]
        child.gate = asyncio.Event()
        seen: dict = {}

        async def script(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            started = _payload(
                await connection.call("subagent.start", task="Take a while.")
            )
            seen["waited"] = _payload(
                await connection.call(
                    "subagent.wait",
                    subagent_ids=[started["subagent_id"]],
                    timeout_seconds=1,
                )
            )
            return "Moving on."

        adapter.script = script
        _, _, turn = _prepare(runtime, project, harness, "Delegate slowly.")
        await asyncio.wait_for(runtime.start_chat_turn(turn.id), timeout=5)
        (record,) = store.list_entities(ChatSubagent)
        assert seen["waited"]["still_running"] == [record.id]
        assert "subagent.wait again" in seen["waited"]["note"]
        assert store.get(ChatSubagent, record.id).reported_at is None
        child.gate.set()
        await _until(
            lambda: (
                store.get(ChatSubagent, record.id).status
                == ChatSubagentStatus.COMPLETED
            )
        )
        await chat.shutdown()

    asyncio.run(scenario())


def test_stopping_the_harness_turn_stops_its_subagents(tmp_path):
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        child = chat.provider_factory(store.get(ProviderProfile, "provider"))
        child.answers = ["Never delivered."]
        child.gate = asyncio.Event()
        started = asyncio.Event()

        async def script(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            _payload(await connection.call("subagent.start", task="Run long."))
            started.set()
            await asyncio.Event().wait()
            return "unreachable"

        adapter.script = script
        parent_chat, _, turn = _prepare(runtime, project, harness, "Delegate.")
        task = runtime.start_chat_turn(turn.id)
        await asyncio.wait_for(started.wait(), timeout=5)
        await runtime.cancel_turn(turn.id, reason="Operator stop")
        try:
            await task
        except asyncio.CancelledError:
            pass

        (record,) = store.list_entities(ChatSubagent)
        await _until(
            lambda: (
                store.get(ChatSubagent, record.id).status == ChatSubagentStatus.STOPPED
            )
        )
        await _until(
            lambda: store.get(ChatSubagent, record.id).result_message_id is not None
        )
        assert _messages(store, parent_chat.id)[-1].metadata["subagent_status"] == (
            "stopped"
        )
        await chat.shutdown()

    asyncio.run(scenario())


def test_subagent_tools_follow_the_chat_setting(tmp_path):
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _setup(tmp_path)
        parent_chat, _, off_turn = _prepare(
            runtime, project, harness, "No delegation.", setting=None
        )
        session = store.get(HarnessSession, off_turn.harness_session_id)
        names = {item["name"] for item in runtime._gateway_catalog(session)["tools"]}
        assert not names & SUBAGENT_TOOLS
        await runtime.start_chat_turn(off_turn.id)
        assert "provider_subagent" not in store.get(
            ChatSession, parent_chat.id
        ).metadata or (
            store.get(ChatSession, parent_chat.id).metadata["provider_subagent"] is None
        )

        _, chat_turn, on_turn = _prepare(
            runtime, project, harness, "Delegate now.", chat_id=parent_chat.id
        )
        # Turning subagents on keeps the vendor session and thread.
        assert on_turn.harness_session_id == off_turn.harness_session_id
        session = store.get(HarnessSession, on_turn.harness_session_id)
        assert session.metadata["provider_subagent"] == SETTING
        assert (
            store.get(ChatSession, parent_chat.id).metadata["provider_subagent"]
            == SETTING
        )
        assert chat_turn.request_snapshot["provider_subagent"] == SETTING
        names = {item["name"] for item in runtime._gateway_catalog(session)["tools"]}
        assert SUBAGENT_TOOLS <= names
        await runtime.start_chat_turn(on_turn.id)
        # The vendor catalog is fixed per connection, so the change reopens it
        # and the new instructions name the subagent model.
        assert len(adapter.opens) == 2
        instructions = _harness_developer_instructions(
            adapter.opens[1].session,
            _session_native_capabilities(adapter.opens[1].session, harness),
            vendor="Codex",
            gateway_tools=adapter.opens[1].gateway_tools,
        )
        assert "Provider subagents" in instructions
        assert "model-a" in instructions
        assert {item["name"] for item in adapter.opens[1].gateway_tools} >= (
            SUBAGENT_TOOLS
        )

        # A turn without the setting refuses the tools even on an old catalog.
        async def stale(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            response = await connection.call("subagent.start", task="Nope.")
            assert response["isError"] is True
            assert "turned off" in response["content"][0]["text"]
            return "Refused."

        _, _, off_again = _prepare(
            runtime,
            project,
            harness,
            "Stop delegating.",
            chat_id=parent_chat.id,
            setting=None,
        )
        adapter.script = stale
        await runtime.start_chat_turn(off_again.id)
        assert len(adapter.opens) == 3
        assert not SUBAGENT_TOOLS & {
            item["name"] for item in adapter.opens[2].gateway_tools
        }
        assert not store.list_entities(ChatSubagent)
        await chat.shutdown()

    asyncio.run(scenario())


def test_subagent_setting_is_validated_before_anything_is_stored(tmp_path):
    store, project, harness, chat, _adapter, runtime = _setup(
        tmp_path,
        providers={
            "provider": ChildProvider("provider"),
            "cloud": ChildProvider("cloud", local=False),
            "closed": ChildProvider("closed", local=False),
        },
    )
    with pytest.raises(ChatConfigurationError, match="tool check"):
        _prepare(
            runtime,
            project,
            harness,
            "Hi",
            setting={"provider_profile_id": "provider", "model": "model-unverified"},
        )
    with pytest.raises(ChatConfigurationError, match="does not exist"):
        _prepare(
            runtime,
            project,
            harness,
            "Hi",
            setting={"provider_profile_id": "missing", "model": "model-a"},
        )
    with pytest.raises(ChatPrivacyError, match="does not permit project data"):
        _prepare(
            runtime,
            project,
            harness,
            "Hi",
            setting={"provider_profile_id": "closed", "model": "model-a"},
        )
    assert not store.list_entities(ChatTurn)
    assert not store.list_entities(ChatSession)
    # A cloud provider that accepts project data is fine: turning subagents on
    # is the consent to send it their tool results.
    _prepare(
        runtime,
        project,
        harness,
        "Hi",
        setting={"provider_profile_id": "cloud", "model": "model-a"},
    )


def test_local_only_projects_refuse_cloud_subagent_models(tmp_path):
    store, project, harness, chat, _adapter, runtime = _setup(
        tmp_path,
        providers={"cloud": ChildProvider("cloud", local=False)},
        local_only_project=True,
    )
    with pytest.raises(ChatPrivacyError, match="local-only"):
        _prepare(
            runtime,
            project,
            harness,
            "Hi",
            setting={"provider_profile_id": "cloud", "model": "model-a"},
        )


def test_subagent_gateway_calls_read_as_delegated_work(tmp_path):
    store, project, harness, chat, _adapter, runtime = _setup(tmp_path)
    _, _, turn = _prepare(runtime, project, harness, "Hi")
    session = store.get(HarnessSession, turn.harness_session_id)
    for name in ("subagent.start", _portable_gateway_tool_name("subagent.wait")):
        event = runtime._record_tool_event(
            turn,
            session,
            HarnessEvent(
                type="tool_started",
                item_id="call-1",
                item_kind="tool",
                server_id="nebula",
                tool_name=name,
            ),
        )
        assert event.item_kind == "subagent"
    other = runtime._record_tool_event(
        turn,
        session,
        HarnessEvent(
            type="tool_started",
            item_id="call-2",
            item_kind="tool",
            server_id="nebula",
            tool_name="knowledge.search",
        ),
    )
    assert other.item_kind == "tool"


def test_waits_stay_below_each_harness_tool_timeout(tmp_path):
    # Codex allows a Nebula tool call 900 s; Grok's limit is unknown, so its
    # waits are short and the model calls again.
    codex = _gateway_subagent_tools(HarnessKind.CODEX_APP_SERVER)["subagent.wait"]
    grok = _gateway_subagent_tools(HarnessKind.GROK_ACP)["subagent.wait"]
    assert codex[1]["properties"]["timeout_seconds"]["maximum"] == 600
    assert "default 300" in codex[0]
    assert grok[1]["properties"]["timeout_seconds"]["maximum"] == 120
    assert "default 60" in grok[0]
    session = HarnessSession(
        engagement_id="project",
        harness_profile_id="grok",
        model="grok-4",
        metadata={"provider_subagent": SETTING},
    )
    instructions = _harness_developer_instructions(
        session,
        _session_native_capabilities(
            session,
            HarnessProfile(
                id="grok",
                name="Grok",
                kind=HarnessKind.GROK_ACP,
                executable="/bin/true",
            ),
        ),
        vendor="Grok",
        gateway_tools=({"name": _portable_gateway_tool_name("subagent.start")},),
    )
    assert "waits up to 60 seconds" in instructions
