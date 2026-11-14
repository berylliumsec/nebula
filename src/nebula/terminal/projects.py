"""Project management over Core's API: create, rename, folder, policy, scope, archive."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .transport import ApiError, ClientError, key, terminal_text

if TYPE_CHECKING:
    from .shell import Shell

APPROVAL_POLICIES = {
    "on_boundary": "On boundary · prompt once for project networking",
    "always": "Always · prompt before every command",
    "never": "Never · no tool prompts",
}


def archived(project: dict[str, Any]) -> bool:
    """Core marks an archived project by status, as the GUI reads it."""
    return project.get("status") == "archived"


async def read(shell: Shell, project_id: str) -> dict[str, Any]:
    return await shell.api.request("GET", f"engagements/{key(project_id)}")


async def browse_folder(shell: Shell, start: str | None = None) -> str | None:
    """Choose a folder on the Core host with Core's bounded folder browser."""
    path, offset = start, 0
    while True:
        params: dict[str, Any] = {}
        if path:
            params["path"] = path
        if offset:
            params["offset"] = offset
        page = await shell.api.request("GET", "workspace-folders", params=params)
        options = [{"id": "__use", "name": f"Use this folder: {page['path']}"}]
        if page.get("parent"):
            options.append({"id": "__up", "name": "Parent folder"})
        options.append({"id": "__new", "name": "New folder here…"})
        options.extend(
            {"id": row["path"], "name": row["name"] + "/"}
            for row in page.get("directories", [])
        )
        if page.get("next_offset") is not None:
            options.append({"id": "__more", "name": "More folders"})
        chosen = await shell.ui.choose(
            "Folder on the Core host: " + terminal_text(page["path"]), options
        )
        if chosen["id"] == "__use":
            return page["path"]
        if chosen["id"] == "__up":
            path, offset = page["parent"], 0
        elif chosen["id"] == "__more":
            offset = page["next_offset"]
        elif chosen["id"] == "__new":
            name = (await shell.ui.ask("New folder name")).strip()
            if not name:
                continue
            created = await shell.api.request(
                "POST",
                "workspace-folders",
                json={"parent_path": page["path"], "name": name},
            )
            path, offset = created["path"], 0
        else:
            path, offset = chosen["id"], 0


async def create(shell: Shell) -> dict[str, Any]:
    """The GUI's New project form: name, client, Core-host folder and mode."""
    name = (await shell.ui.ask("Project name")).strip()
    if not name:
        raise ClientError("A project needs a name. Nothing was created.")
    client = (await shell.ui.ask("Client name (optional)")).strip()
    folder_choice = (
        await shell.ui.choose(
            "Project folder",
            [
                {
                    "id": "none",
                    "name": "No folder · Core creates the project workspace",
                },
                {"id": "browse", "name": "Choose a folder on the Core host…"},
            ],
        )
    )["id"]
    folder = await browse_folder(shell) if folder_choice == "browse" else None
    mode = (
        await shell.ui.choose(
            "Where project commands run",
            [
                {
                    "id": "host",
                    "name": "Host mode · commands run on the Core host (as the GUI creates projects)",
                },
                {"id": "docker", "name": "Docker mode · commands run in a container"},
            ],
        )
    )["id"]
    item = await shell.api.request(
        "POST",
        "engagements",
        json={
            "name": name,
            "description": "",
            "client_name": client or None,
            "status": "draft",
            "tags": [],
            "workspace_path": folder,
            "host_access_acknowledged": mode == "host",
            "request_id": str(uuid4()),
            "metadata": {},
        },
    )
    item = await read(shell, item["id"])
    shell.ui.say(f"Created {terminal_text(item['name'])}.")
    return item


async def archived_projects(shell: Shell) -> dict[str, Any] | None:
    """Restore or permanently delete an archived project."""
    rows = []
    offset = 0
    while True:
        page = await shell.api.page("engagements", offset=offset, limit=1000)
        rows.extend(row for row in page if archived(row))
        offset += len(page)
        if len(page) < 1000:
            break
    if not rows:
        shell.ui.say("No archived projects.")
        return None
    chosen = await shell.ui.choose(
        "Archived projects",
        [{**row, "name": str(row.get("name") or row["id"])} for row in rows],
    )
    action = (
        await shell.ui.choose(
            terminal_text(chosen["name"]),
            [
                {"id": "restore", "name": "Restore project"},
                {"id": "delete", "name": "Delete project permanently"},
                {"id": "back", "name": "Back"},
            ],
        )
    )["id"]
    if action == "restore":
        return await set_archived(shell, chosen["id"], False)
    if action == "delete":
        await delete(shell, chosen["id"])
    return None


async def set_archived(shell: Shell, project_id: str, value: bool) -> dict[str, Any]:
    current = await read(shell, project_id)
    if archived(current) != value:
        await shell.api.request(
            "PATCH",
            f"engagements/{key(project_id)}",
            json={
                "changes": {"status": "archived" if value else "active"},
                "expected_revision": current["revision"],
            },
        )
    observed = await read(shell, project_id)
    if archived(observed) != value:
        raise ClientError("Core did not confirm the change. Reopen /project.")
    shell.ui.say(
        f"{'Archived' if value else 'Restored'} {terminal_text(observed['name'])} in Core."
    )
    return observed


async def delete(shell: Shell, project_id: str) -> None:
    current = await read(shell, project_id)
    if not archived(current):
        raise ClientError("Archive the project before deleting it.")
    name = terminal_text(current["name"])
    if not await shell.ui.confirm(
        f"Permanently delete {name}? Its chats, notes and results are removed; "
        "a linked folder on the Core host is kept."
    ):
        return
    await shell.api.request(
        "DELETE",
        f"engagements/{key(project_id)}",
        headers={"If-Match": str(current["revision"])},
    )
    try:
        await read(shell, project_id)
    except ApiError as exc:
        if exc.status != 404:
            raise
    else:
        raise ClientError("Core did not confirm the deletion. Reopen /projects.")
    shell.ui.say(f"Deleted {name} permanently.")


async def policy(shell: Shell, project: dict[str, Any]) -> None:
    """Execution mode and command policy, as Settings › Project Policy edits them."""
    path = f"engagements/{key(project['id'])}/automation-policy"
    while True:
        current = await shell.api.request("GET", path)
        mode = current.get("execution_mode") or "docker"
        choices = [
            {
                "id": "mode",
                "name": f"Execution mode: {'Host' if mode == 'host' else 'Docker'}",
            },
            {
                "id": "approval",
                "name": "Command approval: "
                + APPROVAL_POLICIES.get(current.get("approval_policy"), "unknown"),
            },
            {
                "id": "network",
                "name": f"Network access: {'on' if current.get('network_enabled') else 'off'}",
            },
            {
                "id": "timeout",
                "name": f"Maximum command time: {int(current.get('max_timeout_ms') or 0) // 1000}s",
            },
            {"id": "__done", "name": "Done"},
        ]
        selected = (await shell.ui.choose("Project execution policy", choices))["id"]
        if selected == "__done":
            return
        change: dict[str, Any] = {}
        if selected == "mode":
            target = (
                await shell.ui.choose(
                    "Execution mode",
                    [
                        {
                            "id": "docker",
                            "name": "Docker · commands run in a container",
                        },
                        {
                            "id": "host",
                            "name": "Host · commands run directly on the Core host",
                        },
                    ],
                )
            )["id"]
            if target == "host" and mode != "host":
                if not await shell.ui.confirm(
                    f"Switch {terminal_text(project['name'])} to Host mode? Commands "
                    "can read and change files and reach the network as Core's user."
                ):
                    continue
            change = {
                "execution_mode": target,
                "host_access_acknowledged": target == "host",
            }
        elif selected == "approval":
            change["approval_policy"] = (
                await shell.ui.choose(
                    "Command approval",
                    [
                        {"id": name, "name": label}
                        for name, label in APPROVAL_POLICIES.items()
                    ],
                )
            )["id"]
        elif selected == "network":
            change["network_enabled"] = not current.get("network_enabled")
        else:
            raw = (
                await shell.ui.ask("Maximum command time in seconds (1–86400)")
            ).strip()
            seconds = int(raw)
            if not 1 <= seconds <= 86_400:
                raise ClientError("Choose between 1 and 86400 seconds.")
            change["max_timeout_ms"] = seconds * 1000
        body = {
            name: current.get(name)
            for name in (
                "execution_mode",
                "host_access_acknowledged",
                "approval_policy",
                "network_enabled",
                "runner_profile_id",
                "vpn_profile_id",
                "max_timeout_ms",
            )
        }
        body["host_access_acknowledged"] = (
            body.get("execution_mode") == "host"
            if "host_access_acknowledged" not in change
            else change["host_access_acknowledged"]
        )
        body.update(change, expected_revision=current["revision"])
        await shell.api.request("PUT", path, json=body)
        observed = await shell.api.request("GET", path)
        if any(observed.get(name) != value for name, value in change.items()):
            raise ClientError(
                "Core did not confirm the policy change. Reopen /project."
            )
        shell.ui.say("Saved the project execution policy in Core.")


SCOPE_FIELDS = (
    "allowed_cidrs",
    "allowed_domains",
    "allowed_urls",
    "allowed_ports",
    "allow_all_targets",
    "bypass_permissions",
    "not_before",
    "not_after",
    "prohibited_actions",
    "local_only",
    "tool_suggestions",
    "on_demand_tools",
    "web_search",
    "web_search_discloses_scope",
    "always_loaded_tools",
    "max_concurrency",
    "grants",
)


def _listed(value: str) -> list[str]:
    return [
        item.strip() for item in value.replace("\n", ",").split(",") if item.strip()
    ]


async def scope(shell: Shell, project: dict[str, Any]) -> None:
    """The GUI's Network scope form: a whole-scope write with Core readback."""
    path = f"engagements/{key(project['id'])}/scope"
    while True:
        current = await shell.api.request("GET", path)
        authorization = (
            "Allow all (bypass Nebula permissions)"
            if current.get("bypass_permissions")
            else "All destinations"
            if current.get("allow_all_targets")
            else "Bounded"
        )
        lists = {
            "allowed_domains": "Allowed domains",
            "allowed_cidrs": "Allowed CIDRs",
            "allowed_ports": "Allowed TCP ports",
            "allowed_urls": "URL-only entries",
            "prohibited_actions": "Prohibited actions",
        }
        toggles = {
            "local_only": "Local only",
            "tool_suggestions": "TypeSafe tool suggestions",
            "web_search": "Web search",
            "web_search_discloses_scope": "Web search may name in-scope targets",
        }
        choices = [{"id": "authorization", "name": f"Authorization: {authorization}"}]
        choices.extend(
            {
                "id": name,
                "name": f"{label}: "
                + (", ".join(str(item) for item in current.get(name) or []) or "none"),
            }
            for name, label in lists.items()
        )
        choices.extend(
            {"id": name, "name": f"{label}: {'on' if current.get(name) else 'off'}"}
            for name, label in toggles.items()
        )
        choices.extend(
            [
                {
                    "id": "window",
                    "name": "Active window: "
                    f"{current.get('not_before') or 'any time'} → {current.get('not_after') or 'no expiry'}",
                },
                {
                    "id": "max_concurrency",
                    "name": f"Maximum concurrency: {current.get('max_concurrency', 1)}",
                },
                {
                    "id": "always_loaded_tools",
                    "name": f"Always loaded tools: {len(current.get('always_loaded_tools') or [])}",
                },
                {"id": "__done", "name": "Done"},
            ]
        )
        selected = (await shell.ui.choose("Project network scope", choices))["id"]
        if selected == "__done":
            return
        change: dict[str, Any] = {}
        if selected == "authorization":
            target = (
                await shell.ui.choose(
                    "Authorization",
                    [
                        {"id": "bounded", "name": "Bounded · only listed destinations"},
                        {
                            "id": "all",
                            "name": "All destinations · every network target and port",
                        },
                        {
                            "id": "bypass",
                            "name": "Allow all · bypass Nebula permissions",
                        },
                    ],
                )
            )["id"]
            if target == "all" and not current.get("allow_all_targets"):
                if not await shell.ui.confirm("Allow every network target and port?"):
                    continue
            if target == "bypass" and not current.get("bypass_permissions"):
                if not await shell.ui.confirm(
                    "Bypass Nebula permissions for this project? Tools run without scope checks."
                ):
                    continue
            change = {
                "allow_all_targets": target in {"all", "bypass"},
                "bypass_permissions": target == "bypass",
            }
        elif selected in lists:
            raw = await shell.ui.ask(
                lists[selected] + " (comma-separated; blank clears)",
                default=", ".join(str(item) for item in current.get(selected) or []),
            )
            values: list[Any] = _listed(raw)
            if selected == "allowed_ports":
                try:
                    values = [int(item) for item in values]
                except ValueError as exc:
                    raise ClientError("Ports must be numbers.") from exc
            change[selected] = values
        elif selected in toggles:
            change[selected] = not current.get(selected)
        elif selected == "window":
            for name, label in (
                ("not_before", "Active from"),
                ("not_after", "Active until"),
            ):
                raw = (
                    await shell.ui.ask(
                        f"{label} (YYYY-MM-DD or ISO time; blank for none)",
                        default=current.get(name) or "",
                    )
                ).strip()
                if raw:
                    try:
                        datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise ClientError(f"{label} is not a date.") from exc
                change[name] = raw or None
        elif selected == "max_concurrency":
            value = int((await shell.ui.ask("Maximum concurrency (1–256)")).strip())
            if not 1 <= value <= 256:
                raise ClientError("Choose between 1 and 256.")
            change[selected] = value
        else:
            candidates = await shell.api.request("GET", path + "/tool-candidates")
            chosen = await shell.ui.multiple(
                "Always loaded tools",
                [
                    {
                        "id": row["name"],
                        "name": f"{row.get('server_name')} · {row.get('tool_name')}",
                    }
                    for row in candidates
                ],
                list(current.get("always_loaded_tools") or []),
            )
            change[selected] = chosen
        body = {name: current.get(name) for name in SCOPE_FIELDS if name in current}
        body.update(change, expected_revision=current["revision"])
        await shell.api.request("PUT", path, json=body)
        observed = await shell.api.request("GET", path)
        if any(
            observed.get(name) != value
            for name, value in change.items()
            if name not in {"not_before", "not_after"}
        ):
            raise ClientError("Core did not confirm the scope change. Reopen /project.")
        shell.ui.say("Saved the project scope in Core.")


async def project(shell: Shell, args: list[str]) -> None:
    """Manage the selected project, as the GUI's switcher, Butler and Settings do."""
    view = shell.current()
    current = await read(shell, view.project_id)
    try:
        policy_state = await shell.api.request(
            "GET", f"engagements/{key(view.project_id)}/automation-policy"
        )
        mode = policy_state.get("execution_mode") or "docker"
    except ApiError:
        # Command execution is optional; the other project actions still work.
        mode = "unavailable on this Core"
    for line in (
        f"Project: {terminal_text(current['name'])}"
        + (
            f" · client {terminal_text(current['client_name'])}"
            if current.get("client_name")
            else ""
        ),
        f"Folder: {terminal_text(current.get('workspace_path') or 'Core-managed workspace')}",
        f"Status: {current.get('status')} · execution mode: {mode}",
    ):
        shell.ui.say(line)
    actions = {
        "rename": "Rename project",
        "folder": "Relink project folder",
        "policy": "Execution mode and command policy",
        "scope": "Network scope",
        "archive": "Archive project",
        "archived": "Archived projects",
    }
    action = args[0] if args and args[0] in actions else None
    if action is None:
        action = (
            await shell.ui.choose(
                terminal_text(current["name"]),
                [{"id": name, "name": label} for name, label in actions.items()]
                + [{"id": "__done", "name": "Done"}],
            )
        )["id"]
    if action == "__done":
        return
    if action == "rename":
        name = (await shell.ui.ask("Project name", default=current["name"])).strip()
        if not name or name == current["name"]:
            shell.ui.say("Project name unchanged.")
            return
        await shell.api.request(
            "PATCH",
            f"engagements/{key(view.project_id)}",
            json={"changes": {"name": name}, "expected_revision": current["revision"]},
        )
        observed = await read(shell, view.project_id)
        if observed["name"] != name:
            raise ClientError("Core did not confirm the new name. Reopen /project.")
        view.project.update(observed)
        if hasattr(shell.ui, "set_identity"):
            shell.ui.set_identity(view.project, view.session)
        shell.ui.say(f"Renamed the project to {terminal_text(name)}.")
    elif action == "folder":
        folder = await browse_folder(shell, current.get("workspace_path"))
        if not folder or folder == current.get("workspace_path"):
            shell.ui.say("Project folder unchanged.")
            return
        if not await shell.ui.confirm(
            f"Relink {terminal_text(current['name'])} to {terminal_text(folder)}?"
        ):
            return
        await shell.api.request(
            "POST",
            f"engagements/{key(view.project_id)}/workspace/relink",
            json={"path": folder, "expected_revision": current["revision"]},
        )
        observed = await read(shell, view.project_id)
        view.project.update(observed)
        shell.ui.say(
            f"Project folder: {terminal_text(observed.get('workspace_path'))}."
        )
    elif action == "policy":
        await policy(shell, current)
    elif action == "scope":
        await scope(shell, current)
    elif action == "archive":
        if not await shell.ui.confirm(
            f"Archive {terminal_text(current['name'])}? It leaves the project list; "
            "/project archived restores it."
        ):
            return
        await set_archived(shell, view.project_id, True)
        await shell.select_project()
    elif action == "archived":
        restored = await archived_projects(shell)
        if restored is not None and await shell.ui.confirm(
            f"Open {terminal_text(restored['name'])} now?"
        ):
            await shell.select_chat(project=restored)
    if hasattr(shell, "refresh_navigation"):
        await shell.refresh_navigation()
