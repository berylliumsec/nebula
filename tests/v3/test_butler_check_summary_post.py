"""Finished Butler checks post one summary to the operator's main Butler chat."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from datetime import timedelta

import pytest
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_check_summary import (
    SUMMARY_KIND,
    deliver_check_summaries,
    publish_check_summary,
    publish_recent_check_summaries,
    says_no_change,
)
from nebula.v3.butler_oversight import project_state_id
from nebula.v3.butler_recovery import GLOBAL_STATE_ID, ButlerRecoveryService
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ButlerGlobalState,
    ButlerProjectState,
    ButlerRecoveryCase,
    ChatBackend,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    Engagement,
    ProviderProfile,
    utc_now,
)
from nebula.v3.storage import NebulaStore

WORKER = "worker-1"


async def _provider(store: NebulaStore) -> str:
    existing = await store.find_entity(ProviderProfile, "name", "Local")
    if existing is not None:
        return existing.id
    provider = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    return provider.id


async def _project(store: NebulaStore, name: str) -> tuple[Engagement, ChatSession]:
    project = await store.create(Engagement(name=name))
    butler = await store.create(
        ChatSession(
            engagement_id=project.id,
            title=f"{name} Butler",
            provider_profile_id=await _provider(store),
            model="model-a",
            metadata={"control_assistant": True},
        )
    )
    policy = await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="act"
        )
    )
    assert policy.revision == 1
    (
        await store.create(
            ButlerProjectState(
                id=project_state_id(project.id),
                engagement_id=project.id,
                lease_owner=WORKER,
                lease_expires_at=utc_now() + timedelta(minutes=3),
            )
        )
    )
    return project, butler


async def _anchor(store: NebulaStore, project_id: str, **values) -> None:
    (
        await store.create(
            ButlerGlobalState(id=GLOBAL_STATE_ID, last_project_id=project_id, **values)
        )
    )


async def _case(
    store: NebulaStore,
    project: Engagement,
    *,
    signal: str = "butler.global_periodic_check",
    trigger: str = "periodic",
    summary: str = "Nebula-wide Butler work check is due.",
) -> ButlerRecoveryCase:
    return await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger=trigger,  # type: ignore[arg-type]
            signal=signal,
            source_event_id=f"event-{signal}",
            source_event_sequence=1,
            authority_revision=1,
            status="observed",
            summary=summary,
        )
    )


def _agent(store: NebulaStore, parent: ChatSession, reply: str, steps: list[dict]):
    class Agent:
        async def run(self, claimed):
            child = await store.create(
                ChatSession(
                    engagement_id=claimed.engagement_id,
                    title=f"Butler check · {claimed.summary}",
                    provider_profile_id=await _provider(store),
                    model="model-a",
                    parent_session_id=parent.id,
                    metadata={
                        "control_assistant": True,
                        "butler_initiated": True,
                        "butler_recovery_case_id": claimed.id,
                    },
                )
            )
            current = await store.get(ButlerRecoveryCase, claimed.id)
            (
                await store.update(
                    ButlerRecoveryCase,
                    claimed.id,
                    {"steps": steps, "investigation_session_id": child.id},
                    expected_revision=current.revision,
                )
            )
            return reply, ChatBackend.PROVIDER, "provider", "model", None

    return Agent()


async def _run(store: NebulaStore, project_id: str, agent) -> ButlerRecoveryService:
    service = ButlerRecoveryService(store, agent)
    await service.process_project(project_id, WORKER)
    await asyncio.gather(*service._workers.values(), return_exceptions=True)
    await service.shutdown()
    return service


async def _summaries(store: NebulaStore, session_id: str) -> list[ChatMessage]:
    return [
        item
        for item in await store.list_session_entities(ChatMessage, session_id)
        if item.metadata.get("kind") == SUMMARY_KIND
    ]


READ = [{"id": "read", "operation": "nebula.global.project", "status": "applied"}]
CHANGE = [
    {"id": "read", "operation": "nebula.work.goals", "status": "applied"},
    {"id": "change", "operation": "nebula.work.create_goal", "status": "applied"},
]


async def test_global_check_posts_once_to_the_anchor_butler_chat(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, butler = await _project(store, "Anchor")
    await _anchor(store, project.id)
    case = await _case(store, project)
    reply = "Two goals advanced; one recovery is waiting for its report."

    await _run(store, project.id, _agent(store, butler, reply, READ))

    settled = await store.get(ButlerRecoveryCase, case.id)
    assert settled.status == "resolved"
    assert settled.check_summary == reply
    assert settled.summary_post is not None
    assert settled.summary_post["state"] == "queued"
    [message] = await _summaries(store, butler.id)
    assert message.role == ChatRole.ASSISTANT
    assert message.metadata["check_scope"] == "nebula"
    assert message.metadata["target_source"] == "global_anchor"
    assert message.metadata["operator_input"] is False
    assert settled.finished_at is not None
    stamp = settled.finished_at.strftime("%Y-%m-%d %H:%M UTC")
    assert f"Butler check summary · Nebula-wide · {stamp}" in message.content
    assert "run from project **Anchor**" in message.content
    link = (
        f"/projects/{project.id}/workbench?view=chat"
        f"&session={settled.investigation_session_id}"
    )
    assert f"]({link})" in message.content
    assert reply in message.content
    session = await store.get(ChatSession, butler.id)
    assert session.metadata["last_sequence"] == message.sequence
    # The check's own child chat keeps only what the check wrote.
    assert not await _summaries(store, settled.investigation_session_id or "")
    state = await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    assert state.check_summary_outbox == []


async def test_event_check_with_a_decision_reaches_the_anchor_project(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    anchor, anchor_butler = await _project(store, "Anchor")
    other, other_butler = await _project(store, "Research")
    await _anchor(store, anchor.id)
    case = await _case(
        store,
        other,
        signal="goal.blocked",
        trigger="immediate",
        summary="Main goal blocked on a missing fixture",
    )
    reply = "Created a recovery goal for the missing fixture."

    await _run(store, other.id, _agent(store, other_butler, reply, CHANGE))

    settled = await store.get(ButlerRecoveryCase, case.id)
    assert settled.status == "needs_attention"
    # The anchor project's worker owns the target chat; Research only queued it.
    assert not await _summaries(store, other_butler.id)
    assert await _summaries(store, anchor_butler.id) == []
    assert await deliver_check_summaries(store, anchor.id) == 1
    [message] = await _summaries(store, anchor_butler.id)
    assert "Butler check summary · Project ·" in message.content
    assert "project **Research** · signal `goal.blocked`" in message.content
    assert "Result: Needs your attention" in message.content
    assert reply in message.content
    assert (
        f"/projects/{other.id}/workbench?view=chat"
        f"&session={settled.investigation_session_id}"
    ) in message.content


@pytest.mark.parametrize(
    "reply",
    ["No change.", "**Outcome:** No changes needed; all goals are running."],
)
async def test_no_change_check_posts_nothing(tmp_path, reply):
    store = NebulaStore(tmp_path / "core.db")
    project, butler = await _project(store, "Anchor")
    await _anchor(store, project.id)
    case = await _case(store, project)

    await _run(store, project.id, _agent(store, butler, reply, READ))

    settled = await store.get(ButlerRecoveryCase, case.id)
    assert settled.status == "resolved"
    assert settled.summary_post is not None
    assert settled.summary_post == {
        "state": "skipped",
        "reason": "no_change",
        "at": settled.summary_post["at"],
    }
    assert await _summaries(store, butler.id) == []
    state = await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    assert state.check_summary_outbox == []


def test_no_change_text_rule_is_conservative():
    assert says_no_change("No change")
    assert says_no_change("\n## No changes\nAll quiet.")
    assert says_no_change("- Result: no change this round")
    assert not says_no_change("Changed nothing, but goal G1 needs approval.")
    assert not says_no_change("Two goals resumed. No change elsewhere.")
    assert not says_no_change("")


async def test_no_change_text_does_not_hide_an_applied_change(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, butler = await _project(store, "Anchor")
    await _anchor(store, project.id)
    await _case(store, project)

    await _run(store, project.id, _agent(store, butler, "No change.", CHANGE))

    assert len(await _summaries(store, butler.id)) == 1


async def test_retried_or_recovered_case_posts_only_once(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, butler = await _project(store, "Anchor")
    await _anchor(store, project.id)
    case = await _case(store, project)
    await _run(store, project.id, _agent(store, butler, "Resumed one goal.", READ))
    assert len(await _summaries(store, butler.id)) == 1

    # The same case is retried and finishes again.
    service = ButlerRecoveryService(store)
    current = await store.get(ButlerRecoveryCase, case.id)
    reopened = await service._update_case(
        current, status="observed", finished_at=None, next_check_at=utc_now()
    )
    await service._update_case(
        reopened, status="resolved", finished_at=utc_now(), outcome="Again"
    )
    (await publish_check_summary(store, case.id))
    (await publish_recent_check_summaries(store, project.id))
    (await deliver_check_summaries(store, project.id))
    await service.process_project(project.id, WORKER)

    assert len(await _summaries(store, butler.id)) == 1

    # A lost queue removal still cannot duplicate the fixed message id.
    state = await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    settled = await store.get(ButlerRecoveryCase, case.id)
    assert settled.summary_post is not None
    (
        await store.update(
            ButlerGlobalState,
            state.id,
            {
                "check_summary_outbox": [
                    {
                        "case_id": case.id,
                        "message_id": settled.summary_post["message_id"],
                        "content": "duplicate",
                        "metadata": {"kind": SUMMARY_KIND},
                        "candidates": [
                            {
                                "source": "global_anchor",
                                "project_id": project.id,
                                "session_id": None,
                            }
                        ],
                        "index": 0,
                        "stage_since": utc_now().isoformat(),
                        "fallbacks": [],
                    }
                ]
            },
            expected_revision=state.revision,
        )
    )
    assert await deliver_check_summaries(store, project.id) == 0
    assert len(await _summaries(store, butler.id)) == 1
    assert (
        await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    ).check_summary_outbox == []


async def test_configured_target_is_honoured_and_validated(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    anchor, anchor_butler = await _project(store, "Anchor")
    chosen, chosen_butler = await _project(store, "Operations")
    await _anchor(store, anchor.id)
    app = create_app(store, auth_token="test-token", execution_data_root=tmp_path)
    route = "/api/v1/butler/check-interval"
    auth = {"Authorization": "Bearer test-token"}
    async with TestClient(app) as client:
        current = (await client.get(route, headers=auth)).json()
        target = current["summary_target"]
        assert target["effective"]["session_id"] == anchor_butler.id
        assert target["effective"]["source"] == "global_anchor"
        assert {item["session_id"] for item in target["options"]} == {
            anchor_butler.id,
            chosen_butler.id,
        }
        refused = await client.put(
            route,
            headers=auth,
            json={
                "expected_revision": current["revision"],
                "summary_target": {
                    "project_id": anchor.id,
                    "session_id": chosen_butler.id,
                },
            },
        )
        assert refused.status_code == 422
        saved = await client.put(
            route,
            headers=auth,
            json={
                "expected_revision": current["revision"],
                "summary_target": {
                    "project_id": chosen.id,
                    "session_id": chosen_butler.id,
                },
            },
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["check_interval_minutes"] == 5
        assert body["summary_target"]["configured"] == {
            "project_id": chosen.id,
            "session_id": chosen_butler.id,
        }
        assert body["summary_target"]["effective"]["source"] == "configured"

    await _case(store, anchor)
    await _run(store, anchor.id, _agent(store, anchor_butler, "Resumed G1.", READ))
    assert await _summaries(store, anchor_butler.id) == []
    assert await deliver_check_summaries(store, chosen.id) == 1
    [message] = await _summaries(store, chosen_butler.id)
    assert message.metadata["target_source"] == "configured"
    assert message.engagement_id == chosen.id


@pytest.mark.parametrize("loss", ["archived", "deleted"])
async def test_unavailable_target_falls_back_and_is_recorded(tmp_path, loss):
    store = NebulaStore(tmp_path / "core.db")
    project, butler = await _project(store, "Anchor")
    gone = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Old Butler",
            provider_profile_id=await _provider(store),
            model="model-a",
            metadata={"control_assistant": True},
        )
    )
    await _anchor(
        store,
        project.id,
        check_summary_target_project_id=project.id,
        check_summary_target_session_id=gone.id,
    )
    if loss == "archived":
        (
            await store.update(
                ChatSession,
                gone.id,
                {"metadata": {**gone.metadata, "archived_at": utc_now().isoformat()}},
                expected_revision=gone.revision,
            )
        )
    else:
        (await store.delete(ChatSession, gone.id))
    await _case(store, project)

    await _run(store, project.id, _agent(store, butler, "Resumed G1.", READ))

    [message] = await _summaries(store, butler.id)
    assert message.metadata["target_source"] == "global_anchor"
    [fallback] = message.metadata["target_fallbacks"]
    assert fallback["source"] == "configured"
    assert fallback["session_id"] == gone.id
    assert (
        fallback["reason"]
        == f"chat {'is archived' if loss == 'archived' else 'was deleted'}"
    )
    state = await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    assert state.check_summary_last_fallback is not None
    assert state.check_summary_last_fallback["delivered_to"]["session_id"] == butler.id


async def test_stale_target_project_hands_the_summary_to_the_next_chat(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    anchor, anchor_butler = await _project(store, "Anchor")
    other, other_butler = await _project(store, "Research")
    await _anchor(store, anchor.id)
    case = await _case(store, other, signal="goal.blocked", trigger="immediate")
    await _run(store, other.id, _agent(store, other_butler, "Paused G2.", CHANGE))
    state = await store.get(ButlerGlobalState, GLOBAL_STATE_ID)
    [entry] = state.check_summary_outbox
    assert entry["case_id"] == case.id
    (
        await store.update(
            ButlerGlobalState,
            state.id,
            {
                "check_summary_outbox": [
                    {
                        **entry,
                        "stage_since": (utc_now() - timedelta(minutes=16)).isoformat(),
                    }
                ]
            },
            expected_revision=state.revision,
        )
    )

    assert await deliver_check_summaries(store, other.id) == 1
    [message] = await _summaries(store, other_butler.id)
    assert message.metadata["target_source"] == "check_project"
    assert message.metadata["target_fallbacks"][0]["project_id"] == anchor.id
    assert await deliver_check_summaries(store, anchor.id) == 0
    assert await _summaries(store, anchor_butler.id) == []


async def test_summary_starts_no_butler_turn_in_the_main_chat(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, butler = await _project(store, "Anchor")
    await _anchor(store, project.id)
    await _case(store, project)

    await _run(store, project.id, _agent(store, butler, "Resumed G1.", READ))

    [message] = await _summaries(store, butler.id)
    assert message.role == ChatRole.ASSISTANT
    assert message.metadata["author"] == "butler"
    assert "chat_turn_id" not in message.metadata
    assert await store.list_session_entities(ChatTurn, butler.id) == []
    assert all(
        item.role != ChatRole.USER
        for item in await store.list_session_entities(ChatMessage, butler.id)
    )
    # Dialogue verification waits only for replies without metadata.kind.
    assert (
        await store.find_entities(
            ChatMessage,
            {"role": ChatRole.ASSISTANT.value, "metadata.kind": None},
            engagement_id=project.id,
            session_id=butler.id,
        )
        == []
    )
    # A later pass finds nothing new to post or start.
    await _run(store, project.id, None)
    assert len(await _summaries(store, butler.id)) == 1
    assert await store.list_session_entities(ChatTurn, butler.id) == []


async def test_check_that_never_ran_posts_nothing(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, butler = await _project(store, "Anchor")
    await _anchor(store, project.id)
    case = await _case(store, project)
    service = ButlerRecoveryService(store)
    current = await store.get(ButlerRecoveryCase, case.id)
    settled = await service._update_case(
        current,
        status="needs_attention",
        outcome="No authorized Codex account has confirmed included usage.",
        finished_at=utc_now(),
    )
    assert settled.summary_post is not None
    assert settled.summary_post["reason"] == "not_run"
    assert await _summaries(store, butler.id) == []


pytestmark = pytest.mark.anyio
