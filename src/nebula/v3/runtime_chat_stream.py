"""Execution writes chat frames before any replaceable viewer can see them."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from .async_utils import finish_cleanup
from .domain import ChatTurn
from .execution_scope import current_execution_lease
from .runtime_coordination import CoordinationConflict, LeaseLost, STREAM_EPOCH

SEGMENT_END = "runtime_stream_end"


class RuntimeChatJournal:
    def __init__(self, coordination, store):
        self.coordination, self.store = coordination, store

    async def publish(self, turn_id, events, *, batch_key=None):
        lease = current_execution_lease()
        if lease is None:
            raise LeaseLost("Chat publication requires exact project ownership")

        async def commit():
            return await self.coordination.append_stream_batch(
                lease, turn_id, batch_key=batch_key or uuid4().hex, events=events
            )

        return await finish_cleanup(commit())

    async def has_history(self, turn_id):
        return bool(await self.coordination.replay_stream(turn_id, limit=1))

    async def close(self, turn_id, segment, *, failed=False):
        frames = []
        if failed:
            frames.append(
                (
                    "error",
                    {
                        "type": "error",
                        "turn_id": turn_id,
                        "code": "chat_stream_failed",
                        "detail": "The response ended before completion. Review its saved state before retrying.",
                    },
                )
            )
        frames.append(
            (SEGMENT_END, {"type": SEGMENT_END, "turn_id": turn_id, "segment": segment})
        )
        await self.publish(turn_id, frames, batch_key="stream-end/" + segment)

    async def validate_cursor(self, turn_id, *, after_sequence=0, epoch=None):
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("Chat stream cursor must be a nonnegative integer")
        cursor = after_sequence if epoch in (None, STREAM_EPOCH) else 0
        await self.store.get(ChatTurn, turn_id)
        head = await self.coordination.stream_head(turn_id)
        if head is not None and cursor > head.sequence:
            raise CoordinationConflict(
                "The saved chat cursor is ahead of its journal; reload the conversation"
            )
        return head

    async def follow(self, turn_id, *, after_sequence=0, epoch=None, poll_interval=0.1):
        if not 0.01 <= poll_interval <= 2:
            raise ValueError("Chat polling must remain bounded")
        cursor = after_sequence if epoch in (None, STREAM_EPOCH) else 0
        # Resolve the durable turn before exposing any journal content.
        head = await self.validate_cursor(
            turn_id, after_sequence=after_sequence, epoch=epoch
        )
        if (
            head is not None
            and head.sequence == cursor
            and head.event_type == SEGMENT_END
        ):
            return
        while True:
            frames = await self.coordination.replay_stream(
                turn_id, after_sequence=cursor
            )
            for index, frame in enumerate(frames):
                cursor = frame.sequence
                if frame.event_type == SEGMENT_END:
                    # A resumed turn can have several committed segments. An
                    # old end marker must not hide a newer segment on reconnect.
                    if index + 1 == len(
                        frames
                    ) and not await self.coordination.replay_stream(
                        turn_id, after_sequence=cursor, limit=1
                    ):
                        return
                    continue
                yield (
                    frame.event_type,
                    {
                        **frame.payload,
                        "sequence": frame.sequence,
                        "epoch": STREAM_EPOCH,
                    },
                )
            if frames:
                continue
            # A terminal turn row can commit just before its final frame. Only
            # the committed segment end proves publication has finished.
            await asyncio.sleep(poll_interval)

    async def sse(self, turn_id, *, after_sequence=0, epoch=None):
        iterator = aiter(
            self.follow(turn_id, after_sequence=after_sequence, epoch=epoch)
        )
        pending = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                done, _ = await asyncio.wait({pending}, timeout=15)
                if not done:
                    yield b": keep-alive\n\n"
                    continue
                try:
                    kind, payload = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {payload['sequence']}\nevent: {kind}\ndata: {encoded}\n\n".encode()
        finally:

            async def cleanup():
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await iterator.aclose()

            await finish_cleanup(cleanup())
