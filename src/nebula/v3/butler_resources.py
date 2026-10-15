"""Project-scoped OpenRouter credit observations and threshold incidents.

The credit reader owns the external request. This module only associates an
observed account with active watched sessions and saves a compact receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from contextlib import suppress
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx

from .butler_oversight import (
    ButlerEventPayload,
    ButlerEvidenceRef,
    _append_project_event,
    _entity_in_transaction,
    incident_id,
)
from .credentials import CredentialStore
from .diagnostics import create_diagnostic_task
from .domain import (
    ButlerAccountCredit,
    ButlerGoalWatch,
    ButlerIncident,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ProviderProfile,
    utc_now,
)
from .provider_credits import OpenRouterCreditSnapshot, read_openrouter_credits
from .storage import NebulaStore, NotFoundError

if TYPE_CHECKING:
    from .storage import StoreTransaction


LOW_CREDIT_USD = 20.0
AUTO_CHECK_INTERVAL = timedelta(minutes=1)
_ACTIVE_GOALS = {
    ChatGoalStatus.RUNNING,
    ChatGoalStatus.PAUSED,
    ChatGoalStatus.BLOCKED,
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def account_key(management_ref: str) -> str:
    """An internal account correlation key that does not expose a secret ref."""

    return _hash(management_ref)


def credit_state_id(engagement_id: str, key: str) -> str:
    return f"butler-credit:{_hash(engagement_id + ':' + key)}"


def _candidate_watches(store: NebulaStore, management_ref: str) -> dict[str, list[str]]:
    """Find projects with an active Watch using this account's profile."""

    projects: dict[str, list[str]] = defaultdict(list)
    profiles: dict[str, ProviderProfile | None] = {}
    for watch in store.iter_readable_entities(ButlerGoalWatch, {"closed_at": None}):
        if watch.status not in _ACTIVE_GOALS:
            continue
        try:
            session = store.get(ChatSession, watch.session_id)
        except NotFoundError:
            continue
        if (
            session.engagement_id != watch.engagement_id
            or not session.provider_profile_id
        ):
            continue
        provider_id = session.provider_profile_id
        if provider_id not in profiles:
            try:
                profiles[provider_id] = store.get(ProviderProfile, provider_id)
            except NotFoundError:
                profiles[provider_id] = None
        profile = profiles[provider_id]
        if (
            profile is not None
            and profile.enabled
            and profile.provider_type == "openrouter"
            and profile.openrouter_management_secret_ref == management_ref
        ):
            projects[watch.engagement_id].append(watch.id)
    return projects


def _current_binding(
    transaction: StoreTransaction,
    *,
    engagement_id: str,
    watch_ids: list[str],
    management_ref: str,
) -> tuple[list[str], list[str], list[str]]:
    """Recheck the project and provider association in the write transaction."""

    goal_ids: list[str] = []
    session_ids: list[str] = []
    provider_ids: list[str] = []
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
            or not session.provider_profile_id
        ):
            continue
        profile = _entity_in_transaction(
            transaction, ProviderProfile, session.provider_profile_id
        )
        if (
            profile is None
            or not profile.enabled
            or profile.provider_type != "openrouter"
            or profile.openrouter_management_secret_ref != management_ref
        ):
            continue
        goal_ids.append(goal.id)
        session_ids.append(session.id)
        provider_ids.append(profile.id)
    return (
        list(dict.fromkeys(goal_ids))[-100:],
        list(dict.fromkeys(session_ids))[-100:],
        list(dict.fromkeys(provider_ids))[-100:],
    )


def record_openrouter_credit(
    store: NebulaStore, *, snapshot: OpenRouterCreditSnapshot, management_ref: str
) -> int:
    """Commit each affected project's balance, event, and threshold transition."""

    if snapshot.status != "observed" or snapshot.observed_at is None:
        return 0
    key = account_key(management_ref)
    observation_key = _hash(
        f"{key}\0{snapshot.observed_at.isoformat()}\0"
        f"{snapshot.total_credits_usd}\0{snapshot.total_usage_usd}\0{snapshot.remaining_usd}"
    )
    low = (
        snapshot.remaining_usd is not None and snapshot.remaining_usd <= LOW_CREDIT_USD
    )
    saved = 0
    for engagement_id, watch_ids in _candidate_watches(store, management_ref).items():
        with store.transaction(immediate=True) as transaction:
            from .butler_oversight import project_stream_id

            transaction.lock_operation_stream(project_stream_id(engagement_id))
            goal_ids, session_ids, provider_ids = _current_binding(
                transaction,
                engagement_id=engagement_id,
                watch_ids=watch_ids,
                management_ref=management_ref,
            )
            if not provider_ids:
                continue
            state_id = credit_state_id(engagement_id, key)
            previous = _entity_in_transaction(
                transaction, ButlerAccountCredit, state_id
            )
            if previous is not None and previous.observed_at > snapshot.observed_at:
                # A slower external read must not roll the account back.
                continue
            if previous is not None and previous.observation_key == observation_key:
                continue
            provider_id = provider_ids[0]
            state_values = {
                "provider_id": provider_id,
                "provider_ids": provider_ids,
                "observation_key": observation_key,
                "total_credits_usd": snapshot.total_credits_usd,
                "total_usage_usd": snapshot.total_usage_usd,
                "remaining_usd": snapshot.remaining_usd,
                "observed_at": snapshot.observed_at,
                "threshold_state": "at_or_below" if low else "above",
            }
            if previous is None:
                transaction.add(
                    ButlerAccountCredit(
                        id=state_id,
                        engagement_id=engagement_id,
                        account_key=key,
                        **state_values,
                    )
                )
            else:
                transaction.update(
                    ButlerAccountCredit,
                    state_id,
                    state_values,
                    expected_revision=previous.revision,
                )
            state_ref = ButlerEvidenceRef(
                kind="resource", id=state_id, project_id=engagement_id
            )
            _append_project_event(
                transaction,
                engagement_id=engagement_id,
                event_type="resource.openrouter_balance_observed",
                payload=ButlerEventPayload(
                    project_id=engagement_id,
                    source="openrouter_credits_api",
                    actor_role="core",
                    summary="OpenRouter account credits observed.",
                    metrics={
                        "provider_id": provider_id,
                        "total_credits_usd": snapshot.total_credits_usd,
                        "total_usage_usd": snapshot.total_usage_usd,
                        "remaining_usd": snapshot.remaining_usd,
                        "observed_at": snapshot.observed_at.isoformat(),
                    },
                    verification_state="provider_observed",
                    evidence_refs=[state_ref],
                ),
                idempotency_key=f"openrouter-credit:{key}:{observation_key}",
                now=utc_now(),
            )
            record_id = incident_id(engagement_id, f"openrouter-low-credit:{key}")
            incident = _entity_in_transaction(transaction, ButlerIncident, record_id)
            impact = f"OpenRouter reports ${snapshot.remaining_usd:.2f} account credits remaining."
            if low:
                if incident is None:
                    incident = ButlerIncident(
                        id=record_id,
                        engagement_id=engagement_id,
                        incident_key=f"openrouter-low-credit:{key}",
                        source="openrouter_credits_api",
                        verification_state="provider_observed",
                        summary="OpenRouter account credits are at or below $20.",
                        impact=impact,
                        next_action="Add OpenRouter credits or select another provider.",
                        evidence_refs=[state_ref.model_dump()],
                        goal_ids=goal_ids,
                        session_ids=session_ids,
                        observed_at=snapshot.observed_at,
                    )
                    transaction.add(incident)
                    event_type = "incident.opened"
                else:
                    reopened = incident.status == "resolved"
                    incident = transaction.update(
                        ButlerIncident,
                        record_id,
                        {
                            "status": "open" if reopened else incident.status,
                            "impact": impact,
                            "evidence_refs": [state_ref.model_dump()],
                            "goal_ids": list(
                                dict.fromkeys([*incident.goal_ids, *goal_ids])
                            )[-100:],
                            "session_ids": list(
                                dict.fromkeys([*incident.session_ids, *session_ids])
                            )[-100:],
                            "observed_at": snapshot.observed_at
                            if reopened
                            else incident.observed_at,
                            "acknowledged_at": None
                            if reopened
                            else incident.acknowledged_at,
                            "resolved_at": None,
                        },
                        expected_revision=incident.revision,
                    )
                    event_type = "incident.reopened" if reopened else "incident.updated"
            elif incident is not None and incident.status != "resolved":
                incident = transaction.update(
                    ButlerIncident,
                    record_id,
                    {
                        "status": "resolved",
                        "resolved_at": snapshot.observed_at,
                        "impact": impact,
                    },
                    expected_revision=incident.revision,
                )
                event_type = "incident.resolved"
            else:
                event_type = None
            if event_type is not None and incident is not None:
                _append_project_event(
                    transaction,
                    engagement_id=engagement_id,
                    event_type=event_type,
                    payload=ButlerEventPayload(
                        project_id=engagement_id,
                        source="openrouter_credits_api",
                        actor_role="core",
                        summary=incident.summary
                        if low
                        else "OpenRouter account credits rose above $20.",
                        metrics={"remaining_usd": snapshot.remaining_usd},
                        verification_state="provider_observed",
                        evidence_refs=[
                            ButlerEvidenceRef(
                                kind="incident",
                                id=incident.id,
                                project_id=engagement_id,
                            ),
                            state_ref,
                        ],
                        incident_key=incident.incident_key,
                    ),
                    idempotency_key=f"openrouter-credit-incident:{incident.id}:{observation_key}",
                    now=utc_now(),
                )
        saved += 1
    return saved


class OpenRouterCreditObserver:
    """Coalesce checks per management ref and persist successful observations."""

    def __init__(
        self,
        store: NebulaStore,
        credentials: CredentialStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.credentials = credentials
        self.transport = transport
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inflight: dict[str, asyncio.Task[OpenRouterCreditSnapshot]] = {}
        self._recent: dict[str, OpenRouterCreditSnapshot] = {}
        self._scheduled: set[asyncio.Task[object]] = set()

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

    def schedule(self, provider_id: str) -> None:
        """Request a bounded background check after setup or model use."""

        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._schedule_on_loop, provider_id)

    def _schedule_on_loop(self, provider_id: str) -> None:
        task = create_diagnostic_task(
            self.refresh(provider_id),
            feature="butler",
            event_code="butler.openrouter_credit_check_failed",
            failure_message="OpenRouter credits could not be checked; a later use or manual refresh can retry.",
            name=f"nebula-openrouter-credits-{provider_id[:24]}",
        )
        self._scheduled.add(task)
        task.add_done_callback(self._scheduled.discard)

    async def refresh(
        self, provider_id: str, *, force: bool = False
    ) -> OpenRouterCreditSnapshot:
        profile = self.store.get(ProviderProfile, provider_id)
        if profile.provider_type != "openrouter":
            raise ValueError("account credits require an OpenRouter profile")
        reference = profile.openrouter_management_secret_ref
        if reference is None or not profile.enabled:
            return await read_openrouter_credits(
                profile, self.credentials, transport=self.transport
            )
        key = account_key(reference)
        recent = self._recent.get(key)
        if (
            not force
            and recent is not None
            and utc_now() - recent.checked_at < AUTO_CHECK_INTERVAL
        ):
            if recent.status == "observed":
                await asyncio.to_thread(
                    record_openrouter_credit,
                    self.store,
                    snapshot=recent,
                    management_ref=reference,
                )
            return recent.model_copy(update={"provider_id": provider_id})
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._read_and_record(profile, reference))
            self._inflight[key] = task
            task.add_done_callback(
                lambda completed: (
                    self._inflight.pop(key, None)
                    if self._inflight.get(key) is completed
                    else None
                )
            )
        snapshot = await asyncio.shield(task)
        return snapshot.model_copy(update={"provider_id": provider_id})

    async def _read_and_record(
        self, profile: ProviderProfile, reference: str
    ) -> OpenRouterCreditSnapshot:
        snapshot = await read_openrouter_credits(
            profile, self.credentials, transport=self.transport
        )
        if snapshot.status == "observed":
            await asyncio.to_thread(
                record_openrouter_credit,
                self.store,
                snapshot=snapshot,
                management_ref=reference,
            )
        self._recent[account_key(reference)] = snapshot
        return snapshot
