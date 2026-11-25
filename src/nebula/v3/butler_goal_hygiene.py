"""Deterministic goal hygiene for Butler's supervised projects.

Replaces an external supervisor script with Core code that makes no model
decisions. One tick, for each non-terminal goal in the policy's hygiene
projects:

1. Budgets: when ``hygiene_unlimited_budgets`` is on, clears the token, time
   and step budgets (the child budget is kept).
2. Resume: resumes a paused or blocked goal only when Core recorded a
   transient cause (a spent budget that no longer binds, a provider or stream
   failure, a Core restart). A goal awaiting closure review, paused by the
   operator, held by a project hold, stopped by the operator, or blocked on
   anything that is not clearly transient stays as it is. Each goal is resumed
   at most ``RESUME_LIMIT`` times per ``RESUME_WINDOW``.
3. Work: a running top-level goal must have a Work item assigned to its chat
   with a stage; a missing one is created, a stageless one gets the default.

Writes go through ``ControlAssistantWork`` (the same Core API routes Butler's
``update_goal``, ``goal_action``, ``create_work_item`` and
``update_work_item`` tools use), so a resume also dispatches the goal's next
turn. Every action, taken or refused, writes one supervisor audit record.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Callable

from .butler_supervisor_policy import SupervisorPolicy, load_policy, write_audit
from .domain import (
    GOAL_COMPLETION_REVIEW_REASON,
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    ChatTurnStatus,
    RiskClass,
    WorkItem,
    utc_now,
)
from .model_refusal import PAUSE_PREFIX as MODEL_REFUSAL_PAUSE_PREFIX
from .project_hold_gate import HOLD_MARKER, active_hold
from .recovery_markers import OPERATOR_RECOVERY_PAUSE
from .storage import NebulaStore, NotFoundError, revise_latest
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


TOOL_NAME = "nebula.supervisor.hygiene_tick"
AUDIT_TOOL = "goal_hygiene"

# Per-goal resume backoff, kept in the goal's own metadata. ChatGoalService's
# resume clears only its named marks, so this key survives a resume.
RESUME_HISTORY_KEY = "butler_hygiene_resumes"
RESUME_LIMIT = 3
RESUME_WINDOW = timedelta(hours=1)

WORK_TITLE_LIMIT = 200
WORK_REQUEST_PREFIX = "butler-hygiene-work:"

NON_TERMINAL = (
    ChatGoalStatus.DRAFT,
    ChatGoalStatus.RUNNING,
    ChatGoalStatus.PAUSED,
    ChatGoalStatus.BLOCKED,
)
BUDGET_FIELDS = ("token_budget", "time_budget_seconds", "step_budget")

# ---------------------------------------------------------------------------
# Cause classification. Core writes its own pause and block reasons as fixed
# text; these prefixes match them exactly (file:line in the comments).
# ---------------------------------------------------------------------------

# Spent budgets: resumable once the budget no longer binds.
_BUDGET_PREFIXES = (
    # chat.py:7016, harnesses.py:13704, harnesses.py:13890
    "Goal step budget is exhausted.",
    # chat.py:7021, harnesses.py:13906, harnesses.py:22430
    "Goal token budget is exhausted.",
    # chat.py:7026, harnesses.py:13716, harnesses.py:13898
    "Goal active-time budget is exhausted.",
    # chat.py:12024, chat.py:12205
    "Goal token budget cannot fit",
    # chat_subagents.py:389 (GOAL_BUDGET_SUBAGENT_NOTE)
    "Token budget exhausted by subagent work.",
    # harnesses.py:22428 (needs the token limit removed)
    "Harness token usage was unavailable.",
)

# Transient provider, stream and restart causes Core records itself.
_TRANSIENT_PREFIXES = (
    # chat.py:6465 - provider turn failed after its one automatic retry
    "Response failed before the goal finished.",
    # chat.py:12092 - the model returned no (empty) answer repeatedly (BLOCKED)
    "The model returned no answer",
    # chat.py:3527, harnesses.py:9993, harnesses.py:10059 - Core restart
    "Core restarted while this goal had an active worker.",
    "Core restarted during a harness goal turn.",
    "Core restarted before the harness goal turn started.",
)
# chat.py:4902-4915, chat.py:6366, chat.py:16643 - "<cause> before this
# response completed." for Core stop/restart/runtime loss. A turn that still
# needs recovery makes ChatGoalService refuse the resume, so this stays safe.
_INTERRUPTED_MARK = " before this response completed."
# chat.py:6029, harnesses.py:13767 - "The goal's latest response ended
# <status> and nothing is running for it."
_LATEST_ENDED = re.compile(r"^The goal's latest response ended (\w+) ")
# harnesses.py:13690 - harness turn "stopped or failed"; the latest turn's
# status tells an operator stop (cancelled) from a failure.
_HARNESS_STOPPED_OR_FAILED = "The harness response stopped or failed."
# chat.py:6643 (via chat.py:2480), harnesses.py:14006 - the cause follows.
_CONTINUATION_FAILED = "Automatic goal continuation could not start"

# Never resumed by hygiene, whatever else the text says.
_NEVER_PREFIXES = (
    # chat.py:6460, chat.py:16926, chat.py:17040
    "Response stopped by the operator.",
    # chat.py:531
    "Required completion hook",
    # chat_goals.py:1675 - a child follows its parent
    "Parent goal paused; child work is paused.",
    # harnesses.py:13930 - needs the operator to look at the replies
    "Three automatic goal responses had no recorded tool activity.",
    # runtime_orphan_recovery.py:745 - an effect is unconfirmed
    "Project runtime stopped after a response ended while",
    # chat_goals.py:1038
    "Paused while project recovery is held.",
    # domain.py:3663
    GOAL_COMPLETION_REVIEW_REASON,
    # model_refusal.py - a model refusal waits for the operator's review
    MODEL_REFUSAL_PAUSE_PREFIX,
)

# Free-text fallback for BLOCKED goals (an agent's goal.block reason) and for
# the cause after "Automatic goal continuation could not start".
_TRANSIENT_WORDS = re.compile(
    r"chat_stream_failed|response failed|\bempty\b|\bprovider\b|\bstream|"
    r"transient|interrupted|overload|rate.?limit|timed? ?out|timeout|"
    r"connection (reset|refused|error|closed)|\b(429|500|502|503|504|529)\b",
    re.IGNORECASE,
)
_OPERATOR_WORDS = re.compile(
    r"operator|decision|decide|approv|human|manual|credential|permission|"
    r"sign.?in|log.?in|account|physical|consent|authori[sz]|clarif|"
    r"which (option|approach)|confirm|quota|billing|unsafe|refus",
    re.IGNORECASE,
)


def classify_reason(
    goal: ChatGoal, latest_turn_status: ChatTurnStatus | None = None
) -> tuple[str, str]:
    """Return (kind, label). kind is budget, transient, or never."""

    reason = (goal.blocked_reason or "").strip()
    if not reason:
        # Core always names its own causes; a bare pause is the operator's.
        return "never", "no_reason_operator_pause"
    for prefix in _NEVER_PREFIXES:
        if reason.startswith(prefix):
            return "never", f"core_reason_not_transient:{prefix[:60]}"
    for prefix in _BUDGET_PREFIXES:
        if reason.startswith(prefix):
            return "budget", "budget_exhausted"
    for prefix in _TRANSIENT_PREFIXES:
        if reason.startswith(prefix):
            return "transient", "core_transient"
    if reason.startswith(_HARNESS_STOPPED_OR_FAILED):
        if latest_turn_status in {ChatTurnStatus.FAILED, ChatTurnStatus.INTERRUPTED}:
            return "transient", "harness_turn_failed"
        return "never", "harness_turn_stopped_or_unknown"
    ended = _LATEST_ENDED.match(reason)
    if ended:
        if ended.group(1) in {"failed", "interrupted"}:
            return "transient", f"latest_turn_{ended.group(1)}"
        return "never", f"latest_turn_{ended.group(1)}"
    if _INTERRUPTED_MARK in reason and reason.startswith(
        ("Core stopped", "Core restarted", "Project runtime", "Runtime")
    ):
        if "goal hold is preserved" in reason:
            return "never", "hold_preserved"
        return "transient", "core_interrupted"
    if reason.startswith(_CONTINUATION_FAILED):
        cause = reason[len(_CONTINUATION_FAILED) :]
        if _TRANSIENT_WORDS.search(cause) and not _OPERATOR_WORDS.search(cause):
            return "transient", "continuation_transient"
        return "never", "continuation_not_transient"
    if goal.status == ChatGoalStatus.BLOCKED:
        if _OPERATOR_WORDS.search(reason):
            return "never", "operator_decision"
        if _TRANSIENT_WORDS.search(reason):
            return "transient", "blocked_transient_text"
    return "never", "not_transient"


def _budgets_bind(goal: ChatGoal) -> bool:
    if goal.step_budget is not None and goal.current_step >= goal.step_budget:
        return True
    if goal.token_budget is not None and (
        goal.usage.total_tokens >= goal.token_budget
        or goal.metadata.get("usage_unknown") is True
    ):
        return True
    return (
        goal.time_budget_seconds is not None
        and goal.elapsed_seconds >= goal.time_budget_seconds
    )


def _recent_resumes(goal: ChatGoal, now: datetime) -> list[str]:
    raw = goal.metadata.get(RESUME_HISTORY_KEY)
    if not isinstance(raw, list):
        return []
    kept: list[str] = []
    for item in raw:
        try:
            at = datetime.fromisoformat(str(item))
        except (
            ValueError
        ):  # diagnostic-expected: an unreadable backoff entry is dropped
            continue
        if now - at < RESUME_WINDOW:
            kept.append(at.isoformat())
    return kept


def _work_title(goal: ChatGoal) -> str:
    for line in goal.objective.splitlines():
        text = " ".join(line.split())
        if text:
            return text[:WORK_TITLE_LIMIT]
    return f"Goal {goal.id}"[:WORK_TITLE_LIMIT]


async def _latest_turn_status(
    store: NebulaStore, session_id: str
) -> ChatTurnStatus | None:
    turns = await store.find_entities(
        ChatTurn, {}, session_id=session_id, newest_first=True, limit=1
    )
    return turns[0].status if turns else None


class _Tick:
    def __init__(
        self,
        store: NebulaStore,
        work: Any,
        policy: SupervisorPolicy,
        *,
        dry_run: bool,
        now: datetime,
    ) -> None:
        self.store = store
        self.work = work
        self.policy = policy
        self.dry_run = dry_run
        self.now = now
        self.actions: list[dict[str, Any]] = []

    def record(
        self,
        project_id: str,
        goal: ChatGoal,
        action: str,
        outcome: str,
        reason: str,
        **detail: Any,
    ) -> dict[str, Any]:
        entry = {
            "project_id": project_id,
            "goal_id": goal.id,
            "session_id": goal.session_id,
            "action": action,
            "outcome": outcome,
            "reason": reason,
            **detail,
        }
        self.actions.append(entry)
        if not self.dry_run:
            write_audit(
                self.policy,
                {
                    "tool": AUDIT_TOOL,
                    "allowed": outcome in {"applied", "skipped"},
                    **{key: value for key, value in entry.items() if key != "reason"},
                    "detail": reason,
                },
            )
        return entry

    async def _fresh(self, goal: ChatGoal) -> ChatGoal:
        return await self.store.get(ChatGoal, goal.id)

    # 1. Budgets ------------------------------------------------------------

    async def null_budgets(self, project_id: str, goal: ChatGoal) -> ChatGoal:
        if not self.policy.hygiene_unlimited_budgets:
            return goal
        if all(getattr(goal, name) is None for name in BUDGET_FIELDS):
            return goal
        before = {name: getattr(goal, name) for name in BUDGET_FIELDS}
        if self.dry_run:
            self.record(
                project_id,
                goal,
                "null_budgets",
                "planned",
                "unlimited policy",
                before=before,
            )
            return goal.model_copy(update=dict.fromkeys(BUDGET_FIELDS))
        last: Exception | None = None
        for _attempt in range(2):
            try:
                await self.work.update_goal(
                    project_id,
                    {
                        "session_id": goal.session_id,
                        "expected_revision": goal.revision,
                        **dict.fromkeys(BUDGET_FIELDS),
                    },
                )
                updated = await self._fresh(goal)
                self.record(
                    project_id,
                    updated,
                    "null_budgets",
                    "applied",
                    "unlimited policy",
                    before=before,
                )
                return updated
            except Exception as exc:  # diagnostic-expected: a moved revision retries once; the last refusal is recorded
                last = exc
                current = await self._fresh(goal)
                if (
                    current.revision == goal.revision
                    or current.status not in NON_TERMINAL
                ):
                    break
                goal = current
        self.record(
            project_id, goal, "null_budgets", "failed", str(last)[:500], before=before
        )
        return await self._fresh(goal)

    # 2. Resume -------------------------------------------------------------

    async def maybe_resume(self, project_id: str, goal: ChatGoal) -> ChatGoal:
        if goal.status not in {ChatGoalStatus.PAUSED, ChatGoalStatus.BLOCKED}:
            return goal

        def skip(reason: str, **detail: Any) -> ChatGoal:
            self.record(
                project_id,
                goal,
                "resume",
                "skipped",
                reason,
                status=goal.status.value,
                **detail,
            )
            return goal

        if goal.awaiting_completion_review:
            return skip("awaiting_completion_review")
        if OPERATOR_RECOVERY_PAUSE in goal.metadata:
            return skip("operator_paused_during_recovery")
        if goal.metadata.get(HOLD_MARKER) is not None:
            return skip("paused_by_project_hold")
        if goal.metadata.get("handoff_target_session_id"):
            return skip("handed_off")
        hold = await active_hold(self.store, goal.engagement_id, goal.session_id)
        if hold is not None:
            return skip("project_held", hold_id=hold.id)
        latest = await _latest_turn_status(self.store, goal.session_id)
        kind, label = classify_reason(goal, latest)
        if kind == "never":
            return skip(label, blocked_reason=(goal.blocked_reason or "")[:300])
        if kind == "budget" and _budgets_bind(goal):
            return skip("budget_still_binds")
        recent = _recent_resumes(goal, self.now)
        if len(recent) >= RESUME_LIMIT:
            return skip("backoff", recent_resumes=recent)
        reason = f"Butler goal hygiene: transient cause ({label})."
        if self.dry_run:
            self.record(
                project_id, goal, "resume", "planned", label, status=goal.status.value
            )
            return goal.model_copy(update={"status": ChatGoalStatus.RUNNING})
        # Count the attempt before acting, so a resume that fails still backs off.
        now = self.now.isoformat()

        def mark(current: ChatGoal) -> dict[str, Any]:
            return {
                "metadata": {
                    **current.metadata,
                    RESUME_HISTORY_KEY: [*_recent_resumes(current, self.now), now],
                }
            }

        marked = await revise_latest(self.store, ChatGoal, goal.id, mark)
        try:
            await self.work.goal_action(
                project_id,
                {
                    "session_id": goal.session_id,
                    "expected_revision": marked.revision,
                    "action": "resume",
                    "reason": reason,
                },
            )
        except Exception as exc:  # diagnostic-expected: Core's refusal is recorded in the tick result and audit
            self.record(
                project_id, marked, "resume", "failed", str(exc)[:500], cause=label
            )
            return await self._fresh(goal)
        resumed = await self._fresh(goal)
        self.record(
            project_id,
            resumed,
            "resume",
            "applied",
            label,
            previous_status=goal.status.value,
            previous_reason=(goal.blocked_reason or "")[:300],
        )
        return resumed

    # 3. Work item ----------------------------------------------------------

    async def ensure_work_item(self, project_id: str, goal: ChatGoal) -> None:
        if goal.status != ChatGoalStatus.RUNNING or goal.parent_goal_id:
            return
        items = await self.store.find_entities(
            WorkItem,
            {"assignee_session_id": goal.session_id},
            engagement_id=project_id,
        )
        open_items = [item for item in items if item.status != "done"]
        stage = self.policy.hygiene_default_stage
        if open_items:
            staged = [item for item in open_items if item.stage]
            if staged:
                return
            item = min(open_items, key=lambda entry: (entry.created_at, entry.id))
            if self.dry_run:
                self.record(
                    project_id,
                    goal,
                    "work_stage",
                    "planned",
                    "assigned item has no stage",
                    item_id=item.id,
                    stage=stage,
                )
                return
            try:
                await self.work.update_work_item(
                    project_id,
                    {
                        "item_id": item.id,
                        "expected_revision": item.revision,
                        "stage": stage,
                    },
                )
            except (
                Exception
            ) as exc:  # diagnostic-expected: recorded in the tick result and audit
                self.record(
                    project_id,
                    goal,
                    "work_stage",
                    "failed",
                    str(exc)[:500],
                    item_id=item.id,
                )
                return
            self.record(
                project_id,
                goal,
                "work_stage",
                "applied",
                "assigned item had no stage",
                item_id=item.id,
                stage=stage,
            )
            return
        arguments = {
            "title": _work_title(goal),
            "status": "in_progress",
            "priority": "high",
            "assignee_session_id": goal.session_id,
            "stage": stage,
            "request_id": f"{WORK_REQUEST_PREFIX}{goal.id}"[:200],
        }
        if self.dry_run:
            self.record(
                project_id,
                goal,
                "work_item",
                "planned",
                "no assigned Work item",
                title=arguments["title"],
                stage=stage,
            )
            return
        try:
            created = await self.work.create_work_item(project_id, arguments)
        except (
            Exception
        ) as exc:  # diagnostic-expected: recorded in the tick result and audit
            self.record(project_id, goal, "work_item", "failed", str(exc)[:500])
            return
        created_item = created.get("item") if isinstance(created, dict) else None
        item_id = created_item.get("id") if isinstance(created_item, dict) else None
        self.record(
            project_id,
            goal,
            "work_item",
            "applied",
            "no assigned Work item",
            item_id=item_id,
            stage=stage,
        )


async def run_hygiene_tick(
    store: NebulaStore,
    work: Any,
    policy: SupervisorPolicy,
    *,
    project_ids: list[str] | tuple[str, ...] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one deterministic hygiene pass. ``work`` is ControlAssistantWork."""

    policy.require_enabled()
    configured = list(policy.hygiene_project_ids)
    if project_ids is None:
        selected = configured
    else:
        outside = [item for item in project_ids if item not in configured]
        if outside:
            raise ValueError(
                f"project_ids outside the hygiene policy: {sorted(outside)}"
            )
        selected = [item for item in configured if item in set(project_ids)]
    tick = _Tick(store, work, policy, dry_run=dry_run, now=utc_now())
    errors: list[dict[str, Any]] = []
    for project_id in selected:
        goals = [
            goal
            async for goal in store.iter_readable_entities(
                ChatGoal,
                {"status": [status.value for status in NON_TERMINAL]},
                engagement_id=project_id,
            )
        ]
        for goal in goals:
            try:
                goal = await tick.null_budgets(project_id, goal)
                goal = await tick.maybe_resume(project_id, goal)
                await tick.ensure_work_item(project_id, goal)
            except NotFoundError:
                # diagnostic-expected: the goal or its chat was removed mid-tick.
                continue
            except Exception as exc:  # diagnostic-expected: one goal never stops the tick; recorded in the result and audit
                errors.append(
                    {
                        "project_id": project_id,
                        "goal_id": goal.id,
                        "error": str(exc)[:500],
                    }
                )
                tick.record(project_id, goal, "tick", "failed", str(exc)[:500])
    counts: dict[str, int] = {}
    for entry in tick.actions:
        key = f"{entry['action']}:{entry['outcome']}"
        counts[key] = counts.get(key, 0) + 1
    return {
        "dry_run": dry_run,
        "project_ids": selected,
        "at": tick.now.isoformat(),
        "counts": counts,
        "actions": tick.actions,
        "errors": errors,
    }


_TICK_INPUT = {
    "type": "object",
    "properties": {
        "project_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 64,
            "description": "A subset of the policy's hygiene projects; all when omitted.",
        },
        "dry_run": {"type": "boolean", "default": False},
    },
    "additionalProperties": False,
}


class ButlerGoalHygiene:
    """Expose the deterministic hygiene tick as one Butler tool."""

    def __init__(
        self,
        store: NebulaStore,
        work: Any,
        policy_loader: Callable[[], SupervisorPolicy] = load_policy,
    ) -> None:
        self.store = store
        self.work = work
        self.policy_loader = policy_loader

    async def tick(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self.policy_loader()
        policy.require_enabled()
        project_ids = arguments.get("project_ids")
        return await run_hygiene_tick(
            self.store,
            self.work,
            policy,
            project_ids=list(project_ids) if project_ids is not None else None,
            dry_run=bool(arguments.get("dry_run", False)),
        )

    def actions(self) -> list[AnalysisTool]:
        return [
            AnalysisTool(
                ToolSpec(
                    name=TOOL_NAME,
                    description=(
                        "Run Core's deterministic goal hygiene now for the operator's "
                        "hygiene projects: clear token/time/step budgets when the policy "
                        "says so, resume goals paused or blocked only for transient "
                        "causes (spent budget, provider or stream failure, Core restart; "
                        "at most 3 resumes per goal per hour), and give every running "
                        "goal a staged Work item. Never resumes a goal awaiting closure "
                        "review, paused by the operator or a project hold, or blocked on "
                        "an operator decision. dry_run reports without changing anything."
                    ),
                    input_schema=_TICK_INPUT,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                self.tick,
            )
        ]
