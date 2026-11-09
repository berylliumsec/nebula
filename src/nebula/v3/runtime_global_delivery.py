"""Atomic SQL-only global commands and their ordinary durable HTTP receipts."""

from __future__ import annotations

import asyncio
import hashlib
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from .database import DeploymentHostRow, RuntimeCommandRow, RuntimeDeliveryRow
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .runtime_coordination import CommandReceipt, CoordinationConflict
from .runtime_delivery import MAX_FRAME_BYTES, _json
from .runtime_ipc import RuntimeIpcError

MAX_GLOBAL_RESPONSE = 8 * 1024 * 1024


class GlobalSqlDelivery:
    def __init__(self, delivery, application):
        self.delivery, self.application = delivery, application

    async def execute(self, intent, *, actor_id, idempotency_key):
        coordinator = self.delivery.coordination
        raw = _json(intent.model_dump(mode="json"))
        reference = await asyncio.to_thread(
            self.delivery.vault.put,
            raw,
            purpose=self.delivery._intent_purpose(actor_id),
        )
        payload = {"format": 1, "request_reference": reference}
        digest = hashlib.sha256(
            _json(
                {
                    "project_id": GLOBAL_EXECUTION_PROJECT,
                    "name": "global.sql/v1",
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
                            "SELECT pg_advisory_xact_lock(hashtext(current_schema()), "
                            "hashtext('nebula:global-sql:v1'))"
                        )
                    )
                existing = await session.scalar(
                    select(RuntimeCommandRow).where(
                        RuntimeCommandRow.actor_id == actor_id,
                        RuntimeCommandRow.idempotency_key == idempotency_key,
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
                            RuntimeCommandRow.project_id == GLOBAL_EXECUTION_PROJECT,
                        )
                    )
                    or 0
                ) + 1
                now = await coordinator._clock_ms(session)
                command = RuntimeCommandRow(
                    id=str(uuid4()),
                    project_id=GLOBAL_EXECUTION_PROJECT,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    sequence=sequence,
                    name="global.sql/v1",
                    lane="control",
                    payload=payload,
                    payload_hash=digest,
                    accepted_at_ms=now,
                    state="running",
                )
                session.add(command)
                await session.flush()
                # All eligible handlers perform SQL only through this exact
                # database. No RPC, subprocess, network or producer is allowed.
                savepoint = await session.begin_nested()
                try:
                    status, headers, body = await self._response(intent, actor_id)
                except BaseException:
                    await savepoint.rollback()
                    raise
                if status >= 400:
                    await savepoint.rollback()
                else:
                    await savepoint.commit()
                frames = [("head", _json({"status": status, "headers": headers}))]
                frames.extend(
                    ("body", body[start : start + MAX_FRAME_BYTES])
                    for start in range(0, len(body), MAX_FRAME_BYTES)
                )
                frames.append(("end", b""))
                for index, (kind, content) in enumerate(frames, start=1):
                    content_reference = await asyncio.to_thread(
                        self.delivery.vault.put,
                        content,
                        purpose=self.delivery._frame_purpose(command.id, index),
                    )
                    session.add(
                        RuntimeDeliveryRow(
                            id=str(uuid4()),
                            project_id=GLOBAL_EXECUTION_PROJECT,
                            command_id=command.id,
                            sequence=index,
                            kind=kind,
                            content_reference=content_reference,
                        )
                    )
                command.state = "succeeded" if status < 400 else "failed"
                command.result = {"http_status": status}
                return CommandReceipt(
                    command.id, command.project_id, command.sequence, now
                )
        except IntegrityError as error:
            # A competing project request may have won the same actor/key.
            # The entire SQL-only global transaction rolls back in that case.
            raise CoordinationConflict(
                "Idempotency-Key conflicts with another accepted request"
            ) from error

    async def _response(self, intent, actor):
        delivered = False
        status, headers, body = None, [], bytearray()

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": intent.body(), "more_body": False}

        async def send(message):
            nonlocal status, headers
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    (key.decode("latin-1"), value.decode("latin-1"))
                    for key, value in message.get("headers", [])
                ]
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))
                if len(body) > MAX_GLOBAL_RESPONSE:
                    raise RuntimeIpcError(
                        "Global SQL response exceeds its delivery bound"
                    )

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "http",
            "server": ("global-sql", 80),
            "client": ("127.0.0.1", 0),
            "root_path": "",
            "method": intent.method,
            "path": intent.path,
            "raw_path": intent.path.encode("ascii"),
            "query_string": intent.query.encode("ascii"),
            "headers": [
                (key.encode("latin-1"), value.encode("latin-1"))
                for key, value in intent.headers
            ],
            "state": {"global_actor": actor},
        }
        await self.application(scope, receive, send)
        if status is None:
            raise RuntimeIpcError("Global SQL handler did not produce a response")
        return status, headers, bytes(body)
