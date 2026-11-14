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
from nebula.v3.runtime_chat_acceptance import (
    CHAT_ACCEPT_COMMAND,
    CHAT_RESUME_COMMAND,
    HTTP_INVOKE_COMMAND,
    bind_chat_command,
)
from nebula.v3.runtime_coordination import CoordinationConflict, LeaseLost
from dataclasses import replace

from nebula.v3.runtime_delivery import (
    DurableHttpRunner,
    HttpIntent,
    typed_chat_command,
)
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


TYPED = frozenset({CHAT_ACCEPT_COMMAND, CHAT_RESUME_COMMAND})


async def cold_start(runtime, request=None, *, key="cold"):
    """A chat start the API accepted while the project had no owner."""
    return await runtime.delivery.accept(
        request or intent(),
        project_id="project",
        actor_id="operator",
        idempotency_key=key,
        chat_start=True,
    )


async def rename(runtime):
    return await runtime.delivery.accept(
        HttpIntent.capture(
            "PATCH",
            "/api/v1/chat-sessions/other",
            b"",
            [(b"content-type", b"application/json")],
            b'{"title": "Renamed while another chat runs"}',
        ),
        project_id="project",
        actor_id="operator",
        idempotency_key="rename",
    )


async def command_row(runtime, command_id):
    async with runtime.coordination.database.session() as session:
        return await session.get(RuntimeCommandRow, command_id)


class KernelLikeApp:
    """Answers a typed chat start at acceptance and streams any other one."""

    def __init__(self):
        self.states = []
        self.finish_stream = asyncio.Event()

    async def __call__(self, scope, receive, send):
        await receive()
        self.states.append(dict(scope["state"]))
        typed = (
            scope["state"]["runtime_chat_acceptance"]
            or scope["state"]["runtime_chat_resume"]
        )
        content_type = b"application/json" if typed else b"text/event-stream"
        await send(
            {
                "type": "http.response.start",
                "status": 202 if typed else 200,
                "headers": [(b"content-type", content_type)],
            }
        )
        if typed or scope["method"] != "POST" or "chat" not in scope["path"]:
            await send({"type": "http.response.body", "body": b"{}"})
            return
        # The HTTP transport follows the turn until it ends.
        await send(
            {"type": "http.response.body", "body": b"data: {}\n\n", "more_body": True}
        )
        await self.finish_stream.wait()
        await send({"type": "http.response.body", "body": b""})


async def test_an_owner_types_a_cold_chat_start_and_frees_the_work_lane(runtime):
    started = await cold_start(runtime)
    renamed = await rename(runtime)
    app = KernelLikeApp()
    runner = DurableHttpRunner(
        runtime.delivery, app, operator_token="synthetic", typed_chat_commands=TYPED
    )

    # The chat start ends at acceptance instead of holding the lane for the
    # whole streamed turn; the rename behind it runs while the turn would.
    assert await asyncio.wait_for(runner.run_one(runtime.lease, lane="work"), 5)
    assert app.states[0]["runtime_chat_acceptance"] is True
    row = await command_row(runtime, started.command_id)
    assert (row.name, row.state) == (CHAT_ACCEPT_COMMAND, "succeeded")
    assert await asyncio.wait_for(runner.run_one(runtime.lease, lane="work"), 5)
    assert (await command_row(runtime, renamed.command_id)).state == "succeeded"
    # Retrying the original request still returns its one receipt.
    assert await cold_start(runtime) == started


async def test_an_owner_without_typed_chat_keeps_the_streamed_transport(runtime):
    started = await cold_start(runtime)
    app = KernelLikeApp()
    runner = DurableHttpRunner(runtime.delivery, app, operator_token="synthetic")
    running = asyncio.create_task(runner.run_one(runtime.lease, lane="work"))
    try:
        await asyncio.sleep(0.3)
        assert not running.done()
        assert app.states[0]["runtime_chat_acceptance"] is False
        assert (await command_row(runtime, started.command_id)).name == (
            HTTP_INVOKE_COMMAND
        )
    finally:
        app.finish_stream.set()
        assert await asyncio.wait_for(running, 5)


async def test_a_cold_resume_is_typed_as_a_resume(runtime):
    request = HttpIntent.capture(
        "POST", "/api/v1/chat/turns/turn-1/resume", b"", [], b""
    )
    started = await cold_start(runtime, request, key="resume")
    app = KernelLikeApp()
    runner = DurableHttpRunner(
        runtime.delivery, app, operator_token="synthetic", typed_chat_commands=TYPED
    )
    assert await asyncio.wait_for(runner.run_one(runtime.lease, lane="work"), 5)
    assert app.states[0]["runtime_chat_resume"] is True
    assert (await command_row(runtime, started.command_id)).name == (
        CHAT_RESUME_COMMAND
    )


async def test_a_typed_cold_chat_start_binds_its_turn(runtime, chat):
    receipt = await cold_start(runtime)
    claimed = await runtime.coordination.coordination.claim_next(
        runtime.lease, lane="work"
    )
    assert claimed is not None and claimed.name == HTTP_INVOKE_COMMAND
    # Untyped, the claimed HTTP request cannot carry a chat binding.
    with pytest.raises(CoordinationConflict, match="does not match"):
        await prepare(runtime, chat, receipt)
    assert await runtime.coordination.coordination.retype_chat_start(
        runtime.lease, receipt.command_id, name=CHAT_ACCEPT_COMMAND
    )
    prepared = await prepare(runtime, chat, receipt)
    async with runtime.coordination.database.session() as session:
        binding = await session.get(RuntimeChatBindingRow, receipt.command_id)
    assert binding is not None and binding.turn_id == prepared.turn.id


async def test_only_its_owner_can_type_a_running_chat_start(runtime):
    coordination = runtime.coordination.coordination
    renamed = await rename(runtime)
    started = await cold_start(runtime)
    # Queued (unclaimed) work is not this owner's to change.
    assert not await coordination.retype_chat_start(
        runtime.lease, started.command_id, name=CHAT_ACCEPT_COMMAND
    )
    with pytest.raises(ValueError, match="typed chat transport"):
        await coordination.retype_chat_start(
            runtime.lease, started.command_id, name=HTTP_INVOKE_COMMAND
        )
    claimed = await coordination.claim_next(runtime.lease, lane="work")
    assert claimed.receipt.command_id == renamed.command_id
    # A running request that is not a chat start keeps its transport.
    assert not await coordination.retype_chat_start(
        runtime.lease, renamed.command_id, name=CHAT_ACCEPT_COMMAND
    )
    with pytest.raises(LeaseLost):
        await coordination.retype_chat_start(
            replace(runtime.lease, fencing_token=runtime.lease.fencing_token + 1),
            renamed.command_id,
            name=CHAT_ACCEPT_COMMAND,
        )
    assert (await command_row(runtime, renamed.command_id)).name == (
        HTTP_INVOKE_COMMAND
    )


@pytest.mark.parametrize(
    ("method", "path", "body", "expected"),
    [
        ("POST", "/api/v1/chat/completions", {"stream": True}, CHAT_ACCEPT_COMMAND),
        (
            "POST",
            "/api/v1/chat/completions",
            {"stream": True, "backend": "provider"},
            CHAT_ACCEPT_COMMAND,
        ),
        ("POST", "/api/v1/chat/completions", {"stream": False}, None),
        (
            "POST",
            "/api/v1/chat/completions",
            {"stream": True, "backend": "harness"},
            None,
        ),
        ("POST", "/api/v1/chat/completions", None, None),
        ("POST", "/api/v1/chat/turns/turn-1/resume", "", CHAT_RESUME_COMMAND),
        ("POST", "/api/v1/chat/turns/turn-1/resume", {"x": 1}, None),
        ("PUT", "/api/v1/chat/completions", {"stream": True}, None),
    ],
)
def test_typed_chat_transport_matches_what_the_api_would_choose(
    method, path, body, expected
):
    raw = (
        b""
        if body == ""
        else b"{not json"
        if body is None
        else json.dumps(body).encode()
    )
    request = HttpIntent.capture(
        method, path, b"", [(b"content-type", b"application/json")], raw
    )
    assert typed_chat_command(request) == expected
