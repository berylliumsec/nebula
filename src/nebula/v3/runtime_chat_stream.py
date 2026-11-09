"""Execution writes chat frames before any replaceable viewer can see them."""

from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import uuid4

from .async_utils import finish_cleanup
from .domain import ChatTurn
from .database import EntityRow, RuntimeResourceRow
from .execution_scope import current_execution_lease
from .runtime_coordination import CoordinationConflict, LeaseLost, STREAM_EPOCH
from .runtime_chat_acceptance import bind_stream_cursor

SEGMENT_END = "runtime_stream_end"


class RuntimeChatJournal:
    def __init__(self, coordination, store):
        self.coordination, self.store = coordination, store

    async def publish(
        self, turn_id, events, *, batch_key=None, apply=None, command_id=None
    ):
        lease = current_execution_lease()
        if lease is None:
            raise LeaseLost("Chat publication requires exact project ownership")

        async def publication(session, frames):
            if command_id is not None:
                await bind_stream_cursor(
                    session,
                    command_id=command_id,
                    turn_id=turn_id,
                    after=frames[0].sequence - 1,
                )
            if apply is not None:
                await apply(session, frames)

        async def commit():
            return await self.coordination.append_stream_batch(
                lease,
                turn_id,
                batch_key=batch_key or uuid4().hex,
                events=events,
                apply=publication,
            )

        return await finish_cleanup(commit())

    async def accept_existing(self, turn_id, segment, *, command_id, after):
        lease = current_execution_lease()
        if lease is None:
            raise LeaseLost("Chat attachment requires exact project ownership")
        async with self.coordination.fenced(lease) as session:
            resource = await session.get(
                RuntimeResourceRow, self.resource_id(turn_id, segment)
            )
            if resource is None or (
                resource.project_id,
                resource.runtime_id,
                resource.fencing_token,
            ) != (lease.project_id, lease.runtime_id, lease.fencing_token):
                raise CoordinationConflict("The active stream belongs to another owner")
            await bind_stream_cursor(
                session, command_id=command_id, turn_id=turn_id, after=after
            )

    async def has_history(self, turn_id):
        return bool(await self.coordination.replay_stream(turn_id, limit=1))

    @staticmethod
    def resource_id(turn_id, segment):
        return (
            "provider-turn:"
            + hashlib.sha256((turn_id + "\0" + segment).encode()).hexdigest()
        )

    async def begin(self, turn_id, segment):
        lease = current_execution_lease()
        if lease is None:
            raise LeaseLost("Chat admission requires exact project ownership")
        async with self.coordination.fenced(lease) as session:
            turn = await session.get(EntityRow, turn_id)
            if (
                turn is None
                or turn.kind != "chat_turns"
                or turn.engagement_id != lease.project_id
            ):
                raise CoordinationConflict(
                    "The provider turn does not belong to this project"
                )
            key = self.resource_id(turn_id, segment)
            if await session.get(RuntimeResourceRow, key) is not None:
                raise CoordinationConflict(
                    "This provider segment already has an execution owner"
                )
            session.add(
                RuntimeResourceRow(
                    id=key,
                    project_id=lease.project_id,
                    runtime_id=lease.runtime_id,
                    fencing_token=lease.fencing_token,
                    kind="provider_turn",
                    state="active",
                    restoration="durable",
                    detail="An accepted provider turn is finishing on its current runtime",
                    checkpoint={"turn_id": turn_id, "segment": segment},
                    updated_at_ms=await self.coordination._clock_ms(session),
                )
            )

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
        lease = current_execution_lease()

        async def release(session, frames):
            resource = await session.get(
                RuntimeResourceRow, self.resource_id(turn_id, segment)
            )
            if resource is not None:
                if lease is None or (
                    resource.project_id,
                    resource.runtime_id,
                    resource.fencing_token,
                ) != (lease.project_id, lease.runtime_id, lease.fencing_token):
                    raise LeaseLost(
                        "The provider segment belongs to another execution owner"
                    )
                resource.state = "closed"
                resource.updated_at_ms = await self.coordination._clock_ms(session)

        await self.publish(
            turn_id, frames, batch_key="stream-end/" + segment, apply=release
        )

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
