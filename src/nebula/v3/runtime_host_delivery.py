"""Durable host effects: accept under SQL lock, execute without a SQL transaction."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.responses import JSONResponse, Response

from .async_utils import finish_cleanup
from .database import DeploymentHostRow, RuntimeCommandRow, RuntimeDeliveryRow
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .runtime_coordination import CommandReceipt, CoordinationConflict
from .runtime_delivery import MAX_FRAME_BYTES, _json
from .runtime_global_delivery import GlobalSqlDelivery

HOST_EFFECT = "global.host/v1"


class HostEffectDelivery(GlobalSqlDelivery):
    """Reuse bounded HTTP capture, with a separately committed external effect."""

    def __init__(self, delivery, application):
        super().__init__(delivery, application)
        self.owner_id = "host-settings:" + str(uuid4())
        self.tasks: dict[str, asyncio.Task] = {}
        self.slots = asyncio.Semaphore(8)
        self.admission = asyncio.Lock()
        self.closed = False

    async def start(self):
        async with self.delivery.coordination._write() as session:
            rows = await session.scalars(
                select(RuntimeCommandRow)
                .where(
                    RuntimeCommandRow.name == HOST_EFFECT,
                    RuntimeCommandRow.state == "running",
                    RuntimeCommandRow.runtime_id != self.owner_id,
                )
                .with_for_update()
            )
            for row in rows:
                row.state = "uncertain"
                row.result = {
                    "detail": "The previous host did not confirm this effect; it has not been retried."
                }

    async def execute(self, intent, *, actor_id, idempotency_key):
        async def accept():
            async with self.admission:
                if self.closed:
                    raise CoordinationConflict("Host Settings is stopping")
                return await self._accept(intent, actor_id, idempotency_key)

        return await finish_cleanup(accept())

    async def _accept(self, intent, actor, key):
        coordinator = self.delivery.coordination
        raw = _json(intent.model_dump(mode="json"))
        reference = await asyncio.to_thread(
            self.delivery.vault.put, raw, purpose=self.delivery._intent_purpose(actor)
        )
        payload = {"format": 1, "request_reference": reference}
        digest = hashlib.sha256(
            _json(
                {
                    "project_id": GLOBAL_EXECUTION_PROJECT,
                    "name": HOST_EFFECT,
                    "payload": payload,
                    "expected_revision": None,
                }
            )
        ).hexdigest()
        try:
            async with coordinator._write() as session:
                if coordinator.database.engine.dialect.name == "postgresql":
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('nebula:global-sql:v1'))"
                        )
                    )
                existing = await session.scalar(
                    select(RuntimeCommandRow).where(
                        RuntimeCommandRow.actor_id == actor,
                        RuntimeCommandRow.idempotency_key == key,
                    )
                )
                if existing is not None:
                    if existing.payload_hash != digest:
                        raise CoordinationConflict(
                            "Idempotency-Key was used for different request details"
                        )
                    return CommandReceipt(
                        existing.id,
                        existing.project_id,
                        existing.sequence,
                        existing.accepted_at_ms,
                    )
                if len(self.tasks) >= 32:
                    raise CoordinationConflict(
                        "Host Settings capacity is busy; retry before submitting new work"
                    )
                host = await session.get(
                    DeploymentHostRow, "host", with_for_update=True
                )
                if host is not None and host.shutdown_requested:
                    raise CoordinationConflict(
                        "Core shutdown is in progress; new work is not accepted"
                    )
                sequence = (
                    await session.scalar(
                        select(func.max(RuntimeCommandRow.sequence)).where(
                            RuntimeCommandRow.project_id == GLOBAL_EXECUTION_PROJECT
                        )
                    )
                    or 0
                ) + 1
                now = await coordinator._clock_ms(session)
                command = RuntimeCommandRow(
                    id=str(uuid4()),
                    project_id=GLOBAL_EXECUTION_PROJECT,
                    actor_id=actor,
                    idempotency_key=key,
                    sequence=sequence,
                    name=HOST_EFFECT,
                    lane="control",
                    payload=payload,
                    payload_hash=digest,
                    accepted_at_ms=now,
                    state="running",
                    runtime_id=self.owner_id,
                )
                session.add(command)
                await session.flush()
                receipt = CommandReceipt(command.id, command.project_id, sequence, now)
        except IntegrityError as error:
            raise CoordinationConflict(
                "Idempotency-Key conflicts with another accepted request"
            ) from error
        task = asyncio.create_task(
            self._run(receipt.command_id, intent, actor), name="nebula-host-setting"
        )
        self.tasks[receipt.command_id] = task

        def completed(value):
            self.tasks.pop(receipt.command_id, None)
            if not value.cancelled():
                value.exception()

        task.add_done_callback(completed)
        return receipt

    async def _run(self, command_id, intent, actor):
        try:
            async with self.slots:
                async with asyncio.timeout(150):
                    status, headers, body = await self._response(intent, actor)
            frames = [("head", _json({"status": status, "headers": headers}))]
            frames.extend(
                ("body", body[start : start + MAX_FRAME_BYTES])
                for start in range(0, len(body), MAX_FRAME_BYTES)
            )
            frames.append(("end", b""))
            saved = []
            for index, (kind, content) in enumerate(frames, 1):
                reference = await asyncio.to_thread(
                    self.delivery.vault.put,
                    content,
                    purpose=self.delivery._frame_purpose(command_id, index),
                )
                saved.append((index, kind, reference))
            async with self.delivery.coordination._write() as session:
                row = await session.get(
                    RuntimeCommandRow, command_id, with_for_update=True
                )
                if (
                    row is None
                    or row.state != "running"
                    or row.runtime_id != self.owner_id
                ):
                    raise CoordinationConflict(
                        "Host Settings ownership changed before confirmation"
                    )
                for index, kind, reference in saved:
                    session.add(
                        RuntimeDeliveryRow(
                            id=str(uuid4()),
                            project_id=GLOBAL_EXECUTION_PROJECT,
                            command_id=command_id,
                            sequence=index,
                            kind=kind,
                            content_reference=reference,
                        )
                    )
                row.state = "succeeded" if status < 400 else "failed"
                row.result = {"http_status": status}
        except BaseException:

            async def uncertain():
                async with self.delivery.coordination._write() as session:
                    row = await session.get(
                        RuntimeCommandRow, command_id, with_for_update=True
                    )
                    if (
                        row is not None
                        and row.state == "running"
                        and row.runtime_id == self.owner_id
                    ):
                        row.state = "uncertain"
                        row.result = {
                            "detail": "Host Settings did not confirm this effect; it has not been retried."
                        }

            await finish_cleanup(uncertain())

    async def response(
        self, actor, command_id, *, receipt=None, after=0, wait_seconds=15
    ):
        task = self.tasks.get(command_id)
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=wait_seconds)
        page = await self.delivery.page(actor, command_id, limit=128)
        if page.state in {"running", "queued"}:
            return JSONResponse(
                {"state": "accepted", "receipt": asdict(receipt)}
                if receipt
                else {"state": page.state, "command_id": command_id},
                status_code=202,
                headers={"X-Nebula-Command-Id": command_id, "Retry-After": "1"},
            )
        if page.state == "uncertain" or not page.frames:
            return JSONResponse(
                {
                    "state": page.state,
                    "command_id": command_id,
                    "detail": "The saved host request needs reconciliation. It has not been retried.",
                },
                status_code=409,
                headers={"X-Nebula-Command-Id": command_id},
            )
        head = json.loads(page.frames[0].content)
        response = Response(
            b"".join(
                frame.content
                for frame in page.frames
                if frame.kind == "body" and frame.sequence > max(after, 1)
            ),
            status_code=head["status"],
        )
        response.raw_headers = [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in head["headers"]
            if name.lower() not in {"content-length", "transfer-encoding", "connection"}
            and not name.lower().startswith("x-nebula-internal-")
        ] + [(b"x-nebula-command-id", command_id.encode())]
        return response

    async def close(self):
        async with self.admission:
            self.closed = True
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
