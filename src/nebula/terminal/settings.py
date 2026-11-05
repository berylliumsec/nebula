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


async def skill_catalog(shell: Shell) -> list[dict[str, Any]]:
    view = shell.current()
    config = await shell.configuration()
    if config["backend"] == "provider":
        return await shell.api.request(
            "GET", "skills", params={"engagement_id": view.project_id}
        )
    await shell.runtimes()
    profile = shell.profiles.get(config["profile_id"], {})
    if not (profile.get("capabilities") or {}).get("skill_invocation"):
        raise ClientError(
            "This harness does not advertise structured skill invocation."
        )
    if not (profile.get("native_capabilities") or {}).get("skills"):
        raise ClientError(
            "Installed skills are disabled for this harness. Enable them on Core."
        )
    return await shell.api.request(
        "GET",
        f"harnesses/{key(config['profile_id'])}/skills",
        params={"engagement_id": view.project_id},
    )


async def select_skill(shell: Shell, selected: str | None = None) -> None:
    skills = await skill_catalog(shell)
    if not skills:
        raise ClientError(
            "No skills were discovered for this project and runtime on Core."
        )
    options = [{**row, "id": row["path"], "name": row["name"]} for row in skills]
    if not selected:
        options.insert(0, {"id": "__none", "name": "No skill"})
    item = await shell.ui.choose("Skill for the next turn", options, selected=selected)
    shell.current().draft.skill = (
        None if item["id"] == "__none" else {"name": item["name"], "path": item["path"]}
    )
    shell.ui.say(
        "Skill cleared."
        if item["id"] == "__none"
        else f"Selected ${item['name']} for one turn. Skill files remain on Core."
    )


async def settings(shell: Shell) -> None:
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
            ("mcp_server_ids", "MCP servers"),
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
    selected = (await shell.ui.choose("Assistant settings (next turn)", choices))["id"]
    if selected == "model":
        await model(shell)
        return
    if selected in toggles:
        await save(shell, {selected: not config.get(selected, False)})
        return
    if selected in {"mcp_server_ids", "hook_ids", "environments"}:
        if selected == "mcp_server_ids":
            rows = await shell.api.page(
                "mcp-servers", engagement_id=shell.current().project_id
            )
            rows = [
                row
                for row in rows
                if row.get("enabled")
                and (
                    not row.get("engagement_id")
                    or row["engagement_id"] == shell.current().project_id
                )
            ]
        elif selected == "hook_ids":
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
        return
    if selected == "max_active_subagents":
        raw = (
            await shell.ui.ask("Maximum active subagents (blank means no limit)")
        ).strip()
        await save(shell, {selected: int(raw) if raw else None})
        return
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
        return
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
