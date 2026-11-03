"""Boundary checks for the benchmark-only shared-resource model."""

import asyncio
from contextvars import ContextVar
import struct

import pytest

from scripts.benchmarks.core_budget_model import (
    BudgetFull,
    FairBudget,
    MAX_FRAME,
    read_frame,
)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def test_noisy_owner_cannot_overtake_waiting_owners():
    async def scenario():
        release = asyncio.Event()

        async def run(payload):
            if payload.get("hold"):
                await release.wait()
            return payload

        budget = FairBudget(run, concurrency=1, capacity=16)
        first = asyncio.create_task(
            budget.run("blocker", {"kind": "cpu", "hold": True})
        )
        await until(lambda: budget.active["blocker"])
        jobs = [
            asyncio.create_task(budget.run(owner, {"kind": "cpu"}))
            for owner in ("noisy", "noisy", "noisy", "quiet-a", "quiet-b")
        ]
        await until(lambda: len(budget.jobs) == 6)
        release.set()
        await asyncio.gather(first, *jobs)
        assert [item["owner"] for item in budget.trace] == [
            "blocker",
            "noisy",
            "quiet-a",
            "quiet-b",
            "noisy",
            "noisy",
        ]
        assert budget.snapshot()["max_owner_active"] == 1
        await budget.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("setting", ["global", "owner"])
def test_admission_limits_reject_without_growing_reservations(setting):
    async def scenario():
        release = asyncio.Event()

        async def run(payload):
            await release.wait()
            return payload

        budget = FairBudget(run, capacity=1 if setting == "global" else 8, per_owner=1)
        first = asyncio.create_task(budget.run("a", {"kind": "cpu"}))
        await until(lambda: budget.snapshot()["active"] == 1)
        before = budget.snapshot()
        with pytest.raises(BudgetFull):
            await budget.run("b" if setting == "global" else "a", {"kind": "cpu"})
        assert budget.snapshot()["admitted"] == 1
        assert budget.snapshot()["input_bytes"] == before["input_bytes"]
        release.set()
        await first
        assert budget.snapshot()["admitted"] == budget.snapshot()["input_bytes"] == 0
        await budget.shutdown()

    asyncio.run(scenario())


def test_byte_limit_counts_accepted_work_until_it_finishes():
    async def scenario():
        release = asyncio.Event()

        async def run(payload):
            await release.wait()
            return payload

        budget = FairBudget(run, byte_limit=50)
        task = asyncio.create_task(budget.run("a", {"kind": "cpu", "text": "x" * 10}))
        await until(lambda: budget.snapshot()["active"] == 1)
        with pytest.raises(BudgetFull):
            await budget.run("b", {"kind": "cpu", "text": "x" * 10})
        release.set()
        await task
        assert (await budget.run("b", {"kind": "cpu"}))["kind"] == "cpu"
        assert budget.bytes == 0
        await budget.shutdown()

    asyncio.run(scenario())


def test_cancellation_never_executes_a_queued_job_or_frees_running_capacity():
    async def scenario():
        release = asyncio.Event()
        executed = []

        async def run(payload):
            executed.append(payload["tag"])
            await release.wait()
            return payload

        budget = FairBudget(run, concurrency=1, capacity=2)
        running = asyncio.create_task(
            budget.run("a", {"kind": "cpu", "tag": "running"})
        )
        await until(lambda: bool(executed))
        queued = asyncio.create_task(budget.run("b", {"kind": "cpu", "tag": "queued"}))
        await until(lambda: len(budget.jobs) == 2)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert budget.snapshot()["active"] == budget.snapshot()["admitted"] == 1
        later = asyncio.create_task(budget.run("c", {"kind": "cpu", "tag": "later"}))
        await until(lambda: len(budget.jobs) == 2)
        with pytest.raises(BudgetFull):
            await budget.run("d", {"kind": "cpu"})
        release.set()
        await later
        assert executed == ["running", "later"]
        assert budget.snapshot()["discarded"] == 1
        assert budget.snapshot()["admitted"] == budget.bytes == 0
        await budget.shutdown()

    asyncio.run(scenario())


def test_start_callback_uses_request_context_and_a_failure_does_not_dispatch():
    async def scenario():
        owner = ContextVar("synthetic_operator", default="none")
        dispatched = []
        callbacks = []

        async def run(payload):
            dispatched.append(payload)
            return payload

        def callback():
            callbacks.append(owner.get())
            raise ValueError("synthetic callback failure")

        budget = FairBudget(run)
        budget.start()
        owner.set("request-operator")
        with pytest.raises(ValueError, match="synthetic callback"):
            await budget.run("a", {"kind": "cpu"}, on_start=callback)
        assert callbacks == ["request-operator"] and not dispatched
        assert budget.snapshot()["admitted"] == budget.bytes == 0
        await budget.run("b", {"kind": "cpu"})
        assert len(dispatched) == 1
        await budget.shutdown()

    asyncio.run(scenario())


def test_compute_failure_releases_capacity_and_later_work_succeeds():
    async def scenario():
        async def run(payload):
            if payload["kind"] == "fail":
                raise RuntimeError("synthetic executor failure")
            return payload

        budget = FairBudget(run, capacity=1)
        with pytest.raises(RuntimeError, match="executor failure"):
            await budget.run("a", {"kind": "fail"})
        assert budget.snapshot()["failed"] == 1
        assert await budget.run("a", {"kind": "cpu"}) == {"kind": "cpu"}
        assert budget.snapshot()["admitted"] == budget.bytes == 0
        await budget.shutdown()

    asyncio.run(scenario())


def test_shutdown_cancels_pending_jobs_and_rejects_new_work():
    async def scenario():
        release = asyncio.Event()

        async def run(payload):
            await release.wait()
            return payload

        budget = FairBudget(run, concurrency=1)
        active = asyncio.create_task(budget.run("a", {"kind": "cpu"}))
        await until(lambda: budget.snapshot()["active"] == 1)
        pending = asyncio.create_task(budget.run("b", {"kind": "cpu"}))
        await until(lambda: len(budget.jobs) == 2)
        closing = asyncio.create_task(budget.shutdown())
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(RuntimeError, match="closed"):
            await budget.run("c", {"kind": "cpu"})
        release.set()
        await asyncio.gather(active, closing)
        assert budget.snapshot()["admitted"] == budget.bytes == 0

    asyncio.run(scenario())


def test_rpc_refuses_an_oversized_frame_before_reading_its_body():
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", MAX_FRAME + 1))
        with pytest.raises(ValueError, match="frame exceeds"):
            await read_frame(reader)

    asyncio.run(scenario())


def test_model_uses_current_chat_binding_after_harness_rollover(tmp_path):
    from scripts.benchmarks.core_budget_model import attached_chat_owner
    from nebula.v3.domain import ChatSession, Engagement, HarnessProfile, HarnessSession
    from nebula.v3.storage import NebulaStore

    store = NebulaStore(tmp_path / "synthetic.db")
    try:
        project = store.create(Engagement(id="project", name="Synthetic ownership"))
        profile = store.create(
            HarnessProfile(
                id="profile",
                name="Inert fixture",
                kind="codex_app_server",
                executable="/bin/true",
                default_model="fixture",
            )
        )
        for identity in ("old-harness", "current-harness", "naming-harness"):
            store.create(
                HarnessSession(
                    id=identity,
                    engagement_id=project.id,
                    harness_profile_id=profile.id,
                    model="fixture",
                )
            )
        chat = store.create(
            ChatSession(
                id="chat",
                engagement_id=project.id,
                title="Synthetic operator title",
                backend="harness",
                harness_profile_id=profile.id,
                harness_session_id="old-harness",
                model="fixture",
            )
        )
        assert attached_chat_owner(store, "old-harness") == chat.id
        store.update(
            ChatSession,
            chat.id,
            {"harness_session_id": "current-harness"},
            expected_revision=chat.revision,
        )
        assert attached_chat_owner(store, "current-harness") == chat.id
        assert attached_chat_owner(store, "old-harness") is None
        assert attached_chat_owner(store, "naming-harness") is None
    finally:
        store.database.dispose()


@pytest.mark.parametrize("eventually_succeeds", [False, True])
def test_report_status_retries_are_bounded_and_every_failure_is_recorded(
    eventually_succeeds,
):
    import httpx
    from scripts.benchmarks.core_resource_budget import read_core_observation

    async def scenario():
        calls = []
        failures = []

        async def handle(request):
            calls.append(request)
            if len(calls) < 3 or not eventually_succeeds:
                raise httpx.ReadError("synthetic observation failure", request=request)
            return httpx.Response(200, json={"status": "completed"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://owned-model.test"
        ) as client:
            if eventually_succeeds:
                result = await read_core_observation(
                    client,
                    "/api/v1/report-renders/synthetic",
                    failures.append,
                    retry_status=True,
                )
                assert result == {"status": "completed"}
                assert len(failures) == 2 and all(row["retried"] for row in failures)
            else:
                with pytest.raises(httpx.ReadError):
                    await read_core_observation(
                        client,
                        "/api/v1/report-renders/synthetic",
                        failures.append,
                        retry_status=True,
                    )
                assert len(failures) == 3 and failures[-1]["retried"] is False
        assert len(calls) == 3 and all(request.method == "GET" for request in calls)

    asyncio.run(scenario())


def test_ordinary_latency_observations_never_retry_transport_errors():
    import httpx
    from scripts.benchmarks.core_resource_budget import read_core_observation

    async def scenario():
        calls = []
        failures = []

        async def handle(request):
            calls.append(request)
            raise httpx.ReadError("synthetic ordinary-read failure", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://owned-model.test"
        ) as client:
            with pytest.raises(httpx.ReadError):
                await read_core_observation(
                    client,
                    "/api/v1/engagements/synthetic/assistant-defaults",
                    failures.append,
                )
        assert len(calls) == len(failures) == 1
        assert failures[0]["retried"] is False

    asyncio.run(scenario())
