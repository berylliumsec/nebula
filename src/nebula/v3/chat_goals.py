"""Core-owned goals for provider-backed chat sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .domain import (
    CHAT_GOAL_CHILD_LIMIT,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    utc_now,
)
from .storage import ConflictError, NebulaStore, NotFoundError, StoreTransaction
from .skill_catalog import SkillSelection, SkillSnapshot


class GoalCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=20_000)
    completion_criteria: list[str] = Field(min_length=1, max_length=50)
    plan: list[str] = Field(default_factory=list, max_length=200)
    token_budget: int | None = Field(default=None, ge=1)
    time_budget_seconds: int | None = Field(default=None, ge=1)
    step_budget: int | None = Field(default=None, ge=1)
    child_budget: int | None = Field(default=None, ge=0, le=CHAT_GOAL_CHILD_LIMIT)


class GoalWrite(BaseModel):
    expected_revision: int = Field(ge=1)
    action: Literal["start", "pause", "resume", "cancel", "block", "complete"]
    reason: str | None = Field(default=None, max_length=2_000)
    completion_summary: str | None = Field(default=None, max_length=20_000)
    completion_evidence: list[dict] = Field(default_factory=list, max_length=200)


class GoalSkillWrite(BaseModel):
    expected_revision: int = Field(ge=1)
    skills: list[SkillSelection] = Field(default_factory=list, max_length=20)


class ChatGoalService:
    def __init__(self, store: NebulaStore):
        self.store = store

    def get(self, session_id: str) -> ChatGoal:
        self.store.get(ChatSession, session_id)
        goals = self.store.list_session_entities(ChatGoal, session_id)
        if not goals:
            raise NotFoundError(f"chat goal not found for session: {session_id}")
        if len(goals) > 1:
            raise ConflictError("conversation has more than one authoritative goal")
        return goals[0]

    def create(self, session_id: str, body: GoalCreate) -> ChatGoal:
        session = self.store.get(ChatSession, session_id)
        if session.backend != ChatBackend.PROVIDER:
            raise ConflictError("provider-backed goals require a provider conversation")
        try:
            self.get(session_id)
        except (
            NotFoundError
        ):  # diagnostic-expected: absence is the precondition for creating a goal
            pass
        else:
            raise ConflictError("conversation already has a goal")
        return self.store.create(
            ChatGoal(
                id=str(uuid4()),
                engagement_id=session.engagement_id,
                session_id=session.id,
                objective=body.objective,
                completion_criteria=body.completion_criteria,
                plan=body.plan,
                token_budget=body.token_budget,
                time_budget_seconds=body.time_budget_seconds,
                step_budget=body.step_budget,
                child_budget=body.child_budget,
            )
        )

    def write(self, session_id: str, body: GoalWrite) -> ChatGoal:
        goal = self.get(session_id)
        if body.expected_revision != goal.revision:
            raise ConflictError(
                "goal changed on another device; reload before retrying"
            )
        now = utc_now()
        changes: dict = {}
        propagate: ChatGoalStatus | None = None
        if body.action == "start":
            if goal.status != ChatGoalStatus.DRAFT:
                raise ConflictError("only a draft goal can be started")
            changes = {
                "status": ChatGoalStatus.RUNNING,
                "started_at": now,
                "active_since": now,
            }
        elif body.action == "pause":
            if goal.status != ChatGoalStatus.RUNNING:
                raise ConflictError("only a running goal can be paused")
            changes = {
                "status": ChatGoalStatus.PAUSED,
                "paused_at": now,
                "active_since": None,
                "elapsed_seconds": goal.active_elapsed_seconds(now),
            }
            propagate = ChatGoalStatus.PAUSED
        elif body.action == "resume":
            if goal.status not in {ChatGoalStatus.PAUSED, ChatGoalStatus.BLOCKED}:
                raise ConflictError("only a paused or blocked goal can be resumed")
            if (
                goal.time_budget_seconds is not None
                and goal.elapsed_seconds >= goal.time_budget_seconds
            ):
                raise ConflictError("goal time budget is exhausted")
            changes = {
                "status": ChatGoalStatus.RUNNING,
                "paused_at": None,
                "active_since": now,
                "blocked_reason": None,
                "consecutive_stalls": 0,
            }
        elif body.action == "cancel":
            if goal.status in {ChatGoalStatus.COMPLETED, ChatGoalStatus.CANCELLED}:
                raise ConflictError("goal is already terminal")
            changes = {
                "status": ChatGoalStatus.CANCELLED,
                "completed_at": now,
                "active_since": None,
                "elapsed_seconds": goal.active_elapsed_seconds(now),
            }
            propagate = ChatGoalStatus.CANCELLED
        elif body.action == "block":
            if goal.status != ChatGoalStatus.RUNNING:
                raise ConflictError("only a running goal can be blocked")
            if not body.reason:
                raise HTTPException(422, "blocking a goal requires a reason")
            changes = {
                "status": ChatGoalStatus.BLOCKED,
                "blocked_reason": body.reason,
                "consecutive_stalls": max(3, goal.consecutive_stalls),
                "active_since": None,
                "elapsed_seconds": goal.active_elapsed_seconds(now),
            }
        else:
            if goal.status != ChatGoalStatus.RUNNING:
                raise ConflictError("only a running goal can be completed")
            if not body.completion_summary or not body.completion_evidence:
                raise HTTPException(422, "completion requires a summary and evidence")
            changes = {
                "status": ChatGoalStatus.COMPLETED,
                "completed_at": now,
                "completion_summary": body.completion_summary,
                "completion_evidence": body.completion_evidence,
                "active_since": None,
                "elapsed_seconds": goal.active_elapsed_seconds(now),
            }
        # Children are read before the unit of work opens and written inside
        # it, after the parent's guarded update: a revision conflict on either
        # side rolls the whole stop back instead of leaving children changed
        # under an unchanged parent.
        children = self.list_children(session_id) if propagate is not None else []
        with self.store.transaction() as transaction:
            updated = transaction.update(
                ChatGoal, goal.id, changes, expected_revision=goal.revision
            )
            if propagate is not None:
                self._propagate_parent_stop(transaction, children, propagate, now)
        return updated

    def reserve_child(self, goal_id: str, *, expected_revision: int) -> ChatGoal:
        """Reserve one cumulative child slot before delegation begins."""

        goal = self.store.get(ChatGoal, goal_id)
        if goal.revision != expected_revision:
            raise ConflictError("goal changed before child capacity was reserved")
        if goal.status != ChatGoalStatus.RUNNING:
            raise ConflictError("child work requires a running goal")
        if goal.child_budget is None:
            raise ConflictError("goal has no child budget; delegation is disabled")
        if goal.children_started >= goal.child_budget:
            raise ConflictError("goal child budget is exhausted")
        if len(goal.child_session_ids) >= CHAT_GOAL_CHILD_LIMIT:
            raise ConflictError(
                f"goal cannot list more than {CHAT_GOAL_CHILD_LIMIT} children"
            )
        return self.store.update(
            ChatGoal,
            goal.id,
            {"children_started": goal.children_started + 1},
            expected_revision=goal.revision,
        )

    def start_child(self, session_id: str, body: GoalCreate) -> ChatGoal:
        parent = self.get(session_id)
        reserved = self.reserve_child(parent.id, expected_revision=parent.revision)
        parent_session = self.store.get(ChatSession, session_id)
        child_session = self.store.create(
            ChatSession(
                id=str(uuid4()),
                engagement_id=parent_session.engagement_id,
                title=f"{parent.objective[:80]} (child)",
                backend=parent_session.backend,
                provider_profile_id=parent_session.provider_profile_id,
                model=parent_session.model,
                parent_session_id=parent_session.id,
                metadata={
                    "delegated_from_session_id": parent_session.id,
                    "delegated_from_goal_id": parent.id,
                    "workspace_is_shared": False,
                    "isolated_child_workspace": True,
                },
            )
        )
        child = self.store.create(
            ChatGoal(
                engagement_id=parent.engagement_id,
                session_id=child_session.id,
                objective=body.objective,
                completion_criteria=body.completion_criteria,
                plan=body.plan,
                token_budget=body.token_budget,
                time_budget_seconds=body.time_budget_seconds,
                step_budget=body.step_budget,
                child_budget=0,
                parent_goal_id=parent.id,
                skill_snapshots=parent.skill_snapshots,
                status=ChatGoalStatus.DRAFT,
            )
        )
        self.store.update(
            ChatGoal,
            reserved.id,
            {"child_session_ids": [*reserved.child_session_ids, child_session.id]},
            expected_revision=reserved.revision,
        )
        return child

    def list_children(self, session_id: str) -> list[ChatGoal]:
        try:
            parent = self.get(session_id)
        except (
            NotFoundError
        ):  # diagnostic-expected: a conversation without a goal has no children
            return []
        children: list[ChatGoal] = []
        offset = 0
        while page := self.store.list_entities(ChatGoal, offset=offset, limit=1_000):
            children.extend(item for item in page if item.parent_goal_id == parent.id)
            offset += len(page)
        return children

    def _propagate_parent_stop(
        self,
        transaction: StoreTransaction,
        children: list[ChatGoal],
        status: ChatGoalStatus,
        now: datetime,
    ) -> None:
        for child in children:
            if child.status in {ChatGoalStatus.COMPLETED, ChatGoalStatus.CANCELLED}:
                continue
            # Only child work that passed Start pauses with its parent; a draft
            # child stays a draft rather than becoming resumable without ever
            # having started. Cancelling still retires drafts.
            if status == ChatGoalStatus.PAUSED and child.status == ChatGoalStatus.DRAFT:
                continue
            transaction.update(
                ChatGoal,
                child.id,
                {
                    "status": status,
                    "paused_at": now
                    if status == ChatGoalStatus.PAUSED
                    else child.paused_at,
                    "completed_at": now
                    if status == ChatGoalStatus.CANCELLED
                    else child.completed_at,
                    "active_since": None,
                    "elapsed_seconds": child.active_elapsed_seconds(now),
                    "blocked_reason": (
                        "Parent goal paused; child work is paused."
                        if status == ChatGoalStatus.PAUSED
                        else child.blocked_reason
                    ),
                },
                expected_revision=child.revision,
            )

    def replace_skills(
        self,
        session_id: str,
        *,
        expected_revision: int,
        snapshots: list[SkillSnapshot],
    ) -> ChatGoal:
        """Atomically replace attached immutable skills at a safe goal boundary."""

        goal = self.get(session_id)
        if goal.revision != expected_revision:
            raise ConflictError(
                "goal changed on another device; reload before retrying"
            )
        if goal.status in {ChatGoalStatus.COMPLETED, ChatGoalStatus.CANCELLED}:
            raise ConflictError("terminal goal skills cannot be edited")
        if goal.execution_claim_id is not None:
            raise ConflictError(
                "pause or wait for active goal work before editing skills"
            )
        paths = [item.path for item in snapshots]
        if len(paths) != len(set(paths)):
            raise ConflictError("the same skill cannot be attached more than once")
        return self.store.update(
            ChatGoal,
            goal.id,
            {"skill_snapshots": [item.model_dump(mode="json") for item in snapshots]},
            expected_revision=goal.revision,
        )


def goals_router(
    store: NebulaStore,
    *,
    skill_snapshot_resolver: Callable[
        [str, list[SkillSelection], list[SkillSnapshot]], list[SkillSnapshot]
    ]
    | None = None,
) -> APIRouter:
    router = APIRouter(tags=["chat"])
    service = ChatGoalService(store)

    @router.get("/chat/sessions/{session_id}/goal", response_model=ChatGoal)
    def get_goal(session_id: str) -> ChatGoal:
        return service.get(session_id)

    @router.post("/chat/sessions/{session_id}/goal", response_model=ChatGoal)
    def create_goal(session_id: str, body: GoalCreate) -> ChatGoal:
        return service.create(session_id, body)

    @router.post("/chat/sessions/{session_id}/goal/actions", response_model=ChatGoal)
    def write_goal(session_id: str, body: GoalWrite) -> ChatGoal:
        return service.write(session_id, body)

    @router.get(
        "/chat/sessions/{session_id}/goal/children", response_model=list[ChatGoal]
    )
    def list_goal_children(session_id: str) -> list[ChatGoal]:
        return service.list_children(session_id)

    @router.post("/chat/sessions/{session_id}/goal/children", response_model=ChatGoal)
    def start_goal_child(session_id: str, body: GoalCreate) -> ChatGoal:
        return service.start_child(session_id, body)

    @router.put("/chat/sessions/{session_id}/goal/skills", response_model=ChatGoal)
    def replace_goal_skills(session_id: str, body: GoalSkillWrite) -> ChatGoal:
        if skill_snapshot_resolver is None:
            raise HTTPException(503, "skill discovery is unavailable")
        try:
            goal = service.get(session_id)
            existing = [
                SkillSnapshot.model_validate(item) for item in goal.skill_snapshots
            ]
            snapshots = skill_snapshot_resolver(session_id, body.skills, existing)
        except (OSError, ValueError) as error:
            raise HTTPException(422, str(error)) from error
        return service.replace_skills(
            session_id,
            expected_revision=body.expected_revision,
            snapshots=snapshots,
        )

    return router
