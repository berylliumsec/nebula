import asyncio
import json

from datetime import timedelta

import pytest
from sqlalchemy import select

from nebula.v3.chat_turn_ledger import ChatTurnLedger
from nebula.v3.database import EntityRow, ProviderTurnQueueRow
from nebula.v3.domain import (
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    utc_now,
)
from nebula.v3.provider_scheduler import ProviderScheduler, ProviderSchedulerConfig
from nebula.v3.storage import NebulaStore


def _store(tmp_path):
    store = NebulaStore(tmp_path / "throughput.db")
    project = store.create(Engagement(id="project", name="Throughput"))
    profile = store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"streaming": True, "tool_calling": True},
        )
    )
    session = store.create(
        ChatSession(
            id="session",
            engagement_id=project.id,
            title="Throughput",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    return store, project, profile, session


def test_long_turn_uses_checkpointed_ledger_without_growing_turn_json(tmp_path):
    store, project, profile, session = _store(tmp_path)
    turn = store.create(
        ChatTurn(
            id="turn",
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.ROUTING,
            queued_at=utc_now(),
        )
    )
    ledger = ChatTurnLedger(store.database)
    baseline_replay_bytes = 0
    compact_replay_bytes = 0
    for step in range(120):
        entry = {
            "step": step,
            "response_group": f"group-{step}",
            "model_call_id": f"call-{step}",
            "tool_call_id": f"tool-{step}",
            "name": "workspace.read",
            "arguments": {"path": f"file-{step}.txt"},
            "status": "complete",
            "provider_result": {"status": "complete", "preview": "x" * 400},
            "result_summary": f"Read file {step}",
        }
        ledger.append(turn.id, entry)
        history = ledger.history(turn)
        checkpoint, replay = ledger.compacted_history(turn)
        baseline_replay_bytes += len(json.dumps(history))
        compact_replay_bytes += len(json.dumps(replay)) + (
            len(json.dumps(checkpoint.summary)) if checkpoint else 0
        )

    checkpoint, replay = ledger.compacted_history(turn)
    assert checkpoint is not None
    assert checkpoint.through_step >= 111
    # The checkpoint advances every 16 folded steps, so the replay is the
    # latest eight groups plus at most 15 steps not folded yet.
    assert len(replay) <= 8 + 15
    assert compact_replay_bytes < baseline_replay_bytes * 0.35
    with store.database.session() as session_db:
        row = session_db.scalar(select(EntityRow).where(EntityRow.id == turn.id))
        assert row is not None
        assert len(json.dumps(row.payload)) < 64 * 1024
        assert row.payload["tool_history"] == []


def test_scheduler_caps_total_and_background_provider_work(tmp_path):
    async def scenario():
        store, project, profile, session = _store(tmp_path)
        scheduler = ProviderScheduler(
            store,
            worker_id="worker",
            config=ProviderSchedulerConfig(
                provider_limit=6,
                background_limit=4,
                per_turn_tool_limit=4,
                global_tool_limit=8,
            ),
        )
        turns = []
        for index in range(20):
            turn = store.create(
                ChatTurn(
                    id=f"turn-{index:02d}",
                    engagement_id=project.id,
                    session_id=session.id,
                    provider_profile_id=profile.id,
                    model="model-a",
                    status=ChatTurnStatus.QUEUED,
                    queued_at=utc_now(),
                    capacity_lane="background" if index < 10 else "direct",
                )
            )
            scheduler.enqueue(turn)
            turns.append(turn)

        release = asyncio.Event()
        active = 0
        active_background = 0
        peak = 0
        peak_background = 0
        all_six = asyncio.Event()

        async def run(turn):
            nonlocal active, active_background, peak, peak_background
            admission = await scheduler.admit(turn.id)
            active += 1
            active_background += turn.capacity_lane == "background"
            peak = max(peak, active)
            peak_background = max(peak_background, active_background)
            if active == 6:
                all_six.set()
            await release.wait()
            active -= 1
            active_background -= turn.capacity_lane == "background"
            await admission.release(turn.id)

        tasks = [asyncio.create_task(run(turn)) for turn in turns]
        await asyncio.wait_for(all_six.wait(), 2)
        assert scheduler.metrics()["active"] == 6
        assert scheduler.metrics()["active_background"] == 4
        release.set()
        await asyncio.gather(*tasks)
        assert peak == 6
        assert peak_background == 4
        assert scheduler.metrics()["queued"] == 0

    asyncio.run(scenario())


def test_queued_cancellation_prevents_provider_admission(tmp_path):
    async def scenario():
        store, project, profile, session = _store(tmp_path)
        scheduler = ProviderScheduler(
            store,
            worker_id="worker",
            config=ProviderSchedulerConfig(provider_limit=1, background_limit=1),
        )
        first = store.create(
            ChatTurn(
                id="first",
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.QUEUED,
                queued_at=utc_now(),
            )
        )
        second = store.create(
            ChatTurn(
                id="second",
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.QUEUED,
                queued_at=utc_now(),
            )
        )
        scheduler.enqueue(first)
        scheduler.enqueue(second)
        first_admission = await scheduler.admit(first.id)
        waiting = asyncio.create_task(scheduler.admit(second.id))
        await asyncio.sleep(0)
        scheduler.cancel(second.id)
        latest = store.get(ChatTurn, second.id)
        store.update(
            ChatTurn,
            second.id,
            {"status": ChatTurnStatus.CANCELLED},
            expected_revision=latest.revision,
        )
        await first_admission.release(first.id)
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert scheduler.metrics()["active"] == 0

    asyncio.run(scenario())


def test_recovery_clears_an_expired_queued_lease(tmp_path):
    store, project, profile, session = _store(tmp_path)
    turn = store.create(
        ChatTurn(
            id="leased",
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.QUEUED,
            queued_at=utc_now(),
        )
    )
    scheduler = ProviderScheduler(store, worker_id="worker")
    scheduler.enqueue(turn)
    with store.database.session() as database_session:
        row = database_session.get(ProviderTurnQueueRow, turn.id)
        assert row is not None
        row.lease_owner = "dead-worker"
        row.lease_expires_at = utc_now() - timedelta(seconds=1)

    assert scheduler.recover() == [turn.id]
    with store.database.session() as database_session:
        recovered = database_session.get(ProviderTurnQueueRow, turn.id)
        assert recovered is not None
        assert recovered.lease_owner is None
        assert recovered.lease_expires_at is None
