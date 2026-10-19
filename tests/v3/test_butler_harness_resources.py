"""Authoritative harness windows produce bounded project quota incidents."""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.butler_harness_resources import (
    quota_state_id,
    record_harness_quota,
    record_harness_rate_limit,
)
from nebula.v3.butler_oversight import acknowledge_incident, project_stream_id
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.control_assistant_work import dispatch_work_tool
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ButlerHarnessQuota,
    ButlerIncident,
    ChatBackend,
    ChatSession,
    Engagement,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
)
from nebula.v3.harness_quota import HarnessQuotaSnapshot
from nebula.v3.harnesses import (
    CodexAppServerAdapter,
    GrokAcpAdapter,
    HarnessEvent,
    HarnessRuntimeService,
)
from nebula.v3.storage import NebulaStore


OBSERVED = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _profile(store: NebulaStore, profile_id: str, kind: HarnessKind) -> None:
    store.create(
        HarnessProfile(
            id=profile_id,
            name=profile_id,
            kind=kind,
            executable="/bin/true",
        )
    )


def _watched_project(store: NebulaStore, project_id: str, profile_id: str) -> None:
    store.create(Engagement(id=project_id, name=project_id))
    store.create(
        HarnessSession(
            id=f"harness-{project_id}",
            engagement_id=project_id,
            harness_profile_id=profile_id,
            model="model",
        )
    )
    store.create(
        ChatSession(
            id=f"chat-{project_id}",
            engagement_id=project_id,
            title="Harness goal",
            backend=ChatBackend.HARNESS,
            harness_profile_id=profile_id,
            harness_session_id=f"harness-{project_id}",
            model="model",
        )
    )
    goals = ChatGoalService(store)
    draft = goals.create(
        f"chat-{project_id}",
        GoalCreate(objective="Research", completion_criteria=["Evidence"]),
    )
    goals.write(
        f"chat-{project_id}",
        GoalWrite(expected_revision=draft.revision, action="start"),
    )


def _snapshot(
    remaining: float,
    *,
    observed_at: datetime = OBSERVED,
    resets_at: datetime | None = None,
    kind: HarnessKind = HarnessKind.CODEX_APP_SERVER,
) -> HarnessQuotaSnapshot:
    return HarnessQuotaSnapshot(
        harness_kind=kind,
        source=(
            "codex.account/rateLimits/read"
            if kind == HarnessKind.CODEX_APP_SERVER
            else "claude.RateLimitEvent"
        ),
        window_id="weekly:primary",
        window_label="Included usage · Short window",
        used_percent=100 - remaining,
        remaining_percent=remaining,
        observed_at=observed_at,
        resets_at=resets_at,
        window_duration_minutes=300,
        basis=(
            "vendor_used_percent"
            if kind == HarnessKind.CODEX_APP_SERVER
            else "vendor_utilization_fraction"
        ),
    )


def _events(store: NebulaStore, project_id: str) -> list[str]:
    return [
        item.event_type
        for item in store.replay_operation_events(project_stream_id(project_id))
    ]


def test_quota_boundary_dedup_acknowledgment_and_new_window_are_project_scoped(
    tmp_path,
):
    store = NebulaStore(tmp_path / "quota.db")
    _profile(store, "codex", HarnessKind.CODEX_APP_SERVER)
    _profile(store, "claude", HarnessKind.CLAUDE_AGENT_SDK)
    _watched_project(store, "one", "codex")
    _watched_project(store, "two", "codex")
    _watched_project(store, "unrelated", "claude")
    reset = OBSERVED + timedelta(hours=5)
    first = _snapshot(20, resets_at=reset)
    assert record_harness_quota(store, profile_id="codex", snapshot=first) == 2
    assert record_harness_quota(store, profile_id="codex", snapshot=first) == 0
    for project_id in ("one", "two"):
        state = store.get(
            ButlerHarnessQuota, quota_state_id(project_id, "codex", first.window_id)
        )
        assert state.remaining_percent == 20
        assert state.threshold_state == "at_or_below"
        incidents = store.find_entities(
            ButlerIncident,
            {"source": "harness_quota"},
            engagement_id=project_id,
            limit=10,
        )
        assert len(incidents) == 1
        assert incidents[0].status == "open"
        assert incidents[0].verification_state == "vendor_reported"
        assert _events(store, project_id) == [
            "goal.started",
            "resource.harness_quota_observed",
            "incident.opened",
        ]
    assert _events(store, "unrelated") == ["goal.started"]

    first_incident = store.find_entities(
        ButlerIncident, {"source": "harness_quota"}, engagement_id="one", limit=10
    )[0]
    acknowledge_incident(
        store,
        engagement_id="one",
        record_id=first_incident.id,
        expected_revision=first_incident.revision,
    )
    record_harness_quota(
        store,
        profile_id="codex",
        snapshot=_snapshot(
            19, observed_at=OBSERVED + timedelta(minutes=1), resets_at=reset
        ),
    )
    assert store.get(ButlerIncident, first_incident.id).status == "acknowledged"
    record_harness_quota(
        store,
        profile_id="codex",
        snapshot=_snapshot(
            21, observed_at=OBSERVED + timedelta(minutes=2), resets_at=reset
        ),
    )
    assert store.get(ButlerIncident, first_incident.id).status == "resolved"
    record_harness_quota(
        store,
        profile_id="codex",
        snapshot=_snapshot(
            20,
            observed_at=OBSERVED + timedelta(hours=5, minutes=1),
            resets_at=reset + timedelta(hours=5),
        ),
    )
    incidents = store.find_entities(
        ButlerIncident, {"source": "harness_quota"}, engagement_id="one", limit=10
    )
    assert len(incidents) == 2
    assert sorted(item.status for item in incidents) == ["open", "resolved"]
    assert _events(store, "one").count("incident.opened") == 2
    assert (
        record_harness_quota(
            store,
            profile_id="codex",
            snapshot=_snapshot(
                21, observed_at=OBSERVED + timedelta(minutes=2), resets_at=reset
            ),
        )
        == 0
    )
    latest = store.get(
        ButlerHarnessQuota, quota_state_id("one", "codex", first.window_id)
    )
    assert latest.threshold_state == "at_or_below"
    assert latest.resets_at == reset + timedelta(hours=5)


def test_saved_claude_numeric_notice_and_codex_usage_trigger_observers(tmp_path):
    store = NebulaStore(tmp_path / "quota.db")
    store.create(Engagement(id="project", name="Project"))
    _profile(store, "claude", HarnessKind.CLAUDE_AGENT_SDK)
    _profile(store, "codex", HarnessKind.CODEX_APP_SERVER)
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda project_id: tmp_path,
    )
    observed = []
    reads = []

    def on_snapshot(profile_id, snapshot):
        assert store.replay_operation_events("claude-turn")
        observed.append((profile_id, snapshot.remaining_percent))

    runtime.bind_quota_observers(on_snapshot, reads.append)
    for profile_id in ("claude", "codex"):
        store.create(
            HarnessSession(
                id=f"{profile_id}-session",
                engagement_id="project",
                harness_profile_id=profile_id,
                model="model",
            )
        )
        store.create(
            HarnessTurn(
                id=f"{profile_id}-turn",
                engagement_id="project",
                harness_session_id=f"{profile_id}-session",
                origin=HarnessTurnOrigin.ANALYSIS,
                prompt="Check quota",
            )
        )
    runtime._persist_activity(
        store.get(HarnessTurn, "claude-turn"),
        store.get(HarnessSession, "claude-session"),
        HarnessEvent(
            type="notice",
            quota_snapshot=_snapshot(19, kind=HarnessKind.CLAUDE_AGENT_SDK),
        ),
    )
    runtime._persist_activity(
        store.get(HarnessTurn, "codex-turn"),
        store.get(HarnessSession, "codex-session"),
        HarnessEvent(type="usage"),
    )
    assert observed == [("claude", 19)]
    assert reads == ["codex"]


def test_saved_rate_limit_rejection_opens_separate_incident_and_usage_resolves_it(
    tmp_path,
):
    store = NebulaStore(tmp_path / "rate-limit.db")
    _profile(store, "claude", HarnessKind.CLAUDE_AGENT_SDK)
    _watched_project(store, "project", "claude")
    turn = store.create(
        HarnessTurn(
            id="turn",
            engagement_id="project",
            harness_session_id="harness-project",
            origin=HarnessTurnOrigin.ANALYSIS,
            prompt="Check work",
        )
    )
    session = store.get(HarnessSession, "harness-project")
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda project_id: tmp_path,
    )
    recorded = []

    def on_rate_limit(profile_id, turn_id, event_id, observed_at, recovered):
        assert store.replay_operation_events(turn_id)[-1].id == event_id
        recorded.append((event_id, recovered))
        record_harness_rate_limit(
            store,
            profile_id=profile_id,
            turn_id=turn_id,
            activity_event_id=event_id,
            observed_at=observed_at,
            recovered=recovered,
        )

    runtime.bind_quota_observers(lambda *_: None, lambda *_: None, on_rate_limit)
    runtime._persist_activity(
        turn,
        session,
        HarnessEvent(
            type="notice",
            vendor=HarnessKind.CLAUDE_AGENT_SDK,
            reason_code="rate_limited",
            summary="rejected",
        ),
    )
    incidents = store.find_entities(
        ButlerIncident,
        {"source": "harness_rate_limit"},
        engagement_id="project",
        limit=10,
    )
    assert len(incidents) == 1
    assert incidents[0].status == "open"
    assert incidents[0].verification_state == "vendor_reported"
    assert store.count_entities(ButlerHarnessQuota, engagement_id="project") == 0
    assert [
        event.event_type
        for event in store.replay_operation_events(project_stream_id("project"))
    ][-1] == "incident.opened"
    assert (
        record_harness_rate_limit(
            store,
            profile_id="claude",
            turn_id=turn.id,
            activity_event_id=recorded[0][0],
            observed_at=incidents[0].observed_at,
        )
        is False
    )

    acknowledge_incident(
        store,
        engagement_id="project",
        record_id=incidents[0].id,
        expected_revision=incidents[0].revision,
    )
    runtime._persist_activity(
        turn,
        session,
        HarnessEvent(
            type="notice",
            vendor=HarnessKind.CLAUDE_AGENT_SDK,
            reason_code="rate_limited",
            summary="rejected again",
        ),
    )
    assert store.get(ButlerIncident, incidents[0].id).status == "acknowledged"
    runtime._persist_activity(
        turn,
        session,
        HarnessEvent(type="usage", vendor=HarnessKind.CLAUDE_AGENT_SDK),
    )
    assert recorded[-1][1] is True
    assert store.get(ButlerIncident, incidents[0].id).status == "resolved"
    runtime._persist_activity(
        turn,
        session,
        HarnessEvent(
            type="notice",
            vendor=HarnessKind.CLAUDE_AGENT_SDK,
            reason_code="rate_limited",
            summary="rejected after recovery",
        ),
    )
    assert store.get(ButlerIncident, incidents[0].id).status == "open"


def test_manual_codex_quota_refresh_persists_project_resource(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "quota.db")
    _profile(store, "codex", HarnessKind.CODEX_APP_SERVER)
    _watched_project(store, "project", "codex")

    async def account_read(self, profile, credentials):
        return {"rateLimits": {"primary": {"usedPercent": 80}}}

    monkeypatch.setattr(CodexAppServerAdapter, "read_account_usage", account_read)
    app = create_app(store=store, auth_token="test-token")
    with TestClient(app) as client:
        path = "/api/v1/harnesses/codex/quota/refresh"
        assert client.post(path).status_code == 401
        response = client.post(path, headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200, response.text
        assert response.json()["snapshots"][0]["remaining_percent"] == 20
        overview = client.get(
            "/api/v1/engagements/project/butler/overview",
            headers={"Authorization": "Bearer test-token"},
        )
        assert overview.status_code == 200, overview.text
        body = overview.json()
        assert body["summary"]["harness_quota_resources"] == 1
        assert body["summary"]["open_incidents"] == 1
        assert body["resources"][0]["kind"] == "harness_quota"
        assert body["resources"][0]["remaining_percent"] == 20


def test_grok_quota_refresh_is_visible_to_butler_and_queryable_by_agent(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "quota.db")
    _profile(store, "grok", HarnessKind.GROK_ACP)
    _watched_project(store, "project", "grok")

    async def account_read(self, profile, credentials):
        return {"config": {
            "creditUsagePercent": 81,
            "isUnifiedBillingUser": True,
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": "2026-10-26T12:00:00Z"},
        }}

    monkeypatch.setattr(GrokAcpAdapter, "read_account_usage", account_read)
    app = create_app(store=store, auth_token="test-token")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/harnesses/grok/quota/refresh",
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["snapshots"][0]["remaining_percent"] == 19
        overview = client.get(
            "/api/v1/engagements/project/butler/overview",
            headers={"Authorization": "Bearer test-token"},
        ).json()
        assert overview["resources"][0]["source"] == "grok._x.ai/billing"
        assert overview["resources"][0]["remaining_percent"] == 19
        assert overview["summary"]["open_incidents"] == 1
        queried = asyncio.run(dispatch_work_tool(
            app.state.control_assistant_work,
            "project",
            "nebula.work.harness_quota",
            {"profile_id": "grok", "refresh": True},
        ))
        assert queried["snapshots"][0]["remaining_percent"] == 19
        assert queried["snapshots"][0]["source"] == "grok._x.ai/billing"
