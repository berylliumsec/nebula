"""Claude /usage and Usage and credits read Claude's last in-turn plan report."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import RateLimitInfo
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import (
    ChatTokenUsage,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
)
from nebula.v3.harness_commands import claude_usage_reply
from nebula.v3.harness_quota import HarnessQuotaSnapshot, claude_quota_snapshot
from nebula.v3.harness_quota_observations import (
    record_quota_observation,
    reported_quota,
)
from nebula.v3.harnesses import (
    HarnessConfigurationError,
    HarnessRuntimeService,
    _profile_commands,
)
from nebula.v3.storage import NebulaStore

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _snapshot(
    window: str = "five_hour",
    *,
    used: float = 0.42,
    observed: datetime = NOW,
    resets: datetime | None = None,
) -> HarnessQuotaSnapshot:
    snapshot = claude_quota_snapshot(
        RateLimitInfo(
            status="allowed_warning",
            rate_limit_type=window,
            utilization=used,
            resets_at=int((resets or NOW + timedelta(hours=3)).timestamp()),
        ),
        observed_at=observed,
    )
    assert snapshot is not None
    return snapshot


def test_claude_windows_use_claude_code_names():
    assert _snapshot("five_hour").window_label == "5-hour limit"
    assert _snapshot("seven_day").window_label == "Weekly limit"
    assert _snapshot("seven_day_opus").window_label == "Weekly Opus limit"


async def test_newest_report_per_window_wins_and_late_notices_are_ignored(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    older = _snapshot(used=0.30, observed=NOW - timedelta(minutes=10))
    newer = _snapshot(used=0.55, observed=NOW)
    assert await record_quota_observation(
        store, engagement_id="one", profile_id="claude", snapshot=older
    )
    assert await record_quota_observation(
        store, engagement_id="one", profile_id="claude", snapshot=newer
    )
    # A notice delivered after a newer one must not roll the figure back.
    assert not await record_quota_observation(
        store, engagement_id="one", profile_id="claude", snapshot=older
    )
    # Account-read sources are read on demand and never stored here.
    codex = newer.model_copy(update={"source": "codex.account/rateLimits/read"})
    assert not await record_quota_observation(
        store, engagement_id="one", profile_id="codex", snapshot=codex
    )
    quota = await reported_quota(store, "claude")
    assert [round(item.used_percent) for item in quota.snapshots] == [55]
    assert (await reported_quota(store, "codex")).snapshots == []


async def test_account_view_takes_the_newest_project_and_drops_reset_windows(
    tmp_path,
):
    store = NebulaStore(tmp_path / "nebula.db")
    await record_quota_observation(
        store,
        engagement_id="one",
        profile_id="claude",
        snapshot=_snapshot(used=0.20, observed=NOW - timedelta(hours=1)),
    )
    await record_quota_observation(
        store,
        engagement_id="two",
        profile_id="claude",
        snapshot=_snapshot(used=0.70, observed=NOW),
    )
    await record_quota_observation(
        store,
        engagement_id="one",
        profile_id="claude",
        snapshot=_snapshot(
            "seven_day",
            used=0.90,
            observed=NOW - timedelta(days=8),
            resets=NOW - timedelta(days=1),
        ),
    )
    account = await reported_quota(store, "claude")
    assert [(s.window_id, round(s.used_percent)) for s in account.snapshots] == [
        ("five_hour", 70)
    ]
    assert account.reset_windows == 1
    project = await reported_quota(store, "claude", engagement_id="one")
    assert [round(s.used_percent) for s in project.snapshots] == [20]


def test_usage_reply_states_source_age_and_missing_reports():
    snapshot = _snapshot(used=0.42)
    reply = claude_usage_reply(
        input_tokens=1_200,
        output_tokens=300,
        total_tokens=1_500,
        turns=2,
        windows=[snapshot],
        reset_windows=1,
    )
    assert "Total tokens: 1,500" in reply
    assert "5-hour limit: 42% used, 58% left · resets" in reply
    assert "last reported by Claude in this project" in reply
    assert "1 plan window has reset" in reply
    empty = claude_usage_reply(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        turns=0,
        windows=[],
        reset_windows=0,
    )
    assert "has not reported plan limits in this project yet" in empty
    assert "0%" not in empty


def test_claude_sessions_advertise_only_usage():
    assert [
        item["name"] for item in _profile_commands(HarnessKind.CLAUDE_AGENT_SDK)
    ] == ["usage"]
    assert "goal" in {
        item["name"] for item in _profile_commands(HarnessKind.CODEX_APP_SERVER)
    }


async def test_claude_usage_command_counts_the_conversation_without_a_model_turn(
    tmp_path,
):
    store = NebulaStore(tmp_path / "nebula.db")
    await store.create(
        HarnessSession(
            id="old-session",
            engagement_id="project",
            harness_profile_id="claude",
            model="claude-sonnet",
        )
    )
    await store.create(
        HarnessSession(
            id="session",
            engagement_id="project",
            harness_profile_id="claude",
            model="claude-opus",
        )
    )
    for turn_id, session_id, chat_id, tokens in [
        ("before-switch", "old-session", "chat", 100),
        ("after-switch", "session", "chat", 50),
        ("other-chat", "session", "other", 999),
    ]:
        await store.create(
            HarnessTurn(
                id=turn_id,
                engagement_id="project",
                harness_session_id=session_id,
                origin=HarnessTurnOrigin.CHAT,
                chat_session_id=chat_id,
                chat_turn_id=f"{turn_id}-chat-turn",
                prompt="Work",
                usage=ChatTokenUsage(
                    input_tokens=tokens - 10, output_tokens=10, total_tokens=tokens
                ),
            )
        )
    command_turn = await store.create(
        HarnessTurn(
            id="usage-turn",
            engagement_id="project",
            harness_session_id="session",
            origin=HarnessTurnOrigin.CHAT,
            chat_session_id="chat",
            chat_turn_id="usage-chat-turn",
            prompt="/usage",
            metadata={"user_prompt": "/usage"},
        )
    )
    await record_quota_observation(
        store, engagement_id="project", profile_id="claude", snapshot=_snapshot()
    )
    runtime = SimpleNamespace(store=store)
    events = [
        event
        async for event in HarnessRuntimeService._claude_command(
            runtime, command_turn, ("/usage", "")
        )
    ]
    assert [event.type for event in events] == [
        "started",
        "message_delta",
        "completed",
    ]
    message = events[-1].message
    assert "Total tokens: 150" in message
    assert "Turns: 2" in message
    assert "5-hour limit: 42% used" in message
    assert all(event.usage is None for event in events)
    with pytest.raises(HarnessConfigurationError, match="without arguments"):
        async for _ in HarnessRuntimeService._claude_command(
            runtime, command_turn, ("/usage", "extra")
        ):
            pass


async def test_usage_page_shows_claude_last_report_or_says_none_is_current(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    await store.create(
        HarnessProfile(
            id="claude",
            name="Claude",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            executable="/bin/true",
        )
    )
    client = TestClient(create_app(store, auth_token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    before = await client.get("/api/v1/runtime-usage", headers=headers)
    vendor = {item["profile_id"]: item for item in before.json()["items"]}["claude"][
        "vendor"
    ]
    assert vendor["state"] == "unavailable"
    assert "only during a turn" in vendor["detail"]
    assert vendor["windows"] == []
    await record_quota_observation(
        store,
        engagement_id="project",
        profile_id="claude",
        snapshot=_snapshot(used=0.42),
    )
    # Saved reports are read every time, so no refresh is needed to see one.
    after = await client.get("/api/v1/runtime-usage", headers=headers)
    vendor = {item["profile_id"]: item for item in after.json()["items"]}["claude"][
        "vendor"
    ]
    assert vendor["state"] == "available"
    assert vendor["source"] == "Claude plan limits · last reported"
    assert vendor["windows"][0]["label"] == "5-hour limit"
    assert vendor["windows"][0]["used_percent"] == 42
    assert datetime.fromisoformat(vendor["checked_at"]) == NOW


async def test_saved_claude_notice_is_kept_for_usage_reads(tmp_path):
    from nebula.v3.credentials import CredentialStore
    from nebula.v3.domain import Engagement
    from nebula.v3.harnesses import HarnessEvent

    store = NebulaStore(tmp_path / "nebula.db")
    await store.create(Engagement(id="project", name="Project"))
    await store.create(
        HarnessProfile(
            id="claude",
            name="Claude",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            executable="/bin/true",
        )
    )
    session = await store.create(
        HarnessSession(
            id="session",
            engagement_id="project",
            harness_profile_id="claude",
            model="model",
        )
    )
    turn = await store.create(
        HarnessTurn(
            id="turn",
            engagement_id="project",
            harness_session_id="session",
            origin=HarnessTurnOrigin.ANALYSIS,
            prompt="Work",
        )
    )
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda project_id: tmp_path,
    )
    await runtime._persist_activity(
        turn,
        session,
        HarnessEvent(type="notice", quota_snapshot=_snapshot(used=0.81)),
    )
    quota = await reported_quota(store, "claude", engagement_id="project")
    assert [round(item.remaining_percent) for item in quota.snapshots] == [19]
