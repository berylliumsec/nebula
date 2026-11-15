import pytest
from nebula.v3.domain import (
    AgentRun,
    HarnessSession,
    HarnessSessionStatus,
    HarnessTurn,
    HarnessTurnStatus,
    RunBudget,
    RunStatus,
    RunBackend,
    ChatTokenUsage,
    ChatMessage,
    HarnessTurnOrigin,
)
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_harnesses import _runtime
from tests.v3.test_turn_tool_bookkeeping import _running_chat_turn

pytestmark = pytest.mark.anyio


async def test_waiting_callback_loses_to_terminal_session(tmp_path):
    store, _engagement, runtime, _chat, _owner, turn = await _running_chat_turn(
        tmp_path
    )
    session = await store.get(HarnessSession, turn.harness_session_id)
    await store.update(
        HarnessSession,
        session.id,
        {"status": HarnessSessionStatus.RUNNING},
        expected_revision=session.revision,
    )
    original = store.update
    stopped = False

    async def update(model, entity_id, changes, **kwargs):
        nonlocal stopped
        if (
            model is HarnessSession
            and changes.get("status") == HarnessSessionStatus.WAITING_APPROVAL
            and not stopped
        ):
            stopped = True
            latest = await store.get(HarnessTurn, turn.id)
            await original(
                HarnessTurn,
                turn.id,
                {"status": HarnessTurnStatus.INTERRUPTED},
                expected_revision=latest.revision,
            )
            latest_session = await store.get(HarnessSession, entity_id)
            await original(
                HarnessSession,
                entity_id,
                {"status": HarnessSessionStatus.CLOSED},
                expected_revision=latest_session.revision,
            )
        return await original(model, entity_id, changes, **kwargs)

    store.update = update
    await runtime._show_waiting_approval(
        turn, turn.harness_session_id, approval_id="approval"
    )
    assert (
        await store.get(HarnessSession, turn.harness_session_id)
    ).status == HarnessSessionStatus.CLOSED
    await runtime.shutdown()


async def test_mission_completion_conflict_after_saved_native_activity(tmp_path):
    store, engagement, profile, _mcp, _adapter, runtime = await _runtime(tmp_path)
    original = store.revise_run_with_event

    async def revise(run_id, plan, **kwargs):
        current = await store.get(AgentRun, run_id)
        await store.update(
            AgentRun,
            run_id,
            {"metadata": {**current.metadata, "saved_native_activity": True}},
            expected_revision=current.revision,
        )
        return await original(run_id, plan, **kwargs)

    store.revise_run_with_event = revise
    run = await runtime.start_mission(
        engagement_id=engagement.id,
        name="Synthetic",
        objective="Finish fake mission",
        profile_id=profile.id,
        model="test-model",
        budget=RunBudget(max_duration_seconds=30),
    )
    await runtime._mission_tasks[run.id]
    saved = await store.get(AgentRun, run.id)
    assert saved.status == RunStatus.COMPLETE
    assert saved.metadata["saved_native_activity"] is True
    await runtime.shutdown()


async def test_remember_subagent_choice_keeps_concurrent_title(tmp_path):
    from nebula.v3.domain import ChatSession

    store, _engagement, runtime, chat, _owner, _turn = await _running_chat_turn(
        tmp_path
    )
    original = store.get
    fired = False

    async def get(model, entity_id):
        nonlocal fired
        record = await original(model, entity_id)
        if model is ChatSession and entity_id == chat.id and not fired:
            fired = True
            await store.update(
                ChatSession,
                entity_id,
                {"title": "Concurrent name"},
                expected_revision=record.revision,
            )
        return record

    store.get = get
    saved = await runtime._remember_chat_provider_subagent(
        chat, {"provider_id": "fake", "model": "fake"}
    )
    assert saved.title == "Concurrent name"
    assert saved.metadata["provider_subagent"] == {
        "provider_id": "fake",
        "model": "fake",
    }
    await runtime.shutdown()


@pytest.mark.parametrize("crash_after_commit", [False, True])
async def test_mission_completion_replay_posts_one_attached_chat_handoff(
    tmp_path, crash_after_commit
):
    store, engagement, runtime, chat, _owner, template = await _running_chat_turn(
        tmp_path
    )
    run = await store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Synthetic",
            status=RunStatus.RUNNING,
            backend=RunBackend.HARNESS,
            harness_profile_id=(
                await store.get(HarnessSession, template.harness_session_id)
            ).harness_profile_id,
            harness_session_id=template.harness_session_id,
        )
    )
    turn = template.model_copy(
        update={
            "id": "mission-turn",
            "origin": HarnessTurnOrigin.MISSION,
            "run_id": run.id,
            "chat_turn_id": None,
            "chat_session_id": None,
        }
    )
    await store.create(turn)
    original = runtime._append_chat_handoff
    if crash_after_commit:

        async def unavailable(*args, **kwargs):
            raise RuntimeError("handoff unavailable after commit")

        runtime._append_chat_handoff = unavailable
        with pytest.raises(RuntimeError, match="after commit"):
            await runtime._complete_owner_once(
                turn, "Synthetic mission finished", ChatTokenUsage()
            )
        runtime._append_chat_handoff = original
    else:
        await runtime._complete_owner_once(
            turn, "Synthetic mission finished", ChatTokenUsage()
        )
    await runtime._complete_owner_once(
        turn, "Synthetic mission finished", ChatTokenUsage()
    )
    messages = await store.list_session_entities(ChatMessage, chat.id)
    assert [
        message.content for message in messages if message.metadata.get("handoff")
    ] == ["Synthetic mission finished"]
    events = await store.replay_events(run.id)
    assert [event.event_type for event in events] == ["run.completed"]
    await runtime.shutdown()
