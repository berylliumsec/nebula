"""Client presentation and in-memory drafts, never execution state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def visible_message(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata") or {}
    return message.get("role") in {"user", "assistant"} and not any(
        metadata.get(name)
        for name in ("retracted_at", "replaced_at", "replaced_by_message_id")
    )


def main_chat(chat: dict[str, Any]) -> bool:
    meta = chat.get("metadata") or {}
    return not any(
        meta.get(name)
        for name in (
            "side_chat",
            "temporary_assistant",
            "control_assistant",
            "subagent_id",
            "delegated_from_goal_id",
        )
    )


@dataclass
class Draft:
    text: str = ""
    skill: dict[str, str] | None = None
    skills: list[dict[str, str]] = field(default_factory=list)
    contexts: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Cursor:
    turn_id: str | None = None
    harness_turn_id: str | None = None
    sequence: int = 0
    epoch: str | None = None
    text: str = ""

    def accept(self, payload: dict[str, Any]) -> bool:
        if payload.get("epoch") and payload["epoch"] != self.epoch:
            self.sequence = 0
            self.epoch = payload["epoch"]
            self.text = ""
        sequence = payload.get("sequence")
        if isinstance(sequence, int):
            if sequence <= self.sequence:
                return False
            self.sequence = sequence
        return True


@dataclass
class View:
    project: dict[str, Any]
    session: dict[str, Any] | None = None
    config: dict[str, Any] = field(default_factory=dict)
    draft: Draft = field(default_factory=Draft)
    pending_draft: Draft | None = None
    cursor: Cursor = field(default_factory=Cursor)
    history_before: str | None = None
    submitting: bool = False
    uncertain: bool = False
    environments: list[str] | None = None

    @property
    def project_id(self) -> str:
        return self.project["id"]

    @property
    def session_id(self) -> str:
        if self.session is None:
            raise ValueError(
                "Send the first message or create a goal to save this conversation."
            )
        return self.session["id"]
