"""Follow-up queue, model switching, MCP delivery, consent and project defaults."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from nebula.terminal import followups
from nebula.terminal.attachments import attachments
from nebula.terminal.settings import model, resume_harness_session, select_mcp_servers
from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, Settings
from nebula.terminal.ui import CancelledInput
from tests.test_terminal_shell import CONFIG, CORE, ENTRY, PROJECT, SESSION, UI


def path(request):
    return request.url.path.removeprefix("/api/v1/")


def shell_for(tmp_path, handler, *, ui=None, saved=True, config=None):
    settings = Settings(tmp_path / "chat.json", {"cores": {CORE: dict(ENTRY)}}, CORE)
    api = AsyncApi(settings, transport=httpx.MockTransport(handler))
    shell = Shell(settings, ui=ui or UI(), api=api)
    shell.view = View(
        copy.deepcopy(PROJECT),
        copy.deepcopy(SESSION) if saved else None,
        copy.deepcopy(config or CONFIG),
    )
    shell.profiles["provider-a"] = {"id": "provider-a", "is_local": True}
    return shell


def item(identifier, text, status="queued"):
    return {
        "id": identifier,
        "status": status,
        "request": {"messages": [{"role": "user", "content": text}]},
    }


class QueueCore:
    """A Core queue that applies the same actions and revision checks."""

    def __init__(self, items, *, busy=False, paused=False):
        self.queue = {"revision": 1, "paused": paused, "items": items}
        self.busy = busy
        self.writes = []
        self.cancelled = False

    def __call__(self, request):
        route = path(request)
        if route == "chat/sessions/chat-a/queue":
            if request.method == "GET":
                return httpx.Response(200, json=self.queue)
            body = json.loads(request.content)
            assert body["expected_revision"] == self.queue["revision"]
            self.writes.append(body)
            action = body["action"]
            items = self.queue["items"]
            if action == "pause":
                self.queue["paused"] = True
            elif action == "resume":
                self.queue["paused"] = False
            elif action == "enqueue":
                new = {
                    "id": f"new-{len(items)}",
                    "status": "queued",
                    "request": body["request"],
                }
                items.insert(0, new) if body.get("first") else items.append(new)
                self.queue["paused"] = body.get("paused", False) or self.queue["paused"]
            elif action == "reorder":
                by_id = {row["id"]: row for row in items}
                assert set(body["order"]) == {
                    row["id"] for row in items if row["status"] == "queued"
                }
                self.queue["items"] = [by_id[key] for key in body["order"]]
            elif action == "remove":
                self.queue["items"] = [
                    row for row in items if row["id"] != body["item_id"]
                ]
            self.queue["revision"] += 1
            return httpx.Response(200, json=self.queue)
        if route == "chat/sessions/chat-a/state":
            return httpx.Response(
                200,
                json={
                    "session_id": "chat-a",
                    "turn_id": "turn-a",
                    "busy": self.busy and not self.cancelled,
                    "pending": [],
                    "actions": ["stop"] if self.busy and not self.cancelled else [],
                    "detail": "Working" if self.busy else "Ready",
                },
            )
        if route == "chat/turns/turn-a/cancel":
            self.writes.append({"action": "cancel"})
            self.cancelled = True
            return httpx.Response(200, json={"status": "cancelled"})
        if route == "chat-sessions/chat-a/assistant-config":
            return httpx.Response(200, json={"settings": CONFIG, "revision": 2})
        pytest.fail(route)


def test_queue_moves_one_step_and_keeps_every_waiting_item_in_order(tmp_path):
    core = QueueCore([item("a", "first"), item("b", "second"), item("c", "third")])

    async def run():
        shell = shell_for(tmp_path, core, ui=UI(choices=["move", "a", "down"]))
        await followups.queue(shell, [])
        assert core.writes == [
            {"action": "reorder", "expected_revision": 1, "order": ["b", "a", "c"]}
        ]
        assert any("1. Queued · second" in line for line in shell.ui.output)
        await shell.api.close()

    asyncio.run(run())


@pytest.mark.parametrize("busy", [False, True])
def test_queue_add_holds_for_later_only_when_idle(tmp_path, busy):
    core = QueueCore([], busy=busy)

    async def run():
        shell = shell_for(tmp_path, core, ui=UI(answers=["Check the logs next"]))
        await followups.queue(shell, ["add"])
        (write,) = core.writes
        assert write["action"] == "enqueue" and write["paused"] is (not busy)
        assert write["request"]["messages"][-1]["content"] == "Check the logs next"
        await shell.api.close()

    asyncio.run(run())


def test_now_stops_the_running_turn_and_sends_the_message_next(tmp_path):
    core = QueueCore([item("a", "older follow-up")], busy=True)

    async def run():
        shell = shell_for(tmp_path, core)
        await followups.now(shell, ["Use", "the", "staging", "host"])
        assert [write["action"] for write in core.writes] == [
            "pause",
            "enqueue",
            "cancel",
            "resume",
        ]
        enqueue = core.writes[1]
        assert enqueue["first"] is True and enqueue["paused"] is True
        assert core.queue["items"][0]["request"]["messages"][-1]["content"] == (
            "Use the staging host"
        )
        assert not core.queue["paused"]
        assert "Stopped the running turn; your message sends next." in shell.ui.output
        await shell.api.close()

    asyncio.run(run())


def test_send_now_moves_an_existing_follow_up_first_then_stops(tmp_path):
    core = QueueCore([item("a", "first"), item("b", "urgent")], busy=True)

    async def run():
        shell = shell_for(tmp_path, core, ui=UI(choices=["send_now", "b"]))
        await followups.queue(shell, [])
        assert [write["action"] for write in core.writes] == [
            "pause",
            "reorder",
            "cancel",
            "resume",
        ]
        assert core.writes[1]["order"] == ["b", "a"]
        await shell.api.close()

    asyncio.run(run())


def test_model_switch_leaves_a_harness_and_verifies_a_new_model(tmp_path):
    calls = []
    preflights = iter(
        [
            httpx.Response(409, json={"detail": "stale revision"}),
            httpx.Response(
                200,
                json={
                    "compatible": False,
                    "reason_code": "model_not_tool_verified",
                    "reason": "Verify first",
                },
            ),
            httpx.Response(
                200, json={"compatible": True, "activation": "after_active_work"}
            ),
        ]
    )

    def handler(request):
        route = path(request)
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, route, body))
        if route == "chat-sessions/chat-a/assistant-config":
            return httpx.Response(
                200,
                json={
                    "settings": {**CONFIG, "backend": "harness", "profile_id": "h"},
                    "revision": 2,
                },
            )
        if route in {"providers", "harnesses"}:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "provider-b",
                        "name": "Provider B",
                        "enabled": True,
                        "model_allowlist": ["model-b"],
                    }
                ]
                if route == "providers"
                else [],
            )
        if route == "chat-sessions/chat-a":
            return httpx.Response(200, json={**SESSION, "revision": 5})
        if route.endswith("runtime-switch/preflight"):
            return next(preflights)
        if route == "providers/provider-b":
            return httpx.Response(200, json={"id": "provider-b", "revision": 9})
        if route == "providers/provider-b/capabilities/verify":
            return httpx.Response(200, json={"status": "verified"})
        if route.endswith("runtime-switch"):
            return httpx.Response(200, json={"ok": True})
        if route == "engagements/project-a/assistant-defaults":
            return httpx.Response(200, json=body["changes"])
        pytest.fail(route)

    async def run():
        shell = shell_for(
            tmp_path, handler, ui=UI(choices=["provider:provider-b:model-b"])
        )
        await model(shell)
        routes = [(method, route) for method, route, _ in calls]
        verify = routes.index(("POST", "providers/provider-b/capabilities/verify"))
        assert (
            routes.count(("POST", "chat/sessions/chat-a/runtime-switch/preflight")) == 3
        )
        assert verify < routes.index(("POST", "chat/sessions/chat-a/runtime-switch"))
        switch = next(
            body for _, route, body in calls if route.endswith("/runtime-switch")
        )
        assert switch == {
            "provider_id": "provider-b",
            "model": "model-b",
            "tools_enabled": False,
            "expected_session_revision": 5,
        }
        defaults = next(
            body for _, route, body in calls if route.endswith("assistant-defaults")
        )
        assert defaults == {
            "changes": {
                "backend": "provider",
                "provider_id": "provider-b",
                "model": "model-b",
            }
        }
        assert any("applies after the running work" in line for line in shell.ui.output)
        await shell.api.close()

    asyncio.run(run())


def test_mcp_tool_schema_mode_and_next_turn_exclusions_are_saved(tmp_path):
    config = {**CONFIG, "mcp_server_ids": ["chosen"], "mcp_context_mode": "on_demand"}
    writes = []
    servers = [
        {
            "id": server,
            "name": server,
            "enabled": True,
            "capabilities": {"checked_at": "now", "tools": [{"name": "t"}]},
        }
        for server in ("chosen", "other")
    ]

    def handler(request):
        route = path(request)
        if route == "chat-sessions/chat-a/assistant-config":
            if request.method == "PATCH":
                patch = json.loads(request.content)["settings"]
                writes.append(patch)
                config.update(patch)
            return httpx.Response(200, json={"settings": config, "revision": 3})
        if route == "mcp-servers":
            return httpx.Response(200, json=servers)
        if route == "engagements/project-a/assistant-defaults":
            return httpx.Response(200, json={})
        pytest.fail(route)

    async def run():
        ui = UI(choices=[["__always_load", "chosen"], ["other"]])
        shell = shell_for(tmp_path, handler, ui=ui, config=config)
        await select_mcp_servers(shell)
        await select_mcp_servers(shell, ["exclude"])
        assert writes == [
            {"mcp_context_mode": "always_loaded"},
            {"excluded_mcp_server_ids": ["other"]},
        ]
        await shell.api.close()

    asyncio.run(run())


def test_always_allow_saves_standing_consent_on_the_runtime(tmp_path):
    patches = []

    def handler(request):
        route = path(request)
        if route == "providers/cloud-a" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "cloud-a",
                    "revision": 4,
                    "privacy": {"local_only": False, "permits_sensitive_data": True},
                },
            )
        if route == "providers/cloud-a":
            body = json.loads(request.content)
            patches.append(body)
            return httpx.Response(
                200, json={"id": "cloud-a", "privacy": body["changes"]["privacy"]}
            )
        pytest.fail(route)

    async def run():
        config = {**CONFIG, "profile_id": "cloud-a", "tools_enabled": True}
        shell = shell_for(tmp_path, handler, ui=UI(choices=["always"]), config=config)
        shell.current().session = None
        shell.profiles = {
            "cloud-a": {
                "id": "cloud-a",
                "name": "Cloud A",
                "privacy": {"local_only": False, "permits_sensitive_data": True},
            }
        }
        body = await shell.body("Use the tools")
        assert body["allow_cloud_tool_results"] is True
        assert patches == [
            {
                "changes": {
                    "privacy": {
                        "local_only": False,
                        "permits_sensitive_data": True,
                        "auto_share_tool_results": True,
                    }
                },
                "expected_revision": 4,
            }
        ]
        shell.ui = UI()
        assert (await shell.body("Again"))["allow_cloud_tool_results"] is True
        shell.ui = UI(choices=["cancel"])
        shell.profiles["cloud-a"]["privacy"]["auto_share_tool_results"] = False
        with pytest.raises(CancelledInput):
            await shell.body("Declined")
        await shell.api.close()

    asyncio.run(run())


def test_new_chat_starts_with_the_projects_remembered_choices(tmp_path):
    def handler(request):
        route = path(request)
        if route == "engagements/project-a/assistant-defaults":
            return httpx.Response(
                200,
                json={
                    "backend": "provider",
                    "provider_id": "provider-a",
                    "model": "fixture-model",
                    "mcp_server_ids": ["mcp-a"],
                    "hook_ids": ["hook-a"],
                    "reasoning_effort": "high",
                    "mcp_context_mode": "always_loaded",
                    "allow_subagents": True,
                },
            )
        if route == "providers":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "provider-a",
                        "name": "Provider A",
                        "enabled": True,
                        "model_allowlist": ["fixture-model"],
                    }
                ],
            )
        if route == "harnesses":
            return httpx.Response(200, json=[])
        pytest.fail(route)

    async def run():
        shell = shell_for(tmp_path, handler, saved=False)
        shell.current().config = {}
        config = await shell.configuration()
        assert config["mcp_server_ids"] == ["mcp-a"]
        assert config["hook_ids"] == ["hook-a"]
        assert config["reasoning_effort"] == "high"
        assert config["mcp_context_mode"] == "always_loaded"
        assert config["allow_subagents"] is True
        await shell.api.close()

    asyncio.run(run())


def test_new_harness_chat_resumes_an_imported_external_session(tmp_path):
    imported = []

    def handler(request):
        route = path(request)
        if route == "harness-sessions":
            return httpx.Response(200, json=[])
        if route == "harnesses/h/external-sessions":
            return httpx.Response(
                200,
                json=[
                    {
                        "external_session_id": "ext-1",
                        "display_name": "Desk session",
                        "model": "m",
                    }
                ],
            )
        if route == "harnesses/h/external-sessions/import":
            imported.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "hs-9"})
        pytest.fail(route)

    async def run():
        config = {**CONFIG, "backend": "harness", "profile_id": "h", "model": "m"}
        shell = shell_for(
            tmp_path,
            handler,
            saved=False,
            config=config,
            ui=UI(choices=["external:ext-1"]),
        )
        await resume_harness_session(shell)
        assert imported == [
            {
                "engagement_id": "project-a",
                "external_session_id": "ext-1",
                "display_name": "Desk session",
                "model": "m",
            }
        ]
        shell.profiles["h"] = {"id": "h", "is_local": True}
        body = await shell.body("Continue")
        assert body["harness_session_id"] == "hs-9"
        assert body["harness_profile_id"] == "h"
        await shell.api.close()

    asyncio.run(run())


def test_attachments_can_all_be_removed_at_once(tmp_path):
    async def run():
        shell = shell_for(
            tmp_path,
            lambda request: pytest.fail(str(request.url)),
            ui=UI(choices=["__all"], confirms=[True]),
        )
        draft = shell.current().draft
        draft.contexts.extend([{"source_label": "a"}, {"source_label": "b"}])
        draft.images.append({"alt": "c.png"})
        await attachments(shell)
        assert draft.contexts == [] and draft.images == []
        await shell.api.close()

    asyncio.run(run())
