"""Plan windows a harness reported during turns, kept for later usage reads.

Claude has no on-demand account usage read: the Agent SDK reports plan
utilization only in a ``RateLimitEvent`` inside a turn, and only when the
limit status changes. Core keeps the newest report per project, profile and
window so ``/usage`` and Usage and credits can show it with its age.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .database import EntityRow
from .domain import HarnessKind, HarnessQuotaObservation
from .harness_quota import HarnessQuotaSnapshot
from .storage import ConflictError, NebulaStore
from .stored_entities import decode_stored

# Sources whose numbers arrive only with turns; account reads cover the rest.
TURN_REPORTED_SOURCES = frozenset({"claude.RateLimitEvent"})


def observation_id(engagement_id: str, profile_id: str, window_id: str) -> str:
    digest = hashlib.sha256(
        ":".join((engagement_id, profile_id, window_id)).encode("utf-8")
    ).hexdigest()
    return f"harness-quota-observation:{digest}"


async def record_quota_observation(
    store: NebulaStore,
    *,
    engagement_id: str,
    profile_id: str,
    snapshot: HarnessQuotaSnapshot,
) -> bool:
    """Keep the newest turn-reported window; return whether it was saved."""

    if snapshot.source not in TURN_REPORTED_SOURCES:
        return False
    entity_id = observation_id(engagement_id, profile_id, snapshot.window_id)
    values: dict[str, Any] = {
        "window_label": snapshot.window_label,
        "source": snapshot.source,
        "used_percent": snapshot.used_percent,
        "remaining_percent": snapshot.remaining_percent,
        "observed_at": snapshot.observed_at,
        "resets_at": snapshot.resets_at,
    }
    async with store.transaction(immediate=True) as transaction:
        row = await transaction.session.get(EntityRow, entity_id)
        if row is not None and row.kind != HarnessQuotaObservation.entity_kind:
            raise ConflictError(f"entity {entity_id} has an unexpected kind")
        previous = (
            None if row is None else decode_stored(HarnessQuotaObservation, row.payload)
        )
        if previous is None:
            await transaction.add(
                HarnessQuotaObservation(
                    id=entity_id,
                    engagement_id=engagement_id,
                    harness_profile_id=profile_id,
                    window_id=snapshot.window_id,
                    **values,
                )
            )
            return True
        if previous.observed_at >= snapshot.observed_at:
            # A late notice must not replace a newer report.
            return False
        await transaction.update(
            HarnessQuotaObservation,
            entity_id,
            values,
            expected_revision=previous.revision,
        )
    return True


@dataclass(frozen=True)
class ReportedQuota:
    """Current windows, newest first, and how many have reset since."""

    snapshots: list[HarnessQuotaSnapshot]
    reset_windows: int

    @property
    def observed_at(self) -> datetime | None:
        return self.snapshots[0].observed_at if self.snapshots else None


async def reported_quota(
    store: NebulaStore,
    profile_id: str,
    *,
    engagement_id: str | None = None,
    now: datetime | None = None,
) -> ReportedQuota:
    """Newest report per window; a window past its reset is no longer current."""

    latest: dict[str, HarnessQuotaObservation] = {}
    async for row in store.iter_readable_entities(
        HarnessQuotaObservation,
        {"harness_profile_id": profile_id},
        engagement_id=engagement_id,
    ):
        current = latest.get(row.window_id)
        if current is None or row.observed_at > current.observed_at:
            latest[row.window_id] = row
    moment = now or datetime.now(timezone.utc)
    snapshots: list[HarnessQuotaSnapshot] = []
    reset_windows = 0
    for row in latest.values():
        if row.resets_at is not None and row.resets_at <= moment:
            reset_windows += 1
            continue
        snapshots.append(
            HarnessQuotaSnapshot(
                harness_kind=HarnessKind.CLAUDE_AGENT_SDK,
                source="claude.RateLimitEvent",
                window_id=row.window_id,
                window_label=row.window_label,
                used_percent=row.used_percent,
                remaining_percent=row.remaining_percent,
                observed_at=row.observed_at,
                resets_at=row.resets_at,
                basis="vendor_utilization_fraction",
            )
        )
    snapshots.sort(key=lambda item: item.observed_at, reverse=True)
    return ReportedQuota(snapshots=snapshots, reset_windows=reset_windows)
