"""Project-bound chat and mission operations for Butler.

This is a project-bound control plane over Core's chat and mission services.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
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
from .chat_goals import (
    ChatGoalService,
    GoalConversationCreate,
    GoalCreate,
    GoalHandoffRequest,
    GoalUpdate,
    GoalWrite,
)
from .chat_queue import ChatQueueService, QueueWrite
from .chat_schedules import ChatScheduleService, ScheduleCreate, ScheduleWrite
from .chat_subagents import is_subagent_session
from .butler_timeline import ButlerTimeline
from .domain import (
    AgentRun,
    Approval,
    ApprovalStatus,
    ChatBackend,
    ChatGoal,
    ChatMessage,
    ChatPlanDirective,
    ChatTaskPlan,
    ChatRole,
    ChatSession,
    ChatSubagent,
    ChatTurn,
    Engagement,
    HarnessProfile,
    HarnessKind,
    HarnessSession,
    HarnessInteraction,
    HarnessInteractionStatus,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    HarnessTurn,
    McpServerProfile,
    OperationEvent,
    ProviderProfile,
    RunBackend,
    RunEvent,
    RunStatus,
    ScopePolicy,
    Task,
    ToolCallOrigin,
)
from .harnesses import HarnessRuntimeService, _session_native_capabilities
from .mcp import usable_mcp_tools
from .native_hooks import discover_native_hooks
from .settings_assistant_tools import model_choices
from .storage import NebulaStore, NotFoundError


EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}
HARNESS_QUOTA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "profile_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "refresh": {"type": "boolean"},
    },
    "required": ["profile_id"],
}
TIMELINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "session_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "agent": {"type": "string", "minLength": 1, "maxLength": 200},
        "hours": {"type": "integer", "minimum": 1, "maximum": 720},
        "limit": {"type": "integer", "minimum": 1, "maximum": 80},
        "cursor": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    "anyOf": [
        {"required": ["session_id"]},
        {"required": ["agent"]},
        {"required": ["cursor"]},
    ],
}
CATALOG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"operation": {"type": "string"}},
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
    "mcp_server_ids": {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 64,
        "description": "Select enabled MCP servers by ID from chat_settings.available.mcp_servers. Their tools are attached on the next message. Preserve existing IDs when adding a server.",
    },
    "hook_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
    "include_knowledge": {"type": "boolean"},
    "allow_cloud_knowledge": {"type": "boolean"},
    "tools_enabled": {
        "type": "boolean",
        "description": "Enable built-in chat tools. This does not select MCP servers; browser-security-testing-tools must be selected with mcp_server_ids when available.",
    },
    "planning_enabled": {
        "type": "boolean",
        "description": "Require a Core-validated task plan before each tool-using turn. The saved next-turn switch defaults off.",
    },
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
REQUIRE_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "session_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "target_kind": {"type": "string", "enum": ["agent", "goal", "turn"]},
        "target_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "expected_revision": {"type": "integer", "minimum": 1},
        "reason": {"type": "string", "minLength": 20, "maxLength": 2000},
    },
    "required": [
        "session_id",
        "target_kind",
        "target_id",
        "expected_revision",
        "reason",
    ],
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
        "delivery": {"type": "string", "enum": ["next", "steer", "immediate"]},
    },
    "required": ["session_id", "text"],
}
AWAIT_CHAT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CHAT_TARGET_SCHEMA["properties"],
        "item_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 25},
    },
    "required": ["session_id", "item_id"],
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
FORK_CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CHAT_TARGET_SCHEMA["properties"],
        "expected_revision": {"type": "integer", "minimum": 1},
        "through_message_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    "required": ["session_id", "expected_revision", "through_message_id"],
}
DELETE_CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CHAT_TARGET_SCHEMA["properties"],
        "expected_revision": {"type": "integer", "minimum": 1},
        "confirm_title": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    "required": ["session_id", "expected_revision", "confirm_title"],
}
RETRY_CHAT_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CHAT_TARGET_SCHEMA["properties"],
        "turn_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "expected_revision": {"type": "integer", "minimum": 1},
    },
    "required": ["session_id", "turn_id", "expected_revision"],
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
PAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"offset": {"type": "integer", "minimum": 0}},
}
GOAL_CREATE_FIELDS: dict[str, Any] = {
    "objective": {"type": "string", "minLength": 1, "maxLength": 20000},
    "completion_criteria": {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": 1,
        "maxItems": 50,
    },
    "plan": {"type": "array", "items": {"type": "string"}, "maxItems": 200},
    "token_budget": {"type": ["integer", "null"], "minimum": 1},
    "time_budget_seconds": {"type": ["integer", "null"], "minimum": 1800},
    "step_budget": {"type": ["integer", "null"], "minimum": 1},
    "child_budget": {"type": ["integer", "null"], "minimum": 0, "maximum": 32},
}
CREATE_GOAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {**CHAT_TARGET_SCHEMA["properties"], **GOAL_CREATE_FIELDS},
    "required": ["session_id", "objective", "completion_criteria"],
}
START_GOAL_CONVERSATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **GOAL_CREATE_FIELDS,
        "backend": {"type": "string", "enum": ["provider", "harness"]},
        "profile_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "model": {"type": "string", "minLength": 1, "maxLength": 500},
        "tools_enabled": {"type": "boolean"},
        "planning_enabled": {"type": "boolean"},
        "mcp_server_ids": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 64,
        },
        "reasoning_effort": {"type": "string"},
        "allow_subagents": {"type": "boolean"},
        "allow_agent_messaging": {"type": "boolean"},
        "max_active_subagents": {"type": "integer", "minimum": 1, "maximum": 100},
        "subagent_reasoning_effort": {"type": "string"},
    },
    "required": ["objective", "completion_criteria"],
}
UPDATE_GOAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CREATE_GOAL_SCHEMA["properties"],
        "expected_revision": {"type": "integer", "minimum": 1},
    },
    "required": ["session_id", "expected_revision"],
}
GOAL_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CHAT_TARGET_SCHEMA["properties"],
        "expected_revision": {"type": "integer", "minimum": 1},
        "action": {
            "type": "string",
            "enum": ["start", "pause", "resume", "cancel", "block", "complete"],
        },
        "reason": {"type": "string", "maxLength": 2000},
        "completion_summary": {"type": "string", "maxLength": 20000},
        "completion_evidence": {
            "type": "array",
            "items": {"type": "object"},
            "maxItems": 200,
        },
    },
    "required": ["session_id", "expected_revision", "action"],
}
HANDOFF_GOAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CHAT_TARGET_SCHEMA["properties"],
        "expected_revision": {"type": "integer", "minimum": 1},
        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
        "checkpoint": {"type": "string", "maxLength": 4000},
        "max_steps": {"type": "integer", "minimum": 1, "maximum": 100},
        "max_time_seconds": {"type": "integer", "minimum": 1800, "maximum": 86400},
    },
    "required": ["session_id", "expected_revision", "reason"],
}
SUBAGENT_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"subagent_id": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["subagent_id"],
}
NATIVE_SUBAGENT_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "turn_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "task_id": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["turn_id", "task_id"],
}
SUBAGENT_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **PAGE_SCHEMA["properties"],
        "parent_session_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "native_turn_offset": {"type": "integer", "minimum": 0},
    },
}
QUEUE_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CHAT_TARGET_SCHEMA["properties"],
        "expected_revision": {"type": "integer", "minimum": 0},
        "action": {
            "type": "string",
            "enum": ["pause", "resume", "remove", "clear", "retry", "reorder", "edit"],
        },
        "item_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "order": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "text": {"type": "string", "minLength": 1, "maxLength": 20000},
    },
    "required": ["session_id", "expected_revision", "action"],
}
MANAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "minLength": 1},
        "arguments": {"type": "object"},
    },
    "required": ["operation", "arguments"],
}
CREATE_GOAL_CHILD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {**CHAT_TARGET_SCHEMA["properties"], **GOAL_CREATE_FIELDS},
    "required": ["session_id", "objective", "completion_criteria"],
}
CREATE_SCHEDULE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CHAT_TARGET_SCHEMA["properties"],
        "interval_seconds": {"type": "integer", "minimum": 3600, "maximum": 2592000},
    },
    "required": ["session_id", "interval_seconds"],
}
SET_SCHEDULE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **CHAT_TARGET_SCHEMA["properties"],
        "expected_revision": {"type": "integer", "minimum": 1},
        "enabled": {"type": "boolean"},
    },
    "required": ["session_id", "expected_revision", "enabled"],
}


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"provide {label} of 1 to {limit} characters")
    return value.strip()


def _chat_url(project_id: str, session_id: str) -> str:
    return f"/projects/{quote(project_id, safe='')}/workbench?view=chat&session={quote(session_id, safe='')}"


def _mission_url(project_id: str, run_id: str) -> str:
    return f"/projects/{quote(project_id, safe='')}/workbench?view=missions&mission={quote(run_id, safe='')}"


def _chat_kind(session: ChatSession) -> str:
    if is_subagent_session(session):
        return "subagent"
    if isinstance(session.metadata.get("delegated_from_goal_id"), str):
        return "goal_child"
    return "main"


class ControlAssistantWork:
    def __init__(
        self,
        store: NebulaStore,
        chat: ChatService,
        queue: ChatQueueService,
        harness: HarnessRuntimeService,
        app: FastAPI,
        token: str,
        quota_observer: Any | None = None,
    ) -> None:
        self.store = store
        self.chat = chat
        self.queue = queue
        self.harness = harness
        self.app = app
        self.token = token
        self.quota_observer = quota_observer
        self.goals = ChatGoalService(store, harness)
        self.schedules = ChatScheduleService(store)
        self.timeline = ButlerTimeline(store)

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

    def _non_subagent_chat(self, project_id: str, session_id: Any) -> ChatSession:
        session = self._chat(project_id, session_id)
        if is_subagent_session(session):
            raise ValueError(
                "this is a delegated subagent; use subagent controls on its parent"
            )
        return session

    def _messageable_chat(self, project_id: str, session_id: Any) -> ChatSession:
        session = self._non_subagent_chat(project_id, session_id)
        if _chat_kind(session) == "goal_child":
            raise ValueError(
                "this is a child goal chat; message its parent main agent instead"
            )
        return session

    def _main_ancestor(
        self, project_id: str, session: ChatSession
    ) -> ChatSession | None:
        current = session
        seen: set[str] = set()
        while _chat_kind(current) != "main":
            if not current.parent_session_id or current.id in seen:
                return None
            seen.add(current.id)
            try:
                current = self._chat(project_id, current.parent_session_id)
            except (
                ValueError
            ):  # diagnostic-expected: stale or private lineage has no message target
                return None
        return current

    def _message_via(self, project_id: str, session: ChatSession) -> dict[str, Any]:
        main = self._main_ancestor(project_id, session)
        return {
            "message_via_session_id": main.id if main else None,
            "message_via_url": _chat_url(project_id, main.id) if main else None,
        }

    def _native_goal(self, session: ChatSession) -> dict[str, Any] | None:
        if session.backend != ChatBackend.HARNESS or not session.harness_session_id:
            return None
        try:
            snapshot = self.harness.session_activity(session.harness_session_id).goal
        except (
            NotFoundError
        ):  # diagnostic-expected: old chat may lack its harness session
            return None
        if snapshot is None:
            return None
        return {
            **snapshot.model_dump(mode="json"),
            "source": "native_harness",
            "session_id": session.id,
            "control_supported": False,
            "control_reason": "The harness owns this goal; Butler can guide or stop its chat turn.",
        }

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
                raise ValueError("Butler has no saved conversation yet")
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
        mcp = []
        for item in self.store.find_entities(McpServerProfile, {}, limit=None):
            if (
                not item.enabled
                or item.capabilities.checked_at is None
                or (
                    item.metadata.get("built_in") == "notebook_v1"
                    and item.metadata.get("engagement_id") != project_id
                )
            ):
                continue
            tools = usable_mcp_tools(
                item,
                bypass_permissions=bool(scope and scope.bypass_permissions),
            )
            mcp.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "description": item.description,
                    "tools": len(tools),
                    "tool_details": [
                        {"name": tool.name, "description": tool.description}
                        for tool in tools[:20]
                    ],
                    "more_tools": max(0, len(tools) - 20),
                }
            )
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
            "task_plans": [
                item.model_dump(mode="json")
                for item in self.store.list_session_entities(
                    ChatTaskPlan, session.id, newest_first=True, limit=10
                )
            ],
            "plan_directives": [
                item.model_dump(mode="json")
                for item in self.store.list_session_entities(
                    ChatPlanDirective, session.id, newest_first=True, limit=10
                )
            ],
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

    def require_plan(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Force planning for one exact target using fresh Core revisions."""

        session = self._chat(project_id, arguments.get("session_id"))
        if isinstance(session.metadata.get("subagent_id"), str):
            raise ValueError(
                "require a plan on the main agent; its subagents receive that plan"
            )
        kind = arguments.get("target_kind")
        target_id = _text(arguments.get("target_id"), "target_id", 200)
        expected = arguments.get("expected_revision")
        reason = _text(arguments.get("reason"), "reason", 2000)
        if len(reason) < 20:
            raise ValueError("record a concrete reason of at least 20 characters")
        target: ChatSession | ChatGoal | ChatSubagent | ChatTurn
        if kind == "agent":
            target = session
            if target_id != session.id:
                raise ValueError("agent target must be the exact session_id")
        elif kind == "goal":
            target = self.store.get(ChatGoal, target_id)
            if target.session_id != session.id:
                raise ValueError("goal does not belong to this chat")
        elif kind == "child":
            target = self._subagent(project_id, target_id)
            if target.child_session_id != session.id:
                raise ValueError("child target must name its child chat")
        elif kind == "turn":
            target = self.store.get(ChatTurn, target_id)
            if target.session_id != session.id:
                raise ValueError("turn does not belong to this chat")
        else:
            raise ValueError("choose an exact agent, goal, child, or turn")
        if type(expected) is not int or target.revision != expected:
            raise ValueError(
                "target changed; read its current revision before forcing a plan"
            )
        active = self.chat.pending_turn(session.id)
        active_harness_in_flight = False
        if (
            active is not None
            and active.backend == ChatBackend.HARNESS
            and active.harness_turn_id
        ):
            harness_turn = self.store.get(HarnessTurn, active.harness_turn_id)
            active_harness_in_flight = harness_turn.status in {
                HarnessTurnStatus.RUNNING,
                HarnessTurnStatus.WAITING_APPROVAL,
            }
        if (
            kind == "turn"
            and active is not None
            and active.id == target_id
            and active_harness_in_flight
        ):
            raise ValueError(
                "an active harness turn cannot accept a new enforced plan; "
                "target its agent or goal for the next turn"
            )
        existing = [
            item
            for item in self.store.list_session_entities(ChatPlanDirective, session.id)
            if item.status == "pending"
            and item.target_kind == kind
            and item.target_id == target_id
        ]
        internal_id = arguments.get("_directive_id")
        if internal_id is not None:
            internal_id = _text(internal_id, "directive id", 200)
            try:
                saved_internal = self.store.get(ChatPlanDirective, internal_id)
            except NotFoundError:
                # diagnostic-expected: no prior directive allows one to be created.
                saved_internal = None
            if saved_internal is not None:
                if (
                    saved_internal.engagement_id != project_id
                    or saved_internal.session_id != session.id
                    or saved_internal.target_kind != kind
                    or saved_internal.target_id != target_id
                ):
                    raise ValueError("directive id is already bound to another target")
                existing = [saved_internal]
        if existing:
            directive = existing[-1]
        else:
            deferred_after_turn_id = (
                active.id if active is not None and active_harness_in_flight else None
            )
            if internal_id is None:
                pending_directive = ChatPlanDirective(
                    engagement_id=project_id,
                    session_id=session.id,
                    target_kind=kind,
                    target_id=target_id,
                    reason=reason,
                    deferred_after_turn_id=deferred_after_turn_id,
                )
            else:
                pending_directive = ChatPlanDirective(
                    id=internal_id,
                    engagement_id=project_id,
                    session_id=session.id,
                    target_kind=kind,
                    target_id=target_id,
                    reason=reason,
                    deferred_after_turn_id=deferred_after_turn_id,
                )
            directive = self.store.create(pending_directive)
        timing = (
            "next_turn"
            if active is not None and active_harness_in_flight
            else "before_turn"
            if active is not None and active.backend == ChatBackend.HARNESS
            else "before_next_tool"
            if active is not None
            else "next_turn"
        )
        return {
            "directive": self.store.get(ChatPlanDirective, directive.id).model_dump(
                mode="json"
            ),
            "timing": timing,
            "session_id": session.id,
            "target_revision": target.revision,
        }

    def _run(self, project_id: str, run_id: Any) -> AgentRun:
        run = self.store.get(AgentRun, _text(run_id, "run_id", 200))
        if run.engagement_id != project_id:
            raise ValueError("choose a mission from this project's work roster")
        return run

    def catalog(self, project_id: str, operation: Any = None) -> dict[str, Any]:
        self._project(project_id)
        if operation is not None:
            target = _text(operation, "operation", 200)
            entry = next((item for item in _ALL_WORK_TOOLS if item[0] == target), None)
            if entry is None:
                raise ValueError("choose an operation from nebula.work.catalog")
            return {
                "name": entry[0],
                "description": entry[1],
                "parameters": entry[2],
                "read_only": entry[3],
            }
        return {
            "project_id": project_id,
            "runtimes": model_choices(self.store),
            "actions": [
                name for name, *_ in _ALL_WORK_TOOLS if name != "nebula.work.catalog"
            ],
            "managed_tool": "nebula.work.manage",
            "scope": "selected project; Assistant settings can attach available browser MCP tools to a chat; direct browser and notes control are not included",
        }

    def overview(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        pending = self.chat.pending_turns(project_id)
        attention = self.attention(project_id)["items"]
        chats = [
            item
            for item in self.store.find_entities(
                ChatSession,
                {"metadata.subagent_id": None, "metadata.delegated_from_goal_id": None},
                engagement_id=project_id,
                newest_first=True,
                limit=80,
            )
            if item.metadata.get("control_assistant") is not True
            and item.metadata.get("side_chat") is not True
            and item.metadata.get("temporary_assistant") is not True
            and _chat_kind(item) == "main"
        ]
        subagents = self.store.find_entities(
            ChatSubagent, {}, engagement_id=project_id, newest_first=True, limit=31
        )
        native_subagents = self._native_subagents_overview(project_id, {})
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
                    "kind": "main",
                    "goal": self._goal_summary(item.id),
                    "native_harness_goal": self._native_goal(item),
                    "archived": isinstance(item.metadata.get("archived_at"), str),
                    "turn_id": pending[item.id].id if item.id in pending else None,
                    "url": _chat_url(project_id, item.id),
                }
                for item in chats[:30]
            ],
            "subagents": [self._subagent_summary(item) for item in subagents[:30]],
            "more_subagents": len(subagents) > 30,
            "native_subagents": native_subagents["native_subagents"],
            "next_native_turn_offset": native_subagents["next_native_turn_offset"],
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

    async def harness_quota(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Read a configured harness's measured account window for agents."""

        self._project(project_id)
        profile = self.store.get(
            HarnessProfile, _text(arguments.get("profile_id"), "profile_id", 200)
        )
        if not profile.enabled:
            raise ValueError(
                "choose an enabled harness profile from the runtime catalog"
            )
        if self.quota_observer is None:
            return {
                "profile_id": profile.id,
                "state": "unavailable",
                "detail": "Quota reads are unavailable in this Core.",
                "snapshots": [],
            }
        read = await self.quota_observer.refresh(
            profile.id, force=arguments.get("refresh") is True
        )
        return {
            "profile_id": profile.id,
            "profile_name": profile.name,
            "harness_kind": profile.kind.value,
            **read.model_dump(mode="json"),
        }

    def chat_detail(self, project_id: str, session_id: Any) -> dict[str, Any]:
        session = self._chat(project_id, session_id)
        messages = self.chat.session_messages(session.id)
        pending = self.chat.pending_turn(session.id)
        queue = self.queue.get(session.id)
        harness_turns = self.store.find_entities(
            HarnessTurn,
            {"chat_session_id": session.id},
            engagement_id=project_id,
            newest_first=True,
            limit=10,
        )
        return {
            "id": session.id,
            "title": session.title,
            "url": _chat_url(project_id, session.id),
            "backend": session.backend.value,
            "model": session.model,
            "kind": _chat_kind(session),
            "parent_session_id": session.parent_session_id,
            "parent_url": (
                _chat_url(project_id, session.parent_session_id)
                if session.parent_session_id
                else None
            ),
            **self._message_via(project_id, session),
            "subagent_id": session.metadata.get("subagent_id"),
            "delegated_from_goal_id": session.metadata.get("delegated_from_goal_id"),
            "goal": self._goal_summary(session.id),
            "native_harness_goal": self._native_goal(session),
            "revision": session.revision,
            "archived": isinstance(session.metadata.get("archived_at"), str),
            "task_plans": [
                item.model_dump(mode="json")
                for item in self.store.list_session_entities(
                    ChatTaskPlan, session.id, newest_first=True, limit=10
                )
            ],
            "plan_directives": [
                item.model_dump(mode="json")
                for item in self.store.list_session_entities(
                    ChatPlanDirective, session.id, newest_first=True, limit=10
                )
            ],
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
                {
                    "id": item.id,
                    "role": item.role.value,
                    "content": item.content[:2000],
                }
                for item in messages[-8:]
                if item.role.value in {"user", "assistant"}
            ],
            "recent_harness_turns": [
                {
                    "id": item.id,
                    "status": item.status.value,
                    "revision": item.revision,
                    "retry_of_turn_id": item.metadata.get("retry_of_turn_id"),
                }
                for item in harness_turns
            ],
        }

    def _goal_summary(self, session_id: str) -> dict[str, Any] | None:
        try:
            goal = self.goals.read(session_id)
        except NotFoundError:  # diagnostic-expected: most chats have no goal
            return None
        return {
            "id": goal.id,
            "status": goal.status.value,
            "objective": goal.objective[:500],
            "current_step": goal.current_step,
            "plan_steps": len(goal.plan),
            "blocked_reason": goal.blocked_reason,
            "revision": goal.revision,
        }

    def goals_overview(self, project_id: str, offset: Any = 0) -> dict[str, Any]:
        self._project(project_id)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        goals = self.store.find_entities(
            ChatGoal,
            {},
            engagement_id=project_id,
            newest_first=True,
            offset=offset,
            limit=31,
        )
        items = []
        for goal in goals[:30]:
            try:
                session = self._chat(project_id, goal.session_id)
            except (
                ValueError
            ):  # diagnostic-expected: private assistant sessions are not work targets
                continue
            items.append(
                {
                    "session_id": session.id,
                    "title": session.title,
                    "kind": _chat_kind(session),
                    "parent_session_id": session.parent_session_id,
                    "parent_url": (
                        _chat_url(project_id, session.parent_session_id)
                        if session.parent_session_id
                        else None
                    ),
                    **self._message_via(project_id, session),
                    "url": _chat_url(project_id, session.id),
                    **(self._goal_summary(session.id) or {}),
                }
            )
        harness_chats = self.store.find_entities(
            ChatSession,
            {"backend": ChatBackend.HARNESS},
            engagement_id=project_id,
            newest_first=True,
            offset=offset,
            limit=31,
        )
        native_goals = []
        for session in harness_chats[:30]:
            try:
                self._chat(project_id, session.id)
            except (
                ValueError
            ):  # diagnostic-expected: private chats are not work targets
                continue
            snapshot = self._native_goal(session)
            if snapshot is not None:
                native_goals.append(
                    {
                        "title": session.title,
                        "kind": _chat_kind(session),
                        "parent_session_id": session.parent_session_id,
                        **self._message_via(project_id, session),
                        "url": _chat_url(project_id, session.id),
                        **snapshot,
                    }
                )
        return {
            "project_id": project_id,
            "goals": items,
            "next_offset": offset + 30 if len(goals) > 30 else None,
            "native_harness_goals": native_goals,
            "next_native_offset": offset + 30 if len(harness_chats) > 30 else None,
        }

    def goal_detail(self, project_id: str, session_id: Any) -> dict[str, Any]:
        session = self._chat(project_id, session_id)
        try:
            goal = self.goals.read(session.id)
        except NotFoundError:
            snapshot = self._native_goal(session)
            if snapshot is None:
                raise
            return {
                "session_id": session.id,
                "title": session.title,
                "kind": _chat_kind(session),
                "parent_session_id": session.parent_session_id,
                "parent_url": (
                    _chat_url(project_id, session.parent_session_id)
                    if session.parent_session_id
                    else None
                ),
                **self._message_via(project_id, session),
                "url": _chat_url(project_id, session.id),
                "goal": None,
                "native_harness_goal": snapshot,
                "children": [],
            }
        children = self.goals.list_children(session.id)
        return {
            "session_id": session.id,
            "title": session.title,
            "kind": _chat_kind(session),
            "parent_session_id": session.parent_session_id,
            "parent_url": (
                _chat_url(project_id, session.parent_session_id)
                if session.parent_session_id
                else None
            ),
            **self._message_via(project_id, session),
            "url": _chat_url(project_id, session.id),
            "goal": goal.model_dump(mode="json"),
            "native_harness_goal": self._native_goal(session),
            "children": [
                {
                    "session_id": item.session_id,
                    "objective": item.objective[:500],
                    "status": item.status.value,
                    "current_step": item.current_step,
                    "revision": item.revision,
                    "url": _chat_url(project_id, item.session_id),
                }
                for item in children
            ],
        }

    async def create_goal(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._chat(project_id, arguments.get("session_id"))
        body = GoalCreate.model_validate(
            {key: value for key, value in arguments.items() if key != "session_id"}
        )
        await self._request(
            "POST",
            f"/api/v1/chat/sessions/{quote(session.id, safe='')}/goal",
            body.model_dump(mode="json"),
        )
        return self.goal_detail(project_id, session.id)

    async def create_goal_conversation(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self._project(project_id)
        backend, profile_id, model = self._runtime(arguments)
        fields = {
            key: value
            for key, value in arguments.items()
            if key in GoalCreate.model_fields
        }
        goal = GoalCreate.model_validate(fields)
        enabled = arguments.get("tools_enabled", False)
        if type(enabled) is not bool:
            raise ValueError("tools_enabled must be true or false")
        body = GoalConversationCreate(
            engagement_id=project_id,
            backend=ChatBackend(backend),
            provider_id=profile_id if backend == "provider" else None,
            harness_profile_id=profile_id if backend == "harness" else None,
            model=model,
            tools_enabled=enabled,
            **{
                key: value
                for key, value in arguments.items()
                if key
                in {
                    "planning_enabled",
                    "mcp_server_ids",
                    "reasoning_effort",
                    "allow_subagents",
                    "allow_agent_messaging",
                    "max_active_subagents",
                    "subagent_reasoning_effort",
                    "harness_reasoning_effort",
                    "harness_service_tier",
                    "harness_cyber_access_program",
                    "butler_recovery_case_id",
                    "butler_recovery_source_goal_id",
                }
            },
            **goal.model_dump(),
        )
        result = await self._request(
            "POST", "/api/v1/chat/goal-conversations", body.model_dump(mode="json")
        )
        return self.goal_detail(project_id, result["session"]["id"])

    async def update_goal(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._chat(project_id, arguments.get("session_id"))
        current = self.goals.get(session.id)
        expected = arguments.get("expected_revision")
        if type(expected) is not int or expected != current.revision:
            raise ValueError("goal changed; read its current revision before editing")
        fields = set(GoalCreate.model_fields)
        if not fields.intersection(arguments):
            raise ValueError("provide at least one goal field to change")
        body = GoalUpdate.model_validate(
            {
                **{key: getattr(current, key) for key in fields},
                **{key: value for key, value in arguments.items() if key in fields},
                "expected_revision": expected,
            }
        )
        await self._request(
            "PATCH",
            f"/api/v1/chat/sessions/{quote(session.id, safe='')}/goal",
            body.model_dump(mode="json"),
        )
        return self.goal_detail(project_id, session.id)

    async def goal_action(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._chat(project_id, arguments.get("session_id"))
        current = self.goals.get(session.id)
        expected = arguments.get("expected_revision")
        if type(expected) is not int or expected != current.revision:
            raise ValueError("goal changed; read its current revision before acting")
        body = GoalWrite.model_validate(
            {key: value for key, value in arguments.items() if key != "session_id"}
        )
        await self._request(
            "POST",
            f"/api/v1/chat/sessions/{quote(session.id, safe='')}/goal/actions",
            body.model_dump(mode="json"),
        )
        return self.goal_detail(project_id, session.id)

    async def handoff_goal(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        source = self._chat(project_id, arguments.get("session_id"))
        body = GoalHandoffRequest.model_validate(
            {key: value for key, value in arguments.items() if key != "session_id"}
        )
        result = await self._request(
            "POST",
            f"/api/v1/chat/sessions/{quote(source.id, safe='')}/goal/handoff",
            body.model_dump(mode="json"),
        )
        target = self.goal_detail(project_id, result["session"]["id"])
        return {
            **target,
            "source_goal_id": result["source_goal"]["id"],
            "source_session_id": source.id,
            "already_exists": result["already_exists"],
        }

    async def create_goal_child(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._chat(project_id, arguments.get("session_id"))
        parent = self.goals.get(session.id)
        if parent.status.value != "running":
            raise ValueError("start the parent goal before creating child work")
        body = GoalCreate.model_validate(
            {key: value for key, value in arguments.items() if key != "session_id"}
        )
        created = await self._request(
            "POST",
            f"/api/v1/chat/sessions/{quote(session.id, safe='')}/goal/children",
            body.model_dump(mode="json"),
        )
        return self.goal_detail(project_id, created["session_id"])

    def schedule_detail(self, project_id: str, session_id: Any) -> dict[str, Any]:
        session = self._chat(project_id, session_id)
        try:
            schedule = self.schedules.get(session.id)
        except NotFoundError:  # diagnostic-expected: recurring work is optional
            schedule = None
        return {
            "session_id": session.id,
            "title": session.title,
            "url": _chat_url(project_id, session.id),
            "schedule": schedule.model_dump(mode="json") if schedule else None,
            "can_schedule": session.backend == ChatBackend.PROVIDER,
        }

    async def create_schedule(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._messageable_chat(project_id, arguments.get("session_id"))
        body = ScheduleCreate.model_validate(
            {"interval_seconds": arguments.get("interval_seconds")}
        )
        await self._request(
            "POST",
            f"/api/v1/chat/sessions/{quote(session.id, safe='')}/schedule",
            body.model_dump(mode="json"),
        )
        return self.schedule_detail(project_id, session.id)

    async def set_schedule(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._messageable_chat(project_id, arguments.get("session_id"))
        current = self.schedules.get(session.id)
        expected = arguments.get("expected_revision")
        if type(expected) is not int or expected != current.revision:
            raise ValueError(
                "schedule changed; read its current revision before acting"
            )
        body = ScheduleWrite.model_validate(
            {
                "expected_revision": expected,
                "enabled": arguments.get("enabled"),
            }
        )
        await self._request(
            "POST",
            f"/api/v1/chat/sessions/{quote(session.id, safe='')}/schedule/actions",
            body.model_dump(mode="json"),
        )
        return self.schedule_detail(project_id, session.id)

    def _subagent(self, project_id: str, subagent_id: Any) -> ChatSubagent:
        record = self.store.get(ChatSubagent, _text(subagent_id, "subagent_id", 200))
        if record.engagement_id != project_id:
            raise ValueError("choose a subagent from this project's work roster")
        self._chat(project_id, record.parent_session_id)
        return record

    def _subagent_summary(self, record: ChatSubagent) -> dict[str, Any]:
        view = self.chat.subagents.view(record)
        parent = self.store.get(ChatSession, record.parent_session_id)
        return {
            "id": record.id,
            "source": "delegated_chat",
            "name": record.name,
            "task": record.task[:500],
            "status": view["status"],
            "parent_session_id": record.parent_session_id,
            "parent_title": parent.title,
            "parent_kind": _chat_kind(parent),
            **self._message_via(record.engagement_id, parent),
            "child_session_id": record.child_session_id,
            "url": _chat_url(record.engagement_id, record.child_session_id),
            "revision": record.revision,
        }

    def _native_turn(self, project_id: str, turn_id: Any) -> HarnessTurn:
        turn = self.store.get(HarnessTurn, _text(turn_id, "turn_id", 200))
        if turn.engagement_id != project_id:
            raise ValueError("choose a native subagent from this project's work roster")
        if turn.origin == HarnessTurnOrigin.CHAT and turn.chat_session_id:
            self._chat(project_id, turn.chat_session_id)
        elif turn.origin == HarnessTurnOrigin.MISSION and turn.run_id:
            self._run(project_id, turn.run_id)
        else:
            raise ValueError("choose a chat or mission native subagent")
        return turn

    def _native_subagent_items(self, turn: HarnessTurn) -> list[dict[str, Any]]:
        """Fold only durable native task events; provider subagents have their own rows."""

        latest: dict[str, dict[str, Any]] = {}
        cursor = 0
        while True:
            rows: Sequence[OperationEvent | RunEvent]
            if turn.origin == HarnessTurnOrigin.CHAT:
                rows = self.store.replay_operation_events(
                    turn.id,
                    after_sequence=cursor,
                    limit=1000,
                    event_types=(
                        "harness.item_upsert",
                        "harness.item_started",
                        "harness.item_completed",
                    ),
                    payload_values={"item_kind": ("subagent",)},
                )
            else:
                rows = self.store.replay_events(
                    turn.run_id or "",
                    after_sequence=cursor,
                    limit=1000,
                    event_type_prefix="harness.",
                    payload_values={
                        "harness_turn_id": (turn.id,),
                        "item_kind": ("subagent",),
                    },
                )
            if not rows:
                break
            for row in rows:
                payload = row.payload if isinstance(row.payload, dict) else {}
                task_id = payload.get("item_id")
                if not isinstance(task_id, str) or not task_id:
                    continue
                latest[task_id] = payload
            cursor = rows[-1].sequence
            if len(rows) < 1000:
                break
        return [
            self._native_subagent_summary(turn, task_id, payload)
            for task_id, payload in latest.items()
        ]

    def _native_subagent_summary(
        self, turn: HarnessTurn, task_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        session = self.store.get(HarnessSession, turn.harness_session_id)
        profile = self.store.get(HarnessProfile, session.harness_profile_id)
        activity = self.harness.session_activity(session.id)
        status = str(payload.get("item_status") or "unknown")
        can_stop = bool(
            profile.kind == HarnessKind.CLAUDE_AGENT_SDK
            and _session_native_capabilities(session, profile).subagents
            and activity.live
            and activity.turn_id == turn.id
            and status
            in {"queued", "running", "streaming", "waiting_approval", "waiting_input"}
        )
        parent = (
            self.store.get(ChatSession, turn.chat_session_id)
            if turn.chat_session_id
            else None
        )
        title = str(payload.get("title") or "Native subagent")[:500]
        return {
            "id": f"native:{turn.id}:{task_id}",
            "source": "native_harness",
            "turn_id": turn.id,
            "task_id": task_id,
            "name": title,
            "task": str(payload.get("summary") or title)[:500],
            "status": status,
            "parent_session_id": parent.id if parent else None,
            "parent_title": parent.title if parent else None,
            "parent_kind": _chat_kind(parent) if parent else "mission",
            **(
                self._message_via(turn.engagement_id, parent)
                if parent
                else {"message_via_session_id": None, "message_via_url": None}
            ),
            "run_id": turn.run_id,
            "url": (
                _chat_url(turn.engagement_id, parent.id)
                if parent
                else _mission_url(turn.engagement_id, turn.run_id or "")
            ),
            "can_stop": can_stop,
            "can_message": False,
        }

    def _native_subagents_overview(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        native_offset = arguments.get("native_turn_offset", 0)
        if type(native_offset) is not int or native_offset < 0:
            raise ValueError("native_turn_offset must be a nonnegative integer")
        parent_id = arguments.get("parent_session_id")
        filters: dict[str, Any] = {}
        if parent_id is not None:
            parent = self._chat(project_id, parent_id)
            filters["chat_session_id"] = parent.id
        turns = self.store.find_entities(
            HarnessTurn,
            filters,
            engagement_id=project_id,
            newest_first=True,
            offset=native_offset,
            limit=31,
        )
        items: list[dict[str, Any]] = []
        for turn in turns[:30]:
            try:
                self._native_turn(project_id, turn.id)
            except ValueError:  # diagnostic-expected: private or unrelated turn
                continue
            items.extend(self._native_subagent_items(turn))
        return {
            "native_subagents": items,
            "next_native_turn_offset": native_offset + 30 if len(turns) > 30 else None,
        }

    def subagents_overview(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self._project(project_id)
        offset = arguments.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        parent_id = arguments.get("parent_session_id")
        filters = {}
        if parent_id is not None:
            parent = self._chat(project_id, parent_id)
            filters["parent_session_id"] = parent.id
        records = self.store.find_entities(
            ChatSubagent,
            filters,
            engagement_id=project_id,
            newest_first=True,
            offset=offset,
            limit=31,
        )
        native = self._native_subagents_overview(project_id, arguments)
        return {
            "project_id": project_id,
            "parent_session_id": parent_id,
            "subagents": [self._subagent_summary(item) for item in records[:30]],
            "next_offset": offset + 30 if len(records) > 30 else None,
            **native,
        }

    def subagent_detail(self, project_id: str, subagent_id: Any) -> dict[str, Any]:
        record = self._subagent(project_id, subagent_id)
        return {
            **self._subagent_summary(record),
            "view": self.chat.subagents.view(record),
            "parent_url": _chat_url(project_id, record.parent_session_id),
        }

    def native_subagent_detail(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        turn = self._native_turn(project_id, arguments.get("turn_id"))
        task_id = _text(arguments.get("task_id"), "task_id", 500)
        item = next(
            (
                row
                for row in self._native_subagent_items(turn)
                if row["task_id"] == task_id
            ),
            None,
        )
        if item is None:
            raise ValueError("choose a native subagent from this project's work roster")
        return item

    async def stop_native_subagent(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        item = self.native_subagent_detail(project_id, arguments)
        if not item["can_stop"]:
            raise ValueError("this native harness subagent cannot be stopped now")
        await self._request(
            "POST",
            f"/api/v1/harness-turns/{quote(item['turn_id'], safe='')}/tasks/{quote(item['task_id'], safe='')}/stop",
            {},
        )
        return self.native_subagent_detail(project_id, arguments)

    async def stop_subagent(self, project_id: str, subagent_id: Any) -> dict[str, Any]:
        record = self._subagent(project_id, subagent_id)
        await self._request(
            "POST",
            f"/api/v1/chat/sessions/{quote(record.parent_session_id, safe='')}/subagents/{quote(record.id, safe='')}/stop",
            {},
        )
        return self.subagent_detail(project_id, record.id)

    async def stop_subagents(
        self, project_id: str, parent_session_id: Any
    ) -> dict[str, Any]:
        parent = self._chat(project_id, parent_session_id)
        await self._request(
            "POST",
            f"/api/v1/chat/sessions/{quote(parent.id, safe='')}/subagents/stop",
            {},
        )
        return self.subagents_overview(project_id, {"parent_session_id": parent.id})

    def queue_detail(self, project_id: str, session_id: Any) -> dict[str, Any]:
        session = self._messageable_chat(project_id, session_id)
        queue = self.queue.get(session.id)
        return {
            "session_id": session.id,
            "url": _chat_url(project_id, session.id),
            "revision": queue.revision if self.queue.exists(session.id) else 0,
            "paused": queue.paused,
            "items": [
                {
                    "id": item.get("id"),
                    "status": item.get("status"),
                    "text": str(
                        (
                            (
                                item.get("request")
                                or item.get("original_request")
                                or {}
                            ).get("messages")
                            or [{}]
                        )[0].get("content")
                        or ""
                    )[:2000],
                    "detail": item.get("detail"),
                    "turn_id": item.get("turn_id"),
                }
                for item in queue.items
                if item.get("status") not in {"complete", "cancelled"}
            ][:20],
        }

    def queue_action(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._messageable_chat(project_id, arguments.get("session_id"))
        current = self.queue.get(session.id)
        revision = current.revision if self.queue.exists(session.id) else 0
        expected = arguments.get("expected_revision")
        if type(expected) is not int or expected != revision:
            raise ValueError("queue changed; read its current revision before acting")
        action = arguments.get("action")
        if action not in {
            "pause",
            "resume",
            "remove",
            "clear",
            "retry",
            "reorder",
            "edit",
        }:
            raise ValueError("choose a supported queue action")
        item_id = arguments.get("item_id")
        if action in {"remove", "retry", "edit"}:
            item_id = _text(item_id, "item_id", 200)
        request = None
        if action == "edit":
            text = _text(arguments.get("text"), "text", 20000)
            item = next(
                (row for row in current.items if row.get("id") == item_id), None
            )
            if item is None:
                raise ValueError("choose a queued item from nebula.work.queue")
            original = item.get("request") or item.get("original_request")
            if not isinstance(original, dict):
                raise ValueError("this queued item cannot be edited")
            prior = ChatCompletionRequest.model_validate(original)
            if len(prior.messages) != 1 or prior.messages[0].role != ChatRole.USER:
                raise ValueError("this queued item cannot be edited")
            request = prior.model_copy(
                update={
                    "messages": [prior.messages[0].model_copy(update={"content": text})]
                }
            )
        order = arguments.get("order", [])
        if not isinstance(order, list):
            raise ValueError("order must contain queued item IDs")
        self.queue.write(
            session.id,
            QueueWrite(
                expected_revision=revision,
                action=action,
                item_id=item_id,
                order=order,
                request=request,
            ),
        )
        return self.queue_detail(project_id, session.id)

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
                # diagnostic-expected: a pending item can become stale or leave this project between list and lookup.
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
                # diagnostic-expected: a pending item can become stale or leave this project between list and lookup.
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

    async def fork_chat(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        source = self._chat(project_id, arguments.get("session_id"))
        if source.revision != arguments.get("expected_revision"):
            raise ValueError("chat changed; read its current revision before forking")
        message_id = _text(
            arguments.get("through_message_id"), "through_message_id", 200
        )
        if not any(
            item.id == message_id for item in self.chat.session_messages(source.id)
        ):
            raise ValueError("choose a message ID from this chat's saved transcript")
        title = arguments.get("title")
        created = await self._request(
            "POST",
            f"/api/v1/chat/sessions/{quote(source.id, safe='')}/fork",
            {
                "through_message_id": message_id,
                **({"title": _text(title, "title", 300)} if title is not None else {}),
            },
        )
        fork = self._chat(project_id, created["id"])
        return {
            "session_id": fork.id,
            "parent_session_id": source.id,
            "forked_from_message_id": message_id,
            "title": fork.title,
            "revision": fork.revision,
            "url": _chat_url(project_id, fork.id),
        }

    async def delete_chat(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._chat(project_id, arguments.get("session_id"))
        if session.revision != arguments.get("expected_revision"):
            raise ValueError("chat changed; read its current revision before deleting")
        if session.title != arguments.get("confirm_title"):
            raise ValueError("chat title changed; read the exact chat before deleting")
        await self._request(
            "DELETE",
            f"/api/v1/chat-sessions/{quote(session.id, safe='')}",
            {},
            headers={"If-Match": str(session.revision)},
        )
        return {
            "session_id": session.id,
            "title": session.title,
            "deleted": True,
            "result": "chat deleted from Core",
        }

    async def retry_chat_turn(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._messageable_chat(project_id, arguments.get("session_id"))
        turn = self.store.get(
            HarnessTurn, _text(arguments.get("turn_id"), "turn_id", 200)
        )
        if (
            turn.engagement_id != project_id
            or turn.chat_session_id != session.id
            or turn.origin != HarnessTurnOrigin.CHAT
        ):
            raise ValueError("choose a harness turn from this project chat")
        if turn.revision != arguments.get("expected_revision"):
            raise ValueError("chat turn changed; read the chat before retrying")
        if turn.status not in {
            HarnessTurnStatus.FAILED,
            HarnessTurnStatus.CANCELLED,
            HarnessTurnStatus.INTERRUPTED,
        }:
            raise ValueError(
                "only failed, cancelled, or interrupted chat turns can be retried"
            )
        existing = self.store.find_entities(
            HarnessTurn,
            {"metadata.retry_of_turn_id": turn.id},
            engagement_id=project_id,
            newest_first=True,
            limit=1,
        )
        if existing:
            replacement = existing[0]
            result = "an existing linked retry is already saved in Core"
        else:
            created = await self._request(
                "POST",
                f"/api/v1/harness-turns/{quote(turn.id, safe='')}/retry",
                {},
            )
            replacement = self.store.get(HarnessTurn, created["id"])
            result = "linked retry started in Core; work may still be running"
        return {
            "session_id": session.id,
            "turn_id": replacement.id,
            "retry_of_turn_id": turn.id,
            "status": replacement.status.value,
            "url": _chat_url(project_id, session.id),
            "result": result,
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
                planning_enabled=chosen.get("planning_enabled", False),
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
                planning_enabled=chosen.get("planning_enabled", False),
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
        self,
        project_id: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        session = self._messageable_chat(project_id, arguments.get("session_id"))
        text = _text(arguments.get("text"), "text", 20000)
        delivery = arguments.get("delivery", "next")
        if delivery in {"steer", "immediate"}:
            pending = self.chat.pending_turn(session.id)
            if (
                pending is not None
                and session.backend == ChatBackend.HARNESS
                and pending.harness_turn_id
            ):
                if delivery == "immediate":
                    await self.harness.steer_turn(
                        pending.harness_turn_id,
                        text,
                        actor_id="control-assistant",
                        title="Butler guidance",
                    )
                else:
                    await self.harness.steer_turn(
                        pending.harness_turn_id, text, actor_id="control-assistant"
                    )
                return {
                    "session_id": session.id,
                    "delivery": "steered",
                    "turn_id": pending.id,
                }
            if delivery == "steer":
                raise ValueError(
                    "live steering requires an active harness chat; use delivery=next to queue a follow-up"
                )
            if pending is not None:
                return {
                    "session_id": session.id,
                    "delivery": "held",
                    "reason": "active_turn_cannot_steer",
                }
        if delivery not in {"next", "immediate"}:
            raise ValueError("choose next, steer, or immediate delivery")
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
            planning_enabled=bool(next_settings["planning_enabled"]),
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
        if delivery == "immediate":
            # This path deliberately bypasses the follow-up queue. Paused legacy
            # queue entries stay visible for the operator to review.
            if next_backend == ChatBackend.PROVIDER:
                prepared = await self.chat.prepare_async(request)
                if prepared.turn is None:
                    raise RuntimeError("Core did not prepare a durable chat turn")
                self.chat.start_provider_turn(prepared)
                return {
                    "session_id": session.id,
                    "delivery": "started",
                    "turn_id": prepared.turn.id,
                }
            _, turn, harness_turn = self.harness.prepare_chat(
                engagement_id=project_id,
                profile_id=next_profile,
                model=next_settings["model"],
                prompt=text,
                chat_session_id=session.id,
                harness_session_id=session.harness_session_id,
                mcp_server_ids=[],
                include_knowledge=next_settings["include_knowledge"],
                allow_cloud_knowledge=next_settings["allow_cloud_knowledge"],
                allow_remote_mcp=False,
                allow_agent_messaging=next_settings["allow_agent_messaging"],
                harness_mode=next_settings["harness_mode"],
                harness_reasoning_effort=next_settings["harness_reasoning_effort"],
                harness_service_tier=next_settings["harness_service_tier"],
                harness_cyber_access_program=next_settings[
                    "harness_cyber_access_program"
                ],
            )
            self.harness.start_chat_turn(harness_turn.id)
            return {"session_id": session.id, "delivery": "started", "turn_id": turn.id}
        queue = self.queue.get(session.id)
        # Proactive Butler actions reuse a stable private key on recovery. The
        # ordinary operator-facing capability still gets a fresh key per call.
        key = (
            _text(idempotency_key, "idempotency_key", 200)
            if idempotency_key is not None
            else f"control:{uuid4()}"
        )
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

    async def await_chat_result(
        self, project_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Wait briefly for one queued follow-up and return its linked saved reply."""

        session = self._messageable_chat(project_id, arguments.get("session_id"))
        item_id = _text(arguments.get("item_id"), "item_id", 200)
        timeout = arguments.get("timeout_seconds", 20)
        if type(timeout) is not int or not 0 <= timeout <= 25:
            raise ValueError("timeout_seconds must be between 0 and 25")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            queue = self.queue.get(session.id)
            item = next((row for row in queue.items if row.get("id") == item_id), None)
            if item is None:
                raise ValueError(
                    "follow-up is no longer in the recent queue; read the chat before reporting its outcome"
                )
            status = str(item.get("status") or "unknown")
            result = {
                "session_id": session.id,
                "item_id": item_id,
                "queue_status": status,
                "turn_id": item.get("turn_id"),
                "harness_turn_id": item.get("harness_turn_id"),
                "detail": item.get("detail"),
                "url": _chat_url(project_id, session.id),
            }
            if status in {
                "complete",
                "cancelled",
                "needs_review",
                "failed",
                "interrupted",
            }:
                turn_id = item.get("turn_id")
                harness_turn_id = item.get("harness_turn_id")
                if isinstance(turn_id, str) or isinstance(harness_turn_id, str):
                    messages = self.store.find_entities(
                        ChatMessage,
                        {"session_id": session.id},
                        engagement_id=project_id,
                        newest_first=True,
                        limit=100,
                    )
                    answer = next(
                        (
                            message
                            for message in messages
                            if message.role == ChatRole.ASSISTANT
                            and (
                                (
                                    isinstance(turn_id, str)
                                    and message.metadata.get("chat_turn_id") == turn_id
                                )
                                or (
                                    isinstance(harness_turn_id, str)
                                    and message.metadata.get("harness_turn_id")
                                    == harness_turn_id
                                )
                            )
                        ),
                        None,
                    )
                    if answer is not None:
                        result["saved_reply"] = answer.content[:4000]
                result["result"] = (
                    "saved reply available"
                    if result.get("saved_reply") and status == "complete"
                    else "follow-up needs review in the chat"
                    if status in {"needs_review", "failed", "interrupted", "cancelled"}
                    else "no saved reply for this follow-up; inspect the chat"
                )
                return result
            if queue.paused or loop.time() >= deadline:
                return {
                    **result,
                    "result": "queue paused" if queue.paused else "still in progress",
                }
            await asyncio.sleep(min(0.5, max(0.0, deadline - loop.time())))

    async def stop_chat(self, project_id: str, session_id: Any) -> dict[str, Any]:
        session = self._non_subagent_chat(project_id, session_id)
        pending = self.chat.pending_turn(session.id)
        if pending is None:
            return {"session_id": session.id, "status": "idle", "stopped": False}
        if pending.harness_turn_id:
            await self.harness.cancel_turn(
                pending.harness_turn_id, reason="Stopped through Butler"
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
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
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
                headers={
                    "Authorization": f"Bearer {self.token}",
                    **(headers or {}),
                },
            )
        if response.is_error:
            detail = response.text or response.reason_phrase
            raise ValueError(
                f"Core rejected the work action ({response.status_code}): {detail[:500]}"
            )
        return response.json() if response.content else {}

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
            {"reason": "Stopped through Butler"},
        )
        observed = self._run(project_id, run.id)
        return {
            "run_id": run.id,
            "status": observed.status.value,
            "stopped": observed.status.value in {"cancelled", "complete"},
        }


_ALL_WORK_TOOLS: tuple[tuple[str, str, dict[str, Any], bool], ...] = (
    (
        "nebula.work.catalog",
        "List project controls and runtimes, including chat settings, MCP capability selection, follow-up delivery, and result checks; pass operation for one exact action schema.",
        CATALOG_SCHEMA,
        True,
    ),
    (
        "nebula.work.overview",
        "List this project's main chats, delegated subagents, and missions with current status and IDs. Call before choosing a target.",
        EMPTY_SCHEMA,
        True,
    ),
    (
        "nebula.work.harness_quota",
        "Read one enabled harness's vendor-reported included quota windows and separate credit balances by profile ID from the runtime catalog. Grok and Codex credits may remain usable after included usage is exhausted; unavailable is unknown, not zero. Set refresh true for a new vendor read.",
        HARNESS_QUOTA_SCHEMA,
        True,
    ),
    (
        "nebula.work.chat",
        "Read recent messages and pending activity for one listed assistant chat. After a follow-up, inspect this and the queue before reporting what the agent achieved.",
        CHAT_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.timeline",
        "Read a named Assistant and its delegated agents over the last hours from saved Core evidence. Accept session_id from overview or agent title; ambiguous names return choices. Page with next_cursor until has_more is false before a thorough brief. Report observed actions, repeated attempts, blockers, evidence and next steps; treat agent reports as claims and quiet gaps as unknown, not proof of wasted time.",
        TIMELINE_SCHEMA,
        True,
    ),
    (
        "nebula.work.goals",
        "List Core-owned conversation goals and separately labelled read-only native harness goal snapshots in this project. Use offset to page older goals.",
        PAGE_SCHEMA,
        True,
    ),
    (
        "nebula.work.goal",
        "Read one listed chat's Core-owned goal and any separate native harness goal snapshot. Native snapshots have no Butler goal_action control. Read before changing a Core goal.",
        CHAT_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.create_goal",
        "Create a draft goal in a listed chat after the operator requests one. Read chat first. Starting it is a separate goal_action.",
        CREATE_GOAL_SCHEMA,
        False,
    ),
    (
        "nebula.work.create_goal_conversation",
        "Create a new main assistant chat with a draft goal in the selected project. Choose a listed runtime. Call goal_action to start the saved draft; creation alone does not start work.",
        START_GOAL_CONVERSATION_SCHEMA,
        False,
    ),
    (
        "nebula.work.update_goal",
        "Edit objective, criteria, plan, or limits on a listed nonterminal goal after explicit operator instruction. Read goal first and pass its revision; omitted fields stay unchanged.",
        UPDATE_GOAL_SCHEMA,
        False,
    ),
    (
        "nebula.work.goal_action",
        "Start, pause, resume, cancel, block, or complete one exact goal after explicit operator instruction. Read goal first and pass its revision. Completion requires actual summary and evidence; a started goal is not finished.",
        GOAL_ACTION_SCHEMA,
        False,
    ),
    (
        "nebula.work.handoff_goal",
        "Recover a paused harness goal after a pre-admission Plan before acting failure. Core checks zero tool effects, pending approvals, queue state, provider tools and privacy, then creates one linked provider draft. Read the source goal first and start the returned draft separately. Never use this to bypass a safety hold.",
        HANDOFF_GOAL_SCHEMA,
        False,
    ),
    (
        "nebula.work.create_goal_child",
        "Create an isolated draft child goal under one running, budgeted parent goal after explicit operator instruction. Read the parent goal first. Start the child separately with goal_action.",
        CREATE_GOAL_CHILD_SCHEMA,
        False,
    ),
    (
        "nebula.work.subagents",
        "List delegated chat subagents and native harness subagents separately from main chats. Native tasks have turn_id and task_id, and only advertise supported controls. Filter by parent_session_id or page older entries with offset and native_turn_offset.",
        SUBAGENT_LIST_SCHEMA,
        True,
    ),
    (
        "nebula.work.subagent",
        "Read one listed subagent's task, progress, result, pending approval or question, and parent chat.",
        SUBAGENT_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.stop_subagent",
        "Stop one exact running subagent after explicit operator instruction. Read subagent first and verify the returned status.",
        SUBAGENT_TARGET_SCHEMA,
        False,
    ),
    (
        "nebula.work.stop_subagents",
        "Stop all active subagents directly delegated by one listed chat after explicit operator instruction. Read subagents first.",
        CHAT_TARGET_SCHEMA,
        False,
    ),
    (
        "nebula.work.native_subagent",
        "Read one native harness subagent listed by subagents, using its exact turn_id and task_id. Its control capability depends on the harness and live turn.",
        NATIVE_SUBAGENT_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.stop_native_subagent",
        "Stop one live native harness subagent only when native_subagent reports can_stop=true. This uses the harness's native task control, not delegated chat messaging.",
        NATIVE_SUBAGENT_TARGET_SCHEMA,
        False,
    ),
    (
        "nebula.work.queue",
        "Read one listed chat's saved follow-up queue, text, status, pause state, and revision.",
        CHAT_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.queue_action",
        "Pause, resume, remove, clear, retry, reorder, or edit queued follow-ups after explicit operator instruction. Read queue first and pass its revision. A queued item is not a completed response.",
        QUEUE_ACTION_SCHEMA,
        False,
    ),
    (
        "nebula.work.schedule",
        "Read a provider chat's recurring schedule, next run, enabled state, and revision. Read before changing it.",
        CHAT_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.create_schedule",
        "Create a recurring schedule for one provider chat after explicit operator instruction. Minimum interval is one hour; read back the saved next run.",
        CREATE_SCHEDULE_SCHEMA,
        False,
    ),
    (
        "nebula.work.set_schedule",
        "Enable or pause one exact recurring chat schedule after explicit operator instruction. Read schedule first and pass its revision.",
        SET_SCHEDULE_SCHEMA,
        False,
    ),
    (
        "nebula.work.chat_settings",
        "Read saved next-turn Assistant settings, revision, and available choices for a listed chat or session_id=self. Available MCP servers include their purpose and usable tools; tools_enabled alone does not attach an MCP browser.",
        CHAT_SETTINGS_TARGET_SCHEMA,
        True,
    ),
    (
        "nebula.work.set_chat_settings",
        "Change one listed chat's next-turn Assistant settings. Read chat_settings first and pass its revision. To give an agent a missing MCP capability, add the listed server ID to mcp_server_ids while preserving existing IDs, read back the setting, then send a new message. Running and queued turns keep their submitted settings.",
        SET_CHAT_SETTINGS_SCHEMA,
        False,
    ),
    (
        "nebula.work.require_plan",
        "Require one exact agent, goal, child round, or turn to save a Core-validated plan before its next enforceable action. Read current Core evidence and revision first, and record a concrete reason. An active harness turn applies this on its next turn.",
        REQUIRE_PLAN_SCHEMA,
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
        "Send a durable next message to a main assistant chat, or steer its active harness turn. For delegated subagents and goal children, message the parent main agent instead. A queued message is not a completed answer; use await_chat_result with its item_id to inspect the saved reply.",
        MESSAGE_CHAT_SCHEMA,
        False,
    ),
    (
        "nebula.work.await_chat_result",
        "Wait up to 25 seconds for one message_chat queued item. Return its exact queue status and linked saved assistant reply when available. If still in progress, call again or tell the operator it is running; never invent a completed result.",
        AWAIT_CHAT_RESULT_SCHEMA,
        True,
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
        "Approve, reject, or stop one exact pending chat or mission request after the operator explicitly directs that decision. Under project Act authority, background Butler may approve only its triggering local-read or passive request after reading the exact approval. Pass its revision. A saved approval is not proof that the work finished.",
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
        "nebula.work.fork_chat",
        "Fork a listed assistant chat through one saved message after explicit operator instruction. Read the chat for its revision and message ID first; the new chat shares project files but has its own transcript and runtime session.",
        FORK_CHAT_SCHEMA,
        False,
    ),
    (
        "nebula.work.delete_chat",
        "Permanently delete one listed assistant chat after explicit operator instruction. Read the exact chat and pass its revision and title; Core rejects active turns, running subagents and stale revisions.",
        DELETE_CHAT_SCHEMA,
        False,
    ),
    (
        "nebula.work.retry_chat_turn",
        "Start a linked retry of one failed, cancelled or interrupted harness chat turn after explicit operator instruction. Read the chat for the exact turn ID and revision. An existing saved retry is returned instead of starting duplicate work.",
        RETRY_CHAT_TURN_SCHEMA,
        False,
    ),
    (
        "nebula.work.retry_mission",
        "Retry one terminal mission after reading nebula.work.mission and passing its revision. A matching saved retry is returned instead of starting duplicate work. Remote MCP result transfer requires the operator's explicit consent.",
        RETRY_MISSION_SCHEMA,
        False,
    ),
)

_DIRECT_WORK_NAMES = frozenset(
    {
        "nebula.work.catalog",
        "nebula.work.overview",
        "nebula.work.start_mission",
    }
)
_MANAGED_WORK_NAMES = frozenset(
    name for name, *_ in _ALL_WORK_TOOLS if name not in _DIRECT_WORK_NAMES
)

# Advertise a compact gateway for most controls. Their full schemas are fetched
# through catalog when needed; sending them on every turn would consume a small
# model's context before the operator can ask a question.
WORK_TOOLS: tuple[tuple[str, str, dict[str, Any], bool], ...] = (
    *(item for item in _ALL_WORK_TOOLS if item[0] not in _MANAGED_WORK_NAMES),
    (
        "nebula.work.manage",
        "Read or change a project chat, its Assistant settings and MCP tools, mission, goal, subagent, follow-up queue, or schedule. Use nebula.work.catalog for the chosen operation's exact fields, then pass operation and arguments here. Read before writes, use saved revisions, and check a delegated result before claiming completion.",
        MANAGE_SCHEMA,
        False,
    ),
)


async def dispatch_work_tool(
    work: ControlAssistantWork, project_id: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    if name == "nebula.work.catalog":
        return work.catalog(project_id, arguments.get("operation"))
    if name == "nebula.work.manage":
        operation = _text(arguments.get("operation"), "operation", 200)
        if operation not in _MANAGED_WORK_NAMES:
            raise ValueError("choose a managed operation from nebula.work.catalog")
        payload = arguments.get("arguments")
        if not isinstance(payload, dict):
            raise ValueError("provide arguments for the managed operation")
        return await dispatch_work_tool(work, project_id, operation, payload)
    if name == "nebula.work.overview":
        return work.overview(project_id)
    if name == "nebula.work.harness_quota":
        return await work.harness_quota(project_id, arguments)
    if name == "nebula.work.chat":
        return work.chat_detail(project_id, arguments.get("session_id"))
    if name == "nebula.work.timeline":
        return work.timeline.read(project_id, arguments)
    if name == "nebula.work.goals":
        return work.goals_overview(project_id, arguments.get("offset", 0))
    if name == "nebula.work.goal":
        return work.goal_detail(project_id, arguments.get("session_id"))
    if name == "nebula.work.create_goal":
        return await work.create_goal(project_id, arguments)
    if name == "nebula.work.create_goal_conversation":
        return await work.create_goal_conversation(project_id, arguments)
    if name == "nebula.work.update_goal":
        return await work.update_goal(project_id, arguments)
    if name == "nebula.work.goal_action":
        return await work.goal_action(project_id, arguments)
    if name == "nebula.work.handoff_goal":
        return await work.handoff_goal(project_id, arguments)
    if name == "nebula.work.create_goal_child":
        return await work.create_goal_child(project_id, arguments)
    if name == "nebula.work.subagents":
        return work.subagents_overview(project_id, arguments)
    if name == "nebula.work.subagent":
        return work.subagent_detail(project_id, arguments.get("subagent_id"))
    if name == "nebula.work.stop_subagent":
        return await work.stop_subagent(project_id, arguments.get("subagent_id"))
    if name == "nebula.work.stop_subagents":
        return await work.stop_subagents(project_id, arguments.get("session_id"))
    if name == "nebula.work.native_subagent":
        return work.native_subagent_detail(project_id, arguments)
    if name == "nebula.work.stop_native_subagent":
        return await work.stop_native_subagent(project_id, arguments)
    if name == "nebula.work.queue":
        return work.queue_detail(project_id, arguments.get("session_id"))
    if name == "nebula.work.queue_action":
        return work.queue_action(project_id, arguments)
    if name == "nebula.work.schedule":
        return work.schedule_detail(project_id, arguments.get("session_id"))
    if name == "nebula.work.create_schedule":
        return await work.create_schedule(project_id, arguments)
    if name == "nebula.work.set_schedule":
        return await work.set_schedule(project_id, arguments)
    if name == "nebula.work.chat_settings":
        return work.chat_settings(project_id, arguments.get("session_id"))
    if name == "nebula.work.set_chat_settings":
        return work.set_chat_settings(project_id, arguments)
    if name == "nebula.work.require_plan":
        return work.require_plan(project_id, arguments)
    if name == "nebula.work.mission":
        return work.mission_detail(project_id, arguments.get("run_id"))
    if name == "nebula.work.start_chat":
        return await work.start_chat(project_id, arguments)
    if name == "nebula.work.message_chat":
        return await work.message_chat(project_id, arguments)
    if name == "nebula.work.await_chat_result":
        return await work.await_chat_result(project_id, arguments)
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
    if name == "nebula.work.fork_chat":
        return await work.fork_chat(project_id, arguments)
    if name == "nebula.work.delete_chat":
        return await work.delete_chat(project_id, arguments)
    if name == "nebula.work.retry_chat_turn":
        return await work.retry_chat_turn(project_id, arguments)
    if name == "nebula.work.retry_mission":
        return await work.retry_mission(project_id, arguments)
    raise ValueError("work tool is not available")
