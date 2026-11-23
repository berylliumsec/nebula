"""Supervisor notes and mid-turn delivery of goal changes."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import nebula.v3.chat as chat_module
from nebula.v3.chat import ChatCompletionRequest, ChatService, _goal_progress_block
from nebula.v3.chat_goal_addenda import (
    ADDENDUM_HEADING,
    GOAL_READ_TOOL_NAME,
    SEEN_KEY,
    goal_guidance_text,
    read_goal_page,
)
from nebula.v3.chat_goals import (
    ChatGoalService,
    GoalCreate,
    GoalNoteCreate,
    GoalUpdate,
    GoalWrite,
)
from nebula.v3.domain import (
    GOAL_SUPERVISOR_NOTE_LIMIT,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessTurn,
    HarnessTurnOrigin,
    ProviderProfile,
)
from nebula.v3.harnesses import HarnessRuntimeService, HarnessStateError
from nebula.v3.providers import ModelRequest, ModelResponse, ToolChoice
from nebula.v3.runtime_routes import command_lane
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError
from nebula.v3.tool_results import model_result_bytes
from tests.v3.test_chat import FakeProvider, _admitted_stream


async def _setup(tmp_path, *, backend: ChatBackend = ChatBackend.PROVIDER):
    store = NebulaStore(tmp_path / "goals.db")
    await store.create(Engagement(id="project", name="Project"))
    provider = await store.create(
        ProviderProfile(id="provider", name="Provider", provider_type="openrouter")
    )
    await store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Goal chat",
            **(
                {"provider_profile_id": provider.id, "model": "model"}
                if backend == ChatBackend.PROVIDER
                else {
                    "backend": ChatBackend.HARNESS,
                    "harness_profile_id": "harness",
                    "harness_session_id": "harness-session",
                    "model": "model",
                }
            ),
        )
    )
    return store


async def _running(goals: ChatGoalService) -> ChatGoal:
    draft = await goals.create(
        "session",
        GoalCreate(
            objective="Audit the parser",
            completion_criteria=["Report every crash"],
            plan=["Fuzz", "Triage"],
        ),
    )
    return await goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )


async def test_notes_append_render_and_refuse_past_the_cap(tmp_path):
    store = await _setup(tmp_path)
    goals = ChatGoalService(store)
    running = await _running(goals)
    assert running.guidance_revision == 0

    with pytest.raises(ValidationError):
        GoalNoteCreate(text="   ")
    with pytest.raises(ValidationError):
        GoalNoteCreate(text="x" * 4_001)

    result = await goals.add_note(
        "session", GoalNoteCreate(text="  Prioritize the heap crash.  ")
    )
    # Nothing is running a turn, so the agent hears it at the next turn.
    assert result.delivery == "next_turn"
    goal = result.goal
    assert [note.text for note in goal.supervisor_notes] == [
        "Prioritize the heap crash."
    ]
    assert goal.supervisor_notes[0].author == "operator"
    assert goal.guidance_revision == 1
    assert goal.guidance_changes[-1].note_id == result.note_id

    # Notes ride on the turn's progress block, not the cached instructions.
    progress = _goal_progress_block(goal)
    assert "Prioritize the heap crash." in progress
    assert "Prioritize the heap crash." not in chat_module._goal_instructions(goal)
    assert "Prioritize the heap crash." in goal_guidance_text(goal)

    for index in range(GOAL_SUPERVISOR_NOTE_LIMIT - 1):
        await goals.add_note(
            "session", GoalNoteCreate(text=f"note {index} " + "é" * 300)
        )
    with pytest.raises(ConflictError, match="already has 50 supervisor notes"):
        await goals.add_note("session", GoalNoteCreate(text="one too many"))
    full = await goals.get("session")
    assert len(full.supervisor_notes) == GOAL_SUPERVISOR_NOTE_LIMIT
    # The delivery log stays bounded however many changes there were.
    assert len(full.guidance_changes) == 20

    removed = await goals.remove_note("session", result.note_id)
    assert result.note_id not in {note.id for note in removed.goal.supervisor_notes}
    assert "withdrawn" in removed.goal.guidance_changes[-1].summary
    with pytest.raises(NotFoundError):
        await goals.remove_note("session", result.note_id)

    # Every page of a full goal fits the model-delivery bound.
    goal = await goals.get("session")
    offset, pages = 0, 0
    while True:
        page = read_goal_page(goal, offset)
        assert model_result_bytes(page) <= 8 * 1024
        pages += 1
        if "next_offset" not in page:
            break
        offset = page["next_offset"]
    assert pages > 1

    await goals.write(
        "session",
        GoalWrite(expected_revision=goal.revision, action="cancel"),
    )
    with pytest.raises(ConflictError, match="cannot take supervisor notes"):
        await goals.add_note("session", GoalNoteCreate(text="too late"))


async def test_only_running_goal_edits_record_a_guidance_change(tmp_path):
    store = await _setup(tmp_path)
    goals = ChatGoalService(store)
    draft = await goals.create(
        "session",
        GoalCreate(objective="Draft", completion_criteria=["One"]),
    )
    edited = await goals.update(
        "session",
        GoalUpdate(
            expected_revision=draft.revision,
            objective="Draft edited",
            completion_criteria=["One"],
        ),
    )
    assert edited.guidance_revision == 0
    running = await goals.write(
        "session", GoalWrite(expected_revision=edited.revision, action="start")
    )
    # A limit-only edit changes no guidance.
    limits = await goals.update(
        "session",
        GoalUpdate(
            expected_revision=running.revision,
            objective=running.objective,
            completion_criteria=running.completion_criteria,
            step_budget=9,
        ),
    )
    assert limits.guidance_revision == 0
    changed = await goals.update(
        "session",
        GoalUpdate(
            expected_revision=limits.revision,
            objective="Draft edited\nAlso cover the CLI.",
            completion_criteria=["One", "Two"],
            plan=["Start"],
        ),
    )
    assert changed.guidance_revision == 1
    summary = changed.guidance_changes[-1].summary
    assert "Objective extended:\nAlso cover the CLI." in summary
    assert "+ Two" in summary
    assert "Plan changed:\n+ Start" in summary


async def test_running_provider_turn_hears_a_note_once_before_its_next_step(
    tmp_path, monkeypatch
):
    store = await _setup(tmp_path)
    goals = ChatGoalService(store)
    running = await _running(goals)

    def addenda(request: ModelRequest) -> int:
        return json.dumps(request.model_dump(mode="json")).count(ADDENDUM_HEADING)

    class SupervisedProvider(FakeProvider):
        routing_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("operation"):
                return await super().complete(request)
            self.requests.append(request)
            if request.tool_choice == ToolChoice.NONE:
                return ModelResponse(
                    provider_id=self.config.id,
                    model="model",
                    text="Still auditing.",
                    finish_reason="stop",
                )
            self.routing_calls += 1
            assert GOAL_READ_TOOL_NAME in {tool.name for tool in request.tools}
            if self.routing_calls == 1:
                assert addenda(request) == 0
                # The operator adds a note while the turn works.
                await goals.add_note(
                    "session", GoalNoteCreate(text="Skip the CLI; focus on the heap.")
                )
            elif self.routing_calls == 2:
                assert addenda(request) == 1
                assert "Skip the CLI; focus on the heap." in json.dumps(
                    request.model_dump(mode="json")
                )
            else:
                # Delivered once: the addendum stays in the history but is
                # not added again.
                assert addenda(request) == 1
                return ModelResponse(
                    provider_id=self.config.id,
                    model="model",
                    text="Focusing on the heap now.",
                    finish_reason="stop",
                )
            return ModelResponse(
                provider_id=self.config.id,
                model="model",
                tool_calls=[
                    {
                        "id": f"read-{self.routing_calls}",
                        "name": GOAL_READ_TOOL_NAME,
                        "arguments": {},
                    }
                ],
                finish_reason="tool_calls",
            )

    async def scenario() -> None:
        profile = await store.get(ProviderProfile, "provider")
        capabilities = profile.capabilities.model_dump(mode="python")
        capabilities["tool_calling"] = True
        await store.update(
            ProviderProfile,
            profile.id,
            {
                "capabilities": capabilities,
                "capability_verifications": {
                    "model": {"model": "model", "status": "verified"}
                },
            },
        )
        provider = SupervisedProvider("provider", local=True)
        provider.config.default_model = "model"
        provider.config.model_allowlist = ["model"]
        provider.config.capabilities.tools = True
        provider.config.capabilities.strict_tools = True
        monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        chat = ChatService(store, workspace_resolver=lambda _: workspace)
        prepared = await chat.prepare_async(
            ChatCompletionRequest(
                provider_id="provider",
                engagement_id="project",
                session_id="session",
                goal_id=running.id,
                messages=[{"role": "user", "content": "Keep auditing"}],
                include_knowledge=False,
                stream=True,
            )
        )
        assert prepared.turn.request_snapshot[SEEN_KEY] == {"guidance_revision": 0}
        async for _ in _admitted_stream(chat, prepared):
            pass
        assert provider.routing_calls == 3
        turn = await store.get(ChatTurn, prepared.turn.id)
        assert turn.status == ChatTurnStatus.COMPLETE
        history = await chat.turn_ledger.history(turn)
        delivered = [entry for entry in history if entry.get("delivered_by_core")]
        assert [entry["name"] for entry in delivered] == [GOAL_READ_TOOL_NAME]
        assert delivered[0][SEEN_KEY] == {"guidance_revision": 1}
        await chat.shutdown()

    await scenario()
    await asyncio.sleep(0)


async def test_running_harness_turn_is_steered_and_grok_falls_back(tmp_path):
    store = await _setup(tmp_path, backend=ChatBackend.HARNESS)
    steered: list[tuple[str, str]] = []
    harness = SimpleNamespace(
        steer_goal_addendum=None,
    )

    async def steer(chat_session_id: str, text: str) -> bool:
        steered.append((chat_session_id, text))
        return True

    harness.steer_goal_addendum = steer
    goals = ChatGoalService(store, harness)  # type: ignore[arg-type]
    running = await _running(goals)
    assert running.status == ChatGoalStatus.RUNNING

    result = await goals.add_note("session", GoalNoteCreate(text="Stop fuzzing."))
    assert result.delivery == "running_turn"
    assert steered[-1][0] == "session"
    assert steered[-1][1].startswith(ADDENDUM_HEADING)
    assert "Stop fuzzing." in steered[-1][1]

    goal = await goals.get("session")
    await goals.update(
        "session",
        GoalUpdate(
            expected_revision=goal.revision,
            objective=goal.objective,
            completion_criteria=[*goal.completion_criteria, "Write a patch"],
            plan=goal.plan,
        ),
    )
    assert "+ Write a patch" in steered[-1][1]

    # A harness that cannot take input mid-turn (Grok) reads the changed
    # goal at its next turn instead.
    async def cannot_steer(chat_session_id: str, text: str) -> bool:
        return False

    harness.steer_goal_addendum = cannot_steer
    fallback = await goals.add_note("session", GoalNoteCreate(text="Later."))
    assert fallback.delivery == "next_turn"


async def test_harness_runtime_reports_a_harness_that_cannot_be_steered(tmp_path):
    store = await _setup(tmp_path, backend=ChatBackend.HARNESS)
    turn = await store.create(
        HarnessTurn(
            engagement_id="project",
            harness_session_id="harness-session",
            origin=HarnessTurnOrigin.CHAT,
            chat_session_id="session",
            chat_turn_id="chat-turn",
            prompt="Work",
        )
    )
    runtime = HarnessRuntimeService.__new__(HarnessRuntimeService)
    runtime.store = store
    runtime._active = {"harness-session": SimpleNamespace(turn_id=turn.id)}
    calls: list[dict] = []

    async def steer_turn(turn_id, text, **kwargs):
        calls.append({"turn_id": turn_id, "text": text, **kwargs})
        if len(calls) > 1:
            raise HarnessStateError("Grok ACP does not advertise turn steering")

    runtime.steer_turn = steer_turn  # type: ignore[method-assign]
    assert await runtime.steer_goal_addendum("session", "change") is True
    assert calls[0]["turn_id"] == turn.id
    assert calls[0]["title"] == "Supervisor addendum"
    assert await runtime.steer_goal_addendum("session", "change") is False
    assert await runtime.steer_goal_addendum("other-session", "change") is False


def test_note_routes_travel_on_control_capacity():
    assert command_lane("/api/v1/chat/sessions/s/goal/notes") == "control"
    assert (
        command_lane("/api/v1/chat/sessions/s/goal/notes/n", method="DELETE")
        == "control"
    )
    assert command_lane("/api/v1/chat/sessions/s/goal") == "work"
