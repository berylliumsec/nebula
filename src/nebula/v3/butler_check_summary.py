"""Post each finished Butler check's summary to the operator's main Butler chat.

A Butler check (the Nebula-wide periodic check, a project's periodic check, or
an event-triggered recovery case) runs in its own child chat ("Butler check ·
…") under the Butler chat of the project it ran in. The operator reads one
main Butler chat, so Core also appends each finished check's final summary
there as one message that names the check and links to its child chat.

Rules:

* **Capture.** ``ButlerRecoveryService._investigate_case`` saves Butler's
  final reply on the case (``check_summary``). A case is summarized once it
  is finished: ``finished_at`` is set and its status is terminal.
* **When to post.** A check that never produced a Butler investigation (no
  saved reply and no child chat, such as a check that could not start) posts
  nothing; Butler Oversight already shows those. A finished check whose
  outcome is "No change" posts nothing (see ``summary_decision``). Every other
  finished Nebula-wide check posts. A project or event-triggered check posts
  when it has a saved summary or a decision: a change it applied or may have
  applied, a recovery goal, guidance it delivered, a main-agent dialogue, or
  a result that needs the operator (``needs_attention`` / ``uncertain``).
* **"No change".** Core keeps no structured "no change" verdict, so the rule
  is conservative and needs both: (1) Core's structured outcome shows nothing
  happened (status ``resolved``; no applied or possible background change; no
  recovery goal, delivered guidance or main-agent dialogue), and (2) the
  first non-empty line of Butler's reply, ignoring Markdown markers and an
  optional ``Outcome:``/``Result:``/``Status:``/``Summary:`` label, starts
  with "No change" or "No changes". Text alone never suppresses a check that
  changed something or needs attention.
* **Exactly once.** The case records ``summary_post`` in the same transaction
  that queues its summary, so a retried, recovered or re-finished case is
  never queued again. The message id is derived from the case id, and the
  queue entry is removed in the transaction that inserts the message.
* **Never a Butler turn.** The summary is a saved assistant message with
  ``metadata.kind == "butler_check_summary"``: Butler's words, posted by Core.
  It is not operator input, starts no turn, and is not a reply that
  dialogue verification waits for (that requires no ``metadata.kind``).
* **Target.** The operator may choose a project Butler chat (Settings →
  Automation → Butler check interval; ``PUT /butler/check-interval`` with
  ``summary_target``). Otherwise Core uses the Butler chat of the project
  that hosts the Nebula-wide check (the global Butler instructions' anchor),
  then the most recently recorded Butler chat, then the check's own project
  Butler chat. A target that is deleted, archived, no longer a root Butler
  chat, or in an archived project falls back to the next candidate; Core
  records each fallback on the message, in ``check_summary_last_fallback``
  and as a diagnostic.
* **Project ownership.** A project worker may write only its own project's
  rows, so the summary is queued on the shared ``ButlerGlobalState`` row and
  the worker that owns the target chat's project inserts the message. A
  target project that does not deliver within ``STALE_TARGET`` hands the
  summary to the next candidate's project. The queue holds at most
  ``OUTBOX_LIMIT`` summaries; a check finishing while it is full records
  ``queue_full`` and a diagnostic instead of displacing a queued summary.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import quote

from .database import EntityRow
from .deployment_protocol import RESERVED_PROJECT_IDS
from .diagnostics import record_caught_exception, record_diagnostic
from .domain import (
    ButlerFocusRuntime,
    ButlerGlobalState,
    ButlerRecoveryCase,
    ChatMessage,
    ChatRole,
    ChatSession,
    Engagement,
    EngagementStatus,
    utc_now,
)
from .storage import REVISE_ATTEMPTS, NebulaStore, RevisionConflict
from .stored_entities import decode_stored

SUMMARY_KIND = "butler_check_summary"
GLOBAL_SIGNAL = "butler.global_periodic_check"
TERMINAL_STATUSES = ("resolved", "needs_attention", "uncertain", "cancelled")
# A finished case older than this when first seen is history, not news.
PUBLISH_WINDOW = timedelta(minutes=30)
STALE_TARGET = timedelta(minutes=15)
MAX_SUMMARY_CHARACTERS = 6_000
OUTBOX_LIMIT = 50

_STATUS_LABELS = {
    "resolved": "Resolved",
    "needs_attention": "Needs your attention",
    "uncertain": "Uncertain; review before retrying",
    "cancelled": "Cancelled",
}
_LINE_MARKERS = re.compile(r"^[\s#>*_`~\-–—•]+|[\s*_`~]+$")
_LABEL = re.compile(r"^(?:outcome|result|status|summary)\s*[:\-–—]\s*", re.IGNORECASE)
_NO_CHANGE = re.compile(r"^no\s+changes?\b", re.IGNORECASE)


def _states() -> tuple[str, Callable[[list[dict[str, Any]]], bool], Any]:
    from .butler_recovery import GLOBAL_STATE_ID, _applied_change, _possible_change

    return GLOBAL_STATE_ID, _applied_change, _possible_change


def _structured_activity(case: ButlerRecoveryCase) -> bool:
    _, applied, possible = _states()
    return bool(
        applied(case.steps)
        or possible(case.steps)
        or case.recovery_goal_ids
        or case.queued_item_id
        or case.delivered_turn_id
        or case.dialogue_results
    )


def says_no_change(text: str) -> bool:
    """Whether the reply's first meaningful line reports "No change"."""

    for line in text.splitlines():
        cleaned = _LINE_MARKERS.sub("", line).strip()
        if not cleaned:
            continue
        unlabelled = _LINE_MARKERS.sub("", _LABEL.sub("", cleaned)).strip()
        return bool(_NO_CHANGE.match(unlabelled))
    return False


def summary_decision(case: ButlerRecoveryCase) -> tuple[bool, str]:
    """Whether a finished case posts its summary, and the recorded reason."""

    summary = case.check_summary.strip()
    if not summary and not case.investigation_session_id:
        return False, "not_run"
    activity = _structured_activity(case)
    if case.status == "resolved" and not activity and says_no_change(summary):
        return False, "no_change"
    if case.signal == GLOBAL_SIGNAL:
        return True, "nebula_check"
    if summary or activity or case.status in {"needs_attention", "uncertain"}:
        return True, "summary_or_decision"
    return False, "no_summary_or_decision"


def message_id(case_id: str) -> str:
    return "butler-check-summary:" + hashlib.sha256(case_id.encode()).hexdigest()[:40]


def chat_url(project_id: str, session_id: str) -> str:
    return (
        f"/projects/{quote(project_id, safe='')}/workbench?view=chat"
        f"&session={quote(session_id, safe='')}"
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


async def compose_summary(
    store: NebulaStore, case: ButlerRecoveryCase
) -> tuple[str, dict[str, Any]]:
    """The message text and metadata, read in the check's own project."""

    project = await store.find_entity(Engagement, "id", case.engagement_id)
    name = project.name if project is not None else case.engagement_id
    nebula_wide = case.signal == GLOBAL_SIGNAL
    finished = case.finished_at or utc_now()
    scope = "Nebula-wide" if nebula_wide else "Project"
    lines = [
        f"**Butler check summary · {scope} · {_time(finished)}**",
        "",
        (
            f"- Scope: Nebula-wide check, run from project **{name}**"
            if nebula_wide
            else f"- Scope: project **{name}** · signal `{case.signal}`"
        ),
        f"- Result: {_STATUS_LABELS.get(case.status, case.status)}",
    ]
    if case.investigation_session_id:
        lines.append(
            f"- Full check: [Butler check · {_clip(case.summary, 120)}]"
            f"({chat_url(case.engagement_id, case.investigation_session_id)})"
        )
    summary = case.check_summary.strip()
    outcome = case.outcome.strip()
    if outcome and not summary.startswith(outcome[:200]):
        lines.append(f"- Core: {_clip(outcome, 500)}")
    if summary:
        lines += ["", _clip(summary, MAX_SUMMARY_CHARACTERS)]
        if len(summary) > MAX_SUMMARY_CHARACTERS:
            lines.append("\n_The full reply is in the check chat._")
    metadata = {
        "kind": SUMMARY_KIND,
        "author": "butler",
        "operator_input": False,
        "butler_recovery_case_id": case.id,
        "check_scope": "nebula" if nebula_wide else "project",
        "check_signal": case.signal,
        "check_status": case.status,
        "check_finished_at": finished.isoformat(),
        "check_project_id": case.engagement_id,
        "check_session_id": case.investigation_session_id,
    }
    return "\n".join(lines), metadata


async def _global_state(store: NebulaStore) -> ButlerGlobalState | None:
    global_state_id, _, _ = _states()
    return await store.find_entity(ButlerGlobalState, "id", global_state_id)


async def _recent_butler_chat(store: NebulaStore) -> ButlerFocusRuntime | None:
    """The newest recorded Butler chat runtime, read from shared rows."""

    newest: ButlerFocusRuntime | None = None
    async for runtime in store.iter_readable_entities(ButlerFocusRuntime):
        if runtime.source_project in RESERVED_PROJECT_IDS:
            continue
        if newest is None or runtime.updated_at > newest.updated_at:
            newest = runtime
    return newest


async def candidate_targets(
    store: NebulaStore, case: ButlerRecoveryCase, state: ButlerGlobalState | None
) -> list[dict[str, Any]]:
    """Ordered targets; a ``session_id`` of None means the project's Butler chat."""

    candidates: list[dict[str, Any]] = []
    if state is not None and state.check_summary_target_project_id:
        candidates.append(
            {
                "source": "configured",
                "project_id": state.check_summary_target_project_id,
                "session_id": state.check_summary_target_session_id,
            }
        )
    if state is not None and state.last_project_id:
        candidates.append(
            {
                "source": "global_anchor",
                "project_id": state.last_project_id,
                "session_id": None,
            }
        )
    recent = await _recent_butler_chat(store)
    if recent is not None:
        candidates.append(
            {
                "source": "recent_butler_chat",
                "project_id": recent.source_project,
                "session_id": recent.session_id,
            }
        )
    parent: str | None = None
    if case.investigation_session_id:
        child = await store.find_entity(
            ChatSession, "id", case.investigation_session_id
        )
        parent = child.parent_session_id if child is not None else None
    candidates.append(
        {
            "source": "check_project",
            "project_id": case.engagement_id,
            "session_id": parent,
        }
    )
    unique: list[dict[str, Any]] = []
    for item in candidates:
        if not any(
            (other["project_id"], other["session_id"])
            == (item["project_id"], item["session_id"])
            for other in unique
        ):
            unique.append(item)
    return unique


def _is_root_butler_chat(session: ChatSession) -> bool:
    return (
        session.metadata.get("control_assistant") is True
        and not session.metadata.get("archived_at")
        and not session.parent_session_id
    )


async def resolve_target(
    store: NebulaStore, project_id: str, session_id: str | None
) -> tuple[ChatSession | None, str | None]:
    """The usable target chat in ``project_id``, or why it cannot be used."""

    from .butler_recovery import _root_butler_sessions

    project = await store.find_entity(Engagement, "id", project_id)
    if project is None:
        return None, "project was deleted"
    if project.status == EngagementStatus.ARCHIVED:
        return None, "project is archived"
    if session_id is None:
        sessions = [item async for item in _root_butler_sessions(store, project_id)]
        if not sessions:
            return None, "project has no Butler chat"
        return max(sessions, key=lambda item: item.created_at), None
    session = await store.find_entity(ChatSession, "id", session_id)
    if session is None:
        return None, "chat was deleted"
    if session.engagement_id != project_id:
        return None, "chat belongs to another project"
    if session.metadata.get("archived_at"):
        return None, "chat is archived"
    if not _is_root_butler_chat(session):
        return None, "chat is not a project Butler chat"
    return session, None


async def publish_check_summary(
    store: NebulaStore,
    case_id: str,
    *,
    wake: Callable[[str], None] | None = None,
) -> ButlerRecoveryCase:
    """Decide once for a finished case, queue its summary, and deliver locally."""

    global_state_id, _, _ = _states()
    case = await store.get(ButlerRecoveryCase, case_id)
    if (
        case.summary_post is not None
        or case.finished_at is None
        or case.status not in TERMINAL_STATUSES
    ):
        return case
    post, reason = summary_decision(case)
    entry: dict[str, Any] | None = None
    if post:
        content, metadata = await compose_summary(store, case)
        state = await _global_state(store)
        candidates = await candidate_targets(store, case, state)
        entry = {
            "case_id": case.id,
            "message_id": message_id(case.id),
            "content": content,
            "metadata": metadata,
            "candidates": candidates,
            "index": 0,
            "stage_since": utc_now().isoformat(),
            "fallbacks": [],
        }
    queued: dict[str, Any] | None = None
    for attempt in range(1, REVISE_ATTEMPTS + 1):
        queued = entry
        try:
            async with store.transaction(immediate=True) as transaction:
                (await transaction.lock_shared_rows())
                row = await transaction.session.get(EntityRow, case.id)
                current = (
                    decode_stored(ButlerRecoveryCase, row.payload) if row else None
                )
                if current is None or current.summary_post is not None:
                    return await store.get(ButlerRecoveryCase, case.id)
                marker: dict[str, Any] = {
                    "state": "queued" if queued else "skipped",
                    "reason": reason,
                    "at": utc_now().isoformat(),
                }
                if queued is not None:
                    state_row = await transaction.session.get(
                        EntityRow, global_state_id
                    )
                    state = (
                        decode_stored(ButlerGlobalState, state_row.payload)
                        if state_row
                        else None
                    )
                    outbox = list(state.check_summary_outbox) if state else []
                    if len(outbox) >= OUTBOX_LIMIT:
                        # Fifty undelivered summaries mean no target project is
                        # processing; keep the queued ones and report this one.
                        marker.update(state="skipped", reason="queue_full")
                        queued = None
                        record_diagnostic(
                            "warning",
                            "butler",
                            "butler.check_summary.queue_full",
                            "Fifty Butler check summaries are waiting for their "
                            "target chats; this check's summary was not queued.",
                            outcome="degraded",
                            stage="check-summary",
                            metadata={
                                "case_id": case.id,
                                "kind": "butler_check_summary",
                            },
                        )
                    elif state is None:
                        marker["message_id"] = queued["message_id"]
                        (
                            await transaction.add(
                                ButlerGlobalState(
                                    id=global_state_id, check_summary_outbox=[queued]
                                )
                            )
                        )
                    else:
                        marker["message_id"] = queued["message_id"]
                        (
                            await transaction.update(
                                ButlerGlobalState,
                                state.id,
                                {"check_summary_outbox": [*outbox, queued]},
                                expected_revision=state.revision,
                            )
                        )
                (
                    await transaction.update(
                        ButlerRecoveryCase,
                        current.id,
                        {"summary_post": marker},
                        expected_revision=current.revision,
                    )
                )
            break
        except RevisionConflict:
            # diagnostic-expected: another writer saved the case or the shared
            # queue first; decide again on the newest revision.
            if attempt == REVISE_ATTEMPTS:
                raise
    if queued is not None:
        target = queued["candidates"][queued["index"]]["project_id"]
        if target == case.engagement_id:
            (await deliver_check_summaries(store, case.engagement_id, wake=wake))
        elif wake is not None:
            wake(target)
    return await store.get(ButlerRecoveryCase, case.id)


def _owner_index(entry: dict[str, Any], project_id: str, now: datetime) -> int | None:
    """The candidate this project's worker should act on for ``entry``."""

    candidates = entry.get("candidates") or []
    index = entry.get("index", 0)
    if not isinstance(index, int) or not 0 <= index < len(candidates):
        return None
    if candidates[index]["project_id"] == project_id:
        return index
    try:
        since = datetime.fromisoformat(str(entry.get("stage_since")))
    except ValueError:
        # diagnostic-expected: an unreadable stage time is treated as stale.
        since = now - STALE_TARGET
    if now - since < STALE_TARGET:
        return None
    for later in range(index + 1, len(candidates)):
        if candidates[later]["project_id"] == project_id:
            return later
    return None


async def deliver_check_summaries(
    store: NebulaStore,
    project_id: str,
    *,
    wake: Callable[[str], None] | None = None,
) -> int:
    """Insert queued summaries whose target chat this project's worker owns."""

    global_state_id, _, _ = _states()
    delivered = 0
    for _ in range(OUTBOX_LIMIT * 4):
        state = await _global_state(store)
        if state is None:
            return delivered
        now = utc_now()
        picked = next(
            (
                (entry, owner)
                for entry in state.check_summary_outbox
                if (owner := _owner_index(entry, project_id, now)) is not None
            ),
            None,
        )
        if picked is None:
            return delivered
        entry, owner = picked
        candidate = entry["candidates"][owner]
        fallbacks = list(entry.get("fallbacks") or [])
        if owner != entry.get("index"):
            skipped = entry["candidates"][entry["index"]]
            fallbacks.append(
                {
                    **skipped,
                    "reason": "its project did not deliver the summary in time",
                    "at": now.isoformat(),
                }
            )
        session, problem = await resolve_target(
            store, project_id, candidate["session_id"]
        )
        existing = await store.find_entity(ChatMessage, "id", entry["message_id"])
        sequence = await _next_sequence(store, session) if session is not None else 0
        try:
            async with store.transaction(immediate=True) as transaction:
                (await transaction.lock_shared_rows())
                row = await transaction.session.get(EntityRow, global_state_id)
                current = decode_stored(ButlerGlobalState, row.payload) if row else None
                if current is None:
                    return delivered
                position = next(
                    (
                        number
                        for number, item in enumerate(current.check_summary_outbox)
                        if item.get("case_id") == entry["case_id"]
                    ),
                    None,
                )
                if position is None or current.check_summary_outbox[position].get(
                    "index"
                ) != entry.get("index"):
                    continue
                outbox = list(current.check_summary_outbox)
                changes: dict[str, Any] = {}
                next_project: str | None = None
                if existing is not None:
                    outbox.pop(position)
                elif session is None:
                    fallbacks.append(
                        {**candidate, "reason": problem, "at": now.isoformat()}
                    )
                    if owner + 1 >= len(entry["candidates"]):
                        outbox.pop(position)
                        changes["check_summary_last_fallback"] = {
                            "case_id": entry["case_id"],
                            "fallbacks": fallbacks,
                            "delivered_to": None,
                            "at": now.isoformat(),
                        }
                        record_diagnostic(
                            "warning",
                            "butler",
                            "butler.check_summary.undeliverable",
                            "No Butler chat could receive this check summary.",
                            outcome="failed",
                            stage="check-summary",
                            metadata={
                                "case_id": entry["case_id"],
                                "kind": "butler_check_summary",
                            },
                        )
                    else:
                        next_project = entry["candidates"][owner + 1]["project_id"]
                        outbox[position] = {
                            **entry,
                            "index": owner + 1,
                            "stage_since": now.isoformat(),
                            "fallbacks": fallbacks,
                        }
                else:
                    message = ChatMessage(
                        id=entry["message_id"],
                        engagement_id=project_id,
                        session_id=session.id,
                        sequence=sequence,
                        role=ChatRole.ASSISTANT,
                        content=entry["content"],
                        metadata={
                            **entry["metadata"],
                            "target_source": candidate["source"],
                            **({"target_fallbacks": fallbacks} if fallbacks else {}),
                        },
                    )
                    (
                        await transaction.update(
                            ChatSession,
                            session.id,
                            {
                                "metadata": {
                                    **session.metadata,
                                    "message_count": sequence,
                                    "last_sequence": sequence,
                                }
                            },
                            expected_revision=session.revision,
                        )
                    )
                    (await transaction.add(message))
                    outbox.pop(position)
                    if fallbacks:
                        changes["check_summary_last_fallback"] = {
                            "case_id": entry["case_id"],
                            "fallbacks": fallbacks,
                            "delivered_to": {
                                "source": candidate["source"],
                                "project_id": project_id,
                                "session_id": session.id,
                            },
                            "at": now.isoformat(),
                        }
                    delivered += 1
                changes["check_summary_outbox"] = outbox
                (
                    await transaction.update(
                        ButlerGlobalState,
                        current.id,
                        changes,
                        expected_revision=current.revision,
                    )
                )
        except RevisionConflict:
            # diagnostic-expected: the chat or the shared queue moved; the
            # next pass reads both again and the fixed message id keeps it once.
            continue
        if session is None and existing is None:
            record_diagnostic(
                "warning",
                "butler",
                "butler.check_summary.target_fallback",
                "A Butler check summary target was unavailable; Core used the next chat.",
                outcome="degraded",
                stage="check-summary",
                metadata={
                    "case_id": entry["case_id"],
                    "target_source": candidate["source"],
                    "reason": problem,
                    "kind": "butler_check_summary",
                },
            )
            if next_project is not None and next_project != project_id and wake:
                wake(next_project)
    return delivered


async def _next_sequence(store: NebulaStore, session: ChatSession) -> int:
    latest = await store.list_session_entities(
        ChatMessage, session.id, newest_first=True, limit=1
    )
    recorded = session.metadata.get("last_sequence")
    return (
        max(
            [item.sequence for item in latest]
            + [recorded if isinstance(recorded, int) else 0]
        )
        + 1
    )


async def publish_recent_check_summaries(
    store: NebulaStore,
    project_id: str,
    *,
    wake: Callable[[str], None] | None = None,
) -> int:
    """Decide for this project's recently finished, undecided cases."""

    published = 0
    cutoff = utc_now() - PUBLISH_WINDOW
    for case in await store.find_entities(
        ButlerRecoveryCase,
        {"summary_post": None, "status": list(TERMINAL_STATUSES)},
        engagement_id=project_id,
        newest_first=True,
        limit=50,
    ):
        if case.finished_at is None or case.finished_at < cutoff:
            continue
        try:
            (await publish_check_summary(store, case.id, wake=wake))
            published += 1
        except Exception as exc:
            record_caught_exception(
                "butler",
                "butler.check_summary.publish_failed",
                "A Butler check summary was not posted; Core retries on the next pass.",
                exc,
                stage="check-summary",
            )
    return published


async def summary_target_snapshot(store: NebulaStore) -> dict[str, Any]:
    """The configured and effective target, its choices, and the last fallback."""

    state = await _global_state(store)
    options: list[dict[str, Any]] = []
    async for project in store.iter_readable_entities(Engagement):
        if (
            project.id in RESERVED_PROJECT_IDS
            or project.status == EngagementStatus.ARCHIVED
        ):
            continue
        for session in await store.find_entities(
            ChatSession,
            {"parent_session_id": None, "metadata.archived_at": None},
            engagement_id=project.id,
            newest_first=True,
            limit=500,
        ):
            if _is_root_butler_chat(session):
                options.append(
                    {
                        "project_id": project.id,
                        "project_name": project.name,
                        "session_id": session.id,
                        "title": session.title,
                    }
                )
    configured = (
        {
            "project_id": state.check_summary_target_project_id,
            "session_id": state.check_summary_target_session_id,
        }
        if state is not None and state.check_summary_target_project_id
        else None
    )
    effective: dict[str, Any] | None = None
    sources: list[tuple[str, str | None, str | None]] = []
    if configured is not None:
        sources.append(
            ("configured", configured["project_id"], configured["session_id"])
        )
    if state is not None and state.last_project_id:
        sources.append(("global_anchor", state.last_project_id, None))
    recent = await _recent_butler_chat(store)
    if recent is not None:
        sources.append(("recent_butler_chat", recent.source_project, recent.session_id))
    problems: list[dict[str, Any]] = []
    for source, target_project, target_session in sources:
        assert target_project is not None
        resolved, problem = await resolve_target(store, target_project, target_session)
        if resolved is None:
            problems.append(
                {"source": source, "project_id": target_project, "reason": problem}
            )
            continue
        effective = {
            "source": source,
            "project_id": target_project,
            "session_id": resolved.id,
            "title": resolved.title,
            "url": chat_url(target_project, resolved.id),
        }
        break
    return {
        "configured": configured,
        "effective": effective,
        "unavailable": problems,
        "last_fallback": state.check_summary_last_fallback if state else None,
        "pending": len(state.check_summary_outbox) if state else 0,
        "options": options[:200],
    }


async def validate_summary_target(
    store: NebulaStore, project_id: str, session_id: str
) -> None:
    if project_id in RESERVED_PROJECT_IDS:
        raise ValueError("Choose a project Butler chat for check summaries")
    session, problem = await resolve_target(store, project_id, session_id)
    if session is None:
        raise ValueError(f"That Butler chat cannot receive check summaries: {problem}")
