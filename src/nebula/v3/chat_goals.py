"""Core-owned goals for provider and harness chat sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from .chat_subagents import SUBAGENT_LIMIT_CEILING
from .chat_agent_messages import resolve_agent_messaging_choice
from .diagnostics import record_caught_exception
from .domain import (
    CHAT_GOAL_CHILD_LIMIT,
    ChatBackend,
    Approval,
    ApprovalStatus,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatQueue,
    ChatMessage,
    ChatRole,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessProfile,
    HarnessSession,
    McpServerProfile,
    ProviderProfile,
    ScopePolicy,
    utc_now,
)
from .providers import ReasoningEffort
from .chat_snapshot_parts import (
    resolve_skill_snapshots,
    split_skill_snapshots,
    stage_snapshot_parts,
)
from .storage import ConflictError, NebulaStore, NotFoundError, StoreTransaction
from .skill_catalog import SkillSelection, SkillSnapshot


class GoalCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=20_000)
    completion_criteria: list[str] = Field(min_length=1, max_length=50)
    plan: list[str] = Field(default_factory=list, max_length=200)
    token_budget: int | None = Field(default=None, ge=1)
    time_budget_seconds: int | None = Field(default=None, ge=30 * 60)
    step_budget: int | None = Field(default=None, ge=1)
    child_budget: int | None = Field(default=None, ge=0, le=CHAT_GOAL_CHILD_LIMIT)


class GoalWrite(BaseModel):
    expected_revision: int = Field(ge=1)
    action: Literal["start", "pause", "resume", "cancel", "block", "complete"]
    reason: str | None = Field(default=None, max_length=2_000)
    completion_summary: str | None = Field(default=None, max_length=20_000)
    completion_evidence: list[dict] = Field(default_factory=list, max_length=200)


class GoalUpdate(GoalCreate):
    expected_revision: int = Field(ge=1)


class GoalSkillWrite(BaseModel):
    expected_revision: int = Field(ge=1)
    skills: list[SkillSelection] = Field(default_factory=list, max_length=20)


class GoalConversationCreate(GoalCreate):
    """Create a durable conversation before its first message."""

    engagement_id: str = Field(min_length=1, max_length=200)
    backend: ChatBackend = ChatBackend.PROVIDER
    provider_id: str | None = Field(default=None, min_length=1, max_length=200)
    harness_profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=500)
    tools_enabled: bool = False
    planning_enabled: bool = False
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=64)
    hook_ids: list[str] = Field(default_factory=list, max_length=32)
    # The rest of the composer's choices, saved as a send or the assistant
    # settings PATCH would save them: the goal's first turn is one Core
    # starts, and it reads them from the conversation.
    reasoning_effort: ReasoningEffort | None = None
    harness_reasoning_effort: str | None = None
    harness_service_tier: str | None = None
    harness_cyber_access_program: str | None = None
    subagent_provider_id: str | None = None
    subagent_harness_id: str | None = None
    subagent_model: str | None = None
    allow_subagents: bool = False
    subagent_reasoning_effort: ReasoningEffort | None = None
    allow_agent_messaging: bool | None = None
    max_active_subagents: int | None = Field(
        default=None, ge=1, le=SUBAGENT_LIMIT_CEILING
    )

    @model_validator(mode="after")
    def selections_are_unique(self) -> "GoalConversationCreate":
        if self.backend == ChatBackend.PROVIDER:
            if not self.provider_id or self.harness_profile_id:
                raise ValueError("provider goals require only provider_id")
        elif not self.harness_profile_id or self.provider_id:
            raise ValueError("harness goals require only harness_profile_id")
        if len(self.mcp_server_ids) != len(set(self.mcp_server_ids)):
            raise ValueError("MCP server selection contains duplicates")
        if len(self.hook_ids) != len(set(self.hook_ids)):
            raise ValueError("hook selection contains duplicates")
        return self


class GoalConversationCreated(BaseModel):
    session: ChatSession
    goal: ChatGoal


class GoalHandoffRequest(BaseModel):
    """One revision-checked recovery from a pre-admission harness plan failure."""

    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2_000)
    checkpoint: str | None = Field(default=None, max_length=4_000)
    max_steps: int = Field(default=20, ge=1, le=100)
    max_time_seconds: int = Field(default=7_200, ge=1_800, le=86_400)


class GoalHandoffCreated(GoalConversationCreated):
    source_goal: ChatGoal
    already_exists: bool = False


def active_time_changes(goal: ChatGoal, *, working: bool, now: datetime) -> dict:
    """Open or close a running goal's active stretch to match its work.

    Active time is time Nebula works on the goal: while a Core worker
    produces one of its turns, or one of its subagents runs. Waiting between
    turns, for Core capacity, on an approval or on a background command's
    callback is not, and neither is a pause. ``active_since`` is set exactly
    while such a stretch is open; the closed stretches are banked in
    ``elapsed_seconds``.
    """

    if goal.status != ChatGoalStatus.RUNNING:
        return {}
    if working and goal.active_since is None:
        return {"active_since": now}
    if not working and goal.active_since is not None:
        return {
            "active_since": None,
            "elapsed_seconds": goal.active_elapsed_seconds(now),
        }
    return {}


class ChatGoalService:
    def __init__(self, store: NebulaStore, harness_runtime: Any | None = None):
        self.store = store
        self.harness_runtime = harness_runtime

    def _validate_token_budget(self, session: ChatSession, budget: int | None) -> None:
        if session.backend != ChatBackend.HARNESS or budget is None:
            return
        profile = self.store.get(HarnessProfile, session.harness_profile_id or "")
        if not profile.capabilities.detailed_usage:
            raise ConflictError(
                "this harness does not report token usage; use a step or time limit"
            )

    def has_working_subagents(self, session_id: str) -> bool:
        """Whether this conversation has a running child at any depth."""

        pending = [session_id]
        seen = {session_id}
        while pending:
            children = self.store.find_entities(
                ChatSubagent, {"parent_session_id": pending.pop()}
            )
            for child in children:
                if child.status == ChatSubagentStatus.RUNNING:
                    return True
                if child.child_session_id in seen:
                    raise ConflictError("subagent descendant chain is invalid")
                seen.add(child.child_session_id)
                pending.append(child.child_session_id)
        return False

    def settle_active_time(self, session_id: str) -> None:
        """Bank or reopen active time after a conversation's work changed.

        Claiming and releasing a goal turn update the stretch in the same
        write; this covers what happens between them, such as the goal's last
        running subagent finishing while no turn is being produced.
        """

        last_error: ConflictError | None = None
        for _ in range(3):
            try:
                goal = self.get(session_id)
            except (
                NotFoundError
            ):  # diagnostic-expected: most conversations have no goal
                return
            working = goal.execution_claim_id is not None or (
                self.has_working_subagents(session_id)
            )
            changes = active_time_changes(goal, working=working, now=utc_now())
            if not changes:
                return
            try:
                self.store.update(
                    ChatGoal, goal.id, changes, expected_revision=goal.revision
                )
                return
            except (
                ConflictError
            ) as exc:  # diagnostic-expected: reread a concurrent goal write
                last_error = exc
        if last_error is not None:
            record_caught_exception(
                "chat",
                "chat.goal.active_time_settle_failed",
                "A goal's active time could not be updated after its work changed.",
                last_error,
                stage="goal-active-time",
            )

    def get(self, session_id: str) -> ChatGoal:
        self.store.get(ChatSession, session_id)
        goals = self.store.list_session_entities(ChatGoal, session_id)
        if not goals:
            raise NotFoundError(f"chat goal not found for session: {session_id}")
        if len(goals) > 1:
            raise ConflictError("conversation has more than one authoritative goal")
        return goals[0]

    def read(self, session_id: str) -> ChatGoal:
        """Return current presentation state without advancing durable revision."""
        goal = self.get(session_id)
        if goal.status != ChatGoalStatus.RUNNING or goal.active_since is None:
            return goal
        # The open stretch is reported as banked up to now and still open from
        # now, so a client that adds the time since ``active_since`` to
        # ``elapsed_seconds`` counts it once, not twice.
        now = utc_now()
        return goal.model_copy(
            update={
                "elapsed_seconds": goal.active_elapsed_seconds(now),
                "active_since": now,
            }
        )

    def create(self, session_id: str, body: GoalCreate) -> ChatGoal:
        session = self.store.get(ChatSession, session_id)
        self._validate_token_budget(session, body.token_budget)
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

    def create_conversation(
        self, body: GoalConversationCreate
    ) -> GoalConversationCreated:
        """Persist an empty conversation and its goal draft as one unit."""

        self.store.get(Engagement, body.engagement_id)
        if body.backend == ChatBackend.HARNESS:
            if self.harness_runtime is None:
                raise ConflictError("harness goals are unavailable in this Core")
            profile = self.store.get(HarnessProfile, body.harness_profile_id or "")
            if (
                body.token_budget is not None
                and not profile.capabilities.detailed_usage
            ):
                raise ConflictError(
                    "this harness does not report token usage; use a step or time limit"
                )
            with self.store.transaction() as transaction:
                harness_session = self.harness_runtime.create_session(
                    engagement_id=body.engagement_id,
                    profile_id=profile.id,
                    model=body.model,
                    mcp_server_ids=body.mcp_server_ids,
                    reasoning_effort=body.harness_reasoning_effort,
                    service_tier=body.harness_service_tier,
                    cyber_access_program=body.harness_cyber_access_program,
                    tools_enabled=body.tools_enabled,
                    transaction=transaction,
                )
                session = ChatSession(
                    id=str(uuid4()),
                    engagement_id=body.engagement_id,
                    title=" ".join(body.objective.split())[:300] or "Goal conversation",
                    backend=ChatBackend.HARNESS,
                    harness_profile_id=profile.id,
                    harness_session_id=harness_session.id,
                    model=harness_session.model,
                    metadata={
                        "context_management": "runtime_managed",
                        "initial_title_state": "goal",
                        "harness_runtime_options": harness_session.metadata.get(
                            "runtime_options", {}
                        ),
                        "provider_subagent": (
                            {
                                "provider_profile_id": body.subagent_provider_id or "",
                                "harness_profile_id": body.subagent_harness_id,
                                "model": body.subagent_model,
                                "max_active": body.max_active_subagents,
                                "reasoning_effort": body.subagent_reasoning_effort,
                            }
                            if body.allow_subagents
                            and body.subagent_model
                            and (body.subagent_provider_id or body.subagent_harness_id)
                            else None
                        ),
                        "allow_agent_messaging": body.allow_agent_messaging,
                        "planning_enabled": body.planning_enabled,
                    },
                )
                goal = ChatGoal(
                    engagement_id=body.engagement_id,
                    session_id=session.id,
                    objective=body.objective,
                    completion_criteria=body.completion_criteria,
                    plan=body.plan,
                    token_budget=body.token_budget,
                    time_budget_seconds=body.time_budget_seconds,
                    step_budget=body.step_budget,
                    child_budget=body.child_budget,
                )
                transaction.add_all([session, goal])
            return GoalConversationCreated(session=session, goal=goal)
        session, goal = self._provider_conversation_entities(body)
        with self.store.transaction() as transaction:
            transaction.add_all([session, goal])
        return GoalConversationCreated(session=session, goal=goal)

    def _provider_conversation_entities(
        self, body: GoalConversationCreate
    ) -> tuple[ChatSession, ChatGoal]:
        """Build a provider draft for an ordinary create or an atomic handoff."""

        provider = self.store.get(ProviderProfile, body.provider_id or "")
        if not provider.enabled:
            raise ConflictError("the selected provider is disabled")
        if provider.model_allowlist and body.model not in provider.model_allowlist:
            raise ConflictError(
                f"model {body.model!r} is not allowed by provider {provider.id!r}"
            )
        for profile_id in body.mcp_server_ids:
            mcp_profile = self.store.get(McpServerProfile, profile_id)
            if not mcp_profile.enabled:
                raise ConflictError(
                    f"MCP server {mcp_profile.name!r} is disabled and cannot be selected"
                )
        session_id = str(uuid4())
        title = " ".join(body.objective.split())[:300] or "Goal conversation"
        session = ChatSession(
            id=session_id,
            engagement_id=body.engagement_id,
            title=title,
            provider_profile_id=provider.id,
            model=body.model,
            metadata={
                "tools_enabled": body.tools_enabled,
                "planning_enabled": body.planning_enabled,
                "mcp_server_ids": body.mcp_server_ids,
                "hook_ids": body.hook_ids,
                "reasoning_effort": body.reasoning_effort,
                "allow_subagents": body.allow_subagents,
                "subagent_reasoning_effort": body.subagent_reasoning_effort,
                "allow_agent_messaging": resolve_agent_messaging_choice(
                    self.store, body.engagement_id, None, body.allow_agent_messaging
                ),
                "max_active_subagents": body.max_active_subagents,
                "message_count": 0,
                "last_sequence": 0,
                "initial_title_state": "goal",
            },
        )
        goal = ChatGoal(
            id=str(uuid4()),
            engagement_id=body.engagement_id,
            session_id=session_id,
            objective=body.objective,
            completion_criteria=body.completion_criteria,
            plan=body.plan,
            token_budget=body.token_budget,
            time_budget_seconds=body.time_budget_seconds,
            step_budget=body.step_budget,
            child_budget=body.child_budget,
        )
        return session, goal

    def handoff_to_provider(
        self, session_id: str, body: GoalHandoffRequest
    ) -> GoalHandoffCreated:
        """Keep a failed harness goal paused and atomically link one provider draft.

        The old turn never passed admission, so no native action from that
        attempt can be silently repeated. Starting the new draft is a separate
        revisioned action, which Butler can safely retry after a lost response.
        """

        source = self.get(session_id)
        existing_id = source.metadata.get("handoff_target_session_id")
        if isinstance(existing_id, str):
            target = self.store.get(ChatSession, existing_id)
            return GoalHandoffCreated(
                session=target,
                goal=self.get(target.id),
                source_goal=source,
                already_exists=True,
            )
        if source.revision != body.expected_revision:
            raise ConflictError("source goal changed; read it again before handoff")
        old_session = self.store.get(ChatSession, session_id)
        if (
            source.status != ChatGoalStatus.PAUSED
            or source.parent_goal_id is not None
            or old_session.backend != ChatBackend.HARNESS
        ):
            raise ConflictError("only a paused harness goal can be handed off")
        if source.execution_claim_id or self.has_working_subagents(session_id):
            raise ConflictError("the source goal still has active work")
        latest = self.store.list_session_entities(
            ChatTurn, session_id, newest_first=True, limit=1
        )
        if not latest or not (
            latest[0].backend == ChatBackend.HARNESS
            and latest[0].goal_id == source.id
            and latest[0].status == ChatTurnStatus.INTERRUPTED
            and latest[0].admitted_at is None
            and latest[0].execution_tool_calls == 0
            and not latest[0].tool_call_ids
            and (latest[0].error or "").startswith(
                "This harness configuration cannot enforce restricted discovery"
            )
        ):
            raise ConflictError(
                "handoff requires a pre-admission harness planning failure with no tool calls"
            )
        for affected_session_id in (session_id, *source.child_session_ids):
            if self.store.find_entities(
                Approval,
                {
                    "chat_session_id": affected_session_id,
                    "status": ApprovalStatus.PENDING,
                },
                engagement_id=source.engagement_id,
                limit=1,
            ):
                raise ConflictError(
                    "resolve the source goal's pending approval before handoff"
                )
        queues = self.store.list_session_entities(ChatQueue, session_id)
        if queues and any(
            any(
                item.get("status") in {"claiming", "sending", "needs_review"}
                for item in queue.items
            )
            or not queue.paused
            and any(
                item.get("status") not in {"complete", "cancelled"}
                for item in queue.items
            )
            for queue in queues
        ):
            raise ConflictError(
                "pause and resolve active or unreviewed source follow-ups before handoff"
            )

        from .chat_assistant_config import chat_assistant_config
        from .context import resolve_context_limits
        from .mcp import resolve_mcp_profiles

        selected = chat_assistant_config(self.store, old_session)
        if (
            selected.get("backend") != "provider"
            or selected.get("planning_enabled") is not True
        ):
            raise ConflictError("save a provider runtime with Plan before acting first")
        profile_id = selected.get("profile_id")
        model = selected.get("model")
        if not isinstance(profile_id, str) or not isinstance(model, str):
            raise ConflictError("saved provider and model are missing")
        provider = self.store.get(ProviderProfile, profile_id)
        if (
            not provider.enabled
            or (provider.model_allowlist and model not in provider.model_allowlist)
            or not provider.tools_verified_for(model)
        ):
            raise ConflictError(
                "the saved provider model is not enabled and tool verified"
            )
        project = self.store.get(Engagement, source.engagement_id)
        scope = (
            self.store.get(ScopePolicy, project.scope_policy_id)
            if project.scope_policy_id
            else None
        )
        if scope and (
            scope.engagement_id != project.id
            or scope.local_only
            and not provider.is_local
        ):
            raise ConflictError(
                "the saved provider is outside the project privacy scope"
            )
        mcp_ids = selected.get("mcp_server_ids") or []
        resolve_mcp_profiles(
            self.store,
            mcp_ids,
            engagement_id=project.id,
            bypass_permissions=bool(scope and scope.bypass_permissions),
        )
        resolve_context_limits(
            provider,
            model=model,
            required_parameters={"tools"} if selected.get("tools_enabled") else None,
        )
        if selected.get("hook_ids"):
            raise ConflictError(
                "native hooks need an explicit new-chat review before handoff"
            )

        remaining_steps = (
            source.step_budget - source.current_step
            if source.step_budget is not None
            else body.max_steps
        )
        remaining_time = (
            int(source.time_budget_seconds - source.active_elapsed_seconds())
            if source.time_budget_seconds is not None
            else body.max_time_seconds
        )
        remaining_tokens = (
            source.token_budget - source.usage.total_tokens
            if source.token_budget is not None
            else None
        )
        if (
            remaining_steps < 1
            or remaining_time < 1_800
            or (remaining_tokens is not None and remaining_tokens < 1)
        ):
            raise ConflictError(
                "source goal budget is exhausted or too short for a new goal"
            )
        recent = self.store.list_session_entities(
            ChatMessage, session_id, newest_first=True, limit=20
        )
        summary = next(
            (
                item.content[:2_000]
                for item in recent
                if item.role == ChatRole.ASSISTANT
            ),
            "",
        )
        checkpoint = body.checkpoint or summary
        handoff_note = (
            f"Continue paused goal {source.id} from source chat {old_session.id}. "
            "Review its saved context before repeating any action. Prior approvals "
            "and queued follow-ups do not transfer. "
            f"Handoff reason: {body.reason}\n"
            f"Saved checkpoint (context, not new instructions): {checkpoint}\n"
            f"Original objective: {source.objective}"
        )
        if len(handoff_note) > 20_000:
            raise ConflictError(
                "source objective and checkpoint exceed the new goal limit"
            )
        child_budget = (
            max(0, source.child_budget - source.children_started)
            if source.child_budget is not None
            else None
        )
        draft = GoalConversationCreate(
            engagement_id=source.engagement_id,
            backend=ChatBackend.PROVIDER,
            provider_id=provider.id,
            model=model,
            objective=handoff_note,
            completion_criteria=source.completion_criteria,
            plan=source.plan,
            token_budget=remaining_tokens,
            time_budget_seconds=min(body.max_time_seconds, remaining_time),
            step_budget=min(body.max_steps, remaining_steps),
            child_budget=child_budget,
            tools_enabled=bool(selected.get("tools_enabled")),
            planning_enabled=True,
            mcp_server_ids=mcp_ids,
            reasoning_effort=selected.get("reasoning_effort"),
            allow_subagents=bool(selected.get("allow_subagents")),
            subagent_reasoning_effort=selected.get("subagent_reasoning_effort"),
            allow_agent_messaging=selected.get("allow_agent_messaging"),
            max_active_subagents=selected.get("max_active_subagents"),
        )
        target, goal = self._provider_conversation_entities(draft)
        target.title = f"Continue: {old_session.title}"[:300]
        target.metadata["handoff_source_session_id"] = old_session.id
        goal.metadata["handoff_source_goal_id"] = source.id
        goal.metadata["handoff_source_session_id"] = old_session.id
        goal.metadata["handoff_source_revision"] = source.revision
        goal.metadata["handoff_reason"] = body.reason
        from .butler_oversight import _entity_in_transaction

        with self.store.transaction() as transaction:
            current_session = _entity_in_transaction(
                transaction, ChatSession, old_session.id
            )
            if (
                current_session is None
                or current_session.revision != old_session.revision
            ):
                raise ConflictError(
                    "source chat settings changed; read them again before handoff"
                )
            transaction.add_all([target, goal])
            updated = transaction.update(
                ChatGoal,
                source.id,
                {
                    "metadata": {
                        **source.metadata,
                        "handoff_target_session_id": target.id,
                        "handoff_target_goal_id": goal.id,
                        "handoff_reason": body.reason,
                    }
                },
                expected_revision=source.revision,
            )
        return GoalHandoffCreated(session=target, goal=goal, source_goal=updated)

    def write(
        self, session_id: str, body: GoalWrite, *, allow_pending_recovery: bool = False
    ) -> ChatGoal:
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
                "blocked_reason": body.reason,
                "execution_owner_id": None,
                "execution_claim_id": None,
                "execution_claimed_at": None,
            }
            propagate = ChatGoalStatus.PAUSED
        elif body.action == "resume":
            if goal.status not in {ChatGoalStatus.PAUSED, ChatGoalStatus.BLOCKED}:
                raise ConflictError("only a paused or blocked goal can be resumed")
            if goal.metadata.get("handoff_target_session_id"):
                raise ConflictError(
                    "goal was handed off; continue in its linked provider chat"
                )
            if (
                goal.token_budget is not None
                and goal.metadata.get("usage_unknown") is True
            ):
                raise ConflictError(
                    "remove the token limit before resuming; harness usage was unavailable"
                )
            interrupted = [
                turn
                for turn in self.store.list_session_entities(ChatTurn, session_id)
                if turn.status == ChatTurnStatus.INTERRUPTED
                and turn.request_snapshot.get("recovery", {}).get("required")
            ]
            if interrupted and not allow_pending_recovery:
                raise ConflictError(
                    "the interrupted response needs recovery before the goal can resume"
                )
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
                "execution_owner_id": None,
                "execution_claim_id": None,
                "execution_claimed_at": None,
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
                "execution_owner_id": None,
                "execution_claim_id": None,
                "execution_claimed_at": None,
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
                "execution_owner_id": None,
                "execution_claim_id": None,
                "execution_claimed_at": None,
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

    def update(self, session_id: str, body: GoalUpdate) -> ChatGoal:
        goal = self.get(session_id)
        self._validate_token_budget(
            self.store.get(ChatSession, session_id), body.token_budget
        )
        if body.token_budget is not None and goal.metadata.get("usage_unknown") is True:
            raise ConflictError(
                "token usage was unavailable in an earlier harness turn"
            )
        if body.expected_revision != goal.revision:
            raise ConflictError(
                "goal changed on another device; review the latest goal before retrying"
            )
        if goal.status in {ChatGoalStatus.COMPLETED, ChatGoalStatus.CANCELLED}:
            raise ConflictError("completed or cancelled goals cannot be edited")
        if body.step_budget is not None and body.step_budget < goal.current_step:
            raise ConflictError("step budget cannot be lower than completed steps")
        if body.child_budget is not None and body.child_budget < goal.children_started:
            raise ConflictError(
                "child budget cannot be lower than children already started"
            )
        if (
            body.time_budget_seconds is not None
            and body.time_budget_seconds < goal.active_elapsed_seconds(utc_now())
        ):
            raise ConflictError("time budget cannot be lower than time already used")
        if (
            body.token_budget is not None
            and body.token_budget < goal.usage.total_tokens
        ):
            raise ConflictError("token budget cannot be lower than tokens already used")
        with self.store.transaction() as transaction:
            updated = transaction.update(
                ChatGoal,
                goal.id,
                {
                    "objective": body.objective,
                    "completion_criteria": body.completion_criteria,
                    "plan": body.plan,
                    "token_budget": body.token_budget,
                    "time_budget_seconds": body.time_budget_seconds,
                    "step_budget": body.step_budget,
                    "child_budget": body.child_budget,
                },
                expected_revision=goal.revision,
            )
            if goal.status != ChatGoalStatus.DRAFT:
                from .butler_oversight import record_goal_plan_change

                record_goal_plan_change(transaction, goal, updated)
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
        if parent.status != ChatGoalStatus.RUNNING or parent.child_budget is None:
            raise ConflictError(
                "child work requires a running goal with a child budget"
            )
        if (
            parent.children_started >= parent.child_budget
            or len(parent.child_session_ids) >= CHAT_GOAL_CHILD_LIMIT
        ):
            raise ConflictError("goal child budget is exhausted")
        parent_session = self.store.get(ChatSession, session_id)
        self._validate_token_budget(parent_session, body.token_budget)
        with self.store.transaction() as transaction:
            harness_session = None
            if parent_session.backend == ChatBackend.HARNESS:
                if self.harness_runtime is None:
                    raise ConflictError("harness goals are unavailable in this Core")
                source = self.store.get(
                    HarnessSession, parent_session.harness_session_id or ""
                )
                harness_session = self.harness_runtime._fork_session(
                    source, reason="goal child", transaction=transaction
                )
            child_session = ChatSession(
                id=str(uuid4()),
                engagement_id=parent_session.engagement_id,
                title=f"{parent.objective[:80]} (child)",
                backend=parent_session.backend,
                provider_profile_id=parent_session.provider_profile_id,
                harness_profile_id=parent_session.harness_profile_id,
                harness_session_id=harness_session.id if harness_session else None,
                model=parent_session.model,
                parent_session_id=parent_session.id,
                metadata={
                    "delegated_from_session_id": parent_session.id,
                    "delegated_from_goal_id": parent.id,
                    "workspace_is_shared": parent_session.backend
                    == ChatBackend.HARNESS,
                    "isolated_child_workspace": parent_session.backend
                    == ChatBackend.PROVIDER,
                    **(
                        {
                            "context_management": "runtime_managed",
                            "initial_title_state": "goal",
                        }
                        if harness_session
                        else {}
                    ),
                },
            )
            skill_entries, skill_parts = split_skill_snapshots(
                resolve_skill_snapshots(
                    self.store, parent.skill_snapshots, session_id=parent.session_id
                ),
                engagement_id=parent.engagement_id,
                session_id=child_session.id,
            )
            child = ChatGoal(
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
                skill_snapshots=skill_entries,
                status=ChatGoalStatus.DRAFT,
            )
            transaction.add(child_session)
            stage_snapshot_parts(transaction, skill_parts)
            transaction.add(child)
            transaction.update(
                ChatGoal,
                parent.id,
                {
                    "children_started": parent.children_started + 1,
                    "child_session_ids": [*parent.child_session_ids, child_session.id],
                },
                expected_revision=parent.revision,
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
        # Every usage charge rewrites the goal, so the instructions are stored
        # beside it and the goal keeps each skill's summary.
        entries, parts = split_skill_snapshots(
            (item.model_dump(mode="json") for item in snapshots),
            engagement_id=goal.engagement_id,
            session_id=goal.session_id,
        )
        with self.store.transaction() as transaction:
            stage_snapshot_parts(transaction, parts)
            return transaction.update(
                ChatGoal,
                goal.id,
                {"skill_snapshots": entries},
                expected_revision=goal.revision,
            )

    def skill_snapshots(self, goal: ChatGoal) -> list[SkillSnapshot]:
        """The goal's attached skills in full, however they were stored."""

        return [
            SkillSnapshot.model_validate(item)
            for item in resolve_skill_snapshots(
                self.store, goal.skill_snapshots, session_id=goal.session_id
            )
        ]


def goals_router(
    store: NebulaStore,
    *,
    goal_dispatcher: Callable[[str, str], Awaitable[str | None]] | None = None,
    harness_runtime: Any | None = None,
    skill_snapshot_resolver: Callable[
        [str, list[SkillSelection], list[SkillSnapshot]], list[SkillSnapshot]
    ]
    | None = None,
) -> APIRouter:
    router = APIRouter(tags=["chat"])
    service = ChatGoalService(store, harness_runtime)

    @router.post(
        "/chat/goal-conversations",
        response_model=GoalConversationCreated,
        status_code=201,
    )
    def create_goal_conversation(
        body: GoalConversationCreate,
    ) -> GoalConversationCreated:
        return service.create_conversation(body)

    @router.post(
        "/chat/sessions/{session_id}/goal/handoff",
        response_model=GoalHandoffCreated,
        status_code=201,
    )
    def handoff_goal(session_id: str, body: GoalHandoffRequest) -> GoalHandoffCreated:
        return service.handoff_to_provider(session_id, body)

    @router.get("/chat/sessions/{session_id}/goal", response_model=ChatGoal)
    def get_goal(session_id: str) -> ChatGoal:
        return service.read(session_id)

    @router.post("/chat/sessions/{session_id}/goal", response_model=ChatGoal)
    def create_goal(session_id: str, body: GoalCreate) -> ChatGoal:
        return service.create(session_id, body)

    @router.patch("/chat/sessions/{session_id}/goal", response_model=ChatGoal)
    def update_goal(session_id: str, body: GoalUpdate) -> ChatGoal:
        return service.update(session_id, body)

    @router.post("/chat/sessions/{session_id}/goal/actions", response_model=ChatGoal)
    async def write_goal(session_id: str, body: GoalWrite) -> ChatGoal:
        updated = service.write(session_id, body)
        if goal_dispatcher is not None and body.action in {"start", "resume"}:
            instruction = (
                "Begin work on the active conversation goal now. Review the goal "
                "objective and completion criteria provided by Core, then make "
                "concrete progress without waiting for another operator message."
                if body.action == "start"
                else "Resume work on the active conversation goal now. Review the "
                "latest durable progress and continue without waiting for another "
                "operator message."
            )
            await goal_dispatcher(session_id, instruction)
            return service.read(session_id)
        return updated

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
            existing = service.skill_snapshots(goal)
            snapshots = skill_snapshot_resolver(session_id, body.skills, existing)
        except (OSError, ValueError) as error:
            raise HTTPException(422, str(error)) from error
        return service.replace_skills(
            session_id,
            expected_revision=body.expected_revision,
            snapshots=snapshots,
        )

    return router
