"""Read a project's active hold; shared by every producer of chat work.

A hold (``ProjectHold``) stops all chat work in one project while Core is
deployed or stopped. This module only reads it, so the chat, goal, subagent,
queue and Butler modules can each refuse or defer their own work without
importing the hold service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .domain import ProjectHold
from .storage import ConflictError

if TYPE_CHECKING:
    from .storage import NebulaStore

# ChatGoal.metadata key: the hold that paused this goal. Only that hold's
# release resumes it; every other resume is refused while it is set.
HOLD_MARKER = "project_hold_id"
ACTIVE_HOLD_STATUSES = ("holding", "held")


class ProjectHoldConflict(ConflictError):
    """Work refused because the project is held; retry after its release."""

    code = "project_held"


def held_detail(hold: ProjectHold) -> str:
    return (
        f"Project is held ({hold.reason}); new chat work starts when the hold "
        "is released"
    )


async def active_hold(
    store: "NebulaStore", engagement_id: str | None
) -> ProjectHold | None:
    """The project's hold while it is holding or held, else None."""

    if not engagement_id:
        return None
    holds = await store.find_entities(
        ProjectHold,
        {"status": list(ACTIVE_HOLD_STATUSES)},
        engagement_id=engagement_id,
        limit=1,
    )
    return holds[0] if holds else None


async def is_held(store: "NebulaStore", engagement_id: str | None) -> bool:
    return (await active_hold(store, engagement_id)) is not None
