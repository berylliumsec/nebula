import asyncio
import pytest
from tests.v3.test_chat import FakeProvider, _profile
from nebula.v3.chat import ChatService, ChatCompletionRequest, ChatRuntimeSwitchRequest
from nebula.v3.chat_queue import ChatQueueService, QueueWrite, link_queue_turn
from nebula.v3.domain import (
    Engagement,
    ChatSession,
    ChatQueue,
    ChatTurn,
    ChatMessage,
    ProviderProfile,
)
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


def test_follow_up_runs_on_the_model_and_effort_picked_after_it_was_queued(tmp_path):
    store, service, request = setup_queue(tmp_path)
    profile = store.get(ProviderProfile, request.provider_id)
    profile = store.update(
        ProviderProfile,
        profile.id,
        {"model_allowlist": ["model-a", "model-b"]},
        expected_revision=profile.revision,
    )
    provider = service.chat.provider_factory(profile)
    provider.config.model_allowlist.append("model-b")

    async def run():
        enqueue(service, request.model_copy(update={"reasoning_effort": "high"}))
        # The operator switches while the follow-up waits; it is the next turn.
        session = store.get(ChatSession, "s")
        service.chat.apply_runtime_switch(
            "s",
            ChatRuntimeSwitchRequest(
                provider_id=profile.id,
                model="model-b",
                expected_session_revision=session.revision,
            ),
        )
        session = store.get(ChatSession, "s")
        store.update(
            ChatSession,
            session.id,
            {"metadata": {**session.metadata, "reasoning_effort": "low"}},
            expected_revision=session.revision,
        )
        for _ in range(100):
            await service.step(service.get("s"))
            if service.get("s").items[0]["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        item = service.get("s").items[0]
        assert item["status"] == "complete", item
        assert store.get(ChatTurn, item["turn_id"]).model == "model-b"
        answered = [r for r in provider.requests if not r.metadata.get("operation")]
        assert [(r.model, r.reasoning_effort) for r in answered] == [("model-b", "low")]
        await service.chat.shutdown()

    asyncio.run(run())


def test_follow_up_is_reviewed_when_the_conversation_moves_provider(tmp_path):
    store, service, request = setup_queue(tmp_path)
    other = store.create(
        _profile(local=True).model_copy(update={"id": "provider-b", "name": "B"})
    )

    async def run():
        enqueue(service, request)
        session = store.get(ChatSession, "s")
        store.update(
            ChatSession,
            session.id,
            {"provider_profile_id": other.id},
            expected_revision=session.revision,
        )
        await service.step(service.get("s"))
        queue = service.get("s")
        assert queue.paused
        assert queue.items[0]["status"] == "needs_review"
        assert "another provider" in queue.items[0]["detail"]
        assert store.count(ChatTurn) == 0

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
        ("waiting_callback", "sending"),
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
    if status == "waiting_callback":
        # The background command's results webhook finishes this same turn later.
        assert not service.get("s").paused
        store.update(
            ChatTurn, turn.id, {"status": "complete"}, expected_revision=turn.revision
        )
        asyncio.run(service.step(service.get("s")))
        assert service.get("s").items[0]["status"] == "complete"
    assert store.count(ChatTurn) == 1


def test_core_update_reclaimed_provider_turn_keeps_linked_follow_up_running(tmp_path):
    from nebula.v3.providers import ModelRequest, ModelStreamEvent, StreamEventType

    class WaitingProvider(FakeProvider):
        async def stream(self, request):
            del request
            yield ModelStreamEvent(type=StreamEventType.STARTED)
            await asyncio.Event().wait()

    async def scenario():
        store, service, request = setup_queue(tmp_path)
        profile = store.get(ProviderProfile, request.provider_id)
        service.chat.provider_factory = lambda _: WaitingProvider(
            profile.id, local=True
        )
        queue = enqueue(service, request)
        reason = "Core stopped before this response completed. Review and resume it."
        turn = store.create(
            ChatTurn(
                engagement_id="p",
                session_id="s",
                provider_profile_id=profile.id,
                model="model-a",
                status="interrupted",
                error=reason,
                request_snapshot={
                    "model_request": ModelRequest(
                        model="model-a",
                        messages=[{"role": "user", "content": "Continue."}],
                    ).model_dump(mode="json"),
                    "context_usage": {},
                    "recovery": {
                        "required": True,
                        "cause": "core_shutdown",
                        "unknown_tool_call_ids": [],
                        "unknown_hook_execution_ids": [],
                    },
                },
            )
        )
        items = queue.items
        items[0].update(status="sending", turn_id=turn.id)
        store.update(
            ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
        )

        await service.chat.startup()
        assert service.chat.resume_turns_stopped_by_core() == [turn.id]
        await service.startup()
        linked = service.get("s")
        assert linked.items[0]["status"] == "sending"
        assert not linked.paused
        assert service.chat.has_active_provider_turn(turn.id)

        current = store.get(ChatTurn, turn.id)
        store.update(
            ChatTurn,
            turn.id,
            {"status": "complete"},
            expected_revision=current.revision,
        )
        await service.step(service.get("s"))
        settled = service.get("s")
        assert settled.items[0]["status"] == "complete"
        assert not settled.paused
        assert store.count(ChatTurn) == 1
        await service.shutdown()
        await service.chat.shutdown()

    asyncio.run(scenario())


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


def test_harness_follow_up_can_carry_a_newly_picked_model(tmp_path):
    from tests.v3.test_harnesses import _runtime

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
        # Picked while the first turn ran; the harness moves to a new session.
        queue = service.write(
            chat.id,
            QueueWrite(
                action="enqueue",
                expected_revision=0,
                idempotency_key="harness-model",
                request=ChatCompletionRequest(
                    backend="harness",
                    harness_profile_id=profile.id,
                    engagement_id=project.id,
                    session_id=chat.id,
                    model="test-model-2",
                    messages=[{"role": "user", "content": "Continue"}],
                    include_knowledge=False,
                ),
            ),
        )
        for _ in range(100):
            await service.step(service.get(chat.id))
            if service.get(chat.id).items[0]["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        assert queue.items[0]["status"] == "queued"
        item = service.get(chat.id).items[0]
        assert item["status"] == "complete", item
        assert store.get(ChatSession, chat.id).model == "test-model-2"
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


def test_saved_uncertain_review_is_reconciled_when_its_turn_completes(tmp_path):
    store, service, request = setup_queue(tmp_path)
    queue = enqueue(service, request)
    turn = store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            provider_profile_id="provider-a",
            model="model-a",
            status="waiting_callback",
        )
    )
    items = queue.items
    items[0].update(
        status="needs_review",
        turn_id=turn.id,
        detail="Core restarted with a response in progress. Delivery is uncertain; inspect the conversation",
    )
    queue = store.update(
        ChatQueue,
        queue.id,
        {"items": items, "paused": True},
        expected_revision=queue.revision,
    )
    asyncio.run(service.step(queue))
    assert service.get("s").items[0]["status"] == "needs_review"
    store.update(
        ChatTurn, turn.id, {"status": "complete"}, expected_revision=turn.revision
    )
    asyncio.run(service.step(service.get("s")))
    item = service.get("s").items[0]
    assert item["status"] == "complete", item
    assert "uncertain" not in (item.get("detail") or "")
    assert store.count(ChatTurn) == 1


def test_follow_up_losing_a_race_to_a_direct_send_waits_instead_of_parking(tmp_path):
    store, service, request = setup_queue(tmp_path)

    async def run():
        queue = enqueue(service, request)
        real_prepare = service.chat.prepare_async
        direct = {}

        async def prepare_after_direct_send(request):
            # The operator's direct Send persists its turn while the drainer prepares.
            if not direct:
                direct["turn"] = store.create(
                    ChatTurn(
                        engagement_id="p",
                        session_id="s",
                        provider_profile_id="provider-a",
                        model="model-a",
                        status="routing",
                    )
                )
            return await real_prepare(request)

        service.chat.prepare_async = prepare_after_direct_send
        await service.step(queue)
        queue = service.get("s")
        assert queue.items[0]["status"] == "queued", queue.items
        assert not queue.paused
        assert store.count(ChatTurn) == 1
        await service.step(queue)
        assert service.get("s").items[0]["status"] == "queued"
        turn = direct["turn"]
        store.update(
            ChatTurn, turn.id, {"status": "complete"}, expected_revision=turn.revision
        )
        for _ in range(100):
            await service.step(service.get("s"))
            if service.get("s").items[0]["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        item = service.get("s").items[0]
        assert item["status"] == "complete", item
        assert item["turn_id"] != turn.id
        assert store.count(ChatTurn) == 2
        await service.chat.shutdown()

    asyncio.run(run())


def test_orphaned_claim_is_reviewed_on_the_next_poll_without_a_restart(tmp_path):
    store, service, request = setup_queue(tmp_path)

    async def run():
        queue = enqueue(service, request)

        async def explode(request):
            raise RuntimeError("provider unavailable")

        service.chat.prepare_async = explode
        real_review = service.review
        lost = []

        def review_losing_once(queue, item_id, detail):
            if not lost:
                lost.append(item_id)
                raise ConflictError("Queue changed on another device")
            return real_review(queue, item_id, detail)

        service.review = review_losing_once
        with pytest.raises(ConflictError):
            await service.step(queue)
        assert service.get("s").items[0]["status"] == "claiming"
        for _ in range(3):
            await service.step(service.get("s"))
        queue = service.get("s")
        assert queue.items[0]["status"] == "needs_review", queue.items
        assert queue.paused
        assert store.count(ChatTurn) == 0

    asyncio.run(run())


def test_stuck_claim_can_be_removed_or_cleared_but_not_while_dispatching(tmp_path):
    store, service, request = setup_queue(tmp_path)

    async def run():
        queue = enqueue(service, request)
        items = queue.items
        items[0]["status"] = "claiming"
        queue = store.update(
            ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
        )
        queue = service.write(
            "s",
            QueueWrite(
                action="remove",
                item_id=items[0]["id"],
                expected_revision=queue.revision,
            ),
        )
        assert queue.items[0]["status"] == "cancelled"
        queue = enqueue(service, request, queue.revision, "second")
        items = queue.items
        items[-1]["status"] = "claiming"
        queue = store.update(
            ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
        )
        queue = service.write(
            "s", QueueWrite(action="clear", expected_revision=queue.revision)
        )
        assert [item["status"] for item in queue.items] == ["cancelled", "cancelled"]

        queue = enqueue(service, request, queue.revision, "third")
        gate = asyncio.Event()
        real_prepare = service.chat.prepare_async

        async def prepare_when_released(request):
            await gate.wait()
            return await real_prepare(request)

        service.chat.prepare_async = prepare_when_released
        dispatch = asyncio.create_task(service.step(queue))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if service.get("s").items[-1]["status"] == "claiming":
                break
        queue = service.get("s")
        assert queue.items[-1]["status"] == "claiming"
        with pytest.raises(ConflictError):
            service.write(
                "s",
                QueueWrite(
                    action="remove",
                    item_id=queue.items[-1]["id"],
                    expected_revision=queue.revision,
                ),
            )
        gate.set()
        await dispatch
        for _ in range(100):
            await service.step(service.get("s"))
            if service.get("s").items[-1]["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        assert service.get("s").items[-1]["status"] == "complete"
        await service.chat.shutdown()

    asyncio.run(run())
