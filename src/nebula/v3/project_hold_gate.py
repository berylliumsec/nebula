"""Read a project's active hold; shared by every producer of chat work.

A hold (``ProjectHold``) stops chat work in one project while Core is deployed
or stopped, or in chosen chats when the operator's priorities shift. This
module only reads it, so the chat, goal, subagent, queue and Butler modules
can each refuse or defer their own work without importing the hold service.

A project-wide hold has no ``session_id`` and covers everything. A chat hold
covers its chat and every subagent or goal-child chat beneath it, but never a
fork, and never a chat started after it: new work in the project goes on.
Callers that know the chat pass ``session_id``; callers that do not see only
project-wide holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .domain import ChatSession, ProjectHold
from .storage import ConflictError, NotFoundError

if TYPE_CHECKING:
    from .storage import NebulaStore

# ChatGoal.metadata key: the hold that paused this goal. Only that hold's
# release resumes it; every other resume is refused while it is set.
HOLD_MARKER = "project_hold_id"
ACTIVE_HOLD_STATUSES = ("holding", "held")
# Deepest subagent / goal-child nesting walked when finding a chat's holders.
MAX_HOLD_DEPTH = 32


class ProjectHoldConflict(ConflictError):
    """Work refused because the project is held; retry after its release."""

    code = "project_held"


def held_detail(hold: ProjectHold) -> str:
    if hold.session_id:
        return (
            f"This chat is on hold ({hold.reason}); it continues when the hold "
            "is released"
        )
    return (
        f"Project is held ({hold.reason}); new chat work starts when the hold "
        "is released"
    )


def is_child_chat(session: ChatSession) -> bool:
    """A subagent or goal-child chat, which a hold on its parent covers."""

    metadata = session.metadata or {}
    return isinstance(metadata.get("subagent_id"), str) or isinstance(
        metadata.get("delegated_from_session_id"), str
    )


async def hold_chain(store: "NebulaStore", session_id: str) -> list[str]:
    """``session_id`` and the chats whose hold covers it, nearest first.

    Walks ``parent_session_id`` only through subagent and goal-child links, so
    a fork of a held chat is not held.
    """

    chain = [session_id]
    current = session_id
    for _ in range(MAX_HOLD_DEPTH):
        try:
            session = await store.get(ChatSession, current)
        except NotFoundError:
            # diagnostic-expected: a deleted chat covers only itself.
            break
        parent = session.parent_session_id
        if not parent or not is_child_chat(session) or parent in chain:
            break
        chain.append(parent)
        current = parent
    return chain


async def active_holds(
    store: "NebulaStore", engagement_id: str | None
) -> list[ProjectHold]:
    """Every holding or held hold in the project, project-wide and chat."""

    if not engagement_id:
        return []
    return await store.find_entities(
        ProjectHold,
        {"status": list(ACTIVE_HOLD_STATUSES)},
        engagement_id=engagement_id,
    )


async def active_hold(
    store: "NebulaStore",
    engagement_id: str | None,
    session_id: str | None = None,
) -> ProjectHold | None:
    """The hold that stops work here, else None.

    Without ``session_id`` only a project-wide hold counts. With it, a
    project-wide hold or a chat hold on that chat or one of its parents.
    """

    holds = await active_holds(store, engagement_id)
    for hold in holds:
        if not hold.session_id:
            return hold
    if not session_id:
        return None
    scoped = {hold.session_id: hold for hold in holds if hold.session_id}
    if not scoped:
        return None
    for chat in await hold_chain(store, session_id):
        if chat in scoped:
            return scoped[chat]
    return None


async def is_held(
    store: "NebulaStore",
    engagement_id: str | None,
    session_id: str | None = None,
) -> bool:
    return (await active_hold(store, engagement_id, session_id)) is not None
