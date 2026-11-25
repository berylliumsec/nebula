"""Find and recover goal turns that stopped making progress.

Goal turns have sat in ``waiting_callback`` for most of an hour with no
progress while nothing would ever wake them; stopping the turn and resuming
the goal fixed every case. Core's callback watchdog
(``ChatService.reconcile_stalled_callback_waits``) does not cover them: it
treats a wait as live while its command is inside its own deadline or a
waited-on subagent is still working, and it skips harness turns entirely.

A goal turn is *stuck* here when it is in one of ``STUCK_STATUSES`` and its
last progress is older than the threshold. Progress follows the session
state's definition (``session_state.session_state``): a provider turn's last
row write (it writes at every saved step, tool result or wait), and for a
harness turn the newest non-status harness event. The statuses:

* ``waiting_callback``: the reported failure mode.
* ``routing``: a provider turn writes its row at every step, so a routing
  turn with no write for the threshold has a hung model call or a lost
  in-process task; it fails the same way (nothing wakes it) and the same
  remedy works. A harness turn runs as ``routing`` while its harness is
  ``running``; a harness whose newest event is a started tool may be running
  a long command, so it is listed but not swept automatically.
* ``finalizing``: a provider turn saving its answer, normally seconds.

Not included: ``queued`` (capacity admission owns it), ``waiting_approval``
(waits for the operator by design), ``interrupted`` (restart recovery owns
it) and terminal turns.

Recovery is one action: re-read the turn and refuse unless it is still the
same stuck turn with an unchanged ``last_progress_at``; stop it through
Core's own stop path (``ChatService.stop_provider_turn`` or
``HarnessRuntimeService.cancel_turn``, as Butler's ``stop_chat`` does), which
pauses the goal; then resume the goal through ``ControlAssistantWork.goal_action``
(the Core route that also dispatches its next turn). Each goal is recovered
at most ``RECOVERY_LIMIT`` times per ``RECOVERY_WINDOW``, counted in its
metadata before acting.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Iterable, Mapping

from sqlalchemy import select

from .butler_supervisor_policy import SupervisorPolicy, load_policy, write_audit
from .database import OperationEventRow
from .domain import (
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    ChatTurnStatus,
    HarnessTurn,
    HarnessTurnStatus,
    RiskClass,
    utc_now,
)
from .project_hold_gate import is_held
from .storage import NebulaStore, NotFoundError, revise_latest
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec

TOOL_NAME = "nebula.supervisor.recover_stuck_turn"
AUDIT_TOOL = "stuck_turn_recovery"

THRESHOLD_ENV = "NEBULA_BUTLER_STUCK_TURN_MINUTES"
AUTO_ENV = "NEBULA_BUTLER_STUCK_TURN_AUTO"
DEFAULT_MINUTES = 12
MIN_MINUTES = 10
MAX_MINUTES = 60

RECOVERY_HISTORY_KEY = "butler_stuck_turn_recoveries"
RECOVERY_LIMIT = 3
RECOVERY_WINDOW = timedelta(hours=1)

STUCK_STATUSES = (
    ChatTurnStatus.ROUTING,
    ChatTurnStatus.WAITING_CALLBACK,
    ChatTurnStatus.FINALIZING,
)
# A restart for a new note may also stop a turn still waiting to start.
RESTARTABLE_STATUSES = (*STUCK_STATUSES, ChatTurnStatus.QUEUED)
_TERMINAL_TURN = {
    ChatTurnStatus.COMPLETE,
    ChatTurnStatus.FAILED,
    ChatTurnStatus.CANCELLED,
}
_TOOL_STARTED = "harness.tool_started"
SETTLE_SECONDS = 10.0
_TRUE = {"1", "true", "yes", "on"}

Sleep = Callable[[float], Awaitable[Any]]


def threshold_minutes(environ: Mapping[str, str] | None = None) -> int:
    raw = (environ if environ is not None else os.environ).get(THRESHOLD_ENV, "")
    try:
        value = int(raw.strip()) if raw.strip() else DEFAULT_MINUTES
    except ValueError:  # diagnostic-expected: a malformed setting keeps the default
        value = DEFAULT_MINUTES
    return max(MIN_MINUTES, min(MAX_MINUTES, value))


def auto_enabled(environ: Mapping[str, str] | None = None) -> bool:
    raw = (environ if environ is not None else os.environ).get(AUTO_ENV, "")
    return raw.strip().casefold() in _TRUE


def _aware(value: datetime) -> datetime:
    # SQLite returns stored UTC times without their zone.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class TurnProgress:
    last_progress_at: datetime
    source: str
    harness_status: str | None = None
    tool_in_flight: bool = False


async def turn_progress(store: NebulaStore, turn: ChatTurn) -> TurnProgress:
    """When the turn last made durable progress, as the session state shows it."""

    progressed = _aware(turn.updated_at)
    if turn.backend != ChatBackend.HARNESS or not turn.harness_turn_id:
        return TurnProgress(progressed, "turn_write")
    try:
        harness = await store.get(HarnessTurn, turn.harness_turn_id)
    except NotFoundError:  # diagnostic-expected: a harness record removed with its runtime leaves the chat turn's own write
        return TurnProgress(progressed, "turn_write")
    async with store.database.session() as database:
        row = (
            await database.execute(
                select(OperationEventRow.occurred_at, OperationEventRow.event_type)
                .where(
                    OperationEventRow.operation_id == harness.id,
                    OperationEventRow.operation_kind == "harness_turn",
                    OperationEventRow.event_type != "harness.status",
                )
                .order_by(OperationEventRow.sequence.desc())
                .limit(1)
            )
        ).first()
    candidates = [progressed]
    if harness.started_at is not None:
        candidates.append(_aware(harness.started_at))
    event_type = None
    if row is not None:
        candidates.append(_aware(row[0]))
        event_type = row[1]
    return TurnProgress(
        max(candidates),
        "harness_event" if row is not None else "turn_write",
        harness_status=harness.status.value,
        tool_in_flight=event_type == _TOOL_STARTED,
    )


@dataclass(frozen=True)
class StuckTurn:
    project_id: str
    session_id: str
    goal_id: str
    turn_id: str
    backend: str
    status: str
    last_progress_at: str
    progress_source: str
    stuck_seconds: int
    threshold_minutes: int
    auto_eligible: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _classify(
    store: NebulaStore,
    turn: ChatTurn,
    *,
    now: datetime,
    threshold: timedelta,
) -> StuckTurn | None:
    if not turn.goal_id or turn.status not in STUCK_STATUSES:
        return None
    try:
        goal = await store.get(ChatGoal, turn.goal_id)
    except NotFoundError:  # diagnostic-expected: a deleted goal has no turn to recover
        return None
    if goal.status != ChatGoalStatus.RUNNING or goal.session_id != turn.session_id:
        return None
    progress = await turn_progress(store, turn)
    age = now - progress.last_progress_at
    if age < threshold:
        return None
    if (
        progress.harness_status is not None
        and progress.harness_status != HarnessTurnStatus.RUNNING.value
    ):
        # A queued harness turn waits for capacity; one waiting for approval
        # waits for the operator.
        return None
    if await is_held(store, turn.engagement_id, turn.session_id):
        return None
    note = (
        "harness tool started and has not reported since; may be a long command"
        if progress.tool_in_flight
        else ""
    )
    return StuckTurn(
        project_id=turn.engagement_id,
        session_id=turn.session_id,
        goal_id=goal.id,
        turn_id=turn.id,
        backend=turn.backend.value,
        status=turn.status.value,
        last_progress_at=progress.last_progress_at.isoformat(),
        progress_source=progress.source,
        stuck_seconds=int(age.total_seconds()),
        threshold_minutes=int(threshold.total_seconds() // 60),
        auto_eligible=not progress.tool_in_flight,
        note=note,
    )


async def find_stuck_turns(
    store: NebulaStore,
    project_ids: Iterable[str] | None = None,
    now: datetime | None = None,
    threshold: timedelta | None = None,
) -> list[StuckTurn]:
    """Goal turns in a stuck status with no progress for the threshold,
    oldest progress first. ``project_ids`` None means every project."""

    now = now or utc_now()
    threshold = threshold or timedelta(minutes=threshold_minutes())
    filters = {"status": [status.value for status in STUCK_STATUSES]}
    scopes: list[str | None] = (
        [None] if project_ids is None else list(dict.fromkeys(project_ids))
    )
    found: list[StuckTurn] = []
    for project_id in scopes:
        async for turn in store.iter_readable_entities(
            ChatTurn, filters, engagement_id=project_id
        ):
            stuck = await _classify(store, turn, now=now, threshold=threshold)
            if stuck is not None:
                found.append(stuck)
    found.sort(key=lambda item: (item.last_progress_at, item.turn_id))
    return found


def _recent(goal: ChatGoal, now: datetime) -> list[str]:
    raw = goal.metadata.get(RECOVERY_HISTORY_KEY)
    if not isinstance(raw, list):
        return []
    kept: list[str] = []
    for item in raw:
        try:
            at = _aware(datetime.fromisoformat(str(item)))
        except (
            ValueError
        ):  # diagnostic-expected: an unreadable backoff entry is dropped
            continue
        if now - at < RECOVERY_WINDOW:
            kept.append(at.isoformat())
    return kept


def _audit(policy: SupervisorPolicy | None, receipt: Mapping[str, Any]) -> None:
    if policy is None:
        return
    write_audit(
        policy,
        {
            "tool": AUDIT_TOOL,
            "allowed": receipt["outcome"] == "recovered",
            **{key: value for key, value in receipt.items() if key != "detail"},
            "detail": str(receipt.get("detail", "")),
        },
    )


async def recover_turn(
    store: NebulaStore,
    work: Any,
    *,
    session_id: str,
    turn_id: str,
    expected_last_progress_at: str | None = None,
    require_stuck: bool = True,
    threshold: timedelta | None = None,
    reason: str = "Butler recovered a goal turn that stopped making progress.",
    policy: SupervisorPolicy | None = None,
    now: datetime | None = None,
    settle_seconds: float = SETTLE_SECONDS,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, Any]:
    """Stop one goal turn and resume its goal, as one guarded action.

    ``work`` is ``ControlAssistantWork`` (``chat``, ``harness`` and
    ``goal_action``). ``require_stuck`` False is the note restart: the turn
    need not be stuck, but the identity guard and backoff still apply.
    Returns a receipt; refusals are receipts, not exceptions.
    """

    now = now or utc_now()
    threshold = threshold or timedelta(minutes=threshold_minutes())
    receipt: dict[str, Any] = {
        "session_id": session_id,
        "turn_id": turn_id,
        "require_stuck": require_stuck,
        "at": now.isoformat(),
    }

    def done(outcome: str, detail: str, **extra: Any) -> dict[str, Any]:
        receipt.update(outcome=outcome, detail=detail, **extra)
        _audit(policy, receipt)
        return receipt

    try:
        turn = await store.get(ChatTurn, turn_id)
    except (
        NotFoundError
    ):  # diagnostic-expected: a missing turn is refused in the receipt
        return done("refused", "turn_not_found")
    receipt["project_id"] = turn.engagement_id
    if turn.session_id != session_id:
        return done("refused", "turn_not_in_session")
    if not turn.goal_id:
        return done("refused", "not_a_goal_turn")
    receipt["goal_id"] = turn.goal_id
    allowed = STUCK_STATUSES if require_stuck else RESTARTABLE_STATUSES
    if turn.status not in allowed:
        return done("refused", "status_not_recoverable", status=turn.status.value)
    goal = await store.get(ChatGoal, turn.goal_id)
    if goal.status != ChatGoalStatus.RUNNING or goal.session_id != session_id:
        return done("refused", "goal_not_running", goal_status=goal.status.value)
    progress = await turn_progress(store, turn)
    observed = progress.last_progress_at.isoformat()
    receipt.update(
        previous_status=turn.status.value,
        backend=turn.backend.value,
        last_progress_at=observed,
        stuck_seconds=int((now - progress.last_progress_at).total_seconds()),
    )
    if expected_last_progress_at is not None and not _same_time(
        expected_last_progress_at, progress.last_progress_at
    ):
        return done("refused", "progress_changed")
    if require_stuck and now - progress.last_progress_at < threshold:
        return done("refused", "not_stuck")
    if await is_held(store, turn.engagement_id, session_id):
        return done("refused", "project_held")
    recent = _recent(goal, now)
    if len(recent) >= RECOVERY_LIMIT:
        return done("refused", "backoff", recent_recoveries=recent)

    # Count the attempt before acting, so a failed recovery still backs off.
    def mark(current: ChatGoal) -> dict[str, Any]:
        return {
            "metadata": {
                **current.metadata,
                RECOVERY_HISTORY_KEY: [*_recent(current, now), now.isoformat()],
            }
        }

    await revise_latest(store, ChatGoal, goal.id, mark)

    # Identity guard, immediately before the stop: the same turn, still the
    # session's pending turn, in the same status with unchanged progress.
    pending = await work.chat.pending_turn(session_id)
    latest = await store.get(ChatTurn, turn_id)
    latest_progress = await turn_progress(store, latest)
    if (
        latest.status != turn.status
        or pending is None
        or pending.id != turn_id
        or latest_progress.last_progress_at != progress.last_progress_at
    ):
        return done("refused", "turn_changed", observed_status=latest.status.value)

    try:
        if latest.backend == ChatBackend.HARNESS and latest.harness_turn_id:
            await work.harness.cancel_turn(latest.harness_turn_id, reason=reason)
        else:
            await work.chat.stop_provider_turn(latest.id)
    except Exception as exc:  # diagnostic-expected: Core's refusal to stop is returned in the receipt and audit
        return done("failed", f"stop_failed: {str(exc)[:400]}")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + settle_seconds
    while True:
        stopped = await store.get(ChatTurn, turn_id)
        goal = await store.get(ChatGoal, goal.id)
        if stopped.status in _TERMINAL_TURN and goal.status != ChatGoalStatus.RUNNING:
            break
        if stopped.status in _TERMINAL_TURN and goal.status == ChatGoalStatus.RUNNING:
            # The goal continued by itself; nothing to resume.
            nxt = await work.chat.pending_turn(session_id)
            return done(
                "recovered",
                "goal_continued_after_stop",
                cancelled_status=stopped.status.value,
                resumed=False,
                goal_status=goal.status.value,
                new_turn_id=nxt.id if nxt is not None else None,
            )
        if loop.time() >= deadline:
            return done(
                "failed",
                "stop_not_settled",
                cancelled_status=stopped.status.value,
                goal_status=goal.status.value,
                resumed=False,
            )
        await sleep(0.25)
    if goal.status not in {ChatGoalStatus.PAUSED, ChatGoalStatus.BLOCKED}:
        return done(
            "failed",
            "goal_not_resumable_after_stop",
            cancelled_status=stopped.status.value,
            goal_status=goal.status.value,
            resumed=False,
        )
    last_error = ""
    for _attempt in range(3):
        try:
            await work.goal_action(
                goal.engagement_id,
                {
                    "session_id": session_id,
                    "expected_revision": goal.revision,
                    "action": "resume",
                    "reason": reason,
                },
            )
            break
        except Exception as exc:  # diagnostic-expected: a moved revision retries; the last refusal is in the receipt
            last_error = str(exc)[:400]
            current = await store.get(ChatGoal, goal.id)
            if current.revision == goal.revision or current.status not in {
                ChatGoalStatus.PAUSED,
                ChatGoalStatus.BLOCKED,
            }:
                goal = current
                break
            goal = current
    resumed = await store.get(ChatGoal, goal.id)
    if resumed.status != ChatGoalStatus.RUNNING:
        return done(
            "failed",
            f"resume_failed: {last_error}" if last_error else "resume_failed",
            cancelled_status=stopped.status.value,
            goal_status=resumed.status.value,
            resumed=False,
        )
    nxt = await work.chat.pending_turn(session_id)
    return done(
        "recovered",
        "stopped_and_resumed",
        cancelled_status=stopped.status.value,
        resumed=True,
        goal_status=resumed.status.value,
        new_turn_id=nxt.id if nxt is not None else None,
    )


def _same_time(expected: str, actual: datetime) -> bool:
    try:
        return _aware(datetime.fromisoformat(expected)) == actual
    except ValueError:  # diagnostic-expected: a malformed expectation never matches
        return False


async def sweep_stuck_turns(
    store: NebulaStore,
    work: Any,
    *,
    project_ids: Iterable[str] | None = None,
    policy: SupervisorPolicy | None = None,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, Any]:
    """One deterministic pass for the periodic tick; off unless
    ``NEBULA_BUTLER_STUCK_TURN_AUTO=1``.

    Scope: ``project_ids``, else the policy's hygiene projects, else every
    project. Also retires released supervisor notes in the same scope.
    """

    from .butler_note_release import sweep_note_releases

    if not auto_enabled(environ):
        return {"enabled": False, "recoveries": [], "skipped": []}
    if policy is None:
        policy = load_policy(dict(environ) if environ is not None else None)
    now = now or utc_now()
    threshold = timedelta(minutes=threshold_minutes(environ))
    scope = (
        list(project_ids)
        if project_ids is not None
        else list(policy.hygiene_project_ids) or None
    )
    retired = (
        await sweep_note_releases(store, scope, now=now) if scope is not None else []
    )
    recoveries: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for stuck in await find_stuck_turns(store, scope, now=now, threshold=threshold):
        if not stuck.auto_eligible:
            skipped.append({**stuck.as_dict(), "skip": "tool_in_flight"})
            continue
        recoveries.append(
            await recover_turn(
                store,
                work,
                session_id=stuck.session_id,
                turn_id=stuck.turn_id,
                expected_last_progress_at=stuck.last_progress_at,
                threshold=threshold,
                reason=(
                    "Butler stuck-turn sweep: no progress for "
                    f"{stuck.stuck_seconds // 60} minutes in {stuck.status}."
                ),
                policy=policy,
                now=now,
                sleep=sleep,
            )
        )
    return {
        "enabled": True,
        "at": now.isoformat(),
        "threshold_minutes": int(threshold.total_seconds() // 60),
        "recoveries": recoveries,
        "skipped": skipped,
        "retired_notes": retired,
    }


_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["list", "recover"]},
        "project_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 64,
        },
        "session_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "turn_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "expected_last_progress_at": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "last_progress_at exactly as the list returned it.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


class ButlerStuckTurns:
    """Butler's list-and-recover tool for stuck goal turns."""

    def __init__(
        self,
        store: NebulaStore,
        work: Any,
        project_id: str,
        policy_loader: Callable[[], SupervisorPolicy] = load_policy,
    ) -> None:
        self.store = store
        self.work = work
        self.project_id = project_id
        self.policy_loader = policy_loader

    def _scope(self, policy: SupervisorPolicy, requested: Any) -> list[str]:
        allowed = [self.project_id, *policy.hygiene_project_ids]
        if requested is None:
            return list(dict.fromkeys(allowed))
        outside = [item for item in requested if item not in allowed]
        if outside:
            raise PermissionError(
                f"project_ids outside this project and the hygiene projects: {sorted(outside)}"
            )
        return list(dict.fromkeys(requested))

    async def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self.policy_loader()
        policy.require_enabled()
        scope = self._scope(policy, arguments.get("project_ids"))
        threshold = timedelta(minutes=threshold_minutes())
        if arguments["action"] == "list":
            stuck = await find_stuck_turns(self.store, scope, threshold=threshold)
            return {
                "threshold_minutes": int(threshold.total_seconds() // 60),
                "auto_sweep": auto_enabled(),
                "stuck_turns": [item.as_dict() for item in stuck],
            }
        missing = [
            key
            for key in ("session_id", "turn_id", "expected_last_progress_at")
            if not arguments.get(key)
        ]
        if missing:
            raise ValueError(f"recover needs {missing}; list the stuck turns first")
        turn = await self.store.get(ChatTurn, arguments["turn_id"])
        if turn.engagement_id not in scope:
            raise PermissionError("the turn is outside the selected projects")
        return await recover_turn(
            self.store,
            self.work,
            session_id=arguments["session_id"],
            turn_id=arguments["turn_id"],
            expected_last_progress_at=arguments["expected_last_progress_at"],
            threshold=threshold,
            policy=policy,
        )

    def actions(self) -> list[AnalysisTool]:
        return [
            AnalysisTool(
                ToolSpec(
                    name=TOOL_NAME,
                    description=(
                        "List goal turns stuck in routing, waiting_callback or "
                        "finalizing with no progress for the threshold "
                        f"({THRESHOLD_ENV}, default {DEFAULT_MINUTES} minutes), or "
                        "recover one: Core re-checks that it is the same stuck turn "
                        "with unchanged last_progress_at, stops it and resumes its "
                        "goal as one action (at most 3 recoveries per goal per hour). "
                        "action=list first; pass its session_id, turn_id and "
                        "last_progress_at to action=recover. Report the receipt's "
                        "outcome; refused and failed receipts changed nothing or "
                        "only stopped the turn."
                    ),
                    input_schema=_INPUT,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                self.run,
            )
        ]
