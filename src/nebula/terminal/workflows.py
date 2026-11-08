"""Saved-chat, work and pending-action workflows over the public Core API."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


async def message_picker(shell: Shell, *, users_only: bool = False) -> dict[str, Any]:
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
                "name": f"{item['role']}: {terminal_text(item.get('content', ''))[:100]}",
            }
            for item in batch
            if visible_message(item) and (not users_only or item["role"] == "user")
        ]

    rows = await more()
    if not rows:
        raise ClientError("No selectable saved messages.")
    return await shell.ui.choose("Saved message", rows, more=more)


async def chat(shell: Shell, args: list[str]) -> None:
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
        "export",
        "delete",
    ]
    action = (
        args[0]
        if args
        else (
            await shell.ui.choose(
                "Conversation action",
                [{"id": name, "name": name.title()} for name in operations],
            )
        )["id"]
    )
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
    elif action in {"rewind", "edit", "retry"}:
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
            if action == "edit":
                shell.current().draft.text = message["content"]
                shell.ui.say(
                    "Saved conversation rewound. Edit the restored draft before sending."
                )
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
                [{"id": name, "name": name.title()} for name in operations],
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
        record(shell, await shell.api.request("GET", path + "/children"))
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
        body = {"expected_revision": current["revision"], "action": action}
        if action in {"cancel", "block"}:
            body["reason"] = await shell.ui.ask("Reason")
        if action == "complete":
            body["completion_summary"] = await shell.ui.ask("Completion summary")
            body["completion_evidence"] = current.get("completion_evidence") or []
        await shell.api.request("POST", path + "/actions", json=body)
    else:
        raise ClientError("Choose a listed /goal action.")
    observed = await shell.api.request("GET", path)
    shell.ui.say(f"Core goal status: {observed['status']}.")


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


async def agents(shell: Shell, args: list[str]) -> None:
    view = shell.current()
    main = await shell.api.page(
        "chat-sessions", engagement_id=view.project_id, recent_first="true"
    )
    from .state import main_chat

    options = [
        {"id": "main:" + row["id"], "name": row["title"], "session": row}
        for row in main
        if main_chat(row)
    ]
    if view.session:
        listing = await shell.api.request(
            "GET", f"chat/sessions/{key(view.session_id)}/subagents"
        )
        options.extend(
            {
                "id": "child:" + row["id"],
                "name": f"Delegated: {row.get('name') or row.get('task') or row.get('objective') or row['id']} ({row.get('status', 'unknown')})",
                "child": row,
            }
            for row in listing.get("subagents", [])
        )
    if not options:
        shell.ui.say("No saved agents yet. Use /new.")
        return
    chosen = await shell.ui.choose("Project agents", options)
    if "session" in chosen:
        await shell.open_view(view.project, chosen["session"])
        return
    record(shell, chosen["child"])
    if await shell.ui.confirm("Stop this delegated agent through Core?"):
        await shell.api.request(
            "POST",
            f"chat/sessions/{key(view.session_id)}/subagents/{key(chosen['child']['id'])}/stop",
        )
        observed = await shell.api.request(
            "GET", f"chat/sessions/{key(view.session_id)}/subagents"
        )
        record(
            shell,
            next(
                (
                    row
                    for row in observed["subagents"]
                    if row["id"] == chosen["child"]["id"]
                ),
                {"status": "removed"},
            ),
        )


async def approvals(shell: Shell, args: list[str]) -> None:
    view = shell.current()
    state = await shell.state()
    pending = [row for row in state.get("pending", []) if row["kind"] == "approval"]
    if not pending:
        shell.ui.say("No pending approvals in this conversation.")
        return
    selected = await shell.ui.choose(
        "Pending approval", [{**row, "name": row["text"]} for row in pending]
    )
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
    decision = (
        await shell.ui.choose(
            "Decision",
            [
                {"id": name, "name": name.title()}
                for name in ["reject", "approve", "stop"]
            ],
        )
    )["id"]
    await shell.api.request(
        "POST", f"approvals/{key(selected['id'])}/decision", json={"decision": decision}
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


async def queue(shell: Shell, args: list[str]) -> None:
    view = shell.current()
    path = f"chat/sessions/{key(view.session_id)}/queue"
    current = await shell.api.request("GET", path)
    for item in current.get("items", []):
        shell.ui.say(
            f"{item['status']}: {((item.get('request') or {}).get('messages') or [{}])[-1].get('content', '')}"
        )
    operations = [
        "show",
        "enqueue",
        "edit",
        "remove",
        "reorder",
        "pause",
        "resume",
        "clear",
        "retry",
    ]
    action = (
        args[0]
        if args
        else (
            await shell.ui.choose(
                "Follow-up queue",
                [{"id": name, "name": name.title()} for name in operations],
            )
        )["id"]
    )
    if action == "show":
        return
    body: dict[str, Any] = {"action": action, "expected_revision": current["revision"]}
    if action == "enqueue":
        from uuid import uuid4

        text = await shell.ui.ask("Follow-up message")
        body.update(
            request=await shell.body(text), idempotency_key=str(uuid4()), paused=True
        )
    elif action in {"edit", "remove", "reorder", "retry"}:
        items = [
            {
                **row,
                "name": f"{row['status']}: {((row.get('request') or {}).get('messages') or [{}])[-1].get('content', '')[:100]}",
            }
            for row in current["items"]
            if row["status"] not in {"complete", "sending", "claiming"}
        ]
        if not items:
            raise ClientError("No editable queue items.")
        item = await shell.ui.choose("Queue item", items)
        body["item_id"] = item["id"]
        if action == "edit":
            request = dict(item["request"])
            request["messages"] = [
                {
                    "role": "user",
                    "content": await shell.ui.ask(
                        "Message", default=request["messages"][-1]["content"]
                    ),
                }
            ]
            body["request"] = request
        elif action == "reorder":
            body["order"] = [
                item["id"],
                *[
                    row["id"]
                    for row in current["items"]
                    if row["id"] != item["id"]
                    and row["status"] in {"queued", "needs_review"}
                ],
            ]
    elif action in {"clear", "resume"} and not await shell.ui.confirm(
        f"{action.title()} this queue?"
    ):
        return
    elif action not in operations:
        raise ClientError("Choose a listed queue action.")
    await shell.api.request("POST", path, json=body)
    observed = await shell.api.request("GET", path)
    shell.ui.say(
        f"Queue saved in Core: {'paused' if observed['paused'] else 'enabled'}, {len(observed['items'])} items."
    )


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
    "schedule": schedule,
}
