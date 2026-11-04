"""Bound expensive control-plane reads without occupying Core's request loop."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from typing import Callable, ParamSpec, TypeVar

from .performance import current_performance


_P = ParamSpec("_P")
_T = TypeVar("_T")
# Share admission across projects, scheduled cases and HTTP readers. A cancelled
# waiter cannot release a still-running read's slot, as an async semaphore would.
# Four readers leave connections for ordinary requests in the default 15-slot DB
# pool, even alongside the four admitted entity listings.
_READERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="nebula-read")


async def read_in_worker(
    function: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
) -> _T:
    """Run a synchronous read, including all Session ownership, in a worker.

    Handlers that own async services stay on their loop. Pass a complete read
    operation here, never a Session created by the caller. Executor capacity is
    held until the actual read ends, including after its caller is cancelled.
    """

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
    return await asyncio.get_running_loop().run_in_executor(
        _READERS, copy_context().run, reader
    )
