"""Stage-based runtime routing for Core-owned research goals.

A research goal's main agent may run on a different runtime at different
stages of its Work: a harness profile (for example a Claude login) while the
goal validates, a provider model while it does bulk work. The operator states
the routes in Core's service environment, never in prompt text:

* ``NEBULA_GOAL_RUNTIME_ROUTING_ENABLED`` (default off) is the master switch.
  Off, nothing here reads or changes a goal and the operator route and the
  Butler tool refuse.
* ``NEBULA_GOAL_RUNTIME_ROUTING`` is a JSON :class:`RoutingPolicy`: named
  routes (``stages`` matched case-insensitively against the goal chat's Work
  stage key or label, a ``main`` runtime, optional ``subagents`` and an
  optional rate-limit ``fallback`` route), an optional promotion rule (``N``
  independent-review send-backs move the goal to a route), and the
  ``projects`` (and optionally ``goals``) it applies to.

The environment holds it, like the other supervisor and reviewer settings,
because it names Core-wide runtime profiles, is set by the host operator, and
must not be widened by an agent; a stored setting would be writable through
the same tools it governs.

Rules (see docs/design/goal-runtime-routing.md):

* **Never silent, never by default.** Routing acts only on a goal it owns. A
  goal becomes owned when its current or already-pending Assistant settings
  already match the route (recorded as ``in_policy``), or by an explicit
  operator or Butler route action. A goal whose runtime differs from the
  policy is recorded as ``out_of_policy`` and left alone; an owned goal whose
  runtime someone changes by hand is released the same way.
* **Same code path.** A switch is ``update_chat_assistant_config`` (the
  ``PATCH /chat-sessions/{id}/assistant-config`` writer), keeping the goal's
  MCP selection, recorded on the goal chat and in the project's events with
  the old and new runtime and the reason.
* **Turn boundary.** Automatic routing is evaluated in
  ``RuntimeSwitchService.settle``, which every goal dispatch and
  continuation calls before it starts the goal's next turn. A busy chat is
  never switched: automatic routing records a deferred switch for the next
  boundary, and an explicit route records the existing waiting-for-idle
  pending switch.
* **Verified harness only.** A route to a harness profile whose Check/Test
  turn is not verified is refused.
* **Rate-limit fallback.** A harness goal turn that fails on a usage or rate
  limit runs again on its route's ``fallback`` runtime; the route is tried
  again after ``fallback_cooldown_seconds``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .diagnostics import record_caught_exception, record_diagnostic
from .domain import (
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    Engagement,
    HarnessProfile,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    ProviderProfile,
    RiskClass,
    utc_now,
)
from .storage import ConflictError, NebulaStore, NotFoundError, revise_latest
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec

ENABLED_ENV = "NEBULA_GOAL_RUNTIME_ROUTING_ENABLED"
CONFIG_ENV = "NEBULA_GOAL_RUNTIME_ROUTING"
ROUTING_KEY = "goal_runtime_routing"
"""Goal chat metadata: routing ownership, applied runtime, cooldown, events."""
TOOL_NAME = "nebula.supervisor.route_goal_runtime"
EVENT_TYPE = "goal.runtime_routing"
EVENT_LIMIT = 50
MAIN_MODEL_LIMIT = 20
DEFAULT_COOLDOWN_SECONDS = 900
FALLBACK_INSTRUCTION = (
    "The previous response stopped at a usage or rate limit on the validation "
    "runtime. Core moved this goal to its fallback runtime for this turn. "
    "Review the saved state and continue the goal."
)
_TRUE = {"1", "true", "yes", "on"}
_LIVE_GOAL = {
    ChatGoalStatus.DRAFT,
    ChatGoalStatus.RUNNING,
    ChatGoalStatus.PAUSED,
    ChatGoalStatus.BLOCKED,
}
_RATE_LIMIT_REASONS = {"rate_limited", "quota_exhausted"}
_RATE_LIMIT_TEXT = re.compile(
    r"rate[\s_-]?limit|usage[\s_-]?limit|hit your limit|limit reached|"
    r"too many requests|\b429\b|quota",
    re.IGNORECASE,
)
_ROUTE_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


class RoutingConfigError(ValueError):
    """The routing environment is set but cannot be used."""


class RoutingRefused(ValueError):
    """Core refuses this route for this goal."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RouteMain(_Strict):
    backend: Literal["provider", "harness"]
    provider_id: str | None = Field(default=None, min_length=1, max_length=200)
    harness_profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=500)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def one_runtime(self) -> "RouteMain":
        if self.backend == "provider" and (
            not self.provider_id or self.harness_profile_id
        ):
            raise ValueError("a provider main runtime names provider_id only")
        if self.backend == "harness" and (
            not self.harness_profile_id or self.provider_id
        ):
            raise ValueError("a harness main runtime names harness_profile_id only")
        if self.backend == "provider" and self.reasoning_effort not in {
            None,
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        }:
            raise ValueError("choose a provider reasoning_effort Nebula supports")
        return self

    @property
    def profile_id(self) -> str:
        return str(self.harness_profile_id or self.provider_id)


class RouteSubagents(_Strict):
    provider_id: str | None = Field(default=None, min_length=1, max_length=200)
    harness_profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=500)
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None
    ) = None

    @model_validator(mode="after")
    def one_runtime(self) -> "RouteSubagents":
        if bool(self.provider_id) == bool(self.harness_profile_id):
            raise ValueError("subagents name one of provider_id or harness_profile_id")
        return self


class Route(_Strict):
    name: str = Field(pattern=_ROUTE_NAME)
    stages: list[str] = Field(default_factory=list, max_length=64)
    main: RouteMain
    subagents: RouteSubagents | None = None
    fallback: str | None = Field(default=None, pattern=_ROUTE_NAME)
    fallback_cooldown_seconds: int = Field(
        default=DEFAULT_COOLDOWN_SECONDS, ge=0, le=86_400
    )


class Promotion(_Strict):
    review_rejections: int = Field(default=2, ge=1, le=20)
    route: str = Field(pattern=_ROUTE_NAME)


class RoutingPolicy(_Strict):
    routes: list[Route] = Field(min_length=1, max_length=32)
    promotion: Promotion | None = None
    projects: list[str] = Field(min_length=1, max_length=200)
    goals: list[str] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def consistent(self) -> "RoutingPolicy":
        names = [route.name for route in self.routes]
        if len(names) != len(set(names)):
            raise ValueError("route names must be unique")
        seen: dict[str, str] = {}
        for route in self.routes:
            for stage in route.stages:
                folded = " ".join(stage.split()).casefold()
                if not folded:
                    raise ValueError("a route stage cannot be empty")
                if folded in seen and seen[folded] != route.name:
                    raise ValueError(f"stage {stage!r} is in two routes")
                seen[folded] = route.name
            if route.fallback is not None and (
                route.fallback not in names or route.fallback == route.name
            ):
                raise ValueError(f"route {route.name} names an unknown fallback")
        if self.promotion is not None and self.promotion.route not in names:
            raise ValueError("the promotion rule names an unknown route")
        return self

    def route(self, name: str | None) -> Route | None:
        return next((item for item in self.routes if item.name == name), None)

    def route_for_stage(self, *values: str | None) -> Route | None:
        wanted = {" ".join(v.split()).casefold() for v in values if v and v.strip()}
        for route in self.routes:
            if wanted & {" ".join(s.split()).casefold() for s in route.stages}:
                return route
        return None

    def in_scope(self, project: Engagement, session_id: str, goal_id: str) -> bool:
        projects = set(self.projects)
        if "*" not in projects and not ({project.id, project.name} & projects):
            return False
        return not self.goals or bool({session_id, goal_id} & set(self.goals))


_POLICY_CACHE: dict[str, RoutingPolicy] = {}


def load_routing_policy(
    environ: Mapping[str, str] | None = None,
) -> RoutingPolicy | None:
    """The policy, or ``None`` while the master switch is off."""

    env = os.environ if environ is None else environ
    if str(env.get(ENABLED_ENV, "")).strip().lower() not in _TRUE:
        return None
    raw = str(env.get(CONFIG_ENV, "")).strip()
    if not raw:
        raise RoutingConfigError(f"{ENABLED_ENV} is on but {CONFIG_ENV} is not set")
    cached = _POLICY_CACHE.get(raw)
    if cached is not None:
        return cached
    try:
        policy = RoutingPolicy.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise RoutingConfigError(f"{CONFIG_ENV} is invalid: {exc}"[:2_000]) from exc
    _POLICY_CACHE.clear()
    _POLICY_CACHE[raw] = policy
    return policy


# --------------------------------------------------------------------------
# Runtime signatures
# --------------------------------------------------------------------------


def config_signature(config: Mapping[str, Any]) -> dict[str, Any]:
    """The runtime identity of an Assistant settings selection."""

    subagent: dict[str, Any] | None = None
    if config.get("allow_subagents") and (
        config.get("subagent_provider_id") or config.get("subagent_harness_profile_id")
    ):
        provider = config.get("subagent_provider_id")
        subagent = {
            "kind": "provider" if provider else "harness",
            "id": provider or config.get("subagent_harness_profile_id"),
            "model": config.get("subagent_model"),
        }
    return {
        "backend": config.get("backend"),
        "profile_id": config.get("profile_id"),
        "model": config.get("model"),
        "subagent": subagent,
    }


def _route_subagent(route: Route) -> dict[str, Any] | None:
    if route.subagents is None:
        return None
    return {
        "kind": "provider" if route.subagents.provider_id else "harness",
        "id": route.subagents.provider_id or route.subagents.harness_profile_id,
        "model": route.subagents.model,
    }


def target_signature(route: Route, current: Mapping[str, Any]) -> dict[str, Any]:
    """The signature the route leaves: unrouted subagents keep their choice."""

    subagent = _route_subagent(route)
    if route.subagents is None:
        subagent = None if route.main.backend == "harness" else current["subagent"]
    return {
        "backend": route.main.backend,
        "profile_id": route.main.profile_id,
        "model": route.main.model,
        "subagent": subagent,
    }


def matches_route(signature: Mapping[str, Any], route: Route) -> bool:
    """Whether a runtime already is this route; efforts are not compared."""

    if (
        signature.get("backend"),
        signature.get("profile_id"),
        signature.get("model"),
    ) != (
        route.main.backend,
        route.main.profile_id,
        route.main.model,
    ):
        return False
    return route.subagents is None or signature.get("subagent") == _route_subagent(
        route
    )


def describe(signature: Mapping[str, Any] | None) -> str:
    if not signature:
        return "unknown"
    text = f"{signature.get('backend')}:{signature.get('profile_id')}/{signature.get('model')}"
    sub = signature.get("subagent")
    if isinstance(sub, dict):
        text += f" + subagents {sub.get('kind')}:{sub.get('id')}/{sub.get('model')}"
    return text[:200]


async def effective_config(
    store: NebulaStore, session: ChatSession
) -> tuple[dict[str, Any], str]:
    """The settings the next turn uses: an already-pending switch, else current.

    The second value is ``none``, ``waiting`` (a waiting-for-idle switch is
    queued; its target is returned) or ``needs_review``.
    """

    from .chat_assistant_config import chat_assistant_config
    from .runtime_switching import RuntimeSwitchService

    pending = session.metadata.get("pending_runtime_switch")
    if isinstance(pending, dict) and pending.get("status") == "needs_review":
        return await chat_assistant_config(store, session, butler_default=False), (
            "needs_review"
        )
    if isinstance(pending, dict) and pending.get("status") == "waiting_for_idle":
        try:
            metadata = RuntimeSwitchService.pending_metadata(session, pending)
            binding = pending["binding"]
            backend = ChatBackend(binding["backend"])
            target = session.model_copy(
                update={
                    "backend": backend,
                    "harness_profile_id": binding.get("harness_profile_id"),
                    "provider_profile_id": binding.get("provider_profile_id"),
                    "harness_session_id": None,
                    "model": binding.get("model"),
                    "metadata": metadata,
                }
            )
            return await chat_assistant_config(store, target, butler_default=False), (
                "waiting"
            )
        except (KeyError, TypeError, ValueError):
            # diagnostic-expected: an unreadable pending switch is reviewed by
            # its own writer; routing reads it as needing review.
            return await chat_assistant_config(store, session, butler_default=False), (
                "needs_review"
            )
    return await chat_assistant_config(store, session, butler_default=False), "none"


# --------------------------------------------------------------------------
# Stage, promotion and desired route
# --------------------------------------------------------------------------


async def goal_stage(store: NebulaStore, session: ChatSession) -> dict[str, Any] | None:
    """The goal chat's Work stage, read as the Butler board reads it."""

    from .chat_work_stage import work_stages_for_chats

    async with store.database.session() as database:
        stages = await work_stages_for_chats(
            database, [session.id], [session.engagement_id]
        )
    return stages.get(session.id)


def review_rejections(goal: ChatGoal) -> int:
    """Independent-review send-backs (REJECT, NOT_READY) recorded on the goal."""

    from .butler_independent_review import (
        REVIEW_HISTORY_KEY,
        REVIEW_KEY,
        SEND_BACK_VERDICTS,
    )

    history = goal.metadata.get(REVIEW_HISTORY_KEY)
    records = (
        [item for item in history if isinstance(item, dict)]
        if isinstance(history, list)
        else []
    )
    if not records and isinstance(goal.metadata.get(REVIEW_KEY), dict):
        records = [goal.metadata[REVIEW_KEY]]
    return sum(1 for item in records if item.get("verdict") in SEND_BACK_VERDICTS)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:  # diagnostic-expected: a malformed cooldown is ignored
        return None


async def desired_route(
    store: NebulaStore,
    policy: RoutingPolicy,
    session: ChatSession,
    goal: ChatGoal,
    state: Mapping[str, Any],
    now: datetime,
) -> tuple[Route | None, str]:
    """The route for the goal's next turn and why; ``None`` keeps the runtime."""

    route: Route | None = None
    reason = ""
    override = policy.route(state.get("override"))
    if override is not None:
        route, reason = override, f"operator route {override.name}"
    if route is None and policy.promotion is not None:
        count = review_rejections(goal)
        if count >= policy.promotion.review_rejections:
            route = policy.route(policy.promotion.route)
            reason = f"{count} independent review send-backs"
    if route is None:
        stage = await goal_stage(store, session)
        if stage is not None:
            route = policy.route_for_stage(stage.get("key"), stage.get("label"))
            if route is not None:
                reason = f"Work stage {stage.get('label') or stage.get('key')}"
    cooldown = state.get("cooldown")
    if route is not None and isinstance(cooldown, dict):
        until = _parse_time(cooldown.get("until"))
        fallback = policy.route(route.fallback)
        if (
            cooldown.get("route") == route.name
            and until is not None
            and until > now
            and fallback is not None
        ):
            return fallback, (
                f"{reason}; {route.name} is cooling down after a rate limit "
                f"until {until.isoformat()}"
            )
    return route, reason


# --------------------------------------------------------------------------
# Readiness, state and events
# --------------------------------------------------------------------------


def _harness_ready(
    profile: HarnessProfile, model: str, *, rate_limited: bool = False
) -> str | None:
    caps = profile.capabilities
    if not profile.enabled:
        return f"harness profile {profile.id} is disabled"
    if caps.models and model not in caps.models:
        return f"harness profile {profile.id} does not advertise {model}"
    # A goal turn that stopped at a usage or rate limit marks the profile's
    # last turn failed; that does not undo its Check/Test, and the fallback
    # cooldown exists to try the harness again.
    limited = rate_limited and caps.turn_state == "failed"
    if (
        caps.turn_state != "verified" and not limited
    ) or caps.authentication_state == "failed":
        return (
            f"harness profile {profile.id} has no verified Check/Test turn "
            f"(turn {caps.turn_state}, sign-in {caps.authentication_state}); "
            "run Check and Test turn on it first"
        )
    return None


async def check_route_ready(
    store: NebulaStore, route: Route, *, state: Mapping[str, Any] | None = None
) -> None:
    """Refuse a route to an unverified harness or a disabled provider.

    ``state`` is the goal's routing state: a route this goal left on a rate
    limit may be retried although that failure marked the profile's turn.
    """

    cooldown = (state or {}).get("cooldown")
    rate_limited = isinstance(cooldown, dict) and cooldown.get("route") == route.name

    targets: list[tuple[str, str, str]] = [
        (route.main.backend, route.main.profile_id, route.main.model)
    ]
    if route.subagents is not None:
        sub = route.subagents
        targets.append(
            (
                "provider" if sub.provider_id else "harness",
                str(sub.provider_id or sub.harness_profile_id),
                sub.model,
            )
        )
    for backend, profile_id, model in targets:
        try:
            if backend == "harness":
                problem = _harness_ready(
                    await store.get(HarnessProfile, profile_id),
                    model,
                    rate_limited=rate_limited and profile_id == route.main.profile_id,
                )
            else:
                provider = await store.get(ProviderProfile, profile_id)
                problem = (
                    None
                    if provider.enabled
                    and (
                        not provider.model_allowlist
                        or model in provider.model_allowlist
                    )
                    else f"provider {profile_id} is disabled or does not allow {model}"
                )
        except NotFoundError as exc:
            raise RoutingRefused(
                f"route {route.name} names a missing {backend} profile {profile_id}"
            ) from exc
        if problem:
            raise RoutingRefused(f"route {route.name}: {problem}")


async def _record(
    store: NebulaStore,
    session: ChatSession,
    goal: ChatGoal | None,
    update: Callable[[dict[str, Any]], dict[str, Any] | None],
    event: dict[str, Any] | None,
) -> ChatSession:
    """Save routing state on the goal chat, and mirror an event to the project."""

    entry: dict[str, Any] | None = None
    if event is not None:
        entry = {"id": uuid4().hex, "at": utc_now().isoformat(), **event}

    def plan(latest: ChatSession) -> dict[str, Any] | None:
        state = dict(latest.metadata.get(ROUTING_KEY) or {})
        changed = update(state)
        if changed is None and entry is None:
            return None
        state = changed if changed is not None else state
        if entry is not None:
            state["events"] = [*(state.get("events") or []), entry][-EVENT_LIMIT:]
        return {"metadata": {**latest.metadata, ROUTING_KEY: state}}

    saved = await revise_latest(store, ChatSession, session.id, plan)
    if entry is not None:
        record_diagnostic(
            "info",
            "chat",
            "chat.goal.runtime_routing",
            f"Goal runtime routing {entry.get('action')}: {entry.get('reason')}"[:500],
            session_id=session.id,
        )
        try:
            await _project_event(store, session, goal, entry)
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.goal.runtime_routing_event_failed",
                "A goal runtime routing event could not be added to the project events.",
                exc,
                stage="goal-runtime-routing",
            )
    return saved


async def _project_event(
    store: NebulaStore,
    session: ChatSession,
    goal: ChatGoal | None,
    entry: dict[str, Any],
) -> None:
    from .butler_oversight import ButlerEventPayload, _append_project_event

    metrics: dict[str, str | int | float | bool | None] = {
        key: str(entry[key])[:200]
        for key in ("action", "route", "from", "to", "actor", "status")
        if entry.get(key) is not None
    }
    async with store.transaction() as transaction:
        await _append_project_event(
            transaction,
            engagement_id=session.engagement_id,
            event_type=EVENT_TYPE,
            payload=ButlerEventPayload(
                project_id=session.engagement_id,
                goal_id=goal.id if goal is not None else None,
                session_id=session.id,
                source="core",
                actor_role="core",
                summary=(
                    f"Goal runtime {entry.get('action')}: {entry.get('reason') or ''}"
                )[:500],
                metrics=metrics,
                verification_state="core_observed",
            ),
            idempotency_key=f"goal-runtime-routing:{entry['id']}",
            now=utc_now(),
        )


def _with_main_model(state: dict[str, Any], *models: str | None) -> list[str]:
    known = [str(item) for item in state.get("main_models") or []]
    for model in models:
        if model and model not in known:
            known.append(model)
    return known[-MAIN_MODEL_LIMIT:]


def _workspace_resolver(store: NebulaStore) -> Any:
    switcher = store.runtime_switch_service
    chat = getattr(switcher, "chat", None)
    return getattr(chat, "workspace_resolver", None)


def route_patch(route: Route, current: Mapping[str, Any]) -> Any:
    """The Assistant settings change that selects this route, MCPs kept."""

    from .chat_assistant_config import ChatAssistantConfigPatch

    values: dict[str, Any] = {
        "backend": route.main.backend,
        "profile_id": route.main.profile_id,
        "model": route.main.model,
        # The goal's MCP selection is the operator's; a route never drops it.
        "mcp_server_ids": list(current.get("mcp_server_ids") or []),
    }
    if route.main.backend == "harness":
        values["harness_reasoning_effort"] = route.main.reasoning_effort
        if (current.get("backend"), current.get("profile_id")) != (
            "harness",
            route.main.profile_id,
        ):
            values.update(
                harness_service_tier=None,
                harness_mode=None,
                harness_cyber_access_program="automatic",
            )
    elif route.main.reasoning_effort is not None:
        values["reasoning_effort"] = route.main.reasoning_effort
    if route.subagents is not None:
        sub = route.subagents
        values.update(
            allow_subagents=True,
            subagent_model=sub.model,
            subagent_reasoning_effort=sub.reasoning_effort,
            **(
                {"subagent_provider_id": sub.provider_id}
                if sub.provider_id
                else {"subagent_harness_profile_id": sub.harness_profile_id}
            ),
        )
    elif route.main.backend == "harness":
        values["allow_subagents"] = False
    return ChatAssistantConfigPatch(**values)


async def _switch(
    store: NebulaStore,
    session: ChatSession,
    goal: ChatGoal,
    route: Route,
    current: Mapping[str, Any],
    *,
    reason: str,
    actor: str,
    action: str,
    extra_state: Mapping[str, Any] | None = None,
) -> ChatSession:
    """Record the intended runtime, then apply it through the settings writer."""

    from .chat_assistant_config import update_chat_assistant_config

    before = config_signature(current)
    target = target_signature(route, before)
    previous = session.metadata.get(ROUTING_KEY) or {}

    def mark(state: dict[str, Any]) -> dict[str, Any]:
        state.update(
            owned=True,
            applied=target,
            route=route.name,
            status="switching",
            deferred=None,
            main_models=_with_main_model(state, before["model"], route.main.model),
            **(extra_state or {}),
        )
        return state

    session = await _record(store, session, goal, mark, None)
    try:
        saved = await update_chat_assistant_config(
            store,
            session,
            route_patch(route, current),
            expected_revision=session.revision,
            workspace_resolver=_workspace_resolver(store),
            preserve_selection=True,
        )
    except Exception as exc:
        record_caught_exception(
            "chat",
            "chat.goal.runtime_routing_switch_failed",
            "A goal runtime route could not be applied.",
            exc,
            stage="goal-runtime-routing",
        )

        def revert(state: dict[str, Any]) -> dict[str, Any]:
            state.update(
                applied=previous.get("applied") or before,
                route=previous.get("route"),
                status="switch_failed",
            )
            return state

        await _record(
            store,
            session,
            goal,
            revert,
            {
                "action": "switch_failed",
                "route": route.name,
                "from": describe(before),
                "to": describe(target),
                "reason": f"{reason}: {exc}"[:500],
                "actor": actor,
            },
        )
        raise
    status = "pending" if saved.metadata.get("pending_runtime_switch") else "routed"

    def done(state: dict[str, Any]) -> dict[str, Any]:
        state["status"] = status
        return state

    return await _record(
        store,
        saved,
        goal,
        done,
        {
            "action": action,
            "route": route.name,
            "from": describe(before),
            "to": describe(target),
            "old_runtime": before,
            "new_runtime": target,
            "status": status,
            "reason": reason[:500],
            "actor": actor,
        },
    )


async def _goal_for(store: NebulaStore, session: ChatSession) -> ChatGoal | None:
    from .chat_goals import ChatGoalService

    try:
        return await ChatGoalService(store).get(session.id)
    except NotFoundError:  # diagnostic-expected: an ordinary chat has no goal
        return None


def _routable(session: ChatSession) -> bool:
    metadata = session.metadata
    return not (
        metadata.get("control_assistant") is True
        or session.parent_session_id
        or metadata.get("side_chat")
        or metadata.get("temporary_assistant")
        or metadata.get("archived_at")
    )


# --------------------------------------------------------------------------
# Automatic evaluation at a turn boundary
# --------------------------------------------------------------------------


async def evaluate_goal_runtime_route(
    store: NebulaStore,
    session_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Bring an owned goal to its route before its next turn starts.

    Returns what was decided, or ``None`` when routing does not apply.
    """

    try:
        policy = load_routing_policy(environ)
    except RoutingConfigError as exc:
        record_caught_exception(
            "chat",
            "chat.goal.runtime_routing_config_invalid",
            "Goal runtime routing is on but its configuration is invalid; no goal is routed.",
            exc,
            stage="goal-runtime-routing",
        )
        return None
    if policy is None:
        return None
    session = await store.get(ChatSession, session_id)
    if not _routable(session):
        return None
    goal = await _goal_for(store, session)
    if goal is None or goal.status not in _LIVE_GOAL:
        return None
    project = await store.get(Engagement, session.engagement_id)
    if not policy.in_scope(project, session.id, goal.id):
        return None
    state = dict(session.metadata.get(ROUTING_KEY) or {})
    if state.get("released"):
        return {"outcome": "released"}
    now = now or utc_now()
    current, pending = await effective_config(store, session)
    signature = config_signature(current)
    if pending == "needs_review":
        return await _note_status(
            store,
            session,
            goal,
            state,
            "pending_review",
            None,
            signature,
            "a pending runtime switch needs operator review",
        )
    route, reason = await desired_route(store, policy, session, goal, state, now)
    owned = state.get("owned") is True
    applied = state.get("applied")
    if owned and isinstance(applied, dict) and signature != applied:
        # Someone changed the runtime by hand: theirs wins until an explicit
        # route action takes the goal back.
        def release(updated: dict[str, Any]) -> dict[str, Any]:
            updated.update(owned=False, status="out_of_policy", applied=None)
            return updated

        await _record(
            store,
            session,
            goal,
            release,
            {
                "action": "out_of_policy",
                "route": route.name if route else None,
                "from": describe(applied),
                "to": describe(signature),
                "reason": "the runtime was changed outside routing; routing leaves it "
                "until an operator or Butler route action",
                "actor": "core",
            },
        )
        return {"outcome": "out_of_policy", "runtime": signature}
    if route is None:
        return {"outcome": "no_route"}
    if not owned:
        if matches_route(signature, route):
            return await _adopt(store, session, goal, route, signature, reason)
        return await _note_status(
            store,
            session,
            goal,
            state,
            "out_of_policy",
            route,
            signature,
            f"{reason} selects {route.name}, but the goal's runtime "
            f"{describe(signature)} was chosen outside routing; it is left as is",
        )
    if matches_route(signature, route):
        if state.get("route") != route.name or state.get("status") not in {
            "routed",
            "in_policy",
            "pending",
        }:

            def settle(updated: dict[str, Any]) -> dict[str, Any]:
                updated.update(route=route.name, status="in_policy", deferred=None)
                return updated

            await _record(store, session, goal, settle, None)
        return {"outcome": "in_policy", "route": route.name}
    try:
        await check_route_ready(store, route, state=state)
    except (
        RoutingRefused
    ) as exc:  # diagnostic-expected: recorded on the goal as refused
        return await _note_status(
            store, session, goal, state, "refused", route, signature, str(exc)
        )
    switcher = store.runtime_switch_service
    busy = await switcher.reason(session) if switcher is not None else None
    if busy:
        # Never switch under a running turn: the next boundary applies it.
        if (state.get("deferred") or {}).get("route") != route.name:

            def defer(updated: dict[str, Any]) -> dict[str, Any]:
                updated["deferred"] = {"route": route.name, "reason": busy}
                return updated

            await _record(
                store,
                session,
                goal,
                defer,
                {
                    "action": "deferred",
                    "route": route.name,
                    "from": describe(signature),
                    "reason": f"{reason}; {busy}"[:500],
                    "actor": "core",
                },
            )
        return {"outcome": "deferred", "route": route.name, "reason": busy}
    saved = await _switch(
        store,
        session,
        goal,
        route,
        current,
        reason=reason,
        actor="core",
        action="routed",
    )
    return {
        "outcome": "routed",
        "route": route.name,
        "status": (saved.metadata.get(ROUTING_KEY) or {}).get("status"),
    }


async def _adopt(
    store: NebulaStore,
    session: ChatSession,
    goal: ChatGoal,
    route: Route,
    signature: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """A goal already on its route (by hand or queued) is in policy: no switch."""

    def adopt(state: dict[str, Any]) -> dict[str, Any]:
        state.update(
            owned=True,
            applied=signature,
            route=route.name,
            status="in_policy",
            released=False,
            main_models=_with_main_model(state, signature.get("model")),
        )
        return state

    await _record(
        store,
        session,
        goal,
        adopt,
        {
            "action": "in_policy",
            "route": route.name,
            "to": describe(signature),
            "reason": f"{reason}: the current or pending runtime already matches",
            "actor": "core",
        },
    )
    return {"outcome": "in_policy", "route": route.name}


async def _note_status(
    store: NebulaStore,
    session: ChatSession,
    goal: ChatGoal,
    state: Mapping[str, Any],
    status: str,
    route: Route | None,
    signature: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Record a non-switching status once per route and runtime."""

    key = {
        "status": status,
        "route": route.name if route else None,
        "runtime": signature,
    }
    if state.get("noted") != key:

        def note(updated: dict[str, Any]) -> dict[str, Any]:
            updated.update(status=status, noted=key)
            return updated

        await _record(
            store,
            session,
            goal,
            note,
            {
                "action": status,
                "route": route.name if route else None,
                "from": describe(signature),
                "reason": reason[:500],
                "actor": "core",
            },
        )
    return {"outcome": status, "route": route.name if route else None, "reason": reason}


# --------------------------------------------------------------------------
# Explicit operator and Butler route
# --------------------------------------------------------------------------


async def route_goal_runtime(
    store: NebulaStore,
    session_id: str,
    *,
    route: str,
    reason: str,
    actor: str,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Explicitly route one goal: a route name, ``auto`` or ``release``.

    A named route pins the goal to it; ``auto`` hands the goal to the policy
    (and switches now if its stage selects a different route); ``release``
    leaves the current runtime and stops routing the goal. A busy goal gets
    the waiting-for-idle pending switch that applies at its next idle boundary.
    """

    try:
        policy = load_routing_policy(environ)
    except RoutingConfigError as exc:
        raise RoutingRefused(str(exc)) from exc
    if policy is None:
        raise RoutingRefused(
            f"goal runtime routing is off; the operator sets {ENABLED_ENV}=1 and "
            f"{CONFIG_ENV} in Core's service environment"
        )
    reason = " ".join(str(reason or "").split())[:500]
    if not reason:
        raise RoutingRefused("give a reason for routing this goal")
    session = await store.get(ChatSession, session_id)
    if not _routable(session):
        raise RoutingRefused("only a saved main goal chat can be routed")
    goal = await _goal_for(store, session)
    if goal is None or goal.status in {
        ChatGoalStatus.COMPLETED,
        ChatGoalStatus.CANCELLED,
    }:
        raise RoutingRefused("this chat has no open goal")
    project = await store.get(Engagement, session.engagement_id)
    if not policy.in_scope(project, session.id, goal.id):
        raise RoutingRefused(
            f"this goal is outside the routing policy's projects/goals scope ({CONFIG_ENV})"
        )
    if route not in {"auto", "release"} and policy.route(route) is None:
        raise RoutingRefused(
            "choose auto, release or one of the routes: "
            + ", ".join(item.name for item in policy.routes)
        )
    state = dict(session.metadata.get(ROUTING_KEY) or {})
    current, pending = await effective_config(store, session)
    signature = config_signature(current)
    if pending == "needs_review":
        raise RoutingRefused(
            "this goal's pending runtime switch needs review; resolve it first"
        )
    if route == "release":
        if dry_run:
            return {"dry_run": True, "outcome": "release", "runtime": signature}

        def release(updated: dict[str, Any]) -> dict[str, Any]:
            updated.update(
                owned=False,
                released=True,
                override=None,
                applied=None,
                status="released",
            )
            return updated

        await _record(
            store,
            session,
            goal,
            release,
            {
                "action": "released",
                "from": describe(signature),
                "reason": reason,
                "actor": actor,
            },
        )
        return {"outcome": "released", "runtime": signature}
    planned = {
        **state,
        "override": None if route == "auto" else route,
        "owned": True,
        "released": False,
    }
    target_route, why = await desired_route(
        store, policy, session, goal, planned, utc_now()
    )
    if target_route is not None:
        await check_route_ready(store, target_route, state=state)
    switcher = store.runtime_switch_service
    busy = await switcher.reason(session) if switcher is not None else None
    in_policy = target_route is None or matches_route(signature, target_route)
    plan = {
        "route": target_route.name if target_route else None,
        "why": why or "no route matches the goal's stage",
        "from": signature,
        "to": (
            target_signature(target_route, signature)
            if target_route is not None
            else signature
        ),
        "would_switch": not in_policy,
        "busy": busy,
    }
    if dry_run:
        return {"dry_run": True, **plan}

    def own(updated: dict[str, Any]) -> dict[str, Any]:
        updated.update(
            override=planned["override"],
            owned=True,
            released=False,
            applied=signature,
            route=target_route.name if target_route else None,
            status="in_policy",
            noted=None,
            main_models=_with_main_model(updated, signature.get("model")),
        )
        return updated

    if in_policy:
        await _record(
            store,
            session,
            goal,
            own,
            {
                "action": "in_policy",
                "route": plan["route"],
                "to": describe(signature),
                "reason": f"{reason} ({plan['why']})",
                "actor": actor,
            },
        )
        return {"outcome": "in_policy", **plan}
    assert target_route is not None
    saved = await _switch(
        store,
        session,
        goal,
        target_route,
        current,
        reason=f"{reason} ({plan['why']})",
        actor=actor,
        action="routed",
        extra_state={"override": planned["override"], "released": False, "noted": None},
    )
    return {
        "outcome": (saved.metadata.get(ROUTING_KEY) or {}).get("status", "routed"),
        **plan,
    }


async def routing_status(
    store: NebulaStore,
    session_id: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read-only routing view of one goal chat."""

    try:
        policy = load_routing_policy(environ)
        problem = None
    except RoutingConfigError as exc:  # diagnostic-expected: returned as config_error
        policy, problem = None, str(exc)
    session = await store.get(ChatSession, session_id)
    current, pending = await effective_config(store, session)
    result: dict[str, Any] = {
        "enabled": policy is not None,
        "config_error": problem,
        "runtime": config_signature(current),
        "pending_switch": pending,
        "state": session.metadata.get(ROUTING_KEY) or {},
    }
    goal = await _goal_for(store, session)
    if policy is not None and goal is not None:
        state = session.metadata.get(ROUTING_KEY) or {}
        route, why = await desired_route(store, policy, session, goal, state, utc_now())
        result["desired_route"] = route.name if route else None
        result["why"] = why
    return result


# --------------------------------------------------------------------------
# Rate-limit fallback
# --------------------------------------------------------------------------


def is_rate_limit_failure(turn: HarnessTurn) -> bool:
    """Whether a harness turn stopped at a usage or rate limit."""

    diagnostic = turn.metadata.get("diagnostic")
    diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
    if diagnostic.get("reason_code") in _RATE_LIMIT_REASONS:
        return True
    info = diagnostic.get("vendor_error_info")
    if isinstance(info, dict):
        if info.get("api_error_status") == 429:
            return True
        if {"rateLimitExceeded", "usageLimitExceeded"} & set(info):
            return True
    elif info in {"rateLimitExceeded", "usageLimitExceeded", "rate_limit"}:
        return True
    return bool(_RATE_LIMIT_TEXT.search(str(turn.error or "")))


async def run_goal_turn_on_fallback(
    runtime: Any,
    harness_turn_id: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Rerun a rate-limited harness goal turn on its route's fallback runtime.

    Returns whether the goal continues on the fallback (the caller then skips
    its own failure handling). Any refusal leaves the failure as it was.
    """

    from .chat_goals import ChatGoalService, GoalWrite

    store: NebulaStore = runtime.store
    try:
        policy = load_routing_policy(environ)
    except RoutingConfigError:  # diagnostic-expected: recorded at the next boundary
        return False
    if policy is None:
        return False
    turn = await store.get(HarnessTurn, harness_turn_id)
    if (
        turn.origin != HarnessTurnOrigin.CHAT
        or turn.status not in {HarnessTurnStatus.FAILED, HarnessTurnStatus.INTERRUPTED}
        or not turn.chat_turn_id
        or not turn.chat_session_id
        or not is_rate_limit_failure(turn)
    ):
        return False
    owner = await store.get(ChatTurn, turn.chat_turn_id)
    if not owner.goal_id:
        return False
    session = await store.get(ChatSession, turn.chat_session_id)
    state = dict(session.metadata.get(ROUTING_KEY) or {})
    route = policy.route(state.get("route"))
    fallback = policy.route(route.fallback) if route is not None else None
    if (
        route is None
        or fallback is None
        or state.get("owned") is not True
        or route.main.backend != "harness"
        or session.backend != ChatBackend.HARNESS
        or session.harness_profile_id != route.main.harness_profile_id
    ):
        return False
    goals = ChatGoalService(store)
    goal = await goals.get(session.id)
    if goal.id != owner.goal_id or not (
        goal.status == ChatGoalStatus.RUNNING
        or (goal.status == ChatGoalStatus.PAUSED and goal.blocked_reason == turn.error)
    ):
        return False
    switcher = store.runtime_switch_service
    if switcher is not None and await switcher.reason(session):
        # A switch must not wait behind this turn's own recovery; the normal
        # failure handling keeps the goal.
        return False
    try:
        await check_route_ready(store, fallback)
    except RoutingRefused as exc:
        record_caught_exception(
            "chat",
            "chat.goal.runtime_fallback_refused",
            "A rate-limited goal turn could not use its fallback runtime.",
            exc,
            stage="goal-runtime-routing",
        )
        return False
    until = utc_now() + timedelta(seconds=route.fallback_cooldown_seconds)
    current, _ = await effective_config(store, session)
    try:
        await _switch(
            store,
            session,
            goal,
            fallback,
            current,
            reason=(
                f"the {route.name} harness turn stopped at a usage or rate limit; "
                f"this turn runs on {fallback.name}, and {route.name} is tried again "
                f"after {until.isoformat()}"
            ),
            actor="core",
            action="rate_limit_fallback",
            extra_state={
                "cooldown": {
                    "route": route.name,
                    "until": until.isoformat(),
                    "turn_id": turn.id,
                }
            },
        )
    except Exception:  # diagnostic-expected: _switch recorded the cause
        return False
    goal = await goals.get(session.id)
    if goal.status == ChatGoalStatus.PAUSED:
        try:
            await goals.write(
                session.id, GoalWrite(expected_revision=goal.revision, action="resume")
            )
        except (ConflictError, NotFoundError, ValueError):
            # diagnostic-expected: an operator action on the goal wins; it
            # stays on the fallback runtime and resumes from there.
            return False
    dispatched = await runtime.dispatch_running_goal(session.id, FALLBACK_INSTRUCTION)
    return (
        dispatched is not None
        or (await goals.get(session.id)).status == ChatGoalStatus.RUNNING
    )


# --------------------------------------------------------------------------
# Reviewer independence
# --------------------------------------------------------------------------


def model_family(model: str | None) -> str:
    from .butler_independent_review import normalize_model

    value = normalize_model(model)
    return re.split(r"[-_]", value, maxsplit=1)[0] if value else ""


def goal_main_models(session: Any) -> tuple[list[str], bool]:
    """Main models the goal runs or ran on, and whether routing moved it."""

    metadata = getattr(session, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    model = getattr(session, "model", None)
    models = [model] if model else []
    pending = metadata.get("pending_runtime_switch")
    if isinstance(pending, dict) and isinstance(pending.get("binding"), dict):
        target = pending["binding"].get("model")
        if isinstance(target, str) and target:
            models.append(target)
    state = metadata.get(ROUTING_KEY)
    routed = isinstance(state, dict) and bool(state.get("main_models"))
    if routed:
        assert isinstance(state, dict)
        models += [str(item) for item in state.get("main_models") or []]
    return list(dict.fromkeys(models)), routed


def reviewer_conflict(
    session: Any,
    reviewer_model: str | None,
    *,
    reviewer_harness_profile_id: str | None = None,
) -> str | None:
    """Why a reviewer is not independent of the goal's main runtime, if it is not.

    The reviewer must differ from the goal's current main model and from the
    model of a pending runtime switch. Once routing moved the goal, a reviewer
    of the same model family as any main model the goal ran on shares that
    context and is refused as well. A reviewer on the goal's own harness
    profile is refused.
    """

    from .butler_independent_review import normalize_model

    reviewer = normalize_model(reviewer_model)
    if not reviewer:
        return "the reviewer has no model"
    if reviewer == normalize_model(getattr(session, "model", None)):
        return (
            f"the reviewer model {reviewer_model} is the goal's current main "
            "runtime model"
        )
    if (
        reviewer_harness_profile_id
        and getattr(session, "backend", None) == ChatBackend.HARNESS
        and getattr(session, "harness_profile_id", None) == reviewer_harness_profile_id
    ):
        return (
            "the reviewer would run on the goal's own main harness profile "
            f"{reviewer_harness_profile_id}"
        )
    models, routed = goal_main_models(session)
    for model in models:
        if reviewer == normalize_model(model):
            return (
                f"the reviewer model {reviewer_model} is a main runtime model of "
                "this goal (current, pending or routed)"
            )
        if routed and model_family(model) == model_family(reviewer_model):
            return (
                f"this goal's main agent was routed to {model}; a "
                f"{model_family(model)} reviewer shares that model family and "
                "context. Configure a reviewer from another model family"
            )
    return None


# --------------------------------------------------------------------------
# Butler supervisor tool
# --------------------------------------------------------------------------

_TOOL_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "route": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "A route name from the policy, auto, or release.",
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["session_id", "route", "reason"],
    "additionalProperties": False,
}


class ButlerGoalRuntimeRouting:
    """Butler's explicit goal runtime route, behind the supervisor kill switch."""

    def __init__(
        self,
        store: NebulaStore,
        project_id: str,
        *,
        actor: str = "butler",
        policy_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.store = store
        self.project_id = project_id
        self.actor = actor
        self._policy_loader = policy_loader

    async def route(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .butler_supervisor_policy import load_policy, write_audit

        policy = (self._policy_loader or load_policy)()
        policy.require_enabled()
        session_id = str(arguments["session_id"])
        session = await self.store.get(ChatSession, session_id)
        if session.engagement_id not in {self.project_id, *policy.hygiene_project_ids}:
            raise PermissionError(
                "choose a goal chat in this project or a supervised hygiene project"
            )
        audit = {
            "tool": "route_goal_runtime",
            "project_id": session.engagement_id,
            "session_id": session_id,
            "route": arguments.get("route"),
            "dry_run": bool(arguments.get("dry_run")),
            "actor": self.actor,
        }
        try:
            result = await route_goal_runtime(
                self.store,
                session_id,
                route=str(arguments["route"]),
                reason=str(arguments.get("reason") or ""),
                actor=f"butler:{self.actor}",
                dry_run=bool(arguments.get("dry_run")),
            )
        except RoutingRefused as exc:
            write_audit(policy, {**audit, "allowed": False, "detail": str(exc)})
            raise
        write_audit(
            policy, {**audit, "allowed": True, "outcome": result.get("outcome")}
        )
        return result

    def actions(self) -> list[AnalysisTool]:
        return [
            AnalysisTool(
                ToolSpec(
                    name=TOOL_NAME,
                    description=(
                        "Route one goal chat's runtime under the operator's goal "
                        "runtime routing policy (NEBULA_GOAL_RUNTIME_ROUTING): a "
                        "route name pins the goal to that route, auto hands it to "
                        "the stage policy, release stops routing it. The switch is "
                        "an audited Assistant settings change that keeps the goal's "
                        "MCP servers; a busy goal switches at its next idle turn "
                        "boundary. Refused while routing is off, outside its scope, "
                        "or toward a harness profile without a verified Test turn. "
                        "dry_run shows the plan without changing anything."
                    ),
                    input_schema=_TOOL_INPUT,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                self.route,
            )
        ]
