"""A durable HTTP identity and its conversation commit together exactly once."""

import asyncio
import json

import pytest
from sqlalchemy import select, update

from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.database import (
    ProjectOwnershipRow,
    RuntimeChatBindingRow,
    RuntimeCommandRow,
    RuntimeResourceRow,
)
from nebula.v3.domain import ChatMessage, ChatSession, ChatTurn, ProviderProfile
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_chat_acceptance import CHAT_ACCEPT_COMMAND, bind_chat_command
from nebula.v3.runtime_coordination import CoordinationConflict, LeaseLost
from nebula.v3.runtime_delivery import HttpIntent
from nebula.v3.storage import NotFoundError
from tests.v3.test_chat import FakeProvider
from tests.v3.test_execution_worker import runtime as runtime
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def intent(text="Synthetic accepted message", **changes):
    body = {
        "provider_id": "provider",
        "engagement_id": "project",
        "model": "model-a",
        "messages": [{"role": "user", "content": text}],
        "include_knowledge": False,
        "stream": True,
        **changes,
    }
    return HttpIntent.capture(
        "POST",
        "/api/v1/chat/completions",
        b"",
        [(b"content-type", b"application/json")],
        json.dumps(body).encode(),
    )


async def accept(
    runtime, *, typed=True, key="accepted", text="Synthetic accepted message"
):
    return await runtime.delivery.accept(
        intent(text),
        project_id="project",
        actor_id="operator",
        idempotency_key=key,
        chat_acceptance=typed,
    )


@pytest.fixture
async def chat(runtime):
    profile = await runtime.store.create(
        ProviderProfile(
            id="provider",
            name="Inert local",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    service = ChatService(
        runtime.application_store,
        provider_factory=lambda _: FakeProvider(profile.id, local=True),
    )
    try:
        yield service
    finally:
        with execution_context(runtime.lease):
            await service.shutdown()


async def prepare(runtime, chat, receipt, *, session_id=None):
    request = ChatCompletionRequest.model_validate_json(intent().body())
    request.session_id = session_id
    request._runtime_command_id = receipt.command_id
    with execution_context(runtime.lease):
        return await chat.prepare_async(request)


@pytest.mark.parametrize("first_typed", [False, True])
async def test_transport_negotiation_does_not_change_an_existing_receipt(
    runtime, first_typed
):
    receipt = await accept(runtime, typed=first_typed)
    assert await accept(runtime, typed=not first_typed) == receipt
    async with runtime.coordination.database.session() as session:
        rows = list(await session.scalars(select(RuntimeCommandRow)))
    assert len(rows) == 1 and rows[0].name == (
        CHAT_ACCEPT_COMMAND if first_typed else "http.invoke/v1"
    )
    with pytest.raises(CoordinationConflict, match="different payload"):
        await accept(runtime, typed=not first_typed, text="Different input")


async def test_concurrent_old_and_new_api_modes_accept_one_identity(runtime):
    results = await asyncio.gather(
        accept(runtime, typed=False), accept(runtime, typed=True)
    )
    assert results[0] == results[1]
    async with runtime.coordination.database.session() as session:
        assert len(list(await session.scalars(select(RuntimeCommandRow)))) == 1


@pytest.mark.parametrize("existing_session", [False, True])
async def test_binding_commits_with_new_turn_and_user_message(
    runtime, chat, existing_session
):
    session_id = None
    if existing_session:
        session_id = "existing-chat"
        await runtime.store.create(
            ChatSession(
                id=session_id,
                engagement_id="project",
                title="Existing",
                provider_profile_id="provider",
                model="model-a",
            )
        )
    receipt = await accept(runtime)
    command = await runtime.coordination.coordination.claim_next(
        runtime.lease, lane="work"
    )
    assert command.receipt == receipt
    prepared = await prepare(runtime, chat, receipt, session_id=session_id)
    binding = await runtime.delivery.chat_binding("operator", receipt.command_id)
    assert (
        binding.turn_id == prepared.turn.id
        and binding.session_id == prepared.session.id
    )
    assert binding.project_id == "project"
    assert len(await runtime.store.list_entities(ChatTurn)) == 1
    assert [
        message.content for message in await runtime.store.list_entities(ChatMessage)
    ] == ["Synthetic accepted message"]
    with pytest.raises(NotFoundError):
        await runtime.delivery.chat_binding("device:another", receipt.command_id)


async def test_rebinding_rolls_back_the_second_session_turn_and_message(runtime, chat):
    receipt = await accept(runtime)
    await runtime.coordination.coordination.claim_next(runtime.lease, lane="work")
    first = await prepare(runtime, chat, receipt)
    with pytest.raises(CoordinationConflict, match="another chat turn"):
        await prepare(runtime, chat, receipt)
    assert [turn.id for turn in await runtime.store.list_entities(ChatTurn)] == [
        first.turn.id
    ]
    assert len(await runtime.store.list_entities(ChatSession)) == 1
    assert len(await runtime.store.list_entities(ChatMessage)) == 1


async def test_unclaimed_or_wrong_transport_cannot_publish_a_binding(runtime, chat):
    receipt = await accept(runtime)
    with pytest.raises(CoordinationConflict, match="claimed request"):
        await prepare(runtime, chat, receipt)
    raw = await accept(runtime, typed=False, key="legacy")
    # Finish the first claim so dispatch reaches the legacy invocation.
    await runtime.coordination.coordination.claim_next(runtime.lease, lane="work")
    await runtime.coordination.coordination.finish_command(
        runtime.lease, receipt.command_id, state="failed", result={}
    )
    await runtime.coordination.coordination.claim_next(runtime.lease, lane="work")
    with pytest.raises(CoordinationConflict, match="claimed request"):
        await prepare(runtime, chat, raw)
    assert await runtime.store.list_entities(ChatTurn) == []


async def test_binding_is_immutable_and_stale_tasks_cannot_create_one(runtime, chat):
    receipt = await accept(runtime)
    await runtime.coordination.coordination.claim_next(runtime.lease, lane="work")
    prepared = await prepare(runtime, chat, receipt)
    with (
        execution_context(runtime.lease),
        pytest.raises(CoordinationConflict, match="immutable"),
    ):
        async with runtime.application_store.database.session() as session:
            await session.execute(
                update(RuntimeChatBindingRow)
                .where(RuntimeChatBindingRow.command_id == receipt.command_id)
                .values(turn_id="another")
            )
    async with runtime.coordination.database.session() as session:
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == "project")
            .values(lease_expires_at_ms=0)
        )
    await runtime.coordination.coordination.register_runtime("new", "new-version")
    await runtime.coordination.coordination.claim_project("project", "new")
    with execution_context(runtime.lease), pytest.raises(LeaseLost):
        async with runtime.application_store.database.session() as session:
            await bind_chat_command(
                session,
                command_id=receipt.command_id,
                turn_id=prepared.turn.id,
                session_id=prepared.session.id,
            )


@pytest.mark.parametrize("stream", [False, True])
async def test_private_request_identity_is_not_accepted_from_public_json(stream):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="_runtime_command_id"):
        ChatCompletionRequest.model_validate(
            {
                **json.loads(intent(stream=stream).body()),
                "_runtime_command_id": "forged",
            }
        )


async def test_typed_mode_rejects_a_non_streaming_http_intent(runtime):
    with pytest.raises(ValueError, match="streaming chat intent"):
        await runtime.delivery.accept(
            intent(stream=False),
            project_id="project",
            actor_id="operator",
            idempotency_key="invalid",
            chat_acceptance=True,
        )


async def test_stream_end_and_provider_resource_release_share_one_fenced_commit(
    runtime, monkeypatch
):
    from nebula.v3.runtime_chat_stream import RuntimeChatJournal, SEGMENT_END

    await runtime.store.create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id="chat",
            model="model-a",
            provider_profile_id="provider",
        )
    )
    journal = RuntimeChatJournal(
        runtime.coordination.coordination, runtime.application_store
    )
    with execution_context(runtime.lease):
        await journal.begin("turn", "segment")
    original = runtime.coordination.coordination._check

    async def rejected(session, lease, *, lock):
        await original(session, lease, lock=lock)
        if not lock:
            raise CoordinationConflict("Injected fence rejection before commit")

    monkeypatch.setattr(runtime.coordination.coordination, "_check", rejected)
    with (
        execution_context(runtime.lease),
        pytest.raises(CoordinationConflict, match="Injected"),
    ):
        await journal.close("turn", "segment")
    async with runtime.coordination.database.session() as session:
        assert (
            await session.get(
                RuntimeResourceRow, journal.resource_id("turn", "segment")
            )
        ).state == "active"
    assert await runtime.coordination.coordination.stream_head("turn") is None
    monkeypatch.setattr(runtime.coordination.coordination, "_check", original)
    with execution_context(runtime.lease):
        await journal.close("turn", "segment")
    async with runtime.coordination.database.session() as session:
        assert (
            await session.get(
                RuntimeResourceRow, journal.resource_id("turn", "segment")
            )
        ).state == "closed"
    assert (
        await runtime.coordination.coordination.stream_head("turn")
    ).event_type == SEGMENT_END
