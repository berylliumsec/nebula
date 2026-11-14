"""Harness activity actions and the tool-output artifact inspector."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .transport import ClientError, check_response, key, terminal_text

if TYPE_CHECKING:
    from .shell import Shell

READ_LINES = 100


def _line(value: Any, width: int = 100) -> str:
    text = " ".join(terminal_text(value or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


async def latest_harness_turn(shell: Shell) -> str | None:
    """The running harness turn, else the latest one an answer records."""
    view = shell.current()
    state = await shell.state()
    if state.get("harness_turn_id"):
        return str(state["harness_turn_id"])
    rows = await shell.api.request(
        "GET",
        f"chat/sessions/{key(view.session_id)}/messages",
        params={"limit": 50, "include_replaced": "true"},
    )
    for row in reversed(rows):
        turn = (row.get("metadata") or {}).get("harness_turn_id")
        if isinstance(turn, str):
            return turn
    return None


def harness_items(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Native subagent tasks and file checkpoints, folded to their latest state."""
    tasks: dict[str, dict[str, Any]] = {}
    checkpoints: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("item_kind") == "subagent" and event.get("item_id"):
            tasks[event["item_id"]] = {**tasks.get(event["item_id"], {}), **event}
        elif event.get("type") == "checkpoint":
            payload = event.get("payload") or {}
            identifier = payload.get("checkpoint_id") or event.get("item_id")
            # A "Files rewound" record reuses the checkpoint ID; it is not a target.
            if identifier and payload.get("action") != "rewind":
                checkpoints[identifier] = {**event, "checkpoint_id": identifier}
    return {"tasks": list(tasks.values()), "checkpoints": list(checkpoints.values())}


async def harness_actions(shell: Shell) -> None:
    """Offer the GUI's activity controls for harness-native work, when advertised."""
    view = shell.current()
    config = await shell.configuration()
    if config.get("backend") != "harness":
        return
    await shell.runtimes()
    capabilities = (shell.profiles.get(config["profile_id"]) or {}).get(
        "capabilities"
    ) or {}
    turn_id = await latest_harness_turn(shell)
    if turn_id is None:
        return
    listing = await shell.api.request(
        "GET",
        f"harness-turns/{key(turn_id)}/events",
        params={"after": 0, "limit": 2000},
    )
    found = harness_items(listing.get("events") or [])
    options: list[dict[str, Any]] = []
    if capabilities.get("subagent_control"):
        # Core's own `stoppable` covers queued, running and streaming tasks.
        options.extend(
            {
                "id": "task:" + row["item_id"],
                "name": "Stop subagent · "
                + _line(
                    row.get("title")
                    or (row.get("payload") or {}).get("task_type")
                    or row["item_id"],
                    70,
                )
                + f" · {row.get('item_status') or 'running'}",
                "task": row,
            }
            for row in found["tasks"]
            if (row.get("payload") or {}).get("stoppable")
        )
    if capabilities.get("checkpoint_rewind"):
        options.extend(
            {
                "id": "checkpoint:" + row["checkpoint_id"],
                "name": "Rewind files to "
                + _line(row.get("title") or "checkpoint", 50)
                + f" · {terminal_text(row.get('created_at') or '')}",
                "checkpoint": row,
            }
            for row in found["checkpoints"]
        )
    if not options:
        return
    options.append({"id": "__done", "name": "Done"})
    chosen = await shell.ui.choose("Harness activity", options)
    if chosen["id"] == "__done":
        return
    if "task" in chosen:
        task = chosen["task"]
        await shell.api.request(
            "POST",
            f"harness-turns/{key(task.get('harness_turn_id') or turn_id)}/tasks/"
            f"{key(task['item_id'])}/stop",
        )
        shell.ui.say(
            "Asked the harness to stop that subagent; /activity shows its state."
        )
        return
    if (await shell.state()).get("busy"):
        raise ClientError("Checkpoint rewind is available while the session is idle.")
    if not await shell.ui.confirm(
        "Rewind files to this checkpoint? The harness restores tracked files; "
        "this is available only while the session is idle."
    ):
        return
    session_id = chosen["checkpoint"].get("harness_session_id") or (
        view.session or {}
    ).get("harness_session_id")
    if not session_id:
        raise ClientError("Core did not report this conversation's harness session.")
    await shell.api.request(
        "POST",
        f"harness-sessions/{key(session_id)}/checkpoints/rewind",
        json={"checkpoint_id": chosen["checkpoint"]["checkpoint_id"]},
    )
    shell.ui.say("Files rewound to the checkpoint.")


async def results(shell: Shell, args: list[str]) -> None:
    """Browse retained results; tool results open the artifact inspector."""
    view = shell.current()
    items: list[dict[str, Any]] = []
    offset: int | None = 0

    async def load() -> None:
        nonlocal offset
        value = await shell.api.request(
            "GET",
            f"chat/sessions/{key(view.session_id)}/results",
            params={"offset": offset, "limit": 40},
        )
        items.extend(value.get("items") or [])
        offset = value.get("next_offset")

    await load()
    if not items:
        shell.ui.say("No retained results in this conversation yet.")
        return
    while True:
        options = [
            {
                **row,
                "name": f"{row.get('kind', 'result')} · "
                f"{_line(row.get('label') or row.get('text'), 90)}"
                + (" · inspect output" if row.get("tool_call_id") else ""),
            }
            for row in items
        ]
        if offset is not None:
            options.append({"id": "__more", "name": "Earlier results"})
        options.append({"id": "__done", "name": "Done"})
        chosen = await shell.ui.choose("Retained results", options)
        if chosen["id"] == "__done":
            return
        if chosen["id"] == "__more":
            await load()
        elif chosen.get("tool_call_id"):
            await inspect_tool_call(shell, chosen["tool_call_id"])
        else:
            shell.ui.say(
                f"{chosen.get('kind', 'result')} · {terminal_text(chosen.get('label') or '')}"
            )
            if chosen.get("text"):
                shell.ui.say(terminal_text(chosen["text"]))


async def inspect_tool_call(shell: Shell, tool_call_id: str) -> None:
    artifacts = await shell.api.request(
        "GET", f"tool-calls/{key(tool_call_id)}/artifacts"
    )
    if not artifacts:
        shell.ui.say("This tool result kept no output artifacts.")
        return
    options = [
        {
            **row,
            "name": f"{_line(row.get('filename') or row['id'], 60)} · "
            f"{row.get('size', 0):,} bytes"
            + (
                " · searchable"
                if (row.get("metadata") or {}).get("searchable", True)
                else " · binary"
            ),
        }
        for row in artifacts
    ]
    options.append({"id": "__search", "name": "Search all output of this tool call"})
    options.append({"id": "__done", "name": "Done"})
    while True:
        chosen = await shell.ui.choose("Tool output", options)
        if chosen["id"] == "__done":
            return
        if chosen["id"] == "__search":
            await search_output(shell, tool_call_id)
            continue
        await artifact_actions(shell, chosen)


async def artifact_actions(shell: Shell, artifact: dict[str, Any]) -> None:
    searchable = (artifact.get("metadata") or {}).get("searchable", True)
    actions = []
    if searchable:
        actions.append({"id": "read", "name": "Read lines"})
    actions.append(
        {"id": "save", "name": "Save the raw output to a file on this client"}
    )
    actions.append({"id": "back", "name": "Back"})
    action = (await shell.ui.choose(_line(artifact.get("filename"), 60), actions))["id"]
    if action == "read":
        raw = (await shell.ui.ask("Start at line", default="1")).strip() or "1"
        await read_output(shell, artifact["id"], starting_line=max(1, int(raw)))
    elif action == "save":
        await save_output(shell, artifact)


async def read_output(
    shell: Shell, artifact_id: str, *, starting_line: int = 1
) -> None:
    body: dict[str, Any] = {"starting_line": starting_line, "line_count": READ_LINES}
    while True:
        page = await shell.api.request(
            "POST", f"artifacts/{key(artifact_id)}/output/read", json=body
        )
        if page.get("searchable") is False:
            shell.ui.say("This output is not text; save the raw file to inspect it.")
            return
        for row in page.get("lines") or []:
            text = row.get("text") if isinstance(row, dict) else row
            number = row.get("line") if isinstance(row, dict) else ""
            shell.ui.say(
                f"{number:>6}  {terminal_text(text)}" if number else terminal_text(text)
            )
        continuation = page.get("continuation") or {}
        if not continuation or not await shell.ui.confirm("Read the next lines?"):
            return
        # Core continues by line or, inside a long line, with an opaque cursor.
        body = (
            {"starting_line": continuation["starting_line"], "line_count": READ_LINES}
            if continuation.get("starting_line")
            else {
                "continuation_cursor": continuation.get("continuation_cursor")
                or page.get("continuation_cursor"),
                "starting_line": page.get("starting_line") or 1,
                "line_count": READ_LINES,
            }
        )


async def search_output(shell: Shell, tool_call_id: str) -> None:
    query = (await shell.ui.ask("Search this tool output")).strip()
    if not query:
        return
    body: dict[str, Any] = {
        "query": query[:512],
        "mode": "literal",
        "case_sensitive": False,
        "context_lines": 1,
        "match_limit": 20,
    }
    while True:
        result = await shell.api.request(
            "POST", f"tool-calls/{key(tool_call_id)}/output/search", json=body
        )
        matches = result.get("matches") or []
        if not matches:
            shell.ui.say(f"No matches for “{_line(query, 40)}”.")
        for match in matches:
            shell.ui.say(
                f"{_line(match.get('filename'), 40)} · line {match.get('line')}"
            )
            for row in match.get("context") or []:
                shell.ui.say(f"{row.get('line'):>6}  {terminal_text(row.get('text'))}")
        for skipped in result.get("skipped") or []:
            shell.ui.say(
                f"Skipped {skipped.get('artifact_id')}: {skipped.get('reason')}"
            )
        cursor = result.get("continuation_cursor")
        if not cursor or not await shell.ui.confirm("Show more matches?"):
            return
        body["cursor"] = cursor


async def save_output(shell: Shell, artifact: dict[str, Any]) -> None:
    if not await shell.ui.confirm(
        "Save the raw tool output? It may contain sensitive data from the tool."
    ):
        return
    default = Path(artifact.get("filename") or f"{artifact['id']}.out").name
    destination = Path(
        (
            await shell.ui.ask(
                "Save to file on this client", default=default, path=True
            )
        ).strip()
    ).expanduser()
    if destination.exists() and not await shell.ui.confirm(f"Replace {destination}?"):
        return
    response = await shell.api.client.get(
        f"artifacts/{key(artifact['id'])}/content",
        headers={
            "X-Nebula-Sensitive-Data-Acknowledged": "true",
            "X-Nebula-Operation-ID": str(uuid4()),
        },
    )
    check_response(response)
    destination.write_bytes(response.content)
    truncated = response.headers.get("x-nebula-artifact-truncated") == "true"
    shell.ui.say(
        f"Saved {len(response.content):,} bytes to {destination}"
        + (" (Core kept a truncated copy)." if truncated else ".")
    )
