"""Nebula-wide, operator-owned Butler mandate and durable scheduled clock."""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_instructions import (
    ButlerInstructionsError,
    MAX_INSTRUCTIONS_BYTES,
    read_butler_instructions,
)
from nebula.v3.butler_oversight import project_state_id, project_stream_id
from nebula.v3.butler_recovery import (
    ButlerRecoveryAgent,
    ButlerRecoveryService,
    GLOBAL_STATE_ID,
)
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ButlerGlobalState,
    ButlerProjectState,
    ButlerRecoveryCase,
    ChatBackend,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessModelOptions,
    HarnessProfile,
    HarnessRuntimeOption,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    ProviderProfile,
    utc_now,
)
from nebula.v3.storage import NebulaStore


def _write(tmp_path, text: str = "Check idle work.") -> None:
    folder = tmp_path / ".butler"
    folder.mkdir(exist_ok=True)
    (folder / "BUTLER.md").write_text(text, encoding="utf-8")


def test_user_owned_instructions_are_optional_fresh_bounded_and_symlink_safe(tmp_path):
    assert read_butler_instructions(tmp_path) is None
    _write(tmp_path)
    first = read_butler_instructions(tmp_path)
    assert first is not None and first.text == "Check idle work."
    _write(tmp_path, "Check projects and goals.")
    second = read_butler_instructions(tmp_path)
    assert second is not None and second.sha256 != first.sha256
    _write(tmp_path, "a" * (MAX_INSTRUCTIONS_BYTES + 1))
    with pytest.raises(ButlerInstructionsError, match="32 KiB"):
        read_butler_instructions(tmp_path)
    (tmp_path / ".butler" / "BUTLER.md").unlink()
    (tmp_path / ".butler" / "BUTLER.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(ButlerInstructionsError, match="safely read"):
        read_butler_instructions(tmp_path)


async def test_one_global_check_wakes_idle_projects_and_persists_across_restart(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    projects = [
        (await store.create(Engagement(name=name))) for name in ("First", "Second")
    ]
    provider = await store.create(
        ProviderProfile(name="Fixture", provider_type="ollama", is_local=True)
    )
    worker_id = "worker-1"
    for project in projects:
        (
            await store.create(
                ButlerAuthorityPolicy(
                    id=policy_id(project.id), engagement_id=project.id, authority="act"
                )
            )
        )
        (
            await store.create(
                ButlerProjectState(
                    id=project_state_id(project.id),
                    engagement_id=project.id,
                    lease_owner=worker_id,
                    lease_expires_at=utc_now() + timedelta(minutes=10),
                )
            )
        )
        (
            await store.create(
                ChatSession(
                    engagement_id=project.id,
                    title="Butler",
                    model="fixture-model",
                    provider_profile_id=provider.id,
                    metadata={"control_assistant": True},
                )
            )
        )
    recovery = ButlerRecoveryService(store, instructions_root=tmp_path)
    assert (await recovery.global_status())["status"] == "missing"
    assert not (await recovery.fire_global_periodic_check(projects[0].id, worker_id))
    _write(tmp_path)
    anchor = await recovery._global_anchor()
    assert anchor in {project.id for project in projects}
    other = next(project.id for project in projects if project.id != anchor)
    assert not (await recovery.fire_global_periodic_check(other, worker_id))
    assert await recovery.fire_global_periodic_check(anchor, worker_id)
    assert not (await recovery.fire_global_periodic_check(anchor, worker_id))
    assert not (await recovery.fire_periodic_check(anchor, worker_id))
    events = await store.replay_operation_events(project_stream_id(anchor))
    assert [event.event_type for event in events] == ["butler.global_periodic_check"]
    assert (await recovery.capture_events(anchor, worker_id)) == 1
    case = (await store.find_entities(ButlerRecoveryCase, {}, engagement_id=anchor))[0]
    assert case.signal == "butler.global_periodic_check"
    agent = ButlerRecoveryAgent(
        store,
        provider_factory=lambda _: None,
        harness_runtime=None,
        artifact_store=None,
        work=None,
        memory=object(),
        instructions_root=tmp_path,
    )
    assert json.loads((await agent._packet(case)))["precedents"] is None
    assert "Check idle work." in (await agent._global_instructions(case))
    state = await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    assert (await recovery.global_status())["last_check_at"] == state.last_periodic_at
    assert (await recovery.global_status())["check_interval_minutes"] == 5
    restarted = ButlerRecoveryService(store, instructions_root=tmp_path)
    assert not (await restarted.fire_global_periodic_check(anchor, worker_id))
    (
        await store.update(
            ButlerGlobalState,
            GLOBAL_STATE_ID,
            {"last_periodic_at": utc_now() - timedelta(minutes=4)},
            expected_revision=state.revision,
        )
    )
    assert not (await restarted.fire_global_periodic_check(anchor, worker_id))
    state = await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    (
        await store.update(
            ButlerGlobalState,
            GLOBAL_STATE_ID,
            {"last_periodic_at": utc_now() - timedelta(minutes=6)},
            expected_revision=state.revision,
        )
    )
    _write(tmp_path, "New operator instructions.")
    with pytest.raises(ButlerInstructionsError, match="changed"):
        (await agent._global_instructions(case))
    # The first check is still open, so the due check waits for it.
    assert not (await restarted.fire_global_periodic_check(anchor, worker_id))
    (
        await store.update(
            ButlerRecoveryCase,
            case.id,
            {"status": "cancelled", "finished_at": utc_now()},
            expected_revision=case.revision,
        )
    )
    assert await restarted.fire_global_periodic_check(anchor, worker_id)
    assert len((await store.replay_operation_events(project_stream_id(anchor)))) == 2


async def _anchored_project(tmp_path):
    """One Act project anchoring the Nebula-wide check, with a leased worker."""

    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Anchor"))
    provider = await store.create(
        ProviderProfile(name="Fixture", provider_type="ollama", is_local=True)
    )
    policy = await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="act"
        )
    )
    await store.create(
        ButlerProjectState(
            id=project_state_id(project.id),
            engagement_id=project.id,
            lease_owner="worker-1",
            lease_expires_at=utc_now() + timedelta(minutes=10),
        )
    )
    await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Butler",
            model="fixture-model",
            provider_profile_id=provider.id,
            metadata={"control_assistant": True},
        )
    )
    _write(tmp_path)
    return store, project, policy


async def _rewind_clock(store, minutes: int = 6) -> None:
    state = await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    await store.update(
        ButlerGlobalState,
        GLOBAL_STATE_ID,
        {"last_periodic_at": utc_now() - timedelta(minutes=minutes)},
        expected_revision=state.revision,
    )


async def _set_status(store, case_id: str, status: str) -> None:
    current = await store.get(ButlerRecoveryCase, case_id)
    await store.update(
        ButlerRecoveryCase,
        case_id,
        {"status": status},
        expected_revision=current.revision,
    )


async def test_global_check_does_not_fire_while_the_last_one_is_open(
    tmp_path, monkeypatch
):
    store, project, _ = await _anchored_project(tmp_path)
    recovery = ButlerRecoveryService(store, instructions_root=tmp_path)
    assert await recovery.fire_global_periodic_check(project.id, "worker-1")
    assert (await recovery.capture_events(project.id, "worker-1")) == 1
    (case,) = await store.find_entities(
        ButlerRecoveryCase, {}, engagement_id=project.id
    )
    await _rewind_clock(store)
    for status in ("observed", "investigating", "verifying"):
        await _set_status(store, case.id, status)
        assert not (await recovery.fire_global_periodic_check(project.id, "worker-1"))
    # Skipping leaves the clock alone, so the check fires once the last one ends.
    before = await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    assert before.last_periodic_at < utc_now() - timedelta(minutes=5)
    await _set_status(store, case.id, "resolved")
    assert await recovery.fire_global_periodic_check(project.id, "worker-1")


async def test_abandoned_open_check_does_not_hold_the_schedule(tmp_path, monkeypatch):
    from nebula.v3 import butler_recovery

    store, project, _ = await _anchored_project(tmp_path)
    recovery = ButlerRecoveryService(store, instructions_root=tmp_path)
    assert await recovery.fire_global_periodic_check(project.id, "worker-1")
    (await recovery.capture_events(project.id, "worker-1"))
    (case,) = await store.find_entities(
        ButlerRecoveryCase, {}, engagement_id=project.id
    )
    await _set_status(store, case.id, "investigating")
    await _rewind_clock(store)
    assert not (await recovery.fire_global_periodic_check(project.id, "worker-1"))
    # Every existing case is now older than the cutoff.
    monkeypatch.setattr(butler_recovery, "STALE_OPEN_CHECK", timedelta(seconds=-60))
    assert await recovery.fire_global_periodic_check(project.id, "worker-1")


async def test_claim_waits_for_the_running_check_but_not_other_cases(tmp_path):
    store, project, policy = await _anchored_project(tmp_path)

    def make(event: str, signal: str, trigger: str) -> ButlerRecoveryCase:
        return ButlerRecoveryCase(
            id=f"case-{event}",
            engagement_id=project.id,
            trigger=trigger,
            signal=signal,
            source_event_id=event,
            source_event_sequence=1,
            authority_revision=policy.revision,
            summary="Fixture case.",
        )

    running = await store.create(
        make("first-check", "butler.global_periodic_check", "periodic")
    )
    await _set_status(store, running.id, "investigating")
    waiting = await store.create(
        make("second-check", "butler.global_periodic_check", "periodic")
    )
    blocked_goal = await store.create(make("goal", "goal.blocked", "immediate"))
    recovery = ButlerRecoveryService(store, instructions_root=tmp_path)
    claimed = await recovery._claim_case(project.id, "worker-1")
    assert claimed is not None and claimed.id == blocked_goal.id
    assert (await recovery._claim_case(project.id, "worker-1")) is None
    assert (await store.get(ButlerRecoveryCase, waiting.id)).status == "observed"
    await _set_status(store, running.id, "resolved")
    claimed = await recovery._claim_case(project.id, "worker-1")
    assert claimed is not None and claimed.id == waiting.id


async def test_global_check_uses_saved_butler_harness_effort(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        HarnessProfile(
            name="Butler harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            default_model="test-model",
            executable="/bin/true",
            privacy={"local_only": True},
            capabilities=HarnessCapabilities(
                models=["test-model"],
                model_options=[
                    HarnessModelOptions(
                        model="test-model",
                        reasoning_efforts=[HarnessRuntimeOption(id="max", label="Max")],
                    )
                ],
            ),
        )
    )
    alternate = await store.create(
        HarnessProfile(
            name="Butler alternate account",
            kind=HarnessKind.CODEX_APP_SERVER,
            default_model="test-model",
            executable="/bin/true",
            privacy={"local_only": True},
            capabilities=profile.capabilities,
        )
    )
    harness_session = await store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id=profile.id,
            model="test-model",
        )
    )
    (
        await store.create(
            ChatSession(
                engagement_id=project.id,
                title="Butler",
                backend=ChatBackend.HARNESS,
                harness_profile_id=profile.id,
                harness_session_id=harness_session.id,
                model="test-model",
                metadata={
                    "control_assistant": True,
                    "assistant_config": {
                        "backend": "harness",
                        "profile_id": profile.id,
                        "model": "test-model",
                        "harness_reasoning_effort": "max",
                    },
                },
            )
        )
    )
    (
        await store.create(
            ButlerAuthorityPolicy(
                id=policy_id(project.id), engagement_id=project.id, authority="act"
            )
        )
    )
    worker_id = "worker-1"
    (
        await store.create(
            ButlerProjectState(
                id=project_state_id(project.id),
                engagement_id=project.id,
                lease_owner=worker_id,
                lease_expires_at=utc_now() + timedelta(minutes=10),
            )
        )
    )
    _write(tmp_path)
    recovery = ButlerRecoveryService(store, instructions_root=tmp_path)
    assert await recovery.fire_global_periodic_check(project.id, worker_id)
    assert (await recovery.capture_events(project.id, worker_id)) == 1
    case = (
        await store.find_entities(ButlerRecoveryCase, {}, engagement_id=project.id)
    )[0]

    # #945 (99bb2e0) runs the scheduled check as a durable harness chat turn:
    # create_session, prepare_chat, stream_turn, then the chat turn's result.
    class Runtime:
        async def _validate_harness_privacy(self, *_args, **_kwargs):
            pass

        async def create_session(self, **kwargs):
            assert kwargs["profile_id"] == profile.id
            assert kwargs["control_assistant"] is True
            return await store.create(
                HarnessSession(
                    engagement_id=project.id,
                    harness_profile_id=kwargs["profile_id"],
                    model=kwargs["model"],
                )
            )

        async def prepare_chat(self, **request):
            assert request["profile_id"] == alternate.id
            assert request["harness_reasoning_effort"] == "max"
            assert "Check idle work." in request["runtime_context"]
            turn = await store.create(
                ChatTurn(
                    engagement_id=project.id,
                    session_id=request["chat_session_id"],
                    backend=ChatBackend.HARNESS,
                    harness_turn_id="harness-turn",
                    model=request["model"],
                    status=ChatTurnStatus.COMPLETE,
                    content="Checked Core state",
                    request_snapshot={"harness_profile_id": request["profile_id"]},
                )
            )
            return None, turn, SimpleNamespace(id="harness-turn")

        async def stream_turn(self, _turn_id):
            if False:
                yield None

    class Subagents:
        async def for_session(self, _session_id):
            return []

    class Work:
        available = True
        chat = SimpleNamespace(subagents=Subagents())

        async def harness_quota(self, project_id, arguments):
            assert project_id == project.id
            assert arguments["refresh"] is True
            if not self.available:
                return {"state": "unavailable", "snapshots": []}
            return {
                "state": "available",
                "snapshots": [
                    {
                        "remaining_percent": (
                            0.0 if arguments["profile_id"] == profile.id else 100.0
                        )
                    }
                ],
            }

    work = Work()

    agent = ButlerRecoveryAgent(
        store,
        provider_factory=lambda _: None,
        harness_runtime=Runtime(),
        artifact_store=None,
        work=work,
        memory=None,
        instructions_root=tmp_path,
    )
    outcome, backend, profile_id, model, turn_id = await agent.run(case)
    assert (outcome, backend.value, profile_id, model, turn_id) == (
        "Checked Core state",
        "harness",
        alternate.id,
        "test-model",
        "harness-turn",
    )
    saved_case = await store.get(ButlerRecoveryCase, case.id)
    assert saved_case.model_turn_id == "harness-turn"
    assert saved_case.model_turn_ids == ["harness-turn"]
    assert saved_case.model_profile_id == alternate.id
    assert (await agent._butler_session(project.id)).harness_profile_id == profile.id
    work.available = False
    with pytest.raises(ValueError, match="confirmed included usage or credits"):
        (
            await agent._scheduled_harness_profile(
                project.id, profile, "test-model", "max", "automatic"
            )
        )

    async def credit_only(_project_id, arguments):
        return {
            "state": "available",
            "ordinary_usage_allowed": False,
            "snapshots": [{"remaining_percent": 0.0}],
            "balances": [
                {"remaining": 2.0 if arguments["profile_id"] == profile.id else 0.0}
            ],
        }

    work.harness_quota = credit_only
    assert (
        await agent._scheduled_harness_profile(
            project.id, profile, "test-model", "max", "automatic"
        )
    ).id == profile.id


async def test_global_check_retries_once_on_another_account_after_a_usage_limit(
    tmp_path,
):
    """A Butler chat left on a spent Claude account no longer fails every check."""

    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    claude = HarnessCapabilities(models=["opus"])
    spent, spare = [
        await store.create(
            HarnessProfile(
                name=name,
                kind=HarnessKind.CLAUDE_AGENT_SDK,
                default_model="opus",
                executable="/bin/true",
                privacy={"local_only": True},
                capabilities=claude,
            )
        )
        for name in ("Spent account", "Spare account")
    ]
    butler_harness = await store.create(
        HarnessSession(
            engagement_id=project.id, harness_profile_id=spent.id, model="opus"
        )
    )
    (
        await store.create(
            ChatSession(
                engagement_id=project.id,
                title="Butler",
                backend=ChatBackend.HARNESS,
                harness_profile_id=spent.id,
                harness_session_id=butler_harness.id,
                model="opus",
                metadata={
                    "control_assistant": True,
                    "assistant_config": {
                        "backend": "harness",
                        "profile_id": spent.id,
                        "model": "opus",
                        "allow_subagents": True,
                        "subagent_harness_profile_id": spent.id,
                        "subagent_model": "opus",
                    },
                },
            )
        )
    )
    (
        await store.create(
            ButlerAuthorityPolicy(
                id=policy_id(project.id), engagement_id=project.id, authority="act"
            )
        )
    )
    worker_id = "worker-1"
    (
        await store.create(
            ButlerProjectState(
                id=project_state_id(project.id),
                engagement_id=project.id,
                lease_owner=worker_id,
                lease_expires_at=utc_now() + timedelta(minutes=10),
            )
        )
    )
    _write(tmp_path)
    recovery = ButlerRecoveryService(store, instructions_root=tmp_path)
    assert await recovery.fire_global_periodic_check(project.id, worker_id)
    assert (await recovery.capture_events(project.id, worker_id)) == 1
    case = (
        await store.find_entities(ButlerRecoveryCase, {}, engagement_id=project.id)
    )[0]
    starts: list[tuple[str, str | None]] = []
    failure = {
        "error": "Claude turn failed: You've hit your weekly limit",
        "diagnostic": {
            "reason_code": "rate_limited",
            "vendor_error_info": {"api_error_status": 429},
        },
    }

    class Runtime:
        async def _validate_harness_privacy(self, *_args, **_kwargs):
            pass

        async def create_session(self, **kwargs):
            return await store.create(
                HarnessSession(
                    engagement_id=project.id,
                    harness_profile_id=kwargs["profile_id"],
                    model=kwargs["model"],
                )
            )

        async def prepare_chat(self, **request):
            child = request["provider_subagent"]
            starts.append(
                (request["profile_id"], child["harness_profile_id"] if child else None)
            )
            limited = request["profile_id"] == spent.id
            turn = await store.create(
                ChatTurn(
                    engagement_id=project.id,
                    session_id=request["chat_session_id"],
                    backend=ChatBackend.HARNESS,
                    model=request["model"],
                    status=(
                        ChatTurnStatus.INTERRUPTED
                        if limited
                        else ChatTurnStatus.COMPLETE
                    ),
                    error=failure["error"] if limited else None,
                    content="" if limited else "Checked Core state",
                    request_snapshot={"harness_profile_id": request["profile_id"]},
                )
            )
            harness_turn = await store.create(
                HarnessTurn(
                    engagement_id=project.id,
                    harness_session_id="harness-session",
                    origin=HarnessTurnOrigin.CHAT,
                    chat_session_id=request["chat_session_id"],
                    chat_turn_id=turn.id,
                    prompt="check",
                    status=(
                        HarnessTurnStatus.INTERRUPTED
                        if limited
                        else HarnessTurnStatus.COMPLETE
                    ),
                    error=failure["error"] if limited else None,
                    metadata=(
                        {"diagnostic": failure["diagnostic"]}
                        if limited and failure["diagnostic"]
                        else {}
                    ),
                )
            )
            turn = await store.update(
                ChatTurn,
                turn.id,
                {"harness_turn_id": harness_turn.id},
                expected_revision=turn.revision,
            )
            return None, turn, harness_turn

        async def stream_turn(self, _turn_id):
            if False:
                yield None

    class Subagents:
        async def for_session(self, _session_id):
            return []

    class Work:
        chat = SimpleNamespace(subagents=Subagents())

        async def harness_quota(self, _project_id, _arguments):
            # Claude reports plan limits only inside turns: nothing is current.
            return {"state": "unavailable", "snapshots": []}

    agent = ButlerRecoveryAgent(
        store,
        provider_factory=lambda _: None,
        harness_runtime=Runtime(),
        artifact_store=None,
        work=Work(),
        memory=None,
        instructions_root=tmp_path,
    )
    outcome, backend, profile_id, model, _turn_id = await agent.run(case)
    assert (outcome, backend.value, profile_id, model) == (
        "Checked Core state",
        "harness",
        spare.id,
        "opus",
    )
    # One retry, with the children moved off the spent account too.
    assert starts == [(spent.id, spent.id), (spare.id, spare.id)]
    saved = await store.get(ButlerRecoveryCase, case.id)
    assert saved.model_profile_id == spare.id
    assert len(saved.model_turn_ids) == 2
    # The operator's Butler chat keeps the runtime they chose.
    assert (await agent._butler_session(project.id)).harness_profile_id == spent.id

    # Any other failure is reported as before, without a second turn.
    failure["error"] = "Claude turn failed: the vendor session closed"
    failure["diagnostic"] = {}
    starts.clear()
    retry_case = await store.update(
        ButlerRecoveryCase,
        case.id,
        {"investigation_session_id": None, "investigation_turn_ids": []},
        expected_revision=saved.revision,
    )
    with pytest.raises(ValueError, match="vendor session closed"):
        await agent.run(retry_case)
    assert starts == [(spent.id, spent.id)]


async def test_authenticated_oversight_reads_nebula_wide_file_status(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    app = create_app(
        store,
        auth_token="test-token",
        execution_data_root=tmp_path,
    )
    route = f"/api/v1/engagements/{project.id}/butler/overview"
    async with TestClient(app) as client:
        assert (await client.get(route)).status_code == 401
        missing = (
            await client.get(route, headers={"Authorization": "Bearer test-token"})
        ).json()["global_instructions"]
        assert missing["status"] == "missing"
        assert missing["path"] == str(tmp_path / ".butler" / "BUTLER.md")
        _write(tmp_path)
        present = (
            await client.get(route, headers={"Authorization": "Bearer test-token"})
        ).json()["global_instructions"]
        assert present["status"] == "needs_act_host"


async def test_check_interval_is_authenticated_revision_guarded_and_durable(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(store, auth_token="test-token", execution_data_root=tmp_path)
    route = "/api/v1/butler/check-interval"
    auth = {"Authorization": "Bearer test-token"}
    async with TestClient(app) as client:
        assert (await client.get(route)).status_code == 401
        default = (await client.get(route, headers=auth)).json()
        assert default == {
            "check_interval_minutes": 5,
            "revision": 0,
            "last_check_at": None,
            "next_check_at": None,
            "summary_target": {
                "configured": None,
                "effective": None,
                "unavailable": [],
                "last_fallback": None,
                "pending": 0,
                "options": [],
            },
        }
        assert (
            await client.put(
                route,
                headers=auth,
                json={"check_interval_minutes": 0, "expected_revision": 0},
            )
        ).status_code == 422
        saved = await client.put(
            route,
            headers=auth,
            json={"check_interval_minutes": 2, "expected_revision": 0},
        )
        assert saved.status_code == 200
        assert saved.json()["check_interval_minutes"] == 2
        assert saved.json()["revision"] == 1
        assert (
            await client.put(
                route,
                headers=auth,
                json={"check_interval_minutes": 10, "expected_revision": 0},
            )
        ).status_code == 409
        assert (await client.get(route, headers=auth)).json() == saved.json()
    reloaded = NebulaStore(tmp_path / "core.db")
    assert (
        await reloaded.get(ButlerGlobalState, GLOBAL_STATE_ID)
    ).check_interval_minutes == 2


pytestmark = pytest.mark.anyio


class _ProjectScopedStore:
    """A project worker's view: it reads only its own project's Engagement."""

    def __init__(self, store, project_id: str) -> None:
        self._store = store
        self._project_id = project_id

    def __getattr__(self, name):
        return getattr(self._store, name)

    async def iter_readable_entities(self, model, *args, **kwargs):
        async for item in self._store.iter_readable_entities(model, *args, **kwargs):
            if model is not Engagement or item.id == self._project_id:
                yield item


async def _set_global(store, **changes) -> None:
    state = await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    await store.update(
        ButlerGlobalState, GLOBAL_STATE_ID, changes, expected_revision=state.revision
    )


async def test_project_workers_keep_one_host_until_it_goes_silent(tmp_path):
    store, alpha, _ = await _anchored_project(tmp_path)
    beta = await store.create(Engagement(name="Beta"))
    provider = (await store.find_entities(ProviderProfile, {}))[0]
    await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(beta.id), engagement_id=beta.id, authority="act"
        )
    )
    await store.create(
        ButlerProjectState(
            id=project_state_id(beta.id),
            engagement_id=beta.id,
            lease_owner="worker-1",
            lease_expires_at=utc_now() + timedelta(minutes=10),
        )
    )
    await store.create(
        ChatSession(
            engagement_id=beta.id,
            title="Butler",
            model="fixture-model",
            provider_profile_id=provider.id,
            metadata={"control_assistant": True},
        )
    )
    workers = {
        project.id: ButlerRecoveryService(
            _ProjectScopedStore(store, project.id), instructions_root=tmp_path
        )
        for project in (alpha, beta)
    }
    # Each worker sees only itself, so each computes itself as the anchor.
    assert await workers[alpha.id]._global_anchor() == alpha.id
    assert await workers[beta.id]._global_anchor() == beta.id

    assert await workers[alpha.id].fire_global_periodic_check(alpha.id, "worker-1")
    assert (await workers[alpha.id].capture_events(alpha.id, "worker-1")) == 1
    await _rewind_clock(store)
    # The check is due, but alpha hosts it and fired recently.
    assert not await workers[beta.id].fire_global_periodic_check(beta.id, "worker-1")
    (case,) = await store.find_entities(ButlerRecoveryCase, {}, engagement_id=alpha.id)
    await _set_status(store, case.id, "resolved")
    assert await workers[alpha.id].fire_global_periodic_check(alpha.id, "worker-1")
    await _set_status(store, case.id, "investigating")

    # A long check keeps alpha the host: skipping it refreshes alpha's presence.
    long_ago = utc_now() - timedelta(minutes=20)
    await _set_global(store, last_periodic_at=long_ago, host_seen_at=long_ago)
    assert not await workers[alpha.id].fire_global_periodic_check(alpha.id, "worker-1")
    assert (await store.get(ButlerGlobalState, GLOBAL_STATE_ID)).host_seen_at > long_ago
    assert not await workers[beta.id].fire_global_periodic_check(beta.id, "worker-1")

    # Alpha's worker goes silent, so beta takes over and then keeps the role.
    await _set_global(store, last_periodic_at=long_ago, host_seen_at=long_ago)
    assert await workers[beta.id].fire_global_periodic_check(beta.id, "worker-1")
    state = await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    assert state.last_project_id == beta.id
    await _rewind_clock(store)
    for open_case in await store.find_entities(
        ButlerRecoveryCase, {}, engagement_id=alpha.id
    ):
        await _set_status(store, open_case.id, "resolved")
    assert not await workers[alpha.id].fire_global_periodic_check(alpha.id, "worker-1")
    status = await workers[alpha.id].global_status(alpha.id)
    assert status["status"] == "ready" and status["host_project_id"] == beta.id


async def test_a_worker_that_reads_every_project_uses_the_stable_anchor(tmp_path):
    store, alpha, _ = await _anchored_project(tmp_path)
    recovery = ButlerRecoveryService(store, instructions_root=tmp_path)
    # A recorded host this store can read but that no longer qualifies does not
    # hold the role until it goes silent.
    other = await store.create(Engagement(name="Retired host"))
    await store.create(
        ButlerGlobalState(
            id=GLOBAL_STATE_ID,
            last_periodic_at=utc_now() - timedelta(minutes=6),
            last_project_id=other.id,
            host_seen_at=utc_now(),
        )
    )
    assert await recovery._global_anchor() == alpha.id
    assert await recovery.fire_global_periodic_check(alpha.id, "worker-1")
