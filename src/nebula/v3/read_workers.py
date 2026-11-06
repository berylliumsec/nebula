"""Bound expensive control-plane reads without occupying Core's request loop."""

from __future__ import annotations

import asyncio
import inspect
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from typing import Callable, ParamSpec, TypeVar

from .performance import current_performance
from .database import Database


_P = ParamSpec("_P")
_T = TypeVar("_T")
# Share admission across projects, scheduled cases and HTTP readers. A cancelled
# waiter cannot release a still-running read's slot, as an async semaphore would.
# Native async database reads use the database pool and its bulk admission.
# This executor bounds remaining CPU and filesystem work.
_READERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="nebula-read")


async def read_in_worker(
    function: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
) -> _T:
    """Await async reads directly; bound remaining synchronous CPU/file work.

    No database session may cross the worker boundary. Executor capacity remains
    held until synchronous work ends, including after caller cancellation.
    """

    if inspect.iscoroutinefunction(function):
        # Runtime storage is native async. Only remaining synchronous CPU/file
        # operations need the executor; never send an AsyncSession to it.
        owner = getattr(function, "__self__", None)
        database = getattr(owner, "database", None) or getattr(
            getattr(owner, "store", None), "database", None
        )
        if isinstance(database, Database):
            async with database.bulk():
                return await function(*args, **kwargs)
        return await function(*args, **kwargs)
    performance = current_performance()
    operation = partial(function, *args, **kwargs)
    if performance is not None:
        queued = time.perf_counter()
        module = getattr(function, "__module__", "")
        name = (
            f"{module}.{getattr(function, '__qualname__', 'read')}"
            if module.startswith("nebula.")
            else "external"
        )

        def measured_read() -> _T:
            performance.record(
                f"worker.queue {name}", (time.perf_counter() - queued) * 1000
            )
            with performance.span(f"worker.run {name}"):
                return operation()

        reader = measured_read
    else:
        reader = operation
    result = await asyncio.get_running_loop().run_in_executor(
        _READERS, copy_context().run, reader
    )
    return await result if inspect.isawaitable(result) else result
