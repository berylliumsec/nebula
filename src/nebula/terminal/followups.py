"""Core-owned follow-up queue: add, edit, move, send now, pause and clear."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .transport import ClientError, key, terminal_text

if TYPE_CHECKING:
    from .shell import Shell

EDITABLE = {"queued", "needs_review"}
STATUS = {
    "queued": "Queued",
    "claiming": "Starting",
    "sending": "Sending",
    "needs_review": "Needs review",
    "failed": "Failed",
    "complete": "Sent",
    "cancelled": "Removed",
}


def item_text(item: dict[str, Any], width: int = 90) -> str:
    messages = (item.get("request") or {}).get("messages") or [{}]
    text = " ".join(terminal_text(messages[-1].get("content") or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def open_items(queue: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in queue.get("items", []) if item["status"] in EDITABLE]


def summary(queue: dict[str, Any]) -> list[str]:
    visible = [
        item
        for item in queue.get("items", [])
        if item["status"] not in {"complete", "cancelled"}
    ]
    lines = [
        f"Follow-up queue · {'paused' if queue.get('paused') else 'sends in order'}"
        f" · {len(visible)} waiting"
    ]
    lines.extend(
        f"  {index}. {STATUS.get(item['status'], item['status'])} · {item_text(item)}"
        + (f" — {terminal_text(item['detail'])}" if item.get("detail") else "")
        for index, item in enumerate(visible, 1)
    )
    return lines


async def write(
    shell: Shell, path: str, current: dict[str, Any], action: str, **fields: Any
) -> dict[str, Any]:
    await shell.api.request(
        "POST",
        path,
        json={"action": action, "expected_revision": current["revision"], **fields},
    )
    return await shell.api.request("GET", path)


async def queue(shell: Shell, args: list[str]) -> None:
    view = shell.current()
    path = f"chat/sessions/{key(view.session_id)}/queue"
    current = await shell.api.request("GET", path)
    for line in summary(current):
        shell.ui.say(line)
    editable = open_items(current)
    busy = bool((await shell.state()).get("busy"))
    operations = [("add", "Add a follow-up")]
    if editable:
        if busy:
            operations.append(("send_now", "Send one now (stops the running turn)"))
        operations.extend(
            [
                ("edit", "Edit a follow-up"),
                ("move", "Move a follow-up"),
                ("remove", "Remove a follow-up"),
            ]
        )
    if any(item["status"] == "needs_review" for item in editable):
        operations.append(("retry", "Retry a reviewed follow-up as new"))
    operations.append(
        ("resume", "Resume the queue")
        if current.get("paused")
        else ("pause", "Pause the queue")
    )
    if editable:
        operations.append(("clear", "Clear the queue"))
    names = {name for name, _label in operations}
    aliases = {"enqueue": "add", "reorder": "move", "now": "send_now", "show": "show"}
    action = aliases.get(args[0], args[0]) if args else None
    if action == "show":
        return
    if action is None:
        action = (
            await shell.ui.choose(
                "Follow-up queue",
                [{"id": name, "name": label} for name, label in operations]
                + [{"id": "__done", "name": "Done · return to chat"}],
            )
        )["id"]
        if action == "__done":
            return
    elif action not in names:
        raise ClientError(
            "That queue action is not available now. Open /queue to see the choices."
        )
    if action == "add":
        text = (await shell.ui.ask("Follow-up message")).strip()
        if not text:
            raise ClientError("Write the follow-up message first.")
        # While a turn runs the follow-up sends after it, as typing does; an
        # idle queue holds it for later, as the GUI's Queue for later does.
        observed = await write(
            shell,
            path,
            current,
            "enqueue",
            request=await shell.body(text),
            idempotency_key=str(uuid4()),
            paused=not busy,
        )
        shell.ui.say(
            "Follow-up saved in Core; it sends after the running turn."
            if busy
            else "Follow-up saved for later. The queue is paused; /queue resume sends it."
        )
    elif action in {"edit", "move", "remove", "send_now", "retry"}:
        choices = [
            item
            for item in editable
            if action != "retry" or item["status"] == "needs_review"
        ]
        if not choices:
            raise ClientError("No follow-up can take that action.")
        item = await shell.ui.choose(
            "Follow-up",
            [
                {**row, "name": f"{index}. {STATUS[row['status']]} · {item_text(row)}"}
                for index, row in enumerate(choices, 1)
            ],
        )
        if action == "edit":
            request = dict(item["request"])
            text = await shell.ui.ask(
                "Follow-up message", default=request["messages"][-1]["content"]
            )
            request["messages"] = [
                {**request["messages"][-1], "role": "user", "content": text}
            ]
            observed = await write(
                shell, path, current, "edit", item_id=item["id"], request=request
            )
        elif action == "remove":
            observed = await write(shell, path, current, "remove", item_id=item["id"])
        elif action == "retry":
            observed = await write(shell, path, current, "retry", item_id=item["id"])
        elif action == "move":
            order = [row["id"] for row in editable]
            index = order.index(item["id"])
            moves = [
                (name, label)
                for name, label, allowed in (
                    ("up", "Move up", index > 0),
                    ("down", "Move down", index < len(order) - 1),
                    ("first", "Send first", index > 0),
                    ("last", "Send last", index < len(order) - 1),
                )
                if allowed
            ]
            if not moves:
                raise ClientError(
                    "Only one follow-up is waiting; there is nothing to move."
                )
            move = (
                await shell.ui.choose(
                    "Move follow-up",
                    [{"id": name, "name": label} for name, label in moves],
                )
            )["id"]
            order.pop(index)
            target = {
                "up": index - 1,
                "down": index + 1,
                "first": 0,
                "last": len(order),
            }[move]
            order.insert(target, item["id"])
            observed = await write(shell, path, current, "reorder", order=order)
        else:
            await send_now(shell, path, current, item)
            return
    elif action in {"pause", "resume", "clear"}:
        if action in {"clear", "resume"} and not await shell.ui.confirm(
            "Remove every waiting follow-up?"
            if action == "clear"
            else "Resume the queue? Waiting follow-ups send in order."
        ):
            return
        observed = await write(shell, path, current, action)
    else:
        raise ClientError("Choose a listed queue action.")
    for line in summary(observed):
        shell.ui.say(line)


async def send_now(
    shell: Shell, path: str, current: dict[str, Any], item: dict[str, Any]
) -> None:
    """Stop the running turn so this follow-up is the next one Core sends."""
    state = await shell.state()
    if "stop" not in state.get("actions", []):
        raise ClientError(
            "The running turn cannot be stopped from here; the follow-up stays queued."
        )
    current = await write(shell, path, current, "pause")
    order = [row["id"] for row in open_items(current)]
    if item["id"] not in order:
        raise ClientError("That follow-up is no longer waiting. Reopen /queue.")
    order.remove(item["id"])
    current = await write(shell, path, current, "reorder", order=[item["id"], *order])
    await _stop_then_resume(shell, path)
    shell.ui.say("Stopped the running turn; that follow-up is next in Core's queue.")


async def now(shell: Shell, args: list[str]) -> None:
    """Stop and send: interrupt the running turn and send this message next."""
    view = shell.current()
    text = " ".join(args).strip() or (await shell.ui.ask("Message to send now")).strip()
    if not text:
        raise ClientError("Write the message to send.")
    state = await shell.state()
    if view.session is None or not state.get("busy"):
        await shell.send(text)
        return
    if "stop" not in state.get("actions", []):
        raise ClientError(
            "The running turn cannot be stopped from here. Send the text normally "
            "to queue it after the turn."
        )
    if view.draft.images:
        raise ClientError("Images send with a normal message after the turn finishes.")
    path = f"chat/sessions/{key(view.session_id)}/queue"
    request = await shell.body(text)
    current = await shell.api.request("GET", path)
    current = await write(shell, path, current, "pause")
    observed = await write(
        shell,
        path,
        current,
        "enqueue",
        request=request,
        idempotency_key=str(uuid4()),
        paused=True,
        first=True,
    )
    if not any(
        item_text(row) == item_text({"request": request})
        for row in open_items(observed)
    ):
        raise ClientError("Core did not confirm the message in its queue. Read /queue.")
    view.draft.text = ""
    view.draft.contexts.clear()
    view.draft.skill = None
    view.draft.skills.clear()
    if hasattr(shell.ui, "submission"):
        shell.ui.submission(text, queued=True)
    await _stop_then_resume(shell, path)
    shell.ui.say("Stopped the running turn; your message sends next.")


async def _stop_then_resume(shell: Shell, path: str) -> None:
    try:
        await shell.stop()
    finally:
        # Resume even when the stop failed so the follow-up still sends next.
        latest = await shell.api.request("GET", path)
        if latest.get("paused"):
            await write(shell, path, latest, "resume")
