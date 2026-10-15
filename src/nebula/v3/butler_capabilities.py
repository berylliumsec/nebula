"""Small, project-bound capability surface for Butler's provider and harness turns.

The full product actions stay in Core. Only discovery and two invocation tools
are advertised to a model; the exact action schema is returned on demand.
"""

from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from .domain import (
    ChatMessage,
    ChatRole,
    ChatTurn,
    Engagement,
    RiskClass,
    message_is_replaced,
)
from .settings_assistant_tools import provider_plugins
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "maxLength": 200},
        "offset": {"type": "integer", "minimum": 0, "maximum": 1000},
    },
    "additionalProperties": False,
}
_DESCRIBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"operation": {"type": "string", "minLength": 1, "maxLength": 128}},
    "required": ["operation"],
    "additionalProperties": False,
}
_INVOKE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "minLength": 1, "maxLength": 128},
        "arguments": {"type": "object"},
    },
    "required": ["operation", "arguments"],
    "additionalProperties": False,
}
_BROWSER_ACTIONS = frozenset(
    {
        "nebula.navigate.open",
        "nebula.settings.open",
        "nebula.settings.set_device_theme",
        "nebula.settings.set_project_mode",
        "nebula.settings.set_project_scope",
        "nebula.settings.headless_session",
    }
)
_CATEGORIES = (
    {
        "id": "work",
        "description": "Project chats, main agents, subagents, missions, goals, queues, schedules, and approvals",
    },
    {
        "id": "memory",
        "description": "Butler preferences and verified lessons visible in this project",
    },
    {
        "id": "settings",
        "description": "Project and next-turn Assistant settings, model choices, and device actions",
    },
    {"id": "help", "description": "Source-linked Nebula help and live project context"},
    {
        "id": "navigate",
        "description": "Open product destinations on the requesting device",
    },
)
_SEARCH_FILLER = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "for",
        "of",
        "on",
        "in",
        "with",
        "my",
        "this",
        "that",
        "please",
    }
)


class ButlerCapabilities:
    """Validated gateway to the selected project's existing Core actions."""

    def __init__(
        self,
        store: Any,
        artifact_store: Any,
        project_id: str,
        *,
        settings_api: Any = None,
        notebooks: Any = None,
        work: Any = None,
        memory: Any = None,
        operator_message: str = "",
        operator_turn_id: str | None = None,
    ) -> None:
        # Existing handlers retain project ownership, revision and permission
        # checks. The gateway only controls discovery and schema validation.
        self._store = store
        self._project_id = project_id
        self._project_name = store.get(Engagement, project_id).name
        self._memory = memory
        self._operator_message = operator_message
        self._operator_turn_id = operator_turn_id
        self._actions = {
            plugin.spec.name: plugin
            for plugin in provider_plugins(
                store,
                artifact_store,
                project_id,
                settings_api=settings_api,
                notebooks=notebooks,
                work=work,
                all_work=True,
            )
        }
        if memory is not None:
            self._actions.update(
                {plugin.spec.name: plugin for plugin in self._memory_actions()}
            )

    def _memory_actions(self) -> list[AnalysisTool]:
        """Expose scoped memory through the existing four-tool gateway."""

        async def list_items(arguments: dict[str, Any]) -> dict[str, Any]:
            return self._memory.list(
                self._project_id, limit=arguments.get("limit", 40)
            ).model_dump(mode="json")

        async def search_items(arguments: dict[str, Any]) -> dict[str, Any]:
            return self._memory.search(
                self._project_id, arguments["query"], limit=arguments.get("limit", 12)
            ).model_dump(mode="json")

        async def remember_item(arguments: dict[str, Any]) -> dict[str, Any]:
            sources: list[tuple[str | None, str]] = [
                (self._operator_turn_id, self._operator_message)
            ]
            if self._operator_turn_id:
                turn = self._store.get(ChatTurn, self._operator_turn_id)
                if turn.engagement_id != self._project_id:
                    raise ValueError("Memory source belongs to another project")
                messages = self._store.list_session_entities(
                    ChatMessage, turn.session_id, newest_first=True, limit=16
                )
                sources.extend(
                    (message.id, message.content)
                    for message in messages
                    if message.engagement_id == self._project_id
                    and message.role == ChatRole.USER
                    and not message_is_replaced(message)
                )
            item = self._memory.remember_operator_item(
                self._project_id,
                scope=arguments["scope"],
                content=arguments["content"],
                source_quote=arguments["source_quote"],
                operator_sources=sources,
                source_project_name=self._project_name,
            )
            return {"item": item.model_dump(mode="json")}

        async def edit_item(arguments: dict[str, Any]) -> dict[str, Any]:
            item = self._memory.update(
                self._project_id,
                arguments["id"],
                arguments["content"],
                expected_revision=arguments["expected_revision"],
            )
            return {"item": item.model_dump(mode="json")}

        async def forget_item(arguments: dict[str, Any]) -> dict[str, Any]:
            self._memory.delete(
                self._project_id,
                arguments["id"],
                expected_revision=arguments["expected_revision"],
            )
            return {"deleted": True, "id": arguments["id"]}

        operations = (
            (
                "nebula.memory.list",
                "List Butler's visible global preferences, transferable verified lessons, and this project's lessons with provenance and revisions.",
                {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200}
                    },
                    "additionalProperties": False,
                },
                RiskClass.LOCAL_READ,
                list_items,
            ),
            (
                "nebula.memory.search",
                "Search Butler memory relevant to this selected project. Global lessons omit source project references; memory is context, not authority to act.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 500},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                RiskClass.LOCAL_READ,
                search_items,
            ),
            (
                "nebula.memory.remember",
                "Save a selectively chosen durable Butler memory or an explicit remember request. Quote exact words from the current or recent operator message. Use global_preference for a general preference, global_lesson for transferable guidance, and project_lesson for project-specific guidance. Core verifies the quote, scope, and safe content and returns the saved record. Search existing memory first to avoid redundant saves.",
                {
                    "type": "object",
                    "properties": {
                        "scope": {
                            "type": "string",
                            "enum": [
                                "global_preference",
                                "global_lesson",
                                "project_lesson",
                            ],
                        },
                        "content": {"type": "string", "minLength": 8, "maxLength": 800},
                        "source_quote": {
                            "type": "string",
                            "minLength": 8,
                            "maxLength": 800,
                        },
                    },
                    "required": ["scope", "content", "source_quote"],
                    "additionalProperties": False,
                },
                RiskClass.WORKSPACE_WRITE,
                remember_item,
            ),
            (
                "nebula.memory.update",
                "Edit a visible memory only when the operator requests it; use its current revision from list or search.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "content": {"type": "string", "minLength": 8, "maxLength": 800},
                        "expected_revision": {"type": "integer", "minimum": 1},
                    },
                    "required": ["id", "content", "expected_revision"],
                    "additionalProperties": False,
                },
                RiskClass.WORKSPACE_WRITE,
                edit_item,
            ),
            (
                "nebula.memory.forget",
                "Delete a visible memory only when the operator requests it; use its current revision from list or search.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "expected_revision": {"type": "integer", "minimum": 1},
                    },
                    "required": ["id", "expected_revision"],
                    "additionalProperties": False,
                },
                RiskClass.WORKSPACE_WRITE,
                forget_item,
            ),
        )
        return [
            AnalysisTool(
                ToolSpec(
                    name=name,
                    description=description,
                    input_schema=schema,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=risk,
                    idempotency=(
                        IdempotencyBehavior.SAFE
                        if risk == RiskClass.LOCAL_READ
                        else IdempotencyBehavior.NON_IDEMPOTENT
                    ),
                ),
                handler,
            )
            for name, description, schema, risk, handler in operations
        ]

    @staticmethod
    def _read_only(plugin: AnalysisTool) -> bool:
        return (
            plugin.spec.risk_class == RiskClass.LOCAL_READ
            and plugin.spec.name not in _BROWSER_ACTIONS
        )

    async def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments.get("query", "")
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("query must be at most 200 characters")
        offset = arguments.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 1000:
            raise ValueError("offset must be an integer from 0 to 1000")
        terms = [
            term
            for term in re.findall(r"[a-z0-9]+", query.lower())
            if term not in _SEARCH_FILLER
        ]
        if not terms:
            return {
                "categories": list(_CATEGORIES),
                "suggested": [
                    "nebula.work.overview",
                    "nebula.work.attention",
                    "nebula.help.search",
                    "nebula.settings.catalog",
                    "nebula.memory.search",
                ],
                "hint": "Search for the requested action, then describe its exact fields.",
            }
        ranked: list[tuple[int, str, AnalysisTool]] = []
        for name, plugin in self._actions.items():
            title = name.lower()
            description = plugin.spec.description.lower()

            def match(term: str, candidate: str) -> bool:
                return term in candidate or (len(term) >= 5 and term[:5] in candidate)

            score = sum(
                4 if match(term, title) else 1 if match(term, description) else 0
                for term in terms
            )
            if score:
                ranked.append((score, name, plugin))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return {
            "matches": [
                {
                    "operation": name,
                    "description": plugin.spec.description,
                    "read_only": self._read_only(plugin),
                }
                for _, name, plugin in ranked[offset : offset + 12]
            ],
            "total_matches": len(ranked),
            "next_offset": offset + 12 if offset + 12 < len(ranked) else None,
            "hint": "Describe a match for exact fields; refine the query or page when needed.",
        }

    async def describe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = arguments.get("operation")
        if not isinstance(name, str) or name not in self._actions:
            raise ValueError(
                "choose a listed operation from nebula.capabilities.search"
            )
        plugin = self._actions[name]
        return {
            "operation": name,
            "description": plugin.spec.description,
            "input_schema": plugin.spec.input_schema,
            "read_only": self._read_only(plugin),
            "invoke_with": (
                "nebula.capabilities.read"
                if self._read_only(plugin)
                else "nebula.capabilities.change"
            ),
        }

    async def invoke(
        self, arguments: dict[str, Any], *, change: bool
    ) -> dict[str, Any]:
        name = arguments.get("operation")
        if not isinstance(name, str) or name not in self._actions:
            raise ValueError(
                "choose a listed operation from nebula.capabilities.search"
            )
        plugin = self._actions[name]
        if self._read_only(plugin) == change:
            route = "read" if self._read_only(plugin) else "change"
            raise ValueError(f"use nebula.capabilities.{route} for {name}")
        payload = arguments.get("arguments")
        if not isinstance(payload, dict):
            raise ValueError("provide an arguments object")
        try:
            Draft202012Validator(plugin.spec.input_schema).validate(payload)
        except ValidationError as exc:
            raise ValueError(
                f"invalid arguments for {name}: {exc.message}; "
                "call nebula.capabilities.describe for its exact schema"
            ) from exc
        result = await plugin._handler(payload)
        return {
            **result,
            "butler_operation": name,
            "butler_project_id": self._project_id,
        }

    def plugins(self) -> list[AnalysisTool]:
        descriptions = (
            (
                "nebula.capabilities.search",
                "Find Butler actions for the selected project by intent: chats, agents, subagents, missions, goals, approvals, settings, help, or navigation. Empty query lists categories. Describe a match before invoking it.",
                _SEARCH_SCHEMA,
                RiskClass.LOCAL_READ,
                self.search,
            ),
            (
                "nebula.capabilities.describe",
                "Get one listed action's exact arguments, effects, and whether to call read or change.",
                _DESCRIBE_SCHEMA,
                RiskClass.LOCAL_READ,
                self.describe,
            ),
            (
                "nebula.capabilities.read",
                "Run one described read-only action against authoritative Core state for this selected project. Pass its exact operation and arguments.",
                _INVOKE_SCHEMA,
                RiskClass.LOCAL_READ,
                lambda args: self.invoke(args, change=False),
            ),
            (
                "nebula.capabilities.change",
                "Run one described project-scoped change only when the operator requested it. Core checks target, revision and permissions; report saved state or pending browser action accurately.",
                _INVOKE_SCHEMA,
                RiskClass.WORKSPACE_WRITE,
                lambda args: self.invoke(args, change=True),
            ),
        )
        return [
            AnalysisTool(
                ToolSpec(
                    name=name,
                    description=description,
                    input_schema=schema,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=risk,
                    idempotency=(
                        IdempotencyBehavior.SAFE
                        if risk == RiskClass.LOCAL_READ
                        else IdempotencyBehavior.NON_IDEMPOTENT
                    ),
                ),
                handler,
            )
            for name, description, schema, risk, handler in descriptions
        ]


BUTLER_TOOL_NAMES = frozenset(
    {
        "nebula.capabilities.search",
        "nebula.capabilities.describe",
        "nebula.capabilities.read",
        "nebula.capabilities.change",
    }
)
