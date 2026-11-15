"""#1100: Core's own bookkeeping lands inside settings and Butler writers.

A harness turn's turn-state bookkeeping, the chat page's automatic provider
capability check, Butler's oversight replay and repeated oversight passes
each save the record between the writer's read and its guarded write.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

import nebula.v3.butler_oversight as oversight
from nebula.v3.butler_improvements import ButlerImprovementService, DraftEdit
from nebula.v3.butler_oversight import ButlerOversightService, project_state_id
from nebula.v3.butler_recovery import ButlerRecoveryService
from nebula.v3.chat_goals import ChatGoalService, GoalWrite
from nebula.v3.database import Database
from nebula.v3.domain import (
    ButlerGoalWatch,
    ButlerIssueCandidate,
    ButlerIssueDestination,
    ButlerProjectState,
    ChatSession,
    Engagement,
    HarnessCapabilities,
    HarnessProfile,
    HarnessTurn,
    ProviderProfile,
)
from nebula.v3.harnesses import HarnessHealth
from nebula.v3.model_catalog import ModelDescriptor
from nebula.v3.provider_settings import (
    _verify_provider_capability,
    persist_provider_model_catalog,
)
from nebula.v3.providers import ProviderHealth
from nebula.v3.settings_assistant_tools import set_default_model
from nebula.v3.storage import NebulaStore
from tests.v3.revision_window import before_update
from tests.v3.test_butler_oversight import _start
from tests.v3.test_harness_provider_subagents import _prepare, _setup
from tests.v3.test_provider_verification import ProbeProvider, _profile

pytestmark = pytest.mark.anyio


async def test_harness_health_is_saved_beside_a_completed_turn(tmp_path):
    store, project, harness, chat, adapter, runtime = await _setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(profile, credential_store):
        entered.set()
        await release.wait()
        return HarnessHealth(
            profile_id=profile.id,
            healthy=True,
            kind=profile.kind,
            capabilities=HarnessCapabilities(models=["gpt-test"]),
            authentication_state="verified",
        )

    adapter.probe = probe
    check = asyncio.create_task(runtime.health(harness.id))
    await asyncio.wait_for(entered.wait(), 5)
    # A chat turn on the same harness completes and saves its turn state.
    _, _, turn = await _prepare(runtime, project, harness, "Hi.", setting=None)
    await (await runtime.start_chat_turn(turn.id))
    mid = await store.get(HarnessProfile, harness.id)
    assert (await store.get(HarnessTurn, turn.id)).status.value == "complete"
    release.set()
    try:
        result = await check
        after = await store.get(HarnessProfile, harness.id)
        assert result.healthy
        assert after.revision > mid.revision
        assert after.capabilities.checked_at is not None
        assert after.capabilities.authentication_state == "verified"
        # The turn's bookkeeping is kept beside the probe's findings.
        assert after.capabilities.turn_state == mid.capabilities.turn_state
        assert (
            after.capabilities.last_successful_turn_at
            == mid.capabilities.last_successful_turn_at
        )
        assert result.turn_state == mid.capabilities.turn_state
    finally:
        await chat.shutdown()


async def test_provider_catalog_is_saved_beside_a_capability_check(tmp_path):
    store = NebulaStore(tmp_path / "health.db")
    await _profile(store)
    gate = asyncio.Event()

    async def provider_health():
        profile = await store.get(ProviderProfile, "provider-a")
        await gate.wait()  # the network health and catalog call
        health = ProviderHealth(
            provider_id=profile.id,
            healthy=True,
            models=["model-a", "model-b"],
            model_descriptors=[
                ModelDescriptor(id="model-a", name="A", context_window=32768)
            ],
        )
        return await persist_provider_model_catalog(store, profile, health)

    task = asyncio.create_task(provider_health())
    await asyncio.sleep(0.05)
    fresh = await store.get(ProviderProfile, "provider-a")
    await _verify_provider_capability(
        store, fresh, "model-a", lambda _: ProbeProvider(valid=True)
    )
    verified = await store.get(ProviderProfile, "provider-a")
    gate.set()
    saved = await task
    after = await store.get(ProviderProfile, "provider-a")
    assert saved.revision == after.revision
    assert [item["id"] for item in after.metadata["model_descriptors"]] == ["model-a"]
    assert after.capability_verifications == verified.capability_verifications
    assert after.capabilities.tool_calling == verified.capabilities.tool_calling


async def test_assistant_default_model_survives_turn_bookkeeping(tmp_path):
    store = NebulaStore(tmp_path / "harness.db")
    harness = await store.create(
        HarnessProfile(
            id="codex",
            name="Codex",
            kind="codex_app_server",
            enabled=True,
            executable="/bin/true",
            capabilities={"models": ["gpt-a", "gpt-b"]},
        )
    )

    async def turn_bookkeeping():
        latest = await store.get(HarnessProfile, harness.id)
        await store.update(
            HarnessProfile,
            harness.id,
            {
                "capabilities": latest.capabilities.model_copy(
                    update={"turn_state": "verified"}
                )
            },
            expected_revision=latest.revision,
        )

    window = before_update(
        store,
        "set_default_model",
        turn_bookkeeping,
        model=HarnessProfile,
        direct=False,
    )
    result = await set_default_model(
        store, {"kind": "harness", "profile_id": harness.id, "model": "gpt-b"}
    )
    assert window.fired
    after = await store.get(HarnessProfile, harness.id)
    assert result["saved"] is True
    assert after.default_model == "gpt-b"
    assert after.capabilities.turn_state == "verified"
    with pytest.raises(ValueError, match="advertised models"):
        await set_default_model(
            store, {"kind": "harness", "profile_id": harness.id, "model": "gpt-z"}
        )


async def _issue_setup(tmp_path):
    store = NebulaStore(tmp_path / "issues.db")
    project = await store.create(Engagement(name="Issues"))
    destination = await store.create(
        ButlerIssueDestination(
            engagement_id=project.id,
            name="Repo",
            repository="owner/repo",
            publisher="github",
            credential_ref="cred",
            review_profile_id="codex",
        )
    )
    candidate = await store.create(
        ButlerIssueCandidate(
            engagement_id=project.id,
            destination_id=destination.id,
            source_kind="incident",
            source_id="i1",
            dedup_key="k1",
            problem_key="key-one",
            title="A defect title",
            body="x" * 40,
            reproduction="y" * 30,
            confidence="observed",
            status="held",
            reason="automatic_publishing_not_granted",
        )
    )
    return store, project, candidate, ButlerImprovementService(store, None, None, None)


async def test_oversight_passes_leave_a_held_draft_unchanged(tmp_path):
    store, project, candidate, service = await _issue_setup(tmp_path)
    for _ in range(3):
        await service.process_project(project.id)
    after = await store.get(ButlerIssueCandidate, candidate.id)
    assert after.revision == candidate.revision
    edited = await service.edit_candidate(
        project.id,
        candidate.id,
        DraftEdit(
            title="A better defect title",
            body="z" * 40,
            reproduction="w" * 30,
            expected_revision=candidate.revision,
        ),
    )
    assert edited.title == "A better defect title"


async def test_oversight_pass_survives_an_operator_edit(tmp_path):
    store, project, candidate, service = await _issue_setup(tmp_path)

    async def operator_edit():
        latest = await store.get(ButlerIssueCandidate, candidate.id)
        await service.edit_candidate(
            project.id,
            candidate.id,
            DraftEdit(
                title="A better defect title",
                body="z" * 40,
                reproduction="w" * 30,
                expected_revision=latest.revision,
            ),
        )

    original_publish = service.publish
    fired = []

    async def publish(stale):
        # The operator saves an edit after the pass read the draft.
        if not fired:
            fired.append(stale.id)
            await operator_edit()
        return await original_publish(stale)

    service.publish = publish
    await service.process_project(project.id)
    after = await store.get(ButlerIssueCandidate, candidate.id)
    assert fired == [candidate.id]
    assert after.title == "A better defect title"
    assert after.status == "reviewed"
    # The next pass decides on the edited draft.
    await service.process_project(project.id)
    again = await store.get(ButlerIssueCandidate, candidate.id)
    assert again.title == "A better defect title"
    assert (again.status, again.reason) == ("held", "automatic_publishing_not_granted")


@pytest.fixture
def postgresql_url():
    configured = os.getenv("NEBULA_TEST_POSTGRES_URL")
    if not configured:
        pytest.skip("NEBULA_TEST_POSTGRES_URL is required for the PostgreSQL race")
    url = make_url(Database._normalize_sync_url(configured))
    schema = "nebula_rv1100_" + uuid4().hex[:12]
    engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=10)
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine.dispose()
    scoped = url.update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        yield scoped.render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


async def test_investigation_heartbeat_survives_oversight_replay_on_postgresql(
    postgresql_url, monkeypatch
):
    for name, value in {
        "ADMIN_RESERVE": "5",
        "OTHER_CONNECTIONS": "12",
        "PEAK_PROCESSES": "6",
        "POOL_SIZE": "4",
    }.items():
        monkeypatch.setenv("NEBULA_V3_DB_" + name, value)
    store = NebulaStore(postgresql_url)
    await store.create(Engagement(id="project", name="project"))
    await store.create(
        ProviderProfile(
            id="provider-project", name="Provider", provider_type="openrouter"
        )
    )
    await store.create(
        ChatSession(
            id="session-project",
            engagement_id="project",
            title="Goal chat",
            provider_profile_id="provider-project",
            model="model",
        )
    )
    goals = ChatGoalService(store)
    running = await _start(goals)
    await goals.write(
        "session-project",
        GoalWrite(expected_revision=running.revision, action="cancel"),
    )
    worker = ButlerOversightService(store)
    recovery = ButlerRecoveryService(store)
    original = oversight._entity_in_transaction
    outcome: dict = {}

    async def heartbeat():
        try:
            outcome["heartbeat"] = await recovery._renew_project_lease(
                "project", worker.worker_id
            )
        except Exception as exc:  # noqa: BLE001 - the outcome is asserted
            outcome["heartbeat"] = exc

    async def hooked(transaction, model, entity_id):
        if model is ButlerGoalWatch and "task" not in outcome:
            # Replay renewed the lease and is processing its page when an
            # investigation's heartbeat fires.
            outcome["task"] = asyncio.create_task(heartbeat())
            await asyncio.sleep(0.5)
        return await original(transaction, model, entity_id)

    monkeypatch.setattr(oversight, "_entity_in_transaction", hooked)
    try:
        await worker.replay_project("project")
        await outcome["task"]
        state = await store.get(ButlerProjectState, project_state_id("project"))
        assert outcome["heartbeat"] is True
        assert state.lease_owner == worker.worker_id
    finally:
        await store.database.dispose()
