"""The same Core goal follows an explicitly selected runtime at an idle boundary."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from contextlib import asynccontextmanager

import pytest

from nebula.v3.chat import ChatHistoryConflict, ChatRuntimeSwitchRequest, ChatService
from nebula.v3.chat_assistant_config import (
    ChatAssistantConfigPatch,
    update_chat_assistant_config,
)
from nebula.v3.chat_goals import ChatGoalService, GoalWrite
from nebula.v3.domain import (
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatQueue,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessSession,
    ProviderProfile,
)
from nebula.v3.storage import ConflictError, NebulaStore
from tests.v3.test_chat_api import ApiChatProvider


@asynccontextmanager
async def fixture(tmp_path):
    store = NebulaStore(tmp_path / "switch.db")
    project = await store.create(Engagement(name="Synthetic runtime switch"))
    provider = await store.create(
        ProviderProfile(
            name="Inert local provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"default_model": "model-a"},
        )
    )
    vendor = await store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id="fixture-harness",
            model="fixture-model",
        )
    )
    session = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Same conversation",
            backend=ChatBackend.HARNESS,
            harness_profile_id="fixture-harness",
            harness_session_id=vendor.id,
            model="fixture-model",
            metadata={
                "assistant_config": {
                    "backend": "harness",
                    "profile_id": "fixture-harness",
                    "model": "fixture-model",
                }
            },
        )
    )
    chat = ChatService(
        store,
        provider_factory=lambda profile: ApiChatProvider(profile.id),
        workspace_resolver=lambda _: tmp_path,
    )
    try:
        yield store, chat, session, provider
    finally:
        await chat.shutdown()
        await chat.runtime_switcher.shutdown()
        await store.event_history.close()
        await store.database.dispose()


def request(session, provider):
    return ChatRuntimeSwitchRequest(
        provider_id=provider.id,
        model="model-a",
        expected_session_revision=session.revision,
    )


@pytest.mark.parametrize(
    "status",
    [
        ChatGoalStatus.DRAFT,
        ChatGoalStatus.RUNNING,
        ChatGoalStatus.PAUSED,
        ChatGoalStatus.BLOCKED,
    ],
)
async def test_idle_switch_retains_goal_identity_status_and_budgets(tmp_path, status):
    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            goal = await store.create(
                ChatGoal(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    objective="Retain this goal",
                    completion_criteria=["Saved evidence"],
                    status=status,
                    blocked_reason="Operator review"
                    if status == ChatGoalStatus.BLOCKED
                    else None,
                    step_budget=3,
                )
            )
            saved = await chat.apply_runtime_switch(
                session.id, request(session, provider)
            )
            assert saved.id == session.id and saved.backend == ChatBackend.PROVIDER
            assert (
                saved.provider_profile_id == provider.id
                and saved.harness_session_id is None
            )
            assert saved.metadata["assistant_config"]["backend"] == "provider"
            assert await store.get(ChatGoal, goal.id) == goal

    (await exercise())


async def test_active_turn_selection_is_persisted_then_activates_after_settlement(
    tmp_path,
):
    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            turn = await store.create(
                ChatTurn(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    backend=ChatBackend.HARNESS,
                    model="fixture-model",
                    status=ChatTurnStatus.WAITING_APPROVAL,
                )
            )
            saved = await chat.apply_runtime_switch(
                session.id, request(session, provider)
            )
            assert saved.backend == ChatBackend.HARNESS
            assert saved.metadata["assistant_config"]["backend"] == "harness"
            assert (
                saved.metadata["pending_runtime_switch"]["binding"][
                    "provider_profile_id"
                ]
                == provider.id
            )
            assert await chat.runtime_switcher.settle(session.id) is None
            await store.update(
                ChatTurn,
                turn.id,
                {"status": ChatTurnStatus.COMPLETE},
                expected_revision=turn.revision,
            )
            for _ in range(100):
                current = await store.get(ChatSession, session.id)
                if current.backend == ChatBackend.PROVIDER:
                    break
                await asyncio.sleep(0.01)
            assert current.backend == ChatBackend.PROVIDER
            assert "pending_runtime_switch" not in current.metadata

    (await exercise())


async def test_saved_follow_up_preserves_its_authorized_runtime_until_removed(tmp_path):
    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            queue = await store.create(
                ChatQueue(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    paused=True,
                    items=[
                        {
                            "id": "saved",
                            "status": "queued",
                            "request": {
                                "backend": "harness",
                                "harness_profile_id": "fixture-harness",
                            },
                        }
                    ],
                )
            )
            selected = await chat.apply_runtime_switch(
                session.id, request(session, provider)
            )
            assert selected.backend == ChatBackend.HARNESS
            assert (await store.get(ChatQueue, queue.id)).items == queue.items
            await store.update(
                ChatQueue, queue.id, {"items": []}, expected_revision=queue.revision
            )
            current = await chat.runtime_switcher.settle(session.id)
            if current is None:
                await asyncio.sleep(0.05)
                current = await store.get(ChatSession, session.id)
            assert current.backend == ChatBackend.PROVIDER

    (await exercise())


async def test_metadata_only_settings_writer_uses_the_same_runtime_switch(tmp_path):
    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            saved = await update_chat_assistant_config(
                store,
                session,
                ChatAssistantConfigPatch(
                    backend="provider", profile_id=provider.id, model="model-a"
                ),
                expected_revision=session.revision,
                workspace_resolver=lambda _: tmp_path,
            )
            assert saved.backend == ChatBackend.PROVIDER
            assert (
                saved.metadata["assistant_config"]["profile_id"]
                == saved.provider_profile_id
            )

    (await exercise())


async def test_explicit_resume_repairs_legacy_split_runtime_without_a_message(tmp_path):
    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            session = await store.update(
                ChatSession,
                session.id,
                {
                    "metadata": {
                        "assistant_config": {
                            "backend": "provider",
                            "profile_id": provider.id,
                            "model": "model-a",
                        }
                    }
                },
                expected_revision=session.revision,
            )
            goal = await store.create(
                ChatGoal(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    objective="Resume here",
                    completion_criteria=["Result"],
                    status=ChatGoalStatus.PAUSED,
                )
            )
            resumed = await ChatGoalService(store).write(
                session.id, GoalWrite(action="resume", expected_revision=goal.revision)
            )
            assert resumed.id == goal.id and resumed.status == ChatGoalStatus.RUNNING
            assert (
                await store.get(ChatSession, session.id)
            ).backend == ChatBackend.PROVIDER

    (await exercise())


async def test_competing_runtime_selection_rejects_stale_revision(tmp_path):
    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            await chat.apply_runtime_switch(session.id, request(session, provider))
            with pytest.raises(ChatHistoryConflict):
                await chat.apply_runtime_switch(session.id, request(session, provider))

    (await exercise())


async def _settled_session(store, session_id):
    async with asyncio.timeout(3):
        while True:
            session = await store.get(ChatSession, session_id)
            pending = session.metadata.get("pending_runtime_switch")
            if not pending or pending.get("status") == "needs_review":
                return session
            await asyncio.sleep(0.01)


async def test_goal_claim_release_activates_selection_without_changing_goal_state(
    tmp_path,
):
    from nebula.v3.domain import utc_now

    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            goal = await store.create(
                ChatGoal(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    objective="Keep the goal",
                    completion_criteria=["Done"],
                    status=ChatGoalStatus.RUNNING,
                    execution_owner_id="worker",
                    execution_claim_id="claim",
                    execution_claimed_at=utc_now(),
                )
            )
            selected = await chat.apply_runtime_switch(
                session.id, request(session, provider)
            )
            assert selected.backend == ChatBackend.HARNESS
            released = await store.update(
                ChatGoal,
                goal.id,
                {
                    "execution_owner_id": None,
                    "execution_claim_id": None,
                    "execution_claimed_at": None,
                },
                expected_revision=goal.revision,
            )
            assert (
                await _settled_session(store, session.id)
            ).backend == ChatBackend.PROVIDER
            assert await store.get(ChatGoal, goal.id) == released

    (await exercise())


async def test_deferred_compaction_confirmation_requires_review_after_context_changes(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            turn = await store.create(
                ChatTurn(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    backend=ChatBackend.HARNESS,
                    model="fixture-model",
                    status=ChatTurnStatus.WAITING_APPROVAL,
                )
            )
            before = SimpleNamespace(
                compatible=True,
                requires_compaction_confirmation=True,
                confirmation_token="old-context",
            )
            binding = chat.runtime_switcher.binding(
                {"backend": "provider", "profile_id": provider.id, "model": "model-a"}
            )

            async def original_context(*args, **kwargs):
                return before

            monkeypatch.setattr(chat, "runtime_switch_preflight", original_context)
            await chat.runtime_switcher.select(
                session,
                session.metadata,
                binding,
                expected_revision=session.revision,
                confirmation_token="old-context",
            )

            async def changed_context(*args, **kwargs):
                return SimpleNamespace(
                    compatible=True,
                    requires_compaction_confirmation=True,
                    confirmation_token="new-context",
                )

            monkeypatch.setattr(chat, "runtime_switch_preflight", changed_context)
            await store.update(
                ChatTurn,
                turn.id,
                {"status": ChatTurnStatus.COMPLETE},
                expected_revision=turn.revision,
            )
            current = await _settled_session(store, session.id)
            assert current.backend == ChatBackend.HARNESS
            assert (
                current.metadata["pending_runtime_switch"]["status"] == "needs_review"
            )
            assert (
                "confirm"
                in current.metadata["pending_runtime_switch"]["reason"].lower()
            )

    (await exercise())


async def test_simultaneous_switches_commit_only_one_revision(tmp_path):
    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            results = await asyncio.gather(
                *(
                    chat.apply_runtime_switch(session.id, request(session, provider))
                    for _ in range(2)
                ),
                return_exceptions=True,
            )
            assert sum(isinstance(result, ChatSession) for result in results) == 1
            assert (
                sum(
                    isinstance(result, (ConflictError, ChatHistoryConflict))
                    for result in results
                )
                == 1
            )
            current = await store.get(ChatSession, session.id)
            assert current.revision == session.revision + 1
            assert current.backend == ChatBackend.PROVIDER

    (await exercise())


async def test_resumed_harness_goal_continues_on_provider_in_the_same_conversation(
    tmp_path,
):
    from nebula.v3.domain import ChatMessage, ChatRole

    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            prior = await store.create(
                ChatMessage(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    sequence=1,
                    role=ChatRole.USER,
                    content="Retained harness conversation.",
                )
            )
            session = await store.update(
                ChatSession,
                session.id,
                {
                    "metadata": {
                        **session.metadata,
                        "message_count": 1,
                        "last_sequence": 1,
                    }
                },
                expected_revision=session.revision,
            )
            goal = await store.create(
                ChatGoal(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    objective="Continue here",
                    completion_criteria=["Saved response"],
                    status=ChatGoalStatus.BLOCKED,
                    blocked_reason="Prior harness/provider split",
                    step_budget=1,
                )
            )
            await chat.apply_runtime_switch(session.id, request(session, provider))
            await ChatGoalService(store).write(
                session.id, GoalWrite(action="resume", expected_revision=goal.revision)
            )
            turn_id = await chat.dispatch_running_goal(
                session.id, "Continue the retained goal."
            )
            assert turn_id is not None
            events = [item async for item in chat.follow_provider_turn(turn_id)]
            assert any(name == "done" for name, _ in events), events
            turn = await store.get(ChatTurn, turn_id)
            assert (
                turn.backend == ChatBackend.PROVIDER
                and turn.status == ChatTurnStatus.COMPLETE
            )
            assert turn.session_id == session.id and turn.goal_id == goal.id
            assert (
                await store.get(ChatSession, session.id)
            ).backend == ChatBackend.PROVIDER
            messages = await store.list_session_entities(ChatMessage, session.id)
            assert any(message.id == prior.id for message in messages)
            assert any(message.content == "API chat works." for message in messages)
            assert (
                await chat.dispatch_running_goal(session.id, "Continue once more.")
                is None
            )
            current = await store.get(ChatGoal, goal.id)
            assert current.current_step == 1 and current.status == ChatGoalStatus.PAUSED
            assert len(await store.list_session_entities(ChatTurn, session.id)) == 1
            await chat.shutdown()

    (await exercise())


async def test_deferred_switch_preserves_later_settings_and_transcript_cursor(tmp_path):
    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            turn = await store.create(
                ChatTurn(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    backend=ChatBackend.HARNESS,
                    model=session.model,
                    status=ChatTurnStatus.WAITING_APPROVAL,
                )
            )
            pending = await chat.apply_runtime_switch(
                session.id, request(session, provider)
            )
            metadata = {
                **pending.metadata,
                "last_sequence": 17,
                "include_knowledge": False,
                "assistant_config": {
                    **pending.metadata["assistant_config"],
                    "planning_enabled": True,
                },
            }
            await store.update(
                ChatSession,
                session.id,
                {"metadata": metadata},
                expected_revision=pending.revision,
            )
            await store.update(
                ChatTurn,
                turn.id,
                {"status": ChatTurnStatus.COMPLETE},
                expected_revision=turn.revision,
            )
            saved = await _settled_session(store, session.id)
            assert saved.backend == ChatBackend.PROVIDER
            assert saved.metadata["last_sequence"] == 17
            assert saved.metadata["include_knowledge"] is False
            assert saved.metadata["assistant_config"]["planning_enabled"] is True

    (await exercise())


async def test_conflicting_setting_edit_keeps_selection_for_review(tmp_path):
    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            turn = await store.create(
                ChatTurn(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    backend=ChatBackend.HARNESS,
                    model=session.model,
                    status=ChatTurnStatus.WAITING_APPROVAL,
                )
            )
            pending = await chat.apply_runtime_switch(
                session.id, request(session, provider)
            )
            metadata = {
                **pending.metadata,
                "tools_enabled": True,
                "assistant_config": {
                    **pending.metadata["assistant_config"],
                    "tools_enabled": True,
                },
            }
            await store.update(
                ChatSession,
                session.id,
                {"metadata": metadata},
                expected_revision=pending.revision,
            )
            await store.update(
                ChatTurn,
                turn.id,
                {"status": ChatTurnStatus.COMPLETE},
                expected_revision=turn.revision,
            )
            saved = await _settled_session(store, session.id)
            assert saved.backend == ChatBackend.HARNESS
            assert saved.metadata["tools_enabled"] is True
            assert saved.metadata["pending_runtime_switch"]["status"] == "needs_review"

    (await exercise())


@pytest.mark.parametrize("changed", ["privacy", "mode"])
async def test_deferred_harness_selection_revalidates_policy_and_options(
    tmp_path, changed
):
    from nebula.v3.domain import HarnessProfile, ScopePolicy

    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            session = await chat.apply_runtime_switch(
                session.id, request(session, provider)
            )
            profile = await store.create(
                HarnessProfile(
                    name="Synthetic target",
                    kind="codex_app_server",
                    executable="/bin/true",
                    default_model="fixture-target",
                    capabilities={"models": ["fixture-target"], "modes": ["ask"]},
                )
            )
            turn = await store.create(
                ChatTurn(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    provider_profile_id=provider.id,
                    model=session.model,
                    status=ChatTurnStatus.WAITING_APPROVAL,
                )
            )
            pending = await update_chat_assistant_config(
                store,
                session,
                ChatAssistantConfigPatch(
                    backend="harness",
                    profile_id=profile.id,
                    model="fixture-target",
                    harness_mode="ask",
                ),
                expected_revision=session.revision,
                workspace_resolver=lambda _: tmp_path,
            )
            assert pending.backend == ChatBackend.PROVIDER
            if changed == "mode":
                await store.update(
                    HarnessProfile,
                    profile.id,
                    {"capabilities": {"models": ["fixture-target"], "modes": []}},
                    expected_revision=profile.revision,
                )
            else:
                policy = await store.create(
                    ScopePolicy(engagement_id=session.engagement_id, local_only=True)
                )
                project = await store.get(Engagement, session.engagement_id)
                await store.update(
                    Engagement,
                    project.id,
                    {"scope_policy_id": policy.id},
                    expected_revision=project.revision,
                )
            await store.update(
                ChatTurn,
                turn.id,
                {"status": ChatTurnStatus.COMPLETE},
                expected_revision=turn.revision,
            )
            saved = await _settled_session(store, session.id)
            assert saved.backend == ChatBackend.PROVIDER
            assert saved.metadata["pending_runtime_switch"]["status"] == "needs_review"
            assert (
                changed == "privacy"
                and "local-only" in saved.metadata["pending_runtime_switch"]["reason"]
                or changed == "mode"
                and "mode" in saved.metadata["pending_runtime_switch"]["reason"]
            )

    (await exercise())


async def test_target_revision_change_between_preflight_and_commit_is_rejected(
    tmp_path, monkeypatch
):
    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            original = chat.runtime_switch_preflight
            calls = 0

            async def racing_preflight(*args, **kwargs):
                nonlocal calls
                result = await original(*args, **kwargs)
                calls += 1
                if calls == 2:
                    await store.update(
                        ProviderProfile,
                        provider.id,
                        {"enabled": False},
                        expected_revision=provider.revision,
                    )
                return result

            monkeypatch.setattr(chat, "runtime_switch_preflight", racing_preflight)
            with pytest.raises(ConflictError, match="during validation"):
                await chat.apply_runtime_switch(session.id, request(session, provider))
            assert (
                await store.get(ChatSession, session.id)
            ).backend == ChatBackend.HARNESS

    (await exercise())


async def test_deferred_compaction_consent_survives_only_its_own_selection_revision(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from nebula.v3.chat import ChatRuntimeSwitchPreflightRequest

    async def exercise():
        async with fixture(tmp_path) as (store, chat, session, provider):
            await store.update(
                ProviderProfile,
                provider.id,
                {
                    "metadata": {
                        **provider.metadata,
                        "model_catalog_revision": "synthetic-small",
                        "model_descriptors": [
                            {
                                "id": "model-a",
                                "context_window": 4000,
                                "max_input_tokens": 3500,
                                "max_output_tokens": 500,
                            }
                        ],
                    }
                },
                expected_revision=provider.revision,
            )

            async def context(*args, **kwargs):
                return SimpleNamespace(estimated_input_tokens=4000)

            monkeypatch.setattr(chat, "context_status", context)
            turn = await store.create(
                ChatTurn(
                    engagement_id=session.engagement_id,
                    session_id=session.id,
                    backend=ChatBackend.HARNESS,
                    model=session.model,
                    status=ChatTurnStatus.WAITING_APPROVAL,
                )
            )
            proposal = ChatRuntimeSwitchPreflightRequest(
                provider_id=provider.id,
                model="model-a",
                expected_session_revision=session.revision,
                max_output_tokens=200,
            )
            preflight = await chat.runtime_switch_preflight(session.id, proposal)
            assert (
                preflight.requires_compaction_confirmation
                and preflight.confirmation_token
            )
            selected = await chat.apply_runtime_switch(
                session.id,
                ChatRuntimeSwitchRequest(
                    **proposal.model_dump(),
                    confirmation_token=preflight.confirmation_token,
                ),
            )
            assert (
                selected.metadata["pending_runtime_switch"]["max_output_tokens"] == 200
            )
            await store.update(
                ChatTurn,
                turn.id,
                {"status": ChatTurnStatus.COMPLETE},
                expected_revision=turn.revision,
            )
            saved = await _settled_session(store, session.id)
            assert saved.backend == ChatBackend.PROVIDER
            assert "pending_runtime_switch" not in saved.metadata

    (await exercise())


pytestmark = pytest.mark.anyio
