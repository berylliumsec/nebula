"""Resume receipts expose their own segment, including through API replacement."""

import json

import httpx
import pytest

from nebula.v3.api_worker import create_api_worker
from nebula.v3.database import Database, RuntimeChatBindingRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import ChatSession, ChatTurn
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_chat_acceptance import CHAT_RESUME_COMMAND, bind_chat_command
from nebula.v3.runtime_chat_stream import RuntimeChatJournal
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_delivery import HttpIntent
from nebula.v3.storage import NebulaStore
from tests.v3.test_execution_worker import runtime as runtime
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def resume_intent(body=b"", path="/api/v1/chat/turns/turn/resume"):
    return HttpIntent.capture("POST", path, b"", [], body)


@pytest.fixture
async def resume(runtime):
    await runtime.store.create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Synthetic",
            model="model-a",
            provider_profile_id="provider",
        )
    )
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
        await journal.publish(
            "turn", [("paused", {"type": "paused", "detail": "Earlier approval"})]
        )
        await journal.close("turn", "old")
    receipt = await runtime.delivery.accept(
        resume_intent(),
        project_id="project",
        actor_id="operator",
        idempotency_key="resume",
        chat_resume=True,
    )
    await runtime.coordination.coordination.claim_next(runtime.lease, lane="work")
    with execution_context(runtime.lease):
        async with runtime.application_store.database.session() as session:
            await bind_chat_command(
                session,
                command_id=receipt.command_id,
                turn_id="turn",
                session_id="chat",
                name=CHAT_RESUME_COMMAND,
            )
    return journal, receipt


async def view(runtime, receipt, *, after=0):
    async def forbidden(*args):
        raise AssertionError("API replay must not contact execution")

    store = NebulaStore(
        Database(
            runtime.coordination.database.url,
            bootstrap=False,
            pool_settings=PoolSettings(5, 12, 3, 5),
        )
    )
    app = create_api_worker(
        store,
        runtime.delivery,
        operator_token="fixture-token",
        project_for=forbidden,
        upstream_for=forbidden,
        command_start_timeout=0.01,
        poll_interval=0.01,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            return await client.get(
                f"/api/v1/runtime/requests/{receipt.command_id}/response?after={after}",
                headers={"Authorization": "Bearer fixture-token"},
            )


async def test_pending_resume_never_returns_an_earlier_pause(runtime, resume):
    journal, receipt = resume
    binding = await runtime.delivery.chat_binding("operator", receipt.command_id)
    assert not binding.ready and binding.stream_after is None
    response = await view(runtime, receipt)
    assert response.status_code == 202 and "Earlier approval" not in response.text
    # A failed start must return its actual error even when old frames exist.
    await runtime.delivery.append(
        runtime.lease,
        receipt.command_id,
        "head",
        json.dumps(
            {"status": 409, "headers": [["content-type", "application/json"]]}
        ).encode(),
    )
    await runtime.delivery.append(
        runtime.lease,
        receipt.command_id,
        "body",
        b'{"detail":"Resume requires reconciliation"}',
    )
    await runtime.delivery.complete(runtime.lease, receipt.command_id, status_code=409)
    response = await view(runtime, receipt)
    assert response.status_code == 409 and "requires reconciliation" in response.text


async def test_resume_cursor_and_initial_frame_commit_together(
    runtime, resume, monkeypatch
):
    journal, receipt = resume
    original = runtime.coordination.coordination._check

    async def rejected(session, lease, *, lock):
        await original(session, lease, lock=lock)
        if not lock:
            raise CoordinationConflict("Injected final-fence rejection")

    monkeypatch.setattr(runtime.coordination.coordination, "_check", rejected)
    with (
        execution_context(runtime.lease),
        pytest.raises(CoordinationConflict, match="Injected"),
    ):
        await journal.publish(
            "turn", [("queued", {"type": "queued"})], command_id=receipt.command_id
        )
    assert not (
        await runtime.delivery.chat_binding("operator", receipt.command_id)
    ).ready
    assert (await journal.coordination.stream_head("turn")).sequence == 2
    monkeypatch.setattr(runtime.coordination.coordination, "_check", original)
    with execution_context(runtime.lease):
        frames = await journal.publish(
            "turn",
            [
                ("queued", {"type": "queued"}),
                ("delta", {"type": "delta", "delta": "Resumed"}),
            ],
            command_id=receipt.command_id,
        )
        await journal.close("turn", "new")
    binding = await runtime.delivery.chat_binding("operator", receipt.command_id)
    assert binding.ready and binding.stream_after == 2
    # A fresh API reads the receipt's segment, rather than old pause history.
    response = await view(runtime, receipt)
    assert response.status_code == 200 and "Resumed" in response.text
    assert "Earlier approval" not in response.text
    response = await view(runtime, receipt, after=frames[0].sequence)
    assert "queued" not in response.text and "Resumed" in response.text


async def test_existing_producer_attachment_requires_its_resource_and_never_rebinds(
    runtime, resume
):
    journal, receipt = resume
    with execution_context(runtime.lease):
        with pytest.raises(CoordinationConflict, match="another owner"):
            await journal.accept_existing(
                "turn", "missing", command_id=receipt.command_id, after=2
            )
        await journal.begin("turn", "active")
        await journal.publish("turn", [("delta", {"delta": "Existing work"})])
        await journal.accept_existing(
            "turn", "active", command_id=receipt.command_id, after=2
        )
        await journal.accept_existing(
            "turn", "active", command_id=receipt.command_id, after=2
        )
        with pytest.raises(CoordinationConflict, match="immutable"):
            await journal.accept_existing(
                "turn", "active", command_id=receipt.command_id, after=3
            )
        await journal.close("turn", "active")
    response = await view(runtime, receipt)
    assert response.status_code == 200 and "Existing work" in response.text
    async with runtime.coordination.database.session() as session:
        assert (
            await session.get(RuntimeChatBindingRow, receipt.command_id)
        ).stream_after == 2


@pytest.mark.parametrize("typed_first", [False, True])
async def test_resume_negotiation_preserves_original_identity(runtime, typed_first):
    async def accept(typed, body=b""):
        return await runtime.delivery.accept(
            resume_intent(body),
            project_id="project",
            actor_id="operator",
            idempotency_key="resume",
            chat_resume=typed,
        )

    original = await accept(typed_first)
    assert await accept(not typed_first) == original
    with pytest.raises(CoordinationConflict, match="different payload"):
        await accept(not typed_first, b"{}")


@pytest.mark.parametrize(
    "intent",
    [
        resume_intent(b'{"unsafe":true}'),
        resume_intent(path="/api/v1/chat/turns/turn/stop"),
    ],
)
async def test_typed_resume_rejects_other_intents(runtime, intent):
    with pytest.raises(ValueError, match="explicit resume intent"):
        await runtime.delivery.accept(
            intent,
            project_id="project",
            actor_id="operator",
            idempotency_key="invalid",
            chat_resume=True,
        )
