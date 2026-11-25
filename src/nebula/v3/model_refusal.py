"""Model refusals stay visible and paused for the operator.

A goal turn that ends because the model refused (a Claude CLI/API refusal
error, a provider ``refusal`` stop reason, a content filter or guardrail) is
not a transient failure, a usage limit or a capacity stop. Asking again with
the same context gets the same answer, and asking a different runtime would
route around the refusal. So Core does none of that here:

* the turn failure is classified ``model_refusal`` (``diagnostic_guidance``);
* the goal pauses with ``Model refusal (<category>) on <model> in turn
  <turn id>: needs operator review``;
* a ``goal.model_refusal`` event goes to the project's event stream, and the
  goal's ``metadata["model_refusal"]`` counts refusals per goal;
* one ``review_model_refusal`` operator action request (de-duplicated per goal
  while open) links the goal and turn on the Butler board. It closes when the
  operator resumes or ends the goal, and closing it never resumes the goal:
  the request is not tied to the goal through the operator-action wait marker
  or ``operator_action_request_id``.

No retry, runtime fallback, rerouting, rephrasing or continuation is armed
for a refusal; the rate-limit fallback and the transient-retry paths check
:func:`is_model_refusal` and leave the goal paused.

Only the category and the model are kept, never the refused content beyond
the turn error Core already stores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .diagnostics import record_caught_exception, record_diagnostic
from .domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    utc_now,
)
from .storage import ConflictError, NebulaStore, NotFoundError, revise_latest

REASON_CODE = "model_refusal"
GOAL_METADATA_KEY = "model_refusal"
EVENT_TYPE = "goal.model_refusal"
OPERATOR_ACTION = "review_model_refusal"
# The operator action request's subject; with the goal chat it makes the
# request unique per goal while it is open.
OPERATOR_ACTION_SUBJECT = "goal"
PAUSE_PREFIX = "Model refusal ("
PAUSE_SUFFIX = ": needs operator review"
UNKNOWN_CATEGORY = "unspecified"
UNKNOWN_MODEL = "the selected model"

_CATEGORY_LIMIT = 80
_MODEL_LIMIT = 120
# Claude CLI/API: "API Error: <model> can't help with this. Start a new
# session to continue. ... Details: [<category>]".
_CLAUDE_REFUSAL = re.compile(
    r"API Error:\s*(?P<model>[^\n]{1,200}?)\s+can(?:not|'t|’t) help with this",
    re.IGNORECASE,
)
_DETAILS_CATEGORY = re.compile(r"Details:\s*\[(?P<category>[^\]\n]{1,200})\]")
_PROVIDER_CATEGORY = re.compile(r"\(category:\s*(?P<category>[^)\n]{1,200})\)")
_UNSAFE = re.compile(r"[^A-Za-z0-9 _.,:/@()+-]")


@dataclass(frozen=True, slots=True)
class ModelRefusal:
    """What Core keeps about a refusal: its category and the model named."""

    category: str
    model: str | None = None


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return _UNSAFE.sub("", text).strip()[:limit].strip()


def refusal_from_text(text: str | None) -> ModelRefusal | None:
    """A Claude CLI/API refusal error, read from its text."""

    match = _CLAUDE_REFUSAL.search(text or "")
    if match is None:
        return None
    detail = _DETAILS_CATEGORY.search(text or "")
    return ModelRefusal(
        category=(_clean(detail["category"], _CATEGORY_LIMIT) if detail else "")
        or UNKNOWN_CATEGORY,
        model=_clean(match["model"], _MODEL_LIMIT) or None,
    )


def _provider_category(reason: str) -> str:
    """The category of a provider refusal, without the model's explanation."""

    named = _PROVIDER_CATEGORY.search(reason or "")
    if named is not None:
        return _clean(named["category"], _CATEGORY_LIMIT) or UNKNOWN_CATEGORY
    # "<kind>: <explanation>" keeps the kind; "... (filtered: a, b)" stays whole.
    head = (reason or "").split(": ", 1)[0]
    if head.count("(") != head.count(")"):
        head = reason or ""
    return _clean(head, _CATEGORY_LIMIT) or UNKNOWN_CATEGORY


def refusal_from_error(error: BaseException | None) -> ModelRefusal | None:
    """A provider refusal (stop reason, content filter, guardrail) or a refusal text."""

    from .providers import ProviderRefusalError

    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ProviderRefusalError):
            return ModelRefusal(category=_provider_category(current.reason))
        if getattr(current, "reason_code", None) == REASON_CODE:
            return refusal_from_text(str(current)) or ModelRefusal(UNKNOWN_CATEGORY)
        current = current.__cause__ or current.__context__
    return None


def is_model_refusal(turn: HarnessTurn) -> bool:
    """Whether Core classified this harness turn's failure as a model refusal."""

    diagnostic = turn.metadata.get("diagnostic")
    return (
        isinstance(diagnostic, Mapping) and diagnostic.get("reason_code") == REASON_CODE
    )


def harness_turn_refusal(turn: HarnessTurn) -> ModelRefusal | None:
    if not is_model_refusal(turn):
        return None
    return refusal_from_text(turn.error) or ModelRefusal(UNKNOWN_CATEGORY)


def pause_reason(refusal: ModelRefusal, *, model: str | None, turn_id: str) -> str:
    named = refusal.model or _clean(model, _MODEL_LIMIT) or UNKNOWN_MODEL
    return (
        f"{PAUSE_PREFIX}{refusal.category}) on {named} in turn {turn_id}{PAUSE_SUFFIX}"
    )[:2_000]


def harness_pause_reason(turn: HarnessTurn, owner: ChatTurn) -> str | None:
    """The goal's paused reason for a refused harness turn, else None."""

    refusal = harness_turn_refusal(turn)
    if refusal is None:
        return None
    return pause_reason(refusal, model=owner.model, turn_id=owner.id)


def is_refusal_pause(reason: str | None) -> bool:
    return bool(reason) and str(reason).startswith(PAUSE_PREFIX)


def refusal_count(metadata: Mapping[str, Any] | None) -> int:
    state = (metadata or {}).get(GOAL_METADATA_KEY)
    count = state.get("count") if isinstance(state, Mapping) else None
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def board_view(
    status: ChatGoalStatus, blocked_reason: str | None, count: int
) -> dict[str, Any] | None:
    """The board's refusal fields for one goal, or None when it never refused."""

    paused = status in {
        ChatGoalStatus.PAUSED,
        ChatGoalStatus.BLOCKED,
    } and is_refusal_pause(blocked_reason)
    if not paused and count <= 0:
        return None
    return {
        "paused": paused,
        "count": max(count, 1 if paused else 0),
        "reason": blocked_reason if paused else None,
    }


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


async def _pause(store: NebulaStore, session_id: str, reason: str) -> None:
    from .chat_goals import ChatGoalService, GoalWrite

    goals = ChatGoalService(store)
    for _ in range(3):
        goal = await goals.get(session_id)
        if goal.status != ChatGoalStatus.RUNNING:
            return
        try:
            await goals.write(
                session_id,
                GoalWrite(
                    expected_revision=goal.revision, action="pause", reason=reason
                ),
            )
            return
        except ConflictError:  # diagnostic-expected: reread a concurrent goal update
            continue


async def record_goal_model_refusal(
    store: NebulaStore,
    *,
    session_id: str,
    chat_turn_id: str,
    refusal: ModelRefusal,
    runtime: str,
    model: str | None,
    goal_id: str | None = None,
    harness_turn_id: str | None = None,
) -> ChatGoal | None:
    """Keep the refused goal paused and make the refusal visible. Never raises.

    Idempotent per turn: a second call for the same turn changes nothing.
    """

    from .chat_goals import ChatGoalService

    try:
        try:
            goal = await ChatGoalService(store).get(session_id)
        except NotFoundError:  # diagnostic-expected: goals are optional
            return None
        if goal_id is not None and goal.id != goal_id:
            return None
        reason = pause_reason(refusal, model=model, turn_id=chat_turn_id)
        # A refused turn whose goal is still running (nothing else paused it)
        # pauses here; nothing continues it.
        await _pause(store, session_id, reason)
        named = refusal.model or _clean(model, _MODEL_LIMIT) or None
        now = utc_now()
        entry = {
            "turn_id": chat_turn_id,
            "harness_turn_id": harness_turn_id,
            "runtime": runtime[:200],
            "model": named,
            "category": refusal.category,
            "at": now.isoformat(),
        }
        recorded = False

        def count(latest: ChatGoal) -> dict[str, Any] | None:
            nonlocal recorded
            state = dict(latest.metadata.get(GOAL_METADATA_KEY) or {})
            last = state.get("last")
            if isinstance(last, Mapping) and last.get("turn_id") == chat_turn_id:
                recorded = False
                return None
            recorded = True
            state.update(count=refusal_count(latest.metadata) + 1, last=entry)
            return {"metadata": {**latest.metadata, GOAL_METADATA_KEY: state}}

        goal = await revise_latest(store, ChatGoal, goal.id, count)
        if not recorded:
            return goal
        record_diagnostic(
            "warning",
            "chat",
            "chat.goal.model_refusal",
            "A goal turn ended with a model refusal; the goal stays paused for "
            "the operator.",
            outcome="paused",
            stage="goal-model-refusal",
            retryable=False,
            session_id=session_id,
            metadata={
                "goal_id": goal.id,
                "turn_id": chat_turn_id,
                "category": refusal.category,
            },
        )
        try:
            await _project_event(store, goal, entry, reason)
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.goal.model_refusal_event_failed",
                "A model refusal could not be added to the project events.",
                exc,
                stage="goal-model-refusal",
            )
        request_id = await _raise_operator_action(store, goal, entry, reason)
        if request_id is not None:

            def link(latest: ChatGoal) -> dict[str, Any] | None:
                state = dict(latest.metadata.get(GOAL_METADATA_KEY) or {})
                if state.get("request_id") == request_id:
                    return None
                state["request_id"] = request_id
                return {"metadata": {**latest.metadata, GOAL_METADATA_KEY: state}}

            goal = await revise_latest(store, ChatGoal, goal.id, link)
        return goal
    except Exception as exc:
        record_caught_exception(
            "chat",
            "chat.goal.model_refusal_record_failed",
            "A model refusal could not be recorded on its goal; the goal is not "
            "continued.",
            exc,
            stage="goal-model-refusal",
            metadata={"turn_id": chat_turn_id},
        )
        return None


async def _project_event(
    store: NebulaStore, goal: ChatGoal, entry: Mapping[str, Any], reason: str
) -> None:
    from .butler_oversight import ButlerEventPayload, _append_project_event

    metrics: dict[str, str | int | float | bool | None] = {
        key: str(entry[key])[:200]
        for key in ("turn_id", "harness_turn_id", "runtime", "model", "category", "at")
        if entry.get(key) is not None
    }
    metrics["count"] = refusal_count(goal.metadata)
    async with store.transaction() as transaction:
        await _append_project_event(
            transaction,
            engagement_id=goal.engagement_id,
            event_type=EVENT_TYPE,
            payload=ButlerEventPayload(
                project_id=goal.engagement_id,
                goal_id=goal.id,
                session_id=goal.session_id,
                source="core",
                actor_role="core",
                summary=reason[:500],
                metrics=metrics,
                verification_state="vendor_reported",
            ),
            idempotency_key=f"goal-model-refusal:{goal.id}:{entry['turn_id']}",
            now=utc_now(),
        )


async def _raise_operator_action(
    store: NebulaStore, goal: ChatGoal, entry: Mapping[str, Any], reason: str
) -> str | None:
    """One open review request per goal; returns its id."""

    from .operator_action_requests import create_request

    try:
        result = await create_request(
            store,
            {
                "action": OPERATOR_ACTION,
                "device_or_host": OPERATOR_ACTION_SUBJECT,
                "reason": (
                    f"{reason}. Core did not retry, reroute or continue the goal. "
                    "Review the turn, then resume, change the runtime, or end the goal."
                )[:1000],
                "goal_session_id": goal.session_id,
                "evidence": [
                    f"{label}: {entry[key]}"[:300]
                    for label, key in (
                        ("Turn", "turn_id"),
                        ("Runtime", "runtime"),
                        ("Model", "model"),
                        ("Category", "category"),
                    )
                    if entry.get(key)
                ],
                "verification": [
                    "Core classified the turn failure as model_refusal.",
                    "The goal is paused; Core does not retry, reroute or continue "
                    "a refused turn.",
                ],
                "urgency": "high",
                "created_by": "core",
            },
        )
    except Exception as exc:
        record_caught_exception(
            "chat",
            "chat.goal.model_refusal_request_failed",
            "A model refusal's operator action request could not be raised.",
            exc,
            stage="goal-model-refusal",
        )
        return None
    request = result.get("request")
    identifier = request.get("id") if isinstance(request, Mapping) else None
    return str(identifier) if identifier else None


async def settle_harness_model_refusal(
    store: NebulaStore, harness_turn_id: str
) -> bool:
    """Record a refused harness goal turn. True means: do not continue it."""

    turn = await store.get(HarnessTurn, harness_turn_id)
    if (
        turn.origin != HarnessTurnOrigin.CHAT
        or turn.status not in {HarnessTurnStatus.FAILED, HarnessTurnStatus.INTERRUPTED}
        or not turn.chat_turn_id
        or not turn.chat_session_id
    ):
        return False
    refusal = harness_turn_refusal(turn)
    if refusal is None:
        return False
    owner = await store.get(ChatTurn, turn.chat_turn_id)
    if owner.goal_id:
        session = await store.get(ChatSession, turn.chat_session_id)
        await record_goal_model_refusal(
            store,
            session_id=owner.session_id,
            chat_turn_id=owner.id,
            goal_id=owner.goal_id,
            refusal=refusal,
            runtime=f"harness:{session.harness_profile_id or 'unknown'}",
            model=owner.model or session.model,
            harness_turn_id=turn.id,
        )
    return True


async def close_model_refusal_request(
    store: NebulaStore, session_id: str, *, action: str
) -> None:
    """Close the goal's open refusal review once the operator resumed or ended it.

    Closing never resumes the goal. Never raises.
    """

    try:
        goals = await store.list_session_entities(ChatGoal, session_id)
        if len(goals) != 1:
            return
        goal = goals[0]
        state = goal.metadata.get(GOAL_METADATA_KEY)
        request_id = state.get("request_id") if isinstance(state, Mapping) else None
        if not isinstance(request_id, str) or not request_id:
            return
        from .operator_action_requests import close_request

        await close_request(
            store,
            goal.engagement_id,
            request_id,
            outcome="closed",
            evidence=[f"The operator chose {action} for the goal."],
            closed_by="operator",
        )
    except NotFoundError:  # diagnostic-expected: the request's Work item was removed
        return
    except Exception as exc:
        record_caught_exception(
            "chat",
            "chat.goal.model_refusal_request_close_failed",
            "A model refusal review request could not be closed after the goal "
            "changed.",
            exc,
            stage="goal-model-refusal",
        )
