"""Bounded Core actions for one Nebula-wide scheduled Butler case.

The case runs on the worker of the Act project that hosts the check, which
otherwise sees only that project. Its reads enter ``global_check_reads`` so the
check sees every Act project its mandate covers. A change to the host project
runs here; a change to another project is sent to that project's own worker
as a ``butler.action/v1`` command (``runtime_butler_actions.py``), which runs
it only under that project's current Act authority. Without an execution
worker (one Core process) every change runs here, under the same checks.
"""

from __future__ import annotations

from .project_hold_gate import active_hold, held_detail
from .async_utils import async_first

import hashlib
import math
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .control_assistant_work import ControlAssistantWork
    from .storage import NebulaStore

from jsonschema import Draft202012Validator, ValidationError

from .butler_instructions import read_butler_instructions
from .execution_scope import global_check_reads
from .butler_sessions import DEFAULT_RUNTIME_FOCUS, focus_runtime_id
from .chat_assistant_config import chat_assistant_config
from .harnesses import HarnessConfigurationError
from .storage import NotFoundError
from .domain import (
    ButlerAuthorityPolicy,
    ButlerFocusRuntime,
    ChatBackend,
    ButlerRecoveryCase,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    Engagement,
    EngagementStatus,
    HarnessKind,
    HarnessProfile,
    RiskClass,
    ScopePolicy,
)
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_ID = {"type": "string", "minLength": 1, "maxLength": 200}
# The scheduled check's prompt roster: every Act project, open main goals.
# Goals per roster page: the scheduled check's prompt carries the first page
# and Butler reads the rest with nebula.global.roster.
ROSTER_PAGE_GOALS = 40
ROSTER_MAX_PAGE_GOALS = 100
ROSTER_GOAL_STATUSES = (
    ChatGoalStatus.RUNNING.value,
    ChatGoalStatus.PAUSED.value,
    ChatGoalStatus.BLOCKED.value,
)
_REV = {"type": "integer", "minimum": 1}
_PROJECT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"project_id": _ID},
    "required": ["project_id"],
}


async def measured_harness_capacity(
    work: Any, project_id: str, profile_id: str
) -> tuple[int, float] | None:
    """Rank confirmed included usage ahead of paid credits; unknown stays unknown."""

    try:
        quota = await work.harness_quota(
            project_id, {"profile_id": profile_id, "refresh": True}
        )
    except Exception:
        # diagnostic-expected: a failed account read excludes only its profile.
        return None
    if not isinstance(quota, dict) or quota.get("state") != "available":
        return None
    snapshots = quota.get("snapshots")
    remaining: list[float] = []
    if isinstance(snapshots, list):
        for item in snapshots:
            if not isinstance(item, dict):
                continue
            value = item.get("remaining_percent")
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                remaining.append(float(value))
    if (
        remaining
        and min(remaining) > 0
        and quota.get("ordinary_usage_allowed") is not False
    ):
        return (2, min(remaining))
    balances = quota.get("balances")
    if isinstance(balances, list):
        for item in balances:
            if not isinstance(item, dict):
                continue
            if item.get("display_value") == "Unlimited":
                return (1, float("inf"))
            amount = item.get("remaining")
            if (
                isinstance(amount, (int, float))
                and not isinstance(amount, bool)
                and math.isfinite(amount)
                and amount > 0
            ):
                return (1, float(amount))
    return None


class ButlerGlobalActions:
    def __init__(
        self,
        store: NebulaStore,
        work: ControlAssistantWork,
        instructions_root: Path,
        case_id: str,
        *,
        runtime_local: bool = False,
        remote_actions: Any = None,
    ) -> None:
        self.store = store
        self.work = work
        self.instructions_root = instructions_root
        self.case_id = case_id
        self.runtime_local = runtime_local
        # The execution worker's client for butler.action/v1 (None in one
        # Core process, where every project is local).
        self.remote_actions = remote_actions

    async def _case(self) -> ButlerRecoveryCase:
        case = await self.store.get(ButlerRecoveryCase, self.case_id)
        if (
            case.status != "investigating"
            or case.signal != "butler.global_periodic_check"
        ):
            raise ValueError("Nebula-wide Butler case is no longer active")
        return case

    async def _mandate(self) -> ButlerRecoveryCase:
        from .butler_oversight import ButlerEventPayload
        from .butler_recovery import ButlerRecoveryService

        case = await self._case()
        loaded = read_butler_instructions(self.instructions_root)
        if loaded is None:
            raise ValueError("Nebula-wide BUTLER.md was removed")
        event = await ButlerRecoveryService(self.store).source_event(case)
        digest = ButlerEventPayload.model_validate(event.payload).metrics.get(
            "instructions_sha256"
        )
        if loaded.sha256 != digest:
            raise ValueError("Nebula-wide BUTLER.md changed during this check")
        return case

    async def _project(self, project_id: str) -> Engagement:
        with global_check_reads():
            project = await self.store.get(Engagement, project_id)
            if project.status == EngagementStatus.ARCHIVED:
                raise ValueError("Archived projects cannot be managed by Butler")
            if not (await self._visible(project)):
                # One generic refusal: a model-supplied ID must not learn a
                # Watch, Steer or local-only project's name or authority.
                raise ValueError(
                    "Project is outside this Butler runtime's Act and privacy "
                    "scope; a Nebula-wide check reads and changes only Act "
                    "projects it may share with this runtime."
                )
        return project

    async def _visible(self, project: Engagement) -> bool:
        policy = await self.store.find_entity(
            ButlerAuthorityPolicy, "engagement_id", project.id
        )
        if policy is None or policy.authority != "act":
            return False
        if not self.runtime_local and project.scope_policy_id:
            scope = await self.store.get(ScopePolicy, project.scope_policy_id)
            if scope.engagement_id != project.id or scope.local_only:
                return False
        return True

    async def _act(self, project_id: str, expected: int) -> Engagement:
        """Check the target project's own Act authority, not the host's."""

        (await self._mandate())
        project = await self._project(project_id)
        with global_check_reads():
            policy = await self.store.find_entity(
                ButlerAuthorityPolicy, "engagement_id", project_id
            )
            if (
                not project.work_enabled
                or policy is None
                or policy.authority != "act"
                or policy.revision != expected
            ):
                raise ValueError(
                    "Target project Act authority changed or is unavailable"
                )
            hold = await active_hold(self.store, project_id)
        if hold is not None:
            raise ValueError(held_detail(hold))
        return project

    async def _remote(self, project_id: str) -> bool:
        """Whether a change to this project must run on its own worker."""

        case = await self._case()
        if project_id == case.engagement_id:
            return False
        if self.remote_actions is not None:
            return True
        if getattr(self.store.database, "execution_runtime_id", None) is not None:
            raise ValueError(
                "This Core worker cannot send Butler's changes to other projects "
                "yet; it will after its next update."
            )
        return False

    async def _send(
        self,
        project_id: str,
        action: str,
        arguments: dict[str, Any],
        authority_revision: int,
    ) -> dict[str, Any]:
        """Have the target project's own worker make one Butler work change."""

        from .runtime_butler_actions import ButlerAction

        case = await self._case()
        with global_check_reads():
            name = (await self.store.get(Engagement, project_id)).name
        outcome = await self.remote_actions.run(
            project_id,
            ButlerAction(
                action=action,
                arguments=arguments,
                initiator="background",
                case_id=case.id,
                authority_revision=authority_revision,
            ),
        )
        if outcome["state"] == "failed":
            detail = outcome["result"].get("detail") or "the change was refused"
            raise ValueError(f"{name} refused the change: {detail}")
        if outcome["state"] == "accepted":
            return {
                "state": "accepted",
                "command_id": outcome["command_id"],
                "detail": (
                    f"Queued behind {name}'s current work; it runs there within "
                    "ten minutes or not at all. Read the project again before "
                    "reporting it done."
                ),
            }
        applied = outcome["result"].get("applied")
        return applied if isinstance(applied, dict) else {"result": applied}

    def _local_only(self, project_id: str, route: str) -> ValueError:
        return ValueError(
            f"nebula.global.{route} runs only in this check's host project; "
            f"{project_id} needs a check hosted there or the operator."
        )

    async def projects(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._projects_read(arguments)

    async def _projects_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        (await self._case())
        with global_check_reads():
            return await self._projects_rows(arguments)

    async def _projects_rows(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        projects, policies = await self._act_projects()
        rows = []
        for project in projects[offset : offset + 50]:
            policy = policies[project.id]
            rows.append(
                {
                    "id": project.id,
                    "name": project.name,
                    "status": project.status.value,
                    "revision": project.revision,
                    "work_enabled": project.work_enabled,
                    "parent_project_id": project.parent_engagement_id,
                    "authority": policy.authority,
                    "authority_revision": policy.revision,
                }
            )
        return {
            "projects": rows,
            "next_offset": offset + 50 if offset + 50 < len(projects) else None,
            "runtimes": (await self.work.catalog((await self._case()).engagement_id))[
                "runtimes"
            ],
        }

    async def _act_projects(
        self,
    ) -> tuple[list[Engagement], dict[str, ButlerAuthorityPolicy]]:
        """Every Act project this runtime may see, oldest first, and policies."""

        # Resolve authority once, preserving find_entity's oldest-policy rule.
        # A roster scan with one policy lookup per project serializes pool waits.
        policies: dict[str, ButlerAuthorityPolicy] = {}
        for policy in await self.store.find_entities(ButlerAuthorityPolicy, {}):
            policies.setdefault(policy.engagement_id, policy)
        act_project_ids = [
            project_id
            for project_id, policy in policies.items()
            if policy.authority == "act"
        ]
        visible_projects = []
        async for project in self.store.iter_readable_entities(
            Engagement, {"id": act_project_ids}
        ):
            if project.status == EngagementStatus.ARCHIVED or not project.work_enabled:
                continue
            if not self.runtime_local and project.scope_policy_id:
                scope = await self.store.get(ScopePolicy, project.scope_policy_id)
                if scope.engagement_id != project.id or scope.local_only:
                    continue
            visible_projects.append(project)
        projects = sorted(
            visible_projects,
            key=lambda item: (item.created_at, item.id),
        )
        return projects, policies

    async def roster(
        self, cursor: int = 0, limit: int = ROSTER_PAGE_GOALS
    ) -> dict[str, Any]:
        """One page of every Act project's open main goals.

        A scheduled check's summary must cover every Act project, so every
        project is listed with its open-goal count, and goals are read in one
        stable order (projects in roster order, each project's newest goal
        first) a page at a time. Nothing is dropped: ``roster_page.next_cursor``
        names the next page, read with nebula.global.roster.
        """

        from .chat_work_stage import work_stages_for_chats

        cursor = max(0, int(cursor))
        limit = max(1, min(int(limit), ROSTER_MAX_PAGE_GOALS))
        end = cursor + limit
        with global_check_reads():
            projects, policies = await self._act_projects()
            rows: list[dict[str, Any]] = []
            position = 0
            for project in projects:
                policy = policies[project.id]
                total = await self.store.count_entities(
                    ChatGoal,
                    {"status": list(ROSTER_GOAL_STATUSES), "parent_goal_id": None},
                    engagement_id=project.id,
                )
                first = max(0, cursor - position)
                stop = max(0, min(total, end - position))
                shown = (
                    await self.store.find_entities(
                        ChatGoal,
                        {
                            "status": list(ROSTER_GOAL_STATUSES),
                            "parent_goal_id": None,
                        },
                        engagement_id=project.id,
                        newest_first=True,
                        offset=first,
                        limit=stop - first,
                    )
                    if stop > first
                    else []
                )
                position += total
                titles = (
                    {
                        session.id: session.title
                        for session in await self.store.find_entities(
                            ChatSession,
                            {"id": [goal.session_id for goal in shown]},
                            engagement_id=project.id,
                        )
                    }
                    if shown
                    else {}
                )
                stages: dict[str, Any] = {}
                if shown:
                    async with self.store.database.session() as database:
                        stages = await work_stages_for_chats(
                            database, [goal.session_id for goal in shown], [project.id]
                        )
                rows.append(
                    {
                        "project_id": project.id,
                        "name": project.name,
                        "status": project.status.value,
                        "authority_revision": policy.revision,
                        "open_main_goals": total,
                        "goals": [
                            {
                                "goal_id": goal.id,
                                "session_id": goal.session_id,
                                "title": titles.get(goal.session_id, "")[:120],
                                "objective": " ".join(goal.objective.split())[:160],
                                "status": goal.status.value,
                                "stage": (stages.get(goal.session_id) or {}).get(
                                    "label"
                                ),
                                "step": goal.current_step,
                                "plan_steps": len(goal.plan),
                            }
                            for goal in shown
                        ],
                        "goals_omitted": total - len(shown),
                    }
                )
        next_cursor = end if end < position else None
        return {
            "act_projects": rows,
            "act_project_count": len(projects),
            "roster_page": {
                "cursor": cursor,
                "limit": limit,
                "goals_total": position,
                "next_cursor": next_cursor,
                "read_next_with": "nebula.global.roster",
            },
            "truncated": next_cursor is not None,
        }

    async def roster_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        (await self._case())
        return await self.roster(
            arguments.get("cursor", 0), arguments.get("limit", ROSTER_PAGE_GOALS)
        )

    async def project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._project_read(arguments)

    async def _project_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        project = await self._project(project_id)
        with global_check_reads():
            return {
                "project": project.model_dump(mode="json"),
                "work": (await self.work.overview(project_id)),
                "goals": (await self.work.goals_overview(project_id)),
            }

    async def goal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._goal_read(arguments)

    async def _goal_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        (await self._project(arguments["project_id"]))
        with global_check_reads():
            return await self.work.goal_detail(
                arguments["project_id"], arguments["session_id"]
            )

    async def assistant_defaults(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._assistant_defaults_read(arguments)

    async def _assistant_defaults_read(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        (await self._project(arguments["project_id"]))
        with global_check_reads():
            return await self.work.assistant_defaults(arguments["project_id"])

    async def set_assistant_defaults(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        expected = arguments["expected_authority_revision"]
        (await self._act(project_id, expected))
        change = {
            "expected_revision": arguments["expected_revision"],
            "settings": arguments["settings"],
        }
        if await self._remote(project_id):
            return await self._send(
                project_id, "nebula.work.set_assistant_defaults", change, expected
            )
        return await self.work.set_assistant_defaults(project_id, change)

    async def chat_settings(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._chat_settings_read(arguments)

    async def _chat_settings_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        (await self._project(arguments["project_id"]))
        with global_check_reads():
            return await self.work.chat_settings(
                arguments["project_id"], arguments["session_id"]
            )

    async def chat_settings_many(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._chat_settings_many_read(arguments)

    async def _chat_settings_many_read(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        (await self._project(arguments["project_id"]))
        with global_check_reads():
            return await self.work.chat_settings_many(
                arguments["project_id"], arguments["session_ids"]
            )

    async def work_catalog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._work_catalog_read(arguments)

    async def _work_catalog_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        (await self._project(project_id))
        with global_check_reads():
            return await self.work.catalog(project_id, arguments.get("operation"))

    async def work_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._dispatch_work(arguments, change=False)

    async def work_change(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._dispatch_work(arguments, change=True)

    async def _dispatch_work(
        self, arguments: dict[str, Any], *, change: bool
    ) -> dict[str, Any]:
        from .control_assistant_work import _ALL_WORK_TOOLS, dispatch_work_tool

        project_id = arguments["project_id"]
        if change:
            (await self._act(project_id, arguments["expected_authority_revision"]))
        else:
            await self._project(project_id)
        operation = arguments["operation"]
        entry = next((item for item in _ALL_WORK_TOOLS if item[0] == operation), None)
        if entry is None or entry[0] == "nebula.work.catalog":
            raise ValueError("Choose an Assistant work action from work_catalog")
        if entry[3] == change:
            route = "work_read" if entry[3] else "work_change"
            raise ValueError(f"Use nebula.global.{route} for {operation}")
        payload = arguments["arguments"]
        try:
            Draft202012Validator(entry[2]).validate(payload)
        except ValidationError as exc:
            raise ValueError(
                f"Invalid Assistant work arguments for {operation}: {exc.message}; "
                "read work_catalog for its exact schema"
            ) from exc
        if not change:
            with global_check_reads():
                return await dispatch_work_tool(
                    self.work, project_id, operation, payload
                )
        expected = arguments["expected_authority_revision"]
        (await self._act(project_id, expected))
        if await self._remote(project_id):
            return await self._send(project_id, operation, payload, expected)
        return await dispatch_work_tool(self.work, project_id, operation, payload)

    async def set_chat_settings(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        expected = arguments["expected_authority_revision"]
        (await self._act(project_id, expected))
        change = {
            key: value
            for key, value in arguments.items()
            if key not in {"project_id", "expected_authority_revision"}
        }
        if await self._remote(project_id):
            return await self._send(
                project_id, "nebula.work.set_chat_settings", change, expected
            )
        return await self.work.set_chat_settings(project_id, change)

    async def host_load(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.work.host_load((await self._case()).engagement_id, {})

    async def host_pressure(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.work.host_pressure((await self._case()).engagement_id, {})

    async def host_cpu_budget(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.work.host_cpu_budget((await self._case()).engagement_id, {})

    async def harness_quota(self, arguments: dict[str, Any]) -> dict[str, Any]:
        (await self._project(arguments["project_id"]))
        # An account's quota is not project data: read it through the host
        # project, whose worker may refresh the shared observation.
        return await self.work.harness_quota(
            (await self._case()).engagement_id, arguments
        )

    async def resume_project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        project = await self._act(project_id, arguments["expected_authority_revision"])
        if project.revision != arguments["expected_project_revision"]:
            raise ValueError("Project revision changed; read it again")
        if project.status not in {
            EngagementStatus.DRAFT,
            EngagementStatus.PAUSED,
        }:
            return {
                "project": project.model_dump(mode="json"),
                "already_active": project.status == EngagementStatus.ACTIVE,
            }
        if await self._remote(project_id):
            raise self._local_only(project_id, "resume_project")
        saved = await self.store.update(
            Engagement,
            project.id,
            {"status": EngagementStatus.ACTIVE},
            expected_revision=project.revision,
        )
        return {"project": saved.model_dump(mode="json"), "resumed": True}

    async def _existing_goal(self, project_id: str, objective: str) -> ChatGoal | None:
        wanted = " ".join(objective.split()).casefold()
        with global_check_reads():
            return await async_first(
                (
                    goal
                    async for goal in self.store.iter_readable_entities(
                        ChatGoal, engagement_id=project_id
                    )
                    if goal.parent_goal_id is None
                    and " ".join(goal.objective.split()).casefold() == wanted
                ),
                None,
            )

    async def create_goal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        project = await self._act(project_id, arguments["expected_authority_revision"])
        if project.revision != arguments["expected_project_revision"]:
            raise ValueError("Project revision changed; read it again")
        if project.status != EngagementStatus.ACTIVE:
            raise ValueError("Activate the project before creating a goal")
        existing = await self._existing_goal(project_id, arguments["objective"])
        if existing is not None:
            with global_check_reads():
                detail = await self.work.goal_detail(project_id, existing.session_id)
            return {**detail, "already_exists": True}
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
        expected = arguments["expected_authority_revision"]
        project = await self._act(project_id, expected)
        if project.status != EngagementStatus.ACTIVE:
            raise ValueError("Activate the project before creating a goal")
        created = await self._change(
            project_id,
            expected,
            "nebula.work.create_goal_conversation",
            goal_args,
            self.work.create_goal_conversation,
        )
        if created.get("state") == "accepted":
            return {**created, "started": False}
        if arguments.get("start", False):
            (await self._act(project_id, expected))
            started = await self._change(
                project_id,
                expected,
                "nebula.work.goal_action",
                {
                    "session_id": created["session_id"],
                    "expected_revision": created["goal"]["revision"],
                    "action": "start",
                },
                self.work.goal_action,
            )
            return {**started, "created": True, "started": True}
        return {**created, "created": True}

    async def _change(
        self,
        project_id: str,
        expected: int,
        action: str,
        arguments: dict[str, Any],
        local: Any,
    ) -> dict[str, Any]:
        """Run one work change here, or on the target project's own worker."""

        if await self._remote(project_id):
            return await self._send(project_id, action, arguments, expected)
        result: dict[str, Any] = await local(project_id, arguments)
        return result

    async def _butler_runtime(
        self,
    ) -> tuple[
        ChatSession | None, ChatBackend | None, str | None, str | None, str | None
    ]:
        """Butler's reserved runtime: its newest main chat, else its shared default.

        Returns the chat (if any), backend, profile id, model and harness
        reasoning effort. All ``None`` when nothing is configured.
        """

        case = await self._case()
        butler_sessions = [
            session
            async for session in self.store.iter_readable_entities(
                ChatSession, engagement_id=case.engagement_id
            )
            if session.metadata.get("control_assistant") is True
            and not session.metadata.get("archived_at")
            and not session.parent_session_id
        ]
        if butler_sessions:
            session = max(butler_sessions, key=lambda item: item.created_at)
            try:
                config = await chat_assistant_config(
                    self.store, session, butler_default=False
                )
            except NotFoundError:
                # diagnostic-expected: a chat whose harness binding is gone
                # names no runtime; the shared default (or Codex) applies.
                config = None
            if config is not None:
                profile_id, model = config["profile_id"], config["model"]
                if isinstance(profile_id, str) and isinstance(model, str):
                    harness = config["backend"] == ChatBackend.HARNESS.value
                    return (
                        session,
                        ChatBackend(config["backend"]),
                        profile_id,
                        model,
                        config["harness_reasoning_effort"] if harness else None,
                    )
        for focus in (case.engagement_id, DEFAULT_RUNTIME_FOCUS):
            runtime = await self.store.find_entity(
                ButlerFocusRuntime, "id", focus_runtime_id(focus)
            )
            if runtime is not None:
                harness = runtime.backend == ChatBackend.HARNESS
                return (
                    None,
                    runtime.backend,
                    runtime.profile_id,
                    runtime.model,
                    runtime.harness_reasoning_effort if harness else None,
                )
        return None, None, None, None, None

    async def _recovery_route(self, project_id: str) -> dict[str, Any]:
        """The recovery goal's harness runtime.

        A recovery goal runs on Butler's own reserved harness and model (for
        example Claude on ``claude-opus-5-5``). A Codex Butler, a provider
        Butler, or no Butler runtime at all keeps the Codex ``gpt-6-sol``
        route with max effort and Daybreak Blue, ranked by measured capacity.
        """

        _, backend, profile_id, model, effort = await self._butler_runtime()
        if backend == ChatBackend.HARNESS and profile_id and model:
            profile = await self.store.get(HarnessProfile, profile_id)
            if profile.kind != HarnessKind.CODEX_APP_SERVER:
                if not profile.enabled or model not in profile.capabilities.models:
                    raise ValueError(
                        f"Butler's harness {profile.name!r} no longer offers {model!r}; "
                        "choose an enabled harness model in Butler's Assistant settings"
                    )
                if not profile.privacy.local_only and not (
                    profile.privacy.permits_sensitive_data
                    and profile.privacy.auto_share_tool_results
                ):
                    raise ValueError(
                        "Butler's harness is not authorized for unattended project "
                        "tool results"
                    )
                try:
                    await self.work.harness._validate_harness_privacy(
                        project_id,
                        profile,
                        [],
                        allow_remote_mcp=profile.privacy.auto_share_tool_results,
                        uses_gateway_tools=True,
                    )
                except HarnessConfigurationError as error:
                    raise ValueError(
                        f"Butler's harness cannot work in this project: {error}"
                    ) from error
                option = next(
                    (
                        item
                        for item in profile.capabilities.model_options
                        if item.model == model
                    ),
                    None,
                )
                advertised = (
                    {item.id for item in option.reasoning_efforts} if option else set()
                )
                return {
                    "profile": profile,
                    "model": model,
                    "harness_reasoning_effort": (
                        effort if effort and effort in advertised else None
                    ),
                    "harness_cyber_access_program": None,
                }
        profile = await self._recovery_harness(project_id)
        return {
            "profile": profile,
            "model": "gpt-6-sol",
            "harness_reasoning_effort": "max",
            "harness_cyber_access_program": "daybreakBlue",
        }

    async def _recovery_harness(self, project_id: str) -> HarnessProfile:
        """Select an authorized Codex route with measured usage or credits."""

        butler_sessions = [
            session
            async for session in self.store.iter_readable_entities(
                ChatSession, engagement_id=(await self._case()).engagement_id
            )
            if session.metadata.get("control_assistant") is True
            and not session.metadata.get("archived_at")
            and not session.parent_session_id
        ]
        preferred_id = (
            max(
                butler_sessions, key=lambda session: session.created_at
            ).harness_profile_id
            if butler_sessions
            else None
        )
        profiles = sorted(
            [item async for item in self.store.iter_readable_entities(HarnessProfile)],
            key=lambda profile: (profile.id != preferred_id, profile.id),
        )
        available: list[tuple[tuple[int, float], bool, HarnessProfile]] = []
        for profile in profiles:
            if not profile.enabled or profile.kind != HarnessKind.CODEX_APP_SERVER:
                continue
            option = next(
                (
                    item
                    for item in profile.capabilities.model_options
                    if item.model == "gpt-6-sol"
                ),
                None,
            )
            if (
                option is None
                or "gpt-6-sol" not in profile.capabilities.models
                or "max" not in {item.id for item in option.reasoning_efforts}
                or "daybreakBlue" not in (option.cyber_access_programs or [])
            ):
                continue
            try:
                await self.work.harness._validate_harness_privacy(
                    project_id,
                    profile,
                    [],
                    allow_remote_mcp=profile.privacy.auto_share_tool_results,
                    uses_gateway_tools=True,
                )
            except HarnessConfigurationError:
                # diagnostic-expected: this profile cannot use gateway tools in this project; inspect the next profile.
                continue
            capacity = await measured_harness_capacity(
                self.work, project_id, profile.id
            )
            if capacity is not None:
                available.append((capacity, profile.id == preferred_id, profile))
        if available:
            return min(
                available,
                key=lambda item: (-item[0][0], -item[0][1], not item[1], item[2].id),
            )[2]
        raise ValueError(
            "No authorized Codex harness has confirmed included usage or credits for "
            "gpt-6-sol with max effort and Daybreak Blue in this project"
        )

    async def create_recovery_goal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Create one typed recovery goal and report its result through this case."""

        case = await self._mandate()
        project_id = arguments["project_id"]
        project = await self._act(project_id, arguments["expected_authority_revision"])
        if await self._remote(project_id):
            # The recovery goal reports through this case, whose rows only
            # the host project's worker can link and read back.
            raise self._local_only(project_id, "create_recovery_goal")
        if project.revision != arguments["expected_project_revision"]:
            raise ValueError("Project revision changed; read it again")
        if project.status != EngagementStatus.ACTIVE:
            raise ValueError("Activate the project before creating a recovery goal")
        source = await self.store.get(ChatGoal, arguments["source_goal_id"])
        if (
            source.engagement_id != project_id
            or source.parent_goal_id is not None
            or source.status not in {ChatGoalStatus.PAUSED, ChatGoalStatus.BLOCKED}
        ):
            raise ValueError("Read a paused or blocked main goal in this project first")
        existing = await async_first(
            (
                goal
                async for goal in self.store.iter_readable_entities(
                    ChatGoal, engagement_id=project_id
                )
                if goal.metadata.get("butler_recovery_source_goal_id") == source.id
                and goal.status
                not in {ChatGoalStatus.COMPLETED, ChatGoalStatus.CANCELLED}
            ),
            None,
        )
        if existing is not None:
            if existing.id not in case.recovery_goal_ids:
                from .butler_oversight import _entity_in_transaction

                async with self.store.transaction() as transaction:
                    current = await _entity_in_transaction(
                        transaction, ButlerRecoveryCase, case.id
                    )
                    if current is None or current.status != "investigating":
                        raise ValueError(
                            "Scheduled Butler check changed; read it again"
                        )
                    if existing.id not in current.recovery_goal_ids:
                        (
                            await transaction.update(
                                ButlerRecoveryCase,
                                current.id,
                                {
                                    "recovery_goal_ids": [
                                        *current.recovery_goal_ids,
                                        existing.id,
                                    ]
                                },
                                expected_revision=current.revision,
                            )
                        )
            result = {
                **(await self.work.goal_detail(project_id, existing.session_id)),
                "already_exists": True,
                "source_goal_id": source.id,
            }
            if arguments.get("start", True) and existing.status == ChatGoalStatus.DRAFT:
                (await self._act(project_id, arguments["expected_authority_revision"]))
                started = await self.work.goal_action(
                    project_id,
                    {
                        "session_id": existing.session_id,
                        "expected_revision": existing.revision,
                        "action": "start",
                    },
                )
                return {
                    **started,
                    "already_exists": True,
                    "started": True,
                    "source_goal_id": source.id,
                }
            return result
        route = await self._recovery_route(project_id)
        if not isinstance(arguments.get("plan"), list) or not arguments["plan"]:
            raise ValueError(
                "Record the main recovery agent's plan before starting its goal"
            )
        goal_args = {
            key: value
            for key, value in arguments.items()
            if key
            in {
                "objective",
                "completion_criteria",
                "plan",
                "step_budget",
                "time_budget_seconds",
            }
        }
        goal_args.update(
            {
                "backend": "harness",
                "profile_id": route["profile"].id,
                "model": route["model"],
                **{
                    key: route[key]
                    for key in (
                        "harness_reasoning_effort",
                        "harness_cyber_access_program",
                    )
                    if route[key] is not None
                },
                "tools_enabled": True,
                "planning_enabled": False,
                "butler_recovery_case_id": case.id,
                "butler_recovery_source_goal_id": source.id,
            }
        )
        (await self._act(project_id, arguments["expected_authority_revision"]))
        created = await self.work.create_goal_conversation(project_id, goal_args)
        if arguments.get("start", True):
            (await self._act(project_id, arguments["expected_authority_revision"]))
            started = await self.work.goal_action(
                project_id,
                {
                    "session_id": created["session_id"],
                    "expected_revision": created["goal"]["revision"],
                    "action": "start",
                },
            )
            return {
                **started,
                "created": True,
                "started": True,
                "source_goal_id": source.id,
            }
        return {**created, "created": True, "source_goal_id": source.id}

    async def goal_action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        project = await self._act(project_id, arguments["expected_authority_revision"])
        if (
            arguments["action"] in {"start", "resume"}
            and project.status != EngagementStatus.ACTIVE
        ):
            raise ValueError("Activate the project before starting or resuming a goal")
        await self._main_goal(project_id, arguments)
        expected = arguments["expected_authority_revision"]
        (await self._act(project_id, expected))
        return await self._change(
            project_id,
            expected,
            "nebula.work.goal_action",
            {
                key: value
                for key, value in arguments.items()
                if key not in {"project_id", "expected_authority_revision"}
            },
            self.work.goal_action,
        )

    async def _main_goal(self, project_id: str, arguments: dict[str, Any]) -> None:
        with global_check_reads():
            current = await self.work.goal_detail(project_id, arguments["session_id"])
        goal = current.get("goal")
        if (
            current.get("kind") != "main"
            or goal is None
            or goal["revision"] != arguments["expected_revision"]
        ):
            raise ValueError("Main goal changed; read it again")

    async def update_goal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        expected = arguments["expected_authority_revision"]
        (await self._act(project_id, expected))
        await self._main_goal(project_id, arguments)
        (await self._act(project_id, expected))
        return await self._change(
            project_id,
            expected,
            "nebula.work.update_goal",
            {
                key: value
                for key, value in arguments.items()
                if key not in {"project_id", "expected_authority_revision"}
            },
            self.work.update_goal,
        )

    async def handoff_goal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Move one pre-admission planning failure to a linked provider draft."""

        project_id = arguments["project_id"]
        project = await self._act(project_id, arguments["expected_authority_revision"])
        if project.status != EngagementStatus.ACTIVE:
            raise ValueError("Activate the project before handing off a goal")
        if project.revision != arguments["expected_project_revision"]:
            raise ValueError("Project changed; read its revision again")
        with global_check_reads():
            current = await self.work.goal_detail(project_id, arguments["session_id"])
        source = current.get("goal")
        if current.get("kind") != "main" or source is None:
            raise ValueError("Handoff requires a listed main goal")
        # A retry after a committed handoff returns the one linked target even
        # when the source revision advanced. Core owns the deduplication.
        if source["revision"] != arguments["expected_revision"] and not source.get(
            "metadata", {}
        ).get("handoff_target_session_id"):
            raise ValueError("Source goal changed; read it again")
        expected = arguments["expected_authority_revision"]
        (await self._act(project_id, expected))
        handoff = await self._change(
            project_id,
            expected,
            "nebula.work.handoff_goal",
            {
                key: value
                for key, value in arguments.items()
                if key
                in {
                    "session_id",
                    "expected_revision",
                    "reason",
                    "checkpoint",
                    "max_steps",
                    "max_time_seconds",
                }
            },
            self.work.handoff_goal,
        )
        if handoff.get("state") == "accepted":
            return {**handoff, "started": False}
        if arguments.get("start", True) and handoff["goal"]["status"] == "draft":
            (await self._act(project_id, expected))
            handoff = {
                **await self._change(
                    project_id,
                    expected,
                    "nebula.work.goal_action",
                    {
                        "session_id": handoff["session_id"],
                        "expected_revision": handoff["goal"]["revision"],
                        "action": "start",
                    },
                    self.work.goal_action,
                ),
                "source_goal_id": source["id"],
                "source_session_id": arguments["session_id"],
                "already_exists": handoff.get("already_exists", False),
            }
        return handoff

    async def create_child_project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .setup import create_engagement_with_default_scope

        parent_id = arguments["parent_project_id"]
        parent = await self._act(parent_id, arguments["expected_authority_revision"])
        if await self._remote(parent_id):
            raise self._local_only(parent_id, "create_child_project")
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
        existing = await self.store.find_entity(Engagement, "id", child_id)
        if existing is None:
            existing = await async_first(
                (
                    item
                    async for item in self.store.iter_readable_entities(Engagement)
                    if item.parent_engagement_id == parent_id
                    and " ".join(item.name.split()).casefold() == normalized
                ),
                None,
            )
        if existing is None:
            (await self._act(parent_id, arguments["expected_authority_revision"]))
            existing = await create_engagement_with_default_scope(
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
            current = await self._existing_goal(existing.id, initial_goal["objective"])
            if current is None:
                (await self._act(parent_id, arguments["expected_authority_revision"]))
                saved = await self.work.create_goal_conversation(
                    existing.id, initial_goal
                )
            else:
                saved = await self.work.goal_detail(existing.id, current.session_id)
            result["goal"] = saved
            if arguments.get("start_initial_goal", False):
                goal = saved["goal"]
                if goal["status"] == "draft":
                    (
                        await self._act(
                            parent_id, arguments["expected_authority_revision"]
                        )
                    )
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
        from .control_assistant_work import (
            GOAL_ACTION_SCHEMA,
            SET_ASSISTANT_DEFAULTS_SCHEMA,
            SET_CHAT_SETTINGS_SCHEMA,
            START_GOAL_CONVERSATION_SCHEMA,
            UPDATE_GOAL_SCHEMA,
        )

        goal_fields = START_GOAL_CONVERSATION_SCHEMA["properties"]
        operations = (
            (
                "nebula.global.host_cpu_budget",
                "Read this Core host's configured CPU budget, saved enabled state and observed PDF worker count/affinity across projects. Distinguish pending/failed from running and disabled; external chat/tool/LLM/GPU execution is separate. This read never changes settings or enables workers.",
                {"type": "object", "additionalProperties": False, "properties": {}},
                RiskClass.LOCAL_READ,
                self.host_cpu_budget,
            ),
            (
                "nebula.global.host_pressure",
                "Read live saved/effective host pressure thresholds, wait cause and effective concurrency. Settings API PUT /api/v1/host-pressure applies revision-checked thresholds live; I/O PSI reports task stall time, not disk utilization.",
                {"type": "object", "additionalProperties": False, "properties": {}},
                RiskClass.LOCAL_READ,
                self.host_pressure,
            ),
            (
                "nebula.global.host_load",
                "Read current Core host CPU and memory pressure, capacity, busy/deferred reason and sample age through a fresh authenticated /api/v1/host-load request before planning work across projects. Core shares its bounded one-second sampler cache; unknown measurements remain unknown. This read starts no work.",
                {"type": "object", "additionalProperties": False, "properties": {}},
                RiskClass.LOCAL_READ,
                self.host_load,
            ),
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
                "nebula.global.roster",
                "Read one page of every Act project's open main goals (id, chat, title, objective, status, stage, step) in the scheduled check's roster order. Start at the packet's roster_page.next_cursor and keep reading until next_cursor is null, so every open goal is judged.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "cursor": {"type": "integer", "minimum": 0, "maximum": 100000},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": ROSTER_MAX_PAGE_GOALS,
                        },
                    },
                },
                RiskClass.LOCAL_READ,
                self.roster_page,
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
                "nebula.global.work_catalog",
                "List every Assistant work action for an Act project, or read one action's exact input schema before using work_read or work_change.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"project_id": _ID, "operation": _ID},
                    "required": ["project_id"],
                },
                RiskClass.LOCAL_READ,
                self.work_catalog,
            ),
            (
                "nebula.global.work_read",
                "Read any listed Assistant work item in an Act project. Get the operation's exact arguments from work_catalog first.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "operation": _ID,
                        "arguments": {"type": "object"},
                    },
                    "required": ["project_id", "operation", "arguments"],
                },
                RiskClass.LOCAL_READ,
                self.work_read,
            ),
            (
                "nebula.global.work_change",
                "Change any listed Assistant chat, goal, mission, queue, schedule, subagent, approval, question, or saved setting in an Act project. Read current state and the exact schema first. Core enforces target ownership, revisions and action policy; verify the observed result.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "expected_authority_revision": _REV,
                        "operation": _ID,
                        "arguments": {"type": "object"},
                    },
                    "required": [
                        "project_id",
                        "expected_authority_revision",
                        "operation",
                        "arguments",
                    ],
                },
                RiskClass.WORKSPACE_WRITE,
                self.work_change,
            ),
            (
                "nebula.global.assistant_defaults",
                "Read every saved Assistant default for future chats in one Act project, with its revision and available runtimes.",
                _PROJECT,
                RiskClass.LOCAL_READ,
                self.assistant_defaults,
            ),
            (
                "nebula.global.set_assistant_defaults",
                "Edit any saved Assistant default for future chats in an Act project after reading its revision.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "expected_authority_revision": _REV,
                        **SET_ASSISTANT_DEFAULTS_SCHEMA["properties"],
                    },
                    "required": [
                        "project_id",
                        "expected_authority_revision",
                        *SET_ASSISTANT_DEFAULTS_SCHEMA["required"],
                    ],
                },
                RiskClass.WORKSPACE_WRITE,
                self.set_assistant_defaults,
            ),
            (
                "nebula.global.chat_settings",
                "Read all next-turn Assistant choices, available runtimes, and revision for one main chat in an Act project.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"project_id": _ID, "session_id": _ID},
                    "required": ["project_id", "session_id"],
                },
                RiskClass.LOCAL_READ,
                self.chat_settings,
            ),
            (
                "nebula.global.chat_settings_many",
                "Read saved next-turn Assistant choices and revisions for up to 100 main chats in one Act project. Use this for roster reviews; use chat_settings for the full runtime catalog before edits that need it.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "session_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 100,
                            "uniqueItems": True,
                            "items": _ID,
                        },
                    },
                    "required": ["project_id", "session_ids"],
                },
                RiskClass.LOCAL_READ,
                self.chat_settings_many,
            ),
            (
                "nebula.global.set_chat_settings",
                "Edit any next-turn Assistant setting of one main chat in an Act project. Read its settings and revision first; the saved choice applies to the next turn.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "expected_authority_revision": _REV,
                        **SET_CHAT_SETTINGS_SCHEMA["properties"],
                    },
                    "required": [
                        "project_id",
                        "expected_authority_revision",
                        *SET_CHAT_SETTINGS_SCHEMA["required"],
                    ],
                },
                RiskClass.WORKSPACE_WRITE,
                self.set_chat_settings,
            ),
            (
                "nebula.global.harness_quota",
                "Read a configured Codex or Grok harness's vendor-reported included quota window and separate credit balance for an Act project; refresh requests a new read. Missing values remain unknown.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "profile_id": _ID,
                        "refresh": {"type": "boolean"},
                    },
                    "required": ["project_id", "profile_id"],
                },
                RiskClass.LOCAL_READ,
                self.harness_quota,
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
                "Create one unique main goal conversation in an Act project; optionally start it after saving. Use harness_reasoning_effort for a harness, or reasoning_effort for a provider.",
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
                "nebula.global.create_recovery_goal",
                "Create or find one recovery goal for a paused or blocked main goal. Core links its report to this scheduled check and runs it on Butler's own reserved harness and model; a Codex (or provider) Butler uses an authorized Codex gpt-6-sol route with max effort and Daybreak Blue. Use this instead of create_goal for recovery work.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "source_goal_id": _ID,
                        "expected_project_revision": _REV,
                        "expected_authority_revision": _REV,
                        "objective": goal_fields["objective"],
                        "completion_criteria": goal_fields["completion_criteria"],
                        "plan": goal_fields["plan"],
                        "step_budget": goal_fields["step_budget"],
                        "time_budget_seconds": goal_fields["time_budget_seconds"],
                        "start": {"type": "boolean"},
                    },
                    "required": [
                        "project_id",
                        "source_goal_id",
                        "expected_project_revision",
                        "expected_authority_revision",
                        "objective",
                        "completion_criteria",
                        "plan",
                    ],
                },
                RiskClass.WORKSPACE_WRITE,
                self.create_recovery_goal,
            ),
            (
                "nebula.global.goal_action",
                "Start, pause, resume, block, complete, or cancel one main goal under Act authority after reading its revision. Core requires completion evidence and enforces holds. Activate the project before starting or resuming.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "expected_authority_revision": _REV,
                        **GOAL_ACTION_SCHEMA["properties"],
                    },
                    "required": [
                        "project_id",
                        "expected_authority_revision",
                        *GOAL_ACTION_SCHEMA["required"],
                    ],
                },
                RiskClass.WORKSPACE_WRITE,
                self.goal_action,
            ),
            (
                "nebula.global.update_goal",
                "Edit any objective, criteria, plan, resume instruction, time, token, step, or child budget on one nonterminal main goal in an Act project. Read its revision first, then resume separately after verifying all other holds.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "expected_authority_revision": _REV,
                        **UPDATE_GOAL_SCHEMA["properties"],
                    },
                    "required": [
                        "project_id",
                        "expected_authority_revision",
                        *UPDATE_GOAL_SCHEMA["required"],
                    ],
                },
                RiskClass.WORKSPACE_WRITE,
                self.update_goal,
            ),
            (
                "nebula.global.handoff_goal",
                "Recover a paused harness main goal after a pre-admission Plan before acting failure. Core verifies no tool effects, approvals, queued work, model tools, privacy and current revisions; it creates one linked provider goal and optionally starts it. Read the source goal and settings first. Do not use this to bypass a safety hold.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_id": _ID,
                        "session_id": _ID,
                        "expected_project_revision": _REV,
                        "expected_authority_revision": _REV,
                        "expected_revision": _REV,
                        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "checkpoint": {"type": "string", "maxLength": 4000},
                        "max_steps": {"type": "integer", "minimum": 1, "maximum": 100},
                        "max_time_seconds": {
                            "type": "integer",
                            "minimum": 1800,
                            "maximum": 86400,
                        },
                        "start": {"type": "boolean"},
                    },
                    "required": [
                        "project_id",
                        "session_id",
                        "expected_project_revision",
                        "expected_authority_revision",
                        "expected_revision",
                        "reason",
                    ],
                },
                RiskClass.WORKSPACE_WRITE,
                self.handoff_goal,
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
