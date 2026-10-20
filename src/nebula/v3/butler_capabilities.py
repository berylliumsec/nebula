"""Small, project-bound capability surface for Butler's provider and harness turns.

The full product actions stay in Core. Only discovery and two invocation tools
are advertised to a model; the exact action schema is returned on demand.
"""

from __future__ import annotations

import re
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from .domain import (
    Approval,
    ApprovalStatus,
    ChatMessage,
    ChatGoal,
    ChatRole,
    ChatSession,
    ChatTurn,
    Engagement,
    EngagementStatus,
    ButlerAuthorityPolicy,
    ButlerRecoveryCase,
    RiskClass,
    message_is_replaced,
    utc_now,
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
_BACKGROUND_CHANGES = frozenset(
    {
        "nebula.work.create_goal",
        "nebula.work.create_goal_conversation",
        "nebula.work.handoff_goal",
        "nebula.work.create_goal_child",
        "nebula.work.stop_subagent",
        "nebula.work.stop_subagents",
        "nebula.work.stop_native_subagent",
        "nebula.work.queue_action",
        "nebula.work.create_schedule",
        "nebula.work.set_schedule",
        "nebula.work.require_plan",
        "nebula.work.start_chat",
        "nebula.work.stop_chat",
        "nebula.work.start_mission",
        "nebula.work.steer_mission",
        "nebula.work.stop_mission",
        "nebula.work.answer_question",
        "nebula.work.update_chat",
        "nebula.work.fork_chat",
        "nebula.work.delete_chat",
        "nebula.work.set_chat_settings",
        "nebula.work.set_assistant_defaults",
        "nebula.work.message_chat",
        "nebula.work.update_goal",
        "nebula.work.goal_action",
        "nebula.work.decide_approval",
        "nebula.work.retry_chat_turn",
        "nebula.work.retry_mission",
        "nebula.global.resume_project",
        "nebula.global.create_goal",
        "nebula.global.create_recovery_goal",
        "nebula.global.handoff_goal",
        "nebula.global.goal_action",
        "nebula.global.update_goal",
        "nebula.global.set_chat_settings",
        "nebula.global.set_assistant_defaults",
        "nebula.global.work_change",
        "nebula.global.create_child_project",
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
        background_case_id: str | None = None,
        instructions_root: Path | None = None,
        background_runtime_local: bool = False,
    ) -> None:
        # Existing handlers retain project ownership, revision and permission
        # checks. The gateway only controls discovery and schema validation.
        self._store = store
        self._project_id = project_id
        self._project_name = store.get(Engagement, project_id).name
        self._memory = memory
        self._operator_message = operator_message
        self._operator_turn_id = operator_turn_id
        self._background_case_id = background_case_id
        self._work = work
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
        if background_case_id is not None:
            self._actions = {
                name: plugin
                for name, plugin in self._actions.items()
                if name in _BACKGROUND_CHANGES
                or name in {"nebula.memory.search", "nebula.memory.list"}
                or name.startswith("nebula.work.")
                and name != "nebula.work.catalog"
                and self._read_only(plugin)
            }
            case = store.get(ButlerRecoveryCase, background_case_id)
            if case.signal == "butler.global_periodic_check":
                if instructions_root is None or work is None:
                    raise ValueError("Nebula-wide Butler tools are unavailable")
                from .butler_global_capabilities import ButlerGlobalActions

                self._actions.update(
                    {
                        plugin.spec.name: plugin
                        for plugin in ButlerGlobalActions(
                            store,
                            work,
                            instructions_root,
                            background_case_id,
                            runtime_local=background_runtime_local,
                        ).plugins()
                    }
                )

    def _background_case(self) -> ButlerRecoveryCase:
        if self._background_case_id is None:
            raise RuntimeError("not a background Butler action")
        case = self._store.get(ButlerRecoveryCase, self._background_case_id)
        project = self._store.get(Engagement, self._project_id)
        policy = self._store.find_entity(
            ButlerAuthorityPolicy, "engagement_id", self._project_id
        )
        if (
            case.engagement_id != self._project_id
            or case.status != "investigating"
            or project.status == EngagementStatus.ARCHIVED
            or not project.work_enabled
            or policy is None
            or policy.authority != "act"
            or policy.revision != case.authority_revision
        ):
            raise ValueError("Butler Act authority or recovery case changed")
        return case

    def _check_background_change_reason(
        self, case: ButlerRecoveryCase, name: str, arguments: dict[str, Any]
    ) -> None:
        if name == "nebula.work.decide_approval":
            if (
                case.signal != "review.approval_requested"
                or arguments.get("decision") != "approve"
            ):
                raise ValueError(
                    "background approval requires the exact pending review signal"
                )
            from .butler_oversight import ButlerEventPayload
            from .butler_recovery import ButlerRecoveryService

            event = ButlerRecoveryService(self._store).source_event(case)
            payload = ButlerEventPayload.model_validate(event.payload)
            approval_id = str(arguments.get("action_id") or "")
            if not any(
                ref.kind == "approval" and ref.id == approval_id
                for ref in payload.evidence_refs
            ):
                raise ValueError(
                    "background Butler may decide only the triggering approval"
                )
            approval = self._store.get(Approval, approval_id)
            if (
                approval.engagement_id != self._project_id
                or approval.status != ApprovalStatus.PENDING
                or approval.risk_class not in {RiskClass.LOCAL_READ, RiskClass.PASSIVE}
                or approval.revision != arguments.get("expected_revision")
            ):
                raise ValueError(
                    "approval is stale, outside this project, or requires operator review"
                )
            return
        if case.signal != "agent.subagent_start_failed":
            return
        from .butler_oversight import ButlerEventPayload
        from .butler_recovery import ButlerRecoveryService

        event = ButlerRecoveryService(self._store).source_event(case)
        metrics = ButlerEventPayload.model_validate(event.payload).metrics
        category = metrics.get("category")
        rule = metrics.get("rule")
        if (category == "budget_exhausted" and name != "nebula.work.update_goal") or (
            category == "permission_denied"
            and rule not in {"subagents.turned_off", "subagents.runtime_model"}
        ):
            raise ValueError(
                "Core's budget or policy refusal requires an authorized edit or operator review"
            )
        if category == "capacity_reached" and name == "nebula.work.set_chat_settings":
            raise ValueError(
                "A full subagent slot is not a runtime configuration failure"
            )

    def _begin_background_step(self, operation: str, arguments: dict[str, Any]) -> str:
        from .butler_oversight import _entity_in_transaction
        from .butler_authority import policy_id

        case = self._background_case()
        step_id = str(uuid4())
        digest = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, default=str).encode()
        ).hexdigest()
        with self._store.transaction(immediate=True) as transaction:
            current = _entity_in_transaction(transaction, ButlerRecoveryCase, case.id)
            policy = _entity_in_transaction(
                transaction,
                ButlerAuthorityPolicy,
                policy_id(self._project_id),
            )
            if (
                current is None
                or current.status != "investigating"
                or current.revision != case.revision
                or policy is None
                or policy.authority != "act"
                or policy.revision != current.authority_revision
            ):
                raise ValueError("Butler Act authority or recovery case changed")
            transaction.update(
                ButlerRecoveryCase,
                current.id,
                {
                    "steps": [
                        *current.steps,
                        {
                            "id": step_id,
                            "operation": operation,
                            "arguments_sha256": digest,
                            "status": "running",
                            "started_at": utc_now().isoformat(),
                        },
                    ]
                },
                expected_revision=current.revision,
            )
        return step_id

    def _finish_background_step(
        self,
        step_id: str,
        *,
        operation: str,
        arguments: dict[str, Any],
        result: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        from .butler_oversight import _entity_in_transaction

        assert self._background_case_id is not None
        with self._store.transaction(immediate=True) as transaction:
            case = _entity_in_transaction(
                transaction, ButlerRecoveryCase, self._background_case_id
            )
            if case is None:
                return
            steps = list(case.steps)
            index = next(
                (i for i, row in enumerate(steps) if row.get("id") == step_id), None
            )
            if index is None or steps[index].get("status") != "running":
                return
            steps[index] = {
                **steps[index],
                "status": (
                    "uncertain"
                    if error and operation in _BACKGROUND_CHANGES
                    else "failed"
                    if error or result and result.get("delivery") == "held"
                    else "applied"
                ),
                "finished_at": utc_now().isoformat(),
                **({"error_type": type(error).__name__} if error else {}),
            }
            updates: dict[str, Any] = {"steps": steps}
            if error is None and operation == "nebula.work.message_chat":
                updates["queued_item_id"] = result.get("item_id") if result else None
                updates["delivered_turn_id"] = (
                    result.get("turn_id")
                    if result and result.get("delivery") in {"steered", "started"}
                    else None
                )
                updates["delivery_hold_reason"] = (
                    result.get("reason")
                    if result and result.get("delivery") == "held"
                    else None
                )
                updates["target_session_id"] = arguments.get("session_id")
            transaction.update(
                ButlerRecoveryCase,
                case.id,
                updates,
                expected_revision=case.revision,
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

    def _description(self, plugin: AnalysisTool) -> str:
        description = plugin.spec.description
        if self._background_case_id is None:
            return description
        if plugin.spec.name == "nebula.work.message_chat":
            return (
                "Send parent guidance to a main assistant chat with "
                "delivery=immediate. Core steers an active harness turn when "
                "supported or starts a new turn when idle. Busy Grok turns "
                "cannot be steered; their guidance is held, not delivered. "
                "For delegated subagents and goal children, "
                "message the parent main agent. A held result is not delivered; "
                "inspect the Core receipt before claiming a handoff."
            )
        return (
            description.replace(
                "after explicit operator instruction",
                "under the project's standing Act authority",
            )
            .replace(
                "after the operator explicitly directs that decision",
                "under the project's standing Act authority after reading the exact request",
            )
            .replace(
                "only when the operator requested it",
                "when needed to repair the observed project work under Act authority",
            )
        )

    def _input_schema(self, plugin: AnalysisTool) -> dict[str, Any]:
        schema = plugin.spec.input_schema
        if (
            self._background_case_id is None
            or plugin.spec.name != "nebula.work.message_chat"
        ):
            return schema
        return {
            **schema,
            "properties": {
                **schema["properties"],
                "delivery": {
                    "type": "string",
                    "enum": ["immediate"],
                    "description": "Required for background Butler parent guidance.",
                },
            },
            "required": [*schema.get("required", []), "delivery"],
        }

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
                "categories": list(
                    _CATEGORIES[:2] if self._background_case_id else _CATEGORIES
                ),
                "suggested": [
                    name
                    for name in (
                        "nebula.work.overview",
                        "nebula.work.attention",
                        "nebula.memory.search",
                        "nebula.help.search",
                        "nebula.settings.catalog",
                    )
                    if name in self._actions
                ],
                "hint": "Search for the requested action, then describe its exact fields.",
            }
        ranked: list[tuple[int, str, AnalysisTool]] = []
        for name, plugin in self._actions.items():
            title = name.lower()
            description = self._description(plugin).lower()

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
                    "description": self._description(plugin),
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
            "description": self._description(plugin),
            "input_schema": self._input_schema(plugin),
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
            Draft202012Validator(self._input_schema(plugin)).validate(payload)
        except ValidationError as exc:
            raise ValueError(
                f"invalid arguments for {name}: {exc.message}; "
                "call nebula.capabilities.describe for its exact schema"
            ) from exc
        step_id: str | None = None
        if self._background_case_id is not None:
            case = self._background_case()
            if change and name not in _BACKGROUND_CHANGES:
                raise ValueError("this operation is unavailable to background Butler")
            if change:
                self._check_background_change_reason(case, name, payload)
            if change and name in {
                "nebula.work.set_chat_settings",
                "nebula.work.message_chat",
                "nebula.work.retry_chat_turn",
            }:
                target = self._store.get(
                    ChatSession, str(payload.get("session_id") or "")
                )
                if target.engagement_id != self._project_id or target.parent_session_id:
                    raise ValueError("background Butler may act only on a main chat")
            if change and name in {
                "nebula.work.goal_action",
                "nebula.work.update_goal",
            }:
                goal = self._store.find_entity(
                    ChatGoal, "session_id", str(payload.get("session_id") or "")
                )
                if goal is None:
                    raise ValueError("read a Core-owned goal before changing it")
                if goal.engagement_id != self._project_id or goal.parent_goal_id:
                    raise ValueError("background Butler may act only on a main goal")
            if (
                name == "nebula.work.message_chat"
                and payload.get("delivery", "next") != "immediate"
            ):
                raise ValueError("background Butler requires immediate parent guidance")
            if name == "nebula.work.set_chat_settings" and self._work is not None:
                settings = payload.get("settings")
                if isinstance(settings, dict) and any(
                    key in settings
                    for key in (
                        "subagent_provider_id",
                        "subagent_harness_profile_id",
                        "subagent_model",
                    )
                ):
                    current = self._work.chat_settings(
                        self._project_id, payload.get("session_id")
                    )
                    selected = {**current["settings"], **settings}
                    if selected.get("backend") == "harness" and selected.get(
                        "allow_subagents"
                    ):
                        self._work.chat.subagents.validate_harness_setting(
                            self._project_id,
                            selected.get("subagent_provider_id") or "",
                            selected.get("subagent_model") or "",
                            selected.get("max_active_subagents"),
                            selected.get("subagent_reasoning_effort"),
                            selected.get("subagent_harness_profile_id"),
                        )
            step_id = self._begin_background_step(name, payload)
            self._background_case()
        try:
            if (
                self._background_case_id is not None
                and name == "nebula.work.message_chat"
                and self._work is not None
                and step_id is not None
            ):
                result = await self._work.message_chat(
                    self._project_id,
                    payload,
                    idempotency_key=f"recovery:{self._background_case_id}:{step_id}",
                )
            else:
                result = await plugin._handler(payload)
        except BaseException as exc:
            if step_id is not None:
                self._finish_background_step(
                    step_id, operation=name, arguments=payload, error=exc
                )
            raise
        if step_id is not None:
            self._finish_background_step(
                step_id, operation=name, arguments=payload, result=result
            )
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
                "Run one described project-scoped change under this project's standing Act authority. Core checks target, revision and permissions; report saved state accurately."
                if self._background_case_id is not None
                else "Run one described project-scoped change only when the operator requested it. Core checks target, revision and permissions; report saved state or pending browser action accurately.",
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
