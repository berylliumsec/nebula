import asyncio
import pytest
from tests.v3.test_chat import FakeProvider, _profile
from nebula.v3.chat import ChatService, ChatCompletionRequest
from nebula.v3.chat_queue import ChatQueueService, QueueWrite, link_queue_turn
from nebula.v3.domain import Engagement, ChatSession, ChatQueue, ChatTurn, ChatMessage
from nebula.v3.storage import NebulaStore, ConflictError


def setup_queue(tmp_path):
    store = NebulaStore(tmp_path / "queue.db")
    store.create(Engagement(id="p", name="Project"))
    profile = store.create(_profile(local=True))
    store.create(
        ChatSession(
            id="s",
            engagement_id="p",
            provider_profile_id=profile.id,
            model="model-a",
            title="Queued work",
        )
    )
    provider = FakeProvider(profile.id, local=True)
    chat = ChatService(store, provider_factory=lambda _: provider)
    service = ChatQueueService(store, chat, None)
    request = ChatCompletionRequest(
        provider_id=profile.id,
        engagement_id="p",
        session_id="s",
        model="model-a",
        messages=[{"role": "user", "content": "First queued task"}],
        include_knowledge=False,
        tools_enabled=False,
    )
    return store, service, request


def enqueue(service, request, revision=0, key="first", **options):
    return service.write(
        "s",
        QueueWrite(
            action="enqueue",
            expected_revision=revision,
            idempotency_key=key,
            request=request,
            **options,
        ),
    )


def test_revision_idempotency_order_and_import(tmp_path):
    store, service, request = setup_queue(tmp_path)
    queue = enqueue(service, request, paused=True)
    assert enqueue(service, request).revision == queue.revision
    with pytest.raises(ConflictError):
        enqueue(service, request.model_copy(update={"include_knowledge": True}))
    with pytest.raises(ConflictError):
        enqueue(service, request, key="stale")
    queue = enqueue(service, request, queue.revision, "second", imported_uncertain=True)
    assert queue.paused and queue.items[-1]["status"] == "needs_review"
    queue = service.write(
        "s",
        QueueWrite(
            action="reorder",
            expected_revision=queue.revision,
            order=[item["id"] for item in reversed(queue.items)],
        ),
    )
    assert queue.items[0]["key"] == "second"
    store.delete_chat_session("s")
    assert store.count(ChatQueue) == 0


def test_claim_is_transactional_and_stale_pause_rolls_back(tmp_path):
    store, service, request = setup_queue(tmp_path)
    queue = enqueue(service, request)
    items = queue.items
    items[0]["status"] = "claiming"
    queue = store.update(
        ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
    )
    claim = (queue.id, queue.revision, items[0]["id"])
    service.write("s", QueueWrite(action="pause", expected_revision=queue.revision))
    with pytest.raises(ConflictError):
        with store.transaction() as tx:
            tx.add(
                ChatTurn(
                    id="uncommitted",
                    engagement_id="p",
                    session_id="s",
                    model="model-a",
                    provider_profile_id="provider-a",
                )
            )
            link_queue_turn(tx, claim, "uncommitted")
    assert store.count(ChatTurn) == 0


def test_core_dispatches_sequentially_without_any_browser(tmp_path):
    store, service, request = setup_queue(tmp_path)

    async def run():
        queue = enqueue(service, request)
        enqueue(service, request, queue.revision, "second")
        for _ in range(100):
            await service.step(service.get("s"))
            if all(item["status"] == "complete" for item in service.get("s").items):
                break
            await asyncio.sleep(0.01)
        queue = service.get("s")
        assert [item["status"] for item in queue.items] == ["complete", "complete"], (
            queue.items
        )
        assert len({item["turn_id"] for item in queue.items}) == 2
        assert store.count(ChatTurn) == 2
        assert store.count(ChatMessage) == 4
        await service.chat.shutdown()

    asyncio.run(run())


def test_restart_and_capability_change_never_blindly_replay(tmp_path):
    store, service, request = setup_queue(tmp_path)

    async def run():
        queue = enqueue(service, request)
        items = queue.items
        items[0]["status"] = "claiming"
        queue = store.update(
            ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
        )
        await service.step(queue, recovering=True)
        assert service.get("s").items[0]["status"] == "needs_review"
        assert store.count(ChatTurn) == 0
        queue = service.get("s")
        queue = service.write(
            "s",
            QueueWrite(
                action="retry",
                item_id=queue.items[0]["id"],
                expected_revision=queue.revision,
            ),
        )
        from nebula.v3.domain import ProviderProfile

        profile = store.get(ProviderProfile, request.provider_id)
        store.update(
            ProviderProfile,
            profile.id,
            {"enabled": False},
            expected_revision=profile.revision,
        )
        queue = service.write(
            "s", QueueWrite(action="resume", expected_revision=queue.revision)
        )
        await service.step(queue)
        assert service.get("s").items[-1]["status"] == "needs_review"
        assert store.count(ChatTurn) == 0

    asyncio.run(run())


def test_two_devices_cannot_overwrite_same_revision(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store, service, request = setup_queue(tmp_path)
    queue = enqueue(service, request, paused=True)

    def update(key):
        try:
            return enqueue(service, request, queue.revision, key)
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["device-a", "device-b"]))
    assert sum(result is not None for result in results) == 1
    assert len(service.get("s").items) == 2


def test_pending_approval_failure_and_revocation_pause_dispatch(tmp_path):
    from datetime import timedelta
    from nebula.v3.domain import PairedDeviceSession, utc_now

    store, service, request = setup_queue(tmp_path)

    async def run():
        queue = enqueue(service, request)
        turn = store.create(
            ChatTurn(
                engagement_id="p",
                session_id="s",
                model="model-a",
                provider_profile_id="provider-a",
                status="waiting_approval",
            )
        )
        await service.step(queue)
        assert service.get("s").items[0]["status"] == "queued"
        store.update(
            ChatTurn, turn.id, {"status": "failed"}, expected_revision=turn.revision
        )
        await service.step(service.get("s"))
        assert service.get("s").paused
        queue = service.get("s")
        queue = service.write(
            "s", QueueWrite(action="clear", expected_revision=queue.revision)
        )
        device = store.create(
            PairedDeviceSession(
                name="Revoked phone",
                token_sha256="0" * 64,
                csrf_sha256="1" * 64,
                idle_expires_at=utc_now() + timedelta(days=1),
                absolute_expires_at=utc_now() + timedelta(days=2),
                revoked_at=utc_now(),
            )
        )
        queue = service.write(
            "s",
            QueueWrite(
                action="enqueue",
                expected_revision=queue.revision,
                idempotency_key="revoked",
                request=request,
            ),
            device.id,
        )
        queue = service.write(
            "s", QueueWrite(action="resume", expected_revision=queue.revision)
        )
        await service.step(queue)
        assert service.get("s").items[-1]["status"] == "needs_review"
        assert store.count(ChatTurn) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,expected",
    [
        ("routing", "needs_review"),
        ("complete", "complete"),
        ("cancelled", "cancelled"),
        ("interrupted", "needs_review"),
    ],
)
def test_restart_after_durable_turn_creation_reconciles_without_replay(
    tmp_path, status, expected
):
    store, service, request = setup_queue(tmp_path)
    queue = enqueue(service, request)
    turn = store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            provider_profile_id="provider-a",
            model="model-a",
            status=status,
        )
    )
    items = queue.items
    items[0].update(status="sending", turn_id=turn.id)
    queue = store.update(
        ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
    )
    asyncio.run(service.step(queue, recovering=True))
    assert service.get("s").items[0]["status"] == expected
    if status == "cancelled":
        assert service.get("s").paused
        asyncio.run(service.step(service.get("s")))
        assert service.get("s").items[0]["status"] == "cancelled"
    assert store.count(ChatTurn) == 1


def test_harness_queue_uses_durable_turn_and_selected_context(tmp_path):
    import hashlib
    from tests.v3.test_harnesses import _runtime
    from nebula.v3.domain import HarnessTurn

    async def run():
        store, project, profile, _, _, runtime = _runtime(tmp_path)
        chat, _, first = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model=None,
            prompt="Hello",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
        )
        await runtime.start_chat_turn(first.id)
        service = ChatQueueService(store, None, runtime)
        selected = "Exact selected queue context"
        request = ChatCompletionRequest(
            backend="harness",
            harness_profile_id=profile.id,
            engagement_id=project.id,
            session_id=chat.id,
            model=chat.model,
            messages=[{"role": "user", "content": "Use this context"}],
            context_attachments=[
                {
                    "source_kind": "assistant_message",
                    "source_id": "source",
                    "source_label": "Selection",
                    "text": selected,
                    "sha256": hashlib.sha256(selected.encode()).hexdigest(),
                }
            ],
            include_knowledge=False,
        )
        queue = service.write(
            chat.id,
            QueueWrite(
                action="enqueue",
                expected_revision=0,
                idempotency_key="harness",
                request=request,
            ),
        )
        await service.step(queue)
        for _ in range(100):
            await service.step(service.get(chat.id))
            if service.get(chat.id).items[0]["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        item = service.get(chat.id).items[0]
        assert item["status"] == "complete", item
        assert selected in store.get(HarnessTurn, item["harness_turn_id"]).prompt
        assert (
            store.get(ChatTurn, item["turn_id"]).harness_turn_id
            == item["harness_turn_id"]
        )
        await runtime.shutdown()

    asyncio.run(run())


def test_saved_cancelled_review_is_reconciled_without_replay(tmp_path):
    store, service, request = setup_queue(tmp_path)
    queue = enqueue(service, request)
    turn = store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            provider_profile_id="provider-a",
            model="model-a",
            status="cancelled",
        )
    )
    items = queue.items
    items[0].update(status="needs_review", turn_id=turn.id)
    queue = store.update(
        ChatQueue,
        queue.id,
        {"items": items, "paused": True},
        expected_revision=queue.revision,
    )
    asyncio.run(service.step(queue, recovering=True))
    assert service.get("s").items[0]["status"] == "cancelled"
    assert service.get("s").paused
    assert store.count(ChatTurn) == 1
