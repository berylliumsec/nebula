"""Operator pause and stop while a project's restoration or activation is held.

A recovering project has no running application: its controllers, schedulers
and producers start only after recovery is verified, and recovery waits for
unresolved effects to be reconciled. The operator's pause and stop must not
wait behind that. This small application applies them as fenced SQL state
changes only. It starts no work, contacts no provider or harness, replays no
interrupted effect and never clears an execution claim: a stop does not prove
that an earlier external effect did not happen, so the claim (and the recovery
blocker it creates) stays for explicit reconciliation.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .database import Database, ProviderTurnQueueRow
from .database_pool import PoolSettings
from .async_utils import finish_cleanup
from .domain import ChatTurn, ChatTurnStatus, utc_now
from .runtime_coordination import ProjectLease
from .runtime_delivery import DurableHttpRunner, RuntimeDelivery
from .runtime_vault import RuntimeVault
from .storage import ConflictError, NebulaStore, NotFoundError

RECOVERY_HELD_DETAIL = (
    "Project recovery is held for unresolved effects; "
    "only pause and stop run until it is reconciled"
)
_ENDED = {ChatTurnStatus.COMPLETE, ChatTurnStatus.CANCELLED, ChatTurnStatus.FAILED}


def recovery_control_app(store: NebulaStore, *, operator_token: str) -> FastAPI:
    from .chat_goals import ChatGoalService, GoalWrite

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    goals = ChatGoalService(store)

    def authorized(request: Request) -> bool:
        presented = request.headers.get("authorization", "")
        return hmac.compare_digest(presented, "Bearer " + operator_token)

    def refusal(status: int, detail: str) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=status)

    @app.post("/api/v1/chat/sessions/{session_id}/goal/actions")
    async def pause_goal(session_id: str, request: Request):
        if not authorized(request):
            return refusal(401, "Authentication required")
        try:
            body = GoalWrite.model_validate_json(await request.body())
        except ValidationError as error:
            return JSONResponse({"detail": error.errors()}, status_code=422)
        if body.action != "pause":
            return refusal(409, RECOVERY_HELD_DETAIL)
        try:
            goal = await goals.write(session_id, body, retain_execution_claim=True)
        except NotFoundError as error:
            return refusal(404, str(error))
        except ConflictError as error:
            return refusal(409, str(error))
        return JSONResponse(goal.model_dump(mode="json"))

    @app.post("/api/v1/chat/turns/{turn_id}/cancel")
    async def stop_turn(turn_id: str, request: Request):
        if not authorized(request):
            return refusal(401, "Authentication required")
        try:
            turn = await store.get(ChatTurn, turn_id)
        except NotFoundError as error:
            return refusal(404, str(error))
        if turn.status in _ENDED:
            # A repeated stop changes nothing.
            return JSONResponse(_turn_view(turn, applied=False))
        if (
            turn.status != ChatTurnStatus.QUEUED
            or turn.execution_claim_id is not None
            or turn.harness_turn_id
        ):
            # It reached an owner that is now gone. Nothing runs it here, and
            # stopping cannot show what it already did.
            return refusal(
                409,
                "This response started under the interrupted owner and its effect "
                "is unresolved; reconcile project recovery, then stop or review it",
            )
        try:
            async with store.transaction() as transaction:
                admission = await transaction.session.get(
                    ProviderTurnQueueRow, turn.id, with_for_update=True
                )
                if admission is not None and admission.state not in {
                    "queued",
                    "cancelled",
                }:
                    raise ConflictError(
                        "The response's provider admission is not queued; "
                        "reconcile project recovery before stopping it"
                    )
                if admission is not None and admission.state == "queued":
                    admission.state = "cancelled"
                    admission.completed_at = utc_now()
                cancelled = await transaction.update(
                    ChatTurn,
                    turn.id,
                    {"status": ChatTurnStatus.CANCELLED, "error": "response stopped"},
                    expected_revision=turn.revision,
                )
        except ConflictError as error:
            return refusal(409, str(error))
        return JSONResponse(_turn_view(cancelled, applied=True))

    @app.api_route(
        "/api/v1/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"]
    )
    async def held(path: str):
        return refusal(409, RECOVERY_HELD_DETAIL)

    return app


def _turn_view(turn: ChatTurn, *, applied: bool) -> dict:
    return {
        "id": turn.id,
        "session_id": turn.session_id,
        "status": turn.status.value,
        "revision": turn.revision,
        "applied_during_recovery": applied,
    }


@asynccontextmanager
async def recovery_control_runner(context, lease: ProjectLease) -> AsyncIterator:
    """A fenced runner for recovery-safe controls, with its own store and pool."""
    bootstrap = context.bootstrap
    configured = getattr(bootstrap, "pool_settings", None)
    settings = PoolSettings(**configured) if configured else None
    database = Database(
        bootstrap.database,
        bootstrap=False,
        pool_settings=settings,
        execution_runtime_id=lease.runtime_id,
    )
    renewal = getattr(context, "renewal", None)
    if renewal is not None:
        database.ride_out_outages(renewal.remaining)
    store = NebulaStore(database)
    try:
        token = bootstrap.operator_token.get_secret_value()
        delivery = RuntimeDelivery(
            context.coordinator.coordination,
            RuntimeVault(Path(bootstrap.data_root) / "runtime" / "payloads"),
        )
        yield DurableHttpRunner(
            delivery,
            recovery_control_app(store, operator_token=token),
            operator_token=token,
        )
    finally:
        await finish_cleanup(store.event_history.close())
        await finish_cleanup(database.dispose())

