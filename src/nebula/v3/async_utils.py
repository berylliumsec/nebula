"""Explicit adapters at injectable async/synchronous callback boundaries."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

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
        if inspect.isawaitable(result):
            loop = asyncio.get_running_loop()
            tasks = _callback_tasks.setdefault(loop, set())
            task = create_diagnostic_task(
                result,
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
    while tasks:
        await asyncio.gather(*tuple(tasks))
    _callback_tasks.pop(loop, None)


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
