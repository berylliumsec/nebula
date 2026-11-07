"""Concurrent recovery workers and durable, case-scoped delegation."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_butler_recovery import _setup, _failure
from tests.v3.test_chat_subagents import (
    RoutedProvider,
    _setup as chat_setup,
    _call,
    _finish,
    _response,
)
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_recovery import ButlerRecoveryAgent, ButlerRecoveryService
from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_oversight import project_state_id
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ButlerProjectState,
    ButlerRecoveryCase,
    ChatBackend,
    ChatSession,
    ChatTurn,
    ChatSubagent,
    ChatSubagentStatus,
    utc_now,
)
from nebula.v3.runtime_platform import RuntimePlatform

pytestmark = pytest.mark.anyio


async def _case(store, project, policy, index, **changes):
    return await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="immediate",
            signal="goal.blocked",
            source_event_id=f"event-{index}",
            source_event_sequence=index + 1,
            authority_revision=policy.revision,
            summary=f"Case {index}",
            **changes,
        )
    )


async def _until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_due_cases_start_together_and_new_arrivals_do_not_wait(tmp_path):
    store, project, _, _, policy, owner = await _setup(tmp_path)
    gate = asyncio.Event()
    started = set()

    class Agent:
        async def run(self, case):
            started.add(case.id)
            await gate.wait()
            if case.summary == "Case 0":
                raise ValueError("synthetic failure")
            return "checked", ChatBackend.PROVIDER, "provider", "model", None

    service = ButlerRecoveryService(store, Agent())
    cases = [await _case(store, project, policy, i) for i in range(3)]
    try:
        await service.process_project(project.id, owner)
        await _until(lambda: len(started) == 3)
        await service.process_project(project.id, owner)
        assert len(started) == 3
        late = await _case(store, project, policy, 3)
        await service.process_project(project.id, owner)
        await _until(lambda: late.id in started)
        # A live worker older than the legacy crash threshold is not abandoned.
        current = await store.get(ButlerRecoveryCase, cases[0].id)
        await store.update(
            ButlerRecoveryCase,
            current.id,
            {"started_at": utc_now() - timedelta(minutes=10)},
            expected_revision=current.revision,
        )
        assert await service.recover_interrupted_cases(project.id) == 0
        gate.set()
        await asyncio.gather(*service._workers.values())
        assert (
            await store.get(ButlerRecoveryCase, cases[0].id)
        ).status == "needs_attention"
        assert all(
            [(await store.get(ButlerRecoveryCase, c.id)).finished_at for c in cases]
        )
    finally:
        await service.shutdown()


async def test_deferred_first_page_does_not_hide_due_cases(tmp_path):
    store, project, _, _, policy, owner = await _setup(tmp_path)
    for i in range(25):
        await _case(
            store, project, policy, i, next_check_at=utc_now() + timedelta(hours=1)
        )
    due = await _case(store, project, policy, 25)
    service = ButlerRecoveryService(store)
    assert (await service._claim_case(project.id, owner)).id == due.id
    assert await service._claim_case(project.id, owner) is None


@pytest.mark.parametrize("revoke", [False, True])
async def test_shared_heartbeat_cancels_all_owned_workers(
    tmp_path, monkeypatch, revoke
):
    import nebula.v3.butler_recovery as recovery

    store, project, _, _, policy, owner = await _setup(tmp_path)
    started, cancelled = set(), set()

    class Agent:
        async def run(self, case):
            started.add(case.id)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(case.id)

    monkeypatch.setattr(recovery, "LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    service = ButlerRecoveryService(store, Agent())
    for i in range(2):
        await _case(store, project, policy, i)
    try:
        await service.process_project(project.id, owner)
        await _until(lambda: len(started) == 2)
        if revoke:
            await store.update(
                ButlerAuthorityPolicy,
                policy.id,
                {"authority": "watch"},
                expected_revision=policy.revision,
            )
        else:
            from nebula.v3.butler_oversight import _entity_in_transaction

            async with store.transaction(immediate=True) as transaction:
                state = await _entity_in_transaction(
                    transaction, ButlerProjectState, project_state_id(project.id)
                )
                await transaction.update(
                    ButlerProjectState,
                    state.id,
                    {"lease_owner": "other"},
                    expected_revision=state.revision,
                )
        await _until(lambda: len(cancelled) == 2)
    finally:
        await service.shutdown()


@pytest.mark.parametrize("cancel", [False, True])
async def test_background_provider_uses_saved_delegation_and_isolated_parent(
    tmp_path, cancel
):
    provider = RoutedProvider(
        parent=[
            _call(
                "spawn",
                "start_subagent",
                task="Check the synthetic case",
                name="Investigator",
                context=None,
            ),
            _call("wait", "wait_subagents", subagent_ids=None, mode=None),
            _finish("done"),
            _response(text="Investigated with a child"),
        ],
        child=[_response(text="Evidence checked")],
    )
    if cancel:
        provider.child_gate = asyncio.Event()
    store, project, profile, chat = await chat_setup(tmp_path, provider)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    platform = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    work = SimpleNamespace(chat=chat)
    platform.bind_control_assistant_work(work)
    chat.tool_platform = platform
    chat.workspace_resolver = lambda _: tmp_path
    parent = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Butler",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={
                "control_assistant": True,
                "allow_subagents": True,
                "max_active_subagents": 2,
            },
        )
    )
    source = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=parent.id,
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="act"
        )
    )
    await store.create(
        ButlerProjectState(
            id=project_state_id(project.id),
            engagement_id=project.id,
            lease_owner="worker",
            lease_expires_at=utc_now() + timedelta(minutes=3),
        )
    )
    await _failure(store, project, parent, source)
    service = ButlerRecoveryService(store)
    await service.capture_events(project.id, "worker")
    (case,) = await store.find_entities(
        ButlerRecoveryCase, {}, engagement_id=project.id
    )
    agent = ButlerRecoveryAgent(
        store,
        provider_factory=lambda _: provider,
        harness_runtime=None,
        artifact_store=artifacts,
        work=work,
        memory=None,
    )
    try:
        task = asyncio.create_task(agent.run(case))
        if cancel:
            await _until(lambda: bool(provider.child_requests))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            async with asyncio.timeout(15):
                result = await task
        saved = await store.get(ButlerRecoveryCase, case.id)
        assert saved.investigation_session_id != parent.id
        assert len(saved.investigation_turn_ids) == 1
        investigation = await store.get(ChatTurn, saved.investigation_turn_ids[0])
        assert investigation.request_snapshot["butler_recovery_case_id"] == case.id
        assert investigation.request_snapshot["max_active_subagents"] == 2
        (child,) = await store.find_entities(ChatSubagent, {}, engagement_id=project.id)
        assert child.parent_session_id == saved.investigation_session_id
        assert child.status == (
            ChatSubagentStatus.STOPPED if cancel else ChatSubagentStatus.COMPLETED
        )
        if not cancel:
            assert result[1] == ChatBackend.PROVIDER
        assert "start_subagent" in {t.name for t in provider.parent_requests[0].tools}
        assert not (await chat.session_messages(parent.id))
    finally:
        await chat.shutdown()


@pytest.mark.parametrize("parent_backend", ["provider", "harness"])
@pytest.mark.parametrize("child_backend", ["provider", "harness"])
@pytest.mark.parametrize("enabled", [True, False])
async def test_background_runtime_matrix(
    tmp_path, parent_backend, child_backend, enabled
):
    from tests.v3.test_harness_provider_subagents import (
        _setup as harness_setup,
        _payload,
    )
    from nebula.v3.domain import HarnessProfile, HarnessSession

    provider = RoutedProvider(
        parent=[
            _call(
                "spawn",
                "start_subagent",
                task="Recovery child task",
                name="Inspector",
                context=None,
            ),
            _call("wait", "wait_subagents", subagent_ids=None, mode=None),
            _finish("done"),
            _response(text="Evidence read"),
        ],
        child=[_response(text="Recovery child evidence")],
    )
    store, project, harness, chat, adapter, runtime = await harness_setup(
        tmp_path, providers={"provider": provider}
    )
    harness = await store.update(
        HarnessProfile,
        harness.id,
        {
            "capabilities": {
                **harness.capabilities.model_dump(mode="json"),
                "models": ["gpt-test"],
            }
        },
        expected_revision=harness.revision,
    )
    work = SimpleNamespace(chat=chat)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    platform = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    platform.bind_control_assistant_work(work)
    chat.tool_platform = platform
    runtime.bind_control_assistant_work(work)
    if not enabled:
        provider.parent = [
            _finish("done"),
            _response(text="Checked without delegation"),
        ]
    settings = {
        "allow_subagents": enabled,
        "max_active_subagents": 2,
        "subagent_provider_id": "provider" if child_backend == "provider" else None,
        "subagent_harness_profile_id": harness.id
        if child_backend == "harness"
        else None,
        "subagent_model": "model-a" if child_backend == "provider" else "gpt-test",
    }
    parent_harness = (
        await runtime.create_session(
            engagement_id=project.id,
            profile_id=harness.id,
            model="gpt-test",
            control_assistant=True,
        )
        if parent_backend == "harness"
        else None
    )
    parent = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Butler",
            backend=parent_backend,
            harness_session_id=parent_harness.id if parent_harness else None,
            provider_profile_id="provider" if parent_backend == "provider" else None,
            harness_profile_id=harness.id if parent_backend == "harness" else None,
            model="model-a" if parent_backend == "provider" else "gpt-test",
            metadata={"control_assistant": True, "assistant_config": settings},
        )
    )
    source = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=parent.id,
            backend=parent_backend,
            provider_profile_id="provider" if parent_backend == "provider" else None,
            model=parent.model,
        )
    )
    await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="act"
        )
    )
    await store.create(
        ButlerProjectState(
            id=project_state_id(project.id),
            engagement_id=project.id,
            lease_owner="worker",
            lease_expires_at=utc_now() + timedelta(minutes=3),
        )
    )
    await _failure(store, project, parent, source)
    service = ButlerRecoveryService(store)
    await service.capture_events(project.id, "worker")
    case = await service._claim_case(project.id, "worker")
    assert case is not None

    async def script(connection, prompt):
        if not enabled:
            catalog = await runtime._gateway_catalog(connection.request.session)
            assert not any(
                item["name"] == "subagent.start" for item in catalog["tools"]
            )
            return "Checked without delegation"
        if "Recovery child task" in prompt:
            return "Recovery child evidence"
        result = _payload(
            await connection.call(
                "subagent.start", task="Recovery child task", name="Inspector"
            )
        )
        assert "subagent_id" in result, result
        result = _payload(await connection.call("subagent.wait"))
        assert result["subagents"][0]["report"] == "Recovery child evidence", result
        return "Evidence read"

    adapter.script = script
    agent = ButlerRecoveryAgent(
        store,
        provider_factory=lambda _: provider,
        harness_runtime=runtime,
        artifact_store=artifacts,
        work=work,
        memory=None,
    )
    try:
        async with asyncio.timeout(20):
            result = await agent.run(case)
        assert result[1].value == parent_backend
        if not enabled:
            assert not await store.find_entities(
                ChatSubagent, {}, engagement_id=project.id
            )
            if parent_backend == "provider":
                assert not any(
                    t.name == "start_subagent"
                    for t in provider.parent_requests[0].tools
                )
            return
        (child,) = await store.find_entities(ChatSubagent, {}, engagement_id=project.id)
        assert child.status == ChatSubagentStatus.COMPLETED
        child_session = await store.get(ChatSession, child.child_session_id)
        assert child_session.backend.value == child_backend
        saved = await store.get(ButlerRecoveryCase, case.id)
        assert child.parent_session_id == saved.investigation_session_id
        turn = await store.get(ChatTurn, saved.investigation_turn_ids[-1])
        assert turn.request_snapshot["allow_subagents"] is True
        assert child.reported_at is not None
        if parent_backend == "harness":
            hs = await store.get(
                HarnessSession, turn.request_snapshot["harness_session_id"]
            )
            from nebula.v3.domain import HarnessNativeCapabilities

            assert hs.metadata[
                "native_capabilities"
            ] == HarnessNativeCapabilities().model_dump(mode="json")
            assert hs.metadata["command_runtime_enabled"] is False
    finally:
        await chat.shutdown()
        await runtime.shutdown()


@pytest.mark.parametrize("link_recorded", [True, False])
async def test_restart_reattaches_to_existing_investigation_instead_of_starting_again(
    tmp_path,
    link_recorded,
):
    store, project, parent, _, policy, owner = await _setup(tmp_path)
    saved_turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=parent.id,
            provider_profile_id=parent.provider_profile_id,
            model="model-a",
            status="complete",
        )
    )
    case = await _case(
        store,
        project,
        policy,
        0,
        status="investigating",
        investigation_session_id=parent.id,
        investigation_turn_ids=[saved_turn.id] if link_recorded else [],
        started_at=utc_now() - timedelta(minutes=5),
    )
    await store.update(
        ChatTurn,
        saved_turn.id,
        {"request_snapshot": {"butler_recovery_case_id": case.id}},
        expected_revision=saved_turn.revision,
    )
    resumed = []

    class Agent:
        async def run(self, case):
            raise AssertionError("A durable investigation must not be started again")

        async def resume(self, case):
            resumed.append(case.investigation_turn_ids[-1])
            return (
                "Recovered saved result",
                ChatBackend.PROVIDER,
                "provider",
                "model-a",
                None,
            )

    service = ButlerRecoveryService(store, Agent())
    try:
        await service.process_project(project.id, owner)
        await asyncio.gather(*service._workers.values())
        assert resumed == [saved_turn.id]
        assert (await store.get(ButlerRecoveryCase, case.id)).finished_at is not None
    finally:
        await service.shutdown()


async def test_investigation_failure_does_not_recursively_spawn_more_cases(tmp_path):
    store, project, parent, turn, policy, owner = await _setup(tmp_path)
    case = await _case(store, project, policy, 0)
    parent = await store.update(
        ChatSession,
        parent.id,
        {"metadata": {"butler_recovery_case_id": case.id}},
        expected_revision=parent.revision,
    )
    await _failure(store, project, parent, turn)
    service = ButlerRecoveryService(store)
    await service.capture_events(project.id, owner)
    cases = await store.find_entities(ButlerRecoveryCase, {}, engagement_id=project.id)
    assert [item.id for item in cases] == [case.id]


async def test_restart_does_not_reuse_a_completed_turn_from_an_earlier_round(tmp_path):
    store, project, parent, _, policy, owner = await _setup(tmp_path)
    old = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=parent.id,
            provider_profile_id=parent.provider_profile_id,
            model="model-a",
            status="complete",
            created_at=utc_now() - timedelta(minutes=5),
        )
    )
    case = await _case(
        store,
        project,
        policy,
        0,
        status="investigating",
        investigation_session_id=parent.id,
        investigation_turn_ids=[old.id],
        started_at=utc_now() - timedelta(minutes=2),
    )
    started = []

    class Agent:
        async def run(self, item):
            started.append(item.id)
            return (
                "New round checked",
                ChatBackend.PROVIDER,
                "provider",
                "model-a",
                None,
            )

        async def resume(self, item):
            raise AssertionError("The old round must not resolve the new investigation")

    service = ButlerRecoveryService(store, Agent())
    try:
        await service.process_project(project.id, owner)
        await asyncio.gather(*service._workers.values())
        assert started == [case.id]
    finally:
        await service.shutdown()


async def test_live_cases_do_not_hide_abandoned_investigations(tmp_path):
    store, project, _, _, policy, _ = await _setup(tmp_path)
    for index in range(21):
        await _case(
            store, project, policy, index, status="investigating", started_at=utc_now()
        )
    abandoned = await _case(
        store,
        project,
        policy,
        22,
        status="investigating",
        started_at=utc_now() - timedelta(minutes=5),
    )
    service = ButlerRecoveryService(store)
    assert await service.recover_interrupted_cases(project.id) == 1
    assert (await store.get(ButlerRecoveryCase, abandoned.id)).status == "observed"
