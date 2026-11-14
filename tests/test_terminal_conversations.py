"""Chat lifecycle parity: thinking, turn details, edits, search, side chats, bulk."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from nebula.terminal import conversations, workflows
from nebula.terminal.shell import Shell
from nebula.terminal.state import Draft, View
from nebula.terminal.transport import AsyncApi, ClientError, Settings
from nebula.terminal.tui import TuiUI, turn_details, turn_elapsed
from tests.test_terminal_shell import CONFIG, CORE, ENTRY, PROJECT, SESSION, UI, frame


class ChatUI(UI):
    show_thinking = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.editing = ""
        self.revealed = []
        self.reasoned = []
        self.meta = []

    def set_editing(self, label):
        self.editing = label

    def reveal(self, text):
        self.revealed.append(text)
        return True

    def reasoning(self, value):
        self.reasoned.append(value)

    def turn_meta(self, detail, *, stopped=False):
        self.meta.append((detail, stopped))

    def set_show_thinking(self, value):
        self.show_thinking = value


def shell_for(tmp_path, handler, *, ui=None, saved=True):
    settings = Settings(tmp_path / "chat.json", {"cores": {CORE: dict(ENTRY)}}, CORE)
    api = AsyncApi(settings, transport=httpx.MockTransport(handler))
    shell = Shell(settings, ui=ui or ChatUI(), api=api)
    shell.view = View(
        copy.deepcopy(PROJECT),
        copy.deepcopy(SESSION) if saved else None,
        copy.deepcopy(CONFIG),
    )
    shell.profiles["provider-a"] = {"id": "provider-a", "is_local": True}
    return shell


def path(request):
    return request.url.path.removeprefix("/api/v1/")


IDLE = {
    "session_id": "chat-a",
    "busy": False,
    "pending": [],
    "actions": [],
    "detail": "Ready",
}


def test_turn_details_match_the_gui_elapsed_and_token_labels():
    assert turn_elapsed(8_240) == "8.2s"
    assert turn_elapsed(252_000) == "4m 12s"
    assert turn_elapsed(3_840_000) == "1h 04m"
    assert turn_elapsed(None) == turn_elapsed(0) == ""
    assert (
        turn_details(8_240, {"input_tokens": 1000, "output_tokens": 234})
        == "8.2s · 1,234 tokens"
    )
    assert (
        turn_details(None, {"total_tokens": 50, "reasoning_tokens": 20})
        == "50 tokens · 20 reasoning"
    )


def test_tui_collapses_thinking_and_labels_answers_with_turn_details():
    ui = TuiUI()
    ui.show_history(
        [
            {"role": "user", "content": "Explain the plan", "metadata": {}},
            {
                "role": "assistant",
                "content": "The plan is ready.",
                "metadata": {"reasoning_available": True},
                "reasoning": "First I read the notes, then I wrote the plan.",
                "elapsed_ms": 8_240,
                "usage": {"input_tokens": 40, "output_tokens": 12},
            },
        ]
    )
    text = ui.transcript.text
    assert "Thinking · 10 words · /thinking shows it" in text
    assert "First I read the notes" not in text
    assert "ASSISTANT · 8.2s · 52 tokens" in text
    ui.set_show_thinking(True)
    text = ui.transcript.text
    assert "THINKING" in text and "First I read the notes" in text
    assert text.index("First I read") < text.index("The plan is ready.")
    # Live thinking stays separate from the streamed answer.
    ui.submission("Next")
    ui.reasoning("Checking ")
    ui.reasoning("sources.")
    ui.delta("Done.")
    ui.turn_meta("2.0s · 9 tokens")
    ui._render()
    assert ui._entries[-2] == ("Thinking", "Checking sources.")
    assert ui._entries[-1] == ("Assistant · 2.0s · 9 tokens", "Done.")
    ui.turn_meta("1.0s", stopped=True)
    assert ui._entries[-1][0] == "Assistant · Stopped · 1.0s"


def test_stream_shows_thinking_and_final_turn_details(tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            content=frame("started", session_id="chat-a", turn_id="turn-a", sequence=1)
            + frame("reasoning_delta", delta="Thinking it over", sequence=2)
            + frame("delta", delta="Answer", sequence=3)
            + frame(
                "done",
                message={"content": "Answer"},
                elapsed_ms=1500,
                usage={"total_tokens": 30},
                sequence=4,
            ),
        )

    async def run():
        shell = shell_for(tmp_path, handler)
        await shell.stream(shell.current(), "POST", "chat/completions", {})
        assert shell.ui.reasoned == ["Thinking it over"]
        assert shell.ui.meta == [("1.5s · 30 tokens", False)]
        await shell.api.close()

    asyncio.run(run())


def test_thinking_toggle_reads_saved_reasoning_only_when_shown(tmp_path):
    seen = []

    def handler(request):
        seen.append(path(request))
        if path(request).endswith("/reasoning"):
            return httpx.Response(200, json={"reasoning": "Saved thoughts"})
        return httpx.Response(
            200,
            json=[
                {"id": "m1", "role": "user", "content": "Hi", "metadata": {}},
                {
                    "id": "m2",
                    "role": "assistant",
                    "content": "Hello",
                    "metadata": {"reasoning_available": True},
                    "reasoning": "",
                },
            ],
        )

    async def run():
        shell = shell_for(tmp_path, handler, ui=TuiUI())
        await shell.history()
        assert not any(item.endswith("/reasoning") for item in seen)
        assert "Thinking saved · /thinking shows it" in shell.ui.transcript.text
        await shell.toggle_thinking()
        assert "chat/sessions/chat-a/messages/m2/reasoning" in seen
        assert "Saved thoughts" in shell.ui.transcript.text
        await shell.history()
        assert seen.count("chat/sessions/chat-a/messages/m2/reasoning") == 1
        await shell.api.close()

    asyncio.run(run())


def test_edit_in_place_rewinds_only_when_the_edit_is_sent(tmp_path):
    seen = []
    message = {
        "id": "u2",
        "role": "user",
        "content": "Original question",
        "metadata": {},
        "content_blocks": [{"type": "image", "artifact_id": "img-a", "alt": "a.png"}],
    }

    def handler(request):
        seen.append((request.method, path(request)))
        route = path(request)
        if route == "chat/sessions/chat-a/state":
            return httpx.Response(200, json=IDLE)
        if route == "chat/sessions/chat-a/messages":
            return httpx.Response(200, json=[message])
        if route == "chat-sessions/chat-a" and request.method == "GET":
            return httpx.Response(200, json={**SESSION, "revision": 7})
        if route == "chat/sessions/chat-a/rewind":
            assert json.loads(request.content) == {
                "before_message_id": "u2",
                "expected_revision": 7,
            }
            return httpx.Response(
                200, json={"session": {**SESSION, "revision": 8}, "messages": []}
            )
        if route == "chat/sessions/chat-a/pending-turn":
            return httpx.Response(200, json=None)
        if route == "chat-sessions/chat-a/assistant-config":
            return httpx.Response(200, json={"settings": CONFIG, "revision": 8})
        if route == "chat/completions":
            body = json.loads(request.content)
            assert body["messages"] == [
                {
                    "role": "user",
                    "content": "Edited question",
                    "content_blocks": message["content_blocks"],
                }
            ]
            return httpx.Response(
                200,
                content=frame(
                    "started", session_id="chat-a", turn_id="turn-b", sequence=1
                )
                + frame("done", message={"content": "New answer"}, sequence=2),
            )
        pytest.fail(route)

    async def run():
        shell = shell_for(
            tmp_path, handler, ui=ChatUI(choices=["edit", "u2", "edit", "u2"])
        )
        await workflows.chat(shell, [])
        view = shell.current()
        assert view.editing["id"] == "u2" and shell.ui.editing == "u2"
        assert view.draft.text == "Original question"
        assert not any(route.endswith("/rewind") for _, route in seen)
        # Ctrl-C clears the draft and cancels the edit without touching Core.
        shell._clear_current_draft_text()
        assert view.editing is None and shell.ui.editing == ""
        await workflows.chat(shell, [])
        await shell.send("Edited question")
        await shell.stream_task
        assert ("POST", "chat/sessions/chat-a/rewind") in seen
        assert seen.index(("POST", "chat/sessions/chat-a/rewind")) < seen.index(
            ("POST", "chat/completions")
        )
        assert view.editing is None and view.session["revision"] == 8
        await shell.api.close()

    asyncio.run(run())


def test_conversation_picker_marks_pins_flags_and_archives_in_gui_order():
    rows = [
        {"id": "a", "title": "Archived", "metadata": {"archived_at": "2026-01-01"}},
        {"id": "b", "title": "Plain", "metadata": {}},
        {"id": "c", "title": "Pinned", "metadata": {"pinned": True}},
        {"id": "d", "title": "Flagged", "metadata": {"flag_color": "red"}},
    ]
    assert [row["id"] for row in conversations.chat_order(rows)] == [
        "c",
        "b",
        "d",
        "a",
    ]
    assert conversations.chat_markers(rows[0]) == "archived"
    assert conversations.chat_markers(rows[2]) == "pinned"
    assert conversations.chat_markers(rows[3]) == "red flag"


def test_message_search_scopes_bookmarks_and_opens_the_match(tmp_path):
    seen = []

    def handler(request):
        route = path(request)
        seen.append((route, dict(request.url.params)))
        if route == "chat/projects/project-a/search":
            params = request.url.params
            if params.get("session_id"):
                return httpx.Response(200, json={"items": [], "next_offset": None})
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "message_id": "m9",
                            "session_id": "chat-b",
                            "title": "Other chat",
                            "role": "assistant",
                            "excerpt": "the deploy checklist lives here",
                        }
                    ],
                    "next_offset": None,
                },
            )
        if route == "chat-sessions/chat-b":
            return httpx.Response(200, json={**SESSION, "id": "chat-b"})
        pytest.fail(route)

    async def run():
        shell = shell_for(
            tmp_path, handler, ui=ChatUI(choices=["__scope", "__bookmarks", "m9"])
        )
        opened = []

        async def open_view(project, session, **kwargs):
            opened.append(session["id"])

        shell.open_view = open_view
        await conversations.search(shell, ["deploy", "checklist"])
        searches = [params for route, params in seen if route.endswith("/search")]
        assert searches[0]["session_id"] == "chat-a" and searches[0]["q"] == (
            "deploy checklist"
        )
        assert "session_id" not in searches[1]
        assert searches[2]["bookmarked"] == "true"
        assert opened == ["chat-b"]
        assert shell.ui.revealed == ["the deploy checklist lives here"]
        await shell.api.close()

    asyncio.run(run())


def test_bookmark_toggle_uses_revision_and_requires_readback(tmp_path):
    saved = {}

    def handler(request):
        route = path(request)
        if route == "chat/sessions/chat-a/bookmarks":
            return httpx.Response(200, json=list(saved.values()))
        if route == "chat/sessions/chat-a/messages":
            return httpx.Response(
                200,
                json=[{"id": "m1", "role": "assistant", "content": "Keep this"}],
            )
        if route == "chat/sessions/chat-a/bookmarks/m1":
            body = json.loads(request.content)
            revision = saved.get("m1", {}).get("revision", 0)
            assert body["expected_revision"] == revision
            saved["m1"] = {
                "message_id": "m1",
                "active": body["active"],
                "revision": revision + 1,
            }
            return httpx.Response(200, json=saved["m1"])
        pytest.fail(route)

    async def run():
        shell = shell_for(tmp_path, handler, ui=ChatUI(choices=["m1", "m1"]))
        await conversations.bookmark(shell, [])
        assert saved["m1"]["active"] is True
        assert any("Bookmarked assistant message" in line for line in shell.ui.output)
        await conversations.bookmark(shell, [])
        assert saved["m1"] == {"message_id": "m1", "active": False, "revision": 2}
        await shell.api.close()

    asyncio.run(run())


def test_side_chat_opens_without_tools_and_close_returns_to_main(tmp_path):
    seen = []
    side = {
        **SESSION,
        "id": "side-a",
        "title": "Side chat #1 · Main A",
        "parent_session_id": "chat-a",
        "metadata": {"side_chat": True, "temporary_assistant": True},
    }
    listed = {"value": []}

    def handler(request):
        route = path(request)
        seen.append((request.method, route))
        if route == "chat/sessions/chat-a/side-chats":
            return httpx.Response(200, json=listed["value"])
        if route == "chat/sessions/chat-a/fork":
            assert json.loads(request.content) == {
                "title": "Side chat #1 · Main A",
                "side_chat": True,
                "through_latest": True,
            }
            listed["value"] = [side]
            return httpx.Response(201, json=side)
        if route == "chat/temporary-sessions/side-a":
            listed["value"] = []
            return httpx.Response(204)
        if route == "chat-sessions/side-a/assistant-config":
            return httpx.Response(
                200,
                json={
                    "settings": {
                        **CONFIG,
                        "mcp_server_ids": ["mcp-a"],
                        "tools_enabled": True,
                    },
                    "revision": 1,
                },
            )
        pytest.fail(route)

    async def run():
        shell = shell_for(tmp_path, handler, ui=ChatUI(confirms=[True]))
        opened = []

        async def open_view(project, session, *, parent=None):
            opened.append((session["id"], (parent or {}).get("id")))
            shell.view = View(project, session, copy.deepcopy(CONFIG), parent=parent)

        shell.open_view = open_view
        await conversations.side(shell, ["new"])
        assert opened == [("side-a", "chat-a")]
        shell.current().draft = Draft(
            contexts=[{"source_label": "x", "text": "y"}], skill={"name": "s"}
        )
        body = await shell.body("Quick aside", consent=False)
        assert body["session_id"] == "side-a"
        assert body["mcp_server_ids"] == [] and body["tools_enabled"] is False
        assert "context_attachments" not in body and "skill" not in body
        assert "side" in shell.available_commands()
        assert "goal" not in shell.available_commands()
        await conversations.side(shell, ["close"])
        assert ("DELETE", "chat/temporary-sessions/side-a") in seen
        assert opened[-1] == ("chat-a", None)
        await shell.api.close()

    asyncio.run(run())


def test_bulk_actions_patch_each_conversation_and_verify_in_core(tmp_path):
    chats = {
        "chat-a": {**SESSION, "metadata": {}},
        "chat-b": {**SESSION, "id": "chat-b", "title": "Main B", "revision": 4},
    }
    patches = []

    def handler(request):
        route = path(request)
        if route == "chat-sessions" and request.method == "GET":
            return httpx.Response(200, json=list(chats.values()))
        if request.method == "PATCH":
            identifier = route.split("/")[1]
            body = json.loads(request.content)
            patches.append((identifier, body))
            chats[identifier] = {
                **chats[identifier],
                "metadata": {"archived_at": "2026-11-14T00:00:00Z"},
            }
            return httpx.Response(200, json=chats[identifier])
        if request.method == "DELETE":
            pytest.fail("Deletion must require typed confirmation")
        pytest.fail(route)

    async def run():
        shell = shell_for(
            tmp_path,
            handler,
            ui=ChatUI(
                choices=[["__all"], "archive", ["chat-b"], "delete"],
                answers=["delete"],
            ),
        )
        await conversations.bulk(shell)
        assert sorted(patches) == [
            ("chat-a", {"expected_revision": 2, "archived": True}),
            ("chat-b", {"expected_revision": 4, "archived": True}),
        ]
        assert "Archive: 2 of 2 conversations confirmed in Core." in shell.ui.output
        await conversations.bulk(shell)
        assert "Deletion cancelled. Nothing changed." in shell.ui.output
        await shell.api.close()

    asyncio.run(run())


def test_finish_answer_resumes_only_a_failed_provider_turn(tmp_path):
    seen = []
    pending = {"value": {"id": "turn-f", "status": "running"}}

    def handler(request):
        route = path(request)
        seen.append((request.method, route))
        if route == "chat-sessions/chat-a":
            return httpx.Response(200, json=SESSION)
        if route == "chat/sessions/chat-a/pending-turn":
            return httpx.Response(200, json=pending["value"])
        if route == "chat/turns/turn-f/resume":
            return httpx.Response(
                200, content=frame("done", message={"content": "Final"}, sequence=1)
            )
        pytest.fail(route)

    async def run():
        shell = shell_for(tmp_path, handler, ui=ChatUI(confirms=[True]))
        with pytest.raises(ClientError, match="No unfinished provider answer"):
            await workflows.chat(shell, ["finish"])
        pending["value"] = {"id": "turn-f", "status": "failed"}
        await workflows.chat(shell, ["finish"])
        await shell.stream_task
        assert ("POST", "chat/turns/turn-f/resume") in seen
        assert "Response complete." in shell.ui.output
        await shell.api.close()

    asyncio.run(run())


def test_harness_retry_starts_a_linked_turn(tmp_path):
    seen = []

    def handler(request):
        route = path(request)
        seen.append((request.method, route))
        if route == "chat-sessions/chat-a":
            return httpx.Response(200, json={**SESSION, "backend": "harness"})
        if route == "chat/sessions/chat-a/messages":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "a1",
                        "role": "assistant",
                        "content": "Harness failed",
                        "metadata": {"harness_turn_id": "hturn-1"},
                    }
                ],
            )
        if route == "harness-turns/hturn-1/retry":
            return httpx.Response(200, json={"id": "hturn-2"})
        if route == "chat/sessions/chat-a/state":
            return httpx.Response(200, json=IDLE)
        pytest.fail(route)

    async def run():
        shell = shell_for(
            tmp_path, handler, ui=ChatUI(choices=["hturn-1"], confirms=[True])
        )
        await workflows.chat(shell, ["retry"])
        assert ("POST", "harness-turns/hturn-1/retry") in seen
        assert not any(route.endswith("/fork") for _, route in seen)
        await shell.api.close()

    asyncio.run(run())
