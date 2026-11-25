"""Stage-based runtime routing for Core-owned research goals."""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from nebula.v3 import goal_runtime_routing as routing
from nebula.v3.butler_independent_review import (
    REVIEW_HISTORY_KEY,
    independent_review_approves,
)
from nebula.v3.chat import ChatService
from nebula.v3.chat_assistant_config import (
    ChatAssistantConfigPatch,
    chat_assistant_config,
    update_chat_assistant_config,
)
from nebula.v3.chat_goals import ChatGoalService, GoalConversationCreate, GoalWrite
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatSubagent,
    ChatTurn,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessProfile,
    HarnessTurn,
    HarnessTurnStatus,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
    WorkItem,
    WorkStage,
    utc_now,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    HarnessAdapter,
    HarnessConnection,
    HarnessEvent,
    HarnessRuntimeService,
    HarnessTurnFailedError,
    _claude_mcp_config,
)
from nebula.v3.providers import ModelRequest, ModelResponse, ToolCall, ToolChoice
from nebula.v3.storage import ConflictError, NebulaStore
from tests.v3.test_harness_provider_subagents import ChildProvider

HARNESS_MODEL = "claude-opus-5-5"
BULK_MODEL = "model-a"
POLICY = {
    "routes": [
        {
            "name": "validation",
            "stages": ["Validation", "runtime"],
            "main": {
                "backend": "harness",
                "harness_profile_id": "claude",
                "model": HARNESS_MODEL,
            },
            "subagents": {
                "provider_id": "bulk",
                "model": BULK_MODEL,
                "reasoning_effort": "high",
            },
            "fallback": "bulk",
            "fallback_cooldown_seconds": 600,
        },
        {
            "name": "bulk",
            "stages": ["recon", "Lane promotion", "simulation", "triage", "coverage"],
            "main": {"backend": "provider", "provider_id": "bulk", "model": BULK_MODEL},
            "subagents": {"provider_id": "bulk", "model": BULK_MODEL},
        },
    ],
    "promotion": {"review_rejections": 2, "route": "validation"},
    "projects": ["project"],
}


class AnyProvider(ChildProvider):
    """Answers main and subagent turns; a tool round finishes routing."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id)
        self.config.capabilities.context_window = 200_000
        self.config.capabilities.max_output_tokens = 8_000
        self.gate: asyncio.Event | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation") == "conversation_naming":
            return self._response("Named")
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if request.tool_choice != ToolChoice.NONE and request.tools:
            response = self._response("")
            return response.model_copy(
                update={
                    "text": "",
                    "tool_calls": [
                        ToolCall(
                            id=f"finish{len(self.requests)}",
                            name="finish_response",
                            arguments={},
                        )
                    ],
                    "finish_reason": "tool_calls",
                }
            )
        return self._response("Bulk work done.")


Script = Callable[["Connection", str], Awaitable[str]]


class Connection(HarnessConnection):
    adapter_version = "test/routing"

    def __init__(self, request: AdapterOpenRequest, adapter: "Adapter") -> None:
        self.request = request
        self.adapter = adapter
        self.external_session_id = request.session.external_session_id

    async def call(self, tool: str, /, **arguments: Any) -> dict:
        assert self.adapter.runtime is not None
        return await self.adapter.runtime._gateway_call(
            self.request.session, tool, arguments
        )

    async def run_turn(
        self, prompt: str, *, model: str, images=None
    ) -> AsyncIterator[HarnessEvent]:
        del images
        self.adapter.prompts.append((model, prompt))
        self.external_session_id = self.external_session_id or "vendor-thread"
        yield HarnessEvent(
            type="started",
            external_session_id=self.external_session_id,
            external_turn_id=f"vendor-turn-{len(self.adapter.prompts)}",
        )
        if self.adapter.failures:
            raise self.adapter.failures.pop(0)
        script = self.adapter.script
        answer = await script(self, prompt) if script else "Validated."
        yield HarnessEvent(type="completed", message=answer)

    async def steer(self, text: str) -> None:
        del text

    async def interrupt(self) -> None:
        pass

    async def close(self) -> None:
        pass


class Adapter(HarnessAdapter):
    kind = HarnessKind.CLAUDE_AGENT_SDK

    def __init__(self) -> None:
        self.runtime: HarnessRuntimeService | None = None
        self.opens: list[AdapterOpenRequest] = []
        self.prompts: list[tuple[str, str]] = []
        self.failures: list[BaseException] = []
        self.script: Script | None = None

    async def probe(self, profile, credential_store):  # pragma: no cover - unused
        raise AssertionError("probe is not used")

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        self.opens.append(request)
        return Connection(request, self)


class Env:
    def __init__(self, store, chat, runtime, adapter, provider, goals, tmp_path):
        self.store = store
        self.chat = chat
        self.runtime = runtime
        self.adapter = adapter
        self.provider = provider
        self.goals = goals
        self.tmp_path = tmp_path
        self.tasks: list[asyncio.Task[None]] = []

    async def settled(self) -> None:
        async with asyncio.timeout(10):
            while pending := [task for task in self.tasks if not task.done()]:
                await asyncio.wait(pending)

    async def close(self) -> None:
        await self.chat.shutdown()
        await self.chat.runtime_switcher.shutdown()
        await self.runtime.shutdown()
        await self.store.event_history.close()
        await self.store.database.dispose()


def _enable(monkeypatch, policy: dict | None = None) -> None:
    monkeypatch.setenv(routing.ENABLED_ENV, "1")
    monkeypatch.setenv(routing.CONFIG_ENV, json.dumps(policy or POLICY))


async def _env(tmp_path: Path, *, verified: bool = True) -> Env:
    store = NebulaStore(tmp_path / "routing.db")
    await store.create(
        Engagement(
            id="project",
            name="Routing fixture",
            work_stages=[
                WorkStage(key="recon", label="Recon"),
                WorkStage(key="validation", label="Validation"),
            ],
        )
    )
    await store.create(
        HarnessProfile(
            id="claude",
            name="Claude fixture",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            executable="/bin/true",
            default_model=HARNESS_MODEL,
            privacy={"local_only": True, "permits_sensitive_data": True},
            capabilities=HarnessCapabilities(
                models=[HARNESS_MODEL],
                authentication_state="verified" if verified else "unverified",
                session_state="verified" if verified else "unverified",
                turn_state="verified" if verified else "unverified",
            ),
        )
    )
    await store.create(
        ProviderProfile(
            id="bulk",
            name="Bulk provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=[BULK_MODEL],
            capabilities={"streaming": True, "tool_calling": True},
            capability_verifications={
                BULK_MODEL: ProviderCapabilityVerification(
                    model=BULK_MODEL, status=ProviderVerificationStatus.VERIFIED
                )
            },
            privacy={"permits_sensitive_data": True},
            metadata={
                "options": {"context_window": 200_000, "max_output_tokens": 8_000}
            },
        )
    )
    await store.create(
        McpServerProfile(
            id="mcp-docs",
            name="docs",
            transport=McpTransport.STREAMABLE_HTTP,
            url="https://mcp.invalid/api",
            enabled=True,
            capabilities=McpCapabilitySnapshot(
                checked_at=utc_now(),
                tools=[
                    McpToolSnapshot(
                        name="search_docs",
                        read_only=True,
                        destructive=False,
                        idempotent=True,
                        open_world=False,
                        credentialed=False,
                        annotations_complete=True,
                    )
                ],
            ),
        )
    )
    provider = AnyProvider("bulk")
    chat = ChatService(
        store,
        provider_factory=lambda _profile: provider,
        worker_id="worker",
        workspace_resolver=lambda _: tmp_path,
    )
    adapter = Adapter()
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        adapter_factory=lambda _: adapter,
    )
    adapter.runtime = runtime
    runtime.bind_provider_subagents(chat.subagents)
    chat.subagents.bind_harness_runtime(runtime)
    chat.runtime_switcher.harness_runtime = runtime
    chat.goal_dispatcher = runtime.dispatch_running_goal
    env = Env(
        store,
        chat,
        runtime,
        adapter,
        provider,
        ChatGoalService(store, runtime),
        tmp_path,
    )
    start = runtime.start_chat_turn

    async def tracked(turn_id: str) -> asyncio.Task[None]:
        task = await start(turn_id)
        env.tasks.append(task)
        return task

    runtime.start_chat_turn = tracked  # type: ignore[method-assign]
    return env


async def _provider_goal(
    env: Env, *, subagents: bool = True, mcp: bool = True, step_budget: int | None = 1
) -> ChatSession:
    created = await env.goals.create_conversation(
        GoalConversationCreate(
            engagement_id="project",
            backend=ChatBackend.PROVIDER,
            provider_id="bulk",
            model=BULK_MODEL,
            mcp_server_ids=["mcp-docs"] if mcp else [],
            allow_subagents=subagents,
            subagent_provider_id="bulk" if subagents else None,
            subagent_model=BULK_MODEL if subagents else None,
            objective="Validate one synthetic candidate",
            step_budget=step_budget,
            completion_criteria=["Evidence is saved"],
        )
    )
    return created.session


async def _stage(env: Env, session: ChatSession, stage: str) -> None:
    items = [
        item
        for item in await env.store.list_entities(WorkItem, engagement_id="project")
        if item.assignee_session_id == session.id
    ]
    if items:
        await env.store.update(
            WorkItem, items[0].id, {"stage": stage}, expected_revision=items[0].revision
        )
        return
    await env.store.create(
        WorkItem(
            engagement_id="project",
            title="Synthetic candidate",
            status="in_progress",
            assignee_session_id=session.id,
            stage=stage,
        )
    )


async def _manual_harness(env: Env, session: ChatSession) -> ChatSession:
    """The operator's PATCH: harness main, provider subagents, MCPs kept."""

    return await update_chat_assistant_config(
        env.store,
        session,
        ChatAssistantConfigPatch(
            backend=ChatBackend.HARNESS,
            profile_id="claude",
            model=HARNESS_MODEL,
            allow_subagents=True,
            subagent_provider_id="bulk",
            subagent_model=BULK_MODEL,
        ),
        expected_revision=session.revision,
        workspace_resolver=lambda _: env.tmp_path,
    )


def _state(session: ChatSession) -> dict:
    return session.metadata.get(routing.ROUTING_KEY) or {}


async def _evaluate(env: Env, session: ChatSession) -> dict | None:
    return await routing.evaluate_goal_runtime_route(env.store, session.id)


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


def test_policy_is_off_by_default_and_validated():
    assert routing.load_routing_policy({}) is None
    assert routing.load_routing_policy({routing.CONFIG_ENV: json.dumps(POLICY)}) is None
    with pytest.raises(routing.RoutingConfigError, match="not set"):
        routing.load_routing_policy({routing.ENABLED_ENV: "1"})
    broken = {**POLICY, "promotion": {"review_rejections": 2, "route": "missing"}}
    with pytest.raises(routing.RoutingConfigError, match="unknown route"):
        routing.load_routing_policy(
            {routing.ENABLED_ENV: "1", routing.CONFIG_ENV: json.dumps(broken)}
        )
    twice = json.loads(json.dumps(POLICY))
    twice["routes"][1]["stages"].append("VALIDATION")
    with pytest.raises(routing.RoutingConfigError, match="two routes"):
        routing.load_routing_policy(
            {routing.ENABLED_ENV: "1", routing.CONFIG_ENV: json.dumps(twice)}
        )
    policy = routing.load_routing_policy(
        {routing.ENABLED_ENV: "true", routing.CONFIG_ENV: json.dumps(POLICY)}
    )
    assert policy is not None
    assert policy.route_for_stage("x", "  validation ").name == "validation"
    assert policy.route_for_stage("lane promotion").name == "bulk"
    assert policy.route_for_stage("reporting") is None


def test_rate_limit_classification():
    def turn(error: str, diagnostic: dict | None = None) -> HarnessTurn:
        return HarnessTurn.model_construct(
            error=error, metadata={"diagnostic": diagnostic or {}}
        )

    assert routing.is_rate_limit_failure(turn("Claude AI usage limit reached"))
    assert routing.is_rate_limit_failure(turn("x", {"reason_code": "rate_limited"}))
    assert routing.is_rate_limit_failure(
        turn("x", {"vendor_error_info": {"api_error_status": 429}})
    )
    assert routing.is_rate_limit_failure(
        turn("x", {"vendor_error_info": "usageLimitExceeded"})
    )
    assert not routing.is_rate_limit_failure(
        turn("Claude turn failed: invalid request")
    )
    assert not routing.is_rate_limit_failure(
        turn(
            "API Error: 529 overloaded",
            {"vendor_error_info": {"api_error_status": 529}},
        )
    )


async def test_disabled_routing_changes_nothing_and_refuses_explicit_routes(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(routing.ENABLED_ENV, raising=False)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env)
        await _stage(env, session, "validation")
        before = await env.store.get(ChatSession, session.id)
        assert await _evaluate(env, session) is None
        assert await env.chat.runtime_switcher.settle(session.id) is not None
        after = await env.store.get(ChatSession, session.id)
        assert after.backend == ChatBackend.PROVIDER
        assert after.metadata == before.metadata
        with pytest.raises(routing.RoutingRefused, match="routing is off"):
            await routing.route_goal_runtime(
                env.store,
                session.id,
                route="validation",
                reason="try",
                actor="operator",
            )
    finally:
        await env.close()


# --------------------------------------------------------------------------
# Manual selections: in policy and out of policy
# --------------------------------------------------------------------------


async def test_manual_switch_matching_route_is_in_policy_without_new_switch(
    tmp_path, monkeypatch
):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env)
        await _stage(env, session, "validation")
        session = await _manual_harness(env, session)
        assert session.backend == ChatBackend.HARNESS
        harness_session_id = session.harness_session_id
        config_before = await chat_assistant_config(env.store, session)

        result = await _evaluate(env, session)
        assert result == {"outcome": "in_policy", "route": "validation"}
        saved = await env.store.get(ChatSession, session.id)
        # No new switch: same vendor session, same settings, no pending switch.
        assert saved.harness_session_id == harness_session_id
        assert "pending_runtime_switch" not in saved.metadata
        assert await chat_assistant_config(env.store, saved) == config_before
        state = _state(saved)
        assert state["owned"] is True and state["status"] == "in_policy"
        assert [event["action"] for event in state["events"]] == ["in_policy"]
        # Evaluating again records nothing new.
        await _evaluate(env, saved)
        again = await env.store.get(ChatSession, session.id)
        assert len(_state(again)["events"]) == 1
        assert again.harness_session_id == harness_session_id
    finally:
        await env.close()


async def test_queued_manual_switch_matching_route_is_in_policy_without_duplicate(
    tmp_path, monkeypatch
):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env)
        await _stage(env, session, "validation")
        # A goal turn holds the claim, so the PATCH queues a waiting switch.
        goal = await env.goals.get(session.id)
        await env.store.update(
            ChatGoal,
            goal.id,
            {
                "status": ChatGoalStatus.RUNNING,
                "execution_claim_id": "turn-held",
                "execution_owner_id": "worker",
                "execution_claimed_at": utc_now(),
            },
            expected_revision=goal.revision,
        )
        session = await _manual_harness(
            env, await env.store.get(ChatSession, session.id)
        )
        pending = session.metadata["pending_runtime_switch"]
        assert pending["status"] == "waiting_for_idle"

        result = await _evaluate(env, session)
        assert result is not None and result["outcome"] == "in_policy"
        saved = await env.store.get(ChatSession, session.id)
        assert saved.backend == ChatBackend.PROVIDER
        assert saved.metadata["pending_runtime_switch"]["id"] == pending["id"]
        assert saved.metadata["pending_runtime_switch"]["binding"] == pending["binding"]
        assert _state(saved)["status"] == "in_policy"
    finally:
        await env.close()


async def test_manual_choice_outside_policy_is_recorded_not_overwritten(
    tmp_path, monkeypatch
):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env)
        await _stage(env, session, "validation")
        before = await chat_assistant_config(env.store, session)
        for _ in range(2):
            result = await _evaluate(env, session)
            assert result is not None and result["outcome"] == "out_of_policy"
            assert await env.chat.runtime_switcher.settle(session.id) is not None
        saved = await env.store.get(ChatSession, session.id)
        assert saved.backend == ChatBackend.PROVIDER
        assert "pending_runtime_switch" not in saved.metadata
        assert await chat_assistant_config(env.store, saved) == before
        state = _state(saved)
        assert state["status"] == "out_of_policy"
        assert state.get("owned") is not True
        assert [event["action"] for event in state["events"]] == ["out_of_policy"]
    finally:
        await env.close()


async def test_operator_change_releases_an_owned_goal(tmp_path, monkeypatch):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env)
        await _stage(env, session, "recon")
        assert (await _evaluate(env, session))["outcome"] == "in_policy"
        session = await env.store.get(ChatSession, session.id)
        # The operator moves it by hand; the stage still says bulk.
        session = await _manual_harness(env, session)
        result = await _evaluate(env, session)
        assert result is not None and result["outcome"] == "out_of_policy"
        saved = await env.store.get(ChatSession, session.id)
        assert saved.backend == ChatBackend.HARNESS
        assert _state(saved)["owned"] is False
        await _evaluate(env, saved)
        assert (await env.store.get(ChatSession, session.id)).backend == (
            ChatBackend.HARNESS
        )
    finally:
        await env.close()


# --------------------------------------------------------------------------
# Owned goals follow their stage at the turn boundary
# --------------------------------------------------------------------------


async def test_owned_goal_moves_to_harness_and_back_keeping_mcp(tmp_path, monkeypatch):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env)
        await _stage(env, session, "recon")
        await _evaluate(env, session)
        await _stage(env, session, "validation")
        # The boundary every goal dispatch and continuation passes.
        switched = await env.chat.runtime_switcher.settle(session.id)
        assert switched is not None
        assert switched.backend == ChatBackend.HARNESS
        assert switched.harness_profile_id == "claude"
        assert switched.model == HARNESS_MODEL
        config = await chat_assistant_config(env.store, switched)
        assert config["mcp_server_ids"] == ["mcp-docs"]
        assert config["allow_subagents"] is True
        assert config["subagent_provider_id"] == "bulk"
        assert config["subagent_model"] == BULK_MODEL
        assert switched.metadata["provider_subagent"]["provider_profile_id"] == "bulk"
        event = _state(switched)["events"][-1]
        assert event["action"] == "routed"
        assert event["old_runtime"]["backend"] == "provider"
        assert event["new_runtime"]["backend"] == "harness"
        assert "Work stage Validation" in event["reason"]

        await _stage(env, session, "recon")
        back = await env.chat.runtime_switcher.settle(session.id)
        assert back is not None and back.backend == ChatBackend.PROVIDER
        config = await chat_assistant_config(env.store, back)
        assert config["mcp_server_ids"] == ["mcp-docs"]
        assert config["allow_subagents"] is True
        assert config["subagent_provider_id"] == "bulk"
        assert _state(back)["events"][-1]["new_runtime"]["backend"] == "provider"
    finally:
        await env.close()


async def test_review_rejections_promote_to_validation(tmp_path, monkeypatch):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env)
        await _stage(env, session, "recon")
        await _evaluate(env, session)
        goal = await env.goals.get(session.id)
        await env.store.update(
            ChatGoal,
            goal.id,
            {
                "metadata": {
                    **goal.metadata,
                    REVIEW_HISTORY_KEY: [
                        {"verdict": "REJECT"},
                        {"verdict": "ACCEPT"},
                        {"verdict": "NOT_READY"},
                    ],
                }
            },
            expected_revision=goal.revision,
        )
        result = await _evaluate(env, await env.store.get(ChatSession, session.id))
        assert result is not None and result["route"] == "validation"
        saved = await env.store.get(ChatSession, session.id)
        assert saved.backend == ChatBackend.HARNESS
        assert (
            "2 independent review send-backs" in _state(saved)["events"][-1]["reason"]
        )
    finally:
        await env.close()


async def test_busy_goal_defers_until_the_next_boundary(tmp_path, monkeypatch):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env)
        await _stage(env, session, "recon")
        await _evaluate(env, session)
        await _stage(env, session, "validation")
        goal = await env.goals.get(session.id)
        goal = await env.store.update(
            ChatGoal,
            goal.id,
            {
                "status": ChatGoalStatus.RUNNING,
                "execution_claim_id": "turn-held",
                "execution_owner_id": "worker",
                "execution_claimed_at": utc_now(),
            },
            expected_revision=goal.revision,
        )
        result = await _evaluate(env, await env.store.get(ChatSession, session.id))
        assert result is not None and result["outcome"] == "deferred"
        held = await env.store.get(ChatSession, session.id)
        assert held.backend == ChatBackend.PROVIDER
        assert "pending_runtime_switch" not in held.metadata
        assert _state(held)["deferred"]["route"] == "validation"
        await env.store.update(
            ChatGoal,
            goal.id,
            {
                "execution_claim_id": None,
                "execution_owner_id": None,
                "execution_claimed_at": None,
            },
            expected_revision=goal.revision,
        )
        switched = await env.chat.runtime_switcher.settle(session.id)
        assert switched is not None and switched.backend == ChatBackend.HARNESS
        assert _state(switched)["deferred"] is None
    finally:
        await env.close()


async def test_unverified_harness_is_refused(tmp_path, monkeypatch):
    _enable(monkeypatch)
    env = await _env(tmp_path, verified=False)
    try:
        session = await _provider_goal(env)
        await _stage(env, session, "recon")
        await _evaluate(env, session)
        await _stage(env, session, "validation")
        result = await _evaluate(env, await env.store.get(ChatSession, session.id))
        assert result is not None and result["outcome"] == "refused"
        assert "verified Check/Test turn" in result["reason"]
        assert (await env.store.get(ChatSession, session.id)).backend == (
            ChatBackend.PROVIDER
        )
        with pytest.raises(routing.RoutingRefused, match="verified Check/Test turn"):
            await routing.route_goal_runtime(
                env.store,
                session.id,
                route="validation",
                reason="validate now",
                actor="operator",
            )
    finally:
        await env.close()


# --------------------------------------------------------------------------
# Explicit route: operator and Butler
# --------------------------------------------------------------------------


async def test_explicit_route_on_busy_goal_waits_for_idle(tmp_path, monkeypatch):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env)
        # Out of policy first: only the explicit action changes it.
        dry = await routing.route_goal_runtime(
            env.store,
            session.id,
            route="validation",
            reason="validate the candidate",
            actor="operator",
            dry_run=True,
        )
        assert dry["dry_run"] is True and dry["would_switch"] is True
        assert dry["to"]["backend"] == "harness"
        assert (
            routing.ROUTING_KEY
            not in (await env.store.get(ChatSession, session.id)).metadata
        )
        goal = await env.goals.get(session.id)
        goal = await env.store.update(
            ChatGoal,
            goal.id,
            {
                "status": ChatGoalStatus.RUNNING,
                "execution_claim_id": "turn-held",
                "execution_owner_id": "worker",
                "execution_claimed_at": utc_now(),
            },
            expected_revision=goal.revision,
        )
        result = await routing.route_goal_runtime(
            env.store,
            session.id,
            route="validation",
            reason="validate the candidate",
            actor="operator",
        )
        assert result["outcome"] == "pending"
        waiting = await env.store.get(ChatSession, session.id)
        assert waiting.backend == ChatBackend.PROVIDER
        assert waiting.metadata["pending_runtime_switch"]["status"] == (
            "waiting_for_idle"
        )
        assert _state(waiting)["override"] == "validation"
        # The routing evaluation sees its own pending switch: no duplicate.
        assert (await _evaluate(env, waiting))["outcome"] == "in_policy"
        assert (await env.store.get(ChatSession, session.id)).metadata[
            "pending_runtime_switch"
        ]["id"] == waiting.metadata["pending_runtime_switch"]["id"]
        await env.store.update(
            ChatGoal,
            goal.id,
            {
                "execution_claim_id": None,
                "execution_owner_id": None,
                "execution_claimed_at": None,
            },
            expected_revision=goal.revision,
        )
        # Releasing the claim schedules the waiting switch; settle applies it.
        for _ in range(200):
            await env.chat.runtime_switcher.settle(session.id)
            switched = await env.store.get(ChatSession, session.id)
            if switched.backend == ChatBackend.HARNESS:
                break
            await asyncio.sleep(0.01)
        assert switched.backend == ChatBackend.HARNESS
        assert "pending_runtime_switch" not in switched.metadata
        config = await chat_assistant_config(env.store, switched)
        assert config["mcp_server_ids"] == ["mcp-docs"]
        event = _state(switched)["events"][-1]
        assert event["actor"] == "operator"
        assert "validate the candidate" in event["reason"]
    finally:
        await env.close()


async def test_butler_tool_is_kill_switch_gated_and_audited(tmp_path, monkeypatch):
    from nebula.v3.butler_supervisor_policy import SupervisorDisabled

    _enable(monkeypatch)
    env = await _env(tmp_path)
    audit = tmp_path / "audit.jsonl"
    try:
        session = await _provider_goal(env)
        monkeypatch.delenv("NEBULA_BUTLER_SUPERVISOR_ENABLED", raising=False)
        tool = routing.ButlerGoalRuntimeRouting(env.store, "project")
        with pytest.raises(SupervisorDisabled):
            await tool.route(
                {"session_id": session.id, "route": "validation", "reason": "x"}
            )
        monkeypatch.setenv("NEBULA_BUTLER_SUPERVISOR_ENABLED", "1")
        monkeypatch.setenv("NEBULA_BUTLER_SUPERVISOR_AUDIT_LOG", str(audit))
        [action] = tool.actions()
        assert action.spec.name == routing.TOOL_NAME
        result = await tool.route(
            {
                "session_id": session.id,
                "route": "validation",
                "reason": "validate the candidate",
                "dry_run": True,
            }
        )
        assert result["dry_run"] is True
        assert (await env.store.get(ChatSession, session.id)).backend == (
            ChatBackend.PROVIDER
        )
        result = await tool.route(
            {
                "session_id": session.id,
                "route": "validation",
                "reason": "validate the candidate",
            }
        )
        assert result["outcome"] == "routed"
        saved = await env.store.get(ChatSession, session.id)
        assert saved.backend == ChatBackend.HARNESS
        assert _state(saved)["events"][-1]["actor"].startswith("butler:")
        with pytest.raises(routing.RoutingRefused, match="choose auto, release"):
            await tool.route(
                {"session_id": session.id, "route": "nonsense", "reason": "x"}
            )
    finally:
        await env.close()


def test_route_tool_is_a_registered_supervisor_action():
    from nebula.v3.butler_capabilities import SUPERVISOR_ACTIONS

    assert routing.TOOL_NAME in SUPERVISOR_ACTIONS


# --------------------------------------------------------------------------
# Mixed runtimes, MCP pass-through and rate-limit fallback on a harness goal
# --------------------------------------------------------------------------


def _payload(result: dict) -> dict:
    content = result.get("content") or []
    if content and isinstance(content[0], dict) and "text" in content[0]:
        return json.loads(content[0]["text"])
    return result.get("structuredContent") or result


async def test_harness_goal_turn_receives_goal_mcp_and_core_gateway(
    tmp_path, monkeypatch
):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env)
        await _stage(env, session, "recon")
        await _evaluate(env, session)
        await _stage(env, session, "validation")
        goal = await env.goals.get(session.id)
        await env.goals.write(
            session.id, GoalWrite(expected_revision=goal.revision, action="start")
        )
        # The routed switch happens at the dispatch boundary.
        assert await env.runtime.dispatch_running_goal(session.id, "Begin validation")
        await env.settled()
        switched = await env.store.get(ChatSession, session.id)
        assert switched.backend == ChatBackend.HARNESS
        opened = env.adapter.opens[0]
        assert opened.profile.kind == HarnessKind.CLAUDE_AGENT_SDK
        assert opened.session.model == HARNESS_MODEL
        names = {tool["name"] for tool in opened.gateway_tools}
        # The goal's selected MCP server and Core's own Work tools both reach
        # the Claude turn, through the one Nebula gateway server.
        assert any("search_docs" in name for name in names), names
        assert {"work.check_in", "work.list"} & names, names
        servers = _claude_mcp_config(opened.gateway_config)
        assert servers and set(servers) == set(opened.gateway_config)
        assert env.adapter.prompts[0][0] == HARNESS_MODEL
    finally:
        await env.close()


async def test_harness_goal_turn_uses_provider_subagents(tmp_path, monkeypatch):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    seen: dict = {}
    try:
        session = await _provider_goal(env, mcp=False)
        await _stage(env, session, "validation")
        session = await _manual_harness(env, session)
        await _evaluate(env, session)

        async def script(connection: Connection, prompt: str) -> str:
            if "Count the routes" in prompt:  # pragma: no cover - provider child
                return "child"
            started = _payload(
                await connection.call(
                    "subagent.start", task="Count the routes", name="Routes"
                )
            )
            seen["started"] = started
            seen["wait"] = _payload(await connection.call("subagent.wait"))
            return "Validated with a provider subagent."

        env.adapter.script = script
        goal = await env.goals.get(session.id)
        await env.goals.write(
            session.id, GoalWrite(expected_revision=goal.revision, action="start")
        )
        assert await env.runtime.dispatch_running_goal(session.id, "Begin validation")
        await env.settled()

        assert env.adapter.prompts[0][0] == HARNESS_MODEL

        assert "subagent_id" in seen["started"]
        record = await env.store.get(ChatSubagent, seen["started"]["subagent_id"])
        child = await env.store.get(ChatSession, record.child_session_id)
        assert child.backend == ChatBackend.PROVIDER
        assert child.provider_profile_id == "bulk"
        assert child.model == BULK_MODEL
        assert child.parent_session_id == session.id
        assert env.provider.requests, "the provider subagent ran"
    finally:
        await env.close()


async def test_rate_limited_harness_turn_runs_on_fallback_then_retries_harness(
    tmp_path, monkeypatch
):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env, mcp=False, step_budget=5)
        await _stage(env, session, "validation")
        session = await _manual_harness(env, session)
        await _evaluate(env, session)
        env.adapter.failures.append(
            HarnessTurnFailedError(
                "Claude AI usage limit reached",
                vendor_error_info={"subtype": "error", "api_error_status": 429},
            )
        )
        env.provider.gate = asyncio.Event()
        goal = await env.goals.get(session.id)
        await env.goals.write(
            session.id, GoalWrite(expected_revision=goal.revision, action="start")
        )
        assert await env.runtime.dispatch_running_goal(session.id, "Begin validation")
        await env.settled()

        saved = await env.store.get(ChatSession, session.id)
        assert saved.backend == ChatBackend.PROVIDER
        assert saved.provider_profile_id == "bulk"
        state = _state(saved)
        assert state["route"] == "bulk"
        assert state["cooldown"]["route"] == "validation"
        fallback_event = state["events"][-1]
        assert fallback_event["action"] == "rate_limit_fallback"
        assert fallback_event["old_runtime"]["backend"] == "harness"
        assert fallback_event["new_runtime"]["backend"] == "provider"
        goal = await env.goals.get(session.id)
        assert goal.status == ChatGoalStatus.RUNNING, goal.blocked_reason
        turns = await env.store.list_session_entities(ChatTurn, session.id)
        assert len(turns) == 2
        assert turns[1].provider_profile_id == "bulk"
        failed = await env.store.get(HarnessTurn, turns[0].harness_turn_id)
        assert failed.status in {
            HarnessTurnStatus.FAILED,
            HarnessTurnStatus.INTERRUPTED,
        }
        # Let the fallback turn finish, then stop the goal from continuing.
        for _ in range(20):
            goal = await env.goals.get(session.id)
            try:
                await env.goals.write(
                    session.id,
                    GoalWrite(expected_revision=goal.revision, action="pause"),
                )
                break
            except ConflictError:
                await asyncio.sleep(0.01)
        env.provider.gate.set()
        for _ in range(500):
            if await env.chat.pending_turn(session.id) is None:
                break
            await asyncio.sleep(0.01)

        # During the cooldown the next boundary stays on the fallback.
        result = await _evaluate(env, await env.store.get(ChatSession, session.id))
        assert result is not None and result["route"] == "bulk"
        assert (await env.store.get(ChatSession, session.id)).backend == (
            ChatBackend.PROVIDER
        )
        # After it, the harness is tried again.
        later = utc_now() + timedelta(seconds=601)
        result = await routing.evaluate_goal_runtime_route(
            env.store, session.id, now=later
        )
        assert result is not None and result["route"] == "validation", result
        assert result["outcome"] == "routed", result
        back = await env.store.get(ChatSession, session.id)
        assert back.backend == ChatBackend.HARNESS
        assert (await chat_assistant_config(env.store, back))[
            "subagent_provider_id"
        ] == ("bulk")
    finally:
        await env.close()


# --------------------------------------------------------------------------
# Reviewer independence after a runtime change
# --------------------------------------------------------------------------


def _session(model: str, backend=ChatBackend.HARNESS, **metadata) -> ChatSession:
    return ChatSession(
        engagement_id="project",
        title="Goal",
        backend=backend,
        harness_profile_id="claude" if backend == ChatBackend.HARNESS else None,
        harness_session_id="vendor" if backend == ChatBackend.HARNESS else None,
        provider_profile_id="bulk" if backend == ChatBackend.PROVIDER else None,
        model=model,
        metadata=metadata,
    )


def test_reviewer_must_differ_from_current_and_routed_main_runtime():
    routed = {routing.ROUTING_KEY: {"main_models": [BULK_MODEL, HARNESS_MODEL]}}
    moved = _session(HARNESS_MODEL, **routed)
    assert "current main runtime model" in routing.reviewer_conflict(
        moved, HARNESS_MODEL
    )
    # Same family as the routed main agent: refused with a reason.
    conflict = routing.reviewer_conflict(moved, "claude-sonnet-5")
    assert conflict is not None and "model family" in conflict
    assert routing.reviewer_conflict(moved, "gpt-review") is None
    # Back on the provider, the Claude context still counts.
    back = _session(BULK_MODEL, backend=ChatBackend.PROVIDER, **routed)
    assert "model family" in routing.reviewer_conflict(back, "claude-sonnet-5")
    # A pending switch to Claude counts as well.
    pending = _session(
        BULK_MODEL,
        backend=ChatBackend.PROVIDER,
        pending_runtime_switch={"binding": {"model": HARNESS_MODEL}},
    )
    assert routing.reviewer_conflict(pending, HARNESS_MODEL) is not None
    # Never routed: only the exact models are compared, as before.
    plain = _session(BULK_MODEL, backend=ChatBackend.PROVIDER)
    assert routing.reviewer_conflict(plain, "claude-sonnet-5") is None
    # A reviewer on the goal's own harness profile is the same context.
    same = _session("other-model")
    assert "own main harness" in routing.reviewer_conflict(
        same, "reviewer-model", reviewer_harness_profile_id="claude"
    )


def test_recorded_approval_is_rechecked_against_the_current_runtime():
    from nebula.v3.butler_independent_review import (
        REVIEW_KEY,
        REVIEW_SOURCE,
        proposal_digest,
    )
    from nebula.v3.domain import GOAL_COMPLETION_PROPOSAL_KEY

    goal = ChatGoal(
        engagement_id="project",
        session_id="goal-chat",
        objective="o",
        completion_criteria=["c"],
        metadata={GOAL_COMPLETION_PROPOSAL_KEY: {"summary": "done"}},
    )
    record = {
        "id": "review-1",
        "source": REVIEW_SOURCE,
        "verdict": "ACCEPT",
        "proposal_digest": proposal_digest(goal),
        "reviewer_model": "claude-sonnet-5",
        "reviewer_session_id": "independent-review:review-1",
        "approval_provenance": {
            "source": REVIEW_SOURCE,
            "provider_id": "reviewer",
            "model": "claude-sonnet-5",
            "review_id": "review-1",
            "packet_sha256": "0" * 64,
        },
    }
    goal = goal.model_copy(update={"metadata": {**goal.metadata, REVIEW_KEY: record}})
    assert independent_review_approves(goal, BULK_MODEL)[0] is True
    moved = _session(
        HARNESS_MODEL, **{routing.ROUTING_KEY: {"main_models": [HARNESS_MODEL]}}
    )
    approved, why = independent_review_approves(goal, moved.model, session=moved)
    assert approved is False and "model family" in why
