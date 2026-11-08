"""Capability-driven next-turn choices, using Core's existing validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .transport import ClientError, key

if TYPE_CHECKING:
    from .shell import Shell


async def save(shell: Shell, patch: dict[str, Any]) -> None:
    view = shell.current()
    if view.session is None:
        view.config.update(patch)
        shell.ui.say("Selected for the first turn.")
        return
    await shell.api.request(
        "PATCH",
        f"chat-sessions/{key(view.session_id)}/assistant-config",
        json={"expected_revision": view.session["revision"], "settings": patch},
    )
    observed = await shell.configuration()
    if any(observed.get(name) != value for name, value in patch.items()):
        raise ClientError(
            "The requested settings were not confirmed. Read /settings before retrying."
        )
    shell.ui.say("Saved in Core for the next turn.")


async def model(shell: Shell) -> None:
    current = await shell.configuration()
    options = await shell.runtimes()
    if not options:
        raise ClientError("No enabled model is configured on Core.")
    chosen = await shell.ui.choose("Model runtime", options)
    patch = {name: chosen[name] for name in ("backend", "profile_id", "model")}
    view = shell.current()
    if view.session and current["backend"] == chosen["backend"] == "provider":
        request = {
            "provider_id": chosen["profile_id"],
            "model": chosen["model"],
            "tools_enabled": current.get("tools_enabled", False),
            "expected_session_revision": view.session["revision"],
        }
        preflight = await shell.api.request(
            "POST",
            f"chat/sessions/{key(view.session_id)}/runtime-switch/preflight",
            json=request,
        )
        if not preflight.get("compatible"):
            raise ClientError(
                preflight.get("reason")
                or "This model cannot accept the saved conversation."
            )
        if preflight.get("requires_compaction_confirmation"):
            if not await shell.ui.confirm(
                preflight.get("reason")
                or "Compact the saved context to use this model?"
            ):
                return
            request["confirmation_token"] = preflight["confirmation_token"]
        await shell.api.request(
            "POST",
            f"chat/sessions/{key(view.session_id)}/runtime-switch",
            json=request,
        )
        await shell.configuration()
        view.draft.skill = None
        view.draft.skills.clear()
        shell.ui.say("Runtime switch saved in Core.")
        return
    # Runtime-specific choices must be advertised again by the new model.
    if chosen["backend"] == "harness":
        patch.update(
            harness_mode=None,
            harness_reasoning_effort=None,
            harness_service_tier=None,
            harness_cyber_access_program="automatic",
        )
    await save(shell, patch)
    view.draft.skill = None
    view.draft.skills.clear()


async def native_skill_catalog(shell: Shell) -> list[dict[str, Any]]:
    view = shell.current()
    config = await shell.configuration()
    await shell.runtimes()
    profile = shell.profiles.get(config["profile_id"], {})
    if not (profile.get("capabilities") or {}).get("skill_invocation"):
        return []
    if not (profile.get("native_capabilities") or {}).get("skills"):
        return []
    return await shell.api.request(
        "GET",
        f"harnesses/{key(config['profile_id'])}/skills",
        params={"engagement_id": view.project_id},
    )


async def select_skill(shell: Shell, selected: str | None = None) -> None:
    view = shell.current()
    config = await shell.configuration()
    core_skills = await shell.api.request(
        "GET", "skills", params={"engagement_id": view.project_id}
    )
    native_skills = (
        await native_skill_catalog(shell) if config["backend"] == "harness" else []
    )
    core_paths = {row["path"] for row in core_skills}
    native_paths = {row["path"] for row in native_skills}
    catalog = list(core_skills) + [
        row for row in native_skills if row["path"] not in core_paths
    ]
    current = [item["path"] for item in view.draft.skills]
    if view.draft.skill:
        current.append(view.draft.skill["path"])
    if not catalog and not current:
        raise ClientError(
            "No skills were discovered for this project and runtime on Core."
        )
    options = [
        {
            **row,
            "id": row["path"],
            "name": row["name"],
            "label": row["name"]
            + (
                " · installed harness"
                if row["path"] not in core_paths
                else " · project/Core"
            ),
            "selection_group": "core" if row["path"] in core_paths else "native",
            "selection_group_limit": 8 if row["path"] in core_paths else 1,
        }
        for row in catalog
    ]
    known_paths = {row["path"] for row in catalog}
    for row in [*view.draft.skills, *([view.draft.skill] if view.draft.skill else [])]:
        if row["path"] not in known_paths:
            options.append(
                {
                    "id": row["path"],
                    "name": row["name"],
                    "label": row["name"] + " · no longer on Core; select to remove",
                    "disabled": True,
                    "disabled_reason": "This skill is unavailable; remove it before saving.",
                }
            )
    if selected:
        item = await shell.ui.choose(
            "Skill for the next turn", options, selected=selected
        )
        chosen = [item["id"]]
    else:
        chosen = await shell.ui.multiple(
            "Skills for the next turn · select several, then save", options, current
        )
    by_path = {row["path"]: row for row in catalog}
    if any(path not in by_path for path in chosen):
        raise ClientError(
            "A selected skill is no longer available on Core. Reopen /skills."
        )
    native_only = [path for path in chosen if path not in core_paths]
    if len(native_only) > 1 or len([path for path in chosen if path in core_paths]) > 8:
        raise ClientError(
            "Select up to eight project skills and one installed harness skill."
        )
    view.draft.skill = None
    view.draft.skills = []
    if len(chosen) == 1 and (
        chosen[0] in native_paths or config["backend"] == "provider"
    ):
        row = by_path[chosen[0]]
        view.draft.skill = {"name": row["name"], "path": row["path"]}
    else:
        for path in chosen:
            row = by_path[path]
            skill = {"name": row["name"], "path": row["path"]}
            if path in core_paths:
                view.draft.skills.append(skill)
            else:
                view.draft.skill = skill
    shell.ui.say(
        f"{len(chosen)} skill{'s' if len(chosen) != 1 else ''} selected for the next turn: "
        + (", ".join("$" + by_path[path]["name"] for path in chosen) or "none")
        + "."
    )


async def select_mcp_servers(shell: Shell) -> None:
    """Select visible MCP profiles on demand; Core owns the saved chat setting."""
    config = await shell.configuration()
    current = list(config.get("mcp_server_ids") or [])
    project_id = shell.current().project_id
    rows: list[dict[str, Any]] = []
    offset = 0
    page_size = 500
    while True:
        page = await shell.api.request(
            "GET", "mcp-servers", params={"offset": offset, "limit": page_size}
        )
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)

    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        owner = (row.get("metadata") or {}).get("engagement_id")
        if owner and owner != project_id:
            continue
        identifier = row["id"]
        if not row.get("enabled") and identifier not in current:
            continue
        seen.add(identifier)
        capabilities = row.get("capabilities") or {}
        tool_count = len(
            [
                tool
                for tool in capabilities.get("tools") or []
                if (
                    not row.get("enabled_tools")
                    or tool.get("name") in row["enabled_tools"]
                )
                and tool.get("name") not in (row.get("disabled_tools") or [])
                and (row.get("tool_overrides") or {}).get(tool.get("name")) != "deny"
            ]
        )
        reason = (
            "Disabled on Core"
            if not row.get("enabled")
            else "Probe this server on Core first"
            if not capabilities.get("checked_at")
            else "No enabled tools"
            if not tool_count
            else ""
        )
        options.append(
            {
                "id": identifier,
                "name": row["name"],
                "label": f"{row['name']} · {reason or f'{tool_count} tools'}",
                "disabled": bool(reason),
                "disabled_reason": reason,
                "selection_group": "mcp",
                "selection_group_limit": 64,
            }
        )
    options.extend(
        {
            "id": identifier,
            "name": identifier,
            "label": f"{identifier} · no longer visible on Core; select to remove",
            "disabled": True,
            "disabled_reason": "This server is unavailable; remove it before saving.",
        }
        for identifier in current
        if identifier not in seen
    )
    if not options:
        raise ClientError(
            "No enabled MCP servers are available for this project. "
            "Add and probe one in Core Settings › MCP servers, then reopen /mcp."
        )
    chosen = await shell.ui.multiple(
        "MCP servers · selected servers go with each turn", options, current
    )
    if chosen == current:
        shell.ui.say(f"MCP servers unchanged: {len(current)} selected.")
        return
    await save(shell, {"mcp_server_ids": chosen})
    shell.ui.say(
        "MCP servers selected: "
        + (", ".join(row["name"] for row in options if row["id"] in chosen) or "none")
        + "."
    )


async def settings(shell: Shell) -> None:
    """Keep the settings workflow open until the operator leaves it."""
    while not await _settings_choice(shell):
        if hasattr(shell.ui, "begin_loading"):
            shell.ui.begin_loading("Refreshing Assistant settings…")


async def _settings_choice(shell: Shell) -> bool:
    config = await shell.configuration()
    await shell.runtimes()
    profile = shell.profiles.get(config["profile_id"], {})
    is_harness = config["backend"] == "harness"
    toggles = {
        "tools_enabled": "Built-in tools",
        "planning_enabled": "Plan before acting",
        "include_knowledge": "Project knowledge",
        "allow_cloud_knowledge": "Allow cloud knowledge",
        "allow_subagents": "Subagents",
        "allow_agent_messaging": "Main-agent messaging",
    }
    choices = [
        {"id": name, "name": f"{label}: {'on' if config.get(name) else 'off'}"}
        for name, label in toggles.items()
    ]
    choices.extend(
        {"id": item, "name": label}
        for item, label in (
            ("model", "Model runtime"),
            ("reasoning", "Reasoning effort"),
            (
                "mcp_server_ids",
                f"MCP servers: {len(config.get('mcp_server_ids') or [])} selected",
            ),
            ("hook_ids", "Lifecycle hooks"),
            ("environments", "Command environments"),
            ("max_active_subagents", "Maximum active subagents"),
            ("subagent_runtime", "Subagent runtime"),
            ("subagent_reasoning_effort", "Subagent reasoning effort"),
        )
    )
    if is_harness:
        choices.extend(
            {"id": name, "name": label}
            for name, label in (
                ("harness_mode", "Harness mode"),
                ("harness_service_tier", "Service tier"),
                ("harness_cyber_access_program", "Account access tier"),
            )
        )
    choices.append({"id": "__done", "name": "Done · return to chat"})
    selected = (await shell.ui.choose("Assistant settings (next turn)", choices))["id"]
    if selected == "__done":
        return True
    if selected == "model":
        await model(shell)
        return False
    if selected in toggles:
        await save(shell, {selected: not config.get(selected, False)})
        return False
    if selected == "mcp_server_ids":
        await select_mcp_servers(shell)
        return False
    if selected in {"hook_ids", "environments"}:
        if selected == "hook_ids":
            rows = await shell.api.request(
                "GET", "hooks", params={"engagement_id": shell.current().project_id}
            )
        else:
            discovery = await shell.api.request(
                "GET", "ssh-environments", params={"resolve": "false"}
            )
            rows = [
                host["environment"]
                for host in discovery.get("hosts", [])
                if host.get("environment") and host["environment"].get("enabled")
            ]
            for row in rows:
                row["name"] = row.get("display_name") or row["alias"]
        options = [
            {**row, "name": row.get("name") or row.get("label") or row["id"]}
            for row in rows
        ]
        if selected == "environments":
            ids = await shell.ui.multiple(
                "Command environments (empty means Core only)",
                options,
                shell.current().environments or [],
            )
            shell.current().environments = ids
            shell.ui.say(
                "Selected environments for the next send; Core validates access."
            )
        else:
            ids = await shell.ui.multiple(
                "Selected " + selected.replace("_ids", ""),
                options,
                config.get(selected) or [],
            )
            await save(shell, {selected: ids})
        return False
    if selected == "max_active_subagents":
        raw = (
            await shell.ui.ask("Maximum active subagents (blank means no limit)")
        ).strip()
        await save(shell, {selected: int(raw) if raw else None})
        return False
    if selected == "subagent_runtime":
        options = await shell.runtimes()
        choice = await shell.ui.choose("Subagent runtime", options)
        await save(
            shell,
            {
                "subagent_provider_id": choice["profile_id"]
                if choice["backend"] == "provider"
                else None,
                "subagent_harness_profile_id": choice["profile_id"]
                if choice["backend"] == "harness"
                else None,
                "subagent_model": choice["model"],
            },
        )
        return False
    field = (
        ("harness_reasoning_effort" if is_harness else "reasoning_effort")
        if selected == "reasoning"
        else selected
    )
    values: list[Any] = []
    if field == "harness_mode":
        values = (profile.get("capabilities") or {}).get("modes", [])
    elif field.startswith("harness_"):
        option: dict[str, Any] = next(
            (
                row
                for row in (profile.get("capabilities") or {}).get("model_options", [])
                if row.get("model") == config["model"]
            ),
            {},
        )
        prop = {
            "harness_reasoning_effort": "reasoning_efforts",
            "harness_service_tier": "service_tiers",
            "harness_cyber_access_program": "cyber_access_programs",
        }[field]
        values = option.get(prop) or []
    else:
        # These are Core's provider-neutral ReasoningEffort enum, not vendor model names.
        values = ["none", "minimal", "low", "medium", "high", "xhigh"]
    if not values:
        raise ClientError(
            "This runtime does not advertise that control for the selected model."
        )
    options = [{"id": "__default", "name": "Runtime default"}]
    options.extend(
        {"id": row.get("id"), "name": row.get("label") or row.get("id")}
        if isinstance(row, dict)
        else {"id": row, "name": row}
        for row in values
    )
    choice = (await shell.ui.choose("Choose " + selected.replace("_", " "), options))[
        "id"
    ]
    await save(
        shell,
        {
            field: ("automatic" if field == "harness_cyber_access_program" else None)
            if choice == "__default"
            else choice
        },
    )
    return False
