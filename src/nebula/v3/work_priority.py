"""Operator-set high priority for chats and projects.

The operator marks a chat (``ChatSession.metadata["priority"]``) or a whole
project (``Engagement.metadata["priority"]``) as ``"high"``. Wherever work
waits for shared capacity, high-priority work is taken first; nothing that is
already running is interrupted and no capacity is set aside.

A chat is high when it, the chat it reports to (through subagent and
goal-child links, never forks), or its project is marked. The answer is cached
for a few seconds per process because every admission poll asks it.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal

from .domain import ChatSession, Engagement
from .storage import NotFoundError

if TYPE_CHECKING:
    from .storage import NebulaStore

PRIORITY_KEY = "priority"
HIGH = "high"
Priority = Literal["high", "normal"]
CACHE_SECONDS = 5.0
MAX_DEPTH = 32

_cache: dict[tuple[str, str | None], tuple[float, bool]] = {}


def marked_high(metadata: dict | None) -> bool:
    return (metadata or {}).get(PRIORITY_KEY) == HIGH


def _child_chat(session: ChatSession) -> bool:
    metadata = session.metadata or {}
    return isinstance(metadata.get("subagent_id"), str) or isinstance(
        metadata.get("delegated_from_session_id"), str
    )


async def _project_high(store: "NebulaStore", engagement_id: str) -> bool:
    try:
        project = await store.get(Engagement, engagement_id)
    except NotFoundError:
        # diagnostic-expected: work of a deleted project has normal priority.
        return False
    return marked_high(project.metadata)


async def _chat_high(store: "NebulaStore", session_id: str) -> bool:
    seen: set[str] = set()
    current: str | None = session_id
    for _ in range(MAX_DEPTH):
        if current is None or current in seen:
            return False
        seen.add(current)
        try:
            session = await store.get(ChatSession, current)
        except NotFoundError:
            # diagnostic-expected: a deleted chat has normal priority.
            return False
        if marked_high(session.metadata):
            return True
        current = session.parent_session_id if _child_chat(session) else None
    return False


async def is_high(
    store: "NebulaStore",
    engagement_id: str | None,
    session_id: str | None = None,
) -> bool:
    """Whether this project's (or chat's) waiting work goes first."""

    if not engagement_id:
        return False
    key = (engagement_id, session_id)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    high = await _project_high(store, engagement_id) or (
        session_id is not None and await _chat_high(store, session_id)
    )
    if len(_cache) > 4096:
        _cache.clear()
    _cache[key] = (now, high)
    return high


def forget() -> None:
    """Drop cached answers after an operator changes a priority here."""

    global _projects_cache
    _cache.clear()
    _projects_cache = None


async def set_project_priority(
    store: "NebulaStore", project_id: str, priority: Priority
) -> Engagement:
    """Mark a whole project high (every chat in it inherits) or normal."""

    from .storage import revise_latest

    def plan(latest: Engagement) -> dict | None:
        if marked_high(latest.metadata) == (priority == HIGH):
            return None
        metadata = {
            key: value
            for key, value in (latest.metadata or {}).items()
            if key != PRIORITY_KEY
        }
        if priority == HIGH:
            metadata[PRIORITY_KEY] = HIGH
        return {"metadata": metadata}

    saved = await revise_latest(store, Engagement, project_id, plan)
    forget()
    return saved


_projects_cache: tuple[float, frozenset[str]] | None = None


async def high_project_ids(store: "NebulaStore") -> frozenset[str]:
    """Projects marked high, or holding a chat marked high (cached briefly).

    Project workers are admitted per project, so a project gets its worker
    first when it, or any chat in it, is high priority.
    """

    global _projects_cache
    now = time.monotonic()
    if _projects_cache is not None and now - _projects_cache[0] < CACHE_SECONDS:
        return _projects_cache[1]
    projects = {
        project.id
        for project in await store.find_entities(
            Engagement, {f"metadata.{PRIORITY_KEY}": HIGH}
        )
    }
    projects.update(
        session.engagement_id
        for session in await store.find_entities(
            ChatSession, {f"metadata.{PRIORITY_KEY}": HIGH}
        )
    )
    _projects_cache = (now, frozenset(projects))
    return _projects_cache[1]
