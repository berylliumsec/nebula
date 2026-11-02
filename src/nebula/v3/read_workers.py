"""Bound expensive control-plane reads without occupying Core's request loop."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from typing import Callable, ParamSpec, TypeVar


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

    return await asyncio.get_running_loop().run_in_executor(
        _READERS, copy_context().run, partial(function, *args, **kwargs)
    )
