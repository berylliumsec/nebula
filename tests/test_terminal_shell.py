"""Focused, network-free contracts for the independently installable client."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from nebula.terminal.attachments import excerpt
from nebula.terminal.settings import select_skill
from nebula.terminal.shell import Shell
from nebula.terminal.state import Cursor, Draft, View
from nebula.terminal.transport import (
    ApiError,
    AsyncApi,
    ClientError,
    Settings,
    terminal_text,
)
from nebula.terminal import workflows

CORE = "https://core.example:8765"
PROJECT = {"id": "project-a", "name": "Project A"}
SESSION = {
    "id": "chat-a",
    "engagement_id": "project-a",
    "title": "Main A",
    "backend": "provider",
    "revision": 2,
    "metadata": {},
}
CONFIG = {
    "backend": "provider",
    "profile_id": "provider-a",
    "model": "fixture-model",
    "include_knowledge": False,
    "tools_enabled": False,
    "harness_cyber_access_program": "automatic",
    "mcp_server_ids": [],
    "hook_ids": [],
}
ENTRY = {
    "device_id": "device-a",
    "device_token": "synthetic-device",
    "csrf_token": "synthetic-csrf",
}


class UI:
    def __init__(self, answers=(), choices=(), confirms=()):
        self.answers = iter(answers)
        self.choices = iter(choices)
        self.confirms = iter(confirms)
        self.output = []
        self.prompts = []
        self.status = ""

    def say(self, value=""):
        self.output.append(str(value))

    def delta(self, value):
        self.output.append(str(value))

    def markdown(self, value):
        self.output.append(str(value))

    async def ask(self, label, **kwargs):
        self.prompts.append((label, kwargs))
        return next(self.answers)

    async def choose(self, title, items, *, selected=None, **kwargs):
        if selected:
            matches = [
                item
                for item in items
                if item["id"] == selected or item["name"] == selected
            ]
            assert len(matches) == 1, (title, items, selected)
            return matches[0]
        answer = next(self.choices)
        return next(item for item in items if item["id"] == answer)

    async def confirm(self, text):
        self.prompts.append((text, {}))
        return next(self.confirms)

    async def multiple(self, title, items, selected):
        return next(self.choices)


def make_shell(tmp_path, handler, *, ui=None, saved=True):
    settings = Settings(tmp_path / "chat.json", {"cores": {CORE: dict(ENTRY)}}, CORE)
    api = AsyncApi(settings, transport=httpx.MockTransport(handler))
    shell = Shell(settings, ui=ui or UI(), api=api)
    shell.view = View(
        copy.deepcopy(PROJECT),
        copy.deepcopy(SESSION) if saved else None,
        copy.deepcopy(CONFIG),
    )
    shell.profiles["provider-a"] = {
        "id": "provider-a",
        "is_local": True,
        "provider_type": "vllm",
    }
    return shell


def response(data, status=200):
    return httpx.Response(status, json=data)


def frame(event, **data):
    return f"event: {event}\ndata: {json.dumps({'type': event, **data})}\n\n".encode()


def test_provider_body_uses_saved_settings_and_excludes_harness_fields(tmp_path):
    def handler(request):
        assert request.url.path.endswith("assistant-config")
        return response(
            {"revision": 3, "settings": {**CONFIG, "model": "changed-on-desktop"}}
        )

    async def run():
        shell = make_shell(tmp_path, handler)
        shell.current().draft.skill = {
            "name": "review",
            "path": "/core/project/.agents/skills/review/SKILL.md",
        }
        body = await shell.body("hello")
        assert body["model"] == "changed-on-desktop"
        assert not any(name.startswith("harness_") for name in body)
        assert body["control_assistant"] is False
        assert body["skill"]["path"].startswith("/core/")
        assert body["messages"] == [{"role": "user", "content": "hello"}]
        await shell.api.close()

    asyncio.run(run())


def test_busy_text_queues_once_with_readback_and_does_not_submit(tmp_path):
    calls = []
    queue = {"revision": 0, "items": [], "paused": False}

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("assistant-config"):
            return response({"revision": 2, "settings": CONFIG})
        if request.url.path.endswith("pending-turn"):
            return response({"id": "turn-a", "status": "running"})
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["idempotency_key"]
            assert body["request"]["messages"][-1]["content"] == "next message"
            queue.update(revision=1, items=[{"id": "queued-a", "status": "queued"}])
        return response(queue)

    async def run():
        shell = make_shell(tmp_path, handler)
        await shell.send("next message")
        assert shell.current().draft == Draft()
        assert sum(method == "POST" for method, _ in calls) == 1
        assert all(not path.endswith("completions") for _, path in calls)
        await shell.api.close()

    asyncio.run(run())


def test_skill_selection_is_discovered_by_name_on_core(tmp_path):
    def handler(request):
        if request.url.path.endswith("assistant-config"):
            return response({"revision": 2, "settings": CONFIG})
        return response(
            [{"name": "review", "path": "/core/skill/SKILL.md", "source": "project"}]
        )

    async def run():
        shell = make_shell(tmp_path, handler)
        await select_skill(shell, "review")
        assert shell.current().draft.skill == {
            "name": "review",
            "path": "/core/skill/SKILL.md",
        }
        await shell.api.close()

    asyncio.run(run())


def test_stream_consumes_skill_once_and_preserves_distinct_turn_ids(tmp_path):
    content = b"".join(
        [
            frame(
                "started",
                session_id="chat-a",
                turn_id="chat-turn",
                harness_turn_id="vendor-turn",
                sequence=1,
            ),
            frame("message_delta", delta="Hello ", sequence=2),
            frame("message_delta", delta="world", sequence=3),
            frame("done", message={"content": "Hello world"}, sequence=4),
        ]
    )

    async def run():
        shell = make_shell(tmp_path, lambda _: httpx.Response(200, content=content))
        view = shell.current()
        view.draft.skill = {"name": "review", "path": "/core/skill"}
        await shell.stream(view, "POST", "chat/completions", {})
        assert view.cursor.turn_id == "chat-turn"
        assert view.cursor.harness_turn_id == "vendor-turn"
        assert view.draft.skill is None and view.pending_draft is None
        assert view.cursor.text == "Hello world"
        assert "Hello " in shell.ui.output and "world" in shell.ui.output
        await shell.api.close()

    asyncio.run(run())


def test_follow_started_replay_keeps_unsent_draft_and_skill(tmp_path):
    content = frame(
        "started", session_id="chat-a", turn_id="turn-a", sequence=1
    ) + frame("done", message={"content": "saved"}, sequence=2)

    async def run():
        shell = make_shell(tmp_path, lambda _: httpx.Response(200, content=content))
        draft = Draft(text="unsent", skill={"name": "review", "path": "/core/skill"})
        shell.current().draft = draft
        await shell.stream(shell.current(), "GET", "chat/turns/turn-a/events")
        assert shell.current().draft is draft
        await shell.api.close()

    asyncio.run(run())


def test_late_stream_events_cannot_enter_another_conversation(tmp_path):
    content = frame("message_delta", delta="old response", sequence=2)

    async def run():
        shell = make_shell(tmp_path, lambda _: httpx.Response(200, content=content))
        old_view = shell.current()
        shell.view = View({"id": "project-b", "name": "Other"})
        await shell.stream(old_view, "GET", "chat/turns/old/events")
        assert shell.ui.output == []
        assert shell.current().project_id == "project-b"
        await shell.api.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "sequence,epoch,expected", [(2, "a", False), (3, "a", True), (1, "b", True)]
)
def test_cursor_replay_and_epoch_reset(sequence, epoch, expected):
    cursor = Cursor(sequence=2, epoch="a", text="previous")
    assert cursor.accept({"sequence": sequence, "epoch": epoch}) is expected
    if epoch == "b":
        assert cursor.text == "" and cursor.sequence == 1


def test_uncertain_submission_is_not_retried(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        raise httpx.ReadError("lost acceptance", request=request)

    async def run():
        shell = make_shell(tmp_path, handler, saved=False)
        view = shell.current()
        view.draft.text = "retain this message"
        await shell.stream(view, "POST", "chat/completions", {})
        assert view.uncertain and view.draft.text == "retain this message"
        with pytest.raises(ClientError, match="uncertain"):
            await shell.send("retain this message")
        assert len(seen) == 1
        await shell.api.close()

    asyncio.run(run())


def test_stale_setting_is_not_overwritten_or_retried(tmp_path):
    writes = []
    revision = 7
    stored = {**CONFIG, "planning_enabled": False}

    class ConcurrentUI(UI):
        async def choose(self, *args, **kwargs):
            nonlocal revision
            result = await super().choose(*args, **kwargs)
            # A different device writes after this menu's snapshot was shown.
            revision = 8
            return result

    def handler(request):
        if request.method == "PATCH":
            body = json.loads(request.content)
            writes.append(body)
            if body["expected_revision"] != revision:
                return response({"detail": "revision conflict"}, 409)
            stored.update(body["settings"])
            return response({"revision": revision, "settings": stored})
        if request.url.path.endswith(("/providers", "/harnesses")):
            return response([])
        return response({"revision": revision, "settings": stored})

    async def run():
        shell = make_shell(
            tmp_path, handler, ui=ConcurrentUI(choices=["planning_enabled"])
        )
        with pytest.raises(ApiError, match="revision conflict"):
            await shell.command("/settings")
        assert writes == [
            {"expected_revision": 7, "settings": {"planning_enabled": True}}
        ]
        assert stored["planning_enabled"] is False
        await shell.api.close()

    asyncio.run(run())


@pytest.mark.parametrize("hash_field", ["sha256", "preview_sha256"])
def test_excerpt_preserves_exact_whitespace_and_hash(tmp_path, hash_field):
    async def run():
        shell = make_shell(tmp_path, lambda _: response({}), ui=UI(confirms=[True]))
        text = "  selected text\n\n"
        await excerpt(
            shell,
            {
                "text": text,
                "label": "notes.md",
                hash_field: hashlib.sha256(text.encode()).hexdigest(),
            },
            "workspace_file",
            "notes.md",
        )
        item = shell.current().draft.contexts[0]
        assert item["text"] == text
        assert item["sha256"] == hashlib.sha256(text.encode()).hexdigest()
        await shell.api.close()

    asyncio.run(run())


@pytest.mark.parametrize("hash_field", ["sha256", "preview_sha256"])
def test_excerpt_rejects_mismatched_hash_before_attaching(tmp_path, hash_field):
    async def run():
        shell = make_shell(tmp_path, lambda _: response({}))
        with pytest.raises(ClientError, match="hash"):
            await excerpt(
                shell, {"text": "text", hash_field: "0" * 64}, "knowledge", "source-a"
            )
        assert not shell.current().draft.contexts
        await shell.api.close()

    asyncio.run(run())


def test_origin_cookies_and_csrf_are_scoped_and_redirects_refused(tmp_path):
    async def run():
        def handler(request):
            assert str(request.url).startswith(CORE + "/api/v1/")
            assert request.headers["origin"] == CORE
            assert request.headers["x-nebula-csrf"] == ENTRY["csrf_token"]
            assert ENTRY["device_token"] in request.headers["cookie"]
            assert "authorization" not in request.headers
            return httpx.Response(307, headers={"location": "https://other.example"})

        shell = make_shell(tmp_path, handler)
        with pytest.raises(ClientError, match="redirected"):
            await shell.api.request("POST", "chat/completions", json={})
        with pytest.raises(ClientError, match="within"):
            await shell.api.request("GET", "https://other.example")
        await shell.api.close()

    asyncio.run(run())


def test_secret_question_answers_use_hidden_input_and_are_not_rendered(tmp_path):
    sent = []
    question = {
        "id": "question-a",
        "chat_session_id": SESSION["id"],
        "prompt": "Private answer",
        "kind": "user_input",
        "contains_secret": True,
        "status": "pending",
        "questions": [{"id": "answer-a", "question": "Answer"}],
    }

    def handler(request):
        if request.url.path.endswith("/state"):
            return response({"harness_turn_id": "vendor-turn"})
        if request.method == "POST":
            sent.append(json.loads(request.content))
            question["status"] = "answered"
            return response(question)
        return response([question])

    async def run():
        ui = UI(answers=["sensitive-answer"], choices=["question-a", "answer"])
        shell = make_shell(tmp_path, handler, ui=ui)
        await workflows.input_question(shell, [])
        assert sent[0]["response"] == {"answer-a": "sensitive-answer"}
        assert ui.prompts[0][1]["secret"] is True
        assert "sensitive-answer" not in "\n".join(ui.output)
        assert "sensitive-answer" not in json.dumps(shell.settings.data)
        await shell.api.close()

    asyncio.run(run())


def test_terminal_control_sequences_are_removed_from_model_output():
    assert (
        terminal_text("hello\x1b]52;c;clipboard\x07\x1b[31m world\x00") == "hello world"
    )


def test_submitting_text_is_removed_from_the_next_prompt_immediately(tmp_path):
    async def run():
        shell = make_shell(
            tmp_path,
            lambda request: (
                response({"revision": 2, "settings": CONFIG})
                if request.url.path.endswith("assistant-config")
                else response(None)
            ),
        )
        await shell.send("first message")
        assert shell.current().draft.text == ""
        assert shell.current().pending_draft.text == "first message"
        await shell.detach()
        await shell.api.close()

    asyncio.run(run())


def test_reconnect_state_failure_never_repeats_post(tmp_path, monkeypatch):
    seen = []

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame("started", session_id="chat-a", turn_id="turn-a", sequence=1)
            raise httpx.ReadError("stream disconnected")

    state_calls = 0

    def handler(request):
        nonlocal state_calls
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, stream=Broken())
        if request.url.path.endswith("/state"):
            state_calls += 1
            if state_calls == 1:
                raise httpx.ReadError("state disconnected", request=request)
            return response({"busy": True})
        return httpx.Response(
            200, content=frame("done", message={"content": "saved result"}, sequence=2)
        )

    async def no_wait(*args):
        return None

    monkeypatch.setattr("nebula.terminal.shell.asyncio.sleep", no_wait)

    async def run():
        shell = make_shell(tmp_path, handler)
        await shell.stream(shell.current(), "POST", "chat/completions", {})
        assert sum(method == "POST" for method, _ in seen) == 1
        assert any(
            method == "GET" and path.endswith("/events") for method, path in seen
        )
        await shell.api.close()

    asyncio.run(run())


def test_capacity_queued_acceptance_is_followed_without_a_new_submission(
    tmp_path, monkeypatch
):
    seen = []

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame("queued", turn_id="queued-turn", sequence=1)
            raise httpx.ReadError("lost while queued")

    def handler(request):
        seen.append((request.method, str(request.url)))
        if request.method == "POST":
            return httpx.Response(200, stream=Broken())
        return httpx.Response(
            200,
            content=frame(
                "started", session_id="chat-a", turn_id="queued-turn", sequence=2
            )
            + frame("done", message={"content": "finished"}, sequence=3),
        )

    async def no_wait(*args):
        return None

    monkeypatch.setattr("nebula.terminal.shell.asyncio.sleep", no_wait)

    async def run():
        shell = make_shell(tmp_path, handler, saved=False)
        await shell.stream(
            shell.current(), "POST", "chat/completions", {"backend": "provider"}
        )
        assert shell.current().cursor.turn_id == "queued-turn"
        assert shell.current().session_id == "chat-a"
        assert not shell.current().uncertain
        assert [method for method, _ in seen] == ["POST", "GET"]
        await shell.api.close()

    asyncio.run(run())


def test_approval_pause_does_not_reconnect_as_a_network_failure(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            content=frame("started", session_id="chat-a", turn_id="turn-a", sequence=1)
            + frame("approval_required", approval={"id": "approval-a"}, sequence=2),
        )

    async def run():
        shell = make_shell(tmp_path, handler)
        await shell.stream(shell.current(), "POST", "chat/completions", {})
        assert len(seen) == 1
        assert any("Action required" in line for line in shell.ui.output)
        assert not any("Connection lost" in line for line in shell.ui.output)
        await shell.api.close()

    asyncio.run(run())


def test_new_chat_cannot_mutate_the_cached_saved_chat(tmp_path):
    def handler(request):
        if request.url.path.endswith("assistant-config"):
            return response({"revision": 2, "settings": CONFIG})
        if request.url.path.endswith("/state"):
            return response({"busy": False, "detail": "Ready"})
        if request.url.path.endswith("/messages"):
            return response([])
        return response(SESSION)

    async def run():
        shell = make_shell(tmp_path, handler)
        saved = shell.current()
        shell.remember()
        await shell.open_view(PROJECT, None)
        assert shell.current() is not saved
        assert saved.session_id == SESSION["id"]
        await shell.open_view(PROJECT, SESSION)
        assert shell.current() is saved
        await shell.api.close()

    asyncio.run(run())


def test_auth_expiry_during_reconnect_does_not_crash_detach(tmp_path, monkeypatch):
    async def no_wait(*args):
        return None

    monkeypatch.setattr("nebula.terminal.shell.asyncio.sleep", no_wait)

    def handler(request):
        if request.url.path.endswith("/state"):
            return response({"detail": "expired"}, 401)
        return httpx.Response(200, content=b"")

    async def run():
        shell = make_shell(tmp_path, handler)
        shell.current().cursor.turn_id = "turn-a"
        shell.stream_task = asyncio.create_task(
            shell.stream(shell.current(), "GET", "chat/turns/turn-a/events")
        )
        await shell.stream_task
        await shell.detach()
        assert any("Pairing expired" in row for row in shell.ui.output)
        await shell.api.close()

    asyncio.run(run())


def test_queued_new_chat_waits_for_binding_before_accepting_another_send(tmp_path):
    seen = []
    queued = asyncio.Event()
    release = asyncio.Event()

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame("queued", turn_id="turn-a", sequence=1)
            queued.set()
            await release.wait()

    def handler(request):
        seen.append(request)
        return httpx.Response(200, stream=Waiting())

    async def run():
        shell = make_shell(tmp_path, handler, saved=False)
        view = shell.current()
        view.submitting = True
        shell.stream_task = asyncio.create_task(
            shell.stream(view, "POST", "chat/completions", {"backend": "provider"})
        )
        await queued.wait()
        assert view.submitting and view.session is None
        with pytest.raises(ClientError):
            await shell.send("second message")
        assert len(seen) == 1
        assert view.draft.text == "second message"
        await shell.detach()
        await shell.api.close()

    asyncio.run(run())


def test_ssh_environment_menu_uses_discovery_hosts(tmp_path):
    from nebula.terminal.settings import settings

    def handler(request):
        if request.url.path.endswith("assistant-config"):
            return response({"revision": 2, "settings": CONFIG})
        if request.url.path.endswith("ssh-environments"):
            return response(
                {
                    "hosts": [
                        {
                            "alias": "lab",
                            "environment": {
                                "id": "ssh-a",
                                "alias": "lab",
                                "display_name": "Lab",
                                "enabled": True,
                            },
                        }
                    ]
                }
            )
        return response([])

    async def run():
        shell = make_shell(
            tmp_path, handler, ui=UI(choices=["environments", ["ssh-a"]])
        )
        await settings(shell)
        assert shell.current().environments == ["ssh-a"]
        await shell.api.close()

    asyncio.run(run())


def test_provider_model_switch_applies_runtime_even_without_compaction(tmp_path):
    from nebula.terminal.settings import model

    writes = []
    current = dict(CONFIG)

    def handler(request):
        if request.url.path.endswith("assistant-config"):
            return response({"revision": 2, "settings": current})
        if request.url.path.endswith("preflight"):
            return response(
                {"compatible": True, "requires_compaction_confirmation": False}
            )
        if request.url.path.endswith("runtime-switch"):
            writes.append(json.loads(request.content))
            current["model"] = "other-model"
            return response(SESSION)
        if request.url.path.endswith("providers"):
            return response(
                [
                    {
                        "id": "provider-a",
                        "name": "Provider",
                        "enabled": True,
                        "model_allowlist": ["other-model"],
                    }
                ]
            )
        return response([])

    async def run():
        shell = make_shell(
            tmp_path, handler, ui=UI(choices=["provider:provider-a:other-model"])
        )
        await model(shell)
        assert writes[0]["model"] == "other-model"
        assert shell.current().config["model"] == "other-model"
        await shell.api.close()

    asyncio.run(run())


def test_numbered_menu_can_browse_entries_after_twenty(tmp_path):
    from nebula.terminal.ui import TerminalUI

    async def run():
        ui = TerminalUI(plain=True)
        answers = iter(["/next", "5"])
        shown = []

        async def ask(*args, **kwargs):
            return next(answers)

        ui.ask = ask
        ui.say = lambda value="": shown.append(str(value))
        item = await ui.choose(
            "Projects",
            [{"id": str(index), "name": f"Project {index}"} for index in range(25)],
        )
        assert item["id"] == "24"
        assert any("Project 20" in line for line in shown)

    asyncio.run(run())


def test_plain_prompt_cancel_is_not_a_value(monkeypatch):
    from nebula.terminal.ui import CancelledInput, TerminalUI

    monkeypatch.setattr("builtins.input", lambda _: "/cancel")

    async def run():
        with pytest.raises(CancelledInput):
            await TerminalUI(plain=True).ask("Project name")

    asyncio.run(run())


def test_cancelling_a_menu_leaves_the_attached_turn_running(tmp_path):
    from nebula.terminal.ui import CancelledInput

    requests = []

    async def run():
        shell = make_shell(
            tmp_path,
            lambda request: requests.append(request) or response({}),
            ui=UI(answers=["/projects", "/quit"]),
        )
        calls = 0

        async def connect(*args):
            return None

        async def select_project(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise CancelledInput()

        shell.connect = connect
        shell.select_project = select_project
        shell.stream_task = asyncio.create_task(asyncio.Event().wait())
        await shell.run()
        assert not requests
        assert calls == 2

    asyncio.run(run())


def test_initial_menu_cancel_closes_gracefully(tmp_path):
    from nebula.terminal.ui import CancelledInput

    async def run():
        shell = make_shell(tmp_path, lambda _: response({}))

        async def connect(*args):
            return None

        async def select_project(*args, **kwargs):
            raise CancelledInput()

        shell.connect = connect
        shell.select_project = select_project
        await shell.run()
        assert shell.api.client.is_closed
        assert any("Terminal closed" in line for line in shell.ui.output)

    asyncio.run(run())


def test_main_prompt_interrupt_stops_detached_selected_turn(tmp_path):
    from nebula.terminal.ui import InterruptedPrompt

    posts = []
    stopped = False

    class InterruptUI(UI):
        async def ask(self, *args, **kwargs):
            if not self.prompts:
                self.prompts.append(("main interrupt", kwargs))
                raise InterruptedPrompt()
            return "/quit"

    def handler(request):
        nonlocal stopped
        if request.method == "POST":
            posts.append(request.url.path)
            stopped = True
            return response({"status": "cancelled"})
        return response(
            {
                "busy": not stopped,
                "turn_id": "selected-turn",
                "actions": ["stop"] if not stopped else [],
                "detail": "Stopped" if stopped else "Working",
            }
        )

    async def run():
        shell = make_shell(tmp_path, handler, ui=InterruptUI())

        async def connect(*args):
            return None

        async def select_project(*args, **kwargs):
            return None

        shell.connect = connect
        shell.select_project = select_project
        assert shell.stream_task is None
        await shell.run()
        assert posts == ["/api/v1/chat/turns/selected-turn/cancel"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "remembered,project", [(True, None), (False, None), (True, "Project A")]
)
def test_explicit_startup_chat_overrides_remembered_selection(
    tmp_path, remembered, project
):
    requested = {**SESSION, "id": "requested-chat", "title": "Requested"}

    def handler(request):
        path = request.url.path
        if path == "/api/v1/engagements":
            return response([PROJECT])
        if path == "/api/v1/engagements/project-a":
            return response(PROJECT)
        if path == "/api/v1/chat-sessions":
            return response([SESSION, requested])
        if path == "/api/v1/chat-sessions/requested-chat":
            return response(requested)
        if path.endswith("/assistant-config"):
            return response({"revision": 2, "settings": CONFIG})
        if path.endswith("/messages"):
            return response([])
        if path.endswith("/state"):
            return response({"busy": False, "detail": "Ready"})
        pytest.fail(f"Unexpected startup request: {path}")

    async def run():
        shell = make_shell(
            tmp_path,
            handler,
            ui=UI(answers=["/quit"], choices=["project-a"]),
        )
        if remembered:
            shell.settings.entry["selection"] = {
                "project_id": "project-a",
                "session_id": "chat-a",
            }

        async def connect(*args):
            return None

        shell.connect = connect
        await shell.run(project=project, chat="Requested")
        assert shell.current().session_id == "requested-chat"
        assert shell.settings.entry["selection"]["session_id"] == "requested-chat"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["provider", "harness"])
def test_approval_resume_uses_exact_turn_backend_instead_of_cached_chat(
    tmp_path, backend
):
    streams = []
    decided = False
    pending_id = "selected-turn"

    def handler(request):
        nonlocal decided
        path = request.url.path
        if path.endswith("/state"):
            return response(
                {
                    "turn_id": "selected-turn",
                    "pending": [
                        {
                            "id": "approval-a",
                            "turn_id": "selected-turn",
                            "kind": "approval",
                            "text": "Review the requested action",
                        }
                    ],
                }
            )
        if path == "/api/v1/approvals/approval-a":
            return response(
                {
                    "id": "approval-a",
                    "chat_session_id": "chat-a",
                    "status": "approved" if decided else "pending",
                    "exact_request": {"action": "synthetic"},
                }
            )
        if path == "/api/v1/chat/sessions/chat-a/pending-turn":
            return response(
                {
                    "id": pending_id,
                    "session_id": "chat-a",
                    "harness_turn_id": "vendor-turn" if backend == "harness" else None,
                }
            )
        if path.endswith("/decision"):
            assert json.loads(request.content) == {"decision": "approve"}
            decided = True
            return response({})
        if path.startswith("/api/v1/chat/turns/"):
            streams.append((request.method, path))
            return httpx.Response(
                200,
                content=frame("started", turn_id="selected-turn", sequence=1)
                + frame("done", message={"content": "Saved answer"}, sequence=2),
            )
        pytest.fail(f"Unexpected approval request: {request.method} {path}")

    async def run():
        nonlocal decided, pending_id
        shell = make_shell(tmp_path, handler, ui=UI(choices=["approval-a", "approve"]))
        shell.current().session["backend"] = (
            "provider" if backend == "harness" else "harness"
        )
        draft = Draft(text="Next unsent turn")
        shell.current().draft = draft
        await workflows.approvals(shell, [])
        await shell.stream_task
        assert streams == [
            (
                "POST" if backend == "provider" else "GET",
                "/api/v1/chat/turns/selected-turn/"
                + ("resume" if backend == "provider" else "events"),
            )
        ]
        assert shell.current().draft is draft
        decided = False
        pending_id = "newer-turn"
        shell.ui = UI(choices=["approval-a"])
        with pytest.raises(ClientError, match="pending turn changed"):
            await workflows.approvals(shell, [])
        assert not decided
        await shell.api.close()

    asyncio.run(run())
