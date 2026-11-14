"""Interactive Assistant: all durable state and execution stay on Core."""

from __future__ import annotations

import asyncio
import shlex
from contextlib import suppress
from typing import Any
from uuid import uuid4

import httpx
from prompt_toolkit.patch_stdout import patch_stdout

from .conversations import chat_markers, chat_order, side_chat_body
from .state import Cursor, Draft, View, main_chat, visible_message
from .transport import (
    AcceptedRequest,
    ApiError,
    AsyncApi,
    ClientError,
    Settings,
    check_response,
    key,
    terminal_text,
)
from .tui import TuiUI, activity_label, turn_details
from .ui import COMMANDS, CancelledInput, InterruptedPrompt, TerminalUI


REASONING_READ_LIMIT = 20


def named(items: list[dict[str, Any]], field: str = "name") -> list[dict[str, Any]]:
    return [
        {**item, "name": str(item.get(field) or item.get("id") or "Unnamed")}
        for item in items
    ]


def conversation_states(rows: Any) -> dict[str, str]:
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("session_id"), str)
        or row.get("state") not in {"working", "waiting", "idle"}
        for row in rows
    ):
        raise ClientError("Core returned unsupported conversation activity.")
    return {row["session_id"]: row["state"] for row in rows}


class Shell:
    def __init__(
        self,
        settings: Settings,
        *,
        ui: TerminalUI | None = None,
        api: AsyncApi | None = None,
    ):
        self.settings = settings
        self.ui = ui or TerminalUI()
        if isinstance(self.ui, TuiUI):
            self.ui.on_draft_cleared = self._clear_current_draft_text
        self._api = api
        self.view: View | None = None
        self.views: dict[tuple[str | None, str, str], View] = {}
        self.stream_task: asyncio.Task[None] | None = None
        self.selected_state_task: asyncio.Task[None] | None = None
        self.profiles: dict[str, dict[str, Any]] = {}
        self.seen_attention: set[str] = set()
        self.running = True
        self._last_progress = ""
        self._known_busy = False
        self._state_identity: tuple[AsyncApi, str] | None = None
        self._state_etag: str | None = None
        self._state_unavailable = False
        self._state_lock = asyncio.Lock()
        self._pending_kinds: set[str] = set()

    def _clear_current_draft_text(self) -> None:
        if self.view is not None:
            self.view.draft.text = ""
            if self.view.editing is not None:
                self.view.editing = None
                if hasattr(self.ui, "set_editing"):
                    self.ui.set_editing("")

    @property
    def api(self) -> AsyncApi:
        if self._api is None:
            raise ClientError("Choose a paired Core first.")
        return self._api

    def current(self) -> View:
        if self.view is None:
            raise ClientError("Choose a project with /projects.")
        return self.view

    def available_commands(self) -> list[str]:
        """Order commands for the selected local view without querying Core on input."""
        view = self.view
        if view is None:
            return ["projects", "core", "help", "commands", "quit"]
        available = set(COMMANDS)
        busy = self._known_busy or view.submitting
        if view.pending_draft is None:
            available.discard("draft")
        if not (
            view.draft.contexts
            or view.draft.images
            or view.draft.skill
            or view.draft.skills
        ):
            available.discard("attachments")
        if view.session is None:
            available -= {
                *(() if view.receipt else ("follow",)),
                "chat",
                "bookmark",
                "side",
                "thinking",
                "history",
                "context",
                "activity",
                "results",
                "queue",
                "schedule",
                "approvals",
                "input",
                "detach",
                "steer",
                "stop",
                "now",
            }
        else:
            meta = view.session.get("metadata") or {}
            if meta.get("control_assistant"):
                available -= {"chat", "side"}
            if meta.get("side_chat"):
                available -= {"chat", "goal", "mcp", "attach", "schedule"}
            if view.config.get("backend") != "provider":
                available.discard("schedule")
            if "approval" not in self._pending_kinds:
                available.discard("approvals")
            if "input" not in self._pending_kinds:
                available.discard("input")
        if not busy and not (view.session is None and view.receipt):
            available -= {"follow", "detach", "steer", "stop", "now"}
        elif view.config.get("backend") != "harness":
            available.discard("steer")
        if self.stream_task is None or self.stream_task.done():
            available.discard("detach")
        priority = (
            ["approvals", "input", "status", "follow", "now", "stop", "steer", "queue"]
            if self._pending_kinds or busy
            else [
                "model",
                "settings",
                "skills",
                "attach",
                "goal",
                "chats",
                "snapshot",
                "board",
                "butler",
                "status",
            ]
            if view.session is None
            else [
                "new",
                "chats",
                "snapshot",
                "board",
                "butler",
                "history",
                "status",
                "search",
                "side",
                "model",
                "settings",
                "goal",
                "attach",
            ]
        )
        return [name for name in priority if name in available] + [
            name for name in COMMANDS if name in available and name not in priority
        ]

    async def connect(self, selected: str | None = None) -> None:
        cores = self.settings.data.get("cores", {})
        if selected is None and (self.settings.core is None or self._api is not None):
            if not cores:
                raise ClientError(
                    "No paired Core. Run nebula-chat --core https://HOST:PORT pair first."
                )
            selected = (
                await self.ui.choose(
                    "Paired Core", [{"id": item, "name": item} for item in cores]
                )
            )["id"]
        if selected is not None:
            if selected not in cores:
                raise ClientError(
                    "Pair this Core first with nebula-chat --core https://HOST:PORT pair."
                )
            candidate_settings = Settings(
                self.settings.path, self.settings.data, selected
            )
            candidate = AsyncApi(candidate_settings)
        else:
            candidate_settings = self.settings
            candidate = self._api or AsyncApi(candidate_settings)
        try:
            health = await candidate.request("GET", "health")
            protocol = health.get("terminal_client_protocol")
            supervisor = (
                "terminal_client_protocol" not in health
                and health.get("api_version") == "v1"
                and type(health.get("api_ready")) is bool
            )
            if supervisor and not health["api_ready"]:
                raise ClientError(
                    "Core is reachable, but its API is not ready. "
                    "Wait for recovery, then reconnect."
                )
            if protocol != 1 and not supervisor:
                raise ClientError(
                    "This Core does not support Assistant shell protocol 1. Upgrade Core or use the legacy 'nebula-chat chat' Butler command."
                )
        except BaseException:
            if candidate is not self._api:
                await candidate.close()
            raise
        await self.detach()
        await self.stop_selected_state()
        if self._api is not None and self._api is not candidate:
            await self._api.close()
        self.settings.core = candidate_settings.core
        self._api = candidate
        self.settings.save(dict(self.settings.entry))
        self.view = None
        self.profiles.clear()
        self.seen_attention.clear()
        self._last_progress = ""
        self._known_busy = False
        self._state_identity = None
        self._state_etag = None
        self._state_unavailable = False
        self._pending_kinds.clear()
        if isinstance(self.ui, TuiUI):
            self.ui.open_core(self.settings.core or "")
            self.ui.status = "Ready"
        self.ui.say(f"Connected to {self.settings.core}")
        if isinstance(self.ui, TuiUI):
            self.selected_state_task = asyncio.create_task(self.selected_state_loop())

    async def collection(
        self,
        title: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        selected: str | None = None,
        extras: list[dict[str, Any]] | None = None,
        field: str = "name",
        predicate: Any = None,
        label: Any = None,
        complete_catalog: bool = False,
        page_size: int = 100,
        order: Any = None,
    ) -> dict[str, Any]:
        offset = 0
        complete = False

        async def more() -> list[dict[str, Any]]:
            nonlocal offset, complete
            while True:
                batch = await self.api.page(
                    path, offset=offset, limit=page_size, **(params or {})
                )
                offset += len(batch)
                complete = len(batch) < page_size
                matches = named(
                    [item for item in batch if predicate is None or predicate(item)],
                    field,
                )
                if label is not None:
                    matches = [{**item, "label": label(item)} for item in matches]
                if matches or complete:
                    return matches

        if isinstance(self.ui, TuiUI):
            self.ui.begin_loading(f"Loading {title.lower()}s from Core…")
        items = await more()
        if complete_catalog and not selected:
            while not complete:
                items.extend(await more())
        if order is not None and complete:
            items = order(items)
        if selected:
            candidates = [
                item
                for item in items
                if item["id"] == selected
                or item["name"].casefold() == selected.casefold()
            ]
            while not complete:
                batch = await more()
                candidates.extend(
                    item
                    for item in batch
                    if item["id"] == selected
                    or item["name"].casefold() == selected.casefold()
                )
            return await self.ui.choose(title, candidates, selected=selected)
        return await self.ui.choose(
            title, [*(extras or []), *items], more=None if complete else more
        )

    def remember(self) -> None:
        if self.view is None:
            return
        view = self.view
        prefs = {
            "project_id": view.project_id,
            "session_id": view.session.get("id") if view.session else None,
        }
        self.settings.save({**self.settings.entry, "selection": prefs})
        if view.session:
            for identity, cached in list(self.views.items()):
                if cached is view and identity[2] != view.session_id:
                    del self.views[identity]
            self.views[(self.settings.core, view.project_id, view.session_id)] = view

    async def select_project(
        self, selected: str | None = None, *, chat: str | None = None
    ) -> None:
        item = await self.collection(
            "Project",
            "engagements",
            selected=selected,
            extras=[{"id": "__create", "name": "Create project"}],
            predicate=lambda row: not (row.get("metadata") or {}).get("archived_at"),
            complete_catalog=True,
            page_size=1000,
        )
        if item["id"] == "__create":
            name = (await self.ui.ask("Project name")).strip()
            if not name:
                raise ClientError("A project needs a name.")
            item = await self.api.request(
                "POST", "engagements", json={"name": name, "request_id": str(uuid4())}
            )
            item = await self.api.request("GET", f"engagements/{key(item['id'])}")
            self.ui.say(f"Created {item['name']}.")
        await self.select_chat(chat, project=item)

    async def select_chat(
        self, selected: str | None = None, *, project: dict[str, Any] | None = None
    ) -> None:
        project = project or self.current().project
        states: dict[str, str] | None = None
        if selected is None and isinstance(self.ui, TuiUI):
            try:
                states = conversation_states(
                    await self.api.request(
                        "GET",
                        "chat/session-activity",
                        params={"engagement_id": project["id"]},
                    )
                )
            except (ClientError, httpx.HTTPError, ValueError):
                pass
        item = await self.collection(
            "Conversation",
            "chat-sessions",
            params={"engagement_id": project["id"], "recent_first": "true"},
            selected=selected,
            field="title",
            predicate=main_chat,
            label=lambda row: (
                (
                    f"{activity_label((states or {}).get(row['id'])):>4}  "
                    if isinstance(self.ui, TuiUI)
                    else ""
                )
                + row["name"]
                + (f" · {chat_markers(row)}" if chat_markers(row) else "")
            ),
            order=chat_order,
            complete_catalog=True,
            page_size=500,
            extras=[
                {"id": "__new", "name": "New conversation"},
                {"id": "__butler", "name": "Butler"},
            ],
        )
        if item["id"] == "__butler":
            await self.open_butler(project)
        elif item["id"] == "__new":
            await self.open_view(project, None)
        else:
            await self.open_view(project, item)

    async def open_view(
        self,
        project: dict[str, Any],
        session: dict[str, Any] | None,
        *,
        parent: dict[str, Any] | None = None,
    ) -> None:
        if session is not None:
            session = await self.api.request(
                "GET", f"chat-sessions/{key(session['id'])}"
            )
            if session.get("engagement_id") != project["id"]:
                raise ClientError(
                    "This conversation belongs to another project. Choose it again."
                )
            meta = session.get("metadata") or {}
            if meta.get("side_chat") and parent is None:
                parent_id = session.get("parent_session_id")
                if parent_id:
                    parent = await self.api.request(
                        "GET", f"chat-sessions/{key(parent_id)}"
                    )
            if (
                not main_chat(session)
                and not meta.get("control_assistant")
                and not (meta.get("side_chat") and parent is not None)
            ):
                raise ClientError(
                    "Delegated conversations are inspected through /agents; message their parent main agent."
                )
        await self.detach()
        identity = (
            self.settings.core,
            project["id"],
            session["id"] if session else "__new",
        )
        view = (self.views.get(identity) if session is not None else None) or View(
            project=project, session=session
        )
        view.session = session
        if session is not None and (session.get("metadata") or {}).get("side_chat"):
            view.parent = parent
        self.views[identity] = view
        self.view = view
        self._last_progress = ""
        self._known_busy = False
        self._pending_kinds.clear()
        if isinstance(self.ui, TuiUI):
            self.ui.open_view(project, session)
        if session:
            await self.configuration()
            view.environments = (session.get("metadata") or {}).get(
                "ssh_environment_ids"
            )
            await self.history()
            state = await self.status()
            if state.get("busy"):
                self.ui.say("Use /follow to attach; new text queues a follow-up.")
        else:
            self.ui.say(f"{project['name']} — New conversation")
        self.remember()
        self.update_toolbar()
        if isinstance(self.ui, TuiUI):
            await self.refresh_navigation()

    async def refresh_navigation(self) -> None:
        """Keep the visible rail useful without making chat depend on its catalog."""
        if not isinstance(self.ui, TuiUI) or self.view is None:
            return
        view = self.view
        try:
            projects = await self.api.page("engagements", offset=0)
            chats = await self.api.page(
                "chat-sessions",
                offset=0,
                engagement_id=view.project_id,
                recent_first="true",
            )
            if self.view is not view:
                return
            visible_projects = projects
            if not any(row.get("id") == view.project_id for row in projects):
                visible_projects = [view.project, *projects]
            visible_chats = [row for row in chats if main_chat(row)]
            selected = view.session
            if selected is not None and not any(
                row.get("id") == selected["id"] for row in visible_chats
            ):
                visible_chats.insert(0, selected)
            self.ui.set_navigation(
                self.settings.core or "",
                visible_projects,
                visible_chats,
            )
        except (ClientError, httpx.HTTPError):
            self.ui.activity("Navigation list unavailable; F2/F3 retries")

    async def refresh_selected_state(self) -> None:
        """Refresh only the visible chat; F3 reads other chat states on demand."""
        async with self._state_lock:
            await self._read_selected_state()

    async def _read_selected_state(self) -> None:
        if (
            not isinstance(self.ui, TuiUI)
            or self.view is None
            or self.view.session is None
        ):
            return
        view = self.view
        api = self.api
        identity = (api, view.session_id)
        if self._state_identity != identity:
            self._state_identity = identity
            self._state_etag = None
            self._state_unavailable = False
            self.ui.set_conversation_activity(None)
        try:
            response = await api.client.get(
                f"chat/sessions/{key(view.session_id)}/state",
                headers={"If-None-Match": self._state_etag} if self._state_etag else {},
            )
            if self._api is not api or self.view is not view:
                return
            if response.status_code == 304:
                return
            check_response(response)
            state = response.json()
            if state.get("session_id") != view.session_id:
                raise ClientError("Core returned state for another conversation.")
            self._state_etag = response.headers.get("etag")
            self._state_unavailable = False
            await self.status(state=state, quiet=True)
        except (ClientError, httpx.HTTPError, ValueError):
            if self._api is not api or self.view is not view:
                return
            self._state_etag = None
            self.ui.set_conversation_activity(None)
            if not self._state_unavailable:
                self.ui.activity("Selected chat status unavailable; /status retries")
            self._state_unavailable = True

    async def runtimes(self) -> list[dict[str, Any]]:
        view = self.current()
        options: list[dict[str, Any]] = []
        for backend, path in (("provider", "providers"), ("harness", "harnesses")):
            offset = 0
            while True:
                profiles = await self.api.page(path, offset=offset)
                for profile in profiles:
                    owner = (profile.get("metadata") or {}).get(
                        "engagement_id"
                    ) or profile.get("engagement_id")
                    if not profile.get("enabled") or (
                        owner and owner != view.project_id
                    ):
                        continue
                    self.profiles[profile["id"]] = profile
                    models = (
                        profile.get("model_allowlist", [])
                        if backend == "provider"
                        else (profile.get("capabilities") or {}).get("models", [])
                    )
                    for model in models:
                        options.append(
                            {
                                "id": f"{backend}:{profile['id']}:{model}",
                                "name": f"{profile['name']} / {model}",
                                "backend": backend,
                                "profile_id": profile["id"],
                                "model": model,
                                "profile": profile,
                            }
                        )
                offset += len(profiles)
                if len(profiles) < 100:
                    break
        return options

    async def configuration(self) -> dict[str, Any]:
        view = self.current()
        if view.session:
            result = await self.api.request(
                "GET", f"chat-sessions/{key(view.session_id)}/assistant-config"
            )
            view.config = result["settings"]
            view.session["revision"] = result["revision"]
        elif not view.config:
            try:
                defaults = await self.api.request(
                    "GET", f"engagements/{key(view.project_id)}/assistant-defaults"
                )
            except ApiError:
                defaults = view.project.get("assistant_defaults") or {}
            view.project["assistant_defaults"] = defaults
            options = await self.runtimes()
            if not options:
                raise ClientError(
                    "No enabled model is configured on Core. Configure a provider or harness on the Core host, then use /model."
                )
            preferred = (
                defaults.get("harness_id")
                if defaults.get("backend") == "harness"
                else defaults.get("provider_id")
            )
            matched = next(
                (
                    row
                    for row in options
                    if row["profile_id"] == preferred
                    and (not defaults.get("model") or row["model"] == defaults["model"])
                ),
                None,
            )
            chosen = matched or (
                options[0]
                if len(options) == 1
                else await self.ui.choose("Model runtime", options)
            )
            view.config = {
                "backend": chosen["backend"],
                "profile_id": chosen["profile_id"],
                "model": chosen["model"],
                "tools_enabled": False,
                "planning_enabled": False,
                "include_knowledge": True,
                "allow_cloud_knowledge": False,
                "mcp_server_ids": [],
                "hook_ids": [],
                "allow_subagents": False,
                "allow_agent_messaging": False,
            }
            if matched is not None:
                # Choices remembered for this project apply to new chats, as in
                # the GUI; Core still validates them on the first turn.
                harness = chosen["backend"] == "harness"
                for name in (
                    "mcp_server_ids",
                    "hook_ids",
                    "allow_subagents",
                    "subagent_provider_id",
                    "subagent_harness_profile_id",
                    "subagent_model",
                    "max_active_subagents",
                    "subagent_reasoning_effort",
                    *(
                        (
                            "harness_reasoning_effort",
                            "harness_service_tier",
                            "harness_mode",
                        )
                        if harness
                        else ("reasoning_effort", "mcp_context_mode")
                    ),
                ):
                    value = defaults.get(name)
                    if value not in (None, [], ""):
                        view.config[name] = value
        return view.config

    async def open_butler(self, project: dict[str, Any] | None = None) -> None:
        project = project or self.current().project
        session = await self.api.request(
            "GET",
            "chat/control-assistant/session",
            params={"engagement_id": project["id"]},
        )
        if session is None:
            # Creating Butler uses its own reserved Core identity, never a main chat.
            previous = self.view
            self.view = View(project)
            try:
                config = await self.configuration()
                session = await self.api.request(
                    "POST",
                    "chat/control-assistant/session",
                    json={
                        "engagement_id": project["id"],
                        "backend": config["backend"],
                        "model": config["model"],
                        "provider_id"
                        if config["backend"] == "provider"
                        else "harness_profile_id": config["profile_id"],
                    },
                )
            finally:
                self.view = previous
        await self.open_view(project, session)

    async def history(self, more: bool = False) -> list[dict[str, Any]]:
        view = self.current()
        if view.session is None:
            self.ui.say("No saved messages yet.")
            return []
        params: dict[str, Any] = {"include_replaced": "true", "limit": 50}
        if more and view.history_before:
            params["before_message_id"] = view.history_before
        rows = await self.api.request(
            "GET", f"chat/sessions/{key(view.session_id)}/messages", params=params
        )
        if rows:
            view.history_before = rows[0]["id"]
        if getattr(self.ui, "show_thinking", False):
            await self.load_reasoning(view, rows)
        if isinstance(self.ui, TuiUI):
            self.ui.show_history(rows, older=more)
        else:
            for item in rows:
                if visible_message(item):
                    self.ui.say("You:" if item["role"] == "user" else "Assistant:")
                    self.ui.markdown(item.get("content", ""))
                    for block in item.get("content_blocks") or []:
                        if block.get("type") == "image":
                            self.ui.say(
                                f"[Image: {block.get('alt') or block.get('artifact_id') or 'attachment'}]"
                            )
        if len(rows) == 50:
            self.ui.say("/history more reads earlier messages.")
        return rows

    async def load_reasoning(self, view: View, rows: list[dict[str, Any]]) -> None:
        """Read saved thinking on demand for the latest answers only."""
        wanted = [
            row
            for row in rows
            if row.get("role") == "assistant"
            and not row.get("reasoning")
            and (row.get("metadata") or {}).get("reasoning_available")
        ][-REASONING_READ_LIMIT:]
        for row in wanted:
            cached = view.reasoning.get(row["id"])
            if cached is None:
                value = await self.api.request(
                    "GET",
                    f"chat/sessions/{key(view.session_id)}/messages/"
                    f"{key(row['id'])}/reasoning",
                )
                cached = terminal_text((value or {}).get("reasoning") or "")
                view.reasoning[row["id"]] = cached
            row["reasoning"] = cached
        if not isinstance(self.ui, TuiUI):
            for row in wanted:
                if row.get("reasoning"):
                    self.ui.say("Thinking:")
                    self.ui.say(row["reasoning"])

    async def toggle_thinking(self) -> None:
        shown = not getattr(self.ui, "show_thinking", False)
        self.ui.set_show_thinking(shown)
        view = self.current()
        if shown and view.session is not None:
            await self.history()
        self.ui.say(
            "Thinking is shown in the transcript."
            if shown
            else "Thinking is collapsed; /thinking shows it again."
        )

    async def state(self, view: View | None = None) -> dict[str, Any]:
        view = view or self.current()
        if view.session is None and view.receipt:
            saved = await self.api.request(
                "GET", f"runtime/requests/{key(view.receipt)}"
            )
            if saved.get("session_id"):
                view.session = await self.api.request(
                    "GET", f"chat-sessions/{key(saved['session_id'])}"
                )
                view.receipt = None
                view.submitting = False
                if self.view is view:
                    if isinstance(self.ui, TuiUI):
                        self.ui.set_identity(view.project, view.session)
                    self.remember()
            else:
                return {
                    "busy": True,
                    "pending": [],
                    "actions": [],
                    "turn_id": saved.get("turn_id"),
                    "detail": "Saved in Core as a new conversation; "
                    f"{terminal_text(saved.get('state') or 'queued')}, "
                    "waiting for its execution owner.",
                }
        if view.session is None:
            return {
                "busy": view.submitting,
                "pending": [],
                "actions": [],
                "detail": "New conversation.",
            }
        return await self.api.request(
            "GET", f"chat/sessions/{key(view.session_id)}/state"
        )

    async def status(
        self, *, state: dict[str, Any] | None = None, quiet: bool = False
    ) -> dict[str, Any]:
        state = await self.state() if state is None else state
        pending = state.get("pending") or []
        self._known_busy = bool(state.get("busy"))
        self._pending_kinds = {row.get("kind") for row in pending if row.get("kind")}
        if not pending and hasattr(self.ui, "clear_notice"):
            self.ui.clear_notice()
        if isinstance(self.ui, TuiUI):
            view = self.view
            if view is not None and view.session is not None:
                activity = (
                    "waiting"
                    if pending
                    or state.get("execution")
                    in {"needs_stop", "status_unavailable", "waiting_approval"}
                    else "working"
                    if self._known_busy
                    else "idle"
                )
                self.ui.set_conversation_activity({view.session_id: activity})
            if pending:
                self.ui.set_turn_phase("waiting")
            elif self._known_busy:
                if self.stream_task is None or self.stream_task.done():
                    self.ui.set_turn_phase("")
                else:
                    self.ui.set_turn_phase("working")
            else:
                self.ui.stream_stopped()
        self.update_toolbar(
            "Waiting for action"
            if pending
            else str(state.get("detail") or "Working")
            if self._known_busy
            else "Ready"
        )
        if not quiet:
            self.ui.say(state.get("detail") or state.get("execution", "Ready"))
        for item in pending:
            action_id = item.get("id")
            if not quiet or action_id not in self.seen_attention:
                self.ui.say(
                    f"Action required: {item['text']}. Use /approvals or /input."
                )
            if isinstance(action_id, str):
                self.seen_attention.add(action_id)
        if len(self.seen_attention) > 200:
            self.seen_attention = {
                item["id"] for item in pending if isinstance(item.get("id"), str)
            }
        return state

    def update_toolbar(self, detail: str = "") -> None:
        if hasattr(self.ui, "set_commands"):
            self.ui.set_commands(self.available_commands())
        if self.view:
            view = self.view
            if isinstance(self.ui, TuiUI):
                self.ui.set_model(view.config.get("model"))
                self.ui.set_selection_summary(
                    skill=", ".join(
                        "$" + name
                        for name in (
                            [item["name"] for item in view.draft.skills]
                            + ([view.draft.skill["name"]] if view.draft.skill else [])
                        )
                    ),
                    mcp_count=len(view.config.get("mcp_server_ids") or []),
                )
            if detail:
                self._last_progress = terminal_text(detail)[:160]
                if hasattr(self.ui, "activity"):
                    self.ui.activity(self._last_progress)
            elif self._known_busy or (
                self.stream_task is not None and not self.stream_task.done()
            ):
                detail = self._last_progress or "Working"
            self.ui.status = " · ".join(
                filter(
                    None,
                    [
                        view.project.get("name"),
                        (view.session or {}).get("title", "New conversation"),
                        view.config.get("model"),
                        detail,
                    ],
                )
            )

    async def body(self, text: str, *, consent: bool = True) -> dict[str, Any]:
        view = self.current()
        config = await self.configuration()
        backend = config["backend"]
        fields = {
            name: value
            for name, value in config.items()
            if name not in {"backend", "profile_id"} and value is not None
        }
        if backend == "provider":
            fields = {
                name: value
                for name, value in fields.items()
                if not name.startswith("harness_")
            }
        block: dict[str, Any] = {"role": "user", "content": text}
        if view.draft.images:
            block["content_blocks"] = view.draft.images
        result = {
            **fields,
            "backend": backend,
            "provider_id" if backend == "provider" else "harness_profile_id": config[
                "profile_id"
            ],
            "engagement_id": view.project_id,
            "messages": [block],
            "stream": True,
            "control_assistant": bool(
                view.session
                and (view.session.get("metadata") or {}).get("control_assistant")
            ),
        }
        if view.session:
            result["session_id"] = view.session_id
        if view.environments is not None:
            result["ssh_environment_ids"] = view.environments
        if view.draft.skill:
            result["skill" if backend == "provider" else "harness_skill"] = (
                view.draft.skill
            )
        if view.draft.skills:
            result["skill_selections"] = view.draft.skills
        if view.draft.contexts:
            result["context_attachments"] = view.draft.contexts
        if view.session and (view.session.get("metadata") or {}).get("side_chat"):
            side_chat_body(result)
        if consent:
            if not self.profiles:
                await self.runtimes()
            profile = self.profiles.get(config["profile_id"], {})
            local = (
                profile.get("is_local") is True
                or (profile.get("privacy") or {}).get("local_only") is True
            )
            tools = any(
                result.get(name)
                for name in (
                    "tools_enabled",
                    "mcp_server_ids",
                    "allow_subagents",
                    "allow_agent_messaging",
                    "context_attachments",
                )
            )
            if not local and tools:
                privacy = profile.get("privacy") or {}
                standing_consent = (
                    privacy.get("auto_share_tool_results") is True
                    and privacy.get("permits_sensitive_data") is True
                )
                if not standing_consent:
                    await self.tool_result_consent(config, profile)
                result["allow_cloud_tool_results"] = True
            if (
                not local
                and result.get("include_knowledge")
                and not result.get("allow_cloud_knowledge")
            ):
                if not await self.ui.confirm(
                    f"Allow selected project knowledge to be sent to {config['model']} for this turn?"
                ):
                    result["include_knowledge"] = False
                else:
                    result["allow_cloud_knowledge"] = True
        return result

    async def tool_result_consent(
        self, config: dict[str, Any], profile: dict[str, Any]
    ) -> None:
        """Ask once per turn, or save standing consent on the runtime as the GUI does."""
        privacy = profile.get("privacy") or {}
        question = (
            f"Allow selected tool/context results to be sent to {config['model']} "
            "for this turn?"
        )
        if privacy.get("permits_sensitive_data") is not True or not profile.get("id"):
            if not await self.ui.confirm(question):
                raise CancelledInput()
            return
        choice = (
            await self.ui.choose(
                question,
                [
                    {"id": "once", "name": "Allow this turn"},
                    {
                        "id": "always",
                        "name": f"Always allow for {profile.get('name') or config['model']}",
                    },
                    {"id": "cancel", "name": "Don't send"},
                ],
            )
        )["id"]
        if choice == "cancel":
            raise CancelledInput()
        if choice == "always":
            kind = "providers" if config["backend"] == "provider" else "harnesses"
            current = await self.api.request("GET", f"{kind}/{key(profile['id'])}")
            saved_privacy = {**(current.get("privacy") or {})}
            saved_privacy["auto_share_tool_results"] = True
            saved = await self.api.request(
                "PATCH",
                f"{kind}/{key(profile['id'])}",
                json={
                    "changes": {"privacy": saved_privacy},
                    "expected_revision": current["revision"],
                },
            )
            if ((saved or {}).get("privacy") or {}).get("auto_share_tool_results"):
                self.profiles[profile["id"]] = {**profile, **saved}
                self.ui.say(
                    "Saved: tool results are shared with this runtime on later turns "
                    "until you turn it off in Core Settings."
                )

    async def send(self, text: str) -> None:
        from .settings import select_skill

        view = self.current()
        if view.session is None and view.receipt and not view.submitting:
            view.draft.text = text
            raise ClientError(
                "Core saved your previous message as a new conversation and has "
                "not started it yet. /follow attaches when it starts; /new starts "
                "a separate conversation. Nothing was resent."
            )
        if view.submitting or view.uncertain:
            if view.submitting:
                view.draft.text = text
            raise ClientError(
                "The previous submission is uncertain. Review /status and /history; use /chats to select its saved conversation. Nothing was resent."
            )
        view.draft.text = text
        if view.editing is not None:
            await self.resend_edited(view, text)
            return
        if text.startswith("$"):
            token = text.split(maxsplit=1)[0][1:]
            await select_skill(self, token)
            if len(text.split(maxsplit=1)) == 1:
                view.draft.text = ""
                return
        body = await self.body(text)
        active = (
            await self.api.request(
                "GET", f"chat/sessions/{key(view.session_id)}/pending-turn"
            )
            if view.session
            else None
        )
        if active:
            self._known_busy = True
            if view.draft.images:
                raise ClientError(
                    "Images cannot be queued during a response. Keep the attachment and send after it finishes."
                )
            queue = await self.api.request(
                "GET", f"chat/sessions/{key(view.session_id)}/queue"
            )
            result = await self.api.request(
                "POST",
                f"chat/sessions/{key(view.session_id)}/queue",
                json={
                    "expected_revision": queue["revision"],
                    "action": "enqueue",
                    "idempotency_key": str(uuid4()),
                    "request": body,
                },
            )
            observed = await self.api.request(
                "GET", f"chat/sessions/{key(view.session_id)}/queue"
            )
            if not any(
                row["id"] in {item["id"] for item in observed["items"]}
                for row in result["items"]
            ):
                raise ClientError(
                    "Queue save could not be confirmed. Read /queue before retrying."
                )
            view.draft = Draft()
            if hasattr(self.ui, "submission"):
                self.ui.submission(text, queued=True)
            self.ui.say("Saved in Core's follow-up queue.")
            return
        await self.detach()
        view.cursor = Cursor()
        view.submitting = True
        self._known_busy = True
        view.pending_draft, view.draft = view.draft, Draft()
        if hasattr(self.ui, "submission"):
            self.ui.submission(text)
        self.stream_task = asyncio.create_task(
            self.stream(view, "POST", "chat/completions", body)
        )
        self.update_toolbar("Submitting")

    async def resend_edited(self, view: View, text: str) -> None:
        """Rewind to the edited saved message, then send the new text there."""
        editing = view.editing
        assert editing is not None and view.session is not None
        if not text.strip():
            raise ClientError("Write the edited message, or press Ctrl-C to cancel.")
        state = await self.state(view)
        if state.get("busy"):
            raise ClientError(
                "Stop or finish the running turn before resending an edited message."
            )
        session = await self.api.request("GET", f"chat-sessions/{key(view.session_id)}")
        result = await self.api.request(
            "POST",
            f"chat/sessions/{key(view.session_id)}/rewind",
            json={
                "before_message_id": editing["id"],
                "expected_revision": session["revision"],
            },
        )
        view.editing = None
        if hasattr(self.ui, "set_editing"):
            self.ui.set_editing("")
        view.session = result["session"]
        view.history_before = None
        # Images on the original message travel with the edit, as in the GUI.
        if not view.draft.images:
            view.draft.images = [
                block
                for block in editing.get("content_blocks") or []
                if block.get("type") == "image"
            ]
        await self.history()
        await self.send(text)

    async def stream(
        self, view: View, method: str, path: str, body: dict[str, Any] | None = None
    ) -> None:
        accepted = method == "GET"
        submission = method == "POST" and path == "chat/completions"
        if submission and view.pending_draft is None:
            view.pending_draft, view.draft = view.draft, Draft()
        finished = False
        retries = 0
        while True:
            try:
                params = (
                    {
                        "after": view.cursor.sequence,
                        **({"epoch": view.cursor.epoch} if view.cursor.epoch else {}),
                    }
                    if method == "GET"
                    else None
                )
                async for event, payload in self.api.events(
                    method, path, body=body, params=params
                ):
                    event = payload.get("type") or event
                    if event == "accepted" and payload.get("command_id"):
                        # Core saved the submission; it is not lost or unsent.
                        accepted = True
                        view.uncertain = False
                        view.receipt = str(payload["command_id"])
                        if submission:
                            view.pending_draft = None
                            if isinstance(self.ui, TuiUI):
                                self.ui.mark_submission("saved")
                                self.ui.set_turn_phase("queued")
                        if self.view is view:
                            self.update_toolbar(
                                "Saved in Core; waiting for its execution owner"
                            )
                        continue
                    if payload.get("turn_id"):
                        view.receipt = None
                        view.cursor.turn_id = payload["turn_id"]
                    if event in {
                        "started",
                        "queued",
                        "admitted",
                        "done",
                    } and payload.get("turn_id"):
                        accepted = True
                        view.submitting = view.session is None and not payload.get(
                            "session_id"
                        )
                        view.uncertain = False
                        if submission:
                            view.pending_draft = None
                            if isinstance(self.ui, TuiUI):
                                self.ui.mark_submission("accepted")
                        view.cursor.turn_id = (
                            payload.get("turn_id") or view.cursor.turn_id
                        )
                        view.cursor.harness_turn_id = (
                            payload.get("harness_turn_id")
                            or view.cursor.harness_turn_id
                        )
                        session_id = payload.get("session_id")
                        if session_id and view.session is None:
                            view.session = {
                                "id": session_id,
                                "engagement_id": view.project_id,
                                "title": "Conversation",
                                "backend": (body or view.config).get("backend"),
                                "model": (body or view.config).get("model"),
                                "metadata": {
                                    "control_assistant": bool(
                                        (body or {}).get("control_assistant")
                                    )
                                },
                            }
                            if isinstance(self.ui, TuiUI):
                                self.ui.set_identity(view.project, view.session)
                                asyncio.create_task(self.refresh_navigation())
                        if self.view is view:
                            self.remember()
                    if not view.cursor.accept(payload):
                        continue
                    if self.view is not view:
                        return
                    assert view is not None
                    if event == "started":
                        if isinstance(self.ui, TuiUI):
                            self.ui.set_turn_phase("working")
                        self.update_toolbar("Core accepted turn")
                    elif event in {"delta", "message_delta"}:
                        delta = terminal_text(payload.get("delta") or "")
                        view.cursor.text = (view.cursor.text + delta)[-1_000_000:]
                        self.ui.delta(delta)
                    elif event == "reasoning_delta":
                        if not view.cursor.thinking:
                            view.cursor.thinking = True
                            self.update_toolbar("Thinking")
                        if hasattr(self.ui, "reasoning"):
                            self.ui.reasoning(terminal_text(payload.get("delta") or ""))
                    elif event == "done":
                        finished = True
                        self._known_busy = False
                        self._pending_kinds.clear()
                        if isinstance(self.ui, TuiUI):
                            self.ui.finish_turn()
                        final = (payload.get("message") or {}).get("content", "")
                        if final and final != view.cursor.text:
                            self.ui.say(
                                "\nSaved answer:" if view.cursor.text else "Assistant:"
                            )
                            self.ui.markdown(final)
                        else:
                            self.ui.say()
                        if hasattr(self.ui, "turn_meta"):
                            self.ui.turn_meta(
                                turn_details(
                                    payload.get("elapsed_ms"), payload.get("usage")
                                )
                            )
                        self.ui.say("Response complete.")
                        break
                    elif event == "cancelled":
                        finished = True
                        self._known_busy = False
                        self._pending_kinds.clear()
                        if isinstance(self.ui, TuiUI):
                            self.ui.finish_turn()
                        if hasattr(self.ui, "turn_meta"):
                            self.ui.turn_meta(
                                turn_details(payload.get("elapsed_ms")), stopped=True
                            )
                        self.ui.say("\nResponse stopped.")
                        break
                    elif event == "error":
                        finished = True
                        self._known_busy = False
                        if isinstance(self.ui, TuiUI):
                            self.ui.finish_turn()
                        raise ClientError(
                            payload.get("operator_detail")
                            or payload.get("detail")
                            or payload.get("message")
                            or "Turn failed. Review /activity."
                        )
                    elif event in {
                        "approval_required",
                        "input_required",
                        "interaction_required",
                    } or (
                        event in {"approval", "interaction"}
                        and payload.get("item_status")
                        in {"waiting_approval", "waiting_input"}
                    ):
                        self._pending_kinds.add(
                            "approval"
                            if event in {"approval_required", "approval"}
                            else "input"
                        )
                        self.ui.say(
                            "\nAction required: "
                            + str(
                                payload.get("detail")
                                or payload.get("summary")
                                or payload.get("title")
                                or event.replace("_", " ")
                            )
                            + ". Use /approvals or /input."
                        )
                        self.update_toolbar("Waiting for input")
                        if isinstance(self.ui, TuiUI):
                            self.ui.set_turn_phase("waiting")
                        return
                    elif event in {
                        "callback_required",
                        "callback_waiting",
                        "waiting_callback",
                    }:
                        self.ui.say(
                            str(
                                payload.get("summary")
                                or "Core is waiting for a tool callback."
                            )
                            + " Work continues on Core; /status reads its progress."
                        )
                        self.update_toolbar("Waiting for tool callback")
                        if isinstance(self.ui, TuiUI):
                            self.ui.set_turn_phase("waiting")
                        return
                    elif event in {
                        "tool_started",
                        "tool_completed",
                        "queued",
                        "admitted",
                        "planning",
                        "progress",
                    }:
                        detail = (
                            payload.get("summary")
                            or payload.get("detail")
                            or payload.get("display_name")
                            or payload.get("message")
                            or event.replace("_", " ")
                        )
                        if event == "queued" and payload.get("queue_position"):
                            detail = f"Queued at position {payload['queue_position']}"
                        elif event == "tool_started":
                            detail = "Tool: " + str(detail)
                        elif event == "tool_completed":
                            detail = "Tool finished: " + str(detail)
                        if isinstance(self.ui, TuiUI):
                            self.ui.set_turn_phase(
                                "queued" if event == "queued" else "working"
                            )
                        self.update_toolbar(str(detail)[:160])
                if finished:
                    view.submitting = False
                    self.update_toolbar("Ready")
                    return
                raise httpx.ReadError("Stream ended before its final frame")
            except asyncio.CancelledError:
                if submission and not accepted:
                    view.uncertain = True
                    self.restore_draft(view)
                    if isinstance(self.ui, TuiUI):
                        self.ui.mark_submission("uncertain")
                view.submitting = False
                raise
            except httpx.HTTPError:
                if (
                    accepted
                    and view.cursor.turn_id
                    and retries < 3
                    and self.view is view
                ):
                    assert view is not None
                    retries += 1
                    method, path, body = (
                        "GET",
                        f"chat/turns/{key(view.cursor.turn_id)}/events",
                        None,
                    )
                    self.update_toolbar("Reconnecting")
                    if isinstance(self.ui, TuiUI):
                        self.ui.set_turn_phase("reconnecting")
                    await asyncio.sleep(retries)
                    try:
                        state = (
                            await self.state(view) if view.session else {"busy": True}
                        )
                        if not state.get("busy"):
                            await self.history()
                            self.ui.say(state.get("detail", "Read saved state."))
                            view.submitting = False
                            if isinstance(self.ui, TuiUI):
                                self.ui.stream_stopped()
                            return
                    except httpx.HTTPError:
                        continue
                    except ClientError as exc:
                        view.submitting = False
                        if isinstance(self.ui, TuiUI):
                            self.ui.stream_stopped()
                        self.ui.say(
                            "Cannot reconcile Core state: "
                            + str(exc)
                            + ". Nothing was resent."
                        )
                        return
                    method, path, body = (
                        "GET",
                        f"chat/turns/{key(view.cursor.turn_id)}/events",
                        None,
                    )
                    continue
                view.uncertain = not accepted
                view.submitting = False
                if isinstance(self.ui, TuiUI):
                    self.ui.stream_stopped()
                if not accepted:
                    self.restore_draft(view)
                    if isinstance(self.ui, TuiUI):
                        self.ui.mark_submission("uncertain")
                if self.view is view:
                    self.update_toolbar("Connection lost")
                    self.ui.say(
                        "\nConnection lost. Nothing was resent. Review /status and /history, then /follow; use /chats if no conversation identity was confirmed."
                    )
                return
            except AcceptedRequest:
                # Saved on Core but not started: keep it out of the draft and
                # never present it as lost. /follow attaches when it starts.
                view.submitting = False
                if isinstance(self.ui, TuiUI):
                    self.ui.set_turn_phase("queued")
                if self.view is view:
                    self.update_toolbar("Saved in Core; not started yet")
                    self.ui.say(
                        "\nCore saved this message but has not started it yet. "
                        "Nothing was resent. /follow attaches when it starts; "
                        "/status reads its state."
                    )
                return
            except ClientError as exc:
                view.submitting = False
                if isinstance(self.ui, TuiUI):
                    self.ui.stream_stopped()
                    self.ui.show_problem(str(exc))
                if not accepted:
                    self._known_busy = False
                    self.restore_draft(view)
                    if isinstance(self.ui, TuiUI):
                        self.ui.mark_submission("failed")
                if self.view is view:
                    self.update_toolbar("Needs review" if accepted else "Failed")
                    self.ui.say("\n" + str(exc))
                return

    def restore_draft(self, view: View) -> None:
        if view.pending_draft is not None:
            if view.draft == Draft():
                view.draft, view.pending_draft = view.pending_draft, None
            elif self.view is view:
                self.ui.say(
                    "The previous submission remains in memory. /draft reviews it; nothing was resent."
                )

    async def follow(self) -> None:
        view = self.current()
        state = (
            await self.state()
            if view.session or view.receipt
            else {
                "turn_id": view.cursor.turn_id,
                "harness_turn_id": view.cursor.harness_turn_id,
            }
        )
        if not state.get("turn_id") and view.session is None and view.receipt:
            # Attach to the saved request itself until Core binds its turn.
            await self.detach()
            view.cursor = Cursor()
            self.stream_task = asyncio.create_task(
                self.stream(
                    view, "GET", f"runtime/requests/{key(view.receipt)}/response"
                )
            )
            self.ui.say(state.get("detail") or "Waiting for Core to start it.")
            return
        if not state.get("turn_id"):
            await self.history()
            return
        await self.detach()
        if view.cursor.turn_id != state["turn_id"]:
            view.cursor = Cursor(
                turn_id=state["turn_id"], harness_turn_id=state.get("harness_turn_id")
            )
        self.stream_task = asyncio.create_task(
            self.stream(view, "GET", f"chat/turns/{key(state['turn_id'])}/events")
        )

    async def stop(self) -> None:
        view = self.current()
        state = await self.state()
        if "stop" not in state.get("actions", []):
            raise ClientError(
                "This turn is idle or its runtime does not support interruption. Read /status."
            )
        result = await self.api.request(
            "POST", f"chat/turns/{key(state['turn_id'])}/cancel"
        )
        await self.detach()
        view.submitting = False
        if isinstance(self.ui, TuiUI):
            self.ui.finish_turn()
        self.ui.say(f"Turn {result['status']}.")
        await self.status()

    async def detach(self) -> None:
        if self.stream_task is not None:
            self.stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.stream_task
            self.stream_task = None
            if isinstance(self.ui, TuiUI):
                self.ui.stream_stopped()

    async def selected_state_loop(self) -> None:
        """Poll only the visible chat; stream events already cover attached turns."""
        while True:
            await asyncio.sleep(5)
            if self.stream_task is None or self.stream_task.done():
                await self.refresh_selected_state()

    async def stop_selected_state(self) -> None:
        if self.selected_state_task:
            self.selected_state_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.selected_state_task
            self.selected_state_task = None

    async def command(self, text: str) -> None:
        from .attachments import attach, attachments
        from .settings import model, select_mcp_servers, select_skill, settings
        from .workflows import actions

        parts = shlex.split(text[1:])
        if not parts:
            return
        name, args = parts[0], parts[1:]
        if name in {"quit", "exit"}:
            self.running = False
        elif name == "help":
            self.ui.help()
        elif name == "commands":
            choice = await self.ui.choose(
                "Command",
                [
                    {"id": key, "name": f"/{key} — {description}"}
                    for key, description in COMMANDS.items()
                    if key != "commands" and key in self.available_commands()
                ],
            )
            await self.command("/" + choice["id"])
        elif name == "draft":
            view = self.current()
            if view.pending_draft is None:
                self.ui.say("No previous submission is waiting in memory.")
            else:
                self.ui.say(view.pending_draft.text)
                if not view.submitting and await self.ui.confirm(
                    "Swap this previous submission into the unsent draft?"
                ):
                    view.draft, view.pending_draft = view.pending_draft, view.draft
        elif name == "core":
            await self.connect(args[0] if args else None)
            await self.select_project()
        elif name == "projects":
            await self.select_project(args[0] if args else None)
        elif name == "snapshot":
            from .snapshot import show_project_snapshot

            await show_project_snapshot(self)
        elif name == "notes":
            from .notes import show_project_notes

            await show_project_notes(self)
        elif name == "board":
            from .board import show_project_board

            await show_project_board(self)
        elif name == "chats":
            await self.select_chat(" ".join(args) or None)
        elif name == "new":
            await self.open_view(self.current().project, None)
            self.current().config = {}
            self.current().draft = Draft()
        elif name == "butler":
            await self.open_butler()
        elif name == "history":
            await self.history(bool(args and args[0] == "more"))
        elif name == "thinking":
            await self.toggle_thinking()
        elif name == "search":
            from .conversations import search

            await search(self, args)
        elif name == "bookmark":
            from .conversations import bookmark

            await bookmark(self, args)
        elif name == "side":
            from .conversations import side

            await side(self, args)
        elif name == "status":
            await self.status()
        elif name == "follow":
            await self.follow()
        elif name == "detach":
            await self.detach()
            self.ui.say("Viewer detached. Core work continues.")
        elif name == "stop":
            await self.stop()
        elif name == "model":
            await model(self)
        elif name == "settings":
            await settings(self)
        elif name == "mcp":
            await select_mcp_servers(self, args)
        elif name == "skills":
            await select_skill(self, " ".join(args) or None)
        elif name == "attach":
            await attach(self, args)
        elif name == "attachments":
            await attachments(self)
        elif name in actions:
            await actions[name](self, args)
        else:
            raise ClientError("Unknown command. Use /help.")
        self.update_toolbar()

    async def run(self, project: str | None = None, chat: str | None = None) -> None:
        try:
            await self.connect(self.settings.core if self._api is None else None)
            prefs = self.settings.entry.get("selection") or {}
            restored = False
            if project is None and prefs.get("project_id"):
                try:
                    selected = await self.api.request(
                        "GET", f"engagements/{key(prefs['project_id'])}"
                    )
                    if chat is not None:
                        await self.select_chat(chat, project=selected)
                    elif prefs.get("session_id"):
                        session = await self.api.request(
                            "GET",
                            f"chat-sessions/{key(prefs['session_id'])}",
                            params={"engagement_id": selected["id"]},
                        )
                        await self.open_view(selected, session)
                    else:
                        await self.open_view(selected, None)
                    restored = True
                except ApiError as exc:
                    if exc.status not in {404, 409}:
                        raise
                    self.ui.say(
                        "The remembered conversation is unavailable. Choose another."
                    )
            if not restored:
                await self.select_project(project, chat=chat)
            self.ui.say("/help for commands. Core work continues after /quit.")
            while self.running:
                text = ""
                try:
                    view = self.current()
                    text = await self.ui.ask("You", default=view.draft.text, main=True)
                    if isinstance(self.ui, TuiUI):
                        preserved = self.ui.take_preserved_draft()
                        if preserved:
                            view.draft.text = preserved
                    if not text.strip():
                        continue
                    if text.startswith("/"):
                        await self.command(text)
                    else:
                        await self.send(text)
                except InterruptedPrompt:
                    try:
                        state = await self.state()
                        if state.get("busy"):
                            await self.stop()
                    except (ClientError, httpx.HTTPError) as exc:
                        if isinstance(self.ui, TuiUI):
                            self.ui.show_problem(str(exc))
                        self.ui.say(
                            str(exc)
                            if isinstance(exc, ClientError)
                            else "Cannot read Core state. Nothing was cancelled; use /status."
                        )
                except CancelledInput:
                    continue
                except EOFError:
                    break
                except (ClientError, ValueError, OSError, httpx.HTTPError) as exc:
                    if isinstance(exc, httpx.ReadTimeout):
                        message = (
                            "Core timed out during /goal. The goal may have changed. "
                            "Use /goal show to read its saved state before retrying; "
                            "nothing was resent."
                            if text == "/goal" or text.startswith("/goal ")
                            else "Core timed out. Core may have processed the request. "
                            "Check its saved state before retrying; nothing was resent."
                        )
                    elif isinstance(exc, httpx.HTTPError):
                        message = (
                            "Cannot reach Core securely. Check the connection; "
                            "nothing was automatically resent."
                        )
                    else:
                        message = str(exc)
                    if isinstance(self.ui, TuiUI):
                        self.ui.show_problem(message)
                    self.ui.say(message)
        except (CancelledInput, EOFError):
            self.ui.say("Terminal closed. Core work continues.")
        finally:
            await self.detach()
            await self.stop_selected_state()
            if self._api:
                await self._api.close()


def run_shell(
    settings: Settings, *, project: str | None = None, chat: str | None = None
) -> None:
    ui = TerminalUI()
    if ui.plain:
        with patch_stdout():
            asyncio.run(Shell(settings, ui=ui).run(project, chat))
        return

    async def run_tui() -> None:
        screen = TuiUI()
        await screen.start()
        try:
            await Shell(settings, ui=screen).run(project, chat)
        finally:
            await screen.close()

    asyncio.run(run_tui())
