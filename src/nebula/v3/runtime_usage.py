"""Read-only usage view with explicit local and vendor account scopes."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from .credentials import CredentialStore
from .domain import (
    AgentAttempt,
    AgentRun,
    ChatTurn,
    ContextOwnerType,
    ContextSnapshot,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    ProviderProfile,
    RunBackend,
)
from .harnesses import CodexAppServerAdapter
from .storage import NebulaStore


class RecordedUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    turns: int = 0
    scope: str


class UsageBalance(BaseModel):
    label: str
    currency: str | None = None
    remaining: float | None = None
    used: float | None = None
    limit: float | None = None
    display_value: str | None = None
    note: str | None = None


class UsageWindow(BaseModel):
    label: str
    used_percent: int = Field(ge=0)
    duration_minutes: int | None = None
    resets_at: datetime | None = None


class VendorUsage(BaseModel):
    state: Literal["available", "unavailable", "error"]
    source: str
    detail: str
    checked_at: datetime | None = None
    balances: list[UsageBalance] = Field(default_factory=list)
    windows: list[UsageWindow] = Field(default_factory=list)
    ordinary_usage_allowed: bool | None = None


class RuntimeUsageItem(BaseModel):
    kind: Literal["provider", "harness"]
    profile_id: str
    name: str
    provider_type: str
    enabled: bool
    recorded: RecordedUsage
    vendor: VendorUsage


class RuntimeUsageResponse(BaseModel):
    generated_at: datetime
    items: list[RuntimeUsageItem]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(Decimal(str(value)))
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _recorded(store: NebulaStore, providers: set[str], harnesses: set[str]) -> dict[tuple[str, str], RecordedUsage]:
    rows: dict[tuple[str, str], RecordedUsage] = {}

    def add(kind: str, profile_id: str | None, usage: Any, *, total_only: int = 0) -> None:
        if profile_id is None or profile_id not in (providers if kind == "provider" else harnesses):
            return
        key = (kind, profile_id)
        if key not in rows:
            rows[key] = RecordedUsage(
                scope=(
                    "Nebula chat turns, native mission attempts and context compactions"
                    if kind == "provider" else "Nebula harness turns"
                )
            )
        row = rows[key]
        row.input_tokens += getattr(usage, "input_tokens", 0)
        row.output_tokens += getattr(usage, "output_tokens", 0)
        row.total_tokens += getattr(usage, "total_tokens", 0) + total_only
        row.cached_input_tokens += getattr(usage, "cached_input_tokens", 0)
        row.turns += 1

    for turn in store.iter_readable_entities(ChatTurn):
        if turn.backend.value == "provider" and turn.usage.total_tokens:
            add("provider", turn.provider_profile_id, turn.usage)
    native_runs = {
        run.id: run.supervisor_provider_id
        for run in store.iter_readable_entities(AgentRun)
        if run.backend == RunBackend.NATIVE and run.supervisor_provider_id in providers
    }
    counted_runs: set[str] = set()
    for attempt in store.iter_readable_entities(AgentAttempt):
        if attempt.tokens_used:
            profile_id = attempt.provider_profile_id or native_runs.get(attempt.run_id)
            if profile_id in providers:
                add("provider", profile_id, None, total_only=attempt.tokens_used)
                counted_runs.add(attempt.run_id)
    for snapshot in store.iter_readable_entities(ContextSnapshot):
        if snapshot.usage.total_tokens:
            # Native attempt totals already include their run's compaction use.
            if snapshot.owner_type == ContextOwnerType.AGENT_RUN and snapshot.owner_id in counted_runs:
                continue
            add("provider", snapshot.provider_profile_id, snapshot.usage)
    sessions = {
        session.id: session.harness_profile_id
        for session in store.iter_readable_entities(HarnessSession)
        if session.harness_profile_id in harnesses
    }
    for turn in store.iter_readable_entities(HarnessTurn):
        if turn.usage.total_tokens:
            add("harness", sessions.get(turn.harness_session_id), turn.usage)
    return rows


def _unsupported(profile: ProviderProfile | HarnessProfile) -> VendorUsage:
    if isinstance(profile, HarnessProfile):
        detail = "This harness does not expose an account balance to Nebula."
    elif profile.is_local:
        detail = "Local runtimes do not have a vendor credit balance."
    else:
        detail = "This provider connection does not expose a readable balance."
    return VendorUsage(state="unavailable", source="Vendor account", detail=detail)


def _codex_window(label: str, value: Any) -> UsageWindow | None:
    if not isinstance(value, dict):
        return None
    percent = value.get("usedPercent")
    if isinstance(percent, bool) or not isinstance(percent, int) or percent < 0:
        return None
    duration = value.get("windowDurationMins")
    reset = value.get("resetsAt")
    return UsageWindow(
        label=label[:100],
        used_percent=percent,
        duration_minutes=duration if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0 else None,
        resets_at=(
            datetime.fromtimestamp(reset, tz=timezone.utc)
            if isinstance(reset, int) and not isinstance(reset, bool) and 0 < reset < 253402300800
            else None
        ),
    )


def parse_codex_usage(payload: Any) -> VendorUsage:
    """Normalize the installed app-server account/rateLimits/read response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rateLimits"), dict):
        raise ValueError("Codex omitted account rate limits")
    snapshots = payload.get("rateLimitsByLimitId")
    if isinstance(snapshots, dict) and snapshots:
        selected = [value for value in list(snapshots.values())[:8] if isinstance(value, dict)]
    else:
        selected = [payload["rateLimits"]]
    windows: list[UsageWindow] = []
    for snapshot in selected:
        name = snapshot.get("limitName") or snapshot.get("limitId") or "Included usage"
        if not isinstance(name, str):
            name = "Included usage"
        for key, fallback in (("primary", "Short window"), ("secondary", "Long window")):
            window = _codex_window(f"{name[:60]} · {fallback}", snapshot.get(key))
            if window:
                windows.append(window)
    primary = payload["rateLimits"]
    credits = primary.get("credits")
    balances: list[UsageBalance] = []
    if isinstance(credits, dict):
        balance = credits.get("balance")
        if credits.get("unlimited") is True:
            balances.append(UsageBalance(label="Codex credits", display_value="Unlimited"))
        elif (amount := _number(balance)) is not None:
            balances.append(UsageBalance(label="Codex credits", remaining=amount, note="Vendor credit units"))
        elif credits.get("hasCredits") is False:
            balances.append(UsageBalance(label="Codex credits", remaining=0, note="Vendor reports no credits"))
    allowed = payload.get("ordinaryUsageAllowed")
    return VendorUsage(
        state="available",
        source="Codex account",
        detail="Account rate-limit windows and credits reported by Codex. Resets do not guarantee access.",
        checked_at=datetime.now(timezone.utc),
        balances=balances,
        windows=windows,
        ordinary_usage_allowed=allowed if isinstance(allowed, bool) else None,
    )


def parse_openrouter_usage(payload: Any) -> VendorUsage:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("OpenRouter omitted key usage")
    used = _number(data.get("usage"))
    remaining = _number(data.get("limit_remaining"))
    limit = _number(data.get("limit"))
    if used is None and remaining is None and limit is None:
        raise ValueError("OpenRouter omitted key allowance")
    reset = data.get("limit_reset")
    note = f"Key cap resets {reset}" if reset in {"daily", "weekly", "monthly"} else "Key cap only; account credits may differ"
    return VendorUsage(
        state="available",
        source="OpenRouter key",
        detail="This is the API key allowance, not the account's total credits.",
        checked_at=datetime.now(timezone.utc),
        balances=[UsageBalance(label="Key allowance", currency="USD", remaining=remaining, used=used, limit=limit, note=note)],
    )


def parse_deepseek_usage(payload: Any) -> VendorUsage:
    infos = payload.get("balance_infos") if isinstance(payload, dict) else None
    if not isinstance(infos, list):
        raise ValueError("DeepSeek omitted account balances")
    balances = []
    for info in infos[:4]:
        if not isinstance(info, dict) or info.get("currency") not in {"USD", "CNY"}:
            continue
        amount = _number(info.get("total_balance"))
        if amount is not None:
            balances.append(UsageBalance(label="Account balance", currency=info["currency"], remaining=amount))
    if not balances:
        raise ValueError("DeepSeek returned no readable balances")
    return VendorUsage(
        state="available",
        source="DeepSeek account",
        detail="Available account balance, including granted and topped-up funds.",
        checked_at=datetime.now(timezone.utc),
        balances=balances,
        ordinary_usage_allowed=payload.get("is_available") if isinstance(payload.get("is_available"), bool) else None,
    )


class RuntimeUsageService:
    def __init__(self, store: NebulaStore, credentials: CredentialStore, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.store = store
        self.credentials = credentials
        self.transport = transport
        self._cache: dict[tuple[str, str, int], tuple[float, VendorUsage]] = {}

    async def _vendor(self, profile: ProviderProfile | HarnessProfile, *, refresh: bool) -> VendorUsage:
        kind = "harness" if isinstance(profile, HarnessProfile) else "provider"
        key = (kind, profile.id, profile.revision)
        cached = self._cache.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < 120:
            return cached[1]
        if isinstance(profile, HarnessProfile) and profile.kind.value != "codex_app_server":
            return _unsupported(profile)
        if isinstance(profile, ProviderProfile) and profile.provider_type not in {"openrouter", "deepseek"}:
            return _unsupported(profile)
        if isinstance(profile, ProviderProfile):
            expected_host = "openrouter.ai" if profile.provider_type == "openrouter" else "api.deepseek.com"
            try:
                endpoint = urlsplit(profile.endpoint or "")
            except ValueError:
                return VendorUsage(state="unavailable", source="Vendor account", detail="Balance reads require the provider's official HTTPS endpoint.")
            if endpoint.scheme != "https" or endpoint.hostname != expected_host:
                return VendorUsage(state="unavailable", source="Vendor account", detail="Balance reads require the provider's official HTTPS endpoint.")
            if not profile.secret_ref:
                return VendorUsage(state="unavailable", source="Vendor account", detail="Add a provider credential to read its balance.")
        try:
            if isinstance(profile, HarnessProfile):
                payload = await CodexAppServerAdapter().read_account_usage(profile, self.credentials)
                value = parse_codex_usage(payload)
            else:
                secret = await asyncio.to_thread(self.credentials.resolve, profile.secret_ref)
                url = (
                    "https://openrouter.ai/api/v1/key"
                    if profile.provider_type == "openrouter"
                    else "https://api.deepseek.com/user/balance"
                )
                async with httpx.AsyncClient(timeout=6.0, transport=self.transport, follow_redirects=False) as client:
                    response = await client.get(url, headers={"Authorization": f"Bearer {secret.get_secret_value()}"})
                response.raise_for_status()
                payload = response.json()
                value = parse_openrouter_usage(payload) if profile.provider_type == "openrouter" else parse_deepseek_usage(payload)
        except Exception:
            # Credential, transport and vendor errors can contain secret-bearing
            # URLs or response bodies. The operator needs a safe retry message.
            value = VendorUsage(
                state="error",
                source="Vendor account",
                detail="Balance could not be read. Check this profile's sign-in and retry.",
                checked_at=datetime.now(timezone.utc),
            )
        self._cache[key] = (time.monotonic(), value)
        return value

    async def read(self, *, refresh: bool = False) -> RuntimeUsageResponse:
        providers = list(self.store.iter_readable_entities(ProviderProfile))
        harnesses = list(self.store.iter_readable_entities(HarnessProfile))
        recorded = await asyncio.to_thread(
            _recorded, self.store, {item.id for item in providers}, {item.id for item in harnesses}
        )
        semaphore = asyncio.Semaphore(3)

        async def item(profile: ProviderProfile | HarnessProfile) -> RuntimeUsageItem:
            kind: Literal["provider", "harness"] = "harness" if isinstance(profile, HarnessProfile) else "provider"
            async with semaphore:
                vendor = await self._vendor(profile, refresh=refresh)
            return RuntimeUsageItem(
                kind=kind,
                profile_id=profile.id,
                name=profile.name,
                provider_type=profile.kind.value if isinstance(profile, HarnessProfile) else profile.provider_type,
                enabled=profile.enabled,
                recorded=recorded.get((kind, profile.id), RecordedUsage(
                    scope="Nebula harness turns" if kind == "harness" else "Nebula chat turns, native mission attempts and context compactions"
                )),
                vendor=vendor,
            )

        profiles: list[ProviderProfile | HarnessProfile] = [*providers, *harnesses]
        items = await asyncio.gather(*(item(profile) for profile in profiles))
        return RuntimeUsageResponse(generated_at=datetime.now(timezone.utc), items=items)
