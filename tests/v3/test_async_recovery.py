"""Crash boundaries preserve turn ownership and already committed tool effects."""

import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import httpx

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatCompletionRequest, ChatService, PreparedChat
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessSession,
    HarnessTurn,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
)
from nebula.v3.policy import PolicyEngine
from nebula.v3.providers import (
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    StreamEventType,
    ToolCall as ModelToolCall,
)
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.sandbox import AnalysisOnlyRunner
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import (
    AnalysisTool,
    StoreToolEvidenceRecorder,
    StoreToolLedger,
    ToolBroker,
    ToolInvocation,
    ToolRegistry,
    ToolSpec,
)
from tests.v3.test_chat import FakeProvider
from tests.v3.test_chat_tool_loop import ScriptedProvider, _response
from tests.v3.test_async_core import (
    reject_synchronous_database_waits as reject_synchronous_database_waits,
)


async def _close(store):
    await store.event_history.close()
    await store.database.dispose()


async def test_async_shutdown_restarts_same_turn_without_duplicate_assistant_effects(
    tmp_path,
):
    class BlockingProvider(FakeProvider):
        async def stream(self, request):
            yield ModelStreamEvent(type=StreamEventType.STARTED)
            yield ModelStreamEvent(
                type=StreamEventType.TEXT_DELTA, delta="Unfinished. "
            )
            await asyncio.Event().wait()

    async def exercise():
        path = tmp_path / "restart.db"
        store = NebulaStore(path)
        project = await store.create(Engagement(name="Synthetic recovery"))
        profile = await store.create(
            ProviderProfile(
                id="provider",
                name="Inert",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a"],
            )
        )
        service = ChatService(
            store, provider_factory=lambda _: BlockingProvider(profile.id, local=True)
        )
        prepared = await service.prepare(
            ChatCompletionRequest(
                provider_id=profile.id,
                engagement_id=project.id,
                model="model-a",
                messages=[{"role": "user", "content": "Continue once."}],
                include_knowledge=False,
                stream=True,
            )
        )
        turn_id = await service.start_provider_turn(prepared)
        follower = service.follow_provider_turn(turn_id)
        for expected in ["queued", "admitted", "started", "delta"]:
            assert (await anext(follower))[0] == expected
        await service.shutdown()
        assert [name async for name, _ in follower] == ["error"]
        interrupted = await store.get(ChatTurn, turn_id)
        assert interrupted.status == ChatTurnStatus.INTERRUPTED
        assert interrupted.execution_claim_id is None
        await _close(store)
        reopened = NebulaStore(path)
        recovered = ChatService(
            reopened,
            provider_factory=lambda _: FakeProvider(profile.id, local=True),
            worker_id="second-worker",
        )
        try:
            await recovered.startup()
            harness = await reopened.create(
                HarnessSession(
                    engagement_id=project.id,
                    harness_profile_id="inert-harness",
                    model="fixture",
                )
            )
            await reopened.create(
                HarnessTurn(
                    engagement_id=project.id,
                    harness_session_id=harness.id,
                    origin="analysis",
                    status="complete",
                    prompt="Settled fixture",
                    response="Saved history",
                )
            )
            effects = []

            async def read(arguments):
                effects.append(arguments["number"])
                await asyncio.sleep(0.01)
                return {"number": arguments["number"]}

            registry = ToolRegistry()
            registry.register(
                AnalysisTool(
                    ToolSpec(
                        name="fixture_read",
                        description="Read an inert fixture",
                        input_schema={
                            "type": "object",
                            "properties": {"number": {"type": "integer"}},
                            "required": ["number"],
                        },
                        output_schema={"type": "object"},
                        risk_class=RiskClass.LOCAL_READ,
                    ),
                    read,
                )
            )
            broker = ToolBroker(
                registry=registry,
                policy_engine=PolicyEngine(),
                runner=AnalysisOnlyRunner(),
                ledger=StoreToolLedger(reopened, enforce_run_budget=False),
                workspace_resolver=lambda _: tmp_path,
            )

            async def recover():
                assert await recovered.resume_turns_stopped_by_core() == [turn_id]
                return [item async for item in recovered.follow_provider_turn(turn_id)]

            async def chat(number):
                prepared = await recovered.prepare(
                    ChatCompletionRequest(
                        provider_id=profile.id,
                        engagement_id=project.id,
                        model="model-a",
                        messages=[
                            {"role": "user", "content": f"Concurrent fixture {number}"}
                        ],
                        include_knowledge=False,
                    )
                )
                assert (await recovered.complete(prepared)).message.content

            async def tool(number):
                result = await broker.execute(
                    ToolInvocation(
                        engagement_id=project.id,
                        run_id=f"fixture-{number}",
                        tool_name="fixture_read",
                        arguments={"number": number},
                        workspace=tmp_path,
                        idempotency_key=f"fixture-{number}",
                    ),
                    ScopePolicy(engagement_id=project.id),
                )
                assert result.output == {"number": number}

            app = create_app(reopened, auth_token="synthetic-recovery-token")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://fixture.test",
                headers={"Authorization": "Bearer synthetic-recovery-token"},
            ) as client:

                async def listing():
                    response = await client.get(
                        "/api/v1/harness-sessions", params={"engagement_id": project.id}
                    )
                    assert response.status_code == 200
                    assert [item["id"] for item in response.json()] == [harness.id]

                async def authenticate():
                    assert (await client.get("/api/v1/auth/devices")).status_code == 200

                events, *_ = await asyncio.gather(
                    recover(),
                    *[
                        operation
                        for number in range(12)
                        for operation in (
                            chat(number),
                            tool(number),
                            listing(),
                            authenticate(),
                        )
                    ],
                )
            assert sorted(effects) == list(range(12))
            assert (
                len(await reopened.list_entities(ToolCall, engagement_id=project.id))
                == 12
            )
            assert any(name == "done" for name, _ in events), events
            assert (
                await reopened.get(ChatTurn, turn_id)
            ).status == ChatTurnStatus.COMPLETE
            messages = await reopened.list_session_entities(
                ChatMessage, interrupted.session_id
            )
            assert (
                len([item for item in messages if item.role == ChatRole.ASSISTANT]) == 1
            )
            assert (
                len(
                    await reopened.list_session_entities(
                        ChatTurn, interrupted.session_id
                    )
                )
                == 1
            )
        finally:
            await recovered.shutdown()
            await _close(reopened)

    (await exercise())


async def test_async_real_tool_loop_and_restart_replay_committed_result_once(tmp_path):
    async def exercise():
        path = tmp_path / "tools.db"
        store = NebulaStore(path)
        effects = []

        async def read(arguments):
            effects.append(arguments)
            await asyncio.sleep(0.01)
            return {"value": arguments["value"]}

        spec = ToolSpec(
            name="safe_read",
            description="Read a synthetic fixture",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            risk_class=RiskClass.LOCAL_READ,
        )
        registry = ToolRegistry()
        registry.register(AnalysisTool(spec, read))
        artifacts = ArtifactStore(tmp_path / "artifacts")

        def broker(target):
            return ToolBroker(
                registry=registry,
                policy_engine=PolicyEngine(),
                runner=AnalysisOnlyRunner(),
                ledger=StoreToolLedger(target, enforce_run_budget=False),
                workspace_resolver=lambda _: tmp_path,
                evidence_recorder=StoreToolEvidenceRecorder(target, artifacts),
            )

        project = await store.create(Engagement(id="project", name="Tool fixture"))
        profile = await store.create(
            ProviderProfile(
                id="provider",
                name="Fixture",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a"],
                capabilities={"streaming": True, "tool_calling": True},
            )
        )
        session = await store.create(
            ChatSession(
                engagement_id=project.id,
                title="Tool loop",
                provider_profile_id=profile.id,
                model="model-a",
                metadata={
                    "message_count": 1,
                    "last_sequence": 1,
                    "initial_title_state": "generated",
                },
            )
        )
        user = await store.create(
            ChatMessage(
                engagement_id=project.id,
                session_id=session.id,
                sequence=1,
                role=ChatRole.USER,
                content="Use the fixture once.",
            )
        )
        turn = await store.create(
            ChatTurn(
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                tools_enabled=True,
                max_tool_calls=5,
            )
        )
        provider = ScriptedProvider(
            [
                _response(
                    calls=[
                        ModelToolCall(
                            id="fixture-call",
                            name="safe_read",
                            arguments={"value": "recorded"},
                        )
                    ]
                ),
                _response(
                    calls=[
                        ModelToolCall(id="finish", name="finish_response", arguments={})
                    ]
                ),
                _response(text="Fixture complete."),
            ]
        )
        prepared = PreparedChat(
            provider=provider,
            provider_profile=profile,
            model_request=ModelRequest(
                model="model-a",
                messages=[ModelMessage(role="user", content=user.content)],
            ),
            resolved_model="model-a",
            citations=[],
            engagement_id=project.id,
            session=session,
            pending_session=None,
            stored_messages=[user],
            new_messages=[],
            tools_enabled=True,
            turn=turn,
            inputs_persisted=True,
            tool_components=RuntimeToolComponents(
                broker=broker(store),
                scope=ScopePolicy(engagement_id=project.id),
                workspace=tmp_path,
                specs={spec.name: spec},
                runtime_digest="fixture",
            ),
        )
        service = ChatService(store, worker_id="first-worker")
        try:
            completion = await service.complete(prepared)
            assert completion.message.content == "Fixture complete."
            assert len(effects) == 1
            calls = await store.list_entities(ToolCall, engagement_id=project.id)
            [call] = [item for item in calls if item.tool_name == "safe_read"]
            assert call.status == ToolCallStatus.COMPLETE
            assert call.idempotency_key
            assert call.metadata["provider_history_intent"]
        finally:
            await service.shutdown()
            await _close(store)
        reopened = NebulaStore(path)
        try:
            invocation = ToolInvocation(
                id=call.id,
                engagement_id=project.id,
                run_id=call.run_id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=session.id,
                chat_turn_id=turn.id,
                tool_name="safe_read",
                arguments=call.arguments,
                workspace=tmp_path,
                idempotency_key=call.idempotency_key,
                provider_call_id="fixture-call",
                provider_step=0,
                provider_history_intent=call.metadata["provider_history_intent"],
            )
            result = await broker(reopened).execute(
                invocation, ScopePolicy(engagement_id=project.id)
            )
            assert result.output
            assert len(effects) == 1
            assert reopened.database.pool_snapshot()["checked_out"] == 0
        finally:
            await _close(reopened)

    (await exercise())


pytestmark = pytest.mark.anyio
