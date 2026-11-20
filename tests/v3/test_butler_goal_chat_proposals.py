"""Butler proposes goal chats as durable drafts the operator starts or cancels."""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import pytest

from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_capabilities import _BACKGROUND_CHANGES
from nebula.v3.control_assistant_work import _ALL_WORK_TOOLS, dispatch_work_tool
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
)
from tests.v3.async_test_support import AsyncTestClient as TestClient
from tests.v3.test_control_assistant_work import _app
from tests.v3.test_runtime_global_reads import AUTH

pytest_plugins = (
    "tests.v3.test_runtime_coordination",
    "tests.v3.test_runtime_global_reads",
)

pytestmark = pytest.mark.anyio

SPEC = {
    "title": "Triage crash logs",
    "backend": "provider",
    "model": "model-a",
    "reasoning_effort": "high",
    "objective": "Triage the crash logs into a deduplicated report",
    "completion_criteria": ["A deduplicated crash report is saved"],
    "time_budget_seconds": 3600,
    "step_budget": 20,
    "allow_subagents": True,
    "max_active_subagents": 2,
}


async def _project(store):
    project = await store.create(Engagement(name="Selected"))
    provider = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    butler = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Butler thread",
            provider_profile_id=provider.id,
            model="model-a",
            metadata={"control_assistant": True, "butler_thread": True},
        )
    )
    return project, provider, butler


def test_proposal_is_a_registered_background_change():
    assert "nebula.work.propose_goal_chat" in _BACKGROUND_CHANGES
    entry = next(
        item for item in _ALL_WORK_TOOLS if item[0] == "nebula.work.propose_goal_chat"
    )
    assert entry[3] is False
    assert {"title", "objective", "completion_criteria"} <= set(entry[2]["required"])


async def test_proposal_saves_a_draft_and_waits_for_the_operator(tmp_path):
    store, app, _ = _app(tmp_path)
    project, provider, butler = await _project(store)
    work = app.state.control_assistant_work

    receipt = await dispatch_work_tool(
        work,
        project.id,
        "nebula.work.manage",
        {
            "operation": "nebula.work.propose_goal_chat",
            "arguments": {**SPEC, "profile_id": provider.id},
        },
        caller_session_id=butler.id,
    )

    assert receipt["state"] == "proposed"
    assert receipt["operator_confirmation_required"] is True
    action = receipt["client_action"]
    assert action["kind"] == "goal_chat_proposal"
    assert action["projectId"] == project.id
    assert action["title"] == "Triage crash logs"
    assert action["objective"] == SPEC["objective"]
    assert action["completionCriteria"] == SPEC["completion_criteria"]
    assert action["runtime"] == {
        "backend": "provider",
        "profileId": provider.id,
        "model": "model-a",
        "reasoningEffort": "high",
    }
    assert action["limits"] == {
        "timeBudgetSeconds": 3600,
        "tokenBudget": None,
        "stepBudget": 20,
        "allowSubagents": True,
        "maxActiveSubagents": 2,
    }
    assert action["reportsTo"] == {
        "sessionId": butler.id,
        "title": "Butler thread",
        "kind": "butler",
    }
    assert action["goalStatus"] == "draft"
    session = await store.get(ChatSession, action["sessionId"])
    assert session.title == "Triage crash logs"
    assert session.metadata["proposed_by"] == "butler"
    assert session.metadata["butler_session_id"] == butler.id
    assert session.metadata["initial_title_state"] == "operator"
    assert "butler_initiated" not in session.metadata
    goal = await store.find_entity(ChatGoal, "session_id", session.id)
    assert goal is not None and goal.status == ChatGoalStatus.DRAFT
    assert action["goalRevision"] == goal.revision

    # The operator's Edit and Cancel use the existing goal and chat routes.
    headers = {"Authorization": "Bearer test-token"}
    async with TestClient(app) as client:
        edited = await client.patch(
            f"/api/v1/chat/sessions/{session.id}/goal",
            headers=headers,
            json={
                "expected_revision": goal.revision,
                "objective": "Triage only the newest crash logs",
                "completion_criteria": SPEC["completion_criteria"],
                "time_budget_seconds": 3600,
                "step_budget": 20,
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["status"] == "draft"
        current = await store.get(ChatSession, session.id)
        removed = await client.delete(
            f"/api/v1/chat-sessions/{session.id}",
            headers={**headers, "If-Match": str(current.revision)},
        )
        assert removed.status_code == 204, removed.text
    assert await store.find_entity(ChatGoal, "session_id", session.id) is None


async def test_delegate_authority_starts_the_proposal_at_once(tmp_path, monkeypatch):
    store, app, _ = _app(tmp_path)
    project, provider, butler = await _project(store)
    await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="delegate"
        )
    )
    started: list[str] = []

    async def dispatcher(session_id: str, _instruction: str) -> str | None:
        started.append(session_id)
        return None

    work = app.state.control_assistant_work
    # Starting the first turn needs a live model; record the dispatch instead.
    monkeypatch.setattr(work.chat, "dispatch_running_goal", dispatcher)

    receipt = await work.propose_goal_chat(
        project.id,
        {**SPEC, "profile_id": provider.id},
        caller_session_id=butler.id,
    )

    assert receipt["state"] == "started", receipt
    assert receipt["operator_confirmation_required"] is False
    assert receipt["client_action"]["kind"] == "goal_chat_started"
    session = await store.get(ChatSession, receipt["client_action"]["sessionId"])
    assert session.metadata["butler_initiated"] is True
    assert session.metadata["proposed_by"] == "butler"
    goal = await store.find_entity(ChatGoal, "session_id", session.id)
    assert goal is not None and goal.status == ChatGoalStatus.RUNNING
    assert started == [session.id]


async def test_watch_authority_leaves_the_proposal_for_the_operator(tmp_path):
    store, app, _ = _app(tmp_path)
    project, provider, _butler = await _project(store)
    await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="steer"
        )
    )
    receipt = await app.state.control_assistant_work.propose_goal_chat(
        project.id, {**SPEC, "profile_id": provider.id}
    )
    assert receipt["state"] == "proposed"
    assert receipt["client_action"]["reportsTo"] is None


async def test_board_lists_proposals_as_waiting_and_labels_butler_starts(reads):
    store = reads.store
    project = await store.create(Engagement(name="Research"))

    async def chat(title, **metadata):
        return await store.create(
            ChatSession(
                engagement_id=project.id,
                title=title,
                provider_profile_id="provider",
                model="model",
                metadata=metadata,
            )
        )

    async def goal(session, status=ChatGoalStatus.DRAFT):
        return await store.create(
            ChatGoal(
                engagement_id=project.id,
                session_id=session.id,
                objective="Triage",
                completion_criteria=["Report"],
                status=status,
            )
        )

    proposal = await chat("Triage crash logs", proposed_by="butler")
    await goal(proposal)
    cancelled = await chat("Rejected idea", proposed_by="butler")
    await goal(cancelled, ChatGoalStatus.CANCELLED)
    plain_draft = await chat("Operator draft")
    await goal(plain_draft)
    butler_started = await chat("Butler run", butler_initiated=True)
    await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=butler_started.id,
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.ROUTING,
        )
    )

    answer = await reads.client.get("/api/v1/butler/board", headers=AUTH)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["counts"] == {"waiting": 1, "working": 1, "failed": 0, "idle": 2}
    items = {item["title"]: item for item in body["items"]}
    assert list(items) == ["Triage crash logs", "Butler run"]
    waiting = items["Triage crash logs"]
    assert waiting["state"] == "waiting"
    assert waiting["attention"]["kind"] == "proposal"
    assert waiting["attention"]["action_id"] == proposal.id
    assert waiting["attention"]["summary"] == "Butler proposed this goal chat"
    assert waiting["now"] == "Butler proposed this goal chat"
    assert waiting["started_by"] is None
    assert waiting["proposed_by"] == "butler"
    assert waiting["goal"]["status"] == "draft"
    assert isinstance(waiting["goal"]["revision"], int)
    assert waiting["session_revision"] == proposal.revision
    assert items["Butler run"]["started_by"] == "butler"

    idle = await reads.client.get(
        "/api/v1/butler/board", params={"state": "idle"}, headers=AUTH
    )
    idle_ids = {item["session_id"] for item in idle.json()["items"]}
    assert idle_ids == {cancelled.id, plain_draft.id}
    assert all(item["started_by"] is None for item in idle.json()["items"])
