"""Durable, Core-enforced plan gate for chat turns and delegated rounds."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator

from .domain import (
    ChatPlanDirective,
    ChatSession,
    ChatTaskPlan,
    ChatTurn,
    NebulaModel,
)
from .storage import NebulaStore, NotFoundError
from .tool_results import ToolOutputAccessError, WorkspaceOutputService
from .tools import ToolExecutionResult, ToolInvocation
from .domain import Approval, ScopePolicy


DISCOVERY_TOOL_NAMES = frozenset(
    {"workspace.find", "workspace.search", "workspace.read"}
)
MAX_DISCOVERY_CALLS = 12
MAX_PLANNING_ROUNDS = 16


class PlanStep(NebulaModel):
    action: str = Field(min_length=1, max_length=1000)
    expected_outcome: str = Field(min_length=1, max_length=1000)


class PlanSubmission(NebulaModel):
    steps: list[PlanStep] = Field(min_length=1, max_length=30)
    tool_names: list[str] = Field(default_factory=list, max_length=128)
    native_categories: list[str] = Field(default_factory=list, max_length=32)
    files: list[str] = Field(default_factory=list, max_length=200)
    no_file_work: bool = False
    unresolved_dependencies: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def coherent(self) -> "PlanSubmission":
        if self.no_file_work == bool(self.files):
            raise ValueError("specify verified files or explicitly set no_file_work")
        for field in ("tool_names", "native_categories", "files"):
            values = getattr(self, field)
            if len(values) != len(set(values)):
                raise ValueError(f"{field} contains duplicates")
        if len(self.model_dump_json()) > 24_000:
            raise ValueError("task plan exceeds the bounded execution context")
        return self


class PlanningBlocked(RuntimeError):
    """A task is pending a plan or chose an undeclared capability."""

    _nebula_before_execution = True


def plan_id(turn_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"nebula:task-plan:{turn_id}"))


def _verified_path(workspace: Path, value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("planned file path is empty or invalid")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or value == ".":
        raise ValueError("planned files must be workspace-relative paths")
    service = WorkspaceOutputService(workspace)
    try:
        candidate = service._safe_path(value, directory=False)
    except ToolOutputAccessError:
        # A file the task will create is valid only when its existing parent
        # is inside the linked workspace and none of its components is a link.
        parent = service._safe_path(str(relative.parent), directory=True)
        candidate = parent / relative.name
        if candidate.exists() or candidate.is_symlink():
            raise ValueError("planned file path is not a regular workspace file")
    return candidate.relative_to(service.workspace).as_posix()


class ChatPlanService:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    def for_turn(self, turn_id: str) -> ChatTaskPlan | None:
        try:
            return self.store.get(ChatTaskPlan, plan_id(turn_id))
        except NotFoundError:
            return None

    def pending_directive(
        self, turn: ChatTurn, session: ChatSession
    ) -> ChatPlanDirective | None:
        child_id = session.metadata.get("subagent_id")
        for item in self.store.list_session_entities(ChatPlanDirective, session.id):
            if item.status != "pending":
                continue
            if item.deferred_after_turn_id == turn.id:
                continue
            if (
                (item.target_kind == "agent" and item.target_id == session.id)
                or (item.target_kind == "goal" and item.target_id == turn.goal_id)
                or (item.target_kind == "child" and item.target_id == child_id)
                or (item.target_kind == "turn" and item.target_id == turn.id)
            ):
                return item
        return None

    def required(self, turn: ChatTurn, session: ChatSession) -> bool:
        return bool(
            turn.request_snapshot.get("planning_enabled")
            or self.for_turn(turn.id)
            or self.pending_directive(turn, session)
        )

    def ensure(self, turn: ChatTurn, session: ChatSession) -> ChatTaskPlan | None:
        if not self.required(turn, session):
            return None
        existing = self.for_turn(turn.id)
        directive = self.pending_directive(turn, session)
        if existing is not None:
            if directive is not None:
                with self.store.transaction() as transaction:
                    existing = transaction.update(
                        ChatTaskPlan,
                        existing.id,
                        {
                            "phase": "replanning"
                            if existing.phase == "execution"
                            else existing.phase,
                            "plan_version": existing.plan_version + 1
                            if existing.phase == "execution"
                            else existing.plan_version,
                            "reason": "Butler required a fresh plan: "
                            + directive.reason,
                            "directive_id": directive.id,
                        },
                        expected_revision=existing.revision,
                    )
                    transaction.update(
                        ChatPlanDirective,
                        directive.id,
                        {"status": "applied", "turn_id": turn.id},
                        expected_revision=directive.revision,
                    )
            return existing
        record = ChatTaskPlan(
            id=plan_id(turn.id),
            engagement_id=turn.engagement_id,
            session_id=turn.session_id,
            turn_id=turn.id,
            harness_turn_id=turn.harness_turn_id,
            goal_id=turn.goal_id,
            subagent_id=session.metadata.get("subagent_id"),
            directive_id=directive.id if directive else None,
        )
        with self.store.transaction() as transaction:
            transaction.add(record)
            if directive is not None:
                transaction.update(
                    ChatPlanDirective,
                    directive.id,
                    {"status": "applied", "turn_id": turn.id},
                    expected_revision=directive.revision,
                )
        return record

    def record_discovery(
        self, plan: ChatTaskPlan, name: str, result: dict[str, Any]
    ) -> ChatTaskPlan:
        if name not in DISCOVERY_TOOL_NAMES:
            raise PlanningBlocked("this tool is unavailable during discovery")
        current = self.for_turn(plan.turn_id)
        if current is None or current.phase not in {"discovery", "replanning"}:
            raise PlanningBlocked("the task is not discovering files")
        if current.discovery_calls >= MAX_DISCOVERY_CALLS:
            raise PlanningBlocked("discovery call limit reached; submit a plan")
        paths: set[str] = set(current.discovered_files)
        for match in result.get("matches", []):
            value = match.get("path") if isinstance(match, dict) else match
            if isinstance(value, str):
                paths.add(value)
        value = result.get("path")
        if isinstance(value, str) and value != ".":
            paths.add(value)
        return self.store.update(
            ChatTaskPlan,
            current.id,
            {
                "discovered_files": sorted(paths)[:500],
                "discovery_calls": current.discovery_calls + 1,
            },
            expected_revision=current.revision,
        )

    def submit(
        self,
        plan: ChatTaskPlan,
        submission: PlanSubmission,
        *,
        available_tools: set[str],
        native_categories: set[str],
        workspace: Path,
    ) -> ChatTaskPlan:
        current = self.for_turn(plan.turn_id)
        if current is None or current.phase not in {"discovery", "replanning"}:
            raise PlanningBlocked("task is not accepting a plan")
        unknown = set(submission.tool_names) - available_tools
        if unknown:
            raise ValueError("unavailable planned tools: " + ", ".join(sorted(unknown)))
        unknown_native = set(submission.native_categories) - native_categories
        if unknown_native:
            raise ValueError(
                "unavailable native categories: " + ", ".join(sorted(unknown_native))
            )
        files = [_verified_path(workspace, value) for value in submission.files]
        return self.store.update(
            ChatTaskPlan,
            current.id,
            {
                "phase": "execution",
                "steps": [item.model_dump() for item in submission.steps],
                "tool_names": submission.tool_names,
                "native_categories": submission.native_categories,
                "files": files,
                "no_file_work": submission.no_file_work,
                "unresolved_dependencies": submission.unresolved_dependencies,
                "reason": None,
            },
            expected_revision=current.revision,
        )

    def guard(self, turn_id: str, tool_name: str) -> None:
        plan = self.for_turn(turn_id)
        if plan is None:
            raise PlanningBlocked("a saved task plan is required before tool execution")
        if plan.phase != "execution":
            raise PlanningBlocked("task is pending a validated plan")
        if tool_name not in plan.tool_names:
            self.store.update(
                ChatTaskPlan,
                plan.id,
                {
                    "phase": "replanning",
                    "plan_version": plan.plan_version + 1,
                    "reason": f"Tool {tool_name} was not declared in plan version {plan.plan_version}.",
                },
                expected_revision=plan.revision,
            )
            raise PlanningBlocked(
                f"{tool_name} was not declared; the task must replan before using it"
            )

    def guard_native(self, turn_id: str, category: str) -> None:
        plan = self.for_turn(turn_id)
        if plan is None or plan.phase != "execution":
            raise PlanningBlocked(
                "a saved task plan is required before native tool use"
            )
        if category not in plan.native_categories:
            self.store.update(
                ChatTaskPlan,
                plan.id,
                {
                    "phase": "replanning",
                    "plan_version": plan.plan_version + 1,
                    "reason": f"Native capability {category} was not declared.",
                },
                expected_revision=plan.revision,
            )
            raise PlanningBlocked(
                f"native capability {category} was not declared; replan first"
            )

    def block(self, turn_id: str, reason: str) -> ChatTaskPlan:
        plan = self.for_turn(turn_id)
        if plan is None:
            raise PlanningBlocked("a task plan is required")
        return self.store.update(
            ChatTaskPlan,
            plan.id,
            {"phase": "blocked", "reason": reason[:2000]},
            expected_revision=plan.revision,
        )

    def answer_without_tools(self, turn_id: str) -> ChatTaskPlan:
        plan = self.for_turn(turn_id)
        if plan is None or plan.phase not in {"discovery", "replanning"}:
            raise PlanningBlocked("task is not in discovery")
        return self.store.update(
            ChatTaskPlan,
            plan.id,
            {
                "phase": "answered_without_tools",
                "reason": "The task was answered without using tools.",
            },
            expected_revision=plan.revision,
        )


class PlanningBroker:
    """Last provider boundary, including approval resumes and parallel waves."""

    def __init__(self, broker: Any, plans: ChatPlanService) -> None:
        self.broker = broker
        self.plans = plans

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Approval | None = None,
    ) -> ToolExecutionResult:
        turn_id = invocation.chat_turn_id
        if turn_id is not None:
            turn = self.plans.store.get(ChatTurn, turn_id)
            session = self.plans.store.get(ChatSession, turn.session_id)
            if self.plans.required(turn, session):
                self.plans.ensure(turn, session)
                self.plans.guard(turn_id, invocation.tool_name)
        return await self.broker.execute(invocation, scope, approval=approval)
