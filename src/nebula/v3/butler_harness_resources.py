"""Durable project observations of vendor-reported harness quota windows."""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from contextlib import suppress
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .butler_oversight import (
    ButlerEventPayload,
    ButlerEvidenceRef,
    _append_project_event,
    _entity_in_transaction,
    incident_id,
    project_stream_id,
)
from .diagnostics import create_diagnostic_task
from .domain import (
    ButlerGoalWatch,
    ButlerHarnessQuota,
    ButlerIncident,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    utc_now,
)
from .harness_quota import HarnessQuotaSnapshot
from .runtime_usage import HarnessQuotaRead, RuntimeUsageService
from .storage import NebulaStore, NotFoundError

if TYPE_CHECKING:
    from .storage import StoreTransaction


LOW_QUOTA_PERCENT = 20.0
AUTO_CHECK_INTERVAL = timedelta(minutes=1)
_ACTIVE_GOALS = {
    ChatGoalStatus.RUNNING,
    ChatGoalStatus.PAUSED,
    ChatGoalStatus.BLOCKED,
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def quota_state_id(engagement_id: str, profile_id: str, window_id: str) -> str:
    return f"butler-quota:{_hash(':'.join((engagement_id, profile_id, window_id)))}"


def _window_instance(snapshot: HarnessQuotaSnapshot) -> str:
    return _hash(
        snapshot.resets_at.isoformat() if snapshot.resets_at else "unknown-reset"
    )


def _incident_key(profile_id: str, window_id: str, window_instance: str) -> str:
    return (
        f"harness-low-quota:{_hash(':'.join((profile_id, window_id, window_instance)))}"
    )


def _rate_limit_incident_key(profile_id: str) -> str:
    return f"harness-rate-limited:{_hash(profile_id)}"


def _candidate_watches(store: NebulaStore, profile_id: str) -> dict[str, list[str]]:
    projects: dict[str, list[str]] = defaultdict(list)
    for watch in store.iter_readable_entities(ButlerGoalWatch, {"closed_at": None}):
        if watch.status not in _ACTIVE_GOALS:
            continue
        try:
            session = store.get(ChatSession, watch.session_id)
        except NotFoundError:
            continue
        if (
            session.engagement_id == watch.engagement_id
            and session.harness_profile_id == profile_id
        ):
            projects[watch.engagement_id].append(watch.id)
    return projects


def _current_binding(
    transaction: StoreTransaction,
    *,
    engagement_id: str,
    watch_ids: list[str],
    profile_id: str,
    harness_kind: HarnessKind,
) -> tuple[list[str], list[str]]:
    profile = _entity_in_transaction(transaction, HarnessProfile, profile_id)
    if profile is None or not profile.enabled or profile.kind != harness_kind:
        return [], []
    goal_ids: list[str] = []
    session_ids: list[str] = []
    for watch_id in watch_ids:
        watch = _entity_in_transaction(transaction, ButlerGoalWatch, watch_id)
        if (
            watch is None
            or watch.engagement_id != engagement_id
            or watch.closed_at is not None
            or watch.status not in _ACTIVE_GOALS
        ):
            continue
        goal = _entity_in_transaction(transaction, ChatGoal, watch.goal_id)
        session = _entity_in_transaction(transaction, ChatSession, watch.session_id)
        if (
            goal is None
            or goal.engagement_id != engagement_id
            or goal.status not in _ACTIVE_GOALS
            or session is None
            or session.engagement_id != engagement_id
            or session.harness_profile_id != profile_id
        ):
            continue
        goal_ids.append(goal.id)
        session_ids.append(session.id)
    return list(dict.fromkeys(goal_ids))[-100:], list(dict.fromkeys(session_ids))[-100:]


def _transition_incident(
    transaction: StoreTransaction,
    *,
    engagement_id: str,
    key: str,
    snapshot: HarnessQuotaSnapshot,
    state_ref: ButlerEvidenceRef,
    goal_ids: list[str],
    session_ids: list[str],
    low: bool,
    window_reset: bool = False,
) -> None:
    record_id = incident_id(engagement_id, key)
    previous = _entity_in_transaction(transaction, ButlerIncident, record_id)
    if low:
        impact = (
            f"{snapshot.window_label}: {snapshot.remaining_percent:.1f}% remaining"
            + (
                f"; resets at {snapshot.resets_at.isoformat()}"
                if snapshot.resets_at
                else "."
            )
        )
        if previous is None:
            incident = ButlerIncident(
                id=record_id,
                engagement_id=engagement_id,
                incident_key=key,
                source="harness_quota",
                verification_state="vendor_reported",
                summary="Harness quota is at or below 20% remaining.",
                impact=impact,
                next_action="Review the quota window or select another harness.",
                evidence_refs=[state_ref.model_dump()],
                goal_ids=goal_ids,
                session_ids=session_ids,
                observed_at=snapshot.observed_at,
            )
            transaction.add(incident)
            event_type = "incident.opened"
        else:
            reopened = previous.status == "resolved"
            incident = transaction.update(
                ButlerIncident,
                record_id,
                {
                    "status": "open" if reopened else previous.status,
                    "impact": impact,
                    "evidence_refs": [state_ref.model_dump()],
                    "goal_ids": list(dict.fromkeys([*previous.goal_ids, *goal_ids]))[
                        -100:
                    ],
                    "session_ids": list(
                        dict.fromkeys([*previous.session_ids, *session_ids])
                    )[-100:],
                    "observed_at": snapshot.observed_at
                    if reopened
                    else previous.observed_at,
                    "acknowledged_at": None if reopened else previous.acknowledged_at,
                    "resolved_at": None,
                },
                expected_revision=previous.revision,
            )
            event_type = "incident.reopened" if reopened else "incident.updated"
    elif previous is not None and previous.status != "resolved":
        incident = transaction.update(
            ButlerIncident,
            record_id,
            {"status": "resolved", "resolved_at": snapshot.observed_at},
            expected_revision=previous.revision,
        )
        event_type = "incident.resolved"
    else:
        return
    _append_project_event(
        transaction,
        engagement_id=engagement_id,
        event_type=event_type,
        payload=ButlerEventPayload(
            project_id=engagement_id,
            source="harness",
            actor_role="core",
            summary=(
                "Previous harness quota window ended."
                if window_reset
                else incident.summary
                if low
                else "Harness quota rose above 20%."
            ),
            metrics={
                "harness_kind": snapshot.harness_kind.value,
                "remaining_percent": snapshot.remaining_percent,
            },
            verification_state="vendor_reported",
            evidence_refs=[
                ButlerEvidenceRef(
                    kind="incident", id=incident.id, project_id=engagement_id
                ),
                state_ref,
            ],
            incident_key=key,
        ),
        idempotency_key=f"harness-quota-incident:{incident.id}:{snapshot.observed_at.isoformat()}:{event_type}",
        now=utc_now(),
    )


def record_harness_quota(
    store: NebulaStore, *, profile_id: str, snapshot: HarnessQuotaSnapshot
) -> int:
    """Persist only authoritative numeric windows for affected watched projects."""

    if snapshot.source not in {
        "codex.account/rateLimits/read",
        "claude.RateLimitEvent",
    }:
        return 0
    observation_key = _hash(
        f"{profile_id}:{snapshot.window_id}:{snapshot.observed_at.isoformat()}:"
        f"{snapshot.used_percent}:{snapshot.remaining_percent}:{snapshot.resets_at}"
    )
    instance = _window_instance(snapshot)
    low = snapshot.remaining_percent <= LOW_QUOTA_PERCENT
    saved = 0
    for engagement_id, watch_ids in _candidate_watches(store, profile_id).items():
        with store.transaction(immediate=True) as transaction:
            transaction.lock_operation_stream(project_stream_id(engagement_id))
            goal_ids, session_ids = _current_binding(
                transaction,
                engagement_id=engagement_id,
                watch_ids=watch_ids,
                profile_id=profile_id,
                harness_kind=snapshot.harness_kind,
            )
            if not session_ids:
                continue
            state_id = quota_state_id(engagement_id, profile_id, snapshot.window_id)
            previous = _entity_in_transaction(transaction, ButlerHarnessQuota, state_id)
            if previous is not None and previous.observed_at > snapshot.observed_at:
                # Vendor notices can arrive after a newer window was saved.
                continue
            if previous is not None and previous.observation_key == observation_key:
                continue
            values = {
                "window_label": snapshot.window_label,
                "window_instance": instance,
                "observation_key": observation_key,
                "source": snapshot.source,
                "used_percent": snapshot.used_percent,
                "remaining_percent": snapshot.remaining_percent,
                "observed_at": snapshot.observed_at,
                "resets_at": snapshot.resets_at,
                "threshold_state": "at_or_below" if low else "above",
            }
            if previous is None:
                transaction.add(
                    ButlerHarnessQuota(
                        id=state_id,
                        engagement_id=engagement_id,
                        harness_profile_id=profile_id,
                        window_id=snapshot.window_id,
                        **values,
                    )
                )
            else:
                transaction.update(
                    ButlerHarnessQuota,
                    state_id,
                    values,
                    expected_revision=previous.revision,
                )
            state_ref = ButlerEvidenceRef(
                kind="resource", id=state_id, project_id=engagement_id
            )
            _append_project_event(
                transaction,
                engagement_id=engagement_id,
                event_type="resource.harness_quota_observed",
                payload=ButlerEventPayload(
                    project_id=engagement_id,
                    source="harness",
                    actor_role="core",
                    summary="Harness quota window observed.",
                    metrics={
                        "harness_profile_id": profile_id,
                        "quota_source": snapshot.source,
                        "window_id": snapshot.window_id,
                        "used_percent": snapshot.used_percent,
                        "remaining_percent": snapshot.remaining_percent,
                        "observed_at": snapshot.observed_at.isoformat(),
                    },
                    verification_state="vendor_reported",
                    evidence_refs=[state_ref],
                ),
                idempotency_key=f"harness-quota:{observation_key}",
                now=utc_now(),
            )
            if previous is not None and previous.window_instance != instance:
                _transition_incident(
                    transaction,
                    engagement_id=engagement_id,
                    key=_incident_key(
                        profile_id, snapshot.window_id, previous.window_instance
                    ),
                    snapshot=snapshot,
                    state_ref=state_ref,
                    goal_ids=goal_ids,
                    session_ids=session_ids,
                    low=False,
                    window_reset=True,
                )
            _transition_incident(
                transaction,
                engagement_id=engagement_id,
                key=_incident_key(profile_id, snapshot.window_id, instance),
                snapshot=snapshot,
                state_ref=state_ref,
                goal_ids=goal_ids,
                session_ids=session_ids,
                low=low,
            )
        saved += 1
    return saved


def record_harness_rate_limit(
    store: NebulaStore,
    *,
    profile_id: str,
    turn_id: str,
    activity_event_id: str,
    observed_at: datetime,
    recovered: bool = False,
) -> bool:
    """Record a vendor rejection separately from unknown numeric quota.

    A later successful usage event from this profile resolves the rejection.
    The saved harness turn, session, and active watches bind the event to one
    project before a project-scoped incident is written.
    """

    try:
        turn = store.get(HarnessTurn, turn_id)
        session = store.get(HarnessSession, turn.harness_session_id)
        profile = store.get(HarnessProfile, profile_id)
    except NotFoundError:
        return False
    if (
        turn.engagement_id != session.engagement_id
        or session.harness_profile_id != profile_id
        or not profile.enabled
    ):
        return False
    engagement_id = turn.engagement_id
    watch_ids = _candidate_watches(store, profile_id).get(engagement_id, [])
    if not watch_ids:
        return False
    with store.transaction(immediate=True) as transaction:
        transaction.lock_operation_stream(project_stream_id(engagement_id))
        goal_ids, session_ids = _current_binding(
            transaction,
            engagement_id=engagement_id,
            watch_ids=watch_ids,
            profile_id=profile_id,
            harness_kind=profile.kind,
        )
        if not session_ids:
            return False
        key = _rate_limit_incident_key(profile_id)
        record_id = incident_id(engagement_id, key)
        prior = _entity_in_transaction(transaction, ButlerIncident, record_id)
        if recovered:
            if (
                prior is None
                or prior.status == "resolved"
                or observed_at < prior.observed_at
            ):
                return False
            incident = transaction.update(
                ButlerIncident,
                prior.id,
                {"status": "resolved", "resolved_at": observed_at},
                expected_revision=prior.revision,
            )
            event_type = "incident.resolved"
            summary = "Harness usage succeeded after a rate-limit rejection."
        else:
            if prior is not None and (
                prior.status != "resolved"
                or prior.resolved_at is not None
                and observed_at <= prior.resolved_at
            ):
                return False
            if prior is None:
                incident = ButlerIncident(
                    id=record_id,
                    engagement_id=engagement_id,
                    incident_key=key,
                    source="harness_rate_limit",
                    verification_state="vendor_reported",
                    summary="Harness rejected a request because of a rate limit.",
                    impact="Harness requests may be delayed; numeric quota remains unknown.",
                    next_action="Wait for the rate limit to clear or select another harness.",
                    evidence_refs=[
                        ButlerEvidenceRef(
                            kind="harness_turn", id=turn_id, project_id=engagement_id
                        ).model_dump()
                    ],
                    goal_ids=goal_ids,
                    session_ids=session_ids,
                    observed_at=observed_at,
                )
                transaction.add(incident)
                event_type = "incident.opened"
            else:
                incident = transaction.update(
                    ButlerIncident,
                    prior.id,
                    {
                        "status": "open",
                        "acknowledged_at": None,
                        "resolved_at": None,
                        "observed_at": observed_at,
                        "goal_ids": goal_ids,
                        "session_ids": session_ids,
                        "evidence_refs": [
                            ButlerEvidenceRef(
                                kind="harness_turn",
                                id=turn_id,
                                project_id=engagement_id,
                            ).model_dump()
                        ],
                    },
                    expected_revision=prior.revision,
                )
                event_type = "incident.reopened"
            summary = "Harness rate-limit rejection was reported by the vendor."
        _append_project_event(
            transaction,
            engagement_id=engagement_id,
            event_type=event_type,
            payload=ButlerEventPayload(
                project_id=engagement_id,
                source="harness",
                actor_role="core",
                summary=summary,
                metrics={
                    "harness_profile_id": profile_id,
                    "reason_code": "rate_limited",
                },
                verification_state="vendor_reported",
                causal_event_id=activity_event_id,
                evidence_refs=[
                    ButlerEvidenceRef(
                        kind="incident", id=incident.id, project_id=engagement_id
                    ),
                    ButlerEvidenceRef(
                        kind="harness_turn", id=turn_id, project_id=engagement_id
                    ),
                ],
                incident_key=key,
            ),
            idempotency_key=f"harness-rate-limit:{record_id}:{activity_event_id}",
            now=observed_at,
        )
    return True


class HarnessQuotaObserver:
    """Read Codex quota after use and accept Claude numeric activity notices."""

    def __init__(self, store: NebulaStore, runtime_usage: RuntimeUsageService) -> None:
        self.store = store
        self.runtime_usage = runtime_usage
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduled: set[asyncio.Task[object]] = set()
        self._inflight: dict[str, asyncio.Task[HarnessQuotaRead]] = {}
        self._recent: dict[str, HarnessQuotaRead] = {}
        self._rate_limit_locks: dict[str, asyncio.Lock] = {}

    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()

    async def shutdown(self) -> None:
        self._loop = None
        tasks = [*self._scheduled, *self._inflight.values()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._scheduled.clear()
        self._inflight.clear()
        self._rate_limit_locks.clear()

    def schedule_read(self, profile_id: str) -> None:
        try:
            profile = self.store.get(HarnessProfile, profile_id)
        except NotFoundError:
            return
        if not profile.enabled or profile.kind != HarnessKind.CODEX_APP_SERVER:
            return
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._schedule_on_loop, profile_id)

    def _schedule_on_loop(self, profile_id: str) -> None:
        task = create_diagnostic_task(
            self.refresh(profile_id),
            feature="butler",
            event_code="butler.harness_quota_check_failed",
            failure_message="Harness quota could not be checked after use.",
            name=f"nebula-harness-quota-{profile_id[:24]}",
        )
        self._scheduled.add(task)
        task.add_done_callback(self._scheduled.discard)

    def submit_snapshot(self, profile_id: str, snapshot: HarnessQuotaSnapshot) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._submit_on_loop, profile_id, snapshot)

    def submit_rate_limit(
        self,
        profile_id: str,
        turn_id: str,
        activity_event_id: str,
        observed_at: datetime,
        recovered: bool,
    ) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(
                self._submit_rate_limit_on_loop,
                profile_id,
                turn_id,
                activity_event_id,
                observed_at,
                recovered,
            )

    def _submit_rate_limit_on_loop(
        self,
        profile_id: str,
        turn_id: str,
        activity_event_id: str,
        observed_at: datetime,
        recovered: bool,
    ) -> None:
        task = create_diagnostic_task(
            self._record_rate_limit_serially(
                profile_id, turn_id, activity_event_id, observed_at, recovered
            ),
            feature="butler",
            event_code="butler.harness_rate_limit_record_failed",
            failure_message="A saved harness rate-limit notice could not be retained.",
            name=f"nebula-harness-rate-limit-{profile_id[:24]}",
        )
        self._scheduled.add(task)
        task.add_done_callback(self._scheduled.discard)

    async def _record_rate_limit_serially(
        self,
        profile_id: str,
        turn_id: str,
        activity_event_id: str,
        observed_at: datetime,
        recovered: bool,
    ) -> None:
        lock = self._rate_limit_locks.setdefault(profile_id, asyncio.Lock())
        async with lock:
            await asyncio.to_thread(
                record_harness_rate_limit,
                self.store,
                profile_id=profile_id,
                turn_id=turn_id,
                activity_event_id=activity_event_id,
                observed_at=observed_at,
                recovered=recovered,
            )

    def _submit_on_loop(self, profile_id: str, snapshot: HarnessQuotaSnapshot) -> None:
        task = create_diagnostic_task(
            asyncio.to_thread(
                record_harness_quota,
                self.store,
                profile_id=profile_id,
                snapshot=snapshot,
            ),
            feature="butler",
            event_code="butler.harness_quota_record_failed",
            failure_message="A reported harness quota window could not be retained.",
            name=f"nebula-harness-quota-record-{profile_id[:24]}",
        )
        self._scheduled.add(task)
        task.add_done_callback(self._scheduled.discard)

    async def refresh(
        self, profile_id: str, *, force: bool = False
    ) -> HarnessQuotaRead:
        profile = self.store.get(HarnessProfile, profile_id)
        if profile.kind != HarnessKind.CODEX_APP_SERVER:
            return await self.runtime_usage.read_harness_quota(
                profile_id, refresh=force
            )
        recent = self._recent.get(profile_id)
        if (
            not force
            and recent is not None
            and recent.checked_at is not None
            and utc_now() - recent.checked_at < AUTO_CHECK_INTERVAL
        ):
            for snapshot in recent.snapshots:
                await asyncio.to_thread(
                    record_harness_quota,
                    self.store,
                    profile_id=profile_id,
                    snapshot=snapshot,
                )
            return recent
        task = self._inflight.get(profile_id)
        if task is None:
            task = asyncio.create_task(self._read_and_record(profile_id))
            self._inflight[profile_id] = task
            task.add_done_callback(
                lambda completed: (
                    self._inflight.pop(profile_id, None)
                    if self._inflight.get(profile_id) is completed
                    else None
                )
            )
        return await asyncio.shield(task)

    async def _read_and_record(self, profile_id: str) -> HarnessQuotaRead:
        result = await self.runtime_usage.read_harness_quota(profile_id, refresh=True)
        for snapshot in result.snapshots:
            await asyncio.to_thread(
                record_harness_quota,
                self.store,
                profile_id=profile_id,
                snapshot=snapshot,
            )
        self._recent[profile_id] = result
        return result
