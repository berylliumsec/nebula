"""Thread/context seam for local or remote synchronous index implementations."""

import asyncio
import hashlib
import json

from .async_utils import finish_cleanup


def index_digest(chunks) -> str:
    return hashlib.sha256(
        json.dumps(
            chunks,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


async def index_call(callback, /, *args, **kwargs):
    # Context, including the exact project fence, crosses the thread boundary.
    # Accepted index work finishes before a cancelled owner can retire.
    return await finish_cleanup(asyncio.to_thread(callback, *args, **kwargs))
