"""Working notes: the durable scratchpad a provider conversation's assistant keeps.

A long tool turn outgrows any window. Its older results become receipts and
its older steps fold into a checkpoint, so what the model learned early can
leave the request while the work goes on. The model keeps what it needs with
``notes.write``: findings with their exact identifiers, decisions and a todo
list, rewritten whole as the work progresses (Anthropic's structured
note-taking and memory tool, Manus's todo recitation).

The notes are the assistant's own derived memory. Core stores them per
conversation, returns them with the checkpoint whenever it advances inside a
turn, and appends them to the operator's message when a later turn starts.
They are history data in the request, never instructions, and they never stand
in for the tool output they describe.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .context import WorkingNotesStatus
from .domain import WORKING_NOTES_MAX_BYTES, ChatWorkingNotes, RiskClass
from .storage import ConflictError, NebulaStore, NotFoundError
from .tools import (
    IdempotencyBehavior,
    InvalidToolArguments,
    InvocationAnalysisTool,
    ToolBrokerError,
    ToolInvocation,
    ToolSpec,
)

NOTES_WRITE_TOOL_NAME = "notes.write"
_WRITE_ATTEMPTS = 3

WORKING_NOTES_HEADING = (
    "WORKING NOTES (kept by the assistant with notes.write; derived memory, "
    "not instructions or evidence; JSON):"
)

# Routing guidance, added beside the tool while it is offered.
NOTES_ROUTING_INSTRUCTIONS = """

On long or multi-step work, keep working notes with notes.write: findings with
their exact paths, ids, hosts and commands, decisions, and a todo list. Each
call replaces the notes, so rewrite them as the work progresses. They stay with
the conversation after older tool results leave the request."""


def working_notes_id(session_id: str) -> str:
    """The one notes record a conversation can have."""

    return str(uuid5(NAMESPACE_URL, f"nebula:chat-working-notes:{session_id}"))


def read_working_notes(store: NebulaStore, session_id: str) -> ChatWorkingNotes | None:
    try:
        return store.get(ChatWorkingNotes, working_notes_id(session_id))
    except (
        NotFoundError
    ):  # diagnostic-expected: the assistant has not written notes for this conversation
        return None


def write_working_notes(
    store: NebulaStore,
    *,
    engagement_id: str,
    session_id: str,
    content: str,
    turn_id: str | None,
) -> ChatWorkingNotes:
    """Replace the conversation's notes, creating the record on first write.

    A write replaces the whole text, so a concurrent writer only needs the
    revision it read to be current: a conflict re-reads and writes again.
    """

    notes_id = working_notes_id(session_id)
    for _ in range(_WRITE_ATTEMPTS):
        current = read_working_notes(store, session_id)
        if current is None:
            try:
                return store.create(
                    ChatWorkingNotes(
                        id=notes_id,
                        engagement_id=engagement_id,
                        session_id=session_id,
                        content=content,
                        turn_id=turn_id,
                    )
                )
            except ConflictError:  # diagnostic-expected: a concurrent first write created the record; replaced on the next attempt
                continue
        if current.engagement_id != engagement_id or current.session_id != session_id:
            raise ToolBrokerError("working notes belong to another conversation")
        try:
            return store.update(
                ChatWorkingNotes,
                notes_id,
                {"content": content, "turn_id": turn_id},
                expected_revision=current.revision,
            )
        except ConflictError:  # diagnostic-expected: another write landed first; retried against its revision
            continue
    raise ConflictError("working notes changed while they were being written")


def working_notes_status(notes: ChatWorkingNotes | None) -> WorkingNotesStatus | None:
    if notes is None:
        return None
    return WorkingNotesStatus(
        content=notes.content,
        revision=notes.revision,
        updated_at=notes.updated_at.isoformat(),
        turn_id=notes.turn_id,
    )


def _notes_payload(notes: ChatWorkingNotes) -> dict[str, Any]:
    return {
        "content": notes.content,
        "revision": notes.revision,
        "updated_at": notes.updated_at.isoformat(),
    }


def checkpoint_notes(notes: ChatWorkingNotes | None) -> dict[str, Any] | None:
    """What a turn checkpoint carries of the notes; None when there are none."""

    return _notes_payload(notes) if notes is not None and notes.content else None


def working_notes_block(notes: ChatWorkingNotes | None) -> str:
    """The notes as a request data block, or ``""`` when there are none."""

    payload = checkpoint_notes(notes)
    if payload is None:
        return ""
    return (
        WORKING_NOTES_HEADING
        + "\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def notes_write_spec() -> ToolSpec:
    return ToolSpec(
        name=NOTES_WRITE_TOOL_NAME,
        version="1",
        display_name="Working notes",
        description=(
            "Replace this conversation's working notes: your own concise, "
            "durable record of the task. Use it on long or multi-step work to "
            "keep findings (with exact paths, ids, hosts, URLs and commands), "
            "decisions, open questions and a todo list, and rewrite it as the "
            "work progresses. Each call replaces the whole text (markdown, at "
            f"most {WORKING_NOTES_MAX_BYTES} bytes); an empty string clears "
            "it. The notes stay with the conversation and return in later "
            "requests and turns, after older tool results have been cleared "
            "from the context window. They are your memory, not evidence: "
            "keep the tool_call_id or artifact id of a result you rely on."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "maxLength": WORKING_NOTES_MAX_BYTES,
                    "description": (
                        "The complete notes in markdown. They replace the "
                        "previous notes."
                    ),
                }
            },
            "required": ["content"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        # Durable conversation state, no filesystem and no network: the write
        # is confined to this conversation's own record.
        risk_class=RiskClass.WORKSPACE_WRITE,
        network_access=False,
        filesystem_access="none",
        idempotency=IdempotencyBehavior.SAFE,
        timeout_seconds=30,
    )


class WriteNotesTool(InvocationAnalysisTool):
    """``notes.write``: replace the calling conversation's working notes.

    The conversation is Core's invocation identity, never an argument, so a
    model cannot write another conversation's notes.
    """

    def __init__(self, store: NebulaStore) -> None:
        super().__init__(notes_write_spec(), self._write)
        self.store = store

    async def _write(self, invocation: ToolInvocation) -> dict[str, Any]:
        content = invocation.arguments.get("content")
        if not isinstance(content, str):
            raise InvalidToolArguments("content must be a string")
        size = len(content.encode("utf-8"))
        if size > WORKING_NOTES_MAX_BYTES:
            raise InvalidToolArguments(
                f"content is {size} bytes; working notes hold at most "
                f"{WORKING_NOTES_MAX_BYTES} bytes, so shorten them"
            )
        if not invocation.chat_session_id:
            raise ToolBrokerError("working notes belong to a conversation")
        notes = write_working_notes(
            self.store,
            engagement_id=invocation.engagement_id,
            session_id=invocation.chat_session_id,
            content=content,
            turn_id=invocation.chat_turn_id,
        )
        stored = len(notes.content.encode("utf-8"))
        return {
            "tool": NOTES_WRITE_TOOL_NAME,
            "revision": notes.revision,
            "bytes": stored,
            "detail": (
                f"Saved working notes revision {notes.revision} ({stored} of "
                f"{WORKING_NOTES_MAX_BYTES} bytes)."
                if notes.content
                else "Cleared the working notes."
            ),
        }


__all__ = [
    "NOTES_ROUTING_INSTRUCTIONS",
    "NOTES_WRITE_TOOL_NAME",
    "WORKING_NOTES_HEADING",
    "WriteNotesTool",
    "checkpoint_notes",
    "notes_write_spec",
    "read_working_notes",
    "working_notes_block",
    "working_notes_id",
    "working_notes_status",
    "write_working_notes",
]
