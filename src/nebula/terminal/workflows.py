"""Saved-chat, work and pending-action workflows over the public Core API."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .followups import now, queue
from .state import Cursor, visible_message
from .transport import ApiError, ClientError, key, terminal_text

if TYPE_CHECKING:
    from .shell import Shell


def record(shell: Shell, value: Any, prefix: str = "") -> None:
    """Human-readable, sanitized inspection without interpreting markup."""
    if isinstance(value, dict):
        for name, item in value.items():
            if name in {
                "device_token",
                "csrf_token",
                "credential_ref",
                "content_base64",
            }:
                continue
            if item not in (None, "", [], {}):
                record(shell, item, prefix + str(name).replace("_", " ") + ": ")
    elif isinstance(value, list):
        for index, item in enumerate(value, 1):
            record(shell, item, prefix + f"{index}. ")
    else:
        shell.ui.say(prefix + str(value))


async def message_picker(
    shell: Shell,
    *,
    users_only: bool = False,
    label: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    view = shell.current()
    before: str | None = None

    async def more() -> list[dict[str, Any]]:
        nonlocal before
        params: dict[str, Any] = {"limit": 50, "include_replaced": "true"}
        if before:
            params["before_message_id"] = before
        batch = await shell.api.request(
            "GET", f"chat/sessions/{key(view.session_id)}/messages", params=params
        )
        if batch:
            before = batch[0]["id"]
        return [
            {
                **item,
                "name": label(item)
                if label
                else f"{item['role']}: {terminal_text(item.get('content', ''))[:100]}",
            }
            for item in batch
            if visible_message(item) and (not users_only or item["role"] == "user")
        ]

    rows = await more()
    if not rows:
        raise ClientError("No selectable saved messages.")
    return await shell.ui.choose("Saved message", rows, more=more)


async def chat(shell: Shell, args: list[str]) -> None:
    from .conversations import bulk

    if args and args[0] == "bulk":
        await bulk(shell)
        return
    view = shell.current()
    session = await shell.api.request("GET", f"chat-sessions/{key(view.session_id)}")
    if (session.get("metadata") or {}).get("control_assistant"):
        raise ClientError(
            "Use /butler for Butler and /chats to manage saved main conversations."
        )
    meta = session.get("metadata") or {}
    operations = [
        "rename",
        "pin",
        "flag",
        "archive",
        "restore",
        "fork",
        "rewind",
        "edit",
        "retry",
        "finish",
        "export",
        "delete",
        "bulk",
    ]
    action = (
        args[0]
        if args
        else (
            await shell.ui.choose(
                "Conversation action",
                [
                    {"id": name, "name": CHAT_ACTION_LABELS.get(name, name.title())}
                    for name in operations
                ],
            )
        )["id"]
    )
    if action == "bulk":
        await bulk(shell)
        return
    patch: dict[str, Any] = {"expected_revision": session["revision"]}
    if action == "rename":
        patch["title"] = await shell.ui.ask("Title", default=session["title"])
    elif action == "pin":
        patch["pinned"] = not meta.get("pinned", False)
    elif action == "flag":
        color = (
            await shell.ui.choose(
                "Flag",
                [
                    {"id": row, "name": row.title()}
                    for row in [
                        "clear",
                        "red",
                        "orange",
                        "yellow",
                        "green",
                        "blue",
                        "purple",
                    ]
                ],
            )
        )["id"]
        patch["flag_color"] = None if color == "clear" else color
    elif action in {"archive", "restore"}:
        patch["archived"] = action == "archive"
    elif action == "delete":
        if (
            await shell.ui.ask(
                f"Delete permanently? Type the exact title ({session['title']})"
            )
        ) != session["title"]:
            shell.ui.say("Deletion cancelled.")
            return
        await shell.api.request(
            "DELETE",
            f"chat-sessions/{key(view.session_id)}",
            headers={"If-Match": str(session["revision"])},
        )
        try:
            await shell.api.request("GET", f"chat-sessions/{key(view.session_id)}")
        except ApiError as exc:
            if exc.status != 404:
                raise
        else:
            raise ClientError(
                "Deletion was not confirmed. Refresh /chats before retrying."
            )
        await shell.open_view(view.project, None)
        shell.ui.say("Conversation deleted in Core.")
        return
    elif action == "fork":
        message = await message_picker(shell)
        title = await shell.ui.ask(
            "New conversation title", default="Branch of " + session["title"]
        )
        created = await shell.api.request(
            "POST",
            f"chat/sessions/{key(view.session_id)}/fork",
            json={"through_message_id": message["id"], "title": title},
        )
        await shell.open_view(view.project, created)
        return
    elif action == "finish":
        await finish_answer(shell)
        return
    elif action == "retry" and session.get("backend") == "harness":
        await retry_harness_turn(shell)
        return
    elif action == "edit":
        if (await shell.state()).get("busy"):
            raise ClientError(
                "Stop or finish the running turn before editing a saved message."
            )
        message = await message_picker(shell, users_only=True)
        view.editing = {
            "id": message["id"],
            "content": message.get("content") or "",
            "content_blocks": message.get("content_blocks") or [],
        }
        view.draft.text = message.get("content") or ""
        shell.ui.set_editing(message["id"])
        shell.ui.say(
            "Edit the message in the composer. Enter rewinds the conversation to it "
            "and sends the edited text; Ctrl-C cancels without changing anything."
        )
        return
    elif action in {"rewind", "retry"}:
        message = await message_picker(shell, users_only=True)
        if action == "retry":
            # A retry creates a visible branch before its exact user message;
            # it never resubmits into the original conversation.
            if not await shell.ui.confirm(
                "Create a branch and retry this saved message?"
            ):
                return
            fork = await shell.api.request(
                "POST",
                f"chat/sessions/{key(view.session_id)}/fork",
                json={
                    "before_message_id": message["id"],
                    "title": "Retry " + session["title"],
                },
            )
            await shell.open_view(view.project, fork)
            await shell.send(message["content"])
        else:
            if not await shell.ui.confirm(
                "Rewind this conversation before the selected message?"
            ):
                return
            result = await shell.api.request(
                "POST",
                f"chat/sessions/{key(view.session_id)}/rewind",
                json={
                    "before_message_id": message["id"],
                    "expected_revision": session["revision"],
                },
            )
            await shell.open_view(view.project, result["session"])
        return
    elif action == "export":
        destination = Path(
            (await shell.ui.ask("Client Markdown export path", path=True)).strip()
        ).expanduser()
        if destination.exists() and not await shell.ui.confirm(
            f"Replace {destination}?"
        ):
            return
        batches = []
        before = None
        while True:
            params = {
                "limit": 200,
                "include_replaced": "true",
                **({"before_message_id": before} if before else {}),
            }
            rows = await shell.api.request(
                "GET", f"chat/sessions/{key(view.session_id)}/messages", params=params
            )
            batches.append(rows)
            if len(rows) < 200:
                break
            before = rows[0]["id"]
        text = "# " + terminal_text(session["title"]) + "\n\n"
        text += "\n\n".join(
            f"## {item['role'].title()}\n\n{terminal_text(item.get('content', ''))}"
            for batch in reversed(batches)
            for item in batch
            if visible_message(item)
        )
        destination.write_text(text + "\n", encoding="utf-8")
        shell.ui.say(f"Exported visible saved messages to {destination}.")
        return
    else:
        raise ClientError("Choose a listed /chat action.")
    await shell.api.request(
        "PATCH", f"chat-sessions/{key(view.session_id)}", json=patch
    )
    observed = await shell.api.request("GET", f"chat-sessions/{key(view.session_id)}")
    view.session = observed
    shell.ui.say(f"Saved {observed['title']} in Core.")


CHAT_ACTION_LABELS = {
    "edit": "Edit a sent message and resend",
    "retry": "Retry",
    "finish": "Finish an unfinished answer",
    "bulk": "Select several conversations",
}


async def finish_answer(shell: Shell) -> None:
    """Ask Core to write the missing final answer of a failed provider turn."""
    view = shell.current()
    turn = await shell.api.request(
        "GET", f"chat/sessions/{key(view.session_id)}/pending-turn"
    )
    if (
        not turn
        or turn.get("status") != "failed"
        or turn.get("harness_turn_id")
        or view.config.get("backend") == "harness"
    ):
        raise ClientError(
            "No unfinished provider answer is waiting. Read /status for this conversation."
        )
    shell.ui.say(
        "The model stopped without finishing this reply. Everything it did is saved; "
        "only the written answer is missing."
    )
    if not await shell.ui.confirm("Ask the model to finish the answer now?"):
        return
    await shell.detach()
    view.cursor = Cursor(turn_id=turn["id"])
    view.submitting = False
    shell.stream_task = asyncio.create_task(
        shell.stream(view, "POST", f"chat/turns/{key(turn['id'])}/resume")
    )


async def retry_harness_turn(shell: Shell) -> None:
    """Start a linked retry of a failed or stopped harness turn."""
    view = shell.current()
    rows = await shell.api.request(
        "GET",
        f"chat/sessions/{key(view.session_id)}/messages",
        params={"limit": 50, "include_replaced": "true"},
    )
    options = [
        {
            **row,
            "id": (row.get("metadata") or {})["harness_turn_id"],
            "name": f"{terminal_text(row.get('content') or 'Harness turn')[:100]}",
        }
        for row in reversed(rows)
        if row.get("role") == "assistant"
        and visible_message(row)
        and isinstance((row.get("metadata") or {}).get("harness_turn_id"), str)
    ]
    if not options:
        raise ClientError("No saved harness turns to retry in the latest messages.")
    chosen = await shell.ui.choose("Harness turn to retry", options)
    if not await shell.ui.confirm(
        "Start a linked new turn? The failed execution stays unchanged."
    ):
        return
    await shell.api.request("POST", f"harness-turns/{key(chosen['id'])}/retry")
    shell.ui.say("Linked retry queued in Core. /follow shows its progress.")
    await shell.status(quiet=True)


async def context(shell: Shell, args: list[str]) -> None:
    view = shell.current()
    record(
        shell,
        await shell.api.request("GET", f"chat/sessions/{key(view.session_id)}/context"),
    )


async def activity(shell: Shell, args: list[str]) -> None:
    view = shell.current()
    record(shell, await shell.state())
    record(
        shell,
        await shell.api.request(
            "GET", f"chat/sessions/{key(view.session_id)}/task-plans"
        ),
    )
    pending = await shell.api.request(
        "GET", f"chat/sessions/{key(view.session_id)}/pending-turn"
    )
    if pending:
        record(shell, pending)


async def results(shell: Shell, args: list[str]) -> None:
    view = shell.current()
    offset = 0
    while True:
        value = await shell.api.request(
            "GET",
            f"chat/sessions/{key(view.session_id)}/results",
            params={"offset": offset, "limit": 40},
        )
        record(shell, value.get("items", []))
        if value.get("next_offset") is None or not await shell.ui.confirm(
            "Read more results?"
        ):
            break
        offset = value["next_offset"]


GOAL_FIELDS = (
    "objective",
    "completion_criteria",
    "plan",
    "resume_instruction",
    "token_budget",
    "time_budget_seconds",
    "step_budget",
    "child_budget",
)


async def goal_fields(
    shell: Shell, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    existing = existing or {}
    fields: dict[str, Any] = {}
    fields["objective"] = await shell.ui.ask(
        "Goal objective", default=existing.get("objective", "")
    )
    for name, label in (
        (
            "completion_criteria",
            "Completion criteria (one per line; Esc+Enter adds a line)",
        ),
        ("plan", "Plan steps (optional; one per line)"),
    ):
        raw = await shell.ui.ask(label, default="\n".join(existing.get(name) or []))
        fields[name] = [line.strip() for line in raw.splitlines() if line.strip()]
    fields["resume_instruction"] = (
        await shell.ui.ask(
            "Resume instruction (blank uses Core default)",
            default=existing.get("resume_instruction") or "",
        )
        or None
    )
    for name, label in (
        ("token_budget", "Token budget"),
        ("time_budget_seconds", "Active-time budget in seconds (minimum 1800)"),
        ("step_budget", "Step budget"),
        ("child_budget", "Child-goal budget"),
    ):
        current = existing.get(name)
        raw = (
            await shell.ui.ask(
                label + " (blank uses Core default)",
                default=str(current) if current is not None else "",
            )
        ).strip()
        fields[name] = int(raw) if raw else None
    return fields


async def goal(shell: Shell, args: list[str]) -> None:
    from .settings import save

    view = shell.current()
    current = None
    if view.session:
        try:
            current = await shell.api.request(
                "GET", f"chat/sessions/{key(view.session_id)}/goal"
            )
        except ApiError as exc:
            if exc.status != 404:
                raise
    if current:
        record(
            shell,
            {
                name: current.get(name)
                for name in (
                    "status",
                    "objective",
                    "completion_criteria",
                    "plan",
                    "current_step",
                    "blocked_reason",
                    "elapsed_seconds",
                )
            },
        )
        operations = [
            "show",
            "edit",
            "skills",
            "children",
            "child",
            "start",
            "pause",
            "resume",
            "cancel",
            "block",
            "complete",
        ]
    else:
        operations = ["create"]
    action = (
        args[0]
        if args
        else (
            await shell.ui.choose(
                "Goal action",
                [
                    {"id": name, "name": GOAL_ACTION_LABELS.get(name, name.title())}
                    for name in operations
                ],
            )
        )["id"]
    )
    if action == "create":
        fields = await goal_fields(shell)
        if view.session:
            current = await shell.api.request(
                "POST", f"chat/sessions/{key(view.session_id)}/goal", json=fields
            )
        else:
            config = dict(await shell.configuration())
            allowed = {
                "backend",
                "model",
                "tools_enabled",
                "planning_enabled",
                "mcp_server_ids",
                "hook_ids",
                "reasoning_effort",
                "harness_reasoning_effort",
                "harness_service_tier",
                "harness_cyber_access_program",
                "allow_subagents",
                "allow_agent_messaging",
                "max_active_subagents",
                "subagent_provider_id",
                "subagent_model",
                "subagent_reasoning_effort",
            }
            body = {name: value for name, value in config.items() if name in allowed}
            body[
                "provider_id"
                if config["backend"] == "provider"
                else "harness_profile_id"
            ] = config["profile_id"]
            if config.get("subagent_harness_profile_id"):
                body["subagent_harness_id"] = config["subagent_harness_profile_id"]
            value = await shell.api.request(
                "POST",
                "chat/goal-conversations",
                json={**body, **fields, "engagement_id": view.project_id},
            )
            view.session = value["session"]
            current = value["goal"]
            shell.remember()
            await save(
                shell,
                {
                    name: value
                    for name, value in config.items()
                    if name not in {"profile_id", "backend", "model"}
                },
            )
        observed = await shell.api.request(
            "GET", f"chat/sessions/{key(view.session_id)}/goal"
        )
        shell.ui.say(
            f"Goal saved in Core: {observed['objective']}. Use /goal skills, then /goal start."
        )
        return
    if current is None:
        raise ClientError(
            "This conversation has no Core-owned goal. Use /goal create. Native harness goals retain their own supported controls."
        )
    path = f"chat/sessions/{key(view.session_id)}/goal"
    if action == "show":
        record(shell, current)
        return
    if action == "children":
        await goal_children(shell, path)
        return
    if action == "child":
        if current.get("status") != "running":
            raise ClientError("Start or resume the goal before starting a child goal.")
        objective = (await shell.ui.ask("Child goal objective")).strip()
        if not objective:
            raise ClientError("A child goal needs an objective. Nothing was started.")
        child = await shell.api.request(
            "POST",
            path + "/children",
            json={
                "objective": objective,
                # The GUI's fixed criterion: children never inherit approvals.
                "completion_criteria": [
                    "Child work completes without sharing parent approvals"
                ],
                "plan": [],
            },
        )
        shell.ui.say(f"Child goal started: {terminal_text(child.get('objective'))}.")
        if child.get("session_id") and await shell.ui.confirm(
            "Open the child goal's conversation?"
        ):
            session = await shell.api.request(
                "GET", f"chat-sessions/{key(child['session_id'])}"
            )
            await shell.open_view(view.project, session)
        return
    if action == "edit":
        fields = await goal_fields(shell, current)
        await shell.api.request(
            "PATCH", path, json={**fields, "expected_revision": current["revision"]}
        )
    elif action == "skills":
        catalog = await shell.api.request(
            "GET", "skills", params={"engagement_id": view.project_id}
        )
        retained = current.get("skill_snapshots") or []
        all_skills = {row["path"]: row for row in [*retained, *catalog]}
        chosen = await shell.ui.multiple(
            "Goal skills (retained immutable snapshots)",
            [
                {
                    "id": row["path"],
                    "name": row["name"],
                    "selection_group": "goal",
                    "selection_group_limit": 20,
                }
                for row in all_skills.values()
            ],
            [row["path"] for row in retained],
        )
        if chosen == [row["path"] for row in retained]:
            shell.ui.say(f"Goal skills unchanged: {len(chosen)} selected.")
            return
        await shell.api.request(
            "PUT",
            path + "/skills",
            json={
                "expected_revision": current["revision"],
                "skills": [
                    {"name": all_skills[item]["name"], "path": item} for item in chosen
                ],
            },
        )
    elif action in {"start", "pause", "resume", "cancel", "block", "complete"}:
        if action in {"cancel", "complete"} and not await shell.ui.confirm(
            f"{action.title()} this goal?"
        ):
            return
        if action in {"start", "resume"} and (view.draft.images or view.draft.contexts):
            raise ClientError(
                "Send the pending attachments in a normal message before starting this goal; they are not part of a goal action."
            )
        body = {"action": action}
        if action in {"cancel", "block"}:
            body["reason"] = await shell.ui.ask("Reason")
        if action == "complete":
            # Core requires a summary and evidence, as the GUI's form does.
            body["completion_summary"] = (
                await shell.ui.ask("Completion summary")
            ).strip()
            evidence = []
            while True:
                detail = (
                    await shell.ui.ask(
                        "Completion evidence"
                        if not evidence
                        else "More evidence (blank to finish)"
                    )
                ).strip()
                if not detail:
                    break
                evidence.append({"kind": "operator_confirmation", "detail": detail})
            if not body["completion_summary"] or not evidence:
                raise ClientError(
                    "Completing a goal needs a summary and at least one evidence item. "
                    "The goal is unchanged."
                )
            body["completion_evidence"] = evidence
        try:
            await shell.api.request(
                "POST",
                path + "/actions",
                json={**body, "expected_revision": current["revision"]},
            )
        except ApiError as exc:
            # A running goal's revision moves every turn; retry once on fresh state.
            if exc.status != 409:
                raise
            latest = await shell.api.request("GET", path)
            if latest.get("status") != current.get("status"):
                raise ClientError(
                    f"The goal changed to {latest.get('status')} before this action. "
                    "Read /goal show and choose again."
                ) from exc
            await shell.api.request(
                "POST",
                path + "/actions",
                json={**body, "expected_revision": latest["revision"]},
            )
    else:
        raise ClientError("Choose a listed /goal action.")
    observed = await shell.api.request("GET", path)
    shell.ui.say(f"Core goal status: {observed['status']}.")


GOAL_ACTION_LABELS = {
    "children": "Child goals",
    "child": "Start a child goal",
    "skills": "Edit goal skills",
}


async def goal_children(shell: Shell, path: str) -> None:
    view = shell.current()
    children = await shell.api.request("GET", path + "/children")
    if not children:
        shell.ui.say("This goal has no child goals. /goal child starts one.")
        return
    chosen = await shell.ui.choose(
        "Child goals · choose one to open its conversation",
        [
            {
                **row,
                "name": f"{terminal_text(row.get('objective') or 'Child goal')[:90]}"
                f" · {str(row.get('status', '')).replace('_', ' ')}",
            }
            for row in children
        ],
    )
    session = await shell.api.request(
        "GET", f"chat-sessions/{key(chosen['session_id'])}"
    )
    await shell.open_view(view.project, session)


async def steer(shell: Shell, args: list[str]) -> None:
    state = await shell.state()
    config = await shell.configuration()
    await shell.runtimes()
    profile = shell.profiles.get(config["profile_id"], {})
    if (
        not state.get("busy")
        or not state.get("harness_turn_id")
        or not (profile.get("capabilities") or {}).get("steering")
    ):
        raise ClientError(
            "This active turn does not support steering. Send text to queue a follow-up instead."
        )
    text = " ".join(args) or await shell.ui.ask("Guidance for the current turn")
    await shell.api.request(
        "POST",
        f"harness-turns/{key(state['harness_turn_id'])}/steer",
        json={"text": text},
    )
    shell.ui.say("Guidance delivered to the active harness turn.")


ACTIVE_SUBAGENT = {"queued", "running", "waiting_approval", "recovering"}
SSH_TOOL = re.compile(r"^ssh\.([a-z0-9_-]+?)(?:-[0-9a-f]{6})?\.run_command$")
SSH_DESCRIPTION = re.compile(
    r"^Run a shell command on the operator's machine (.+) \(SSH host (\S+)\)\.$",
    re.MULTILINE,
)


def ssh_target(approval: dict[str, Any]) -> tuple[str, str] | None:
    """The SSH host (label, alias) an approval would run on, as the GUI reads it."""
    exact = approval.get("exact_request") or approval
    tool = exact.get("tool_name") if isinstance(exact, dict) else None
    match = SSH_TOOL.match(tool or "")
    if match is None:
        return None
    for effect in approval.get("expected_effects") or []:
        found = SSH_DESCRIPTION.search(effect) if isinstance(effect, str) else None
        if found:
            return found.group(1), found.group(2)
    return match.group(1), match.group(1)


def subagent_label(row: dict[str, Any]) -> str:
    seconds = int(row.get("elapsed_seconds") or 0)
    elapsed = f"{seconds // 60}m {seconds % 60:02}s" if seconds >= 60 else f"{seconds}s"
    name = row.get("name") or row.get("task") or row.get("objective") or row["id"]
    status = str(row.get("status", "unknown")).replace("_", " ")
    return f"Delegated: {terminal_text(name)[:70]} · {status} · {elapsed}"


async def agents(shell: Shell, args: list[str]) -> None:
    view = shell.current()
    main = await shell.api.page(
        "chat-sessions", engagement_id=view.project_id, recent_first="true"
    )
    from .state import main_chat

    options: list[dict[str, Any]] = []
    if view.session:
        listing = await shell.api.request(
            "GET", f"chat/sessions/{key(view.session_id)}/subagents"
        )
        children = listing.get("subagents", [])
        active = [row for row in children if row.get("status") in ACTIVE_SUBAGENT]
        if active:
            options.append(
                {
                    "id": "__stop_all",
                    "name": f"Stop all {len(active)} running subagents",
                }
            )
        options.extend(
            {"id": "child:" + row["id"], "name": subagent_label(row), "child": row}
            for row in children
        )
    options.extend(
        {"id": "main:" + row["id"], "name": row["title"], "session": row}
        for row in main
        if main_chat(row)
    )
    if not options:
        shell.ui.say("No saved agents yet. Use /new.")
        return
    chosen = await shell.ui.choose("Project agents", options)
    if "session" in chosen:
        await shell.open_view(view.project, chosen["session"])
        return
    path = f"chat/sessions/{key(view.session_id)}/subagents"
    if chosen["id"] == "__stop_all":
        if not await shell.ui.confirm(
            "Stop every running subagent of this conversation?"
        ):
            return
        await shell.api.request("POST", path + "/stop")
        observed = (await shell.api.request("GET", path)).get("subagents", [])
        still = [row for row in observed if row.get("status") in ACTIVE_SUBAGENT]
        shell.ui.say(
            "All subagents stopped in Core."
            if not still
            else f"{len(still)} subagents are still stopping; /agents shows them."
        )
        return
    child = chosen["child"]
    for line in (
        subagent_label(child),
        f"Task: {terminal_text(child.get('task') or '')}",
        f"Result: {terminal_text(child.get('result') or '')[:400]}"
        if child.get("result")
        else "",
        f"Error: {terminal_text(child.get('error') or '')}"
        if child.get("error")
        else "",
    ):
        if line:
            shell.ui.say(line)
    actions = []
    if child.get("child_session_id"):
        actions.append(
            {"id": "open", "name": "Open its conversation (read and message it)"}
        )
    if child.get("status") in ACTIVE_SUBAGENT:
        actions.append({"id": "stop", "name": "Stop this subagent"})
    actions.append({"id": "back", "name": "Back to chat"})
    action = (await shell.ui.choose("Subagent", actions))["id"]
    if action == "open":
        session = await shell.api.request(
            "GET", f"chat-sessions/{key(child['child_session_id'])}"
        )
        await shell.open_view(view.project, session)
    elif action == "stop":
        await shell.api.request("POST", f"{path}/{key(child['id'])}/stop")
        observed = await shell.api.request("GET", path)
        row = next(
            (row for row in observed["subagents"] if row["id"] == child["id"]),
            {"status": "removed"},
        )
        shell.ui.say(
            f"Subagent status in Core: {str(row.get('status')).replace('_', ' ')}."
        )


async def approvals(shell: Shell, args: list[str]) -> None:
    view = shell.current()
    state = await shell.state()
    pending = [row for row in state.get("pending", []) if row["kind"] == "approval"]
    options = [
        {**row, "id": "main:" + row["id"], "name": row["text"]} for row in pending
    ]
    if view.session is not None:
        listing = await shell.api.request(
            "GET", f"chat/sessions/{key(view.session_id)}/subagents"
        )
        for row in listing.get("subagents", []):
            approval = row.get("approval") or {}
            if row.get("status") == "waiting_approval" and approval.get("id"):
                reason = (
                    approval.get("rationale")
                    or approval.get("tool")
                    or "wants to run a tool"
                )
                options.append(
                    {
                        "id": "sub:" + approval["id"],
                        "name": f"Subagent {terminal_text(row.get('name') or row['id'])}: "
                        f"{terminal_text(reason)[:100]}",
                        "approval": approval,
                    }
                )
    if not options:
        shell.ui.say("No pending approvals in this conversation or its subagents.")
        return
    selected = await shell.ui.choose("Pending approval", options)
    if selected["id"].startswith("sub:"):
        approval = selected["approval"]
        shell.ui.say("Subagent request:")
        record(
            shell,
            {
                name: approval.get(name)
                for name in ("tool", "rationale", "risk_class", "detail")
            },
        )
        decision = (
            await shell.ui.choose(
                "Decision",
                [
                    {"id": "reject", "name": "Deny"},
                    {"id": "approve", "name": "Allow once"},
                ],
            )
        )["id"]
        await shell.api.request(
            "POST",
            f"approvals/{key(approval['id'])}/decision",
            json={"decision": decision},
        )
        observed = await shell.api.request("GET", f"approvals/{key(approval['id'])}")
        shell.ui.say(
            f"Decision recorded: {observed['status']}. The subagent continues on Core."
        )
        return
    await decide_main_approval(
        shell, view, {**selected, "id": selected["id"].removeprefix("main:")}
    )


async def decide_main_approval(
    shell: Shell, view: Any, selected: dict[str, Any]
) -> None:
    approval = await shell.api.request("GET", f"approvals/{key(selected['id'])}")
    if approval.get("chat_session_id") not in {None, view.session_id}:
        raise ClientError(
            "This approval belongs to another conversation. Refresh /status."
        )
    turn = await shell.api.request(
        "GET", f"chat/sessions/{key(view.session_id)}/pending-turn"
    )
    if (
        not turn
        or turn.get("id") != selected["turn_id"]
        or turn.get("session_id") != view.session_id
        or approval.get("chat_turn_id") not in {None, selected["turn_id"]}
    ):
        raise ClientError(
            "The pending turn changed. Refresh /approvals before deciding."
        )
    shell.ui.say("Exact requested action:")
    record(shell, approval.get("exact_request") or approval)
    host = ssh_target(approval)
    decisions = [
        {"id": "reject", "name": "Reject"},
        {"id": "approve", "name": "Approve"},
    ]
    if not turn.get("harness_turn_id"):
        decisions.append({"id": "edit", "name": "Edit the request, then approve"})
    if host is not None:
        decisions.append(
            {
                "id": "always_host",
                "name": f"Always allow on {host[0]}, then approve",
            }
        )
    decisions.append({"id": "stop", "name": "Stop the response"})
    decision = (await shell.ui.choose("Decision", decisions))["id"]
    payload: dict[str, Any] = {"decision": decision}
    if decision == "edit":
        exact = approval.get("exact_request") or {}
        current_args = exact.get("arguments") if isinstance(exact, dict) else None
        raw = await shell.ui.ask(
            "Edited arguments (one JSON object)",
            default=json.dumps(current_args or {}, separators=(", ", ": ")),
        )
        try:
            edited = json.loads(raw)
        except ValueError as exc:
            raise ClientError(
                "Edited arguments must be valid JSON. Nothing was decided."
            ) from exc
        if not isinstance(edited, dict):
            raise ClientError("Edited arguments must be one JSON object.")
        payload = {"decision": "approve", "edited_arguments": edited}
        decision = "approve"
    elif decision == "always_host":
        assert host is not None
        await shell.api.request(
            "PUT",
            f"ssh-environments/{key(host[1])}",
            json={"command_approval": "allow"},
        )
        shell.ui.say(f"Commands on {host[0]} no longer ask for approval.")
        payload = {"decision": "approve"}
        decision = "approve"
    await shell.api.request(
        "POST", f"approvals/{key(selected['id'])}/decision", json=payload
    )
    observed = await shell.api.request("GET", f"approvals/{key(selected['id'])}")
    shell.ui.say(
        f"Decision recorded: {observed['status']}. This is not proof the action completed."
    )
    if decision != "stop":
        await shell.detach()
        view.cursor = Cursor(
            turn_id=selected["turn_id"], harness_turn_id=turn.get("harness_turn_id")
        )
        provider = not turn.get("harness_turn_id")
        shell.stream_task = asyncio.create_task(
            shell.stream(
                view,
                "POST" if provider else "GET",
                f"chat/turns/{key(selected['turn_id'])}/"
                + ("resume" if provider else "events"),
            )
        )


async def form(
    shell: Shell, schema: dict[str, Any], *, secret: bool = False
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, prop in schema.get("properties", {}).items():
        label = prop.get("title") or name
        kind = prop.get("type", "string")
        if kind == "object":
            values[name] = await form(shell, prop, secret=secret)
        elif kind == "array" and (prop.get("items") or {}).get("enum"):
            options = [
                {"id": str(item), "name": str(item)} for item in prop["items"]["enum"]
            ]
            values[name] = await shell.ui.multiple(label, options, [])
        elif kind in {"string", "boolean", "integer", "number"}:
            choices = [str(item) for item in prop.get("enum", [])]
            raw = await shell.ui.ask(
                label,
                secret=secret,
                choices=choices or (["true", "false"] if kind == "boolean" else None),
            )
            if raw == "" and name not in schema.get("required", []):
                continue
            if kind == "boolean":
                if raw.lower() not in {"true", "false"}:
                    raise ClientError("Choose true or false.")
                value: Any = raw.lower() == "true"
            else:
                value = (
                    int(raw)
                    if kind == "integer"
                    else float(raw)
                    if kind == "number"
                    else raw
                )
            if "enum" in prop and value not in prop["enum"]:
                raise ClientError(f"Choose a listed value for {label}.")
            values[name] = value
        else:
            raise ClientError(
                "This form type requires the existing Assistant device interface. No answer was sent."
            )
    return values


async def input_question(shell: Shell, args: list[str]) -> None:
    state = await shell.state()
    if not state.get("harness_turn_id"):
        shell.ui.say("No active harness questions.")
        return
    rows = await shell.api.request(
        "GET",
        f"harness-turns/{key(state['harness_turn_id'])}/interactions",
        params={"status": "pending"},
    )
    if not rows:
        shell.ui.say("No pending input in this conversation.")
        return
    item = await shell.ui.choose(
        "Question",
        [{**row, "name": row.get("prompt", "Input required")} for row in rows],
    )
    if item.get("chat_session_id") != shell.current().session_id:
        raise ClientError("This question belongs to another conversation.")
    action = (
        await shell.ui.choose(
            "Answer question",
            [
                {"id": name, "name": name.title()}
                for name in ["answer", "decline", "cancel"]
            ],
        )
    )["id"]
    response: dict[str, Any] = {}
    if action == "answer":
        secret = item.get("contains_secret", False)
        if item.get("kind") == "mcp_elicitation":
            response = await form(
                shell, item.get("response_schema") or {}, secret=secret
            )
        else:
            for index, question in enumerate(item.get("questions") or []):
                suggestions = [
                    str(option.get("label"))
                    if isinstance(option, dict)
                    else str(option)
                    for option in question.get("options") or []
                ]
                response[
                    question.get("id") or f"question_{index + 1}"
                ] = await shell.ui.ask(
                    question.get("question") or f"Question {index + 1}",
                    secret=secret,
                    choices=suggestions or None,
                )
    await shell.api.request(
        "POST",
        f"harness-interactions/{key(item['id'])}/decision",
        json={"action": action, "response": response},
    )
    response.clear()
    observed = await shell.api.request(
        "GET", f"harness-turns/{key(state['harness_turn_id'])}/interactions"
    )
    status = next(
        (row["status"] for row in observed if row["id"] == item["id"]), "unavailable"
    )
    shell.ui.say(f"Question status: {status}.")


async def schedule(shell: Shell, args: list[str]) -> None:
    view = shell.current()
    if (await shell.configuration())["backend"] != "provider":
        raise ClientError(
            "Core schedules currently support provider conversations only."
        )
    path = f"chat/sessions/{key(view.session_id)}/schedule"
    try:
        current = await shell.api.request("GET", path)
    except ApiError as exc:
        if exc.status != 404:
            raise
        hours = float(await shell.ui.ask("Schedule interval in hours (1–720)"))
        if not await shell.ui.confirm(f"Create a Core schedule every {hours:g} hours?"):
            return
        await shell.api.request(
            "POST", path, json={"interval_seconds": int(hours * 3600)}
        )
    else:
        record(shell, current)
        if not await shell.ui.confirm(
            "Disable this schedule?" if current["enabled"] else "Enable this schedule?"
        ):
            return
        await shell.api.request(
            "POST",
            path + "/actions",
            json={
                "expected_revision": current["revision"],
                "enabled": not current["enabled"],
            },
        )
    observed = await shell.api.request("GET", path)
    shell.ui.say(
        f"Schedule {'enabled' if observed['enabled'] else 'disabled'} in Core."
    )


actions = {
    "chat": chat,
    "context": context,
    "activity": activity,
    "results": results,
    "goal": goal,
    "steer": steer,
    "agents": agents,
    "approvals": approvals,
    "input": input_question,
    "queue": queue,
    "now": now,
    "schedule": schedule,
}
