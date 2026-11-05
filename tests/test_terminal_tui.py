"""Full-screen presentation contracts independent of Core or a terminal device."""

from __future__ import annotations

import asyncio

import httpx

from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, Settings
from nebula.terminal.tui import TuiUI
from nebula.terminal.ui import TerminalUI


def test_tui_keeps_progress_visible_and_replaces_chat_history() -> None:
    async def run() -> None:
        ui = TuiUI()
        project = {"id": "project", "name": "Project Ω"}
        ui.open_view(project, {"id": "first", "title": "First"})
        ui.submission("Please review this")
        ui.activity("Core accepted turn")
        ui.activity("Tool: Search index")
        ui.delta("Partial answer")
        ui._render()
        assert "You · sending\n  Please review this" in ui.transcript.text
        assert "Assistant\n  Partial answer" in ui.transcript.text
        assert "Tool: Search index" in ui._activity_text()
        ui.say("\nAction required: review the call")
        assert "Action required" in ui._activity_text()
        ui.say("Decision recorded: rejected")
        assert "Action required" not in ui._activity_text()
        ui.mark_submission("accepted")
        ui._render()
        assert "You\n  Please review this" in ui.transcript.text
        ui.show_history(
            [
                {"role": "user", "content": "Saved question", "metadata": {}},
                {"role": "assistant", "content": "Saved answer", "metadata": {}},
            ]
        )
        assert ui.transcript.text.index("Saved question") < ui.transcript.text.index(
            "Saved answer"
        )
        ui.open_view(project, {"id": "second", "title": "Second"})
        assert "Saved answer" not in ui.transcript.text
        assert "Second" in ui._header_text()
        ui.set_navigation(
            "https://paired-core.example",
            [project],
            [{"id": "second", "title": "Renamed on Core"}],
        )
        assert "Renamed on Core" in ui._header_text()
        ui.open_core("https://other-core.example")
        assert "Saved question" not in ui.transcript.text
        assert "Choose a project" in ui._header_text()

    asyncio.run(run())


def test_tui_renders_saved_and_streamed_markdown_with_source_view() -> None:
    ui = TuiUI()
    ui.open_view(
        {"id": "project", "name": "Project"}, {"id": "chat", "title": "Butler"}
    )
    answer = (
        "**Paused** [parent](/projects/project/workbench?view=chat&session=chat)\n\n"
        "| Goal | State |\n|---|---|\n| HEIF | Running |"
    )
    ui.show_history([{"role": "assistant", "content": answer, "metadata": {}}])
    assert "Paused parent" in ui.transcript.text
    assert "HEIF" in ui.transcript.text
    assert "Running" in ui.transcript.text
    assert "**Paused**" not in ui.transcript.text
    assert "|---|" not in ui.transcript.text
    assert "/projects/project" not in ui.transcript.text

    ui.delta("\n\n## Live update\n\n**Done**")
    ui._render()
    assert "Live update" in ui.transcript.text
    assert "**Done**" not in ui.transcript.text
    ui.finish_turn()
    ui._source_view = True
    ui._render()
    assert "**Paused**" in ui.transcript.text
    assert "/projects/project/workbench?view=chat&session=chat" in ui.transcript.text
    assert "|---|---|" in ui.transcript.text
    assert "**Done**" in ui.transcript.text


def test_tui_sidebar_keeps_distinct_suffixes_and_selected_chat_visible() -> None:
    ui = TuiUI()
    project = {"id": "project", "name": "Apple Bug Bounty"}
    ui.open_view(project, {"id": "butler", "title": "Butler"})
    ui.set_navigation(
        "https://core.example:8765",
        [
            project,
            {"id": "other-1", "name": "Studio live browser check 179104001"},
            {"id": "other-2", "name": "Studio live browser check 179104002"},
        ],
        [
            {"id": "other-1", "title": "Complete the macOS 27.2 26B5091g 1"},
            {"id": "other-2", "title": "Complete the macOS 27.2 26B5091g 2"},
            {"id": "butler", "title": "Butler"},
        ],
    )
    sidebar = ui._sidebar_text()
    assert "› Apple Bug Bounty" in sidebar
    assert "› Butler" in sidebar
    assert "179104001" in sidebar and "179104002" in sidebar
    assert "26B5091g 1" in sidebar and "26B5091g 2" in sidebar


def test_tui_sidebar_distinguishes_running_waiting_idle_and_unknown() -> None:
    ui = TuiUI()
    project = {"id": "project", "name": "Project"}
    selected = {"id": "selected", "title": "Selected chat"}
    ui.open_view(project, selected)
    ui.set_navigation(
        "https://core.example:8765",
        [project],
        [
            selected,
            *({"id": f"idle-{i}", "title": f"Idle {i}"} for i in range(9)),
            {"id": "working", "title": "External work"},
            {"id": "waiting", "title": "Needs action"},
        ],
    )
    ui.set_conversation_activity(
        {
            "selected": "idle",
            "working": "working",
            "waiting": "waiting",
            **{f"idle-{i}": "idle" for i in range(9)},
        }
    )
    rows = ui._sidebar_text().splitlines()
    assert any("› Selected chat" in row and row.endswith("IDLE") for row in rows)
    assert any("Needs action" in row and row.endswith("WAIT") for row in rows)
    assert any("External work" in row and row.endswith("RUN") for row in rows)
    assert {len(row) for row in rows if row.endswith(("RUN", "WAIT", "IDLE"))} == {32}
    assert rows.index(next(row for row in rows if "Needs action" in row)) < rows.index(
        next(row for row in rows if "Idle 0" in row)
    )
    ui.set_conversation_activity(None)
    assert any(
        "› Selected chat" in row and row.endswith("?")
        for row in ui._sidebar_text().splitlines()
    )
    ui.open_view(
        {"id": "other-project", "name": "Other"}, {"id": "other", "title": "Other chat"}
    )
    ui.set_navigation(
        "https://core.example:8765", [], [{"id": "other", "title": "Other chat"}]
    )
    assert any(
        "› Other chat" in row and row.endswith("?")
        for row in ui._sidebar_text().splitlines()
    )


def test_tui_generated_markdown_title_stays_readable_in_navigation() -> None:
    ui = TuiUI()
    project = {"id": "project", "name": "Apple Bug Bounty"}
    chat = {"id": "chat", "title": "**Synthetic answer.** | Goal |"}
    ui.open_view(project, chat)
    ui.set_navigation("https://core.example:8765", [project], [chat])
    assert "Synthetic answer." in ui._header_text()
    assert "**" not in ui._header_text()
    assert "› Synthetic answer." in ui._sidebar_text()
    assert "**" not in ui._sidebar_text()


def test_navigation_rail_includes_the_selected_butler(tmp_path) -> None:
    core = "https://core.example:8765"
    project = {"id": "project", "name": "Apple Bug Bounty"}
    butler = {
        "id": "butler",
        "title": "Butler",
        "metadata": {"control_assistant": True},
    }
    main = {"id": "main", "title": "Main", "metadata": {}}

    def handler(request):
        if request.url.path.endswith("/engagements"):
            return httpx.Response(200, json=[{"id": "other", "name": "Other"}])
        if request.url.path.endswith("/chat-sessions"):
            return httpx.Response(200, json=[main, butler])
        if request.url.path.endswith("/chat/session-activity"):
            return httpx.Response(
                200,
                json=[
                    {"session_id": "butler", "state": "working"},
                    {"session_id": "main", "state": "idle"},
                ],
            )
        raise AssertionError(request.url)

    async def run() -> None:
        settings = Settings(
            tmp_path / "chat.json",
            {
                "cores": {
                    core: {
                        "device_id": "device",
                        "device_token": "synthetic-token",
                        "csrf_token": "synthetic-csrf",
                    }
                }
            },
            core,
        )
        api = AsyncApi(settings, transport=httpx.MockTransport(handler))
        ui = TuiUI()
        shell = Shell(settings, ui=ui, api=api)
        shell.view = View(project, butler)
        ui.open_view(project, butler)
        await shell.refresh_navigation()
        assert [row["id"] for row in ui._projects] == ["project", "other"]
        assert "› Apple Bug Bounty" in ui._sidebar_text()
        assert [row["id"] for row in ui._chats] == ["butler", "main"]
        assert "› Butler" in ui._sidebar_text()
        assert any(
            "› Butler" in row and row.endswith("RUN")
            for row in ui._sidebar_text().splitlines()
        )
        assert any(
            "Main" in row and row.endswith("IDLE")
            for row in ui._sidebar_text().splitlines()
        )
        await api.close()

    asyncio.run(run())


def test_navigation_rail_keeps_an_older_selected_chat(tmp_path) -> None:
    core = "https://core.example:8765"
    project = {"id": "project", "name": "Project"}
    selected = {"id": "older", "title": "Older active chat", "metadata": {}}

    def handler(request):
        if request.url.path.endswith("/engagements"):
            return httpx.Response(200, json=[project])
        if request.url.path.endswith("/chat-sessions"):
            return httpx.Response(
                200, json=[{"id": "recent", "title": "Recent chat", "metadata": {}}]
            )
        if request.url.path.endswith("/chat/session-activity"):
            return httpx.Response(
                200,
                json=[
                    {"session_id": "older", "state": "working"},
                    {"session_id": "recent", "state": "idle"},
                ],
            )
        raise AssertionError(request.url)

    async def run() -> None:
        settings = Settings(
            tmp_path / "chat.json",
            {
                "cores": {
                    core: {
                        "device_id": "device",
                        "device_token": "token",
                        "csrf_token": "csrf",
                    }
                }
            },
            core,
        )
        api = AsyncApi(settings, transport=httpx.MockTransport(handler))
        ui = TuiUI()
        shell = Shell(settings, ui=ui, api=api)
        shell.view = View(project, selected)
        ui.open_view(project, selected)
        await shell.refresh_navigation()
        assert [row["id"] for row in ui._chats] == ["older", "recent"]
        assert any(
            "› Older active chat" in row and row.endswith("RUN")
            for row in ui._sidebar_text().splitlines()
        )
        await api.close()

    asyncio.run(run())


def test_conversation_activity_poll_handles_304_failure_and_project_switch(
    tmp_path,
) -> None:
    core = "https://core.example:8765"
    first = {"id": "first-project", "name": "First"}
    second = {"id": "second-project", "name": "Second"}
    entered = asyncio.Event()
    release = asyncio.Event()
    phase = "working"
    headers: list[str | None] = []

    async def handler(request):
        nonlocal phase
        assert request.url.path.endswith("/chat/session-activity")
        headers.append(request.headers.get("if-none-match"))
        project_id = request.url.params["engagement_id"]
        if phase == "unchanged":
            return httpx.Response(304)
        if phase == "failed":
            return httpx.Response(503, json={"detail": "Offline"})
        if phase == "delayed":
            entered.set()
            await release.wait()
        return httpx.Response(
            200,
            headers={"ETag": '"activity-1"'},
            json=[
                {
                    "session_id": project_id + "-chat",
                    "state": phase if phase in {"working", "idle"} else "working",
                }
            ],
        )

    async def run() -> None:
        nonlocal phase
        settings = Settings(
            tmp_path / "chat.json",
            {
                "cores": {
                    core: {
                        "device_id": "device",
                        "device_token": "token",
                        "csrf_token": "csrf",
                    }
                }
            },
            core,
        )
        api = AsyncApi(settings, transport=httpx.MockTransport(handler))
        ui = TuiUI()
        shell = Shell(settings, ui=ui, api=api)
        shell.view = View(first, {"id": "first-project-chat", "title": "First chat"})
        ui.open_view(first, shell.view.session)

        await shell.refresh_conversation_activity()
        assert ui._chat_activity == {"first-project-chat": "working"}
        phase = "unchanged"
        await shell.refresh_conversation_activity()
        assert headers[-1] == '"activity-1"'
        assert ui._chat_activity == {"first-project-chat": "working"}

        phase = "failed"
        await shell.refresh_conversation_activity()
        assert ui._chat_activity is None
        phase = "idle"
        await shell.refresh_conversation_activity()
        assert headers[-1] is None
        assert ui._chat_activity == {"first-project-chat": "idle"}

        phase = "delayed"
        old_read = asyncio.create_task(shell.refresh_conversation_activity())
        await entered.wait()
        shell.view = View(second, {"id": "second-project-chat", "title": "Second chat"})
        ui.open_view(second, shell.view.session)
        release.set()
        await old_read
        assert ui._chat_activity is None
        phase = "working"
        await shell.refresh_conversation_activity()
        assert ui._chat_activity == {"second-project-chat": "working"}
        assert headers[-1] is None
        await api.close()

    asyncio.run(run())


def test_tui_searches_and_selects_a_menu_without_opaque_ids() -> None:
    async def run() -> None:
        ui = TuiUI()
        choice = asyncio.create_task(
            ui.choose(
                "Project",
                [{"id": "id-a", "name": "Alpha"}, {"id": "id-b", "name": "Beta"}],
            )
        )
        await asyncio.sleep(0)
        assert ui._modal_input is not None
        ui._modal_input.text = "Beta"
        assert [item["id"] for item in ui._matching_items()] == ["id-b"]
        ui._menu_select()
        assert (await choice)["id"] == "id-b"

    asyncio.run(run())


def test_plain_fallback_prints_progress_transitions_once(capsys) -> None:
    ui = TerminalUI(plain=True)
    ui.submission("hello")
    ui.activity("Tool: Search index")
    ui.activity("Tool: Search index")
    output = capsys.readouterr().out
    assert "Sending to Core" in output
    assert output.count("Working: Tool: Search index") == 1
