"""Goals, approvals and subagents: terminal parity with the GUI's controls."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from nebula.terminal import workflows
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


RUNNING = {"id": "goal-a", "status": "running", "revision": 4, "objective": "Ship it"}


def goal_core(writes, *, conflict=False):
    state = {"goal": dict(RUNNING), "conflicted": False}

    def handler(request):
        route = path(request)
        if route == "chat/sessions/chat-a/goal" and request.method == "GET":
            return httpx.Response(200, json=state["goal"])
        if route == "chat/sessions/chat-a/goal/actions":
            body = json.loads(request.content)
            writes.append(body)
            if conflict and not state["conflicted"]:
                state["conflicted"] = True
                state["goal"]["revision"] += 1
                return httpx.Response(409, json={"detail": "stale revision"})
            assert body["expected_revision"] == state["goal"]["revision"]
            state["goal"] = {**state["goal"], "status": "completed"}
            return httpx.Response(200, json=state["goal"])
        pytest.fail(route)

    return handler


def test_goal_complete_sends_operator_evidence_core_requires(tmp_path):
    writes = []

    async def run():
        shell = shell_for(
            tmp_path,
            goal_core(writes),
            ui=UI(
                confirms=[True],
                answers=["Shipped to staging", "Smoke test passed", "Logs clean", ""],
            ),
        )
        await workflows.goal(shell, ["complete"])
        assert writes == [
            {
                "action": "complete",
                "completion_summary": "Shipped to staging",
                "completion_evidence": [
                    {"kind": "operator_confirmation", "detail": "Smoke test passed"},
                    {"kind": "operator_confirmation", "detail": "Logs clean"},
                ],
                "expected_revision": 4,
            }
        ]
        assert "Core goal status: completed." in shell.ui.output
        await shell.api.close()

    asyncio.run(run())


def test_goal_complete_without_evidence_changes_nothing(tmp_path):
    writes = []

    async def run():
        shell = shell_for(
            tmp_path, goal_core(writes), ui=UI(confirms=[True], answers=["Done", ""])
        )
        with pytest.raises(ClientError, match="at least one evidence item"):
            await workflows.goal(shell, ["complete"])
        assert writes == []
        await shell.api.close()

    asyncio.run(run())


def test_goal_action_retries_once_on_a_moved_revision(tmp_path):
    writes = []

    async def run():
        shell = shell_for(
            tmp_path,
            goal_core(writes, conflict=True),
            ui=UI(confirms=[True], answers=["Done", "Verified", ""]),
        )
        await workflows.goal(shell, ["complete"])
        assert [write["expected_revision"] for write in writes] == [4, 5]
        await shell.api.close()

    asyncio.run(run())


def test_child_goal_starts_with_the_gui_criterion_and_opens(tmp_path):
    seen = []

    def handler(request):
        route = path(request)
        seen.append((request.method, route))
        if route == "chat/sessions/chat-a/goal":
            return httpx.Response(200, json=RUNNING)
        if route == "chat/sessions/chat-a/goal/children":
            assert json.loads(request.content) == {
                "objective": "Audit the logs",
                "completion_criteria": [
                    "Child work completes without sharing parent approvals"
                ],
                "plan": [],
            }
            return httpx.Response(
                200, json={"objective": "Audit the logs", "session_id": "child-chat"}
            )
        if route == "chat-sessions/child-chat":
            return httpx.Response(200, json={**SESSION, "id": "child-chat"})
        pytest.fail(route)

    async def run():
        shell = shell_for(
            tmp_path, handler, ui=UI(answers=["Audit the logs"], confirms=[True])
        )
        opened = []

        async def open_view(project, session, **kwargs):
            opened.append(session["id"])

        shell.open_view = open_view
        await workflows.goal(shell, ["child"])
        assert opened == ["child-chat"]
        await shell.api.close()

    asyncio.run(run())


SUBAGENTS = {
    "session_id": "chat-a",
    "subagents": [
        {
            "id": "sub-1",
            "name": "Researcher",
            "task": "Find the config",
            "status": "waiting_approval",
            "child_session_id": "child-1",
            "elapsed_seconds": 75,
            "approval": {
                "id": "appr-sub",
                "tool": "files.read",
                "rationale": "Read the config file",
                "detail": "path: app.yaml",
            },
        },
        {"id": "sub-2", "name": "Writer", "status": "complete", "elapsed_seconds": 3},
    ],
}


def test_subagent_approvals_are_listed_and_decided_without_a_resume(tmp_path):
    decisions = []

    def handler(request):
        route = path(request)
        if route == "chat/sessions/chat-a/state":
            return httpx.Response(
                200, json={"session_id": "chat-a", "busy": True, "pending": []}
            )
        if route == "chat/sessions/chat-a/subagents":
            return httpx.Response(200, json=SUBAGENTS)
        if route == "approvals/appr-sub/decision":
            decisions.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "approved"})
        if route == "approvals/appr-sub":
            return httpx.Response(200, json={"status": "approved"})
        pytest.fail(route)

    async def run():
        shell = shell_for(tmp_path, handler, ui=UI(choices=["sub:appr-sub", "approve"]))
        await workflows.approvals(shell, [])
        assert decisions == [{"decision": "approve"}]
        assert shell.stream_task is None
        await shell.api.close()

    asyncio.run(run())


def main_approval_core(record, exact):
    def handler(request):
        route = path(request)
        if route == "chat/sessions/chat-a/state":
            return httpx.Response(
                200,
                json={
                    "session_id": "chat-a",
                    "busy": True,
                    "pending": [
                        {
                            "id": "appr-1",
                            "kind": "approval",
                            "text": "Run a command",
                            "turn_id": "turn-1",
                        }
                    ],
                },
            )
        if route == "chat/sessions/chat-a/subagents":
            return httpx.Response(200, json={"subagents": []})
        if route == "approvals/appr-1":
            return httpx.Response(
                200, json={"status": "pending", "chat_session_id": "chat-a", **exact}
            )
        if route == "chat/sessions/chat-a/pending-turn":
            return httpx.Response(200, json={"id": "turn-1", "session_id": "chat-a"})
        record.append((request.method, route, json.loads(request.content or b"null")))
        if route == "approvals/appr-1/decision":
            return httpx.Response(200, json={"status": "approved"})
        if route.startswith("ssh-environments/"):
            return httpx.Response(200, json={"alias": "lab-box"})
        if route == "chat/turns/turn-1/resume":
            return httpx.Response(
                200,
                content=b'event: done\ndata: {"type":"done","message":{"content":"ok"}}\n\n',
            )
        pytest.fail(route)

    return handler


def test_edit_pending_request_sends_edited_arguments(tmp_path):
    record = []
    exact = {"exact_request": {"tool_name": "files.write", "arguments": {"path": "a"}}}

    async def run():
        shell = shell_for(
            tmp_path,
            main_approval_core(record, exact),
            ui=UI(choices=["main:appr-1", "edit"], answers=['{"path": "b.txt"}']),
        )
        await workflows.approvals(shell, [])
        await shell.stream_task
        decision = next(
            body for _, route, body in record if route.endswith("/decision")
        )
        assert decision == {
            "decision": "approve",
            "edited_arguments": {"path": "b.txt"},
        }
        await shell.api.close()

    asyncio.run(run())


def test_ssh_always_allow_saves_the_host_then_approves(tmp_path):
    record = []
    exact = {
        "exact_request": {
            "tool_name": "ssh.lab-a1b2c3.run_command",
            "arguments": {"command": "uptime"},
        },
        "expected_effects": [
            "Run a shell command on the operator's machine Lab box (SSH host lab-box)."
        ],
    }

    async def run():
        shell = shell_for(
            tmp_path,
            main_approval_core(record, exact),
            ui=UI(choices=["main:appr-1", "always_host"]),
        )
        await workflows.approvals(shell, [])
        await shell.stream_task
        assert record[0] == (
            "PUT",
            "ssh-environments/lab-box",
            {"command_approval": "allow"},
        )
        assert record[1] == (
            "POST",
            "approvals/appr-1/decision",
            {"decision": "approve"},
        )
        await shell.api.close()

    asyncio.run(run())


def test_agents_opens_a_subagent_chat_and_stops_all(tmp_path):
    seen = []

    def handler(request):
        route = path(request)
        seen.append((request.method, route))
        if route == "chat-sessions" and request.method == "GET":
            return httpx.Response(200, json=[SESSION])
        if route == "chat/sessions/chat-a/subagents":
            return httpx.Response(200, json=SUBAGENTS)
        if route == "chat-sessions/child-1":
            return httpx.Response(
                200,
                json={**SESSION, "id": "child-1", "metadata": {"subagent_id": "sub-1"}},
            )
        if route == "chat/sessions/chat-a/subagents/stop":
            return httpx.Response(200, json={"stopped": 1})
        pytest.fail(route)

    async def run():
        shell = shell_for(
            tmp_path,
            handler,
            ui=UI(choices=["child:sub-1", "open", "__stop_all"], confirms=[True]),
        )
        opened = []

        async def open_view(project, session, **kwargs):
            opened.append(session["id"])

        shell.open_view = open_view
        await workflows.agents(shell, [])
        assert opened == ["child-1"]
        assert any("1m 15s" in line for line in shell.ui.output)
        await workflows.agents(shell, [])
        assert ("POST", "chat/sessions/chat-a/subagents/stop") in seen
        await shell.api.close()

    asyncio.run(run())


def test_subagent_chat_opens_instead_of_being_refused(tmp_path):
    child = {
        **SESSION,
        "id": "child-1",
        "title": "Researcher",
        "metadata": {"subagent_id": "sub-1", "subagent_parent_session_id": "chat-a"},
    }

    def handler(request):
        route = path(request)
        if route == "chat-sessions/child-1":
            return httpx.Response(200, json=child)
        if route == "chat-sessions/child-1/assistant-config":
            return httpx.Response(200, json={"settings": CONFIG, "revision": 2})
        if route == "chat/sessions/child-1/messages":
            return httpx.Response(200, json=[])
        if route == "chat/sessions/child-1/state":
            return httpx.Response(
                200, json={"session_id": "child-1", "busy": False, "pending": []}
            )
        pytest.fail(route)

    async def run():
        shell = shell_for(tmp_path, handler)
        await shell.open_view(PROJECT, child)
        assert shell.current().session_id == "child-1"
        await shell.api.close()

    asyncio.run(run())
