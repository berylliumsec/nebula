"""Project-scoped Work records shared by the UI and built-in MCP gateway."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from .domain import AgentRun, ChatSession, Engagement, NebulaModel, RiskClass, ScopePolicy, WorkItem, WorkUpdate, utc_now
from .runtime_platform import RuntimeToolComponents
from .storage import ConflictError, NebulaStore, NotFoundError
from .tools import IdempotencyBehavior, ToolExecutionResult, ToolInvocation, ToolSpec
from pathlib import Path

WorkStatus = Literal["backlog", "ready", "in_progress", "blocked", "review", "done"]
WorkPriority = Literal["low", "normal", "high", "urgent"]


class WorkCreate(NebulaModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20_000)
    status: WorkStatus = "backlog"
    priority: WorkPriority = "normal"
    assignee_session_id: str | None = Field(default=None, max_length=200)
    source_kind: Literal["chat", "mission", "manual", "import"] = "manual"
    source_id: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


class WorkCheckIn(NebulaModel):
    summary: str = Field(min_length=1, max_length=4_000)
    next_step: str | None = Field(default=None, max_length=2_000)
    blocker: str | None = Field(default=None, max_length=2_000)
    status: WorkStatus | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


class WorkPatch(NebulaModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    priority: WorkPriority | None = None
    assignee_session_id: str | None = Field(default=None, max_length=200)


class WorkService:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    def enabled(self, engagement_id: str) -> bool:
        return self.store.get(Engagement, engagement_id).work_enabled

    def set_enabled(self, engagement_id: str, enabled: bool) -> Engagement:
        for _ in range(3):
            project = self.store.get(Engagement, engagement_id)
            if project.work_enabled == enabled:
                return project
            try:
                return self.store.update(Engagement, engagement_id, {"work_enabled": enabled}, expected_revision=project.revision)
            except ConflictError:
                continue
        raise ConflictError("project Work setting changed concurrently; retry")

    def _session(self, engagement_id: str, session_id: str | None) -> None:
        if session_id is None:
            return
        session = self.store.get(ChatSession, session_id)
        if session.engagement_id != engagement_id:
            raise NotFoundError("conversation does not belong to this project")

    def get(self, engagement_id: str, item_id: str) -> WorkItem:
        item = self.store.get(WorkItem, item_id)
        if item.engagement_id != engagement_id:
            raise NotFoundError("work item does not belong to this project")
        return item

    def list(self, engagement_id: str, *, limit: int = 500) -> list[WorkItem]:
        self.store.get(Engagement, engagement_id)
        return self.store.list_entities(WorkItem, engagement_id=engagement_id, limit=limit, newest_first=True)

    def updates(self, engagement_id: str, item_id: str, *, limit: int = 200) -> list[WorkUpdate]:
        self.get(engagement_id, item_id)
        return self.store.find_entities(WorkUpdate, {"item_id": item_id}, engagement_id=engagement_id, limit=limit, newest_first=True)

    def create(self, engagement_id: str, data: WorkCreate, *, actor_id: str, source_session_id: str | None = None) -> WorkItem:
        self.store.get(Engagement, engagement_id)
        self._session(engagement_id, data.assignee_session_id)
        self._session(engagement_id, source_session_id)
        if data.source_kind == "chat" and data.source_id:
            self._session(engagement_id, data.source_id)
        if data.source_kind == "mission" and data.source_id:
            run = self.store.get(AgentRun, data.source_id)
            if run.engagement_id != engagement_id:
                raise NotFoundError("mission does not belong to this project")
        if source_session_id is not None:
            data = data.model_copy(update={"source_kind": "chat", "source_id": source_session_id,
                                           "assignee_session_id": data.assignee_session_id or source_session_id})
        item_id = str(uuid5(NAMESPACE_URL, f"nebula:work:{engagement_id}:{actor_id}:{data.request_id}")) if data.request_id else None
        if item_id:
            try:
                return self.get(engagement_id, item_id)
            except NotFoundError:
                pass
        item = WorkItem(id=item_id, engagement_id=engagement_id, title=data.title, description=data.description,
                        status=data.status, priority=data.priority, assignee_session_id=data.assignee_session_id,
                        source_kind=data.source_kind, source_id=data.source_id) if item_id else WorkItem(
                            engagement_id=engagement_id, title=data.title, description=data.description,
                            status=data.status, priority=data.priority, assignee_session_id=data.assignee_session_id,
                            source_kind=data.source_kind, source_id=data.source_id)
        try:
            return self.store.create(item)
        except ConflictError:
            if item_id:
                return self.get(engagement_id, item_id)
            raise

    def patch(self, engagement_id: str, item_id: str, data: WorkPatch) -> WorkItem:
        changes = {key: value for key, value in data.model_dump(exclude_unset=True).items()
                   if value is not None or key == "assignee_session_id"}
        self._session(engagement_id, changes.get("assignee_session_id"))
        for _ in range(3):
            item = self.get(engagement_id, item_id)
            if not changes:
                return item
            try:
                return self.store.update(WorkItem, item_id, changes, expected_revision=item.revision)
            except ConflictError:
                continue
        raise ConflictError("work item changed concurrently; retry")

    def check_in(self, engagement_id: str, item_id: str, data: WorkCheckIn, *, actor_kind: Literal["operator", "agent", "import"],
                 actor_id: str, source_session_id: str | None = None, source_turn_id: str | None = None,
                 source_run_id: str | None = None) -> WorkUpdate:
        self._session(engagement_id, source_session_id)
        update_id = str(uuid5(NAMESPACE_URL, f"nebula:work-update:{engagement_id}:{actor_id}:{data.request_id}")) if data.request_id else None
        if update_id:
            try:
                existing = self.store.get(WorkUpdate, update_id)
                if existing.engagement_id == engagement_id and existing.item_id == item_id:
                    return existing
                raise ConflictError("request_id is already used for another work item")
            except NotFoundError:
                pass
        for _ in range(3):
            item = self.get(engagement_id, item_id)
            status = "blocked" if data.blocker else (data.status or item.status)
            update = WorkUpdate(id=update_id, engagement_id=engagement_id, item_id=item_id, summary=data.summary,
                                next_step=data.next_step, blocker=data.blocker, status=status, actor_kind=actor_kind,
                                actor_id=actor_id, source_session_id=source_session_id, source_turn_id=source_turn_id,
                                source_run_id=source_run_id) if update_id else WorkUpdate(
                                    engagement_id=engagement_id, item_id=item_id, summary=data.summary,
                                    next_step=data.next_step, blocker=data.blocker, status=status, actor_kind=actor_kind,
                                    actor_id=actor_id, source_session_id=source_session_id, source_turn_id=source_turn_id,
                                    source_run_id=source_run_id)
            try:
                with self.store.transaction() as transaction:
                    transaction.add(update)
                    transaction.update(WorkItem, item.id, {"status": status, "last_update_at": update.created_at}, expected_revision=item.revision)
                return update
            except ConflictError:
                if update_id:
                    try:
                        return self.store.get(WorkUpdate, update_id)
                    except NotFoundError:
                        pass
        raise ConflictError("work item changed concurrently; retry")

    def last_agent_update(self, engagement_id: str, actor_id: str, *, mission: bool = False) -> datetime | None:
        key = "source_run_id" if mission else "source_session_id"
        updates = self.store.find_entities(WorkUpdate, {key: actor_id, "actor_kind": "agent"},
                                           engagement_id=engagement_id, limit=1, newest_first=True)
        return updates[0].created_at if updates else None

    def update_due(self, engagement_id: str, actor_id: str, started_at: datetime, *, mission: bool = False, now: datetime | None = None) -> bool:
        if not self.enabled(engagement_id):
            return False
        latest = self.last_agent_update(engagement_id, actor_id, mission=mission)
        return (now or utc_now()) - max(started_at, latest or started_at) >= timedelta(minutes=20)


WORK_ROUTING_INSTRUCTIONS = """
Project Work is enabled. Use work_list to find or reuse a work item, work_create
when starting substantial work, and work_check_in to report factual progress,
next steps, and blockers. Check in at meaningful changes and about every 30
minutes of active work. A Work item tracks progress; it does not prove a claim.
"""


class WorkBroker:
    def __init__(self, service: WorkService) -> None:
        self.service = service

    async def execute(self, invocation: ToolInvocation, scope: ScopePolicy, *, approval: object | None = None) -> ToolExecutionResult:
        del scope, approval
        if not self.service.enabled(invocation.engagement_id):
            raise ValueError("Work is off for this project")
        actor_id = invocation.chat_session_id or invocation.run_id
        if invocation.tool_name == "work_list":
            return ToolExecutionResult(output={"items": [item.model_dump(mode="json") for item in self.service.list(invocation.engagement_id)]})
        if invocation.tool_name == "work_create":
            data = WorkCreate.model_validate(invocation.arguments)
            item = self.service.create(invocation.engagement_id, data, actor_id=actor_id,
                                       source_session_id=invocation.chat_session_id)
            return ToolExecutionResult(output={"item": item.model_dump(mode="json")})
        if invocation.tool_name == "work_check_in":
            item_id = str(invocation.arguments["item_id"])
            data = WorkCheckIn.model_validate({key: value for key, value in invocation.arguments.items() if key != "item_id"})
            update = self.service.check_in(
                invocation.engagement_id, item_id, data, actor_kind="agent", actor_id=actor_id,
                source_session_id=invocation.chat_session_id, source_turn_id=invocation.chat_turn_id,
                source_run_id=None if invocation.chat_session_id else invocation.run_id,
            )
            return ToolExecutionResult(output={"update": update.model_dump(mode="json")})
        raise ValueError("unknown Work tool")


def work_components(service: WorkService, engagement_id: str, workspace: Path, scope: ScopePolicy | None = None) -> RuntimeToolComponents:
    fields = {
        "work_list": ("List this project's Work items before creating a new one.", {}, []),
        "work_create": ("Create a Work item. Give request_id for retry safety.", {
            "title": {"type": "string", "minLength": 1, "maxLength": 300},
            "description": {"type": "string", "maxLength": 20000},
            "status": {"type": "string", "enum": ["backlog", "ready", "in_progress", "blocked", "review", "done"]},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
        }, ["title"]),
        "work_check_in": ("Post progress, next step, and blocker. Give request_id for retry safety.", {
            "item_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            "next_step": {"type": "string", "maxLength": 2000},
            "blocker": {"type": "string", "maxLength": 2000},
            "status": {"type": "string", "enum": ["backlog", "ready", "in_progress", "blocked", "review", "done"]},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
        }, ["item_id", "summary"]),
    }
    specs = {
        name: ToolSpec(name=name, description=description,
                       input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
                       output_schema={"type": "object", "additionalProperties": True},
                       risk_class=RiskClass.LOCAL_READ,
                       idempotency=IdempotencyBehavior.SAFE if name == "work_list" else IdempotencyBehavior.KEY_REQUIRED,
                       budget_class="artifact_query")
        for name, (description, properties, required) in fields.items()
    }
    return RuntimeToolComponents(
        broker=WorkBroker(service),
        scope=scope or ScopePolicy(id=str(uuid5(NAMESPACE_URL, f"nebula:skill-scope:{engagement_id}")), engagement_id=engagement_id),
        workspace=workspace, specs=specs, runtime_digest="work-v1",
    )
