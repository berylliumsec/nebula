"""Harness activity actions and the tool-output artifact inspector."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from nebula.terminal import inspect
from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, ClientError, Settings
from tests.test_terminal_shell import CONFIG, CORE, ENTRY, PROJECT, SESSION, UI

HARNESS = {**CONFIG, "backend": "harness", "profile_id": "h"}


def path(request):
    return request.url.path.removeprefix("/api/v1/")


def shell_for(tmp_path, handler, *, ui=None, config=None, capabilities=None):
    settings = Settings(tmp_path / "chat.json", {"cores": {CORE: dict(ENTRY)}}, CORE)
    api = AsyncApi(settings, transport=httpx.MockTransport(handler))
    shell = Shell(settings, ui=ui or UI(), api=api)
    session = {**copy.deepcopy(SESSION), "harness_session_id": "hs-1"}
    shell.view = View(copy.deepcopy(PROJECT), session, copy.deepcopy(config or HARNESS))
    shell.profiles["h"] = {"id": "h", "capabilities": capabilities or {}}

    async def configuration():
        return shell.current().config

    async def runtimes():
        return []

    shell.configuration = configuration
    shell.runtimes = runtimes
    return shell


EVENTS = [
    {
        "type": "item_upsert",
        "item_kind": "subagent",
        "item_id": "task-1",
        "item_status": "running",
        "title": "Explore the repo",
        "harness_turn_id": "ht-1",
        "payload": {"task_type": "explore", "stoppable": True},
    },
    {
        "type": "item_upsert",
        "item_kind": "subagent",
        "item_id": "task-1",
        "item_status": "streaming",
        "harness_turn_id": "ht-1",
        "payload": {"stoppable": True},
    },
    {
        "type": "item_upsert",
        "item_kind": "subagent",
        "item_id": "task-2",
        "item_status": "completed",
        "payload": {"stoppable": False},
    },
    {
        "type": "checkpoint",
        "item_id": "cp-1",
        "title": "File checkpoint",
        "payload": {"checkpoint_id": "cp-1"},
    },
    {
        "type": "checkpoint",
        "item_id": "cp-1",
        "title": "Files rewound",
        "payload": {"checkpoint_id": "cp-1", "action": "rewind"},
    },
]


def test_harness_items_fold_tasks_and_skip_rewind_records():
    found = inspect.harness_items(EVENTS)
    tasks = {row["item_id"]: row for row in found["tasks"]}
    assert tasks["task-1"]["item_status"] == "streaming"
    assert tasks["task-1"]["title"] == "Explore the repo"
    assert [row["checkpoint_id"] for row in found["checkpoints"]] == ["cp-1"]
    assert found["checkpoints"][0]["title"] == "File checkpoint"


def harness_core(writes, *, busy=False):
    def handler(request):
        route = path(request)
        if route == "chat/sessions/chat-a/state":
            return httpx.Response(
                200,
                json={
                    "session_id": "chat-a",
                    "busy": busy,
                    "harness_turn_id": "ht-1",
                    "pending": [],
                },
            )
        if route == "harness-turns/ht-1/events":
            assert request.url.params["after"] == "0"
            return httpx.Response(200, json={"events": EVENTS, "next_sequence": 5})
        writes.append((request.method, route, json.loads(request.content or b"null")))
        return httpx.Response(200, json={})

    return handler


def test_stoppable_task_is_offered_even_while_streaming(tmp_path):
    writes = []

    async def run():
        shell = shell_for(
            tmp_path,
            harness_core(writes),
            ui=UI(choices=["task:task-1"]),
            capabilities={"subagent_control": True},
        )
        await inspect.harness_actions(shell)
        assert writes == [("POST", "harness-turns/ht-1/tasks/task-1/stop", None)]
        await shell.api.close()

    asyncio.run(run())


def test_checkpoint_rewind_follows_the_capability_and_idle_rule(tmp_path):
    writes = []

    async def run():
        # Without the advertised capability (Core reports it off today), nothing is offered.
        shell = shell_for(tmp_path, harness_core(writes), capabilities={})
        await inspect.harness_actions(shell)
        assert writes == []
        await shell.api.close()

        shell = shell_for(
            tmp_path,
            harness_core(writes),
            ui=UI(choices=["checkpoint:cp-1"], confirms=[True]),
            capabilities={"checkpoint_rewind": True},
        )
        await inspect.harness_actions(shell)
        assert writes == [
            (
                "POST",
                "harness-sessions/hs-1/checkpoints/rewind",
                {"checkpoint_id": "cp-1"},
            )
        ]
        await shell.api.close()

        busy = shell_for(
            tmp_path,
            harness_core([], busy=True),
            ui=UI(choices=["checkpoint:cp-1"]),
            capabilities={"checkpoint_rewind": True},
        )
        with pytest.raises(ClientError, match="while the session is idle"):
            await inspect.harness_actions(busy)
        await busy.api.close()

    asyncio.run(run())


def test_artifact_inspector_reads_searches_with_cursors_and_saves(
    tmp_path, monkeypatch
):
    seen = []
    reads = iter(
        [
            {
                "searchable": True,
                "starting_line": 1,
                "lines": [{"line": 1, "text": "PORT 22 open"}],
                "continuation": {"starting_line": 101},
            },
            {
                "searchable": True,
                "starting_line": 101,
                "lines": [{"line": 101, "text": "long line part"}],
                "continuation": {"continuation_cursor": "c-1"},
            },
            {
                "searchable": True,
                "lines": [{"line": 101, "text": "rest"}],
                "continuation": None,
            },
        ]
    )
    searches = iter(
        [
            {
                "matches": [
                    {
                        "filename": "scan.txt",
                        "line": 1,
                        "context": [{"line": 1, "text": "PORT 22 open"}],
                    }
                ],
                "continuation_cursor": "s-1",
            },
            {"matches": [], "continuation_cursor": None},
        ]
    )

    def handler(request):
        route = path(request)
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, route, body, dict(request.headers)))
        if route == "chat/sessions/chat-a/results":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "r1",
                            "kind": "tool",
                            "label": "nmap",
                            "tool_call_id": "tc-1",
                        }
                    ],
                    "next_offset": None,
                },
            )
        if route == "tool-calls/tc-1/artifacts":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "art-1",
                        "filename": "scan.txt",
                        "size": 12,
                        "metadata": {"searchable": True},
                    }
                ],
            )
        if route == "artifacts/art-1/output/read":
            return httpx.Response(200, json=next(reads))
        if route == "tool-calls/tc-1/output/search":
            return httpx.Response(200, json=next(searches))
        if route == "artifacts/art-1/content":
            return httpx.Response(200, content=b"PORT 22 open\n")
        pytest.fail(route)

    destination = tmp_path / "scan-copy.txt"

    async def run():
        ui = UI(
            choices=[
                "r1",
                "art-1",
                "read",
                "__search",
                "art-1",
                "save",
                "__done",
                "__done",
            ],
            answers=["1", "PORT", str(destination)],
            confirms=[True, True, True, True],
        )
        shell = shell_for(tmp_path, handler, ui=ui, config=CONFIG)
        await inspect.results(shell, [])
        bodies = [
            (route, body) for _, route, body, _ in seen if route.endswith("/read")
        ]
        assert [body for _, body in bodies] == [
            {"starting_line": 1, "line_count": 100},
            {"starting_line": 101, "line_count": 100},
            {"continuation_cursor": "c-1", "starting_line": 101, "line_count": 100},
        ]
        search = [body for _, route, body, _ in seen if route.endswith("/search")]
        assert search[0]["query"] == "PORT" and "cursor" not in search[0]
        assert search[1]["cursor"] == "s-1"
        content = next(
            headers for _, route, _, headers in seen if route.endswith("/content")
        )
        assert content["x-nebula-sensitive-data-acknowledged"] == "true"
        assert content["x-nebula-operation-id"]
        assert destination.read_bytes() == b"PORT 22 open\n"
        assert "     1  PORT 22 open" in shell.ui.output
        await shell.api.close()

    asyncio.run(run())
