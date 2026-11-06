"""Explicit adapters at injectable async/synchronous callback boundaries."""

from __future__ import annotations

from builtins import BaseExceptionGroup
import inspect
import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")
MaybeAsync = Callable[_P, _T | Awaitable[_T]]

WorkspaceResolver = Callable[[str], Path | Awaitable[Path]]


async def resolve_workspace(resolver: WorkspaceResolver, project_id: str) -> Path:
    result = resolver(project_id)
    return await result if inspect.isawaitable(result) else result


async def call_async(
    callback: MaybeAsync[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
) -> _T:
    """Await persistence callbacks, while retaining pure synchronous injections."""
    result = callback(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


_callback_tasks: dict[object, set] = {}


def async_done_callback(callback):
    """Bridge asyncio's synchronous done hook to supervised persistence work."""
    import asyncio
    from .diagnostics import create_diagnostic_task

    def completed(future):
        result = callback(future)
        # Registry-pop callbacks return the task they just removed. That is
        # bookkeeping, not additional persistence work (including cancellation).
        if result is future:
            return
        if inspect.isawaitable(result):
            loop = asyncio.get_running_loop()
            tasks = _callback_tasks.setdefault(loop, set())

            async def join_result():
                return await result

            task = create_diagnostic_task(
                result if asyncio.iscoroutine(result) else join_result(),
                feature="runtime",
                event_code="runtime.persistence_callback",
                failure_message="A completed task's persistence callback failed.",
                name="nebula-persistence-callback",
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    return completed


async def drain_callback_tasks():
    """Finish callbacks before disposing the loop's database engine."""
    import asyncio

    loop = asyncio.get_running_loop()
    tasks = _callback_tasks.get(loop, set())
    failures = []
    while tasks:
        batch = tuple(tasks)
        results = await asyncio.gather(*batch, return_exceptions=True)
        # gather may complete inline for finished tasks, before their scheduled
        # discard callbacks run. Remove only the batch we actually joined.
        tasks.difference_update(batch)
        failures.extend(value for value in results if isinstance(value, BaseException))
    _callback_tasks.pop(loop, None)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("Persistence callback cleanup failed", failures)


async def finish_cleanup(operation: Coroutine[Any, Any, _T]) -> _T:
    """Join owned cleanup even when its caller is cancelled repeatedly."""
    task = asyncio.create_task(
        operation
    )  # diagnostic-expected: this owned task is shielded and joined below; its result or exception is always consumed.
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            # diagnostic-expected: remember caller cancellation until owned cleanup has finished; child cancellation is consumed by result below.
            if not task.cancelled():
                cancellation = exc
        except BaseException:
            # diagnostic-expected: propagate the joined cleanup failure via result below, retaining caller cancellation if both failed.
            break
    try:
        result = task.result()
    except BaseException as exc:
        if cancellation is not None:
            raise BaseExceptionGroup(
                "Cancellation and cleanup failure", [cancellation, exc]
            ) from None
        raise
    if cancellation is not None:
        raise cancellation
    return result


async def close_store_resources(stores) -> None:
    """Drain every store before disposal, preserving failures after cleanup."""

    async def close_all():
        async with AsyncExitStack() as stack:
            databases = {id(store.database): store.database for store in stores}
            for database in databases.values():
                stack.push_async_callback(database.dispose)
            for store in stores:
                stack.push_async_callback(store.event_history.close)
            stack.push_async_callback(drain_callback_tasks)

    await finish_cleanup(close_all())


async def async_first(iterator, default=None):
    if hasattr(iterator, "__aiter__"):
        return await anext(iterator.__aiter__(), default)
    return next(iter(iterator), default)


async def async_any(iterator):
    async for value in iterator:
        if value:
            return True
    return False


async def async_all(iterator):
    async for value in iterator:
        if not value:
            return False
    return True


async def async_collect(iterator):
    return [value async for value in iterator]


async def async_take(iterator, count: int):
    result: list[Any] = []
    try:
        async for value in iterator:
            if len(result) == count:
                break
            result.append(value)
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()
    return result


async def async_map(callback, *iterables):
    return [await call_async(callback, *values) for values in zip(*iterables)]
