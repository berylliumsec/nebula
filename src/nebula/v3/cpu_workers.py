"""Experimental bounded CPU processes; Core alone owns application state.

Only importable stateless callables and plain inputs cross an owned pipe. No
service instance, SQLAlchemy Session, artifact writer or async callback is sent.
Each dispatcher owns one child and one exchange at a time. Cancelled running
jobs keep their capacity until the child finishes; their results are discarded.
"""

from __future__ import annotations

import asyncio
from _thread import LockType
from collections import deque
from contextvars import Context, copy_context
from dataclasses import dataclass, field
import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
import os
import pickle
from threading import Lock
import time
from typing import Any, Callable, TypeVar

from .diagnostics import create_diagnostic_task, record_caught_exception

_T = TypeVar("_T")
_MAX_REPLY_BYTES = 32 * 1024 * 1024


class CpuWorkerError(RuntimeError):
    pass


class CpuWorkerBusy(CpuWorkerError):
    pass


class CpuWorkerLost(CpuWorkerError):
    pass


class CpuWorkerClosed(CpuWorkerError):
    pass


def _worker_main(connection: Connection) -> None:
    """A spawn target that never starts a Core or opens its database."""

    try:
        connection.send(("ready", os.getpid()))
        while True:
            request = connection.recv()
            if request is None:
                return
            function, arguments = request
            started = time.process_time()
            try:
                value = function(*arguments)
                reply = ("ok", value, time.process_time() - started)
            except Exception as exc:
                # diagnostic-expected: only the exception type crosses the pipe;
                # the coordinator records the failure without raw input/tracebacks.
                reply = ("error", type(exc).__name__, time.process_time() - started)
            encoded = pickle.dumps(reply, protocol=pickle.HIGHEST_PROTOCOL)
            if len(encoded) > _MAX_REPLY_BYTES:
                encoded = pickle.dumps(("error", "ResultTooLarge", reply[2]))
            connection.send_bytes(encoded)
    except (EOFError, OSError):
        # diagnostic-expected: the owning Core closed its worker pipe or exited.
        return
    finally:
        connection.close()


def _exchange(connection: Connection, function: Callable, arguments: tuple) -> tuple:
    connection.send((function, arguments))
    return pickle.loads(connection.recv_bytes(maxlength=_MAX_REPLY_BYTES))


@dataclass(eq=False)
class _Job:
    function: Callable
    arguments: tuple
    result: asyncio.Future
    input_bytes: int
    context: Context
    on_start: Callable[[], None] | None = None
    started: bool = False


@dataclass
class _Worker:
    process: BaseProcess
    connection: Connection
    pid: int
    generation: int = 1
    running: bool = False
    cpu_seconds: float = 0.0
    completed: int = 0
    stop_lock: LockType = field(default_factory=Lock, repr=False, compare=False)


def _stop_worker(worker: _Worker, *, force: bool = False) -> None:
    # Shutdown can terminate a child while its dispatcher observes the exit.
    # Both owners must serialize process teardown and Connection.close(), whose
    # idempotent handle check is not safe across concurrent threads.
    with worker.stop_lock:
        process = worker.process
        if process.is_alive() and not force:
            try:
                worker.connection.send(None)
            except (EOFError, OSError):
                # diagnostic-expected: a worker may already have exited.
                pass
            process.join(timeout=0.5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.5)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.5)
        worker.connection.close()


class CpuWorkerPool:
    """A coordinator-owned FIFO with fixed processes and fail-fast admission.

    ``input_bytes`` is the caller's serialized snapshot size. Both admission
    count and total snapshot bytes are bounded before a job enters the queue.
    A crashed job fails once; only subsequent jobs use a replacement process.
    """

    def __init__(
        self,
        *,
        workers: int = 2,
        queued: int = 6,
        max_input_bytes: int = 8 * 1024 * 1024,
        max_total_input_bytes: int = 32 * 1024 * 1024,
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 5.0,
    ) -> None:
        if not 1 <= workers <= 4 or not 0 <= queued <= 32:
            raise ValueError("CPU workers must be 1–4 and queued jobs 0–32")
        if not 0 < max_input_bytes <= max_total_input_bytes:
            raise ValueError("CPU snapshot limits must be positive and ordered")
        if startup_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("CPU worker timeouts must be positive")
        self.worker_count = workers
        self.capacity = workers + queued
        self.max_input_bytes = max_input_bytes
        self.max_total_input_bytes = max_total_input_bytes
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self._context = multiprocessing.get_context("spawn")
        self._workers: list[_Worker] = []
        self._dispatchers: list[asyncio.Task] = []
        self._pending: deque[_Job] = deque()
        self._jobs: set[_Job] = set()
        self._input_bytes = 0
        self._wake = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._broken = False
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._stats = {
            "accepted": 0,
            "completed": 0,
            "failed": 0,
            "rejected": 0,
            "cancelled_queued": 0,
            "cancelled_running": 0,
            "discarded_results": 0,
            "worker_exits": 0,
            "restarts": 0,
            "max_active": 0,
            "max_admitted": 0,
            "max_input_bytes": 0,
        }

    async def _spawn(self, generation: int = 1) -> _Worker:
        parent, child = self._context.Pipe()
        process = self._context.Process(
            target=_worker_main, args=(child,), name="nebula-cpu", daemon=True
        )
        try:
            process.start()
        except BaseException:
            # diagnostic-expected: no child owns the parent endpoint on failure.
            parent.close()
            raise
        finally:
            child.close()
        worker = _Worker(process, parent, process.pid or 0, generation)
        try:
            ready = await asyncio.wait_for(
                asyncio.to_thread(parent.recv), timeout=self.startup_timeout
            )
            if ready != ("ready", worker.pid):
                raise CpuWorkerLost("CPU worker did not acknowledge startup")
            if self._closed:
                raise CpuWorkerClosed("CPU workers stopped during startup")
        except BaseException:
            # diagnostic-expected: the caller records startup/replacement failure;
            # stop the owned child before propagating it.
            await asyncio.to_thread(_stop_worker, worker, force=True)
            raise
        return worker

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._owner_loop is not None and loop is not self._owner_loop:
            raise CpuWorkerError("CPU pool belongs to a different Core event loop")
        async with self._start_lock:
            if self._closed or self._broken:
                raise CpuWorkerClosed("CPU workers are stopped")
            if self._started:
                return
            self._owner_loop = loop
            try:
                for _ in range(self.worker_count):
                    self._workers.append(await self._spawn())
            except BaseException:
                # diagnostic-expected: partial startup owns and removes every child.
                self._closed = True
                await asyncio.gather(
                    *(
                        asyncio.to_thread(_stop_worker, worker, force=True)
                        for worker in self._workers
                    )
                )
                raise
            self._started = True
            self._dispatchers = [
                create_diagnostic_task(
                    self._dispatch(index),
                    feature="reports",
                    event_code="reports.cpu_worker.dispatch_failed",
                    failure_message="A CPU worker dispatcher stopped unexpectedly.",
                    name=f"nebula-cpu-{index}",
                )
                for index in range(self.worker_count)
            ]

    async def run(
        self,
        function: Callable[..., _T],
        /,
        *arguments: Any,
        input_bytes: int = 0,
        on_start: Callable[[], None] | None = None,
    ) -> _T:
        await self.start()
        if input_bytes < 0 or input_bytes > self.max_input_bytes:
            self._stats["rejected"] += 1
            raise CpuWorkerBusy("CPU job exceeds the snapshot size limit")
        if len(self._jobs) >= self.capacity or (
            self._input_bytes + input_bytes > self.max_total_input_bytes
        ):
            self._stats["rejected"] += 1
            raise CpuWorkerBusy("CPU worker queue is full; retry the saved report")
        job = _Job(
            function,
            arguments,
            asyncio.get_running_loop().create_future(),
            input_bytes,
            copy_context(),
            on_start,
        )
        self._jobs.add(job)
        self._input_bytes += input_bytes
        self._pending.append(job)
        self._stats["accepted"] += 1
        self._stats["max_admitted"] = max(self._stats["max_admitted"], len(self._jobs))
        self._stats["max_input_bytes"] = max(
            self._stats["max_input_bytes"], self._input_bytes
        )
        self._wake.set()
        try:
            return await job.result
        except asyncio.CancelledError:
            # diagnostic-expected: cancellation never cancels the dispatcher or
            # frees a running slot before its actual exchange has stopped.
            if not job.started:
                if job in self._pending:
                    self._pending.remove(job)
                self._release(job)
                self._stats["cancelled_queued"] += 1
            else:
                self._stats["cancelled_running"] += 1
            raise

    def _release(self, job: _Job) -> None:
        if job in self._jobs:
            self._jobs.remove(job)
            self._input_bytes -= job.input_bytes

    async def _next_job(self) -> _Job | None:
        while not self._pending:
            if self._closed or self._broken:
                return None
            self._wake.clear()
            await self._wake.wait()
        return self._pending.popleft()

    async def _dispatch(self, index: int) -> None:
        while (job := await self._next_job()) is not None:
            worker = self._workers[index]
            job.started = True
            worker.running = True
            active = sum(item.running for item in self._workers)
            self._stats["max_active"] = max(self._stats["max_active"], active)
            lost = False
            try:
                if job.on_start is not None:
                    job.context.run(job.on_start)
                status, value, cpu = await asyncio.to_thread(
                    _exchange, worker.connection, job.function, job.arguments
                )
                worker.cpu_seconds += cpu
                worker.completed += 1
                if status == "error":
                    raise CpuWorkerError(
                        f"CPU job failed ({value}); retry the saved report"
                    )
                self._stats["completed"] += 1
                if not job.result.done():
                    job.result.set_result(value)
                else:
                    self._stats["discarded_results"] += 1
            except (EOFError, OSError) as exc:
                lost = True
                self._stats["worker_exits"] += 1
                self._stats["failed"] += 1
                record_caught_exception(
                    "reports",
                    "reports.cpu_worker.exited",
                    "A CPU worker exited; its job was not replayed.",
                    exc,
                    stage="cpu-worker",
                )
                if not job.result.done():
                    job.result.set_exception(
                        CpuWorkerLost("CPU worker exited; retry the saved report")
                    )
            except Exception as exc:
                self._stats["failed"] += 1
                record_caught_exception(
                    "reports",
                    "reports.cpu_worker.job_failed",
                    "A CPU job failed without changing Core state.",
                    exc,
                    stage="cpu-worker",
                )
                if not job.result.done():
                    job.result.set_exception(exc)
            finally:
                worker.running = False
                self._release(job)
            if lost:
                await asyncio.to_thread(_stop_worker, worker, force=True)
                if self._closed:
                    return
                try:
                    replacement = await self._spawn(worker.generation + 1)
                except Exception as exc:
                    if self._closed:
                        return
                    record_caught_exception(
                        "reports",
                        "reports.cpu_worker.restart_failed",
                        "A replacement CPU worker could not start.",
                        exc,
                        stage="cpu-worker",
                    )
                    self._broken = True
                    self._cancel_pending()
                    self._wake.set()
                    return
                replacement.cpu_seconds += worker.cpu_seconds
                replacement.completed += worker.completed
                self._workers[index] = replacement
                self._stats["restarts"] += 1

    def _cancel_pending(self) -> None:
        while self._pending:
            job = self._pending.popleft()
            job.result.cancel()
            self._release(job)

    async def shutdown(self) -> None:
        self._closed = True
        async with self._start_lock:
            pass
        self._cancel_pending()
        self._wake.set()
        if self._dispatchers:
            _, pending = await asyncio.wait(
                self._dispatchers, timeout=self.shutdown_timeout
            )
        else:
            pending = set()
        await asyncio.gather(
            *(
                asyncio.to_thread(_stop_worker, worker, force=bool(pending))
                for worker in self._workers
            )
        )
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def snapshot(self) -> dict[str, Any]:
        return {
            **self._stats,
            "coordinator_pid": os.getpid(),
            "active": sum(worker.running for worker in self._workers),
            "queued": len(self._pending),
            "admitted": len(self._jobs),
            "input_bytes": self._input_bytes,
            "capacity": self.capacity,
            "closed": self._closed,
            "broken": self._broken,
            "workers": [
                {
                    "pid": worker.pid,
                    "alive": worker.process.is_alive(),
                    "generation": worker.generation,
                    "running": worker.running,
                    "cpu_seconds": worker.cpu_seconds,
                    "completed": worker.completed,
                }
                for worker in self._workers
            ],
        }
