"""Butler mode's chat brief and the goal's active plan step."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from nebula.v3.chat_brief import first_sentence, tool_phrase
from nebula.v3.chat_goals import GoalUpdate
from nebula.v3.domain import (
    Approval,
    ButlerAgentReport,
    ChatGoal,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    EngagementStatus,
    RiskClass,
    ToolCall,
    ToolCallOrigin,
    WorkspaceProvenanceObservation,
    utc_now,
)
from tests.v3.test_runtime_global_reads import AUTH

pytest_plugins = (
    "tests.v3.test_runtime_coordination",
    "tests.v3.test_runtime_global_reads",
)

pytestmark = pytest.mark.anyio


def anyio_backend():
    return "asyncio"


@pytest.fixture
async def project_reads(reads):
    return reads


def test_turn_line_is_derived_without_a_model():
    assert (
        first_sentence("## Result\n\nBuilt the harness. It links cleanly.")
        == "Built the harness."
    )
    assert first_sentence("```sh\nmake\n```\n\n- Fuzzed 4 inputs") == "Fuzzed 4 inputs"
    assert len(first_sentence("word " * 100) or "") == 160
    assert first_sentence("   ") is None
    counts = {"commands": 3, "edits": 1, "approvals": 0, "tools": 5}
    assert (
        tool_phrase(counts, ["src/fuzz.c"])
        == "Ran 3 commands · edited fuzz.c · used 1 tool"
    )
    assert tool_phrase({**counts, "commands": 0, "tools": 1}, ["a", "b"]) == (
        "Edited 2 files"
    )
    assert (
        tool_phrase({"commands": 0, "edits": 0, "approvals": 0, "tools": 0}, []) is None
    )


async def test_chat_brief_reads_turns_files_goal_and_attention(project_reads):
    store = project_reads.store
    project = await store.create(Engagement(name="Bounty"))
    session = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Fuzz parser",
            provider_profile_id="provider",
            model="model",
        )
    )
    goal = await store.create(
        ChatGoal(
            engagement_id=project.id,
            session_id=session.id,
            objective="Find a crash",
            completion_criteria=["A reproducible crash"],
            plan=["Build", "Fuzz", "Reproduce"],
            current_step=3,
            active_plan_index=1,
        )
    )

    async def turn(status, **fields):
        return await store.create(
            ChatTurn(
                engagement_id=project.id,
                session_id=session.id,
                goal_id=goal.id,
                provider_profile_id="provider",
                model="model",
                status=status,
                **fields,
            )
        )

    async def call(turn_id, tool_name, arguments, **fields):
        return await store.create(
            ToolCall(
                engagement_id=project.id,
                run_id=turn_id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=session.id,
                chat_turn_id=turn_id,
                tool_name=tool_name,
                risk_class=RiskClass.WORKSPACE_WRITE,
                arguments=arguments,
                **fields,
            )
        )

    answer = await store.create(
        ChatMessage(
            engagement_id=project.id,
            session_id=session.id,
            sequence=1,
            role=ChatRole.ASSISTANT,
            content="Built the harness with ASan. Next I fuzz it.",
        )
    )
    built = await turn(ChatTurnStatus.COMPLETE, final_message_id=answer.id)
    await call(built.id, "run_command", {"command": "make"})
    await call(built.id, "nebula.workspace.write", {"path": "harness/fuzz.c"})
    await store.create(
        ButlerAgentReport(
            engagement_id=project.id,
            goal_id=goal.id,
            session_id=session.id,
            turn_id=built.id,
            report_type="progress",
            source="provider_tool",
            actor_role="main_agent",
            idempotency_key="report-1",
            details={"plan_index": 0},
        )
    )
    fuzzed = await turn(ChatTurnStatus.COMPLETE)
    for index in range(3):
        await call(fuzzed.id, "run_command", {"command": f"./fuzz {index}"})
    await call(fuzzed.id, "vendor:claude:Edit", {"file_path": "harness/fuzz.c"})
    now = utc_now()
    await store.create(
        WorkspaceProvenanceObservation(
            engagement_id=project.id,
            workspace_root="/workspace",
            scope_kind="turn",
            scope_id=fuzzed.id,
            actor_id="chat",
            chat_session_id=session.id,
            chat_turn_id=fuzzed.id,
            status="complete",
            mutations=[{"path": "corpus/seed-1", "before": None, "after": {}}],
            started_at=now - timedelta(seconds=5),
            completed_at=now,
        )
    )
    await store.create(
        ButlerAgentReport(
            engagement_id=project.id,
            goal_id=goal.id,
            session_id=session.id,
            turn_id=fuzzed.id,
            report_type="progress",
            source="provider_tool",
            actor_role="main_agent",
            idempotency_key="report-2",
            details={"plan_index": 1},
        )
    )
    await turn(ChatTurnStatus.FAILED, error="Download timed out")
    approval = await store.create(
        Approval(
            engagement_id=project.id,
            run_id="chat-run",
            origin="chat",
            chat_session_id=session.id,
            risk_class=RiskClass.HIGH_IMPACT_ACTION,
            exact_request={
                "tool_name": "run_command",
                "arguments": {"command": "make install"},
            },
            policy_rationale="Installs on the host",
            requested_by="model",
        )
    )
    waiting = await turn(ChatTurnStatus.WAITING_APPROVAL, approval_id=approval.id)
    await call(
        waiting.id, "run_command", {"command": "make install"}, approval_id=approval.id
    )

    path = f"/api/v1/butler/sessions/{session.id}/brief"
    assert (await project_reads.client.get(path)).status_code == 401
    response = await project_reads.client.get(path, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "waiting"
    assert body["project"] == {"id": project.id, "name": "Bounty"}
    assert body["turn_count"] == 4 and body["subagent_count"] == 0
    assert body["cost_usd"] is None
    assert body["attention"] == {
        "kind": "approval",
        "action_id": approval.id,
        "summary": "Approve run_command: make install",
        "command": "make install",
    }
    assert body["goal"] == {
        "objective": "Find a crash",
        "completion_criteria": ["A reproducible crash"],
        "status": "draft",
        "plan": ["Build", "Fuzz", "Reproduce"],
        "active_plan_index": 1,
        "turns_used": 3,
        "blocked_reason": None,
    }
    turns = body["turns"]
    assert [item["index"] for item in turns] == [1, 2, 3, 4]
    assert body["older_before"] is None
    assert turns[0]["line"] == "Built the harness with ASan."
    assert turns[0]["counts"] == {"commands": 1, "edits": 1, "approvals": 0, "tools": 2}
    assert turns[0]["files"] == ["harness/fuzz.c"]
    assert turns[0]["finished_at"] is not None
    assert turns[1]["line"] == "Ran 3 commands · edited 2 files"
    assert turns[1]["files"] == ["harness/fuzz.c", "corpus/seed-1"]
    assert turns[2]["line"] == "Download timed out"
    assert turns[3]["counts"]["approvals"] == 1
    assert turns[3]["finished_at"] is None
    assert [item["plan_index"] for item in turns] == [0, 1, 1, 1]
    assert {item["path"] for item in body["files"]} == {
        "harness/fuzz.c",
        "corpus/seed-1",
    }
    assert all(item["last_turn_id"] for item in body["files"])

    unchanged = await project_reads.client.get(
        path, headers={**AUTH, "If-None-Match": response.headers["etag"]}
    )
    assert unchanged.status_code == 304

    older = await project_reads.client.get(path, params={"limit": 2}, headers=AUTH)
    assert [item["index"] for item in older.json()["turns"]] == [3, 4]
    assert older.json()["older_before"] == 3
    first = await project_reads.client.get(
        path, params={"limit": 2, "before": 3}, headers=AUTH
    )
    assert [item["index"] for item in first.json()["turns"]] == [1, 2]
    assert first.json()["older_before"] is None


async def test_chat_brief_hides_butler_archived_and_unknown_chats(project_reads):
    store = project_reads.store
    project = await store.create(Engagement(name="Research"))
    archived = await store.create(
        Engagement(name="Old", status=EngagementStatus.ARCHIVED)
    )

    async def chat(owner, **metadata):
        return await store.create(
            ChatSession(
                engagement_id=owner.id,
                title="Chat",
                provider_profile_id="provider",
                model="model",
                metadata=metadata,
            )
        )

    hidden = [
        await chat(project, control_assistant=True),
        await chat(project, butler_thread=True),
        await chat(project, side_chat=True),
        await chat(project, archived_at="2026-11-01T00:00:00Z"),
        await chat(archived),
    ]
    for session_id in [item.id for item in hidden] + ["missing", project.id]:
        response = await project_reads.client.get(
            f"/api/v1/butler/sessions/{session_id}/brief", headers=AUTH
        )
        assert response.status_code == 404, session_id
    idle = await chat(project)
    response = await project_reads.client.get(
        f"/api/v1/butler/sessions/{idle.id}/brief", headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "idle"
    assert body["turns"] == [] and body["files"] == []
    assert body["goal"] is None and body["attention"] is None


def test_goal_active_plan_index_must_name_a_plan_entry():
    base = {
        "engagement_id": "project",
        "session_id": "session",
        "objective": "Find a crash",
        "completion_criteria": ["Crash"],
        "plan": ["Build", "Fuzz"],
    }
    assert ChatGoal(**base).active_plan_index is None
    assert ChatGoal(**base, active_plan_index=1).active_plan_index == 1
    with pytest.raises(ValidationError):
        ChatGoal(**base, active_plan_index=2)
    with pytest.raises(ValidationError):
        GoalUpdate(
            expected_revision=1,
            objective="x",
            completion_criteria=["y"],
            plan=["One"],
            active_plan_index=1,
        )
