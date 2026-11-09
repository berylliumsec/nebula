"""A live handoff drains roots, while their already-admitted descendants finish."""

from sqlalchemy import select

from .database import EntityRow, ProjectHandoffRow

HANDOFF_FEATURE = "project.handoff/v1"
PARTICIPANT_FEATURE = "project.participants/v1"
HANDOFF_WAIT = "Waiting for the project update; this response remains queued"


async def turn_admission_allowed(session, turn):
    """Call inside the admission transaction holding the project ownership lock."""
    handoff = await session.get(ProjectHandoffRow, turn.engagement_id)
    if handoff is None or not handoff.admission_held or handoff.phase == "verified":
        return True
    if handoff.phase == "transferred":
        return False
    admitted = set(handoff.admitted_turn_ids or ())
    current, seen = turn.id, set()
    for _ in range(32):
        if current in admitted:
            return True
        if current in seen:
            return False
        seen.add(current)
        row = await session.get(EntityRow, current)
        if (
            row is None
            or row.kind != "chat_turns"
            or row.engagement_id != turn.engagement_id
        ):
            return False
        parent = await session.scalar(
            select(EntityRow)
            .where(
                EntityRow.kind == "chat_subagents",
                EntityRow.engagement_id == turn.engagement_id,
                EntityRow.payload["child_session_id"].as_string()
                == row.payload.get("session_id"),
                EntityRow.payload["status"].as_string() == "running",
            )
            .limit(1)
        )
        if parent is None:
            return False
        current = parent.payload.get("parent_turn_id")
        if not isinstance(current, str):
            return False
    return False
