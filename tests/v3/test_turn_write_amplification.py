"""Per-step durable writes of a provider tool turn, and the records they touch.

A routing step used to rewrite the whole turn row, request snapshot and all,
about three times, and every goal charge rewrote the goal's attached skill
instructions. These tests pin the smaller write set, the snapshot parts that
replace the inline values, the single-commit answer, and the off-loop,
bounded workspace provenance around turn hooks.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from fastapi.testclient import TestClient

from nebula.v3 import chat as chat_module
from nebula.v3 import workspace_provenance
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import (
    ChatCompletionRequest,
    ChatRequestMessage,
    ChatService,
    PreparedChat,
)
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.chat_snapshot_parts import (
    SKILL_PART_KEY,
    SNAPSHOT_PARTS_KEY,
    resolve_request_snapshot,
    split_request_snapshot,
    stage_snapshot_parts,
)
from nebula.v3.chat_turn_ledger import ChatTurnLedger
from nebula.v3.database import EntityRow
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatSnapshotPart,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
    WorkspaceProvenanceObservation,
    utc_now,
)
from nebula.v3.providers import ModelMessage, ModelRequest, ToolCall
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.skill_catalog import SkillSnapshot
from nebula.v3.storage import CorruptRecordError, NebulaStore, StoreTransaction
from nebula.v3.tools import ParallelismPolicy, ToolExecutionResult, ToolSpec
from tests.v3.test_chat import FakeProvider, _profile, _write_native_hook
from tests.v3.test_chat_tool_loop import ScriptedProvider, _response

CATALOG = [{"id": f"mcp-{index}", "tools": "c" * 1_500} for index in range(100)]
MODEL_REQUEST = {
    "model": "model-a",
    "messages": [{"role": "user", "content": "q" * 45_000}],
}


def _skills(count: int = 2) -> list[SkillSnapshot]:
    return [
        SkillSnapshot(
            name=f"skill-{index}",
            path=f"/skills/skill-{index}/SKILL.md",
            source="project",
            root="/skills",
            sha256=f"{index}" * 64,
            instructions="Follow the skill. " * 800,
        )
        for index in range(count)
    ]


class _Broker:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, invocation, scope, *, approval=None):
        del scope, approval
        self.calls += 1
        return ToolExecutionResult(
            output={"value": invocation.arguments["value"], "blob": "r" * 1_000}
        )


def _conversation(store: NebulaStore, session_id: str = "session") -> ChatSession:
    if not store.list_entities(Engagement):
        store.create(Engagement(id="project", name="Writes"))
        store.create(
            ProviderProfile(
                id="provider",
                name="Provider",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a"],
                capabilities={"streaming": True, "tool_calling": True},
            )
        )
    return store.create(
        ChatSession(
            id=session_id,
            engagement_id="project",
            title="Writes",
            provider_profile_id="provider",
            model="model-a",
            metadata={
                "message_count": 1,
                "last_sequence": 1,
                "initial_title_state": "generated",
            },
        )
    )


def _tool_turn(tmp_path: Path, *, steps: int, prose: bool, thought: str = ""):
    """A goal tool turn persisted as prepare_async persists one."""

    store = NebulaStore(tmp_path / f"writes-{prose}-{len(thought)}.db")
    session = _conversation(store)
    user = store.create(
        ChatMessage(
            id="user-message",
            engagement_id="project",
            session_id=session.id,
            sequence=1,
            role=ChatRole.USER,
            content="Read every value.",
        )
    )
    goal = store.create(
        ChatGoal(
            id="goal",
            engagement_id="project",
            session_id=session.id,
            objective="Read every value.",
            completion_criteria=["done"],
            status=ChatGoalStatus.RUNNING,
            active_since=utc_now(),
        )
    )
    ChatGoalService(store).replace_skills(
        session.id, expected_revision=goal.revision, snapshots=_skills()
    )
    turn = ChatTurn(
        id="turn",
        engagement_id="project",
        session_id=session.id,
        goal_id=goal.id,
        provider_profile_id="provider",
        model="model-a",
        status=ChatTurnStatus.ROUTING,
        queued_at=utc_now(),
        tools_enabled=True,
        max_tool_calls=steps + 5,
        request_snapshot={
            "model_request": MODEL_REQUEST,
            "mcp_catalog_snapshot": CATALOG,
            "mcp_snapshot": CATALOG[:8],
            "include_oci_tools": True,
        },
    )
    spec = ToolSpec(
        name="safe_read",
        description="Return one bounded value.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        parallelism=ParallelismPolicy.SERIAL,
    )
    provider = ScriptedProvider(
        [
            _response(
                calls=[
                    ToolCall(
                        id=f"call-{index}",
                        name="safe_read",
                        arguments={"value": str(index)},
                    )
                ],
                reasoning=f"Step {index} needs value {index}. {thought}",
                text=f"Reading value {index}." if prose else "",
            )
            for index in range(steps)
        ]
        + [_response(text="Done.")]
    )
    broker = _Broker()
    prepared = PreparedChat(
        provider=provider,
        provider_profile=store.get(ProviderProfile, "provider"),
        model_request=ModelRequest(
            model="model-a", messages=[ModelMessage(role="user", content=user.content)]
        ),
        resolved_model="model-a",
        citations=[],
        engagement_id="project",
        session=session,
        pending_session=None,
        stored_messages=[user],
        new_messages=[ChatRequestMessage(role=ChatRole.USER, content="Go on.")],
        tools_enabled=True,
        tool_components=RuntimeToolComponents(
            broker=broker,
            scope=ScopePolicy(engagement_id="project"),
            workspace=tmp_path,
            specs={spec.name: spec},
            runtime_digest="writes",
        ),
        turn=turn,
    )
    service = ChatService(store, worker_id="worker")
    service._persist_turn_inputs(prepared)
    return store, service, prepared, broker


def _measure(monkeypatch, store, service, prepared):
    writes: list[tuple[str, int, tuple[str, ...]]] = []
    original_update = StoreTransaction.update

    def recording_update(self, model, entity_id, changes, *, expected_revision=None):
        result = original_update(
            self, model, entity_id, changes, expected_revision=expected_revision
        )
        writes.append(
            (
                model.__name__,
                len(json.dumps(result.model_dump(mode="json"))),
                tuple(sorted(changes)),
            )
        )
        return result

    fetched: list[int] = []
    original_rows = ChatTurnLedger._rows_after

    def recording_rows(self, turn_id, sequence):
        rows = original_rows(self, turn_id, sequence)
        fetched.append(len(rows))
        return rows

    monkeypatch.setattr(StoreTransaction, "update", recording_update)
    monkeypatch.setattr(ChatTurnLedger, "_rows_after", recording_rows)
    completion = asyncio.run(service.complete(prepared))
    return completion, writes, fetched


@pytest.mark.parametrize("prose", [False, True])
def test_routing_steps_rewrite_only_the_turn_and_goal_fields_that_change(
    tmp_path, monkeypatch, prose
):
    steps = 24
    store, service, prepared, broker = _tool_turn(tmp_path, steps=steps, prose=prose)

    completion, writes, fetched = _measure(monkeypatch, store, service, prepared)

    # The answer and the tool history are what they always were.
    assert completion.message.content.endswith("Done.")
    final = store.get(ChatTurn, "turn")
    assert final.status == ChatTurnStatus.COMPLETE
    assert final.final_message_id == completion.message.id
    history = service._turn_history(final)
    assert [entry["status"] for entry in history] == ["complete"] * steps
    assert broker.calls == steps
    if prose:
        assert "Reading value 23." in final.content

    # The write-once request values are stored once, beside the turn.
    with store.database.session() as session:
        row = session.get(EntityRow, "turn")
        assert row is not None
        stored_snapshot = row.payload["request_snapshot"]
    assert "mcp_catalog_snapshot" not in stored_snapshot
    assert "model_request" not in stored_snapshot
    assert set(stored_snapshot[SNAPSHOT_PARTS_KEY]) == {
        "model_request",
        "mcp_catalog_snapshot",
        "mcp_snapshot",
    }
    resolved = resolve_request_snapshot(
        store, final.request_snapshot, session_id=final.session_id
    )
    assert resolved["mcp_catalog_snapshot"] == CATALOG
    assert resolved["model_request"] == MODEL_REQUEST

    turn_writes = [item for item in writes if item[0] == "ChatTurn"]
    goal_writes = [item for item in writes if item[0] == "ChatGoal"]
    # Before, each turn write carried the ~200 KB snapshot and each goal
    # charge its ~30 KB of skill instructions.
    assert max(size for _, size, _ in turn_writes) < 16 * 1024
    assert max(size for _, size, _ in goal_writes) < 4 * 1024
    # Prose beside a tool call rides on the step's usage write.
    assert not any(changes == ("content",) for _, _, changes in turn_writes)
    assert len(turn_writes) <= 3 * steps
    # The answer completes the turn in one write, not a fence and a completion.
    assert [
        changes for _, _, changes in turn_writes if "final_message_id" in changes
    ] == [("final_message_id", "status")]
    # Each ledger row is read once, not once per read of the history.
    read = sum(fetched)
    appended = len(ChatTurnLedger(store.database)._rows_after("turn", 0))
    assert read <= appended


def test_long_thinking_is_sealed_out_of_the_turn_row_and_read_back_whole(
    tmp_path, monkeypatch
):
    steps = 30
    thought = "Considering the next value carefully. " * 300
    store, service, prepared, _ = _tool_turn(
        tmp_path, steps=steps, prose=False, thought=thought
    )

    completion, writes, _ = _measure(monkeypatch, store, service, prepared)

    expected = ""
    for index in range(steps):
        expected = chat_module._joined_reasoning(
            expected, f"Step {index} needs value {index}. {thought}"
        )
    assert len(expected) == chat_module._REASONING_LIMIT
    final = store.get(ChatTurn, "turn")
    # The whole bounded episode reads back exactly as one row used to hold it.
    assert service.turn_reasoning(final) == expected
    assert completion.message.reasoning == expected
    assert len(final.reasoning) < chat_module._REASONING_SEAL_CHARS
    sealed = final.request_snapshot[chat_module.REASONING_PARTS_KEY]
    assert sum(item["chars"] for item in sealed[1:]) < chat_module._REASONING_LIMIT
    # A 200,000-character episode no longer rides on every step's write.
    turn_writes = [size for model, size, _ in writes if model == "ChatTurn"]
    assert max(turn_writes) < 40 * 1024


def test_prose_does_not_add_a_turn_write(tmp_path, monkeypatch):
    counts = {}
    for prose in (False, True):
        store, service, prepared, _ = _tool_turn(tmp_path, steps=6, prose=prose)
        _, writes, _ = _measure(monkeypatch, store, service, prepared)
        counts[prose] = sum(1 for item in writes if item[0] == "ChatTurn")
        monkeypatch.undo()
    assert counts[True] == counts[False]


def test_snapshot_parts_are_shared_in_a_conversation_and_deleted_with_it(tmp_path):
    store = NebulaStore(tmp_path / "parts.db")
    first = _conversation(store, "first")
    second = _conversation(store, "second")
    for turn_id, session in (("a", first), ("b", first), ("c", second)):
        compact, parts = split_request_snapshot(
            {"mcp_catalog_snapshot": CATALOG, "include_oci_tools": True},
            engagement_id="project",
            session_id=session.id,
        )
        with store.transaction() as transaction:
            stage_snapshot_parts(transaction, parts)
            transaction.add(
                ChatTurn(
                    id=turn_id,
                    engagement_id="project",
                    session_id=session.id,
                    provider_profile_id="provider",
                    model="model-a",
                    status=ChatTurnStatus.COMPLETE,
                    request_snapshot=compact,
                )
            )

    def parts_of(session_id: str) -> list[ChatSnapshotPart]:
        return [
            item
            for item in store.list_entities(ChatSnapshotPart, limit=1_000)
            if item.session_id == session_id
        ]

    # One catalog, stored once per conversation that uses it.
    assert len(parts_of("first")) == 1
    assert len(parts_of("second")) == 1

    store.delete_chat_session("first")

    assert parts_of("first") == []
    survivor = store.get(ChatTurn, "c")
    assert (
        resolve_request_snapshot(
            store, survivor.request_snapshot, session_id=survivor.session_id
        )["mcp_catalog_snapshot"]
        == CATALOG
    )


def test_altered_or_missing_snapshot_part_is_reported_as_corruption(tmp_path):
    store = NebulaStore(tmp_path / "corrupt-part.db")
    session = _conversation(store)
    compact, parts = split_request_snapshot(
        {"mcp_catalog_snapshot": CATALOG},
        engagement_id="project",
        session_id=session.id,
    )
    with store.transaction() as transaction:
        stage_snapshot_parts(transaction, parts)
    with store.database.session() as db:
        db.execute(
            update(EntityRow)
            .where(EntityRow.id == parts[0].id)
            .values(
                payload={
                    **parts[0].model_dump(mode="json"),
                    "value": [{"id": "forged"}],
                }
            )
        )
    with pytest.raises(CorruptRecordError, match="integrity"):
        resolve_request_snapshot(store, compact, session_id=session.id)
    with pytest.raises(CorruptRecordError, match="missing"):
        resolve_request_snapshot(
            store,
            {SNAPSHOT_PARTS_KEY: {"model_request": "absent-part"}},
            session_id=session.id,
        )


def test_a_part_resolves_only_for_the_conversation_that_stored_it(tmp_path):
    """A reference written into another conversation's goal or turn reads nothing."""

    store = NebulaStore(tmp_path / "foreign-part.db")
    owner = _conversation(store, "owner")
    other = _conversation(store, "other")
    goals = ChatGoalService(store)
    owner_goal = goals.create(
        owner.id, GoalCreate(objective="Review.", completion_criteria=["done"])
    )
    owner_goal = goals.replace_skills(
        owner.id, expected_revision=owner_goal.revision, snapshots=_skills(1)
    )
    compact, parts = split_request_snapshot(
        {"mcp_catalog_snapshot": CATALOG},
        engagement_id="project",
        session_id=owner.id,
    )
    with store.transaction() as transaction:
        stage_snapshot_parts(transaction, parts)
    # However a reference to the owner's part reaches another conversation's
    # goal, it must not carry the owner's instructions there.
    foreign = store.create(
        ChatGoal(
            engagement_id="project",
            session_id=other.id,
            objective="Borrow.",
            completion_criteria=["done"],
            skill_snapshots=owner_goal.skill_snapshots,
        )
    )

    assert goals.skill_snapshots(owner_goal) == _skills(1)
    with pytest.raises(CorruptRecordError, match="another conversation"):
        goals.skill_snapshots(foreign)
    with pytest.raises(CorruptRecordError, match="another conversation"):
        resolve_request_snapshot(store, compact, session_id=other.id)


def test_snapshot_parts_have_no_generic_record_routes(tmp_path):
    """Parts are read only through the turn or goal that owns them."""

    store = NebulaStore(tmp_path / "routes.db")
    session = _conversation(store)
    _, parts = split_request_snapshot(
        {"mcp_catalog_snapshot": CATALOG},
        engagement_id="project",
        session_id=session.id,
    )
    with store.transaction() as transaction:
        stage_snapshot_parts(transaction, parts)
    app = create_app(
        store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        auth_token="test-token",
    )
    paths = {getattr(route, "path", "") for route in app.routes}

    assert not [path for path in paths if "snapshot-parts" in path]
    assert not [path for path in paths if "snapshot_parts" in path]
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    base = "/api/v1/chat-snapshot-parts"
    assert client.get(base, headers=headers).status_code == 404
    assert client.get(f"{base}/{parts[0].id}", headers=headers).status_code == 404
    assert (
        client.post(base, headers=headers, json=parts[0].model_dump(mode="json"))
    ).status_code in {404, 405}
    assert client.delete(f"{base}/{parts[0].id}", headers=headers).status_code in {
        404,
        405,
    }
    assert store.get(ChatSnapshotPart, parts[0].id) == parts[0]


def test_value_written_inline_after_the_split_supersedes_its_part(tmp_path):
    store = NebulaStore(tmp_path / "inline-wins.db")
    session = _conversation(store)
    compact, parts = split_request_snapshot(
        {"model_request": MODEL_REQUEST},
        engagement_id="project",
        session_id=session.id,
    )
    with store.transaction() as transaction:
        stage_snapshot_parts(transaction, parts)
    newer = {"model": "model-a", "messages": [{"role": "user", "content": "newer"}]}
    assert (
        resolve_request_snapshot(
            store, {**compact, "model_request": newer}, session_id=session.id
        )["model_request"]
        == newer
    )


def _resumable_turn(store, service, *, split: bool) -> ChatTurn:
    request = ModelRequest(
        model="model-a",
        messages=[{"role": "user", "content": "Summarize the notes. " * 200}],
    ).model_dump(mode="json")
    snapshot = {
        "model_request": request,
        "citations": [],
        "context_usage": {},
        "include_oci_tools": False,
    }
    parts: list[ChatSnapshotPart] = []
    if split:
        snapshot, parts = split_request_snapshot(
            snapshot, engagement_id="eng-resume", session_id="session-resume"
        )
    with store.transaction() as transaction:
        stage_snapshot_parts(transaction, parts)
        transaction.add(
            ChatTurn(
                id=f"turn-{split}",
                engagement_id="eng-resume",
                session_id="session-resume",
                provider_profile_id="provider-a",
                model="model-a",
                status=ChatTurnStatus.ROUTING,
                queued_at=utc_now(),
                request_snapshot=snapshot,
            )
        )
    return service._interrupt_orphaned_turn(
        store.get(ChatTurn, f"turn-{split}"), [], [], cause="Core restarted"
    )


@pytest.mark.parametrize("split", [False, True])
def test_interrupted_turn_resumes_from_split_and_inline_snapshots(
    tmp_path, monkeypatch, split
):
    store = NebulaStore(tmp_path / f"resume-{split}.db")
    store.create(Engagement(id="eng-resume", name="Resume"))
    profile = store.create(_profile(local=True))
    store.create(
        ChatSession(
            id="session-resume",
            engagement_id="eng-resume",
            title="Resume",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    provider = FakeProvider(profile.id, local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    service = ChatService(store)
    turn = _resumable_turn(store, service, split=split)
    assert turn.status == ChatTurnStatus.INTERRUPTED

    prepared = service.prepare_resume(turn.id)

    assert prepared.model_request.messages[0].content.startswith("Summarize the notes.")
    completion = asyncio.run(service.complete(prepared))
    assert completion.message.content
    assert store.get(ChatTurn, turn.id).status == ChatTurnStatus.COMPLETE


def test_goal_skill_instructions_live_beside_the_goal(tmp_path):
    store = NebulaStore(tmp_path / "goal-skills.db")
    session = _conversation(store)
    goals = ChatGoalService(store)
    goal = goals.create(
        session.id,
        GoalCreate(objective="Review.", completion_criteria=["done"], child_budget=1),
    )
    snapshots = _skills()

    attached = goals.replace_skills(
        session.id, expected_revision=goal.revision, snapshots=snapshots
    )

    with store.database.session() as db:
        row = db.get(EntityRow, attached.id)
        assert row is not None
        stored = row.payload["skill_snapshots"]
    # The goal keeps what the goal panel and API show, not the instructions.
    assert [item["name"] for item in stored] == ["skill-0", "skill-1"]
    assert all("instructions" not in item and item[SKILL_PART_KEY] for item in stored)
    assert all(
        {"name", "path", "source", "root", "sha256", "resources"} <= set(item)
        for item in stored
    )
    assert len(json.dumps(row.payload)) < 4 * 1024
    assert goals.skill_snapshots(attached) == snapshots

    # A child goal's copy lives in the child's conversation.
    goals.write(
        session.id, GoalWrite(expected_revision=attached.revision, action="start")
    )
    child = goals.start_child(
        session.id, GoalCreate(objective="Child.", completion_criteria=["done"])
    )
    assert goals.skill_snapshots(child) == snapshots
    child_part = store.get(ChatSnapshotPart, child.skill_snapshots[0][SKILL_PART_KEY])
    assert child_part.session_id == child.session_id


def test_goal_with_inline_skills_from_before_the_split_still_resolves(tmp_path):
    store = NebulaStore(tmp_path / "goal-inline.db")
    session = _conversation(store)
    snapshots = _skills(1)
    goal = store.create(
        ChatGoal(
            engagement_id="project",
            session_id=session.id,
            objective="Review.",
            completion_criteria=["done"],
            skill_snapshots=[item.model_dump(mode="json") for item in snapshots],
        )
    )
    assert ChatGoalService(store).skill_snapshots(goal) == snapshots


def _answer_messages(store: NebulaStore, session_id: str, turn_id: str):
    return [
        item
        for item in store.list_session_entities(ChatMessage, session_id)
        if item.role == ChatRole.ASSISTANT
        and item.metadata.get("chat_turn_id") == turn_id
        and "kind" not in item.metadata
    ]


def _answering_service(tmp_path, monkeypatch, name: str):
    store = NebulaStore(tmp_path / f"{name}.db")
    engagement = store.create(Engagement(id="eng-answer", name="Answer"))
    profile = store.create(_profile(local=True))
    provider = FakeProvider(profile.id, local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    service = ChatService(store)
    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            messages=[{"role": "user", "content": "Reply with FLASH_OK."}],
            include_knowledge=False,
            stream=True,
        )
    )
    assert prepared.turn is not None
    return store, service, prepared


_CRASH_EXIT = 17


def _answer_then_die(database: str, crash_point: str) -> None:
    """Child process: answer one turn and exit at ``crash_point`` like a killed Core."""

    store = NebulaStore(database)
    engagement = store.create(Engagement(id="eng-answer", name="Answer"))
    profile = store.create(_profile(local=True))
    provider = FakeProvider(profile.id, local=True)
    service = ChatService(store, provider_factory=lambda _: provider)
    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            messages=[{"role": "user", "content": "Reply with FLASH_OK."}],
            include_knowledge=False,
            stream=True,
        )
    )
    if crash_point == "after_answer_commit":
        # Releases before this one committed the answer and then, in a
        # second write, completed its turn. The process dies in between.
        naming = ChatService._start_initial_naming

        def die_once_answered(self, prepared, assistant_response=""):
            if assistant_response:
                os._exit(_CRASH_EXIT)
            return naming(self, prepared, assistant_response)

        ChatService._start_initial_naming = die_once_answered  # type: ignore[method-assign]
    else:
        add_all = StoreTransaction.add_all

        def die_before_commit(self, entities):
            add_all(self, entities)
            if any(
                isinstance(item, ChatMessage) and item.role == ChatRole.ASSISTANT
                for item in entities
            ):
                os._exit(_CRASH_EXIT)
            return entities

        StoreTransaction.add_all = die_before_commit  # type: ignore[method-assign]
    asyncio.run(service.complete(prepared))
    os._exit(0)


@pytest.mark.parametrize("crash_point", ["after_answer_commit", "inside_answer_commit"])
def test_core_killed_while_saving_the_answer_leaves_one_answer_after_resume(
    tmp_path, crash_point
):
    database = tmp_path / "killed.db"
    child = multiprocessing.get_context("spawn").Process(
        target=_answer_then_die, args=(str(database), crash_point)
    )
    child.start()
    child.join(120)
    assert child.exitcode == _CRASH_EXIT

    store = NebulaStore(database)
    (turn,) = store.list_entities(ChatTurn)
    if crash_point == "inside_answer_commit":
        # The answer's commit never happened: nothing of it was saved.
        assert _answer_messages(store, turn.session_id, turn.id) == []
        assert turn.final_message_id is None
    provider = FakeProvider("provider-a", local=True)
    restarted = ChatService(store, provider_factory=lambda _: provider)
    asyncio.run(restarted.startup())
    pending = restarted.pending_turn(turn.session_id)
    if crash_point == "inside_answer_commit":
        assert pending is not None and pending.id == turn.id
    if pending is not None:
        asyncio.run(restarted.complete(restarted.prepare_resume(pending.id)))

    answers = _answer_messages(store, turn.session_id, turn.id)
    assert len(answers) == 1
    completed = store.get(ChatTurn, turn.id)
    assert completed.status == ChatTurnStatus.COMPLETE
    assert completed.final_message_id == answers[0].id


def test_turn_resumed_with_its_answer_already_saved_adopts_that_answer(
    tmp_path, monkeypatch
):
    """A turn an older Core left answered but unfinished completes on it."""

    store, service, prepared = _answering_service(tmp_path, monkeypatch, "adopt")
    turn = store.update(
        ChatTurn,
        prepared.turn.id,
        {"status": ChatTurnStatus.ROUTING},
        expected_revision=store.get(ChatTurn, prepared.turn.id).revision,
    )
    saved = store.create(
        ChatMessage(
            engagement_id=turn.engagement_id,
            session_id=turn.session_id,
            sequence=99,
            role=ChatRole.ASSISTANT,
            content="The answer saved before Core stopped.",
            metadata={"chat_turn_id": turn.id, "tool_call_ids": [], "tool_results": []},
        )
    )
    restarted = ChatService(store)
    asyncio.run(restarted.startup())

    completion = asyncio.run(restarted.complete(restarted.prepare_resume(turn.id)))

    assert completion.message.id == saved.id
    assert completion.message.content == saved.content
    assert [item.id for item in _answer_messages(store, turn.session_id, turn.id)] == [
        saved.id
    ]
    completed = store.get(ChatTurn, turn.id)
    assert completed.status == ChatTurnStatus.COMPLETE
    assert completed.final_message_id == saved.id


def _hook_service(tmp_path, *, events):
    store = NebulaStore(tmp_path / "hooks.db")
    workspace = tmp_path / "workspace"
    _write_native_hook(workspace, "observer", events=events)
    engagement = store.create(Engagement(id="eng-hooks", name="Hooks"))
    profile = store.create(_profile(local=True))
    provider = FakeProvider(profile.id, local=True)
    service = ChatService(
        store,
        provider_factory=lambda _: provider,
        workspace_resolver=lambda _: workspace,
    )
    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            hook_ids=["observer"],
            messages=[{"role": "user", "content": "Answer."}],
            include_knowledge=False,
        )
    )
    assert [item.id for item in prepared.hook_snapshots] == ["observer"]
    return store, service, prepared


def test_turn_workspace_provenance_keeps_the_event_loop_running(tmp_path, monkeypatch):
    store, service, prepared = _hook_service(
        tmp_path, events=["chat.turn.started", "chat.turn.completed"]
    )
    original_snapshot = workspace_provenance.snapshot

    def slow_snapshot(workspace, **kwargs):
        # A large dirty checkout: Git status and hashing take a while.
        time.sleep(0.4)
        return original_snapshot(workspace, **kwargs)

    monkeypatch.setattr(workspace_provenance, "snapshot", slow_snapshot)

    async def scenario():
        gaps: list[float] = []
        running = True

        async def heartbeat():
            last = time.monotonic()
            while running:
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())  # diagnostic-expected: test heartbeat
        try:
            await service.complete(prepared)
        finally:
            running = False
            await beat
        return gaps

    gaps = asyncio.run(scenario())

    assert max(gaps) < 0.3
    observations = store.list_entities(WorkspaceProvenanceObservation)
    assert [item.status for item in observations] == ["complete"]


def test_turn_workspace_provenance_is_skipped_for_tool_only_hooks(
    tmp_path, monkeypatch
):
    store, service, prepared = _hook_service(tmp_path, events=["tool.after"])
    snapshots: list[Path] = []

    def recording_snapshot(workspace, **kwargs):
        snapshots.append(workspace)
        raise AssertionError("no turn hook reads this snapshot")

    monkeypatch.setattr(workspace_provenance, "snapshot", recording_snapshot)

    asyncio.run(service.complete(prepared))

    assert snapshots == []
    assert store.list_entities(WorkspaceProvenanceObservation) == []


def _git_repository(tmp_path: Path) -> Path:
    import subprocess

    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(("git", "-C", str(root), "init", "-q", "-b", "main"), check=True)
    return root


def test_workspace_snapshot_past_its_time_limit_is_unsupported(tmp_path, monkeypatch):
    root = _git_repository(tmp_path)
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "git").write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    (shim / "git").chmod(0o700)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")

    started = time.monotonic()
    observed = workspace_provenance.snapshot(root, time_limit=0.3)

    assert time.monotonic() - started < 3
    assert not observed.supported
    assert observed.reason == "snapshot_time_limit_exceeded:0.3s"
    assert observed.paths == {}


def test_workspace_snapshot_past_its_byte_limit_is_unsupported(tmp_path, monkeypatch):
    root = _git_repository(tmp_path)
    (root / "capture.bin").write_bytes(b"x" * 4_096)
    monkeypatch.setattr(workspace_provenance, "MAX_DIRTY_BYTES", 1_024)

    observed = workspace_provenance.snapshot(root)

    assert not observed.supported
    assert observed.reason == "dirty_bytes_limit_exceeded:4096>1024"
    assert observed.paths == {}


def test_entity_update_validates_the_changed_payload_once(tmp_path):
    store = NebulaStore(tmp_path / "update.db")
    _conversation(store)
    turn = store.create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model-a",
            request_snapshot={"include_oci_tools": True},
        )
    )

    updated = store.update(
        ChatTurn,
        turn.id,
        {"usage": ChatTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3)},
        expected_revision=turn.revision,
    )

    assert updated.revision == turn.revision + 1
    assert updated.created_at == turn.created_at
    assert updated.request_snapshot == turn.request_snapshot
    assert store.get(ChatTurn, turn.id) == updated
    with pytest.raises(ValidationError):
        store.update(ChatTurn, turn.id, {"next_step": -1})
    with store.database.session() as db:
        db.execute(
            update(EntityRow)
            .where(EntityRow.id == turn.id)
            .values(payload={**updated.model_dump(mode="json"), "status": "unknown"})
        )
    with pytest.raises(CorruptRecordError):
        store.update(ChatTurn, turn.id, {"next_step": 1})


def test_ledger_history_reads_each_row_once_and_hands_out_copies(tmp_path):
    store = NebulaStore(tmp_path / "ledger.db")
    _conversation(store)
    turn = store.create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model-a",
            queued_at=utc_now(),
        )
    )
    ledger = ChatTurnLedger(store.database)
    other_worker = ChatTurnLedger(store.database)
    fetched: list[int] = []
    original = ledger._rows_after

    def recording(turn_id, sequence):
        rows = original(turn_id, sequence)
        fetched.append(len(rows))
        return rows

    ledger._rows_after = recording  # type: ignore[method-assign]
    for step in range(12):
        ledger.append(
            turn.id,
            {"step": step, "status": "running", "arguments": {"path": [str(step)]}},
            idempotency_key=f"intent:{step}",
        )
        ledger.append(
            turn.id,
            {"step": step, "status": "complete", "arguments": {"path": [str(step)]}},
        )
        assert [entry["step"] for entry in ledger.history(turn)] == list(
            range(step + 1)
        )
        assert ledger.history(turn)[-1]["status"] == "complete"
    # Each of the 24 rows was read once across 24 history reads.
    assert sum(fetched) == 24

    history = ledger.history(turn)
    history[0]["arguments"]["path"].append("mutated")
    assert ledger.history(turn)[0]["arguments"]["path"] == ["0"]

    # A row another worker appends is seen on the next read.
    other_worker.append(turn.id, {"step": 12, "status": "complete"})
    assert ledger.history(turn)[-1]["step"] == 12
    assert ledger.history(turn) == other_worker.history(turn)


def test_subagent_gets_the_ssh_hosts_of_a_parent_whose_snapshot_is_split(tmp_path):
    """The hosts a child inherits are read back from the parent's snapshot part."""

    from nebula.v3.domain import SshEnvironment
    from nebula.v3.tools import InvalidToolArguments, ToolCallOrigin, ToolInvocation
    from tests.v3.test_chat_subagent_lifecycle import _session
    from tests.v3.test_chat_subagents import RoutedProvider, _setup

    async def scenario() -> list:
        store, _, _, chat = _setup(tmp_path, RoutedProvider([], []))
        store.create(SshEnvironment(id="ssh-a", alias="lab-a", enabled=True))
        parent_session = _session(store)
        hosts = [{"id": "ssh-a", "alias": "lab-a", "notes": "n" * 2_000}]
        compact, parts = split_request_snapshot(
            {
                "include_oci_tools": True,
                "ssh_environment_snapshot": hosts,
                "allow_subagents": True,
            },
            engagement_id="project",
            session_id=parent_session.id,
        )
        assert "ssh_environment_snapshot" not in compact
        parent = ChatTurn(
            engagement_id="project",
            session_id=parent_session.id,
            provider_profile_id="provider",
            model="model-a",
            status=ChatTurnStatus.ROUTING,
            request_snapshot=compact,
        )
        with store.transaction() as transaction:
            stage_snapshot_parts(transaction, parts)
            transaction.add(parent)
        captured: list = []

        async def capture(request):
            captured.append(request)
            raise RuntimeError("stop before the provider")

        chat.prepare_async = capture  # type: ignore[method-assign]
        with pytest.raises(InvalidToolArguments):
            await chat.subagents.start(
                ToolInvocation(
                    engagement_id="project",
                    run_id=parent.id,
                    origin=ToolCallOrigin.CHAT,
                    chat_session_id=parent_session.id,
                    chat_turn_id=parent.id,
                    tool_name="start_subagent",
                    workspace=tmp_path,
                    idempotency_key="start-1",
                ),
                task="Check the host.",
                name=None,
                context=None,
            )
        await chat.shutdown()
        return captured

    captured = asyncio.run(scenario())

    assert captured[0].ssh_environment_ids == ["ssh-a"]
