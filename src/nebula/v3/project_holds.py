"""Core's durable hold on all chat work in one project.

A deployment that stops Core, migrates non-additively or retires a pinned
worker needs a project with no work running. Asking agents to wind down is
not enough: subagent reports, goal continuations and Butler's own checks keep
starting new turns. A hold is enforced by Core itself:

* Placing it pauses every running goal with the hold's mark. Every producer
  of chat work refuses or defers in a held project (``project_hold_gate``),
  and no one but this hold's release can resume the goals it paused.
* Running turns may finish for ``grace_seconds``. After that each tick stops
  what is still active, through the same stop an operator uses, and the hold
  becomes ``held`` once no turn is active.
* Release resumes exactly the goals carrying its mark, then wakes the
  subagent deliveries and queued follow-ups it deferred.

The record keeps what the hold paused, stopped and restored, so a deployment
record reads it from Core instead of from an agent's summary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .chat_goals import ChatGoalService
from .diagnostics import record_caught_exception
from .domain import (
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProjectHold,
    ProjectHoldGoal,
    ProjectHoldRestore,
    ProjectHoldTurn,
    utc_now,
)
from .project_hold_gate import HOLD_MARKER, active_hold
from .storage import ConflictError, NotFoundError, revise_latest

ACTIVE_TURN_STATUSES = [
    ChatTurnStatus.QUEUED.value,
    ChatTurnStatus.ROUTING.value,
    ChatTurnStatus.WAITING_APPROVAL.value,
    ChatTurnStatus.WAITING_CALLBACK.value,
    ChatTurnStatus.FINALIZING.value,
]
STOP_REASON = "Stopped by a project hold"


class HoldPlace(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    deployment_id: str | None = Field(default=None, max_length=200)
    grace_seconds: int = Field(default=180, ge=0, le=3600)


class HoldRelease(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ProjectHoldService:
    def __init__(
        self,
        store: Any,
        *,
        stop_provider_turn: Callable[[str], Awaitable[Any]],
        stop_harness_turn: Callable[[str, str], Awaitable[Any]],
        dispatch_goal: Callable[[str, str], Awaitable[Any]] | None = None,
        wake_subagents: Callable[[str], Awaitable[None]] | None = None,
        wake_queues: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.goals = ChatGoalService(store)
        self.stop_provider_turn = stop_provider_turn
        self.stop_harness_turn = stop_harness_turn
        self.dispatch_goal = dispatch_goal
        self.wake_subagents = wake_subagents
        self.wake_queues = wake_queues
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, project_id: str) -> asyncio.Lock:
        return self._locks.setdefault(project_id, asyncio.Lock())

    async def _active_turns(self, project_id: str) -> list[ChatTurn]:
        return await self.store.find_entities(
            ChatTurn, {"status": ACTIVE_TURN_STATUSES}, engagement_id=project_id
        )

    async def _running_goals(self, project_id: str) -> list[ChatGoal]:
        return await self.store.find_entities(
            ChatGoal,
            {"status": ChatGoalStatus.RUNNING.value},
            engagement_id=project_id,
        )

    async def view(self, project_id: str) -> dict[str, Any]:
        hold = await self.tick(project_id)
        latest = hold
        if latest is None:
            recent = await self.store.find_entities(
                ProjectHold, {}, engagement_id=project_id, newest_first=True, limit=1
            )
            latest = recent[0] if recent else None
        return {
            "project_id": project_id,
            "held": hold is not None,
            "hold": latest.model_dump(mode="json") if latest is not None else None,
            "active_turns": len(await self._active_turns(project_id)),
            "running_goals": len(await self._running_goals(project_id)),
        }

    async def place(self, project_id: str, body: HoldPlace) -> ProjectHold:
        await self.store.get(Engagement, project_id)
        async with self._lock(project_id):
            existing = await active_hold(self.store, project_id)
            if existing is not None:
                if body.deployment_id and existing.deployment_id == body.deployment_id:
                    return existing
                raise ConflictError(
                    f"Project already has hold {existing.id} ({existing.reason})"
                )
            hold = await self.store.create(
                ProjectHold(
                    engagement_id=project_id,
                    reason=body.reason,
                    deployment_id=body.deployment_id,
                    grace_seconds=body.grace_seconds,
                    grace_until=utc_now() + timedelta(seconds=body.grace_seconds),
                )
            )
            hold = await self._pause_running_goals(hold)
        return await self.tick(project_id) or hold

    async def _pause_running_goals(self, hold: ProjectHold) -> ProjectHold:
        """Pause every running goal with this hold's mark; record each one."""

        paused = {item.goal_id for item in hold.paused_goals}
        added: list[ProjectHoldGoal] = []
        for goal in await self._running_goals(hold.engagement_id):
            if goal.id in paused:
                continue
            try:
                await self.goals.write_latest(
                    goal.session_id,
                    "pause",
                    reason=f"Paused by a project hold: {hold.reason}",
                    hold_id=hold.id,
                    check=lambda latest: _require_running(latest),
                )
            except (ConflictError, NotFoundError):
                # diagnostic-expected: the goal stopped running on its own.
                continue
            added.append(ProjectHoldGoal(goal_id=goal.id, session_id=goal.session_id))
        if not added:
            return hold

        def plan(latest: ProjectHold) -> dict[str, Any]:
            known = {item.goal_id for item in latest.paused_goals}
            return {
                "paused_goals": [
                    *latest.paused_goals,
                    *(item for item in added if item.goal_id not in known),
                ]
            }

        return await revise_latest(self.store, ProjectHold, hold.id, plan)

    async def tick(self, project_id: str) -> ProjectHold | None:
        """Advance the project's active hold; return it, or None if released."""

        async with self._lock(project_id):
            hold = await active_hold(self.store, project_id)
            if hold is None:
                return None
            # A goal started or resumed in the moment before the hold existed
            # is caught here and paused under the hold as well.
            hold = await self._pause_running_goals(hold)
            active = await self._active_turns(project_id)
            grace_until = hold.grace_until or hold.created_at + timedelta(
                seconds=hold.grace_seconds
            )
            if active and utc_now() >= grace_until:
                hold = await self._stop_turns(hold, active)
                active = await self._active_turns(project_id)
            status = "holding" if active else "held"
            if status != hold.status:
                hold = await revise_latest(
                    self.store,
                    ProjectHold,
                    hold.id,
                    lambda latest: (
                        None
                        if latest.status == "released"
                        else {
                            "status": status,
                            "held_at": utc_now() if status == "held" else None,
                        }
                    ),
                )
            return hold

    async def _stop_turns(
        self, hold: ProjectHold, turns: list[ChatTurn]
    ) -> ProjectHold:
        stopped: list[ProjectHoldTurn] = []
        for turn in turns:
            try:
                if turn.backend == ChatBackend.HARNESS and turn.harness_turn_id:
                    await self.stop_harness_turn(turn.harness_turn_id, STOP_REASON)
                else:
                    await self.stop_provider_turn(turn.id)
                outcome = "stopped"
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.project_hold.stop_failed",
                    "A project hold could not stop a running turn; the next tick retries.",
                    exc,
                    stage="project-hold",
                )
                outcome = f"stop failed: {type(exc).__name__}"
            stopped.append(
                ProjectHoldTurn(
                    turn_id=turn.id, session_id=turn.session_id, outcome=outcome
                )
            )
        subagents = [
            record.id
            for record in await self.store.find_entities(
                ChatSubagent,
                {"status": ChatSubagentStatus.RUNNING.value},
                engagement_id=hold.engagement_id,
            )
        ]

        def plan(latest: ProjectHold) -> dict[str, Any]:
            seen = {(item.turn_id, item.outcome) for item in latest.interrupted_turns}
            return {
                "interrupted_turns": [
                    *latest.interrupted_turns,
                    *(
                        item
                        for item in stopped
                        if (item.turn_id, item.outcome) not in seen
                    ),
                ][-5000:],
                "stopped_subagents": sorted(
                    {*latest.stopped_subagents, *subagents}
                )[:5000],
            }

        return await revise_latest(self.store, ProjectHold, hold.id, plan)

    async def release(
        self, project_id: str, hold_id: str, body: HoldRelease
    ) -> ProjectHold:
        async with self._lock(project_id):
            hold = await self.store.get(ProjectHold, hold_id)
            if hold.engagement_id != project_id:
                raise NotFoundError(f"project_holds entity not found: {hold_id}")
            if hold.status != "released":
                # Released first: the goal gate admits resumes only once no
                # hold is active, and only for goals carrying this hold's mark.
                hold = await revise_latest(
                    self.store,
                    ProjectHold,
                    hold.id,
                    lambda latest: (
                        None
                        if latest.status == "released"
                        else {
                            "status": "released",
                            "released_at": utc_now(),
                            "release_reason": body.reason,
                        }
                    ),
                )
            restored = await self._restore_goals(hold)
            if restored:
                hold = await revise_latest(
                    self.store,
                    ProjectHold,
                    hold.id,
                    lambda latest: {
                        "restored_goals": [*latest.restored_goals, *restored][-1000:]
                    },
                )
        await self._wake(project_id)
        return hold

    async def _restore_goals(self, hold: ProjectHold) -> list[ProjectHoldRestore]:
        """Resume every goal still carrying this hold's mark, and only those."""

        restored: list[ProjectHoldRestore] = []
        marked = await self.store.find_entities(
            ChatGoal, {f"metadata.{HOLD_MARKER}": hold.id}, engagement_id=hold.engagement_id
        )
        for goal in marked:
            try:
                resumed = await self.goals.write_latest(
                    goal.session_id,
                    "resume",
                    hold_id=hold.id,
                    check=lambda latest: _require_marked(latest, hold.id),
                )
                outcome = "resumed"
                if self.dispatch_goal is not None:
                    try:
                        await self.dispatch_goal(
                            goal.session_id, resumed.resume_instruction
                        )
                    except Exception as exc:
                        # The running goal's reconciler continues it later.
                        record_caught_exception(
                            "chat",
                            "chat.project_hold.dispatch_failed",
                            "A goal resumed after a project hold could not start at once; its reconciler continues it.",
                            exc,
                            stage="project-hold",
                        )
            except Exception as exc:
                # diagnostic-expected: the refusal is recorded in the hold's
                # restored_goals. A goal that cannot resume (a spent budget,
                # say) stays paused without the mark, so its operator can
                # resume it normally.
                outcome = f"left paused: {str(exc)[:300] or type(exc).__name__}"
                await self._clear_mark(goal.id, hold.id)
            restored.append(ProjectHoldRestore(goal_id=goal.id, outcome=outcome))
        return restored

    async def _clear_mark(self, goal_id: str, hold_id: str) -> None:
        def plan(latest: ChatGoal) -> dict[str, Any] | None:
            if latest.metadata.get(HOLD_MARKER) != hold_id:
                return None
            return {
                "metadata": {
                    key: value
                    for key, value in latest.metadata.items()
                    if key != HOLD_MARKER
                }
            }

        try:
            await revise_latest(self.store, ChatGoal, goal_id, plan)
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.project_hold.mark_clear_failed",
                "A goal kept a released hold's mark; release the hold again to clear it.",
                exc,
                stage="project-hold",
            )

    async def _wake(self, project_id: str) -> None:
        if self.wake_subagents is not None:
            await self.wake_subagents(project_id)
        if self.wake_queues is not None:
            self.wake_queues()

    async def tick_all(self) -> None:
        """Advance every active hold; called from Core's periodic chat pass."""

        holds = await self.store.find_entities(
            ProjectHold, {"status": ["holding", "held"]}
        )
        for project_id in sorted({hold.engagement_id for hold in holds}):
            try:
                await self.tick(project_id)
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.project_hold.tick_failed",
                    "A project hold pass failed; the next pass retries.",
                    exc,
                    stage="project-hold",
                )


def _require_running(goal: ChatGoal) -> None:
    if goal.status != ChatGoalStatus.RUNNING:
        raise ConflictError("goal is no longer running")


def _require_marked(goal: ChatGoal, hold_id: str) -> None:
    if goal.metadata.get(HOLD_MARKER) != hold_id:
        raise ConflictError("goal no longer carries this hold's mark")


def project_holds_router(service: ProjectHoldService) -> APIRouter:
    router = APIRouter(tags=["project-holds"])

    @router.get("/engagements/{project_id}/hold")
    async def read_hold(project_id: str) -> dict[str, Any]:
        return await service.view(project_id)

    @router.post("/engagements/{project_id}/hold")
    async def place_hold(project_id: str, body: HoldPlace) -> dict[str, Any]:
        await service.place(project_id, body)
        return await service.view(project_id)

    @router.post("/engagements/{project_id}/hold/{hold_id}/release")
    async def release_hold(
        project_id: str, hold_id: str, body: HoldRelease
    ) -> dict[str, Any]:
        await service.release(project_id, hold_id, body)
        return await service.view(project_id)

    return router
