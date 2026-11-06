"""Boundary checks for the benchmark-only shared-resource model."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

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


async def test_noisy_owner_cannot_overtake_waiting_owners():
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

    (await scenario())


@pytest.mark.parametrize("setting", ["global", "owner"])
async def test_admission_limits_reject_without_growing_reservations(setting):
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

    (await scenario())


async def test_byte_limit_counts_accepted_work_until_it_finishes():
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

    (await scenario())


async def test_cancellation_never_executes_a_queued_job_or_frees_running_capacity():
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

    (await scenario())


async def test_start_callback_uses_request_context_and_a_failure_does_not_dispatch():
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

    (await scenario())


async def test_compute_failure_releases_capacity_and_later_work_succeeds():
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

    (await scenario())


async def test_shutdown_cancels_pending_jobs_and_rejects_new_work():
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

    (await scenario())


async def test_rpc_refuses_an_oversized_frame_before_reading_its_body():
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", MAX_FRAME + 1))
        with pytest.raises(ValueError, match="frame exceeds"):
            await read_frame(reader)

    (await scenario())


async def test_model_uses_current_chat_binding_after_harness_rollover(tmp_path):
    from scripts.benchmarks.core_budget_model import attached_chat_owner
    from nebula.v3.domain import ChatSession, Engagement, HarnessProfile, HarnessSession
    from nebula.v3.storage import NebulaStore

    store = NebulaStore(tmp_path / "synthetic.db")
    try:
        project = await store.create(
            Engagement(id="project", name="Synthetic ownership")
        )
        profile = await store.create(
            HarnessProfile(
                id="profile",
                name="Inert fixture",
                kind="codex_app_server",
                executable="/bin/true",
                default_model="fixture",
            )
        )
        for identity in ("old-harness", "current-harness", "naming-harness"):
            (
                await store.create(
                    HarnessSession(
                        id=identity,
                        engagement_id=project.id,
                        harness_profile_id=profile.id,
                        model="fixture",
                    )
                )
            )
        chat = await store.create(
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
        (
            await store.update(
                ChatSession,
                chat.id,
                {"harness_session_id": "current-harness"},
                expected_revision=chat.revision,
            )
        )
        assert attached_chat_owner(store, "current-harness") == chat.id
        assert attached_chat_owner(store, "old-harness") is None
        assert attached_chat_owner(store, "naming-harness") is None
    finally:
        (await store.database.dispose())


@pytest.mark.parametrize("eventually_succeeds", [False, True])
async def test_report_status_retries_are_bounded_and_every_failure_is_recorded(
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

    (await scenario())


async def test_ordinary_latency_observations_never_retry_transport_errors():
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

    (await scenario())


async def test_live_resize_grows_then_drains_without_cancelling_running_jobs():
    async def scenario():
        releases = [asyncio.Event() for _ in range(4)]
        started = []

        async def runner(payload):
            started.append(payload["index"])
            await releases[payload["index"]].wait()
            return payload

        budget = FairBudget(runner)
        tasks = [
            asyncio.create_task(budget.run(str(i), {"kind": "cpu", "index": i}))
            for i in range(3)
        ]
        await until(lambda: len(started) == 2)
        budget.resize(3)
        await until(lambda: len(started) == 3)
        budget.resize(1)
        final = asyncio.create_task(budget.run("3", {"kind": "cpu", "index": 3}))
        await until(lambda: budget.snapshot()["admitted"] == 4)
        releases[0].set()
        releases[1].set()
        await until(lambda: budget.snapshot()["active"] == 1)
        assert started == [0, 1, 2] and not tasks[2].done()
        releases[2].set()
        await until(lambda: len(started) == 4)
        releases[3].set()
        await asyncio.gather(*tasks, final)
        assert budget.snapshot()["admitted"] == budget.snapshot()["input_bytes"] == 0
        await budget.shutdown()

    (await scenario())


@pytest.mark.parametrize("policy", ["shared-budget", "admission-only"])
async def test_benchmark_lifespan_reads_the_capacity_gate_with_inert_executor(
    tmp_path, monkeypatch, policy
):
    from argparse import Namespace
    from tests.v3.async_test_support import AsyncTestClient as TestClient
    from scripts.benchmarks import core_resource_budget as benchmark

    observed = []
    quota = [2.0]

    class InertCompute:
        def __init__(self, *args):
            pass

        async def request(self, operation, payload=None):
            assert operation == "metrics"
            return {"cpu_max": f"{int(quota[0] * 100000)} 100000"}

        async def run(self, payload):
            raise AssertionError("No computation may run during this startup check")

    class InertProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def spawn(*command, **options):
        assert command[:3] == ("docker", "--host=unix:///var/run/docker.sock", "update")
        quota[0] = float(command[command.index("--cpus") + 1])
        return InertProcess()

    async def run_app(app, **options):
        async with TestClient(app) as client:
            response = await client.get(
                "/api/v1/host-cpu-budget",
                headers={"Authorization": "Bearer synthetic-budget-startup"},
            )
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["cpu_capacity"] > 0
            assert data["effective_cpu_limit"] == max(0.01, data["cpu_capacity"] / 2)
            assert data["apply_state"] == (
                "applied" if policy == "shared-budget" else "inactive"
            )
            observed.append(data)

    monkeypatch.setenv("NEBULA_RESOURCE_MODEL_TOKEN", "synthetic-budget-startup")
    monkeypatch.setenv(
        "NEBULA_RESOURCE_MODEL_CONTAINER", "nebula-resource-model-startup"
    )
    monkeypatch.setattr(benchmark, "ComputeClient", InertCompute)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("uvicorn.run", run_app)
    (
        await benchmark.serve(
            Namespace(
                directory=str(tmp_path),
                chats=1,
                history=0,
                pdfs=0,
                calls=1,
                cpu_seconds=0.05,
                policy=policy,
                host="127.0.0.1",
                port=0,
                output=tmp_path / "startup.json",
            )
        )
    )
    assert len(observed) == 1


pytestmark = pytest.mark.anyio
