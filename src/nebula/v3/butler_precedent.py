"""Bounded, project-authorized memory context for Butler review and action guards."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from .domain import NebulaModel

if TYPE_CHECKING:
    from .butler_memory import ButlerMemoryService


_QUERIES = {
    "agent.progress_reported": "investigation progress evidence next checkpoint lesson",
    "agent.attention_reported": "urgent machine unreachable blocker pause parent agent lesson",
    "agent.plan_changed": "investigation plan change correction precedent lesson",
    "goal.progress_overdue": "progress checkpoint overdue parent agent status lesson",
    "goal.plan_changed": "investigation plan change correction precedent lesson",
    "goal.blocked": "blocked investigation recovery parent agent precedent lesson",
    "goal.read_only_tool_failures_repeated": "repeated read only tool failure parent agent reassessment lesson",
}


class ButlerPrecedent(NebulaModel):
    id: str = Field(min_length=1, max_length=200)
    scope: Literal["global_preference", "global_lesson", "project_lesson"]
    content: str = Field(min_length=1, max_length=400)
    source_label: str = Field(max_length=100)
    revision: int = Field(ge=1)
    updated_at: str = Field(min_length=1, max_length=50)


class ButlerPrecedentSnapshot(NebulaModel):
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_state: Literal["vector_ready", "lexical_fallback"]
    items: list[ButlerPrecedent] = Field(max_length=4)


def read_precedents(
    memory: ButlerMemoryService, project_id: str, event_type: str
) -> ButlerPrecedentSnapshot:
    """Read only selected-project memories; foreign source refs never enter packets."""

    query = _QUERIES.get(event_type)
    if query is None:
        raise ValueError("unsupported Butler precedent event")
    recent = memory.list(project_id, limit=40)
    relevant = memory.search(project_id, query, limit=3)
    selected = [item for item in recent.items if item.scope == "global_preference"][:2]
    selected.extend(
        item
        for item in relevant.items
        if item.id not in {known.id for known in selected}
    )
    entries = [
        ButlerPrecedent(
            id=item.id,
            scope=item.scope,
            content=item.content[:400],
            source_label=item.source_label[:100],
            revision=item.revision,
            updated_at=item.updated_at.isoformat(),
        )
        for item in selected[:4]
    ]
    basis = {
        "index_state": relevant.index_status.state,
        "items": [item.model_dump(mode="json") for item in entries],
    }
    digest = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ButlerPrecedentSnapshot(
        digest=digest,
        index_state=relevant.index_status.state,
        items=entries,
    )
