"""Project-scoped Work records shared by the UI and built-in MCP gateway."""

from __future__ import annotations

import asyncio
import builtins
from datetime import datetime
from hashlib import sha256
import json
from threading import Lock
from typing import Literal
from uuid import NAMESPACE_URL, uuid5
from weakref import WeakKeyDictionary

from pydantic import Field, model_validator

from .domain import (
    AgentRun,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    EngagementStatus,
    NebulaModel,
    RiskClass,
    ScopePolicy,
    WorkItem,
    WorkUpdate,
)
from .chat_subagents import core_resumes_interrupted_turn, is_subagent_session
from .runtime_platform import RuntimeToolComponents
from .setup import create_engagement_with_default_scope
from .storage import ConflictError, NebulaStore, NotFoundError
from .tool_results import MAX_EXCERPT_BYTES, model_result_bytes
from .tools import (
    IdempotencyBehavior,
    ToolExecutionResult,
    ToolInvocation,
    ToolNotPermitted,
    ToolSpec,
)
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
    expected_revision: int | None = Field(default=None, ge=1)


class WorkSettingPatch(NebulaModel):
    enabled: bool
    expected_revision: int | None = Field(default=None, ge=1)


class WorkListRequest(NebulaModel):
    project_name: str | None = Field(default=None, min_length=1, max_length=300)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class WorkGetRequest(NebulaModel):
    item_id: str = Field(min_length=1, max_length=200)
    description_offset: int = Field(default=0, ge=0)


class WorkPatch(NebulaModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    priority: WorkPriority | None = None
    assignee_session_id: str | None = Field(default=None, max_length=200)
    expected_revision: int | None = Field(default=None, ge=1)


class WorkImportUpdate(NebulaModel):
    summary: str = Field(min_length=1, max_length=4_000)
    next_step: str | None = Field(default=None, max_length=2_000)
    blocker: str | None = Field(default=None, max_length=2_000)
    status: WorkStatus | None = None


class WorkImportItem(NebulaModel):
    external_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20_000)
    status: WorkStatus = "backlog"
    priority: WorkPriority = "normal"
    update: WorkImportUpdate | None = None


class WorkImportProject(NebulaModel):
    external_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20_000)
    status: EngagementStatus = EngagementStatus.DRAFT
    engagement_id: str | None = None
    parent_engagement_id: str | None = None
    workspace_path: str | None = Field(default=None, max_length=4096)
    items: list[WorkImportItem] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_items(self) -> "WorkImportProject":
        if self.engagement_id is not None and self.workspace_path is not None:
            raise ValueError(
                "workspace_path cannot change an existing project during import"
            )
        if self.engagement_id is not None and self.parent_engagement_id is not None:
            raise ValueError("use the project batch API to link an existing project")
        ids = [item.external_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("import item external IDs must be unique in each project")
        return self


class WorkImportBatch(NebulaModel):
    source: str = Field(min_length=1, max_length=100)
    projects: list[WorkImportProject] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def bounded_unique_projects(self) -> "WorkImportBatch":
        ids = [project.external_id for project in self.projects]
        if len(ids) != len(set(ids)):
            raise ValueError("import project external IDs must be unique in each batch")
        if sum(len(project.items) for project in self.projects) > 500:
            raise ValueError("an import batch may contain at most 500 items")
        return self


class WorkImportItemResult(NebulaModel):
    external_id: str
    item_id: str
    update_id: str | None = None


class WorkImportProjectResult(NebulaModel):
    external_id: str
    engagement_id: str
    items: list[WorkImportItemResult]


class WorkImportResult(NebulaModel):
    projects: list[WorkImportProjectResult]


class WorkProjectBatchUpdate(NebulaModel):
    project_ids: list[str] = Field(min_length=1, max_length=100)
    parent_engagement_id: str | None = None
    work_enabled: bool | None = None

    @model_validator(mode="after")
    def valid_batch(self) -> "WorkProjectBatchUpdate":
        if len(self.project_ids) != len(set(self.project_ids)):
            raise ValueError("project IDs must be unique")
        if self.parent_engagement_id is None and self.work_enabled is None:
            raise ValueError("parent_engagement_id or work_enabled is required")
        return self


class WorkProjectBatchResult(NebulaModel):
    updated: int


class WorkAgentActivity(NebulaModel):
    session_id: str
    engagement_id: str
    title: str
    state: Literal["working", "waiting"]
    turn_id: str


class WorkAgentItem(NebulaModel):
    id: str
    title: str
    status: WorkStatus
    last_update_at: datetime | None


class WorkAgentRosterEntry(WorkAgentActivity):
    # A recorded assignment or chat link, not proof of the agent's exact focus.
    work_item: WorkAgentItem | None = None


class WorkChangeFeed:
    """In-process signals; clients reread Core after connecting or a change."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]] = (
            set()
        )

    def subscribe(self) -> asyncio.Queue[str]:
        subscriber = (asyncio.get_running_loop(), asyncio.Queue[str](maxsize=1))
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber[1]

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        with self._lock:
            self._subscribers = {
                subscriber
                for subscriber in self._subscribers
                if subscriber[1] is not queue
            }

    def publish(self, kind: Literal["work", "projects"] = "work") -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._enqueue, queue, kind)
            except (
                RuntimeError
            ):  # diagnostic-expected: the subscriber event loop has closed.
                self.unsubscribe(queue)

    @staticmethod
    def _enqueue(queue: asyncio.Queue[str], kind: str) -> None:
        if queue.full() and queue.get_nowait() == "projects":
            kind = "projects"
        queue.put_nowait(kind)


_feeds_lock = Lock()
_feeds: WeakKeyDictionary[NebulaStore, WorkChangeFeed] = WeakKeyDictionary()


def work_change_feed(store: NebulaStore) -> WorkChangeFeed:
    with _feeds_lock:
        feed = _feeds.get(store)
        if feed is None:
            feed = WorkChangeFeed()
            _feeds[store] = feed
            store.on_chat_activity_change(feed.publish)
        return feed


class WorkService:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store
        self.changes = work_change_feed(store)

    def enabled(self, engagement_id: str) -> bool:
        return self.store.get(Engagement, engagement_id).work_enabled

    def active_agents(
        self, engagement_id: str | None = None
    ) -> list[WorkAgentActivity]:
        """The live Work roster used by the operator view and agent tool."""

        if engagement_id is not None:
            project = self.store.get(Engagement, engagement_id)
            if project.status == EngagementStatus.ARCHIVED:
                return []
        active_statuses = [
            ChatTurnStatus.QUEUED.value,
            ChatTurnStatus.ROUTING.value,
            ChatTurnStatus.WAITING_APPROVAL.value,
            ChatTurnStatus.WAITING_CALLBACK.value,
            ChatTurnStatus.FINALIZING.value,
            ChatTurnStatus.INTERRUPTED.value,
        ]
        turns = self.store.find_entities(
            ChatTurn,
            {"status": active_statuses},
            engagement_id=engagement_id,
            limit=None,
        )
        activity: list[WorkAgentActivity] = []
        project_statuses: dict[str, EngagementStatus] = {}
        for turn in turns:
            if turn.status == ChatTurnStatus.INTERRUPTED:
                recovery = turn.request_snapshot.get("recovery", {})
                if not isinstance(recovery, dict) or not (
                    recovery.get("required") or recovery.get("automatic_retry_pending")
                ):
                    continue
            session = self.store.get(ChatSession, turn.session_id)
            if engagement_id is not None and session.engagement_id != engagement_id:
                continue
            if session.metadata.get("temporary_assistant") or session.metadata.get(
                "side_chat"
            ):
                continue
            if engagement_id is None:
                project_status = project_statuses.get(session.engagement_id)
                if project_status is None:
                    project_status = self.store.get(
                        Engagement, session.engagement_id
                    ).status
                    project_statuses[session.engagement_id] = project_status
                if project_status == EngagementStatus.ARCHIVED:
                    continue
            waiting = not is_subagent_session(session) and (
                turn.status == ChatTurnStatus.WAITING_APPROVAL
                or turn.status == ChatTurnStatus.INTERRUPTED
                and not core_resumes_interrupted_turn(turn)
            )
            activity.append(
                WorkAgentActivity(
                    session_id=session.id,
                    engagement_id=session.engagement_id,
                    title=session.title,
                    state="waiting" if waiting else "working",
                    turn_id=turn.id,
                )
            )
        return activity

    def agent_roster(self, engagement_id: str) -> list[WorkAgentRosterEntry]:
        """Read this project's live agents and their recorded open Work links."""

        activity = self.active_agents(engagement_id)
        if not activity:
            return []
        items: list[WorkItem] = []
        offset = 0
        while page := self.store.list_entities(
            WorkItem,
            engagement_id=engagement_id,
            offset=offset,
            limit=1_000,
            newest_first=True,
        ):
            items.extend(page)
            offset += len(page)
        open_items = [item for item in items if item.status != "done"]
        roster: list[WorkAgentRosterEntry] = []
        for agent in activity:
            linked = next(
                (
                    item
                    for item in open_items
                    if item.assignee_session_id == agent.session_id
                    or item.source_kind == "chat"
                    and item.source_id == agent.session_id
                ),
                None,
            )
            roster.append(
                WorkAgentRosterEntry(
                    **agent.model_dump(),
                    work_item=(
                        WorkAgentItem(
                            id=linked.id,
                            title=linked.title,
                            status=linked.status,
                            last_update_at=linked.last_update_at,
                        )
                        if linked is not None
                        else None
                    ),
                )
            )
        return roster

    def set_enabled(
        self, engagement_id: str, enabled: bool, *, expected_revision: int | None = None
    ) -> Engagement:
        for _ in range(3):
            project = self.store.get(Engagement, engagement_id)
            if expected_revision is not None and project.revision != expected_revision:
                raise ConflictError(
                    "project Work setting changed; read its current revision"
                )
            if project.work_enabled == enabled:
                return project
            try:
                updated = self.store.update(
                    Engagement,
                    engagement_id,
                    {"work_enabled": enabled},
                    expected_revision=project.revision,
                )
                self.changes.publish("projects")
                return updated
            except ConflictError:
                if expected_revision is not None:
                    raise
                # diagnostic-expected: concurrent settings edits are retried.
                continue
        raise ConflictError("project Work setting changed concurrently; retry")

    def _session(
        self,
        engagement_id: str,
        session_id: str | None,
        *,
        allow_parent: bool = False,
    ) -> None:
        if session_id is None:
            return
        session = self.store.get(ChatSession, session_id)
        if session.engagement_id == engagement_id:
            return
        if (
            allow_parent
            and self.store.get(Engagement, engagement_id).parent_engagement_id
            == session.engagement_id
        ):
            return
        raise NotFoundError("conversation does not belong to this project")

    def agent_project(self, engagement_id: str, project_name: str | None = None) -> str:
        """Resolve this project or one directly linked child for a Work tool."""
        if not self.enabled(engagement_id):
            raise ValueError("Work is off for this project")
        if not project_name:
            return engagement_id
        projects = self.store.find_entities(
            Engagement,
            {"name": project_name, "parent_engagement_id": engagement_id},
            limit=2,
        )
        if not projects:
            raise NotFoundError("linked Work project not found")
        if len(projects) != 1:
            raise ConflictError("linked Work project name is ambiguous")
        if not projects[0].work_enabled:
            raise ValueError("Work is off for this project")
        return projects[0].id

    def agent_item_project(self, engagement_id: str, item_id: str) -> str:
        """Allow a Work tool to use an item in its project or a direct child."""
        if not self.enabled(engagement_id):
            raise ValueError("Work is off for this project")
        item = self.store.get(WorkItem, item_id)
        if item.engagement_id == engagement_id:
            return engagement_id
        project = self.store.get(Engagement, item.engagement_id)
        if project.parent_engagement_id != engagement_id:
            raise NotFoundError("work item does not belong to this project")
        if not project.work_enabled:
            raise ValueError("Work is off for this project")
        return project.id

    def get(self, engagement_id: str, item_id: str) -> WorkItem:
        item = self.store.get(WorkItem, item_id)
        if item.engagement_id != engagement_id:
            raise NotFoundError("work item does not belong to this project")
        return item

    def list(
        self, engagement_id: str, *, offset: int = 0, limit: int = 500
    ) -> list[WorkItem]:
        self.store.get(Engagement, engagement_id)
        return self.store.list_entities(
            WorkItem,
            engagement_id=engagement_id,
            offset=offset,
            limit=limit,
            newest_first=True,
        )

    def agent_list_page(
        self,
        engagement_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
        envelope: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """A byte-fitted page of Work summaries for either agent transport."""

        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset must be nonnegative and limit between 1 and 100")
        envelope = envelope or {}
        rows = self.list(engagement_id, offset=offset, limit=limit + 1)
        items: list[dict[str, object]] = []
        for item in rows[:limit]:
            summary = item.model_dump(mode="json", exclude={"description"})
            summary["description_preview"] = item.description[:256]
            summary["description_truncated"] = len(item.description) > 256
            next_count = len(items) + 1
            candidate = {
                **envelope,
                "items": [*items, summary],
                "offset": offset,
                "next_offset": offset + next_count if len(rows) > next_count else None,
            }
            if model_result_bytes(candidate) > MAX_EXCERPT_BYTES:
                break
            items.append(summary)
        if rows and not items:
            raise ValueError("Work item summary exceeds the model-delivery bound")
        return {
            **envelope,
            "items": items,
            "offset": offset,
            "next_offset": offset + len(items) if len(rows) > len(items) else None,
        }

    def agent_get_item(
        self,
        engagement_id: str,
        item_id: str,
        *,
        description_offset: int = 0,
        envelope: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Read a Work item with its description in byte-fitted chunks."""

        item = self.get(engagement_id, item_id)
        envelope = envelope or {}
        description = item.description
        if not 0 <= description_offset <= len(description):
            raise ValueError("description_offset is outside this item's description")
        base = item.model_dump(mode="json", exclude={"description"})

        def chunk(length: int) -> dict[str, object]:
            end = description_offset + length
            return {
                **envelope,
                "item": {**base, "description": description[description_offset:end]},
                "description_offset": description_offset,
                "next_description_offset": end if end < len(description) else None,
            }

        remaining = min(4096, len(description) - description_offset)
        low, high = 0, remaining
        while low < high:
            middle = (low + high + 1) // 2
            if model_result_bytes(chunk(middle)) <= MAX_EXCERPT_BYTES:
                low = middle
            else:
                high = middle - 1
        result = chunk(low)
        if model_result_bytes(result) > MAX_EXCERPT_BYTES or remaining > 0 and low == 0:
            raise ValueError("Work item metadata exceeds the model-delivery bound")
        return result

    def updates(
        self, engagement_id: str, item_id: str, *, limit: int = 200
    ) -> builtins.list[WorkUpdate]:
        self.get(engagement_id, item_id)
        return self.store.find_entities(
            WorkUpdate,
            {"item_id": item_id},
            engagement_id=engagement_id,
            limit=limit,
            newest_first=True,
        )

    def agent_updates_page(
        self,
        engagement_id: str,
        item_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
        envelope: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Page every saved check-in with compact, byte-fitted summaries."""

        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset must be nonnegative and limit between 1 and 100")
        self.get(engagement_id, item_id)
        envelope = envelope or {}
        rows = self.store.find_entities(
            WorkUpdate,
            {"item_id": item_id},
            engagement_id=engagement_id,
            offset=offset,
            limit=limit + 1,
            newest_first=True,
        )
        updates: list[dict[str, object]] = []
        for update in rows[:limit]:
            summary = update.model_dump(
                mode="json", exclude={"summary", "next_step", "blocker"}
            )
            summary["summary_preview"] = update.summary[:256]
            summary["summary_truncated"] = len(update.summary) > 256
            next_count = len(updates) + 1
            candidate = {
                **envelope,
                "updates": [*updates, summary],
                "offset": offset,
                "next_offset": offset + next_count if len(rows) > next_count else None,
            }
            if model_result_bytes(candidate) > MAX_EXCERPT_BYTES:
                break
            updates.append(summary)
        if rows and not updates:
            raise ValueError("Work check-in summary exceeds the model-delivery bound")
        return {
            **envelope,
            "updates": updates,
            "offset": offset,
            "next_offset": offset + len(updates) if len(rows) > len(updates) else None,
        }

    def agent_get_update(
        self,
        engagement_id: str,
        item_id: str,
        update_id: str,
        *,
        field: Literal["summary", "next_step", "blocker"] = "summary",
        content_offset: int = 0,
        envelope: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Read exact check-in text in byte-fitted chunks."""

        self.get(engagement_id, item_id)
        update = self.store.get(WorkUpdate, update_id)
        if update.engagement_id != engagement_id or update.item_id != item_id:
            raise NotFoundError("work check-in does not belong to this item")
        if field not in {"summary", "next_step", "blocker"}:
            raise ValueError("field must be summary, next_step, or blocker")
        content = getattr(update, field)
        if not 0 <= content_offset <= len(content or ""):
            raise ValueError("content_offset is outside this check-in field")
        envelope = envelope or {}
        base = update.model_dump(
            mode="json", exclude={"summary", "next_step", "blocker"}
        )

        def chunk(length: int) -> dict[str, object]:
            end = content_offset + length
            return {
                **envelope,
                "update": {
                    **base,
                    field: content[content_offset:end] if content is not None else None,
                },
                "field": field,
                "content_offset": content_offset,
                "content_length": len(content or ""),
                "next_content_offset": end
                if content is not None and end < len(content)
                else None,
            }

        remaining = min(4096, len(content or "") - content_offset)
        low, high = 0, remaining
        while low < high:
            middle = (low + high + 1) // 2
            if model_result_bytes(chunk(middle)) <= MAX_EXCERPT_BYTES:
                low = middle
            else:
                high = middle - 1
        result = chunk(low)
        if model_result_bytes(result) > MAX_EXCERPT_BYTES or remaining > 0 and low == 0:
            raise ValueError("Work check-in metadata exceeds the model-delivery bound")
        return result

    def create(
        self,
        engagement_id: str,
        data: WorkCreate,
        *,
        actor_id: str,
        source_session_id: str | None = None,
        notify: bool = True,
    ) -> WorkItem:
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
            data = data.model_copy(
                update={
                    "source_kind": "chat",
                    "source_id": source_session_id,
                    "assignee_session_id": data.assignee_session_id
                    or source_session_id,
                }
            )
        item_id = (
            str(
                uuid5(
                    NAMESPACE_URL,
                    f"nebula:work:{engagement_id}:{actor_id}:{data.request_id}",
                )
            )
            if data.request_id
            else None
        )
        if item_id:
            try:
                return self.get(engagement_id, item_id)
            except NotFoundError:
                # diagnostic-expected: an unused request ID has no existing item.
                pass
        item = (
            WorkItem(
                id=item_id,
                engagement_id=engagement_id,
                title=data.title,
                description=data.description,
                status=data.status,
                priority=data.priority,
                assignee_session_id=data.assignee_session_id,
                source_kind=data.source_kind,
                source_id=data.source_id,
            )
            if item_id
            else WorkItem(
                engagement_id=engagement_id,
                title=data.title,
                description=data.description,
                status=data.status,
                priority=data.priority,
                assignee_session_id=data.assignee_session_id,
                source_kind=data.source_kind,
                source_id=data.source_id,
            )
        )
        try:
            created = self.store.create(item)
            if notify:
                self.changes.publish()
            return created
        except ConflictError:
            if item_id:
                return self.get(engagement_id, item_id)
            raise

    def patch(self, engagement_id: str, item_id: str, data: WorkPatch) -> WorkItem:
        changes = {
            key: value
            for key, value in data.model_dump(
                exclude_unset=True, exclude={"expected_revision"}
            ).items()
            if value is not None or key == "assignee_session_id"
        }
        self._session(engagement_id, changes.get("assignee_session_id"))
        for _ in range(1 if data.expected_revision is not None else 3):
            item = self.get(engagement_id, item_id)
            if (
                data.expected_revision is not None
                and item.revision != data.expected_revision
            ):
                raise ConflictError("work item changed; read its current revision")
            if not changes:
                return item
            try:
                updated = self.store.update(
                    WorkItem, item_id, changes, expected_revision=item.revision
                )
                self.changes.publish()
                return updated
            except ConflictError:
                if data.expected_revision is not None:
                    raise
                # diagnostic-expected: a concurrent edit retries with the latest revision.
                continue
        raise ConflictError("work item changed concurrently; retry")

    def check_in(
        self,
        engagement_id: str,
        item_id: str,
        data: WorkCheckIn,
        *,
        actor_kind: Literal["operator", "agent", "import"],
        actor_id: str,
        source_session_id: str | None = None,
        source_turn_id: str | None = None,
        source_run_id: str | None = None,
        allow_parent_source_session: bool = False,
        notify: bool = True,
    ) -> WorkUpdate:
        self._session(
            engagement_id,
            source_session_id,
            allow_parent=allow_parent_source_session,
        )
        source_engagement_id = (
            self.store.get(ChatSession, source_session_id).engagement_id
            if source_session_id is not None
            else None
        )
        update_id = (
            str(
                uuid5(
                    NAMESPACE_URL,
                    f"nebula:work-update:{engagement_id}:{actor_id}:{data.request_id}",
                )
            )
            if data.request_id
            else None
        )
        if update_id:
            try:
                existing = self.store.get(WorkUpdate, update_id)
                if (
                    existing.engagement_id == engagement_id
                    and existing.item_id == item_id
                ):
                    return existing
                raise ConflictError("request_id is already used for another work item")
            except NotFoundError:
                # diagnostic-expected: an unused request ID has no existing update.
                pass
        for _ in range(3):
            item = self.get(engagement_id, item_id)
            if (
                data.expected_revision is not None
                and item.revision != data.expected_revision
            ):
                raise ConflictError("work item changed; read its current revision")
            status = "blocked" if data.blocker else (data.status or item.status)
            update = (
                WorkUpdate(
                    id=update_id,
                    engagement_id=engagement_id,
                    item_id=item_id,
                    summary=data.summary,
                    next_step=data.next_step,
                    blocker=data.blocker,
                    status=status,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    source_session_id=source_session_id,
                    source_engagement_id=source_engagement_id,
                    source_turn_id=source_turn_id,
                    source_run_id=source_run_id,
                )
                if update_id
                else WorkUpdate(
                    engagement_id=engagement_id,
                    item_id=item_id,
                    summary=data.summary,
                    next_step=data.next_step,
                    blocker=data.blocker,
                    status=status,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    source_session_id=source_session_id,
                    source_engagement_id=source_engagement_id,
                    source_turn_id=source_turn_id,
                    source_run_id=source_run_id,
                )
            )
            try:
                with self.store.transaction() as transaction:
                    transaction.add(update)
                    transaction.update(
                        WorkItem,
                        item.id,
                        {"status": status, "last_update_at": update.created_at},
                        expected_revision=item.revision,
                    )
                if notify:
                    self.changes.publish()
                return update
            except ConflictError:
                # diagnostic-expected: concurrent check-ins retry after an ID lookup.
                if update_id:
                    try:
                        return self.store.get(WorkUpdate, update_id)
                    except NotFoundError:
                        # diagnostic-expected: the conflicting write was another item edit.
                        pass
                if data.expected_revision is not None:
                    raise
        raise ConflictError("work item changed concurrently; retry")

    def import_batch(self, batch: WorkImportBatch) -> WorkImportResult:
        """Create source-identified projects and Work without overwriting later edits."""

        actor_id = f"import:{batch.source}"
        results: list[WorkImportProjectResult] = []
        for project in batch.projects:
            if project.engagement_id is not None:
                engagement = self.store.get(Engagement, project.engagement_id)
            else:
                identity = json.dumps(
                    [batch.source, project.external_id], separators=(",", ":")
                )
                project_id = str(
                    uuid5(NAMESPACE_URL, f"nebula:work-import-project:{identity}")
                )
                try:
                    engagement = self.store.get(Engagement, project_id)
                except NotFoundError:
                    # diagnostic-expected: this source project has not been imported yet.
                    candidate = Engagement(
                        id=project_id,
                        name=project.name,
                        description=project.description,
                        status=project.status,
                        parent_engagement_id=project.parent_engagement_id,
                        workspace_path=project.workspace_path,
                        metadata={
                            "work_import": {
                                "source": batch.source,
                                "external_id": project.external_id,
                            }
                        },
                    )
                    if project.parent_engagement_id is not None:
                        self.store.get(Engagement, project.parent_engagement_id)
                    try:
                        engagement = create_engagement_with_default_scope(
                            self.store, candidate
                        )
                    except ConflictError:
                        # diagnostic-expected: another importer created this source project.
                        engagement = self.store.get(Engagement, project_id)
                if engagement.metadata.get("work_import") != {
                    "source": batch.source,
                    "external_id": project.external_id,
                }:
                    raise ConflictError(
                        "import project ID belongs to a different source"
                    )
            item_results: list[WorkImportItemResult] = []
            for entry in project.items:
                identity = json.dumps(
                    [project.external_id, entry.external_id], separators=(",", ":")
                )
                request_id = sha256(identity.encode("utf-8")).hexdigest()
                item = self.create(
                    engagement.id,
                    WorkCreate(
                        title=entry.title,
                        description=entry.description,
                        status=entry.status,
                        priority=entry.priority,
                        source_kind="import",
                        source_id=entry.external_id,
                        request_id=request_id,
                    ),
                    actor_id=actor_id,
                    notify=False,
                )
                update_id = None
                if entry.update is not None:
                    update = self.check_in(
                        engagement.id,
                        item.id,
                        WorkCheckIn(
                            summary=entry.update.summary,
                            next_step=entry.update.next_step,
                            blocker=entry.update.blocker,
                            status=entry.update.status,
                            request_id=request_id,
                        ),
                        actor_kind="import",
                        actor_id=actor_id,
                        notify=False,
                    )
                    update_id = update.id
                item_results.append(
                    WorkImportItemResult(
                        external_id=entry.external_id,
                        item_id=item.id,
                        update_id=update_id,
                    )
                )
            results.append(
                WorkImportProjectResult(
                    external_id=project.external_id,
                    engagement_id=engagement.id,
                    items=item_results,
                )
            )
        self.changes.publish("projects")
        return WorkImportResult(projects=results)

    def update_projects(self, batch: WorkProjectBatchUpdate) -> WorkProjectBatchResult:
        """Link existing projects in bounded, atomic batches."""

        parent_id = batch.parent_engagement_id
        if parent_id is not None:
            self.store.get(Engagement, parent_id)
        projects = [
            self.store.get(Engagement, project_id) for project_id in batch.project_ids
        ]
        if parent_id is not None:
            for project in projects:
                if project.id == parent_id:
                    raise ConflictError("a project cannot be its own parent")
                ancestor_id: str | None = parent_id
                seen: set[str] = set()
                while ancestor_id is not None:
                    if ancestor_id == project.id or ancestor_id in seen:
                        raise ConflictError("project hierarchy would contain a cycle")
                    seen.add(ancestor_id)
                    ancestor_id = self.store.get(
                        Engagement, ancestor_id
                    ).parent_engagement_id
        updated = 0
        with self.store.transaction() as transaction:
            for project in projects:
                changes: dict[str, str | bool] = {}
                if parent_id is not None and project.parent_engagement_id != parent_id:
                    changes["parent_engagement_id"] = parent_id
                if (
                    batch.work_enabled is not None
                    and project.work_enabled != batch.work_enabled
                ):
                    changes["work_enabled"] = batch.work_enabled
                if changes:
                    transaction.update(
                        Engagement,
                        project.id,
                        changes,
                        expected_revision=project.revision,
                    )
                    updated += 1
        if updated:
            self.changes.publish("projects")
        return WorkProjectBatchResult(updated=updated)


WORK_AVAILABLE_INSTRUCTIONS = (
    "Project Work is available. Use work_list to find tasks, work_create to create "
    "a task, and work_check_in to post an update when appropriate. Start work_list "
    "without offset; repeat it with the returned next_offset until next_offset is "
    "null. For a linked child project, repeat project_name on each page. Use "
    "work_get with an item_id for the full description; repeat it with each "
    "next_description_offset until null."
)
WORK_ROSTER_INSTRUCTIONS = (
    "Use work_agents to see which conversations are working or waiting in this "
    "project and their linked open Work items. An unlinked conversation has no "
    "recorded task; a link is an assignment, not proof of its exact current focus."
)


class WorkRosterBroker:
    def __init__(self, service: WorkService) -> None:
        self.service = service

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: object | None = None,
    ) -> ToolExecutionResult:
        del approval
        if invocation.tool_name != "work_agents":
            raise ValueError("unknown Work roster tool")
        if invocation.engagement_id != scope.engagement_id:
            raise ToolNotPermitted(
                "Work roster is limited to this project", rule="work.roster.project"
            )
        if invocation.chat_session_id is not None:
            session = self.service.store.get(ChatSession, invocation.chat_session_id)
            if session.engagement_id != invocation.engagement_id:
                raise ToolNotPermitted(
                    "Work roster is limited to this project", rule="work.roster.project"
                )
        return ToolExecutionResult(
            output={
                "agents": [
                    agent.model_dump(mode="json")
                    for agent in self.service.agent_roster(invocation.engagement_id)
                ]
            }
        )


def work_roster_components(
    service: WorkService,
    engagement_id: str,
    workspace: Path,
    scope: ScopePolicy | None = None,
) -> RuntimeToolComponents:
    return RuntimeToolComponents(
        broker=WorkRosterBroker(service),
        scope=scope
        or ScopePolicy(
            id=str(uuid5(NAMESPACE_URL, f"nebula:skill-scope:{engagement_id}")),
            engagement_id=engagement_id,
        ),
        workspace=workspace,
        specs={
            "work_agents": ToolSpec(
                name="work_agents",
                description=(
                    "List this project's live working and waiting agents, with their "
                    "linked open Work item when recorded. Available even when Work "
                    "posting and peer messaging are off."
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                idempotency=IdempotencyBehavior.SAFE,
                budget_class="artifact_query",
            )
        },
        runtime_digest="work-roster-v1",
    )


class WorkBroker:
    def __init__(self, service: WorkService) -> None:
        self.service = service

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: object | None = None,
    ) -> ToolExecutionResult:
        del scope, approval
        if not self.service.enabled(invocation.engagement_id):
            raise ValueError("Work is off for this project")
        actor_id = invocation.chat_session_id or invocation.run_id
        if invocation.tool_name == "work_list":
            list_request = WorkListRequest.model_validate(invocation.arguments)
            target_id = self.service.agent_project(
                invocation.engagement_id,
                list_request.project_name,
            )
            return ToolExecutionResult(
                output=self.service.agent_list_page(
                    target_id, offset=list_request.offset, limit=list_request.limit
                )
            )
        if invocation.tool_name == "work_get":
            get_request = WorkGetRequest.model_validate(invocation.arguments)
            target_id = self.service.agent_item_project(
                invocation.engagement_id, get_request.item_id
            )
            return ToolExecutionResult(
                output=self.service.agent_get_item(
                    target_id,
                    get_request.item_id,
                    description_offset=get_request.description_offset,
                )
            )
        if invocation.tool_name == "work_create":
            target_id = self.service.agent_project(
                invocation.engagement_id,
                invocation.arguments.get("project_name"),
            )
            data = WorkCreate.model_validate(
                {k: v for k, v in invocation.arguments.items() if k != "project_name"}
            )
            item = self.service.create(
                target_id,
                data,
                actor_id=actor_id,
                source_session_id=(
                    invocation.chat_session_id
                    if target_id == invocation.engagement_id
                    else None
                ),
            )
            return ToolExecutionResult(output={"item": item.model_dump(mode="json")})
        if invocation.tool_name == "work_check_in":
            item_id = str(invocation.arguments["item_id"])
            target_id = self.service.agent_item_project(
                invocation.engagement_id, item_id
            )
            check_in_data = WorkCheckIn.model_validate(
                {
                    key: value
                    for key, value in invocation.arguments.items()
                    if key != "item_id"
                }
            )
            update = self.service.check_in(
                target_id,
                item_id,
                check_in_data,
                actor_kind="agent",
                actor_id=actor_id,
                source_session_id=invocation.chat_session_id,
                source_turn_id=invocation.chat_turn_id,
                source_run_id=None if invocation.chat_session_id else invocation.run_id,
                allow_parent_source_session=True,
            )
            return ToolExecutionResult(
                output={"update": update.model_dump(mode="json")}
            )
        raise ValueError("unknown Work tool")


def work_components(
    service: WorkService,
    engagement_id: str,
    workspace: Path,
    scope: ScopePolicy | None = None,
) -> RuntimeToolComponents:
    fields = {
        "work_list": (
            "List Work summaries. Start without offset, then call again with each returned next_offset until it is null; repeat project_name for a linked child. Use work_get for a full description.",
            {
                "project_name": {"type": "string", "minLength": 1, "maxLength": 300},
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Use the previous result's next_offset; omit for the first page.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "Maximum items requested; Core may return fewer to fit the byte bound.",
                },
            },
            [],
        ),
        "work_get": (
            "Read one Work item. Start with item_id only, then repeat with each returned next_description_offset as description_offset until it is null. Concatenate description chunks in order.",
            {
                "item_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "description_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Use the previous result's next_description_offset; omit for the first chunk.",
                },
            },
            ["item_id"],
        ),
        "work_create": (
            "Create a Work item. Give request_id for retry safety.",
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 300},
                "description": {"type": "string", "maxLength": 20000},
                "status": {
                    "type": "string",
                    "enum": [
                        "backlog",
                        "ready",
                        "in_progress",
                        "blocked",
                        "review",
                        "done",
                    ],
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "urgent"],
                },
                "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "project_name": {"type": "string", "minLength": 1, "maxLength": 300},
            },
            ["title"],
        ),
        "work_check_in": (
            "Post progress to this project's item or a linked child project's item. Give request_id for retry safety.",
            {
                "item_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                "next_step": {"type": "string", "maxLength": 2000},
                "blocker": {"type": "string", "maxLength": 2000},
                "status": {
                    "type": "string",
                    "enum": [
                        "backlog",
                        "ready",
                        "in_progress",
                        "blocked",
                        "review",
                        "done",
                    ],
                },
                "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            ["item_id", "summary"],
        ),
    }
    specs = {
        name: ToolSpec(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            risk_class=RiskClass.LOCAL_READ,
            idempotency=IdempotencyBehavior.SAFE
            if name in {"work_list", "work_get"}
            else IdempotencyBehavior.KEY_REQUIRED,
            budget_class="artifact_query",
        )
        for name, (description, properties, required) in fields.items()
    }
    return RuntimeToolComponents(
        broker=WorkBroker(service),
        scope=scope
        or ScopePolicy(
            id=str(uuid5(NAMESPACE_URL, f"nebula:skill-scope:{engagement_id}")),
            engagement_id=engagement_id,
        ),
        workspace=workspace,
        specs=specs,
        runtime_digest="work-v2",
    )
