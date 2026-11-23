"""Bounded admission of one tool call to the process and host tool pools.

Every execution tool holds a slot in this process's tool semaphore and, under
supervisor mode, a ``tool`` unit of the host-wide SQL capacity pool for as long
as it runs. Long commands can hold every slot, so the wait for one is bounded
(``NEBULA_TOOL_CAPACITY_WAIT_SECONDS``) and reported while it lasts: a call
that cannot be admitted in time is refused with ``CapacityReached`` before it
runs, which the model sees as an ordinary tool failure it may retry. The bound
covers only the wait for a slot, never the tool's own run time.

Control-plane built-ins that only read or write Core's own records in process
(``ToolSpec.uses_execution_capacity`` is false) never take a slot, so they
keep working while long tools fill the pools.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack

from .async_utils import finish_cleanup
from .diagnostics import record_caught_exception
from .tools import CapacityReached, refused_before_execution

TOOL_CAPACITY_WAITING = "Waiting for tool capacity"
HOST_TOOL_CAPACITY_WAITING = "Waiting for shared host tool capacity"

OnWait = Callable[[str], Awaitable[None]]
HostSlot = Callable[[OnWait], AbstractAsyncContextManager[object]]


class ToolCapacityAdmission:
    """One call's claim on the process slots and, optionally, a host slot."""

    def __init__(
        self,
        slots: asyncio.Semaphore,
        *,
        limit: int,
        wait_seconds: float,
        host_slot: HostSlot | None = None,
    ) -> None:
        self._slots = slots
        self._limit = limit
        self._wait_seconds = wait_seconds
        self._host_slot = host_slot
        self._stack: AsyncExitStack | None = None

    @property
    def held(self) -> bool:
        return self._stack is not None

    async def acquire(self, on_wait: OnWait | None = None) -> None:
        """Hold both slots, or raise ``CapacityReached`` at the deadline.

        ``on_wait`` is told once per pool that this call has to wait for.
        """

        async def notify(detail: str) -> None:
            if on_wait is not None:
                await on_wait(detail)

        async def host_wait(_detail: str) -> None:
            await notify(HOST_TOOL_CAPACITY_WAITING)

        stack = AsyncExitStack()
        deadline = asyncio.timeout(self._wait_seconds)
        try:
            async with deadline:
                if self._slots.locked():
                    await notify(TOOL_CAPACITY_WAITING)
                await self._slots.acquire()
                stack.callback(self._slots.release)
                if self._host_slot is not None:
                    await stack.enter_async_context(self._host_slot(host_wait))
        except TimeoutError as exc:
            await finish_cleanup(stack.aclose())
            if not deadline.expired():
                raise
            record_caught_exception(
                "chat",
                "chat.tool_capacity.wait_expired",
                "A tool call waited too long for tool capacity; the model was "
                "told to retry later.",
                exc,
                stage="tool-capacity",
                metadata={"wait_seconds": self._wait_seconds},
            )
            raise refused_before_execution(
                CapacityReached(
                    "every tool slot stayed in use for "
                    f"{self._wait_seconds:g} seconds; retry later, or continue "
                    "with tools already running",
                    resource="tool_slots",
                    maximum=self._limit,
                    current=self._limit,
                )
            ) from exc
        except BaseException:
            # diagnostic-expected: cancellation or a capacity-store failure propagates to the tool step after the partial claim is released.
            await finish_cleanup(stack.aclose())
            raise
        self._stack = stack

    async def waits(self) -> AsyncGenerator[str, None]:
        """Acquire, yielding each wait as it starts; return once both are held.

        Use under ``contextlib.aclosing`` so an abandoned stream stops the
        acquisition. The caller still owns ``release`` once this returns.
        """

        details: asyncio.Queue[str] = asyncio.Queue()

        async def queued(detail: str) -> None:
            details.put_nowait(detail)

        # diagnostic-expected: this owned acquisition is joined below, and its CapacityReached or failure is re-raised to the caller.
        task = asyncio.create_task(self.acquire(queued))
        try:
            while not task.done():
                getter = asyncio.ensure_future(details.get())
                try:
                    await asyncio.wait({task, getter}, return_when="FIRST_COMPLETED")
                finally:
                    if not getter.done():
                        getter.cancel()
                if getter.done() and not getter.cancelled():
                    yield getter.result()
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def release(self) -> None:
        stack, self._stack = self._stack, None
        if stack is not None:
            await finish_cleanup(stack.aclose())
