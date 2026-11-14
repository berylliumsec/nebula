"""Conversation search, bookmarks, side chats and bulk actions over Core's API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .state import main_chat
from .transport import ApiError, ClientError, key, terminal_text

if TYPE_CHECKING:
    from .shell import Shell

FLAGS = ("red", "orange", "yellow", "green", "blue", "purple")
BULK_ACTIONS = {
    "archive": "Archive",
    "restore": "Restore from archive",
    "pin": "Pin to top",
    "unpin": "Unpin",
    "flag": "Flag",
    "clear_flag": "Clear flag",
    "delete": "Delete permanently",
}


def chat_markers(row: dict[str, Any]) -> str:
    """The pinned, flag and archive state the GUI shows beside a title."""
    meta = row.get("metadata") or {}
    archived = bool(meta.get("archived_at") or row.get("archived_at"))
    pinned = bool(meta.get("pinned") or row.get("pinned"))
    flag = meta.get("flag_color") or row.get("flag_color")
    parts = []
    if pinned and not archived:
        parts.append("pinned")
    if flag in FLAGS:
        parts.append(f"{flag} flag")
    if archived:
        parts.append("archived")
    return " · ".join(parts)


def chat_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pinned first, archived last; Core's recent-first order otherwise."""

    def rank(row: dict[str, Any]) -> int:
        meta = row.get("metadata") or {}
        if meta.get("archived_at"):
            return 2
        return 0 if meta.get("pinned") else 1

    return sorted(rows, key=rank)


async def all_conversations(shell: Shell) -> list[dict[str, Any]]:
    view = shell.current()
    rows: list[dict[str, Any]] = []
    while True:
        page = await shell.api.page(
            "chat-sessions",
            offset=len(rows),
            limit=500,
            engagement_id=view.project_id,
            recent_first="true",
        )
        rows.extend(page)
        if len(page) < 500:
            return [row for row in rows if main_chat(row)]


def _excerpt(value: Any, width: int = 110) -> str:
    text = " ".join(terminal_text(value or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


async def search(shell: Shell, args: list[str]) -> None:
    """Search saved messages, then open the conversation at the match."""
    view = shell.current()
    query = " ".join(args).strip()
    if not query:
        query = (await shell.ui.ask("Search saved messages")).strip()
    scope_chat = view.session_id if view.session is not None else None
    bookmarked = False
    while True:
        offset: int | None = 0

        async def page() -> list[dict[str, Any]]:
            nonlocal offset
            if offset is None:
                return []
            params: dict[str, Any] = {"q": query, "offset": offset, "limit": 50}
            if scope_chat:
                params["session_id"] = scope_chat
            if bookmarked:
                params["bookmarked"] = "true"
            result = await shell.api.request(
                "GET",
                f"chat/projects/{key(view.project_id)}/search",
                params=params,
            )
            offset = result.get("next_offset")
            return [
                {
                    **item,
                    "id": item["message_id"],
                    "name": f"{_excerpt(item.get('title'), 40)} · "
                    f"{item.get('role', 'message')}: {_excerpt(item.get('excerpt'))}",
                }
                for item in result.get("items", [])
            ]

        if not query and not bookmarked:
            raise ClientError(
                "Type words to search for, or choose bookmarked messages."
            )
        if hasattr(shell.ui, "begin_loading"):
            shell.ui.begin_loading("Searching saved messages…")
        rows = await page()
        where = "this conversation" if scope_chat else "all project conversations"
        controls = [
            {
                "id": "__scope",
                "name": "Search all project conversations"
                if scope_chat
                else "Search only this conversation",
            }
            if view.session is not None
            else None,
            {
                "id": "__bookmarks",
                "name": "Show all matches"
                if bookmarked
                else "Bookmarked messages only",
            },
        ]
        items = [row for row in controls if row is not None] + rows
        if not rows:
            items.insert(0, {"id": "__none", "name": f"No matches in {where}."})
        title = (
            ("Bookmarked " if bookmarked else "")
            + f"messages in {where}"
            + (f" matching “{_excerpt(query, 40)}”" if query else "")
        )
        chosen = await shell.ui.choose(
            title, items, more=page if offset is not None else None
        )
        if chosen["id"] == "__scope":
            scope_chat = None if scope_chat else view.session_id
            continue
        if chosen["id"] == "__bookmarks":
            bookmarked = not bookmarked
            continue
        if chosen["id"] == "__none":
            return
        break
    if view.session is None or chosen["session_id"] != view.session_id:
        session = await shell.api.request(
            "GET", f"chat-sessions/{key(chosen['session_id'])}"
        )
        await shell.open_view(view.project, session)
    if not shell.ui.reveal(chosen.get("excerpt") or ""):
        shell.ui.say(
            f"Match in {terminal_text(chosen.get('title'))} · "
            f"{chosen.get('role', 'message')}:\n{terminal_text(chosen.get('excerpt'))}"
        )
        shell.ui.say("/history more reads earlier messages around it.")


async def bookmark(shell: Shell, args: list[str]) -> None:
    """Bookmark a saved message, or remove its bookmark, with a Core readback."""
    from .workflows import message_picker

    view = shell.current()
    path = f"chat/sessions/{key(view.session_id)}/bookmarks"
    marks = {row["message_id"]: row for row in await shell.api.request("GET", path)}
    message = await message_picker(
        shell,
        label=lambda row: (
            ("★ " if (marks.get(row["id"]) or {}).get("active") else "  ")
            + f"{row['role']}: {_excerpt(row.get('content'), 100)}"
        ),
    )
    current = marks.get(message["id"])
    active = not bool(current and current.get("active"))
    await shell.api.request(
        "PUT",
        f"{path}/{key(message['id'])}",
        json={
            "active": active,
            "expected_revision": current["revision"] if current else 0,
        },
    )
    observed = {
        row["message_id"]: row for row in await shell.api.request("GET", path)
    }.get(message["id"])
    if bool(observed and observed.get("active")) != active:
        raise ClientError("Core did not confirm the bookmark. Reopen /bookmark.")
    shell.ui.say(
        ("Bookmarked" if active else "Bookmark removed from")
        + f" {message['role']} message: {_excerpt(message.get('content'), 80)}"
        + (" · /search finds bookmarked messages." if active else "")
    )


def _side_parent(shell: Shell) -> dict[str, Any]:
    view = shell.current()
    if view.session is None:
        raise ClientError(
            "Send a message first; side chats branch from a saved conversation."
        )
    meta = view.session.get("metadata") or {}
    if meta.get("side_chat"):
        if view.parent is None:
            raise ClientError(
                "This side chat's main conversation is unavailable. Use /chats."
            )
        return view.parent
    if not main_chat(view.session):
        raise ClientError("Side chats branch from main conversations.")
    return view.session


async def side(shell: Shell, args: list[str]) -> None:
    """Open, create, leave or close a side chat of the current main conversation."""
    view = shell.current()
    parent = _side_parent(shell)
    inside = view.session is not parent
    listing = await shell.api.request(
        "GET", f"chat/sessions/{key(parent['id'])}/side-chats"
    )
    options: list[dict[str, Any]] = []
    if inside:
        options.append({"id": "__back", "name": "Back to main chat"})
        options.append({"id": "__close", "name": "Close this side chat (discard it)"})
    options.append({"id": "__new", "name": "New side chat"})
    options.extend(
        {**row, "name": _excerpt(row.get("title") or "Side chat", 80)}
        for row in listing
        if not inside or row["id"] != view.session_id
    )
    action = args[0] if args else None
    if action == "new":
        chosen = {"id": "__new"}
    elif action in {"back", "close"} and inside:
        chosen = {"id": "__" + action}
    else:
        chosen = await shell.ui.choose(
            "Side chats of " + _excerpt(parent.get("title"), 60), options
        )
    if chosen["id"] == "__back":
        await shell.open_view(view.project, parent)
        return
    if chosen["id"] == "__close":
        if not await shell.ui.confirm(
            "Close and discard this side chat? The main conversation is unchanged."
        ):
            return
        closing = view.session_id
        try:
            await shell.api.request("DELETE", f"chat/temporary-sessions/{key(closing)}")
        except ApiError as exc:
            if exc.status != 404:
                raise
        remaining = await shell.api.request(
            "GET", f"chat/sessions/{key(parent['id'])}/side-chats"
        )
        if any(row["id"] == closing for row in remaining):
            raise ClientError(
                "Core did not confirm the side chat closed. Retry /side close."
            )
        await shell.open_view(view.project, parent)
        shell.ui.say("Side chat closed. The main conversation is unchanged.")
        return
    if chosen["id"] == "__new":
        title = f"Side chat #{len(listing) + 1} · {terminal_text(parent.get('title') or 'Conversation')}"
        created = await shell.api.request(
            "POST",
            f"chat/sessions/{key(parent['id'])}/fork",
            json={"title": title[:300], "side_chat": True, "through_latest": True},
        )
        await shell.open_view(view.project, created, parent=parent)
        shell.ui.say(
            "Side chat opened with the main conversation's history. Tools, MCP and "
            "attachments stay off here; /side returns to the main chat."
        )
        return
    await shell.open_view(view.project, chosen, parent=parent)


async def bulk(shell: Shell) -> None:
    """Apply one action to several saved conversations, then verify each in Core."""
    rows = chat_order(await all_conversations(shell))
    if not rows:
        raise ClientError("This project has no saved conversations.")
    options = [{"id": "__all", "name": f"Select all {len(rows)} conversations"}]
    options.extend(
        {
            "id": row["id"],
            "name": _excerpt(row.get("title") or "Conversation", 70)
            + (f" · {chat_markers(row)}" if chat_markers(row) else ""),
        }
        for row in rows
    )
    chosen = await shell.ui.multiple("Select conversations", options, [])
    ids = [row["id"] for row in rows] if "__all" in chosen else chosen
    if not ids:
        shell.ui.say("No conversations selected.")
        return
    action = (
        await shell.ui.choose(
            f"Action for {len(ids)} conversation{'s' if len(ids) != 1 else ''}",
            [{"id": name, "name": label} for name, label in BULK_ACTIONS.items()],
        )
    )["id"]
    color = None
    if action == "flag":
        color = (
            await shell.ui.choose(
                "Flag colour", [{"id": item, "name": item.title()} for item in FLAGS]
            )
        )["id"]
    if action == "delete":
        answer = await shell.ui.ask(
            f"Permanently delete {len(ids)} conversation{'s' if len(ids) != 1 else ''}? "
            f"Type delete {len(ids)} to confirm"
        )
        if answer.strip().casefold() != f"delete {len(ids)}":
            shell.ui.say("Deletion cancelled. Nothing changed.")
            return
    by_id = {row["id"]: row for row in rows}
    done, failed = [], []
    current = shell.view
    for identifier in ids:
        try:
            if action == "delete":
                try:
                    await shell.api.request(
                        "DELETE", f"chat-sessions/{key(identifier)}"
                    )
                except ApiError as exc:
                    if exc.status != 404:
                        raise
            else:
                patch: dict[str, Any] = {
                    "expected_revision": by_id[identifier]["revision"]
                }
                if action in {"archive", "restore"}:
                    patch["archived"] = action == "archive"
                elif action in {"pin", "unpin"}:
                    patch["pinned"] = action == "pin"
                else:
                    patch["flag_color"] = color
                await shell.api.request(
                    "PATCH", f"chat-sessions/{key(identifier)}", json=patch
                )
            done.append(identifier)
        except (ClientError, ValueError) as exc:
            failed.append((identifier, str(exc)))
    # Verify from Core rather than trusting each response.
    observed = {row["id"]: row for row in await all_conversations(shell)}
    unconfirmed = []
    for identifier in done:
        row = observed.get(identifier)
        meta = (row or {}).get("metadata") or {}
        confirmed = {
            "delete": row is None,
            "archive": bool(meta.get("archived_at")),
            "restore": row is not None and not meta.get("archived_at"),
            "pin": bool(meta.get("pinned")),
            "unpin": row is not None and not meta.get("pinned"),
            "flag": meta.get("flag_color") == color,
            "clear_flag": row is not None and not meta.get("flag_color"),
        }[action]
        if not confirmed:
            unconfirmed.append(identifier)
    ok = len(done) - len(unconfirmed)
    shell.ui.say(
        f"{BULK_ACTIONS[action]}: {ok} of {len(ids)} conversation"
        f"{'s' if len(ids) != 1 else ''} confirmed in Core."
    )
    for identifier, error in failed:
        shell.ui.say(
            f"Failed · {_excerpt(by_id[identifier].get('title'), 50)}: {error}"
        )
    for identifier in unconfirmed:
        shell.ui.say(
            f"Not confirmed · {_excerpt(by_id[identifier].get('title'), 50)}; refresh /chats before retrying."
        )
    if (
        current is not None
        and current.session is not None
        and action == "delete"
        and current.session_id in done
        and current.session_id not in observed
    ):
        await shell.open_view(current.project, None)
    elif current is not None and current.session is not None:
        fresh = observed.get(current.session_id)
        if fresh is not None:
            current.session = fresh


def side_chat_body(body: dict[str, Any]) -> dict[str, Any]:
    """A side chat is a conversation only: no tools, MCP, hooks or attachments."""
    for name in (
        "mcp_server_ids",
        "hook_ids",
        "ssh_environment_ids",
    ):
        if name in body:
            body[name] = []
    for name in (
        "context_attachments",
        "skill",
        "harness_skill",
        "skill_selections",
        "goal_id",
    ):
        body.pop(name, None)
    body["tools_enabled"] = False
    body["allow_subagents"] = False
    body["allow_agent_messaging"] = False
    return body
