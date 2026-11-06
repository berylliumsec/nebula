"""Owned CPU processes preserve admission, cancellation and shutdown boundaries."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from contextvars import ContextVar
import os
import threading

import pytest

from nebula.v3 import cpu_workers
from nebula.v3.cpu_workers import (
    CpuWorkerBusy,
    CpuWorkerClosed,
    CpuWorkerError,
    CpuWorkerLost,
    CpuWorkerPool,
)
from tests.v3.cpu_worker_fixtures import (
    exit_worker,
    fail,
    hold,
    identify,
    no_startup_ack,
    write_marker,
)


async def until(predicate, timeout=8):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize(
    "arguments",
    [
        {"workers": 0},
        {"workers": 5},
        {"queued": -1},
        {"queued": 33},
        {"max_input_bytes": 0},
        {"max_input_bytes": 3, "max_total_input_bytes": 2},
        {"startup_timeout": 0},
        {"shutdown_timeout": 0},
    ],
)
def test_invalid_pool_bounds_are_rejected(arguments):
    with pytest.raises(ValueError):
        CpuWorkerPool(**arguments)


async def test_jobs_run_in_owned_processes_and_callbacks_stay_in_core():
    async def scenario():
        pool = CpuWorkerPool(workers=2)
        callbacks = []
        try:
            results = await asyncio.gather(
                *(
                    pool.run(
                        identify,
                        i,
                        on_start=lambda: callbacks.append(
                            (os.getpid(), threading.get_ident())
                        ),
                    )
                    for i in range(8)
                )
            )
            state = pool.snapshot()
            pids = {worker["pid"] for worker in state["workers"]}
            assert len(pids) == 2 and os.getpid() not in pids
            assert {pid for pid, _ in results} == pids
            assert [value for _, value in results] == list(range(8))
            assert callbacks == [(os.getpid(), threading.get_ident())] * 8
            assert state["max_active"] == 2 and state["admitted"] == 0
        finally:
            await pool.shutdown()
        assert not any(worker["alive"] for worker in pool.snapshot()["workers"])

    (await scenario())


async def test_start_callback_preserves_the_request_context():
    actor = ContextVar("cpu_test_actor", default="startup")

    async def scenario():
        pool = CpuWorkerPool(workers=1)
        observed = []
        try:
            await pool.start()
            actor.set("requesting-operator")
            await pool.run(identify, on_start=lambda: observed.append(actor.get()))
            assert observed == ["requesting-operator"]
        finally:
            await pool.shutdown()

    (await scenario())


async def test_queue_bound_fifo_and_queued_cancellation(tmp_path):
    async def scenario():
        pool = CpuWorkerPool(workers=1, queued=2)
        release, started = tmp_path / "release", tmp_path / "started"
        order = []
        first = asyncio.create_task(
            pool.run(
                hold,
                str(release),
                str(started),
                input_bytes=10,
                on_start=lambda: order.append("first"),
            )
        )
        try:
            await until(started.exists)
            cancelled = asyncio.create_task(
                pool.run(
                    write_marker,
                    str(tmp_path / "cancelled"),
                    on_start=lambda: order.append("cancelled"),
                )
            )
            last = asyncio.create_task(
                pool.run(identify, "last", on_start=lambda: order.append("last"))
            )
            await until(lambda: pool.snapshot()["queued"] == 2)
            with pytest.raises(CpuWorkerBusy, match="queue is full"):
                await pool.run(identify)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert pool.snapshot()["queued"] == 1
            release.touch()
            await first
            assert (await last)[1] == "last"
            assert order == ["first", "last"]
            assert not (tmp_path / "cancelled").exists()
            state = pool.snapshot()
            assert state["max_admitted"] == 3 and state["cancelled_queued"] == 1
            assert state["input_bytes"] == 0 and state["admitted"] == 0
        finally:
            release.touch()
            await pool.shutdown()

    (await scenario())


async def test_running_cancellation_holds_capacity_until_actual_completion(tmp_path):
    async def scenario():
        pool = CpuWorkerPool(workers=1, queued=0)
        release, started = tmp_path / "release", tmp_path / "started"
        work = asyncio.create_task(pool.run(hold, str(release), str(started)))
        try:
            await until(started.exists)
            pid = int(started.read_text())
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            assert pool.snapshot()["active"] == 1 and pool.snapshot()["admitted"] == 1
            with pytest.raises(CpuWorkerBusy):
                await pool.run(identify)
            release.touch()
            await until(lambda: pool.snapshot()["admitted"] == 0)
            assert (await pool.run(identify))[0] == pid
            state = pool.snapshot()
            assert state["cancelled_running"] == 1 and state["discarded_results"] == 1
        finally:
            release.touch()
            await pool.shutdown()

    (await scenario())


async def test_snapshot_bytes_are_bounded_independently_of_job_count(tmp_path):
    async def scenario():
        pool = CpuWorkerPool(
            workers=1, queued=4, max_input_bytes=100, max_total_input_bytes=150
        )
        release, started = tmp_path / "release", tmp_path / "started"
        work = asyncio.create_task(
            pool.run(hold, str(release), str(started), input_bytes=100)
        )
        try:
            await until(started.exists)
            with pytest.raises(CpuWorkerBusy):
                await pool.run(identify, input_bytes=60)
            with pytest.raises(CpuWorkerBusy):
                await pool.run(identify, input_bytes=101)
            state = pool.snapshot()
            assert state["input_bytes"] == 100 and state["max_input_bytes"] == 100
            assert state["rejected"] == 2
        finally:
            release.touch()
            await work
            await pool.shutdown()

    (await scenario())


async def test_job_exception_and_unpicklable_input_do_not_poison_the_worker():
    async def scenario():
        pool = CpuWorkerPool(workers=1)
        try:
            pid, _ = await pool.run(identify)
            with pytest.raises(CpuWorkerError, match="ValueError") as caught:
                await pool.run(fail)
            assert "synthetic payload text" not in str(caught.value)
            with pytest.raises((AttributeError, TypeError)):
                await pool.run(lambda: None)
            assert (await pool.run(identify))[0] == pid
            assert pool.snapshot()["failed"] == 2 and pool.snapshot()["admitted"] == 0
        finally:
            await pool.shutdown()

    (await scenario())


async def test_start_callback_failure_does_not_execute_or_restart_a_worker(tmp_path):
    async def scenario():
        pool = CpuWorkerPool(workers=1)

        def rejected_start():
            raise RuntimeError("coordinator revision conflict")

        try:
            pid, _ = await pool.run(identify)
            with pytest.raises(RuntimeError, match="revision conflict"):
                await pool.run(
                    write_marker, str(tmp_path / "unexecuted"), on_start=rejected_start
                )
            assert not (tmp_path / "unexecuted").exists()
            assert (await pool.run(identify))[0] == pid
            assert pool.snapshot()["restarts"] == 0
        finally:
            await pool.shutdown()

    (await scenario())


async def test_worker_exit_fails_once_then_replaces_only_that_worker(tmp_path):
    async def scenario():
        pool = CpuWorkerPool(workers=1, queued=2)
        try:
            original, _ = await pool.run(identify)
            failed = asyncio.create_task(
                pool.run(exit_worker, str(tmp_path / "executed"))
            )
            next_job = asyncio.create_task(pool.run(identify, "after exit"))
            with pytest.raises(CpuWorkerLost):
                await failed
            replacement, value = await next_job
            assert replacement != original and value == "after exit"
            assert (tmp_path / "executed").read_text() == "one execution"
            state = pool.snapshot()
            assert state["worker_exits"] == 1 and state["restarts"] == 1
            assert state["failed"] == 1 and state["workers"][0]["generation"] == 2
        finally:
            await pool.shutdown()

    (await scenario())


async def test_shutdown_cancels_queue_and_stops_running_children(tmp_path):
    async def scenario():
        pool = CpuWorkerPool(workers=1, queued=1, shutdown_timeout=0.05)
        release, started = tmp_path / "release", tmp_path / "started"
        work = asyncio.create_task(pool.run(hold, str(release), str(started)))
        await until(started.exists)
        queued = asyncio.create_task(
            pool.run(write_marker, str(tmp_path / "unexecuted"))
        )
        await until(lambda: pool.snapshot()["queued"] == 1)
        async with asyncio.timeout(3):
            await pool.shutdown()
        with pytest.raises(CpuWorkerLost):
            await work
        with pytest.raises(asyncio.CancelledError):
            await queued
        state = pool.snapshot()
        assert state["closed"] and state["admitted"] == 0 and state["input_bytes"] == 0
        assert not any(worker["alive"] for worker in state["workers"])
        assert not (tmp_path / "unexecuted").exists()
        with pytest.raises(CpuWorkerClosed):
            await pool.run(identify)
        await pool.shutdown()

    (await scenario())


async def test_concurrent_teardown_waits_for_the_owned_pipe_close():
    async def scenario():
        closing = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()

        class StoppedProcess:
            def is_alive(self):
                return False

        class HeldConnection:
            closed = False

            def close(self):
                if self.closed:
                    return
                assert not closing.is_set(), "Two owners closed the same pipe together"
                closing.set()
                assert release.wait(5), "The test did not release the held close"
                self.closed = True

        connection = HeldConnection()
        worker = cpu_workers._Worker(StoppedProcess(), connection, 0)
        first = asyncio.create_task(
            asyncio.to_thread(cpu_workers._stop_worker, worker, force=True)
        )
        assert await asyncio.to_thread(closing.wait, 5)

        def second_stop():
            second_entered.set()
            cpu_workers._stop_worker(worker, force=True)

        second = asyncio.create_task(asyncio.to_thread(second_stop))
        try:
            assert await asyncio.to_thread(second_entered.wait, 5)
            done, _ = await asyncio.wait({second}, timeout=0.05)
            assert not done, "A competing teardown finished before the held close"
        finally:
            release.set()
            outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert outcomes == [None, None] and connection.closed

    (await scenario())


async def test_startup_timeout_cleans_up_owned_child(monkeypatch):
    monkeypatch.setattr(cpu_workers, "_worker_main", no_startup_ack)

    async def scenario():
        pool = CpuWorkerPool(workers=1, startup_timeout=0.1)
        with pytest.raises(TimeoutError):
            await pool.start()
        await pool.shutdown()
        assert pool.snapshot()["closed"] and not pool._workers

    (await scenario())


async def test_shutdown_during_startup_cannot_leave_a_new_child():
    async def scenario():
        pool = CpuWorkerPool(workers=2)
        starting = asyncio.create_task(pool.start())
        await until(lambda: pool._owner_loop is not None)
        await pool.shutdown()
        with pytest.raises(CpuWorkerClosed):
            await starting
        assert not any(worker["alive"] for worker in pool.snapshot()["workers"])

    (await scenario())


async def test_pool_refuses_a_different_event_loop():
    async def scenario():
        pool = CpuWorkerPool(workers=1)
        try:
            await pool.start()

            def other_loop():
                with pytest.raises(CpuWorkerError, match="different Core event loop"):
                    asyncio.run(pool.run(identify))

            await asyncio.to_thread(other_loop)
        finally:
            await pool.shutdown()

    (await scenario())


pytestmark = pytest.mark.anyio
