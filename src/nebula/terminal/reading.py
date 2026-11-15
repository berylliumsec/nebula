"""Catch-up, quoting, supporting evidence and turn details over Core's chat API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .transport import ApiError, ClientError, key, terminal_text

if TYPE_CHECKING:
    from .shell import Shell
    from .state import View

CATCHUP_PREVIEW = 5
KIND_LABELS = {
    "failure": "Failed response",
    "results": "New results",
    "completed": "New answer",
    "pending": "Needs your action",
}


def _line(value: Any, width: int = 110) -> str:
    text = " ".join(terminal_text(value or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def device_id(shell: Shell) -> str:
    """This paired terminal's device; Core uses the authenticated one anyway."""
    return str(shell.settings.entry.get("device_id") or "nebula-terminal")


async def read_catchup(shell: Shell, view: View | None = None) -> dict[str, Any]:
    view = view or shell.current()
    return await shell.api.request(
        "GET",
        f"chat/sessions/{key(view.session_id)}/catch-up",
        params={"device_id": device_id(shell)},
    )


async def acknowledge(
    shell: Shell, record: dict[str, Any], view: View | None = None
) -> None:
    view = view or shell.current()
    await shell.api.request(
        "PUT",
        f"chat/sessions/{key(view.session_id)}/read-cursor",
        json={
            "device_id": device_id(shell),
            "expected_revision": record.get("revision") or 0,
            "through_at": record["through_at"],
        },
    )
    view.catchup = "current"


async def mark_seen(shell: Shell, view: View) -> None:
    """An answer watched live is read, as at the bottom of the GUI's transcript.

    Only a conversation whose catch-up was read on open is marked. While its
    summary waits for /catchup, the cursor stays put so the summary still lists
    everything since the last read, as the GUI's card does.
    """
    if view.catchup != "current" or view.session is None:
        return
    try:
        record = await read_catchup(shell, view)
        if (
            isinstance(record, dict)
            and record.get("through_at")
            and (not record.get("initialized") or record.get("items"))
        ):
            await acknowledge(shell, record, view)
    except (ClientError, ValueError):
        return  # A later open shows the catch-up instead.


async def catchup_on_open(shell: Shell) -> None:
    """Summarize what changed since this device last read the conversation."""
    try:
        record = await read_catchup(shell)
        if not isinstance(record, dict) or not record.get("through_at"):
            return
        if not record.get("initialized"):
            # First visit from this device: start its read cursor, as the GUI does.
            await acknowledge(shell, record)
            return
    except (ClientError, ValueError):
        return  # Catch-up is a convenience; the transcript is already loaded.
    items = record.get("items") or []
    pending = record.get("pending") or []
    shell.current().catchup = "waiting" if items else "current"
    if not items:
        return
    shell.ui.say(
        f"While you were away: {len(items)} update{'s' if len(items) != 1 else ''}"
        + (f", {len(pending)} needing your action" if pending else "")
        + (" (showing the latest 50)" if record.get("truncated") else "")
        + "."
    )
    for item in items[:CATCHUP_PREVIEW]:
        shell.ui.say(
            f"  {KIND_LABELS.get(item.get('kind'), 'Update')} · {_line(item.get('text'))}"
        )
    shell.ui.say("/catchup reviews them; /catchup read marks them as read.")


async def catchup(shell: Shell, args: list[str]) -> None:
    record = await read_catchup(shell)
    items = record.get("items") or []
    if args and args[0] == "read" or not items:
        await acknowledge(shell, record)
        shell.ui.say(
            "Marked this conversation as read on this device."
            if items
            else "Nothing new since this device last read this conversation."
        )
        return
    options = [{"id": "__read", "name": "Mark all as read"}]
    options.extend(
        {
            **item,
            "id": f"item:{index}",
            "name": f"{KIND_LABELS.get(item.get('kind'), 'Update')} · {_line(item.get('text'), 90)}",
        }
        for index, item in enumerate(items)
    )
    chosen = await shell.ui.choose("Since you last read this conversation", options)
    await acknowledge(shell, record)
    if chosen["id"] == "__read":
        shell.ui.say("Marked this conversation as read on this device.")
        return
    if chosen.get("kind") == "pending":
        shell.ui.say("This needs your action: /approvals or /input answers it.")
    elif chosen.get("kind") == "failure" and chosen.get("turn_id"):
        await show_turn(shell, chosen["turn_id"])
    elif chosen.get("message_id"):
        if not shell.ui.reveal(chosen.get("text") or ""):
            shell.ui.say(terminal_text(chosen.get("text") or ""))
    elif chosen.get("turn_id"):
        await show_turn(shell, chosen["turn_id"])


def answer_text(message: dict[str, Any]) -> str:
    """The saved answer without its progress prose, as the GUI quotes it."""
    content = message.get("content") or ""
    metadata = message.get("metadata") or {}
    prefix = metadata.get("progress_prefix_utf16_length")
    if message.get("role") != "assistant" or not isinstance(prefix, int) or prefix <= 0:
        return content
    encoded = content.encode("utf-16-le")
    if prefix * 2 >= len(encoded):
        return content
    head = encoded[: prefix * 2].decode("utf-16-le", errors="strict")
    rest = encoded[prefix * 2 :].decode("utf-16-le", errors="strict")
    # Only a clean paragraph boundary outside a code fence separates them.
    if not rest.startswith("\n\n") or head.count("```") % 2:
        return content
    return rest[2:]


async def quote(shell: Shell, args: list[str]) -> None:
    from .workflows import message_picker

    view = shell.current()
    message = await message_picker(
        shell,
        label=lambda row: f"{row['role']}: {_line(answer_text(row), 100)}",
    )
    text = answer_text(message)
    if not text.strip():
        raise ClientError("That message has no text to quote.")
    quoted = "\n".join("> " + line for line in text.split("\n"))
    current = view.draft.text
    view.draft.text = (current + "\n\n" if current.strip() else "") + quoted + "\n\n"
    shell.ui.say("Quoted into your draft. Add your reply below the quote and send it.")


async def evidence(shell: Shell, args: list[str]) -> None:
    from .workflows import message_picker

    view = shell.current()
    message = await message_picker(
        shell,
        label=lambda row: f"{row['role']}: {_line(answer_text(row), 100)}",
    )
    if message.get("role") != "assistant":
        raise ClientError("Supporting evidence belongs to assistant answers.")
    record = await shell.api.request(
        "GET",
        f"chat/sessions/{key(view.session_id)}/messages/{key(message['id'])}/evidence",
    )
    shell.ui.say(f"Supporting evidence for: {_line(answer_text(message), 90)}")
    retrieved = record.get("retrieved") or []
    observations = record.get("observations") or []
    artifacts = record.get("artifacts") or []
    limitations = record.get("limitations") or []
    if not (retrieved or observations or artifacts):
        shell.ui.say("No retrieved sources, observations or artifacts were recorded.")
    for row in retrieved:
        page = f", page {row['page']}" if row.get("page") else ""
        shell.ui.say(
            f"  Source · {_line(row.get('name'), 60)}{page}: {_line(row.get('excerpt'), 160)}"
        )
    for row in observations:
        detail = row.get("summary") or row.get("status") or ""
        shell.ui.say(
            f"  {str(row.get('kind') or 'observed').title()} · {_line(row.get('label'), 60)}"
            + (f": {_line(detail, 120)}" if detail else "")
        )
    for row in artifacts:
        shell.ui.say(f"  Artifact · {_line(row.get('label'), 60)} ({row.get('kind')})")
    for line in limitations:
        shell.ui.say(f"  Limitation · {_line(line, 160)}")
    if artifacts or observations:
        shell.ui.say("/results lists retained results for this conversation.")


async def show_turn(shell: Shell, turn_id: str) -> None:
    view = shell.current()
    try:
        summary = await shell.api.request(
            "GET",
            f"chat/sessions/{key(view.session_id)}/turns/{key(turn_id)}/summary",
        )
    except ApiError as exc:
        if exc.status == 404:
            raise ClientError(
                "That response is not part of this conversation."
            ) from exc
        raise
    shell.ui.say(
        f"Response {str(summary.get('status')).replace('_', ' ')}"
        f" · {terminal_text(summary.get('model') or 'model unknown')}"
        f" · started {terminal_text(summary.get('created_at') or '')}"
        f" · updated {terminal_text(summary.get('updated_at') or '')}"
    )
    if summary.get("error"):
        shell.ui.say("Error: " + terminal_text(summary["error"]))
    if summary.get("message_id"):
        rows = await shell.api.request(
            "GET",
            f"chat/sessions/{key(view.session_id)}/messages",
            params={"limit": 50, "include_replaced": "true"},
        )
        source = next((row for row in rows if row["id"] == summary["message_id"]), None)
        if source is not None and not shell.ui.reveal(source.get("content") or ""):
            shell.ui.say(
                f"Source message ({source['role']}): {_line(source.get('content'), 200)}"
            )
    else:
        shell.ui.say("No transcript message was retained for this response.")


async def turn(shell: Shell, args: list[str]) -> None:
    from .workflows import message_picker

    message = await message_picker(
        shell,
        label=lambda row: f"{row['role']}: {_line(answer_text(row), 100)}",
    )
    turn_id = (message.get("metadata") or {}).get("chat_turn_id")
    if not isinstance(turn_id, str):
        raise ClientError("Core recorded no response details for that message.")
    await show_turn(shell, turn_id)
