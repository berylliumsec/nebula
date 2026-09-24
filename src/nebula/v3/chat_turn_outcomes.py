"""How a provider turn that ended without an answer reads in the history.

A failed or stopped turn leaves the operator's message unanswered. Replayed
as it is, the next request carries two user messages in a row, which strict
chat templates and Bedrock Converse refuse, and the model never learns which
tools the turn already ran, so it may run a side-effecting command again.
Core records a short note in the turn's place instead: what ended the turn and
which tool steps completed, bounded and redacted (opencode keeps a stopped
turn's completed tool results and marks the rest interrupted; Codex patches
missing outputs as "aborted").
"""

from __future__ import annotations

import json
import re
from typing import Any

from .domain import ChatMessage, ChatTurn, ChatTurnStatus
from .providers import ModelMessage
from .redaction import redact_text

TURN_OUTCOME_KIND = "turn_outcome"

# The UI already shows a saved message with this finish reason as stopped
# (#491), so the note reads as the turn's outcome rather than an answer.
TURN_OUTCOME_FINISH_REASON = "interrupted"

_STEP_LIMIT = 12
_REASON_CHARS = 300
_ARGUMENTS_CHARS = 160
_SUMMARY_CHARS = 160
_NOTE_CHARS = 6_000
_OPERATOR_STOP = "response stopped"


def is_turn_outcome(message: ChatMessage) -> bool:
    return message.metadata.get("kind") == TURN_OUTCOME_KIND


def _clip(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", redact_text(value)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?", "…")) else text + "."


def _step_line(entry: dict[str, Any]) -> str:
    name = _clip(str(entry.get("name") or "tool"), 80)
    arguments = entry.get("arguments")
    rendered = (
        _clip(
            json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str),
            _ARGUMENTS_CHARS,
        )
        if arguments
        else ""
    )
    label = f"{name} {rendered}".rstrip()
    status = str(entry.get("status") or "")
    if entry.get("provider_result") is not None:
        summary = _clip(
            str(entry.get("result_summary") or status or "done"), _SUMMARY_CHARS
        )
        outcome = summary if status in {"", "complete"} else f"{status}: {summary}"
    elif status == "waiting_approval":
        outcome = "did not run: it was waiting for approval"
    elif status == "waiting_callback":
        outcome = "started; its result had not arrived when the response ended"
    else:
        outcome = "outcome unknown: the response ended before it was recorded"
    return f"- {label} → {outcome}"


def turn_outcome_text(
    turn: ChatTurn, *, history: list[dict[str, Any]] | None = None
) -> str:
    """The note Core saves for a turn that failed or stopped before answering."""

    reason = _clip(turn.error or "", _REASON_CHARS)
    if turn.status == ChatTurnStatus.CANCELLED:
        if not reason or reason.casefold() == _OPERATOR_STOP:
            reason = "the operator stopped it before it finished"
        lines = [_sentence(f"Response stopped: {reason}")]
    else:
        lines = [
            _sentence(
                f"Response failed: {reason or 'the response could not be completed'}"
            )
        ]
    resolution = turn.request_snapshot.get("completion_hook_resolution")
    if isinstance(resolution, dict):
        candidate = _clip(str(resolution.get("candidate") or ""), 3_500)
        if candidate:
            lines.extend(
                [
                    "Model decision after completion-hook feedback "
                    "(not accepted as completion):",
                    candidate,
                ]
            )
    steps = [
        entry
        for entry in (turn.tool_history if history is None else history)
        if isinstance(entry, dict)
    ]
    omitted = max(0, len(steps) - _STEP_LIMIT)
    shown = steps[omitted:]
    completed = [entry for entry in shown if entry.get("provider_result") is not None]
    unfinished = [entry for entry in shown if entry.get("provider_result") is None]
    if omitted:
        lines.append(f"({omitted} earlier tool steps are not listed.)")
    if completed:
        lines.append("Completed tool steps:")
        lines.extend(_step_line(entry) for entry in completed)
    if unfinished:
        lines.append("Not completed:")
        lines.extend(_step_line(entry) for entry in unfinished)
    note = "\n".join(lines)
    return note if len(note) <= _NOTE_CHARS else note[: _NOTE_CHARS - 1] + "…"


def turn_outcome_metadata(
    turn: ChatTurn, *, history: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Metadata in the shape of a completed turn's answer, plus the outcome."""

    return {
        "kind": TURN_OUTCOME_KIND,
        "chat_turn_id": turn.id,
        "turn_status": turn.status.value,
        "interrupted": True,
        "tool_call_ids": [
            str(item["tool_call_id"])
            for item in (turn.tool_history if history is None else history)
            if isinstance(item, dict) and item.get("tool_call_id")
        ],
        "tool_results": [
            {
                "tool_call_id": item.get("tool_call_id"),
                "capability": item.get("name"),
                "display_name": item.get("display_name"),
                "status": item.get("status"),
                "summary": item.get("result_summary"),
                "evidence_ids": item.get("evidence_ids", []),
                "result_artifact_id": item.get("result_artifact_id"),
                "artifacts": item.get("artifacts", []),
            }
            for item in (turn.tool_history if history is None else history)
            if isinstance(item, dict)
        ],
    }


def _parts(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else content


def join_consecutive_assistant_messages(
    messages: list[ModelMessage],
) -> list[ModelMessage]:
    """Fold assistant messages that follow one another into one turn.

    Subagent reports and messages are posted to a provider parent after its
    own answer (or after an outcome note), and a request with two assistant
    messages in a row is refused by Bedrock and strict chat templates. The
    transcript keeps each post as its own message for the UI; only the model
    request joins them onto the answer they followed, as LiteLLM does for
    Bedrock.
    """

    joined: list[ModelMessage] = []
    for message in messages:
        previous = joined[-1] if joined else None
        if (
            previous is None
            or previous.role != "assistant"
            or message.role != "assistant"
        ):
            joined.append(message)
            continue
        if isinstance(previous.content, str) and isinstance(message.content, str):
            content: str | list[dict[str, Any]] = "\n\n".join(
                text for text in (previous.content, message.content) if text
            )
        else:
            content = [
                *_parts(previous.content),
                {"type": "text", "text": "\n\n"},
                *_parts(message.content),
            ]
        joined[-1] = ModelMessage(role="assistant", content=content)
    return joined
