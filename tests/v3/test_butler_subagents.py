"""Butler hands longer work to subagents that keep its Core tools."""

import asyncio
from pathlib import Path

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_butler_sessions import _runtime
from tests.v3.test_chat_subagents import (
    RoutedProvider,
    _call,
    _drain,
    _finish,
    _messages,
    _request,
    _response,
    _setup,
    _until,
)

from nebula.v3.butler_sessions import ButlerSessionReservation, reserve_butler_session
from nebula.v3.chat import (
    BUTLER_DELEGATION_INSTRUCTION,
    BUTLER_SUBAGENT_CHILD_INSTRUCTION,
)
from nebula.v3.chat_assistant_config import (
    ChatAssistantConfigPatch,
    chat_assistant_config,
    update_chat_assistant_config,
)
from nebula.v3.domain import (
    ChatBackend,
    ChatSession,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
)
from nebula.v3.project_reads import active_control_assistant_session


async def test_butler_delegates_to_a_butler_mode_child_and_stays_the_butler(
    tmp_path: Path,
) -> None:
    provider = RoutedProvider(
        parent=[
            _call(
                "p1",
                "start_subagent",
                task="Pause every running goal in this project.",
                name="Pause goals",
                context=None,
            ),
            _finish("p2"),
            _response(text="Started a subagent to pause the goals."),
        ],
        child=[_response(text="Paused two goals.")],
    )
    store, project, _, chat = await _setup(tmp_path, provider)
    provider.child_gate = asyncio.Event()
    prepared = await chat.prepare_async(
        _request(
            project,
            content="Pause the goals and tell me when done.",
            control_assistant=True,
            allow_subagents=True,
        )
    )
    parent_turn_id = await chat.start_provider_turn(prepared)
    await _drain(chat, parent_turn_id)
    parent_turn = await store.get(ChatTurn, parent_turn_id)
    # Butler answered without waiting for the child.
    assert parent_turn.status == ChatTurnStatus.COMPLETE
    assert BUTLER_DELEGATION_INSTRUCTION in provider.parent_requests[0].instructions
    (record,) = await store.list_entities(ChatSubagent)
    assert record.status == ChatSubagentStatus.RUNNING

    child = await store.get(ChatSession, record.child_session_id)
    assert child.metadata["control_assistant"] is True
    assert child.parent_session_id == parent_turn.session_id
    await _until(lambda: bool(provider.child_requests))
    assert BUTLER_SUBAGENT_CHILD_INSTRUCTION in provider.child_requests[0].instructions
    assert BUTLER_DELEGATION_INSTRUCTION not in provider.child_requests[0].instructions

    # The newer child chat never replaces Butler's own conversation.
    butler = await reserve_butler_session(
        store,
        _runtime(store, tmp_path),
        ButlerSessionReservation(
            engagement_id=project.id,
            backend=ChatBackend.PROVIDER,
            provider_id="provider",
        ),
    )
    assert butler.id == parent_turn.session_id
    current = await active_control_assistant_session(store, project.id)
    assert current is not None and current.id == parent_turn.session_id

    provider.child_gate.set()
    for _ in range(500):
        if (await store.get(ChatSubagent, record.id)).result_message_id:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the child never reported to Butler")
    messages = await _messages(store, parent_turn.session_id)
    assert "Paused two goals." in messages[-1].content
    await chat.shutdown()


async def test_butler_without_subagents_gets_no_delegation_instruction(
    tmp_path: Path,
) -> None:
    provider = RoutedProvider(parent=[_response(text="Done.")], child=[])
    store, project, _, chat = await _setup(tmp_path, provider)
    prepared = await chat.prepare_async(
        _request(project, content="Status?", control_assistant=True)
    )
    await _drain(chat, await chat.start_provider_turn(prepared))
    assert BUTLER_DELEGATION_INSTRUCTION not in provider.parent_requests[0].instructions
    await chat.shutdown()


async def test_provider_butler_delegates_by_default_until_the_operator_chooses(
    tmp_path: Path,
) -> None:
    store, project, _, _chat = await _setup(
        tmp_path, RoutedProvider(parent=[], child=[])
    )
    butler = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Butler",
            provider_profile_id="provider",
            model="model-a",
            # Older releases saved allow_subagents false with any settings save.
            metadata={
                "control_assistant": True,
                "assistant_config": {"allow_subagents": False},
            },
        )
    )
    ordinary = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Chat",
            provider_profile_id="provider",
            model="model-a",
        )
    )
    assert (await chat_assistant_config(store, butler))["allow_subagents"] is True
    assert (await chat_assistant_config(store, ordinary))["allow_subagents"] is False

    off = await update_chat_assistant_config(
        store,
        butler,
        ChatAssistantConfigPatch(allow_subagents=False),
        expected_revision=butler.revision,
    )
    assert off.metadata["butler_allow_subagents"] is False
    assert (await chat_assistant_config(store, off))["allow_subagents"] is False

    # An unrelated save keeps the operator's choice.
    effort = await update_chat_assistant_config(
        store,
        off,
        ChatAssistantConfigPatch(reasoning_effort="low"),
        expected_revision=off.revision,
    )
    assert (await chat_assistant_config(store, effort))["allow_subagents"] is False

    on = await update_chat_assistant_config(
        store,
        effort,
        ChatAssistantConfigPatch(allow_subagents=True),
        expected_revision=effort.revision,
    )
    assert (await chat_assistant_config(store, on))["allow_subagents"] is True
