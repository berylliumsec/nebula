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
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessModelOptions,
    HarnessProfile,
    HarnessRuntimeOption,
    HarnessSession,
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
    assert await restarted.fire_global_periodic_check(anchor, worker_id)
    assert len((await store.replay_operation_events(project_stream_id(anchor)))) == 2


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

    class Runtime:
        def _validate_harness_privacy(self, *_args, **_kwargs):
            pass

        async def analyze_structured(self, **request):
            assert request["profile_id"] == alternate.id
            assert request["reasoning_effort"] == "max"
            assert "Check idle work." in request["prompt"]
            request["on_turn_created"](SimpleNamespace(id="harness-turn"))
            return SimpleNamespace(
                status=HarnessTurnStatus.COMPLETE,
                response="Checked Core state",
                id="harness-turn",
            )

    class Work:
        available = True

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
