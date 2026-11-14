"""Capability-driven next-turn choices, using Core's existing validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .transport import ApiError, ClientError, key

if TYPE_CHECKING:
    from .shell import Shell

# Chat choices the GUI also remembers as the project's defaults for new chats.
REMEMBERED = {
    "reasoning_effort",
    "harness_reasoning_effort",
    "harness_service_tier",
    "harness_mode",
    "mcp_server_ids",
    "mcp_context_mode",
    "hook_ids",
    "allow_subagents",
    "subagent_provider_id",
    "subagent_harness_profile_id",
    "subagent_model",
    "max_active_subagents",
    "subagent_reasoning_effort",
}


def project_defaults(config: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Translate saved chat choices into Core's project assistant defaults."""
    changes = {name: value for name, value in patch.items() if name in REMEMBERED}
    if (
        "max_active_subagents" in changes
        and (changes["max_active_subagents"] or 0) > 32
    ):
        changes.pop("max_active_subagents")
    if {"backend", "profile_id", "model"} & set(patch):
        backend = patch.get("backend", config.get("backend"))
        changes["backend"] = backend
        changes["provider_id" if backend == "provider" else "harness_id"] = patch.get(
            "profile_id", config.get("profile_id")
        )
        changes["model"] = patch.get("model", config.get("model"))
    return changes


async def remember_defaults(shell: Shell, patch: dict[str, Any]) -> None:
    """Keep new chats in this project on the operator's latest choices."""
    view = shell.current()
    changes = project_defaults(view.config, patch)
    if not changes:
        return
    try:
        saved = await shell.api.request(
            "PATCH",
            f"engagements/{key(view.project_id)}/assistant-defaults",
            json={"changes": changes},
        )
    except ClientError as exc:
        # The chat setting is saved; only the project default for new chats
        # is unchanged, so report it without failing the change.
        shell.ui.say(f"Project default for new chats not updated: {exc}")
        return
    if isinstance(saved, dict):
        view.project["assistant_defaults"] = saved


async def save(shell: Shell, patch: dict[str, Any]) -> None:
    view = shell.current()
    if view.session is None:
        view.config.update(patch)
        await remember_defaults(shell, patch)
        shell.ui.say("Selected for the first turn and for new chats in this project.")
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
    await remember_defaults(shell, patch)
    shell.ui.say("Saved in Core for the next turn.")


async def model(shell: Shell) -> None:
    current = await shell.configuration()
    options = await shell.runtimes()
    if not options:
        raise ClientError("No enabled model is configured on Core.")
    chosen = await shell.ui.choose("Model runtime", options)
    patch = {name: chosen[name] for name in ("backend", "profile_id", "model")}
    view = shell.current()
    if view.session and chosen["backend"] == "provider":
        # A saved conversation switches provider models through Core's checked
        # runtime switch, also when it is leaving a harness, as the GUI does.
        await switch_provider(shell, current, chosen)
        await remember_defaults(shell, patch)
        view.draft.skill = None
        view.draft.skills.clear()
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


async def switch_provider(
    shell: Shell, current: dict[str, Any], chosen: dict[str, Any]
) -> None:
    view = shell.current()
    path = f"chat/sessions/{key(view.session_id)}/runtime-switch"
    tools = current.get("backend") == "provider" and bool(current.get("tools_enabled"))

    async def preflight() -> dict[str, Any]:
        for attempt in range(2):
            request = {
                "provider_id": chosen["profile_id"],
                "model": chosen["model"],
                "tools_enabled": tools,
                "expected_session_revision": view.session["revision"],
            }
            try:
                result = await shell.api.request(
                    "POST", path + "/preflight", json=request
                )
                return {**result, "request": request}
            except ApiError as exc:
                # Core bumps the revision whenever a turn starts or settles.
                if exc.status != 409 or attempt:
                    raise
                view.session = await shell.api.request(
                    "GET", f"chat-sessions/{key(view.session_id)}"
                )
        raise AssertionError("unreachable")

    checked = await preflight()
    if not checked.get("compatible") and checked.get("reason_code") == (
        "model_not_tool_verified"
    ):
        shell.ui.say(f"Verifying {chosen['model']} for tool use…")
        profile = await shell.api.request(
            "GET", f"providers/{key(chosen['profile_id'])}"
        )
        await shell.api.request(
            "POST",
            f"providers/{key(chosen['profile_id'])}/capabilities/verify",
            json={"model": chosen["model"], "expected_revision": profile["revision"]},
        )
        shell.profiles.pop(chosen["profile_id"], None)
        checked = await preflight()
    if not checked.get("compatible"):
        raise ClientError(
            checked.get("reason") or "This model cannot accept the saved conversation."
        )
    request = checked["request"]
    if checked.get("requires_compaction_confirmation"):
        if not await shell.ui.confirm(
            checked.get("reason") or "Compact the saved context to use this model?"
        ):
            return
        request["confirmation_token"] = checked["confirmation_token"]
    await shell.api.request("POST", path, json=request)
    await shell.configuration()
    shell.ui.say(
        "Runtime switch saved in Core; it applies after the running work."
        if checked.get("activation") == "after_active_work"
        else "Runtime switch saved in Core."
    )


async def resume_harness_session(shell: Shell) -> None:
    """Start a new chat on a saved or external harness session, as the GUI can."""
    view = shell.current()
    config = await shell.configuration()
    profile_id = config["profile_id"]
    saved = await shell.api.page(
        "harness-sessions", offset=0, limit=200, engagement_id=view.project_id
    )
    options: list[dict[str, Any]] = [
        {"id": "__new", "name": "Start a new harness session"}
    ]
    known = set()
    for row in saved:
        if row.get("harness_profile_id") != profile_id:
            continue
        known.add(row.get("external_session_id"))
        options.append(
            {
                "id": row["id"],
                "name": f"{row.get('display_name') or row['id']} · {row.get('model')}"
                f" · {row.get('status', 'saved')}",
            }
        )
    try:
        external = await shell.api.request(
            "GET",
            f"harnesses/{key(profile_id)}/external-sessions",
            params={"engagement_id": view.project_id},
        )
    except ClientError:
        external = []
    for row in external:
        if row.get("internal_session_id") or row["external_session_id"] in known:
            continue
        options.append(
            {
                "id": "external:" + row["external_session_id"],
                "name": f"{row['display_name']} · outside Nebula · import",
                "external": row,
            }
        )
    chosen = await shell.ui.choose("Harness session for this new chat", options)
    if chosen["id"] == "__new":
        config.pop("harness_session_id", None)
        shell.ui.say("This chat starts a new harness session.")
        return
    identifier = chosen["id"]
    if "external" in chosen:
        row = chosen["external"]
        imported = await shell.api.request(
            "POST",
            f"harnesses/{key(profile_id)}/external-sessions/import",
            json={
                "engagement_id": view.project_id,
                "external_session_id": row["external_session_id"],
                "display_name": row["display_name"],
                "model": config.get("model") or row.get("model"),
            },
        )
        identifier = imported["id"]
    config["harness_session_id"] = identifier
    shell.ui.say("This chat resumes the selected harness session on its first message.")


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


ALWAYS_LOAD = "__always_load"


def mcp_state(row: dict[str, Any], selected: bool, mode: str, excluded: bool) -> str:
    """The GUI's per-server delivery state for the next provider turn."""
    if excluded:
        return "excluded next turn"
    if selected:
        return "on demand" if mode == "on_demand" else "always loaded"
    if (row.get("metadata") or {}).get("engagement_id"):
        return "select to grant this assistant"
    return "on demand" if mode == "on_demand" else "not sent"


async def select_mcp_servers(shell: Shell, args: list[str] | None = None) -> None:
    """Select visible MCP profiles on demand; Core owns the saved chat setting."""
    config = await shell.configuration()
    current = list(config.get("mcp_server_ids") or [])
    provider = config.get("backend") == "provider"
    mode = config.get("mcp_context_mode") or "on_demand"
    excluded = list(config.get("excluded_mcp_server_ids") or [])
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
        state = (
            " · " + mcp_state(row, identifier in current, mode, identifier in excluded)
            if provider and not reason
            else ""
        )
        options.append(
            {
                "id": identifier,
                "name": row["name"],
                "label": f"{row['name']} · {reason or f'{tool_count} tools'}{state}",
                "disabled": bool(reason),
                "disabled_reason": reason,
                "selection_group": "mcp",
                "selection_group_limit": 64,
                "global": not (row.get("metadata") or {}).get("engagement_id"),
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
    if provider and args and args[0] == "exclude":
        candidates = [
            row
            for row in options
            if row.get("global")
            and not row.get("disabled")
            and row["id"] not in current
        ]
        if not candidates:
            raise ClientError("No on-demand MCP servers to exclude next turn.")
        chosen_excluded = await shell.ui.multiple(
            "Exclude MCP servers next turn · their tools are not offered",
            candidates,
            [
                identifier
                for identifier in excluded
                if identifier in {row["id"] for row in candidates}
            ],
        )
        if sorted(chosen_excluded) == sorted(excluded):
            shell.ui.say(f"MCP exclusions unchanged: {len(excluded)} excluded.")
            return
        await save(shell, {"excluded_mcp_server_ids": chosen_excluded})
        shell.ui.say(
            f"{len(chosen_excluded)} MCP server"
            f"{'s' if len(chosen_excluded) != 1 else ''} excluded next turn."
        )
        return
    initial = list(current)
    if provider:
        options.insert(
            0,
            {
                "id": ALWAYS_LOAD,
                "name": "Always load selected tool schemas",
                "label": "Always load selected tool schemas (otherwise loaded on demand)",
            },
        )
        if mode == "always_loaded":
            initial.insert(0, ALWAYS_LOAD)
    chosen = await shell.ui.multiple(
        "MCP servers · selected servers go with each turn", options, initial
    )
    next_mode = "always_loaded" if ALWAYS_LOAD in chosen else "on_demand"
    chosen = [identifier for identifier in chosen if identifier != ALWAYS_LOAD]
    patch: dict[str, Any] = {}
    if chosen != current:
        patch["mcp_server_ids"] = chosen
        if provider:
            kept = [identifier for identifier in excluded if identifier not in chosen]
            if kept != excluded:
                patch["excluded_mcp_server_ids"] = kept
    if provider and next_mode != mode:
        patch["mcp_context_mode"] = next_mode
    if not patch:
        shell.ui.say(f"MCP servers unchanged: {len(current)} selected.")
        return
    await save(shell, patch)
    shell.ui.say(
        "MCP servers selected: "
        + (", ".join(row["name"] for row in options if row["id"] in chosen) or "none")
        + (
            f" · tool schemas {'always loaded' if next_mode == 'always_loaded' else 'loaded on demand'}"
            if "mcp_context_mode" in patch
            else ""
        )
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
        if shell.current().session is None:
            resumed = config.get("harness_session_id")
            choices.append(
                {
                    "id": "harness_session",
                    "name": "Resume a harness session: "
                    + ("selected" if resumed else "new session"),
                }
            )
    choices.append({"id": "__done", "name": "Done · return to chat"})
    selected = (await shell.ui.choose("Assistant settings (next turn)", choices))["id"]
    if selected == "__done":
        return True
    if selected == "model":
        await model(shell)
        return False
    if selected == "harness_session":
        await resume_harness_session(shell)
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
