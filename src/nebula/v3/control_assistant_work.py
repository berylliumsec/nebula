"""Project-bound chat and mission operations for the dedicated Nebula assistant.

This is a project-bound control plane over Core's chat and mission services.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import FastAPI

from .chat import (
    ChatCompletionRequest,
    ChatRequestMessage,
    ChatRuntimeSwitchPreflightRequest,
    ChatService,
)
from .chat_assistant_config import (
    ChatAssistantConfigPatch,
    chat_assistant_config,
    update_chat_assistant_config,
)
from .chat_queue import ChatQueueService, QueueWrite
from .domain import (
    AgentRun,
    Approval,
    ApprovalStatus,
    ChatBackend,
    ChatRole,
    ChatSession,
    Engagement,
    HarnessProfile,
    HarnessInteraction,
    HarnessInteractionStatus,
    HarnessTurnOrigin,
    McpServerProfile,
    ProviderProfile,
    RunBackend,
    RunStatus,
    ScopePolicy,
    Task,
    ToolCallOrigin,
)
from .harnesses import HarnessRuntimeService
from .mcp import usable_mcp_tools
from .native_hooks import discover_native_hooks
from .settings_assistant_tools import model_choices
from .storage import NebulaStore, NotFoundError


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
CHAT_SETTINGS_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"session_id": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["session_id"],
}
CHAT_SETTINGS_FIELDS: dict[str, Any] = {
    "backend": {"type": "string", "enum": ["provider", "harness"]},
    "profile_id": {"type": "string", "minLength": 1, "maxLength": 200},
    "model": {"type": "string", "minLength": 1, "maxLength": 500},
    "reasoning_effort": {
        "type": ["string", "null"],
        "enum": [None, "none", "minimal", "low", "medium", "high", "xhigh"],
    },
    "harness_reasoning_effort": {"type": ["string", "null"]},
    "harness_service_tier": {"type": ["string", "null"]},
    "harness_mode": {"type": ["string", "null"]},
    "harness_cyber_access_program": {
        "type": ["string", "null"],
        "enum": [None, "automatic", "standard", "daybreakBlue", "daybreakRed"],
    },
    "mcp_server_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
    "hook_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
    "include_knowledge": {"type": "boolean"},
    "allow_cloud_knowledge": {"type": "boolean"},
    "tools_enabled": {"type": "boolean"},
    "allow_subagents": {"type": "boolean"},
    "allow_agent_messaging": {"type": "boolean"},
    "max_active_subagents": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
    "subagent_provider_id": {"type": ["string", "null"]},
    "subagent_harness_profile_id": {"type": ["string", "null"]},
    "subagent_model": {"type": ["string", "null"]},
    "subagent_reasoning_effort": {
        "type": ["string", "null"],
        "enum": [None, "none", "minimal", "low", "medium", "high", "xhigh"],
    },
}
SET_CHAT_SETTINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "session_id": CHAT_SETTINGS_TARGET_SCHEMA["properties"]["session_id"],
        "expected_revision": {"type": "integer", "minimum": 1},
        "settings": {
            "type": "object",
            "additionalProperties": False,
            "properties": CHAT_SETTINGS_FIELDS,
            "minProperties": 1,
        },
    },
    "required": ["session_id", "expected_revision", "settings"],
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
        "harness_session_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "settings": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                key: value
                for key, value in CHAT_SETTINGS_FIELDS.items()
                if key not in {"backend", "profile_id", "model"}
            },
            "minProperties": 1,
        },
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
ACTION_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"action_id": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["action_id"],
}
DECIDE_APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **ACTION_TARGET_SCHEMA["properties"],
        "expected_revision": {"type": "integer", "minimum": 1},
        "decision": {"type": "string", "enum": ["approve", "reject", "stop"]},
        "reason": {"type": "string", "maxLength": 1000},
    },
    "required": ["action_id", "expected_revision", "decision"],
}
ANSWER_QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **ACTION_TARGET_SCHEMA["properties"],
        "expected_revision": {"type": "integer", "minimum": 1},
        "action": {"type": "string", "enum": ["answer", "decline", "cancel"]},
        "response": {"type": "object", "maxProperties": 32},
    },
    "required": ["action_id", "expected_revision", "action"],
}
UPDATE_CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CHAT_TARGET_SCHEMA["properties"],
        "expected_revision": {"type": "integer", "minimum": 1},
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
        "archived": {"type": "boolean"},
    },
    "required": ["session_id", "expected_revision"],
}
RETRY_MISSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **MISSION_TARGET_SCHEMA["properties"],
        "expected_revision": {"type": "integer", "minimum": 1},
        "allow_cloud_tool_results": {"type": "boolean"},
    },
    "required": ["run_id", "expected_revision"],
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

    def _settings_chat(self, project_id: str, session_id: Any) -> ChatSession:
        target = _text(session_id, "session_id", 200)
        if target == "self":
            matches = self.store.find_entities(
                ChatSession,
                {},
                engagement_id=project_id,
                newest_first=True,
                limit=None,
            )
            session = next(
                (
                    item
                    for item in matches
                    if item.metadata.get("control_assistant") is True
                    and item.metadata.get("archived_at") is None
                ),
                None,
            )
            if session is None:
                raise ValueError("Nebula assistant has no saved conversation yet")
            return session
        return self._chat(project_id, target)

    def chat_settings(self, project_id: str, session_id: Any) -> dict[str, Any]:
        session = self._settings_chat(project_id, session_id)
        current = chat_assistant_config(self.store, session)
        available = model_choices(self.store)
        harness = None
        if current["backend"] == "harness":
            harness = self.store.get(HarnessProfile, current["profile_id"])
        model_option = (
            next(
                (
                    item
                    for item in harness.capabilities.model_options
                    if item.model == current["model"]
                ),
                None,
            )
            if harness
            else None
        )
        project = self._project(project_id)
        scope = (
            self.store.get(ScopePolicy, project.scope_policy_id)
            if project.scope_policy_id
            else None
        )
        mcp = [
            {
                "id": item.id,
                "name": item.name,
                "tools": len(
                    usable_mcp_tools(
                        item,
                        bypass_permissions=bool(scope and scope.bypass_permissions),
                    )
                ),
            }
            for item in self.store.find_entities(McpServerProfile, {}, limit=None)
            if item.enabled
            and (
                item.metadata.get("built_in") != "notebook_v1"
                or item.metadata.get("engagement_id") == project_id
            )
            and item.capabilities.checked_at is not None
        ]
        hooks = (
            [
                {"id": item.id, "name": item.manifest.name}
                for item in discover_native_hooks(
                    self.chat.workspace_resolver(project_id)
                )
            ]
            if current["backend"] == "provider"
            else []
        )
        return {
            "session_id": session.id,
            "target": "self"
            if session.metadata.get("control_assistant") is True
            else "chat",
            "title": session.title,
            "url": _chat_url(project_id, session.id),
            "revision": session.revision,
            "settings": current,
            "bound_runtime": {
                "backend": session.backend.value,
                "profile_id": session.provider_profile_id
                if session.backend == ChatBackend.PROVIDER
                else session.harness_profile_id,
                "model": session.model,
            },
            "available": {
                **available,
                "harness_reasoning_efforts": [
                    item.model_dump(mode="json")
                    for item in model_option.reasoning_efforts
                ]
                if model_option
                else [],
                "harness_service_tiers": [
                    item.model_dump(mode="json") for item in model_option.service_tiers
                ]
                if model_option
                else [],
                "harness_cyber_access_programs": model_option.cyber_access_programs
                if model_option
                else None,
                "harness_modes": harness.capabilities.modes if harness else [],
                "mcp_servers": mcp,
                "hooks": hooks,
            },
            "applies_to": "next message; queued and running turns keep their submitted settings",
        }

    def set_chat_settings(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._settings_chat(project_id, arguments.get("session_id"))
        expected_revision = arguments.get("expected_revision")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("read current settings and provide expected_revision")
        patch = ChatAssistantConfigPatch.model_validate(arguments.get("settings"))
        current = chat_assistant_config(self.store, session)
        selected = {**current, **patch.model_dump(exclude_unset=True, mode="json")}
        if (
            session.backend == ChatBackend.PROVIDER
            and selected["backend"] == "provider"
            and (selected["profile_id"], selected["model"])
            != (session.provider_profile_id, session.model)
        ):
            preflight = self.chat.runtime_switch_preflight(
                session.id,
                ChatRuntimeSwitchPreflightRequest(
                    provider_id=selected["profile_id"],
                    model=selected["model"],
                    tools_enabled=bool(selected["tools_enabled"]),
                    expected_session_revision=expected_revision,
                ),
            )
            if not preflight.compatible or preflight.requires_compaction_confirmation:
                raise ValueError(
                    preflight.reason or "open the chat to confirm this runtime switch"
                )
        saved = update_chat_assistant_config(
            self.store,
            session,
            patch,
            expected_revision=expected_revision,
            workspace_resolver=self.chat.workspace_resolver,
        )
        return {
            "session_id": saved.id,
            "url": _chat_url(project_id, saved.id),
            "revision": saved.revision,
            "settings": chat_assistant_config(self.store, saved),
            "applies_to": "next message; queued and running turns keep their submitted settings",
        }

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
                "nebula.work.chat_settings",
                "nebula.work.set_chat_settings",
                "nebula.work.mission",
                "nebula.work.start_chat",
                "nebula.work.message_chat",
                "nebula.work.stop_chat",
                "nebula.work.start_mission",
                "nebula.work.steer_mission",
                "nebula.work.stop_mission",
                "nebula.work.attention",
                "nebula.work.approval",
                "nebula.work.decide_approval",
                "nebula.work.question",
                "nebula.work.answer_question",
                "nebula.work.update_chat",
                "nebula.work.retry_mission",
            ],
            "scope": "selected project; browser and notes control are not included",
        }

    def overview(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        pending = self.chat.pending_turns(project_id)
        attention = self.attention(project_id)["items"]
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
                    "archived": isinstance(item.metadata.get("archived_at"), str),
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
            "attention": attention[:5],
            "more_attention": len(attention) > 5,
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
            "revision": session.revision,
            "archived": isinstance(session.metadata.get("archived_at"), str),
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
            "revision": run.revision,
            "backend": run.backend.value,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "tasks": [
                {"id": task.id, "title": task.title, "status": task.status.value}
                for task in tasks
            ],
        }

    def _approval(self, project_id: str, action_id: Any) -> tuple[Approval, str, str]:
        approval = self.store.get(Approval, _text(action_id, "action_id", 200))
        if approval.engagement_id != project_id:
            raise ValueError(
                "choose a pending approval from this project's attention list"
            )
        if approval.origin == ToolCallOrigin.CHAT and approval.chat_session_id:
            chat = self._chat(project_id, approval.chat_session_id)
            return approval, chat.title, _chat_url(project_id, chat.id)
        if approval.origin == ToolCallOrigin.MISSION:
            run = self._run(project_id, approval.run_id)
            return (
                approval,
                str(run.metadata.get("name") or run.objective)[:300],
                _mission_url(project_id, run.id),
            )
        raise ValueError("approval has no supported chat or mission owner")

    def _question(
        self, project_id: str, action_id: Any
    ) -> tuple[HarnessInteraction, str, str]:
        question = self.store.get(
            HarnessInteraction, _text(action_id, "action_id", 200)
        )
        if question.engagement_id != project_id:
            raise ValueError(
                "choose a pending question from this project's attention list"
            )
        if question.origin == HarnessTurnOrigin.CHAT and question.chat_session_id:
            chat = self._chat(project_id, question.chat_session_id)
            return question, chat.title, _chat_url(project_id, chat.id)
        if question.origin == HarnessTurnOrigin.MISSION and question.run_id:
            run = self._run(project_id, question.run_id)
            return (
                question,
                str(run.metadata.get("name") or run.objective)[:300],
                _mission_url(project_id, run.id),
            )
        raise ValueError("question has no supported chat or mission owner")

    def attention(self, project_id: str) -> dict[str, Any]:
        self._project(project_id)
        items: list[dict[str, Any]] = []
        for approval in self.store.find_entities(
            Approval,
            {"status": ApprovalStatus.PENDING.value},
            engagement_id=project_id,
            newest_first=True,
            limit=100,
        ):
            try:
                _, title, url = self._approval(project_id, approval.id)
            except (NotFoundError, ValueError):
                continue
            items.append(
                {
                    "kind": "approval",
                    "action_id": approval.id,
                    "owner_kind": approval.origin.value,
                    "owner_id": approval.chat_session_id
                    if approval.origin == ToolCallOrigin.CHAT
                    else approval.run_id,
                    "owner": title,
                    "url": url,
                    "summary": str(
                        approval.exact_request.get("tool_name") or "Tool request"
                    )[:200],
                    "risk": approval.risk_class.value,
                    "requested_at": approval.requested_at.isoformat(),
                }
            )
        for question in self.store.find_entities(
            HarnessInteraction,
            {"status": HarnessInteractionStatus.PENDING.value},
            engagement_id=project_id,
            newest_first=True,
            limit=100,
        ):
            try:
                _, title, url = self._question(project_id, question.id)
            except (NotFoundError, ValueError):
                continue
            items.append(
                {
                    "kind": "question",
                    "action_id": question.id,
                    "owner_kind": question.origin.value,
                    "owner_id": question.chat_session_id
                    if question.origin == HarnessTurnOrigin.CHAT
                    else question.run_id,
                    "owner": title,
                    "url": url,
                    "summary": question.prompt[:500],
                    "contains_secret": question.contains_secret,
                    "requested_at": question.created_at.isoformat(),
                }
            )
        items.sort(
            key=lambda item: (item["requested_at"], item["action_id"]), reverse=True
        )
        return {"project_id": project_id, "items": items, "count": len(items)}

    def approval_detail(self, project_id: str, action_id: Any) -> dict[str, Any]:
        approval, title, url = self._approval(project_id, action_id)
        exact = approval.exact_request
        return {
            "action_id": approval.id,
            "revision": approval.revision,
            "status": approval.status.value,
            "owner": title,
            "url": url,
            "tool_name": str(exact.get("tool_name") or "Tool request")[:200],
            "arguments": exact.get("arguments")
            if isinstance(exact.get("arguments"), dict)
            else {},
            "command": exact.get("argv")
            if isinstance(exact.get("argv"), list)
            else None,
            "target": approval.target,
            "risk": approval.risk_class.value,
            "expected_effects": approval.expected_effects,
            "policy_rationale": approval.policy_rationale,
            "requested_by": approval.requested_by,
            "expires_at": approval.expires_at.isoformat()
            if approval.expires_at
            else None,
        }

    async def decide_approval(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        approval, _, url = self._approval(project_id, arguments.get("action_id"))
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError("approval is no longer pending; read it again")
        if approval.revision != arguments.get("expected_revision"):
            raise ValueError(
                "approval changed; read its current revision before deciding"
            )
        decision = arguments.get("decision")
        if decision not in {"approve", "reject", "stop"}:
            raise ValueError("choose approve, reject, or stop")
        reason = arguments.get("reason")
        if reason is not None:
            reason = _text(reason, "reason", 1000)
        await self._request(
            "POST",
            f"/api/v1/approvals/{quote(approval.id, safe='')}/decision",
            {"decision": decision, "reason": reason},
        )
        saved = self.store.get(Approval, approval.id)
        return {
            "action_id": saved.id,
            "status": saved.status.value,
            "decision_recorded": saved.status.value
            in {"approved", "rejected", "cancelled"},
            "url": url,
            "result": "decision saved in Core; the chat or mission may still be running",
        }

    def question_detail(self, project_id: str, action_id: Any) -> dict[str, Any]:
        question, title, url = self._question(project_id, action_id)
        return {
            "action_id": question.id,
            "revision": question.revision,
            "status": question.status.value,
            "owner": title,
            "url": url,
            "prompt": question.prompt,
            "questions": question.questions,
            "response_schema": question.response_schema,
            "contains_secret": question.contains_secret,
        }

    async def answer_question(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        question, _, url = self._question(project_id, arguments.get("action_id"))
        if question.status != HarnessInteractionStatus.PENDING:
            raise ValueError("question is no longer pending; read it again")
        if question.revision != arguments.get("expected_revision"):
            raise ValueError(
                "question changed; read its current revision before responding"
            )
        action = arguments.get("action")
        if action not in {"answer", "decline", "cancel"}:
            raise ValueError("choose answer, decline, or cancel")
        if question.contains_secret and action == "answer":
            raise ValueError(
                "this answer contains a secret; open the chat or mission to enter it privately"
            )
        response = arguments.get("response") or {}
        if not isinstance(response, dict) or (action == "answer" and not response):
            raise ValueError("provide the requested structured response")
        await self._request(
            "POST",
            f"/api/v1/harness-interactions/{quote(question.id, safe='')}/decision",
            {"action": action, "response": response},
        )
        saved = self.store.get(HarnessInteraction, question.id)
        return {
            "action_id": saved.id,
            "status": saved.status.value,
            "response_recorded": saved.status != HarnessInteractionStatus.PENDING,
            "url": url,
            "result": "response delivered to the active harness turn; work may still be running",
        }

    async def update_chat(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._chat(project_id, arguments.get("session_id"))
        if session.revision != arguments.get("expected_revision"):
            raise ValueError("chat changed; read its current revision before updating")
        body: dict[str, Any] = {"expected_revision": session.revision}
        if "title" in arguments:
            body["title"] = _text(arguments["title"], "title", 300)
        if "archived" in arguments:
            if type(arguments["archived"]) is not bool:
                raise ValueError("archived must be true or false")
            body["archived"] = arguments["archived"]
        if len(body) == 1:
            raise ValueError("provide a title or archived choice")
        await self._request(
            "PATCH", f"/api/v1/chat-sessions/{quote(session.id, safe='')}", body
        )
        saved = self._chat(project_id, session.id)
        return {
            "session_id": saved.id,
            "title": saved.title,
            "archived": isinstance(saved.metadata.get("archived_at"), str),
            "revision": saved.revision,
            "url": _chat_url(project_id, saved.id),
        }

    async def retry_mission(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        run = self._run(project_id, arguments.get("run_id"))
        if run.revision != arguments.get("expected_revision"):
            raise ValueError(
                "mission changed; read its current revision before retrying"
            )
        if run.status not in {
            RunStatus.COMPLETE,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }:
            raise ValueError(
                "only a completed, failed, cancelled, or interrupted mission can be retried"
            )
        allow_cloud = arguments.get("allow_cloud_tool_results", False)
        if type(allow_cloud) is not bool:
            raise ValueError("allow_cloud_tool_results must be true or false")
        existing = next(
            (
                item
                for item in self.store.find_entities(
                    AgentRun, {}, engagement_id=project_id, newest_first=True, limit=100
                )
                if item.metadata.get("retry_of_run_id") == run.id
            ),
            None,
        )
        if existing is not None:
            return {
                "run_id": existing.id,
                "retry_of_run_id": run.id,
                "status": existing.status.value,
                "url": _mission_url(project_id, existing.id),
                "result": "an existing retry is already saved in Core",
            }
        created = await self._request(
            "POST",
            f"/api/v1/runs/{quote(run.id, safe='')}/retry",
            {"allow_cloud_tool_results": allow_cloud},
        )
        saved = self._run(project_id, created["id"])
        return {
            "run_id": saved.id,
            "retry_of_run_id": run.id,
            "status": saved.status.value,
            "url": _mission_url(project_id, saved.id),
            "result": "retry started in Core; work may still be running",
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
            provider_profile = self.store.get(ProviderProfile, profile_id)
            choices = provider_profile.model_allowlist
            default = provider_profile.metadata.get("default_model")
            enabled = provider_profile.enabled
        else:
            harness_profile = self.store.get(HarnessProfile, profile_id)
            choices = harness_profile.capabilities.models
            default = harness_profile.default_model
            enabled = harness_profile.enabled
        model = (
            requested_model
            or (default if default in choices else None)
            or (choices[0] if choices else None)
        )
        if not enabled or not isinstance(model, str) or model not in choices:
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
        setting_values = arguments.get("settings") or {}
        if not isinstance(setting_values, dict):
            raise ValueError("settings must contain Assistant settings choices")
        if {"backend", "profile_id", "model"} & setting_values.keys():
            raise ValueError(
                "choose the new chat runtime with backend, profile_id, and model"
            )
        settings = (
            ChatAssistantConfigPatch.model_validate(setting_values)
            if setting_values
            else None
        )
        tools_enabled = setting_values.get(
            "tools_enabled", arguments.get("tools_enabled", False)
        )
        if type(tools_enabled) is not bool:
            raise ValueError("tools_enabled must be true or false")
        harness_session_id = arguments.get("harness_session_id")
        if harness_session_id is not None:
            harness_session_id = _text(harness_session_id, "harness_session_id", 200)
            if backend != "harness":
                raise ValueError("only harness chats can resume an existing session")
        chosen = (
            settings.model_dump(exclude_unset=True, mode="json") if settings else {}
        )
        if backend == "provider":
            request = ChatCompletionRequest(
                backend=ChatBackend.PROVIDER,
                engagement_id=project_id,
                provider_id=profile_id,
                model=model,
                messages=[ChatRequestMessage(role=ChatRole.USER, content=prompt)],
                tools_enabled=tools_enabled,
                mcp_server_ids=chosen.get("mcp_server_ids", []),
                hook_ids=chosen.get("hook_ids", []),
                include_knowledge=chosen.get("include_knowledge", True),
                allow_cloud_knowledge=chosen.get("allow_cloud_knowledge", False),
                allow_subagents=chosen.get("allow_subagents", False),
                allow_agent_messaging=chosen.get("allow_agent_messaging", False),
                max_active_subagents=chosen.get("max_active_subagents"),
                subagent_reasoning_effort=chosen.get("subagent_reasoning_effort"),
                reasoning_effort=chosen.get("reasoning_effort"),
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
                harness_session_id=harness_session_id,
                mcp_server_ids=chosen.get("mcp_server_ids", []),
                include_knowledge=chosen.get("include_knowledge", True),
                allow_cloud_knowledge=chosen.get("allow_cloud_knowledge", False),
                allow_remote_mcp=False,
                allow_agent_messaging=chosen.get("allow_agent_messaging", False),
                harness_mode=chosen.get("harness_mode"),
                harness_reasoning_effort=chosen.get("harness_reasoning_effort"),
                harness_service_tier=chosen.get("harness_service_tier"),
                harness_cyber_access_program=chosen.get("harness_cyber_access_program"),
                provider_subagent={
                    **(
                        {"harness_profile_id": chosen["subagent_harness_profile_id"]}
                        if chosen.get("subagent_harness_profile_id")
                        else {
                            "provider_profile_id": chosen.get(
                                "subagent_provider_id", ""
                            )
                        }
                    ),
                    "model": chosen.get("subagent_model", ""),
                    **(
                        {"max_active": chosen["max_active_subagents"]}
                        if chosen.get("max_active_subagents")
                        else {}
                    ),
                    **(
                        {"reasoning_effort": chosen["subagent_reasoning_effort"]}
                        if chosen.get("subagent_reasoning_effort")
                        else {}
                    ),
                }
                if chosen.get("allow_subagents")
                else None,
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
        next_settings = chat_assistant_config(self.store, session)
        next_backend = ChatBackend(next_settings["backend"])
        next_profile = next_settings["profile_id"]
        sharing_allowed = (
            self.store.get(
                ProviderProfile, next_profile
            ).privacy.auto_share_tool_results
            if next_backend == ChatBackend.PROVIDER
            else self.store.get(
                HarnessProfile, next_profile
            ).privacy.auto_share_tool_results
        )
        request = ChatCompletionRequest(
            backend=next_backend,
            engagement_id=project_id,
            session_id=session.id,
            provider_id=next_profile if next_backend == ChatBackend.PROVIDER else None,
            harness_profile_id=next_profile
            if next_backend == ChatBackend.HARNESS
            else None,
            model=next_settings["model"],
            messages=[ChatRequestMessage(role=ChatRole.USER, content=text)],
            tools_enabled=bool(next_settings["tools_enabled"]),
            mcp_server_ids=next_settings["mcp_server_ids"],
            hook_ids=next_settings["hook_ids"]
            if next_backend == ChatBackend.PROVIDER
            else [],
            include_knowledge=next_settings["include_knowledge"],
            allow_cloud_knowledge=next_settings["allow_cloud_knowledge"],
            allow_cloud_tool_results=sharing_allowed,
            reasoning_effort=next_settings["reasoning_effort"]
            if next_backend == ChatBackend.PROVIDER
            else None,
            harness_reasoning_effort=next_settings["harness_reasoning_effort"]
            if next_backend == ChatBackend.HARNESS
            else None,
            harness_service_tier=next_settings["harness_service_tier"]
            if next_backend == ChatBackend.HARNESS
            else None,
            harness_mode=next_settings["harness_mode"]
            if next_backend == ChatBackend.HARNESS
            else None,
            harness_cyber_access_program=next_settings["harness_cyber_access_program"]
            if next_backend == ChatBackend.HARNESS
            else None,
            allow_subagents=next_settings["allow_subagents"],
            allow_agent_messaging=next_settings["allow_agent_messaging"],
            max_active_subagents=next_settings["max_active_subagents"],
            subagent_provider_id=next_settings["subagent_provider_id"]
            if next_backend == ChatBackend.HARNESS
            else None,
            subagent_harness_profile_id=next_settings["subagent_harness_profile_id"]
            if next_backend == ChatBackend.HARNESS
            else None,
            subagent_model=next_settings["subagent_model"]
            if next_backend == ChatBackend.HARNESS
            else None,
            subagent_reasoning_effort=next_settings["subagent_reasoning_effort"],
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
            detail = response.text or response.reason_phrase
            raise ValueError(
                f"Core rejected the work action ({response.status_code}): {detail[:500]}"
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
        body: dict[str, Any] = {
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
        "nebula.work.chat_settings",
        "Read saved next-turn Assistant settings, their revision, and available choices for a listed chat or session_id=self.",
        CHAT_SETTINGS_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.set_chat_settings",
        "Change the next-turn Assistant settings for one listed chat or session_id=self. First read chat_settings and pass its revision. Running and queued turns keep their submitted settings. Read back the saved result.",
        SET_CHAT_SETTINGS_SCHEMA,
        False,
    ),
    (
        "nebula.work.mission",
        "Read the saved status and tasks for one listed mission.",
        MISSION_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.start_chat",
        "Start a separate Core-owned assistant chat for this project. Use a listed runtime. For provider chats, tools_enabled grants the usual project tools; use only when the operator requested work. Peer messaging starts off. The result means started, not finished.",
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
    (
        "nebula.work.attention",
        "List pending approvals and questions in the selected project's assistant chats and missions. Use this before choosing a request; do not guess IDs.",
        EMPTY_SCHEMA,
        True,
    ),
    (
        "nebula.work.approval",
        "Read the exact pending approval, risk, target, and expected effects before deciding. Use an action_id from nebula.work.attention.",
        ACTION_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.decide_approval",
        "Approve, reject, or stop one exact pending chat or mission request after the operator explicitly directs that decision. Read nebula.work.approval first and pass its revision. A saved approval is not proof that the work finished.",
        DECIDE_APPROVAL_SCHEMA,
        False,
    ),
    (
        "nebula.work.question",
        "Read one pending harness question, its answer choices and revision. Use an action_id from nebula.work.attention.",
        ACTION_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.answer_question",
        "Answer, decline, or cancel one pending harness question only on the operator's explicit instruction. Read nebula.work.question first and pass its revision. Secret answers must use the private chat or mission UI.",
        ANSWER_QUESTION_SCHEMA,
        False,
    ),
    (
        "nebula.work.update_chat",
        "Rename, archive, or restore one listed assistant chat. Read nebula.work.chat first and pass its revision. Active turns must finish or stop before changing title or archive state.",
        UPDATE_CHAT_SCHEMA,
        False,
    ),
    (
        "nebula.work.retry_mission",
        "Retry one terminal mission after reading nebula.work.mission and passing its revision. A matching saved retry is returned instead of starting duplicate work. Remote MCP result transfer requires the operator's explicit consent.",
        RETRY_MISSION_SCHEMA,
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
    if name == "nebula.work.chat_settings":
        return work.chat_settings(project_id, arguments.get("session_id"))
    if name == "nebula.work.set_chat_settings":
        return work.set_chat_settings(project_id, arguments)
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
    if name == "nebula.work.attention":
        return work.attention(project_id)
    if name == "nebula.work.approval":
        return work.approval_detail(project_id, arguments.get("action_id"))
    if name == "nebula.work.decide_approval":
        return await work.decide_approval(project_id, arguments)
    if name == "nebula.work.question":
        return work.question_detail(project_id, arguments.get("action_id"))
    if name == "nebula.work.answer_question":
        return await work.answer_question(project_id, arguments)
    if name == "nebula.work.update_chat":
        return await work.update_chat(project_id, arguments)
    if name == "nebula.work.retry_mission":
        return await work.retry_mission(project_id, arguments)
    raise ValueError("work tool is not available")
