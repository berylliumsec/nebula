import asyncio
import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import (
    HarnessMissionHandoffRequest,
    MissionStartRequest,
    create_app,
)
from nebula.v3.domain import (
    AgentAttempt,
    AgentRun,
    Engagement,
    ProviderProfile,
    RiskClass,
    RunBudget,
    RunBackend,
    RunStatus,
    ScopePolicy,
    Task,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.missions import MissionService
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderFlavor,
    ProviderHealth,
    ProviderKind,
)
from nebula.v3.storage import ConflictError, NebulaStore


def _auth():
    return {"Authorization": "Bearer test-token"}


def test_scheduled_native_mission_survives_service_restart_and_can_be_cancelled(
    tmp_path,
):
    async def scenario() -> None:
        store = NebulaStore(tmp_path / "scheduled.db")
        engagement = store.create(Engagement(name="Scheduled mission"))
        profile = _profile(store)
        provider = RecordingProvider(profile)
        scheduled_for = utc_now() + timedelta(days=1)
        first = MissionService(
            store,
            checkpoint_path=tmp_path / "scheduled-checkpoints.db",
            provider_factory=lambda selected: provider,
        )
        run = await first.start_mission(
            engagement_id=engagement.id,
            name="Daily review",
            objective="Review the bounded scope",
            provider_id=profile.id,
            model="security-model",
            budget=RunBudget(
                max_duration_seconds=30,
                max_tokens=2_000,
                max_tool_calls=0,
                max_delegation_depth=0,
            ),
            scheduled_for=scheduled_for,
            repeat_interval_seconds=86_400,
        )
        assert store.get(AgentRun, run.id).status == RunStatus.QUEUED
        assert run.id in first._scheduled_tasks
        await first.shutdown()
        assert store.get(AgentRun, run.id).status == RunStatus.QUEUED

        restored = MissionService(
            store,
            checkpoint_path=tmp_path / "scheduled-checkpoints.db",
            provider_factory=lambda selected: provider,
        )
        await restored.startup()
        assert run.id in restored._scheduled_tasks
        cancelled = await restored.stop_mission(
            run.id, reason="Schedule no longer needed"
        )
        assert cancelled.status == RunStatus.CANCELLED
        await restored.shutdown()

    asyncio.run(scenario())


def test_native_startup_leaves_scheduled_harness_missions_for_harness_runtime(tmp_path):
    async def scenario() -> None:
        store = NebulaStore(tmp_path / "cross-runtime-schedule.db")
        engagement = store.create(Engagement(name="Harness schedule ownership"))
        scheduled = store.create(
            AgentRun(
                engagement_id=engagement.id,
                objective="Remain owned by the harness runtime",
                backend=RunBackend.HARNESS,
                status=RunStatus.QUEUED,
                harness_profile_id="harness-profile",
                supervisor_model="harness-model",
                metadata={
                    "origin": "api",
                    "scheduled_for": (utc_now() + timedelta(days=1)).isoformat(),
                },
            )
        )
        service = MissionService(
            store,
            checkpoint_path=tmp_path / "cross-runtime-checkpoints.db",
        )

        await service.startup()

        assert store.get(AgentRun, scheduled.id).status == RunStatus.QUEUED
        assert store.replay_events(scheduled.id) == []
        await service.shutdown()

    asyncio.run(scenario())


def _profile(store, *, enabled=True, models=None):
    return store.create(
        ProviderProfile(
            name="Lab model",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8000/v1",
            enabled=enabled,
            is_local=True,
            model_allowlist=models if models is not None else ["security-model"],
        )
    )


def _config(profile):
    return ProviderConfig(
        id=profile.id,
        kind=ProviderKind.OPENAI_COMPATIBLE,
        flavor=ProviderFlavor.VLLM if profile.is_local else ProviderFlavor.CUSTOM,
        base_url=profile.endpoint or "http://127.0.0.1:8000/v1",
        default_model="security-model",
        local=profile.is_local,
        enabled=profile.enabled,
        capabilities=ModelCapabilities(),
    )


class RecordingProvider(ModelProvider):
    def __init__(self, profile):
        super().__init__(_config(profile))
        self.requests = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        model = self.require(request)
        return ModelResponse(
            provider_id=self.config.id,
            model=model,
            text="Scope is bounded and ready for analyst review.",
            usage=ModelUsage(input_tokens=8, output_tokens=7, total_tokens=15),
            finish_reason="stop",
            provider_request_id="mission-request-1",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.config.id, healthy=True)


class BlockingProvider(ModelProvider):
    def __init__(self, profile):
        super().__init__(_config(profile))
        self.started = threading.Event()
        self.cancelled = threading.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.require(request)
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.config.id, healthy=True)


class FailingProvider(ModelProvider):
    def __init__(self, profile):
        super().__init__(_config(profile))

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.require(request)
        raise RuntimeError("provider unavailable")

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.config.id, healthy=False)


def _app(tmp_path, provider, *, max_active_missions=4):
    store = NebulaStore(tmp_path / "nebula.db")
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
        provider_factory=lambda profile: provider,
        max_active_missions=max_active_missions,
        cancellation_timeout_seconds=2,
    )
    app = create_app(store, auth_token="test-token", mission_service=service)
    return app, store, service


def _wait_for_status(client, run_id, expected, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}", headers=_auth())
        assert response.status_code == 200
        if response.json()["status"] == expected:
            return response.json()
        time.sleep(0.02)
    raise AssertionError(
        f"run {run_id} did not reach {expected}: {response.json()['status']}"
    )


def _start_payload(engagement, profile, **changes):
    payload = {
        "engagement_id": engagement.id,
        "name": "Bounded scope review",
        "objective": "Review the explicitly bounded scope",
        "provider_id": profile.id,
        "model": "security-model",
        "max_duration_seconds": 60,
        "max_tokens": 100,
        "max_retries": 0,
    }
    payload.update(changes)
    return payload


def test_mission_budget_defaults_are_unlimited():
    budget = RunBudget()
    assert budget.max_duration_seconds is None
    assert budget.max_tokens is None
    assert budget.max_cost_usd is None
    assert budget.max_tool_calls is None
    assert budget.max_artifact_queries is None

    request = MissionStartRequest(
        engagement_id="engagement-1",
        name="Unlimited defaults",
        objective="Review the configured scope",
        provider_id="provider-1",
        model="security-model",
    )
    assert request.max_duration_seconds is None
    assert request.max_tokens is None
    assert request.max_cost_usd is None
    assert request.max_tool_calls is None
    assert request.max_artifact_queries is None

    handoff = HarnessMissionHandoffRequest()
    assert handoff.max_duration_seconds is None
    assert handoff.max_tokens is None
    assert handoff.max_cost_usd is None
    assert handoff.max_tool_calls is None
    assert handoff.max_artifact_queries is None

    long_duration = MissionStartRequest(
        engagement_id="engagement-1",
        name="Long mission",
        objective="Review the configured scope",
        provider_id="provider-1",
        model="security-model",
        max_duration_seconds=86_400,
    )
    assert long_duration.max_duration_seconds == 86_400


def test_mission_budget_accepts_explicit_long_duration(tmp_path):
    store = NebulaStore(tmp_path / "long-duration.db")
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "long-duration-checkpoints.db",
    )

    service._validate_budget(
        RunBudget(max_duration_seconds=86_400, max_delegation_depth=0),
        tool_names=[],
    )


def test_api_rejects_conflicting_mission_service_configuration(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    service = MissionService(store, checkpoint_path=tmp_path / "owned.db")

    with pytest.raises(ValueError, match="either mission_service"):
        create_app(
            store,
            auth_token="test-token",
            mission_service=service,
            mission_checkpoint_path=tmp_path / "other.db",
        )


def test_startup_fails_interrupted_api_runs_but_leaves_external_runs_untouched(
    tmp_path,
):
    store = NebulaStore(tmp_path / "restart.db")
    engagement = store.create(Engagement(name="Restart reconciliation"))
    interrupted = store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Interrupted API work",
            status=RunStatus.RUNNING,
            metadata={"origin": "api", "analysis_only": True},
        )
    )
    task = store.create(
        Task(
            engagement_id=engagement.id,
            run_id=interrupted.id,
            specialist_role="scope_planning",
            title="Interrupted task",
            status=TaskStatus.RUNNING,
        )
    )
    attempt = store.create(
        AgentAttempt(
            engagement_id=engagement.id,
            run_id=interrupted.id,
            task_id=task.id,
            agent_role="scope_planning",
            attempt_number=1,
            status=TaskStatus.RUNNING,
        )
    )
    external = store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Owned by a different runtime",
            status=RunStatus.RUNNING,
        )
    )
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
    )
    app = create_app(store, auth_token="test-token", mission_service=service)

    with TestClient(app) as client:
        assert (
            client.get(
                "/api/v1/health", headers={"Authorization": "Bearer test-token"}
            ).status_code
            == 200
        )

    failed = store.get(AgentRun, interrupted.id)
    assert failed.status == RunStatus.FAILED
    assert failed.completed_at is not None
    assert "restarted" in str(failed.metadata["error"])
    assert store.get(Task, task.id).status == TaskStatus.FAILED
    assert store.get(AgentAttempt, attempt.id).status == TaskStatus.FAILED
    assert store.replay_events(interrupted.id)[-1].event_type == "run.failed"
    assert store.get(AgentRun, external.id).status == RunStatus.RUNNING
    assert store.replay_events(external.id) == []


def test_startup_retries_an_optimistic_reconciliation_race(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "restart-race.db")
    engagement = store.create(Engagement(name="Restart race"))
    interrupted = store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Interrupted API work",
            status=RunStatus.QUEUED,
            metadata={"origin": "api"},
        )
    )
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
    )
    original = service._finalize_failed
    calls = 0

    def race_once(run_id, error):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConflictError("simulated optimistic race")
        return original(run_id, error)

    monkeypatch.setattr(service, "_finalize_failed", race_once)

    asyncio.run(service.startup())

    assert calls == 2
    assert store.get(AgentRun, interrupted.id).status == RunStatus.FAILED


def test_api_rejects_cloud_mission_for_local_only_engagement(tmp_path):
    store = NebulaStore(tmp_path / "privacy.db")
    engagement = store.create(Engagement(id="eng-local", name="Local mission"))
    policy = store.create(
        ScopePolicy(
            id="scope-local",
            engagement_id=engagement.id,
            local_only=True,
        )
    )
    engagement = store.update(
        Engagement,
        engagement.id,
        {"scope_policy_id": policy.id},
        expected_revision=engagement.revision,
    )
    profile = store.create(
        ProviderProfile(
            name="Cloud model",
            provider_type="custom",
            endpoint="https://provider.invalid/v1",
            is_local=False,
            model_allowlist=["security-model"],
        )
    )
    provider = RecordingProvider(profile)
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
        provider_factory=lambda selected: provider,
    )

    with TestClient(
        create_app(store, auth_token="test-token", mission_service=service)
    ) as client:
        response = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json=_start_payload(engagement, profile),
        )

    assert response.status_code == 422
    assert "local-only" in response.json()["detail"]
    assert provider.requests == []
    assert store.count(AgentRun, engagement_id=engagement.id) == 0


def test_api_starts_explicit_analysis_mission_and_persists_events(tmp_path):
    bootstrap_store = NebulaStore(tmp_path / "nebula.db")
    engagement = bootstrap_store.create(Engagement(name="API mission"))
    profile = _profile(bootstrap_store)
    provider = RecordingProvider(profile)
    service = MissionService(
        bootstrap_store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
        provider_factory=lambda selected: provider,
    )
    app = create_app(bootstrap_store, auth_token="test-token", mission_service=service)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json=_start_payload(engagement, profile),
        )
        assert response.status_code == 202, response.text
        queued = response.json()
        assert queued["status"] == "queued"
        assert queued["supervisor_provider_id"] == profile.id
        assert queued["supervisor_model"] == "security-model"
        assert queued["budget"]["max_tool_calls"] is None
        assert queued["budget"]["max_delegation_depth"] == 0

        completed = _wait_for_status(client, queued["id"], "complete")
        assert completed["completed_at"] is not None
        assert completed["metadata"] == {
            "name": "Bounded scope review",
            "analysis_only": True,
            "origin": "api",
            "total_tasks": 1,
            "completed_tasks": 1,
            "spent_usd": 0.0,
            "input_tokens": 8,
            "output_tokens": 7,
            "tool_calls": 0,
            "final_summary": (
                "Review the explicitly bounded scope: "
                "Scope is bounded and ready for analyst review."
            ),
        }
        events = bootstrap_store.replay_events(queued["id"])
        event_types = [event.event_type for event in events]
        assert event_types == [
            "run.queued",
            "run.started",
            "run.planned",
            "task.started",
            "task.completed",
            "task.verified",
            "run.completed",
        ]
        assert events[0].payload["provider_id"] == profile.id
        assert events[0].payload["model"] == "security-model"
        assert events[0].payload["analysis_only"] is True
        assert provider.requests[0].model == "security-model"
        assert provider.requests[0].tools == []

        terminal_stop = client.post(
            f"/api/v1/runs/{queued['id']}/stop",
            headers=_auth(),
            json={"reason": "too late"},
        )
        assert terminal_stop.status_code == 409
        assert (
            client.post(
                f"/api/v1/runs/{queued['id']}/pause", headers=_auth(), json={}
            ).status_code
            == 404
        )


def test_api_retry_creates_a_new_audited_run_with_frozen_runtime(tmp_path):
    store = NebulaStore(tmp_path / "retry.db")
    engagement = store.create(Engagement(name="Mission retry"))
    profile = _profile(store)
    provider = RecordingProvider(profile)
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "retry-checkpoints.db",
        provider_factory=lambda selected: provider,
    )
    original = store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Recheck the bounded scope",
            status=RunStatus.INTERRUPTED,
            supervisor_provider_id=profile.id,
            supervisor_model="security-model",
            budget=RunBudget(
                max_duration_seconds=60,
                max_tokens=100,
                max_tool_calls=0,
                max_concurrency=1,
                max_delegation_depth=0,
            ),
            runtime_snapshot={"mcp_server_ids": [], "remote_mcp_confirmed": False},
            metadata={
                "name": "Interrupted review",
                "stages": [
                    {"title": "Verify", "objective": "Verify the strongest observation"}
                ],
            },
        )
    )

    with TestClient(
        create_app(store, auth_token="test-token", mission_service=service)
    ) as client:
        response = client.post(
            f"/api/v1/runs/{original.id}/retry",
            headers=_auth(),
            json={},
        )
        assert response.status_code == 202, response.text
        retried = response.json()
        assert retried["id"] != original.id
        assert retried["supervisor_provider_id"] == profile.id
        assert retried["supervisor_model"] == "security-model"
        assert retried["metadata"]["retry_of_run_id"] == original.id
        assert retried["metadata"]["stages"] == original.metadata["stages"]
        _wait_for_status(client, retried["id"], "complete")

    assert store.get(AgentRun, original.id).status == RunStatus.INTERRUPTED


def test_stop_cancels_the_actual_provider_task_and_open_records(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Cancellation"))
    profile = _profile(store)
    provider = BlockingProvider(profile)
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
        provider_factory=lambda selected: provider,
        cancellation_timeout_seconds=2,
    )
    app = create_app(store, auth_token="test-token", mission_service=service)

    with TestClient(app) as client:
        started = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json=_start_payload(engagement, profile),
        )
        assert started.status_code == 202
        run_id = started.json()["id"]
        assert provider.started.wait(timeout=5)
        waiting_call = store.create(
            ToolCall(
                engagement_id=engagement.id,
                run_id=run_id,
                tool_name="nmap.connect_scan",
                status=ToolCallStatus.WAITING_APPROVAL,
                risk_class=RiskClass.ACTIVE_SCAN,
            )
        )

        stopped = client.post(
            f"/api/v1/runs/{run_id}/stop",
            headers=_auth(),
            json={"reason": "Operator ended the review"},
        )

        assert stopped.status_code == 200
        assert stopped.json()["status"] == "cancelled"
        assert provider.cancelled.wait(timeout=2)
        assert run_id not in service.active_run_ids
        tasks = [task for task in store.list_entities(Task) if task.run_id == run_id]
        attempts = [
            attempt
            for attempt in store.list_entities(AgentAttempt)
            if attempt.run_id == run_id
        ]
        assert tasks and {task.status for task in tasks} == {TaskStatus.CANCELLED}
        assert attempts and {attempt.status for attempt in attempts} == {
            TaskStatus.CANCELLED
        }
        cancelled_call = store.get(ToolCall, waiting_call.id)
        assert cancelled_call.status == ToolCallStatus.CANCELLED
        assert cancelled_call.error == "Operator ended the review"
        events = store.replay_events(run_id)
        assert [event.event_type for event in events][-4:] == [
            "run.stop_requested",
            "task.cancelled",
            "tool.cancelled",
            "run.cancelled",
        ]
        assert events[-1].payload["reason"] == "Operator ended the review"


def test_allowlist_disabled_profiles_and_capacity_fail_before_extra_runs(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Validation"))
    profile = _profile(store)
    disabled = _profile(store, enabled=False, models=["disabled-model"])
    provider = BlockingProvider(profile)
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
        provider_factory=lambda selected: provider,
        max_active_missions=1,
        cancellation_timeout_seconds=2,
    )
    app = create_app(store, auth_token="test-token", mission_service=service)

    with TestClient(app) as client:
        outside_allowlist = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json=_start_payload(engagement, profile, model="other-model"),
        )
        assert outside_allowlist.status_code == 422
        assert (
            "outside the provider profile allowlist"
            in outside_allowlist.json()["detail"]
        )
        disabled_response = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json=_start_payload(
                engagement,
                disabled,
                model="disabled-model",
            ),
        )
        assert disabled_response.status_code == 422
        assert "disabled" in disabled_response.json()["detail"]
        missing_model = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json={
                key: value
                for key, value in _start_payload(engagement, profile).items()
                if key != "model"
            },
        )
        assert missing_model.status_code == 422
        missing_name = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json={
                key: value
                for key, value in _start_payload(engagement, profile).items()
                if key != "name"
            },
        )
        assert missing_name.status_code == 422
        assert store.count(AgentRun) == 0

        first = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json=_start_payload(engagement, profile),
        )
        assert first.status_code == 202
        assert provider.started.wait(timeout=5)
        capacity = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json=_start_payload(engagement, profile),
        )
        assert capacity.status_code == 429
        assert store.count(AgentRun) == 1
        client.post(
            f"/api/v1/runs/{first.json()['id']}/stop",
            headers=_auth(),
            json={},
        )


def test_background_provider_failure_is_durable(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Failure"))
    profile = _profile(store)
    provider = FailingProvider(profile)
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
        provider_factory=lambda selected: provider,
    )
    app = create_app(store, auth_token="test-token", mission_service=service)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json=_start_payload(engagement, profile),
        )
        run_id = response.json()["id"]
        failed = _wait_for_status(client, run_id, "failed")
        assert failed["completed_at"] is not None
        assert [event.event_type for event in store.replay_events(run_id)][-2:] == [
            "task.failed",
            "run.failed",
        ]


def test_stop_refuses_to_fake_cancellation_for_an_unowned_run(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="External runner"))
    external = store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Owned by another runner",
            status=RunStatus.RUNNING,
        )
    )
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
    )
    app = create_app(store, auth_token="test-token", mission_service=service)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/runs/{external.id}/stop",
            headers=_auth(),
            json={"reason": "Cannot prove this stopped"},
        )

    assert response.status_code == 409
    assert "cancellation cannot be confirmed" in response.json()["detail"]
    assert store.get(AgentRun, external.id).status == RunStatus.RUNNING
    assert store.replay_events(external.id) == []


def test_app_shutdown_cancels_owned_background_missions(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Shutdown"))
    profile = _profile(store)
    provider = BlockingProvider(profile)
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
        provider_factory=lambda selected: provider,
        cancellation_timeout_seconds=2,
    )
    app = create_app(store, auth_token="test-token", mission_service=service)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/missions",
            headers=_auth(),
            json=_start_payload(engagement, profile),
        )
        run_id = response.json()["id"]
        assert provider.started.wait(timeout=5)

    run = store.get(AgentRun, run_id)
    assert run.status == RunStatus.CANCELLED
    assert provider.cancelled.is_set()
    events = store.replay_events(run_id)
    assert events[-1].event_type == "run.cancelled"
    assert events[-1].actor_id == "system"
    assert events[-1].payload["reason"] == "Nebula Core is shutting down"


def test_failure_cleanup_paginates_all_run_tasks_and_attempts(tmp_path):
    store = NebulaStore(tmp_path / "pagination.db")
    engagement = store.create(Engagement(name="Large interrupted mission"))
    run = store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="reconcile every persisted work item",
            status=RunStatus.RUNNING,
            metadata={"origin": "api"},
        )
    )
    tasks = [
        Task(
            id=f"task-{number:04d}",
            engagement_id=engagement.id,
            run_id=run.id,
            specialist_role="scope_planning",
            title=f"Task {number}",
        )
        for number in range(1_001)
    ]
    attempts = [
        AgentAttempt(
            id=f"attempt-{number:04d}",
            engagement_id=engagement.id,
            run_id=run.id,
            task_id=tasks[number].id,
            agent_role="scope_planning",
            attempt_number=1,
            status=TaskStatus.COMPLETE,
        )
        for number in range(1_001)
    ]
    store.create_many([*tasks, *attempts])
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
    )

    assert len(list(service._run_attempts(run))) == 1_001
    service._fail_open_work(run, "Core restarted")

    assert all(task.status == TaskStatus.FAILED for task in service._run_tasks(run))
    events = store.replay_events(run.id, limit=10_000)
    assert len(events) == 1_001
    assert events[-1].payload["task_id"] == "task-1000"
