"""Only vendor-reported numeric windows can trigger Butler quota thresholds."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo, ResultMessage

from nebula.v3.domain import HarnessKind, HarnessProfile
from nebula.v3.harness_quota import claude_quota_snapshot, codex_quota_snapshots
from nebula.v3.harnesses import ClaudeAgentSdkConnection, CodexAppServerAdapter
from nebula.v3.runtime_usage import RuntimeUsageService, parse_codex_usage
from nebula.v3.storage import NebulaStore


OBSERVED = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def test_codex_numeric_account_windows_keep_source_reset_and_window_identity():
    payload = {
        "rateLimits": {"primary": {"usedPercent": 99}},
        "rateLimitsByLimitId": {
            "weekly": {
                "limitName": "Included usage",
                "primary": {
                    "usedPercent": 80,
                    "windowDurationMins": 300,
                    "resetsAt": 1_800_000_000,
                },
                "secondary": {"usedPercent": 12.5},
            }
        },
    }
    snapshots = codex_quota_snapshots(payload, observed_at=OBSERVED)
    assert [(item.window_id, item.remaining_percent) for item in snapshots] == [
        ("weekly:primary", 20),
        ("weekly:secondary", 87.5),
    ]
    assert snapshots[0].source == "codex.account/rateLimits/read"
    assert snapshots[0].basis == "vendor_used_percent"
    assert snapshots[0].observed_at == OBSERVED
    assert snapshots[0].resets_at == datetime.fromtimestamp(
        1_800_000_000, tz=timezone.utc
    )
    assert snapshots[0].window_duration_minutes == 300
    assert parse_codex_usage(payload).quota_snapshots[0].remaining_percent == 20


def test_codex_usage_or_invalid_percent_never_becomes_quota():
    for value in (None, True, -1, 101, float("nan"), "80"):
        payload = {
            "tokenUsage": {"total": 1_000_000},
            "rateLimits": {"primary": {"usedPercent": value}},
        }
        assert codex_quota_snapshots(payload) == []


def test_claude_numeric_utilization_is_consumed_fraction_not_remaining():
    info = RateLimitInfo(
        status="allowed_warning",
        rate_limit_type="five_hour",
        utilization=0.81,
        resets_at=1_800_000_000,
    )
    snapshot = claude_quota_snapshot(info, observed_at=OBSERVED)
    assert snapshot is not None
    assert snapshot.harness_kind == HarnessKind.CLAUDE_AGENT_SDK
    assert snapshot.window_id == "five_hour"
    assert snapshot.remaining_percent == 19
    assert snapshot.basis == "vendor_utilization_fraction"
    assert snapshot.resets_at == datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)


def test_claude_rejected_status_without_numeric_utilization_is_not_zero_percent():
    assert (
        claude_quota_snapshot(RateLimitInfo(status="rejected", resets_at=1_800_000_000))
        is None
    )
    assert claude_quota_snapshot({"status": "rejected", "utilization": True}) is None
    assert claude_quota_snapshot({"utilization": float("inf")}) is None
    assert claude_quota_snapshot({"utilization": 1.01}) is None
    assert claude_quota_snapshot({"overage_status": "rejected"}) is None


def test_profile_read_returns_numeric_codex_and_explicit_unknown_grok(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "quota.db")
    store.create(
        HarnessProfile(
            id="codex",
            name="Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
        )
    )
    store.create(
        HarnessProfile(
            id="grok",
            name="Grok",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
        )
    )
    calls = []
    response = {"rateLimits": {"primary": {"usedPercent": 80}}}

    async def account_read(self, profile, credentials):
        calls.append(profile.id)
        return response

    monkeypatch.setattr(CodexAppServerAdapter, "read_account_usage", account_read)
    service = RuntimeUsageService(store, object())

    async def scenario():
        codex = await service.read_harness_quota("codex", refresh=True)
        unknown = await service.read_harness_quota("grok", refresh=True)
        return codex, unknown

    codex, unknown = asyncio.run(scenario())
    assert calls == ["codex"]
    assert codex.state == "available"
    assert codex.snapshots[0].remaining_percent == 20
    assert unknown.state == "unavailable"
    assert unknown.snapshots == []
    response["rateLimits"]["primary"].pop("usedPercent")
    without_numeric = asyncio.run(service.read_harness_quota("codex", refresh=True))
    assert without_numeric.state == "unavailable"
    assert without_numeric.snapshots == []
    assert calls == ["codex", "codex"]


def test_claude_adapter_attaches_only_numeric_quota_to_rate_limit_notice(tmp_path):
    class Client:
        def __init__(self):
            self.messages: asyncio.Queue[object] = asyncio.Queue()

        async def query(self, prompt):
            self.messages.put_nowait(
                RateLimitEvent(
                    rate_limit_info=RateLimitInfo(
                        status="allowed_warning",
                        rate_limit_type="seven_day",
                        utilization=0.85,
                    ),
                    uuid="quota-1",
                    session_id="claude-session",
                )
            )
            self.messages.put_nowait(
                RateLimitEvent(
                    rate_limit_info=RateLimitInfo(status="rejected"),
                    uuid="quota-2",
                    session_id="claude-session",
                )
            )
            self.messages.put_nowait(
                ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id="claude-session",
                    result="done",
                )
            )

        async def receive_messages(self):
            while True:
                yield await self.messages.get()

        async def disconnect(self):
            return None

    async def permission(_request):
        raise AssertionError("unexpected permission")

    async def scenario():
        connection = ClaudeAgentSdkConnection(
            Client(),
            permission_handler=permission,
            sdk=SimpleNamespace(),
            external_session_id=None,
            workspace=tmp_path,
        )
        try:
            return [
                event async for event in connection.run_turn("status", model="test")
            ]
        finally:
            await connection.close()

    events = asyncio.run(scenario())
    notices = [event for event in events if event.title == "Claude rate limit"]
    assert len(notices) == 2
    assert notices[0].quota_snapshot is not None
    assert notices[0].quota_snapshot.remaining_percent == 15
    assert notices[1].quota_snapshot is None
    assert notices[1].summary == "rejected"
    assert notices[1].reason_code == "rate_limited"
