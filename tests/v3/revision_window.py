"""Run a real concurrent writer inside a target's read-then-write window.

Each helper wraps one store entry point. When ``target`` calls it, after the
target's reads and awaits but before its revision-guarded write, the writer
runs first. That is the interleaving an asyncio task switch, or another Core
worker, produces; the helpers make it deterministic for regression tests.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Any, Awaitable, Callable


def _callers(depth: int = 8) -> list[str]:
    names = []
    frame = sys._getframe(2)
    while frame is not None and len(names) < depth:
        names.append(frame.f_code.co_name)
        frame = frame.f_back
    return names


class Window:
    def __init__(self) -> None:
        self.fired: list[str] = []


def before_transaction(
    store: Any,
    target: str,
    writer: Callable[[], Awaitable[Any]],
    *,
    times: int = 1,
    direct: bool = True,
) -> Window:
    """Run ``writer`` before ``store.transaction()`` opened by ``target``."""

    state = Window()
    original = store.transaction

    def wrapped(*args: Any, **kwargs: Any):
        names = _callers()
        hit = (names[0] == target) if direct else (target in names)
        if hit and len(state.fired) < times:
            state.fired.append(target)

            @contextlib.asynccontextmanager
            async def cm():
                await writer()
                async with original(*args, **kwargs) as transaction:
                    yield transaction

            return cm()
        return original(*args, **kwargs)

    store.transaction = wrapped
    return state


def before_update(
    store: Any,
    target: str,
    writer: Callable[[], Awaitable[Any]],
    *,
    model: Any = None,
    times: int = 1,
    direct: bool = True,
    method: str = "update",
) -> Window:
    """Run ``writer`` before ``store.<method>`` called by ``target``."""

    state = Window()
    original = getattr(store, method)

    async def wrapped(*args: Any, **kwargs: Any):
        names = _callers()
        hit = (names[0] == target) if direct else (target in names)
        if model is not None and args and args[0] is not model:
            hit = False
        if hit and len(state.fired) < times:
            state.fired.append(target)
            await writer()
        return await original(*args, **kwargs)

    setattr(store, method, wrapped)
    return state
