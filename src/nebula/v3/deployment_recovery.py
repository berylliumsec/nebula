"""Bind deployment effects to their durable operation before child admission."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar

_CHECKPOINT: ContextVar[Callable[[dict], Awaitable[None]] | None] = ContextVar(
    "deployment_operation_checkpoint", default=None
)


async def checkpoint(**values) -> None:
    callback = _CHECKPOINT.get()
    if callback is not None:
        await callback(values)
