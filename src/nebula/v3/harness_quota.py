"""Authoritative, bounded harness quota observations for Butler oversight.

Token counts, context-window use, and a rate-limit status are not a quota
percentage. Only the named vendor fields below establish a numeric snapshot.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .domain import HarnessKind


class HarnessQuotaSnapshot(BaseModel):
    """One vendor-reported quota window; never inferred from turn usage."""

    harness_kind: HarnessKind
    source: Literal[
        "codex.account/rateLimits/read", "claude.RateLimitEvent", "grok._x.ai/billing"
    ]
    window_id: str = Field(min_length=1, max_length=160)
    window_label: str = Field(min_length=1, max_length=160)
    used_percent: float = Field(ge=0, le=100)
    remaining_percent: float = Field(ge=0, le=100)
    observed_at: datetime
    resets_at: datetime | None = None
    window_duration_minutes: int | None = Field(default=None, gt=0)
    basis: Literal["vendor_used_percent", "vendor_utilization_fraction"]


def _percent(value: Any, *, fraction: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    ceiling = 1 if fraction else 100
    if not math.isfinite(number) or not 0 <= number <= ceiling:
        return None
    return number * 100 if fraction else number


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 < value < 253402300800:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _observed_at(value: datetime | None) -> datetime:
    return value or datetime.now(timezone.utc)


def codex_quota_snapshots(
    payload: Any, *, observed_at: datetime | None = None
) -> list[HarnessQuotaSnapshot]:
    """Read numeric windows from Codex app-server's account limits response."""

    if not isinstance(payload, dict):
        return []
    groups = payload.get("rateLimitsByLimitId")
    candidates: list[tuple[str, dict[str, Any]]] = []
    if isinstance(groups, dict):
        candidates = [
            (key, value)
            for key, value in list(groups.items())[:8]
            if isinstance(key, str) and isinstance(value, dict)
        ]
    if not candidates and isinstance(payload.get("rateLimits"), dict):
        candidates = [("included_usage", payload["rateLimits"])]

    def extract(items: list[tuple[str, dict[str, Any]]]) -> list[HarnessQuotaSnapshot]:
        snapshots: list[HarnessQuotaSnapshot] = []
        for fallback_id, group in items:
            identifier = group.get("limitId") or fallback_id
            identifier = identifier if isinstance(identifier, str) else fallback_id
            name = group.get("limitName")
            name = name if isinstance(name, str) and name.strip() else "Included usage"
            for key, label in (
                ("primary", "Short window"),
                ("secondary", "Long window"),
            ):
                window = group.get(key)
                if not isinstance(window, dict):
                    continue
                used = _percent(window.get("usedPercent"))
                if used is None:
                    continue
                duration = window.get("windowDurationMins")
                snapshots.append(
                    HarnessQuotaSnapshot(
                        harness_kind=HarnessKind.CODEX_APP_SERVER,
                        source="codex.account/rateLimits/read",
                        window_id=f"{identifier[:130]}:{key}",
                        window_label=f"{name[:120]} · {label}",
                        used_percent=used,
                        remaining_percent=100 - used,
                        observed_at=_observed_at(observed_at),
                        resets_at=_timestamp(window.get("resetsAt")),
                        window_duration_minutes=(
                            duration
                            if isinstance(duration, int)
                            and not isinstance(duration, bool)
                            and duration > 0
                            else None
                        ),
                        basis="vendor_used_percent",
                    )
                )
        return snapshots

    snapshots = extract(candidates)
    if not snapshots and candidates and isinstance(payload.get("rateLimits"), dict):
        snapshots = extract([("included_usage", payload["rateLimits"])])
    return snapshots


def grok_quota_snapshots(
    payload: Any, *, observed_at: datetime | None = None
) -> list[HarnessQuotaSnapshot]:
    """Read Grok's reported included-credit window, never session token use.

    A shared subscription pool can include other Grok products. Missing or
    malformed percentages remain unknown; no spend-derived estimate is used.
    """

    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        return []
    used = _percent(config.get("creditUsagePercent"))
    period = config.get("currentPeriod")
    if used is None or not isinstance(period, dict):
        return []
    kind = period.get("type")
    if not isinstance(kind, str):
        return []
    label = {
        "USAGE_PERIOD_TYPE_WEEKLY": "Weekly",
        "USAGE_PERIOD_TYPE_MONTHLY": "Monthly",
    }.get(kind)
    if label is None:
        return []
    resets_at = None
    end = period.get("end")
    if isinstance(end, str):
        try:
            candidate = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if candidate.utcoffset() is not None:
                resets_at = candidate.astimezone(timezone.utc)
        except ValueError:
            pass  # diagnostic-expected: the percentage remains usable without a reset time.
    return [
        HarnessQuotaSnapshot(
            harness_kind=HarnessKind.GROK_ACP,
            source="grok._x.ai/billing",
            window_id=f"included_credits:{label.lower()}",
            window_label=(
                f"Shared Grok credits · {label}"
                if config.get("isUnifiedBillingUser") is True
                else f"Grok included credits · {label}"
            ),
            used_percent=used,
            remaining_percent=100 - used,
            observed_at=_observed_at(observed_at),
            resets_at=resets_at,
            basis="vendor_used_percent",
        )
    ]


def claude_quota_snapshot(
    info: Any, *, observed_at: datetime | None = None
) -> HarnessQuotaSnapshot | None:
    """Read SDK RateLimitInfo.utilization, a documented consumed fraction.

    A `rejected` status without utilization is an exhaustion notice, not a
    numeric observation. Overage status is a separate allowance and does not
    establish the main window's percentage.
    """

    values = (
        info
        if isinstance(info, dict)
        else vars(info)
        if hasattr(info, "__dict__")
        else {}
    )
    used = _percent(values.get("utilization"), fraction=True)
    if used is None:
        return None
    window = values.get("rate_limit_type") or values.get("rateLimitType")
    window = window if isinstance(window, str) and window.strip() else "unspecified"
    resets_at = values.get("resets_at", values.get("resetsAt"))
    return HarnessQuotaSnapshot(
        harness_kind=HarnessKind.CLAUDE_AGENT_SDK,
        source="claude.RateLimitEvent",
        window_id=window[:160],
        window_label=window.replace("_", " ").title()[:160],
        used_percent=used,
        remaining_percent=100 - used,
        observed_at=_observed_at(observed_at),
        resets_at=_timestamp(resets_at),
        basis="vendor_utilization_fraction",
    )
