"""Persist admission explanations without changing an owner's lifecycle."""

from __future__ import annotations

from typing import Any

from .domain import ChatTurn, Entity
from .storage import ConflictError, NebulaStore


def save_host_wait(
    store: NebulaStore, model: type[Entity], identity: str, reason: str | None
) -> None:
    for attempt in range(3):
        current = store.get(model, identity)
        if getattr(current, "status", None) != "queued":
            return
        changes: dict[str, Any]
        if isinstance(current, ChatTurn):
            if current.admission_reason == reason:
                return
            changes = {"admission_reason": reason}
        else:
            metadata = dict(getattr(current, "metadata"))
            if metadata.get("host_load_wait") == reason:
                return
            if reason is None:
                metadata.pop("host_load_wait", None)
            else:
                metadata["host_load_wait"] = reason
            changes = {"metadata": metadata}
        try:
            store.update(model, identity, changes, expected_revision=current.revision)
            return
        except ConflictError:
            # diagnostic-expected: reread a concurrently changed owner, never overwrite it.
            if attempt == 2:
                raise
