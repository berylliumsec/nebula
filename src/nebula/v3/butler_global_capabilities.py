"""Bounded Core actions for one Nebula-wide scheduled Butler case."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .butler_instructions import read_butler_instructions
from .domain import (
    ButlerAuthorityPolicy,
    ButlerRecoveryCase,
    ChatGoal,
    Engagement,
    EngagementStatus,
    RiskClass,
    ScopePolicy,
)
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_REV = {"type": "integer", "minimum": 1}
_PROJECT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"project_id": _ID},
    "required": ["project_id"],
}


class ButlerGlobalActions:
    def __init__(
        self,
        store: Any,
        work: Any,
        instructions_root: Path,
        case_id: str,
        *,
        runtime_local: bool = False,
    ) -> None:
        self.store = store
        self.work = work
        self.instructions_root = instructions_root
        self.case_id = case_id
        self.runtime_local = runtime_local

    def _case(self) -> ButlerRecoveryCase:
        case = self.store.get(ButlerRecoveryCase, self.case_id)
        if (
            case.status != "investigating"
            or case.signal != "butler.global_periodic_check"
        ):
            raise ValueError("Nebula-wide Butler case is no longer active")
        return case

    def _mandate(self) -> ButlerRecoveryCase:
        from .butler_oversight import ButlerEventPayload
        from .butler_recovery import ButlerRecoveryService

        case = self._case()
        loaded = read_butler_instructions(self.instructions_root)
        if loaded is None:
            raise ValueError("Nebula-wide BUTLER.md was removed")
        event = ButlerRecoveryService(self.store).source_event(case)
        digest = ButlerEventPayload.model_validate(event.payload).metrics.get(
            "instructions_sha256"
        )
        if loaded.sha256 != digest:
            raise ValueError("Nebula-wide BUTLER.md changed during this check")
        return case

    def _project(self, project_id: str) -> Engagement:
        project = self.store.get(Engagement, project_id)
        if project.status == EngagementStatus.ARCHIVED:
            raise ValueError("Archived projects cannot be managed by Butler")
        if not self._visible(project):
            raise ValueError(
                "Project is outside this Butler runtime's Act and privacy scope"
            )
        return project

    def _visible(self, project: Engagement) -> bool:
        policy = self.store.find_entity(
            ButlerAuthorityPolicy, "engagement_id", project.id
        )
        if policy is None or policy.authority != "act":
            return False
        if not self.runtime_local and project.scope_policy_id:
            scope = self.store.get(ScopePolicy, project.scope_policy_id)
            if scope.engagement_id != project.id or scope.local_only:
                return False
        return True

    def _act(self, project_id: str, expected: int) -> Engagement:
        self._mandate()
        project = self._project(project_id)
        policy = self.store.find_entity(
            ButlerAuthorityPolicy, "engagement_id", project_id
        )
        if (
            not project.work_enabled
            or policy is None
            or policy.authority != "act"
            or policy.revision != expected
        ):
            raise ValueError("Target project Act authority changed or is unavailable")
        return project

    async def projects(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._case()
        offset = arguments.get("offset", 0)
        projects = sorted(
            (
                project
                for project in self.store.iter_readable_entities(Engagement)
                if project.status != EngagementStatus.ARCHIVED
                and project.work_enabled
                and self._visible(project)
            ),
            key=lambda item: (item.created_at, item.id),
        )
        rows = []
        for project in projects[offset : offset + 50]:
            policy = self.store.find_entity(
                ButlerAuthorityPolicy, "engagement_id", project.id
            )
            rows.append(
                {
                    "id": project.id,
                    "name": project.name,
                    "status": project.status.value,
                    "revision": project.revision,
                    "work_enabled": project.work_enabled,
                    "parent_project_id": project.parent_engagement_id,
                    "authority": policy.authority if policy else "watch",
                    "authority_revision": policy.revision if policy else 0,
                }
            )
        return {
            "projects": rows,
            "next_offset": offset + 50 if offset + 50 < len(projects) else None,
            "runtimes": self.work.catalog(self._case().engagement_id)["runtimes"],
        }

    async def project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        project = self._project(project_id)
        return {
            "project": project.model_dump(mode="json"),
            "work": self.work.overview(project_id),
            "goals": self.work.goals_overview(project_id),
        }

    async def goal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._project(arguments["project_id"])
        return self.work.goal_detail(arguments["project_id"], arguments["session_id"])

    async def resume_project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        project = self._act(project_id, arguments["expected_authority_revision"])
        if project.revision != arguments["expected_project_revision"]:
            raise ValueError("Project revision changed; read it again")
        if project.status not in {EngagementStatus.DRAFT, EngagementStatus.PAUSED}:
            return {
                "project": project.model_dump(mode="json"),
                "already_active": project.status == EngagementStatus.ACTIVE,
            }
        saved = self.store.update(
            Engagement,
            project.id,
            {"status": EngagementStatus.ACTIVE},
            expected_revision=project.revision,
        )
        return {"project": saved.model_dump(mode="json"), "resumed": True}

    def _existing_goal(self, project_id: str, objective: str) -> ChatGoal | None:
        wanted = " ".join(objective.split()).casefold()
        return next(
            (
                goal
                for goal in self.store.iter_readable_entities(
                    ChatGoal, engagement_id=project_id
                )
                if goal.parent_goal_id is None
                and " ".join(goal.objective.split()).casefold() == wanted
            ),
            None,
        )

    async def create_goal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        project = self._act(project_id, arguments["expected_authority_revision"])
        if project.revision != arguments["expected_project_revision"]:
            raise ValueError("Project revision changed; read it again")
        if project.status != EngagementStatus.ACTIVE:
            raise ValueError("Activate the project before creating a goal")
        existing = self._existing_goal(project_id, arguments["objective"])
        if existing is not None:
            return {
                **self.work.goal_detail(project_id, existing.session_id),
                "already_exists": True,
            }
        goal_args = {
            key: value
            for key, value in arguments.items()
            if key
            not in {
                "project_id",
                "expected_project_revision",
                "expected_authority_revision",
                "start",
            }
        }
        project = self._act(project_id, arguments["expected_authority_revision"])
        if project.status != EngagementStatus.ACTIVE:
            raise ValueError("Activate the project before creating a goal")
        created = await self.work.create_goal_conversation(project_id, goal_args)
        if arguments.get("start", False):
            self._act(project_id, arguments["expected_authority_revision"])
            started = await self.work.goal_action(
                project_id,
                {
                    "session_id": created["session_id"],
                    "expected_revision": created["goal"]["revision"],
                    "action": "start",
                },
            )
            return {**started, "created": True, "started": True}
        return {**created, "created": True}

    async def goal_action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        project = self._act(project_id, arguments["expected_authority_revision"])
        if project.status != EngagementStatus.ACTIVE:
            raise ValueError("Activate the project before starting or resuming a goal")
        if arguments["action"] not in {"start", "resume"}:
            raise ValueError("Scheduled Butler may only start or resume a main goal")
        current = self.work.goal_detail(project_id, arguments["session_id"])
        goal = current.get("goal")
        if (
            current.get("kind") != "main"
            or goal is None
            or goal["revision"] != arguments["expected_revision"]
        ):
            raise ValueError("Main goal changed; read it again")
        project = self._act(project_id, arguments["expected_authority_revision"])
        if project.status != EngagementStatus.ACTIVE:
            raise ValueError("Activate the project before starting or resuming a goal")
        return await self.work.goal_action(
            project_id,
            {
                "session_id": arguments["session_id"],
                "expected_revision": arguments["expected_revision"],
                "action": arguments["action"],
            },
        )

    async def create_child_project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .setup import create_engagement_with_default_scope

        parent_id = arguments["parent_project_id"]
        parent = self._act(parent_id, arguments["expected_authority_revision"])
        if parent.revision != arguments["expected_parent_revision"]:
            raise ValueError("Parent project revision changed; read it again")
        if parent.status != EngagementStatus.ACTIVE:
            raise ValueError("Activate the parent project before creating child work")
        name = arguments["name"].strip()
        if not name:
            raise ValueError("Child project name cannot be blank")
        normalized = " ".join(name.split()).casefold()
        child_id = (
            "butler-child:"
            + hashlib.sha256(f"{parent_id}\0{normalized}".encode()).hexdigest()
        )
        existing = self.store.find_entity(Engagement, "id", child_id)
        if existing is None:
            existing = next(
                (
                    item
                    for item in self.store.iter_readable_entities(Engagement)
                    if item.parent_engagement_id == parent_id
                    and " ".join(item.name.split()).casefold() == normalized
                ),
                None,
            )
        if existing is None:
            self._act(parent_id, arguments["expected_authority_revision"])
            existing = create_engagement_with_default_scope(
                self.store,
                Engagement(
                    id=child_id,
                    name=name,
                    description=arguments.get("description", ""),
                    status=EngagementStatus.ACTIVE,
                    parent_engagement_id=parent_id,
                ),
            )
            created = True
        else:
            created = False
        result: dict[str, Any] = {
            "project": existing.model_dump(mode="json"),
            "created": created,
        }
        initial_goal = arguments.get("initial_goal")
        if initial_goal is None and arguments.get("start_initial_goal"):
            raise ValueError("Provide initial_goal before requesting its start")
        if initial_goal is not None:
            current = self._existing_goal(existing.id, initial_goal["objective"])
            if current is None:
                self._act(parent_id, arguments["expected_authority_revision"])
                saved = await self.work.create_goal_conversation(
                    existing.id, initial_goal
                )
            else:
                saved = self.work.goal_detail(existing.id, current.session_id)
            result["goal"] = saved
            if arguments.get("start_initial_goal", False):
                goal = saved["goal"]
                if goal["status"] == "draft":
                    self._act(parent_id, arguments["expected_authority_revision"])
                    saved = await self.work.goal_action(
                        existing.id,
                        {
                            "session_id": saved["session_id"],
                            "expected_revision": goal["revision"],
                            "action": "start",
                        },
                    )
                result["goal"] = saved
        return result

    def plugins(self) -> list[AnalysisTool]:
        from .control_assistant_work import START_GOAL_CONVERSATION_SCHEMA

        goal_fields = START_GOAL_CONVERSATION_SCHEMA["properties"]
        operations = (
            (
                "nebula.global.projects",
                "List Nebula projects, their Act authority and available runtimes before choosing work.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "offset": {"type": "integer", "minimum": 0, "maximum": 10000}
                    },
                },
                RiskClass.LOCAL_READ,
                self.projects,
            ),
            (
                "nebula.global.project",
                "Read one project's live chats, goals, status and revisions.",
                _PROJECT,
                RiskClass.LOCAL_READ,
                self.project,
            ),
            (
                "nebula.global.goal",
                "Read one exact project's main goal before starting or resuming it.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"project_id": _ID, "session_id": _ID},
                    "required": ["project_id", "session_id"],
                },
                RiskClass.LOCAL_READ,
                self.goal,
            ),
            (
                "nebula.global.resume_project",
                "Activate one draft or paused Act project using current revisions before creating, starting, or resuming a goal.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "expected_project_revision": _REV,
                        "expected_authority_revision": _REV,
                    },
                    "required": [
                        "project_id",
                        "expected_project_revision",
                        "expected_authority_revision",
                    ],
                },
                RiskClass.WORKSPACE_WRITE,
                self.resume_project,
            ),
            (
                "nebula.global.create_goal",
                "Create one unique main goal conversation in an Act project; optionally start it after saving.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "expected_project_revision": _REV,
                        "expected_authority_revision": _REV,
                        "start": {"type": "boolean"},
                        **goal_fields,
                    },
                    "required": [
                        "project_id",
                        "expected_project_revision",
                        "expected_authority_revision",
                        "objective",
                        "completion_criteria",
                    ],
                },
                RiskClass.WORKSPACE_WRITE,
                self.create_goal,
            ),
            (
                "nebula.global.goal_action",
                "Start or resume one main goal in an active Act project after reading its revision. Activate a draft or paused project first.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "session_id": _ID,
                        "expected_authority_revision": _REV,
                        "expected_revision": _REV,
                        "action": {"type": "string", "enum": ["start", "resume"]},
                    },
                    "required": [
                        "project_id",
                        "session_id",
                        "expected_authority_revision",
                        "expected_revision",
                        "action",
                    ],
                },
                RiskClass.WORKSPACE_WRITE,
                self.goal_action,
            ),
            (
                "nebula.global.create_child_project",
                "Create or find a child of an Act project with managed workspace and default scope; optionally create and start its first goal.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "parent_project_id": _ID,
                        "expected_parent_revision": _REV,
                        "expected_authority_revision": _REV,
                        "name": {"type": "string", "minLength": 1, "maxLength": 300},
                        "description": {"type": "string", "maxLength": 2000},
                        "initial_goal": START_GOAL_CONVERSATION_SCHEMA,
                        "start_initial_goal": {"type": "boolean"},
                    },
                    "required": [
                        "parent_project_id",
                        "expected_parent_revision",
                        "expected_authority_revision",
                        "name",
                    ],
                },
                RiskClass.WORKSPACE_WRITE,
                self.create_child_project,
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
