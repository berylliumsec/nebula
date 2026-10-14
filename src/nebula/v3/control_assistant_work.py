"""Project-bound chat and mission operations for the dedicated Nebula assistant.

This is a narrow control plane over Core's existing services. It deliberately
does not expose a generic API caller or general agent tools to the control turn.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import FastAPI

from .chat import ChatCompletionRequest, ChatRequestMessage, ChatService
from .chat_queue import ChatQueueService, QueueWrite
from .domain import (
    AgentRun,
    ChatBackend,
    ChatSession,
    Engagement,
    HarnessProfile,
    ProviderProfile,
    RunBackend,
    Task,
)
from .harnesses import HarnessRuntimeService
from .settings_assistant_tools import model_choices
from .storage import NebulaStore


EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}
CHAT_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"session_id": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["session_id"],
}
MISSION_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"run_id": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["run_id"],
}
START_CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
        "backend": {"type": "string", "enum": ["provider", "harness"]},
        "profile_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "model": {"type": "string", "minLength": 1, "maxLength": 500},
        "tools_enabled": {"type": "boolean"},
    },
    "required": ["prompt"],
}
MESSAGE_CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CHAT_TARGET_SCHEMA["properties"],
        "text": {"type": "string", "minLength": 1, "maxLength": 20000},
        "delivery": {"type": "string", "enum": ["next", "steer"]},
    },
    "required": ["session_id", "text"],
}
START_MISSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 300},
        "objective": {"type": "string", "minLength": 1, "maxLength": 10000},
        "backend": {"type": "string", "enum": ["provider", "harness"]},
        "profile_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "model": {"type": "string", "minLength": 1, "maxLength": 500},
        "max_tool_calls": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["name", "objective"],
}
STEER_MISSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **MISSION_TARGET_SCHEMA["properties"],
        "text": {"type": "string", "minLength": 1, "maxLength": 20000},
    },
    "required": ["run_id", "text"],
}


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"provide {label} of 1 to {limit} characters")
    return value.strip()


def _chat_url(project_id: str, session_id: str) -> str:
    return f"/projects/{quote(project_id, safe='')}/workbench?view=chat&session={quote(session_id, safe='')}"


def _mission_url(project_id: str, run_id: str) -> str:
    return f"/projects/{quote(project_id, safe='')}/workbench?view=missions&mission={quote(run_id, safe='')}"


class ControlAssistantWork:
    def __init__(
        self,
        store: NebulaStore,
        chat: ChatService,
        queue: ChatQueueService,
        harness: HarnessRuntimeService,
        app: FastAPI,
        token: str,
    ) -> None:
        self.store = store
        self.chat = chat
        self.queue = queue
        self.harness = harness
        self.app = app
        self.token = token

    def _project(self, project_id: str) -> Engagement:
        return self.store.get(Engagement, project_id)

    def _chat(self, project_id: str, session_id: Any) -> ChatSession:
        chat = self.store.get(ChatSession, _text(session_id, "session_id", 200))
        if (
            chat.engagement_id != project_id
            or chat.metadata.get("control_assistant") is True
            or chat.metadata.get("side_chat") is True
            or chat.metadata.get("temporary_assistant") is True
        ):
            raise ValueError("choose an assistant chat from this project's work roster")
        return chat

    def _run(self, project_id: str, run_id: Any) -> AgentRun:
        run = self.store.get(AgentRun, _text(run_id, "run_id", 200))
        if run.engagement_id != project_id:
            raise ValueError("choose a mission from this project's work roster")
        return run

    def catalog(self, project_id: str) -> dict[str, Any]:
        self._project(project_id)
        return {
            "project_id": project_id,
            "runtimes": model_choices(self.store),
            "actions": [
                "nebula.work.overview",
                "nebula.work.chat",
                "nebula.work.mission",
                "nebula.work.start_chat",
                "nebula.work.message_chat",
                "nebula.work.stop_chat",
                "nebula.work.start_mission",
                "nebula.work.steer_mission",
                "nebula.work.stop_mission",
            ],
            "scope": "selected project; browser and notes control are not included",
        }

    def overview(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        pending = self.chat.pending_turns(project_id)
        chats = [
            item
            for item in self.store.find_entities(
                ChatSession,
                {},
                engagement_id=project_id,
                newest_first=True,
                limit=80,
            )
            if item.metadata.get("control_assistant") is not True
            and item.metadata.get("side_chat") is not True
            and item.metadata.get("temporary_assistant") is not True
        ]
        runs = self.store.find_entities(
            AgentRun,
            {},
            engagement_id=project_id,
            newest_first=True,
            limit=50,
        )
        return {
            "project": {"id": project.id, "name": project.name},
            "chats": [
                {
                    "id": item.id,
                    "title": item.title,
                    "backend": item.backend.value,
                    "model": item.model,
                    "state": "working" if item.id in pending else "idle",
                    "turn_id": pending[item.id].id if item.id in pending else None,
                    "url": _chat_url(project_id, item.id),
                }
                for item in chats[:30]
            ],
            "missions": [
                {
                    "id": item.id,
                    "name": str(item.metadata.get("name") or item.objective)[:300],
                    "status": item.status.value,
                    "backend": item.backend.value,
                    "updated_at": item.updated_at.isoformat(),
                    "url": _mission_url(project_id, item.id),
                }
                for item in runs[:30]
            ],
            "more_chats": len(chats) > 30,
            "more_missions": len(runs) > 30,
        }

    def chat_detail(self, project_id: str, session_id: Any) -> dict[str, Any]:
        session = self._chat(project_id, session_id)
        messages = self.chat.session_messages(session.id)
        pending = self.chat.pending_turn(session.id)
        queue = self.queue.get(session.id)
        return {
            "id": session.id,
            "title": session.title,
            "url": _chat_url(project_id, session.id),
            "backend": session.backend.value,
            "model": session.model,
            "pending_turn": {
                "id": pending.id,
                "status": pending.status.value,
                "harness_turn_id": pending.harness_turn_id,
            }
            if pending
            else None,
            "queued_follow_ups": [
                {"id": item.get("id"), "status": item.get("status")}
                for item in queue.items
                if item.get("status") not in {"complete", "cancelled"}
            ][:20],
            "recent_messages": [
                {"role": item.role.value, "content": item.content[:2000]}
                for item in messages[-8:]
                if item.role.value in {"user", "assistant"}
            ],
        }

    def mission_detail(self, project_id: str, run_id: Any) -> dict[str, Any]:
        run = self._run(project_id, run_id)
        tasks = self.store.find_entities(
            Task, {"run_id": run.id}, engagement_id=project_id, limit=30
        )
        return {
            "id": run.id,
            "name": str(run.metadata.get("name") or run.objective)[:300],
            "url": _mission_url(project_id, run.id),
            "objective": run.objective[:5000],
            "status": run.status.value,
            "backend": run.backend.value,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "tasks": [
                {"id": task.id, "title": task.title, "status": task.status.value}
                for task in tasks
            ],
        }

    def _runtime(self, arguments: dict[str, Any]) -> tuple[str, str, str]:
        requested_backend = arguments.get("backend")
        requested_profile = arguments.get("profile_id")
        requested_model = arguments.get("model")
        if requested_backend is not None and requested_backend not in {
            "provider",
            "harness",
        }:
            raise ValueError("choose provider or harness backend")
        if requested_profile is not None:
            profile_id = _text(requested_profile, "profile_id", 200)
            if requested_backend is None:
                raise ValueError("provide backend with profile_id")
            backend = requested_backend
        else:
            available = model_choices(self.store)
            backend = requested_backend or (
                "provider" if available["providers"] else "harness"
            )
            options = available["providers" if backend == "provider" else "harnesses"]
            if not options:
                raise ValueError(
                    f"no enabled {backend} runtime is configured; choose one in Settings"
                )
            profile_id = options[0]["id"]
        if backend == "provider":
            profile = self.store.get(ProviderProfile, profile_id)
            choices = profile.model_allowlist
            default = profile.metadata.get("default_model")
        else:
            profile = self.store.get(HarnessProfile, profile_id)
            choices = profile.capabilities.models
            default = profile.default_model
        model = (
            requested_model
            or (default if default in choices else None)
            or (choices[0] if choices else None)
        )
        if not profile.enabled or not isinstance(model, str) or model not in choices:
            raise ValueError(
                "choose an enabled runtime and one of its listed models from nebula.work.catalog"
            )
        return backend, profile_id, model

    async def start_chat(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self._project(project_id)
        prompt = _text(arguments.get("prompt"), "prompt", 20000)
        backend, profile_id, model = self._runtime(arguments)
        tools_enabled = arguments.get("tools_enabled", False)
        if type(tools_enabled) is not bool:
            raise ValueError("tools_enabled must be true or false")
        if backend == "provider":
            request = ChatCompletionRequest(
                backend=ChatBackend.PROVIDER,
                engagement_id=project_id,
                provider_id=profile_id,
                model=model,
                messages=[ChatRequestMessage(role="user", content=prompt)],
                tools_enabled=tools_enabled,
                stream=True,
            )
            prepared = await self.chat.prepare_async(request)
            if prepared.turn is None:
                raise RuntimeError("Core did not prepare a durable chat turn")
            self.chat.start_provider_turn(prepared)
            session = prepared.session or prepared.pending_session
            if session is None:
                raise RuntimeError("Core did not save the assistant chat")
            turn_id = prepared.turn.id
        else:
            session, turn, harness_turn = self.harness.prepare_chat(
                engagement_id=project_id,
                profile_id=profile_id,
                model=model,
                prompt=prompt,
                chat_session_id=None,
                harness_session_id=None,
                mcp_server_ids=[],
                include_knowledge=True,
                allow_cloud_knowledge=False,
                allow_remote_mcp=False,
            )
            self.harness.start_chat_turn(harness_turn.id)
            turn_id = turn.id
        saved = self._chat(project_id, session.id)
        return {
            "session_id": saved.id,
            "title": saved.title,
            "url": _chat_url(project_id, saved.id),
            "turn_id": turn_id,
            "backend": saved.backend.value,
            "status": "started",
            "result": "work continues in Core",
        }

    async def message_chat(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._chat(project_id, arguments.get("session_id"))
        text = _text(arguments.get("text"), "text", 20000)
        delivery = arguments.get("delivery", "next")
        if delivery == "steer":
            pending = self.chat.pending_turn(session.id)
            if (
                session.backend != ChatBackend.HARNESS
                or pending is None
                or not pending.harness_turn_id
            ):
                raise ValueError(
                    "live steering requires an active harness chat; use delivery=next to queue a follow-up"
                )
            await self.harness.steer_turn(
                pending.harness_turn_id, text, actor_id="control-assistant"
            )
            observed = self.chat.pending_turn(session.id)
            return {
                "session_id": session.id,
                "delivery": "steered",
                "turn_id": observed.id if observed else pending.id,
            }
        if delivery != "next":
            raise ValueError("choose next or steer delivery")
        request = ChatCompletionRequest(
            backend=session.backend,
            engagement_id=project_id,
            session_id=session.id,
            provider_id=session.provider_profile_id,
            harness_profile_id=session.harness_profile_id,
            model=session.model,
            messages=[ChatRequestMessage(role="user", content=text)],
            tools_enabled=bool(session.metadata.get("tools_enabled")),
            stream=True,
        )
        queue = self.queue.get(session.id)
        key = f"control:{uuid4()}"
        saved = self.queue.write(
            session.id,
            QueueWrite(
                expected_revision=queue.revision
                if self.queue.exists(session.id)
                else 0,
                action="enqueue",
                idempotency_key=key,
                request=request,
            ),
        )
        item = next(item for item in saved.items if item.get("key") == key)
        return {
            "session_id": session.id,
            "url": _chat_url(project_id, session.id),
            "delivery": "queued",
            "item_id": item["id"],
            "status": item["status"],
        }

    async def stop_chat(self, project_id: str, session_id: Any) -> dict[str, Any]:
        session = self._chat(project_id, session_id)
        pending = self.chat.pending_turn(session.id)
        if pending is None:
            return {"session_id": session.id, "status": "idle", "stopped": False}
        if pending.harness_turn_id:
            await self.harness.cancel_turn(
                pending.harness_turn_id, reason="Stopped through Nebula assistant"
            )
        else:
            await self.chat.stop_provider_turn(pending.id)
        observed = self.chat.pending_turn(session.id)
        return {
            "session_id": session.id,
            "turn_id": pending.id,
            "stop_requested": True,
            "stopped": observed is None,
            "status": observed.status.value if observed else "idle",
        }

    async def _request(
        self, method: str, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://127.0.0.1",
            timeout=30.0,
        ) as client:
            response = await client.request(
                method,
                path,
                json=body,
                headers={"Authorization": f"Bearer {self.token}"},
            )
        if response.is_error:
            try:
                detail = response.json().get("detail", response.reason_phrase)
            except (ValueError, AttributeError):
                detail = response.reason_phrase
            raise ValueError(
                f"Core rejected the work action ({response.status_code}): {str(detail)[:500]}"
            )
        return response.json()

    async def start_mission(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self._project(project_id)
        name = _text(arguments.get("name"), "name", 300)
        objective = _text(arguments.get("objective"), "objective", 10000)
        backend, profile_id, model = self._runtime(arguments)
        max_tool_calls = arguments.get("max_tool_calls")
        if max_tool_calls is not None and (
            type(max_tool_calls) is not int or not 0 <= max_tool_calls <= 100
        ):
            raise ValueError("max_tool_calls must be from 0 to 100")
        body = {
            "engagement_id": project_id,
            "name": name,
            "objective": objective,
            "backend": RunBackend.NATIVE.value
            if backend == "provider"
            else RunBackend.HARNESS.value,
            "provider_id": profile_id if backend == "provider" else None,
            "harness_profile_id": profile_id if backend == "harness" else None,
            "model": model,
            "allow_cloud_tool_results": False,
        }
        if max_tool_calls is not None:
            body["max_tool_calls"] = max_tool_calls
        result = await self._request("POST", "/api/v1/missions", body)
        run = self._run(project_id, result["id"])
        return {
            "run_id": run.id,
            "url": _mission_url(project_id, run.id),
            "name": str(run.metadata.get("name") or name),
            "status": run.status.value,
            "result": "mission is saved in Core",
        }

    async def steer_mission(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        run = self._run(project_id, arguments.get("run_id"))
        text = _text(arguments.get("text"), "text", 20000)
        if run.backend != RunBackend.HARNESS:
            raise ValueError(
                "live mission steering is available for active harness missions only; inspect this mission to discuss or stop it"
            )
        await self._request("POST", f"/api/v1/runs/{run.id}/steer", {"text": text})
        observed = self._run(project_id, run.id)
        return {"run_id": run.id, "status": observed.status.value, "steered": True}

    async def stop_mission(self, project_id: str, run_id: Any) -> dict[str, Any]:
        run = self._run(project_id, run_id)
        await self._request(
            "POST",
            f"/api/v1/runs/{run.id}/stop",
            {"reason": "Stopped through Nebula assistant"},
        )
        observed = self._run(project_id, run.id)
        return {
            "run_id": run.id,
            "status": observed.status.value,
            "stopped": observed.status.value in {"cancelled", "complete"},
        }


WORK_TOOLS: tuple[tuple[str, str, dict[str, Any], bool], ...] = (
    (
        "nebula.work.catalog",
        "List available project chat and mission controls and configured runtimes.",
        EMPTY_SCHEMA,
        True,
    ),
    (
        "nebula.work.overview",
        "List this project's assistant chats and missions with current status and IDs. Call before choosing a target.",
        EMPTY_SCHEMA,
        True,
    ),
    (
        "nebula.work.chat",
        "Read recent messages and pending activity for one listed assistant chat.",
        CHAT_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.mission",
        "Read the saved status and tasks for one listed mission.",
        MISSION_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.start_chat",
        "Start a separate Core-owned assistant chat for this project. Use a listed runtime. tools_enabled grants that chat its usual project tools; use only when the operator requested work. The result means started, not finished.",
        START_CHAT_SCHEMA,
        False,
    ),
    (
        "nebula.work.message_chat",
        "Send a durable next message to one listed assistant chat, or steer its active harness turn. Use delivery=next unless the operator wants live steering. A queued message is not a completed answer.",
        MESSAGE_CHAT_SCHEMA,
        False,
    ),
    (
        "nebula.work.stop_chat",
        "Stop the selected assistant chat's active turn only. Queued follow-ups are separate.",
        CHAT_TARGET_SCHEMA,
        False,
    ),
    (
        "nebula.work.start_mission",
        "Start a Core-owned mission for this project using a listed runtime. The result means saved and queued or running, not finished.",
        START_MISSION_SCHEMA,
        False,
    ),
    (
        "nebula.work.steer_mission",
        "Guide an active harness mission. Native provider missions cannot be live-steered.",
        STEER_MISSION_SCHEMA,
        False,
    ),
    (
        "nebula.work.stop_mission",
        "Stop one selected mission and read back its Core status.",
        MISSION_TARGET_SCHEMA,
        False,
    ),
)


async def dispatch_work_tool(
    work: ControlAssistantWork, project_id: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    if name == "nebula.work.catalog":
        return work.catalog(project_id)
    if name == "nebula.work.overview":
        return work.overview(project_id)
    if name == "nebula.work.chat":
        return work.chat_detail(project_id, arguments.get("session_id"))
    if name == "nebula.work.mission":
        return work.mission_detail(project_id, arguments.get("run_id"))
    if name == "nebula.work.start_chat":
        return await work.start_chat(project_id, arguments)
    if name == "nebula.work.message_chat":
        return await work.message_chat(project_id, arguments)
    if name == "nebula.work.stop_chat":
        return await work.stop_chat(project_id, arguments.get("session_id"))
    if name == "nebula.work.start_mission":
        return await work.start_mission(project_id, arguments)
    if name == "nebula.work.steer_mission":
        return await work.steer_mission(project_id, arguments)
    if name == "nebula.work.stop_mission":
        return await work.stop_mission(project_id, arguments.get("run_id"))
    raise ValueError("work tool is not available")
