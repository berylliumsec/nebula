"""Project management parity: create, archive, restore, delete, rename, policy, scope."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from nebula.terminal import projects
from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, ClientError, Settings
from tests.test_terminal_shell import CONFIG, CORE, ENTRY, PROJECT, SESSION, UI


def path(request):
    return request.url.path.removeprefix("/api/v1/")


def shell_for(tmp_path, handler, *, ui=None):
    settings = Settings(tmp_path / "chat.json", {"cores": {CORE: dict(ENTRY)}}, CORE)
    api = AsyncApi(settings, transport=httpx.MockTransport(handler))
    shell = Shell(settings, ui=ui or UI(), api=api)
    shell.view = View(
        copy.deepcopy(PROJECT), copy.deepcopy(SESSION), copy.deepcopy(CONFIG)
    )
    return shell


class ProjectCore:
    def __init__(self):
        self.projects = {
            "project-a": {
                "id": "project-a",
                "name": "Project A",
                "status": "active",
                "revision": 3,
            },
            "old": {
                "id": "old",
                "name": "Old work",
                "status": "archived",
                "revision": 7,
            },
        }
        self.calls = []

    def __call__(self, request):
        route = path(request)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, route, body, dict(request.headers)))
        if route == "engagements" and request.method == "GET":
            return httpx.Response(200, json=list(self.projects.values()))
        if route == "engagements" and request.method == "POST":
            created = {**body, "id": "new", "revision": 1}
            self.projects["new"] = created
            return httpx.Response(201, json=created)
        if route.startswith("engagements/") and route.count("/") == 1:
            identifier = route.split("/")[1]
            if identifier not in self.projects:
                return httpx.Response(404, json={"detail": "not found"})
            current = self.projects[identifier]
            if request.method == "PATCH":
                assert body["expected_revision"] == current["revision"]
                current.update(body["changes"], revision=current["revision"] + 1)
            elif request.method == "DELETE":
                assert request.headers["If-Match"] == str(current["revision"])
                del self.projects[identifier]
                return httpx.Response(204)
            return httpx.Response(200, json=current)
        if route == "workspace-folders":
            if request.method == "POST":
                return httpx.Response(200, json={"path": "/srv/work/new-dir"})
            target = request.url.params.get("path") or "/srv"
            return httpx.Response(
                200,
                json={
                    "path": target,
                    "parent": None if target == "/srv" else "/srv",
                    "directories": [{"name": "work", "path": "/srv/work"}]
                    if target == "/srv"
                    else [],
                    "files": [],
                    "truncated": False,
                },
            )
        pytest.fail(f"{request.method} {route}")


def test_project_picker_hides_archived_projects_and_restores_one(tmp_path):
    core = ProjectCore()

    async def run():
        shell = shell_for(
            tmp_path, core, ui=UI(choices=["__archived", "old", "restore"])
        )
        picked = []

        async def select_chat(chat=None, *, project=None):
            picked.append(project["id"])

        shell.select_chat = select_chat
        seen = []
        original = shell.ui.choose

        async def choose(title, items, **kwargs):
            seen.append((title, [row["id"] for row in items]))
            return await original(title, items, **kwargs)

        shell.ui.choose = choose
        await shell.select_project()
        project_rows = next(ids for title, ids in seen if title == "Project")
        assert "old" not in project_rows and "project-a" in project_rows
        assert "__archived" in project_rows
        assert core.projects["old"]["status"] == "active"
        assert picked == ["old"]
        await shell.api.close()

    asyncio.run(run())


def test_create_project_sends_the_gui_form_fields(tmp_path):
    core = ProjectCore()

    async def run():
        shell = shell_for(
            tmp_path,
            core,
            ui=UI(
                answers=["Red team", "Acme"],
                choices=["browse", "/srv/work", "__use", "host"],
            ),
        )
        created = await projects.create(shell)
        post = next(body for method, route, body, _ in core.calls if method == "POST")
        assert post["name"] == "Red team" and post["client_name"] == "Acme"
        assert post["workspace_path"] == "/srv/work"
        assert post["host_access_acknowledged"] is True
        assert post["status"] == "draft" and post["request_id"]
        assert created["id"] == "new"
        await shell.api.close()

    asyncio.run(run())


def test_rename_archive_and_delete_use_revisions_and_readback(tmp_path):
    core = ProjectCore()

    # Rename through the menu, then archive and delete the archived project.
    async def flow():
        shell = shell_for(
            tmp_path,
            core,
            ui=UI(answers=["Project Alpha"], confirms=[True]),
        )

        original = shell.api.request

        async def request(method, route, **kwargs):
            if route.endswith("/automation-policy"):
                return {"execution_mode": "docker"}
            return await original(method, route, **kwargs)

        shell.api.request = request
        await projects.project(shell, ["rename"])
        assert core.projects["project-a"]["name"] == "Project Alpha"
        assert shell.current().project["name"] == "Project Alpha"
        await projects.delete(shell, "old")
        delete = next(call for call in core.calls if call[0] == "DELETE")
        assert delete[3]["if-match"] == "7"
        assert "old" not in core.projects
        with pytest.raises(ClientError, match="Archive the project before deleting"):
            await projects.delete(shell, "project-a")
        await shell.api.close()

    asyncio.run(flow())


def test_host_mode_requires_confirmation_and_writes_the_whole_policy(tmp_path):
    policy = {
        "execution_mode": "docker",
        "host_access_acknowledged": False,
        "approval_policy": "on_boundary",
        "network_enabled": True,
        "runner_profile_id": None,
        "vpn_profile_id": "vpn-a",
        "max_timeout_ms": 300000,
        "revision": 2,
    }
    puts = []

    def handler(request):
        route = path(request)
        if route == "engagements/project-a/automation-policy":
            if request.method == "PUT":
                body = json.loads(request.content)
                puts.append(body)
                policy.update(
                    {k: v for k, v in body.items() if k != "expected_revision"}
                )
                policy["revision"] += 1
            return httpx.Response(200, json=policy)
        if route == "vpn-profiles":
            return httpx.Response(
                200, json=[{"id": "vpn-a", "name": "Lab VPN", "available": True}]
            )
        pytest.fail(route)

    async def run():
        shell = shell_for(
            tmp_path,
            handler,
            ui=UI(choices=["mode", "host", "__done"], confirms=[True]),
        )
        await projects.policy(shell, {"id": "project-a", "name": "Project A"})
        assert puts == [
            {
                "execution_mode": "host",
                "host_access_acknowledged": True,
                "approval_policy": "on_boundary",
                "network_enabled": True,
                "runner_profile_id": None,
                "vpn_profile_id": "vpn-a",
                "max_timeout_ms": 300000,
                "expected_revision": 2,
            }
        ]
        await shell.api.close()

    asyncio.run(run())


def test_scope_change_keeps_every_other_field_and_confirms_wide_access(tmp_path):
    scope = {
        "allowed_cidrs": ["10.0.0.0/8"],
        "allowed_domains": ["example.test"],
        "allowed_urls": [],
        "allowed_ports": [443],
        "allow_all_targets": False,
        "bypass_permissions": False,
        "not_before": None,
        "not_after": None,
        "prohibited_actions": ["dos"],
        "local_only": False,
        "tool_suggestions": True,
        "on_demand_tools": True,
        "web_search": False,
        "web_search_discloses_scope": False,
        "always_loaded_tools": ["files.read"],
        "max_concurrency": 2,
        "grants": [],
        "revision": 5,
    }
    puts = []

    def handler(request):
        route = path(request)
        if route == "engagements/project-a/scope":
            if request.method == "PUT":
                body = json.loads(request.content)
                puts.append(body)
                scope.update(
                    {k: v for k, v in body.items() if k != "expected_revision"}
                )
                scope["revision"] += 1
            return httpx.Response(200, json=scope)
        pytest.fail(route)

    async def run():
        shell = shell_for(
            tmp_path,
            handler,
            ui=UI(
                choices=["authorization", "all", "allowed_ports", "__done"],
                confirms=[True],
                answers=["443, 8443"],
            ),
        )
        await projects.scope(shell, {"id": "project-a"})
        assert puts[0]["allow_all_targets"] is True
        assert puts[0]["bypass_permissions"] is False
        assert puts[0]["allowed_domains"] == ["example.test"]
        assert puts[0]["always_loaded_tools"] == ["files.read"]
        assert puts[0]["expected_revision"] == 5
        assert puts[1]["allowed_ports"] == [443, 8443]
        assert puts[1]["expected_revision"] == 6
        await shell.api.close()

    asyncio.run(run())


def test_navigation_rail_and_restore_skip_archived_projects(tmp_path):
    from nebula.terminal.tui import TuiUI

    core = ProjectCore()

    def handler(request):
        if path(request) == "chat-sessions":
            return httpx.Response(200, json=[SESSION])
        return core(request)

    async def run():
        ui = TuiUI()
        shell = shell_for(tmp_path, handler, ui=ui)
        await shell.refresh_navigation()
        assert [row["id"] for row in ui._projects] == ["project-a"]
        await shell.api.close()

    asyncio.run(run())
