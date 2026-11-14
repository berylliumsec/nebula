"""Durable host operation receipts; an unknown effect is never replayed."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import json
from uuid import uuid4

from .database import DeploymentOperationRow
from .deployments import DeploymentCoordinator, _checkpoint
from .runtime_coordination import CoordinationConflict, OperationRejected
from .runtime_ipc import RuntimeIpcError


def _document(value: dict) -> dict:
    value = _checkpoint(value)
    if len(json.dumps(value, sort_keys=True).encode()) > 64 * 1024:
        raise ValueError("deployment operation document exceeds its bound")
    return value


class DeploymentOperations:
    def __init__(
        self, coordinator: DeploymentCoordinator, *, owner_id: str | None = None
    ):
        self.coordinator = coordinator
        self.owner_id = owner_id or str(uuid4())

    @staticmethod
    def _receipt(row):
        return {
            "operation_id": row.operation_id,
            "action": row.action,
            "state": row.state,
            "result": row.result,
        }

    async def execute(
        self,
        *,
        operation_id: str,
        actor_id: str,
        action: str,
        parameters: dict,
        dispatch: Callable[[str, dict], Awaitable[dict]],
    ) -> dict:
        for value, name, maximum in (
            (operation_id, "operation_id", 200),
            (actor_id, "actor_id", 200),
            (action, "action", 80),
        ):
            if not value or len(value) > maximum:
                raise ValueError(name + " is missing or exceeds its bound")
        parameters = _document(parameters)
        digest = hashlib.sha256(
            json.dumps(
                [action, parameters], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            row = await session.get(DeploymentOperationRow, operation_id)
            now = await self.coordinator.coordination._clock_ms(session)
            if row is not None:
                if row.actor_id != actor_id or row.request_hash != digest:
                    raise CoordinationConflict(
                        "operation ID already identifies another request"
                    )
                if row.state == "running" and row.claim_owner != self.owner_id:
                    row.state, row.updated_at_ms = "uncertain", now
                    row.result = {
                        "detail": "The previous host did not confirm this operation. Read deployment status before reconciling it."
                    }
                return self._receipt(row)
            row = DeploymentOperationRow(
                operation_id=operation_id,
                actor_id=actor_id,
                action=action,
                request_hash=digest,
                parameters=parameters,
                state="running",
                claim_owner=self.owner_id,
                result=None,
                created_at_ms=now,
                updated_at_ms=now,
            )
            session.add(row)
        try:
            result = _document(await dispatch(action, parameters))
        except BaseException as error:
            error_type = type(error).__name__
            rejected = isinstance(error, OperationRejected)
            detail = (
                str(error)[:4000]
                if rejected
                else "The operation did not report a confirmed result. Read deployment status before reconciliation."
            )
            # Refusals carry operator-facing text; keep it beside the uncertain
            # outcome so the operator learns why. Other errors may embed
            # private values and stay classified by type only.
            reason = (
                (str(error) or error_type)[:4000]
                if not rejected
                and isinstance(error, (RuntimeIpcError, CoordinationConflict))
                else None
            )

            async def uncertain():
                await self._finish(
                    operation_id,
                    "failed" if rejected else "uncertain",
                    {
                        "detail": detail,
                        "error_type": error_type,
                        **({"reason": reason} if reason else {}),
                    },
                )

            from .async_utils import finish_cleanup

            await finish_cleanup(uncertain())
            if isinstance(
                error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
            return await self.status(operation_id, actor_id=actor_id)
        await self._finish(operation_id, "succeeded", result)
        return await self.status(operation_id, actor_id=actor_id)

    async def _finish(self, operation_id, state, result):
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            row = await session.get(DeploymentOperationRow, operation_id)
            if (
                row is None
                or row.claim_owner != self.owner_id
                or row.state != "running"
            ):
                raise CoordinationConflict(
                    "operation ownership changed before completion"
                )
            row.state, row.result = state, result
            row.updated_at_ms = await self.coordinator.coordination._clock_ms(session)

    async def status(self, operation_id: str, *, actor_id: str) -> dict:
        async with self.coordinator.database.session() as session:
            row = await session.get(DeploymentOperationRow, operation_id)
            if row is None or row.actor_id != actor_id:
                raise CoordinationConflict("deployment operation is unavailable")
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            row = await session.get(
                DeploymentOperationRow, operation_id, with_for_update=True
            )
            if row is None or row.actor_id != actor_id:
                raise CoordinationConflict("deployment operation is unavailable")
            if row.state == "running" and row.claim_owner != self.owner_id:
                row.state = "uncertain"
                row.updated_at_ms = await self.coordinator.coordination._clock_ms(
                    session
                )
                row.result = {
                    "detail": "The previous host did not confirm this operation. Read deployment status before reconciling it."
                }
            return self._receipt(row)
