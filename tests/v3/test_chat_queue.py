from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import asyncio
import pytest
from fastapi import HTTPException
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


async def setup_queue(tmp_path):
    store = NebulaStore(tmp_path / "queue.db")
    (await store.create(Engagement(id="p", name="Project")))
    profile = await store.create(_profile(local=True))
    (
        await store.create(
            ChatSession(
                id="s",
                engagement_id="p",
                provider_profile_id=profile.id,
                model="model-a",
                title="Queued work",
            )
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


async def enqueue(
    service: ChatQueueService, request, revision=0, key="first", **options
):
    return await service.write(
        "s",
        QueueWrite(
            action="enqueue",
            expected_revision=revision,
            idempotency_key=key,
            request=request,
            **options,
        ),
    )


async def test_revision_idempotency_order_and_import(tmp_path):
    store, service, request = await setup_queue(tmp_path)
    queue = await enqueue(service, request, paused=True)
    assert (await enqueue(service, request)).revision == queue.revision
    with pytest.raises(ConflictError):
        (await enqueue(service, request.model_copy(update={"include_knowledge": True})))
    with pytest.raises(ConflictError):
        (await enqueue(service, request, key="stale"))
    queue = await enqueue(
        service, request, queue.revision, "second", imported_uncertain=True
    )
    assert queue.paused and queue.items[-1]["status"] == "needs_review"
    queue = await service.write(
        "s",
        QueueWrite(
            action="reorder",
            expected_revision=queue.revision,
            order=[item["id"] for item in reversed(queue.items)],
        ),
    )
    assert queue.items[0]["key"] == "second"
    (await store.delete_chat_session("s"))
    assert (await store.count(ChatQueue)) == 0


async def test_enqueue_refuses_project_relink_before_queue_admission(tmp_path):
    store, service, request = await setup_queue(tmp_path)
    project = await store.get(Engagement, "p")
    new = tmp_path / "relinked"
    new.mkdir()
    original_get = store.get

    async def read_then_relink(model, entity_id):
        value = await original_get(model, entity_id)
        if model is Engagement:
            store.get = original_get
            (
                await store.relink_project_workspace(
                    project.id, str(new), expected_revision=project.revision
                )
            )
        return value

    store.get = read_then_relink
    with pytest.raises(ConflictError, match="Project workspace changed"):
        (await enqueue(service, request))
    assert (await store.count(ChatQueue)) == 0
    assert (await store.get(Engagement, project.id)).workspace_path == str(new)


async def test_claim_is_transactional_and_stale_pause_rolls_back(tmp_path):
    store, service, request = await setup_queue(tmp_path)
    queue = await enqueue(service, request)
    items = queue.items
    items[0]["status"] = "claiming"
    queue = await store.update(
        ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
    )
    claim = (queue.id, queue.revision, items[0]["id"])
    (
        await service.write(
            "s", QueueWrite(action="pause", expected_revision=queue.revision)
        )
    )
    with pytest.raises(ConflictError):
        async with store.transaction() as tx:
            (
                await tx.add(
                    ChatTurn(
                        id="uncommitted",
                        engagement_id="p",
                        session_id="s",
                        model="model-a",
                        provider_profile_id="provider-a",
                    )
                )
            )
            (await link_queue_turn(tx, claim, "uncommitted"))
    assert (await store.count(ChatTurn)) == 0


async def test_core_dispatches_sequentially_without_any_browser(tmp_path):
    store, service, request = await setup_queue(tmp_path)

    async def run():
        queue = await enqueue(service, request)
        (await enqueue(service, request, queue.revision, "second"))
        for _ in range(100):
            await service.step((await service.get("s")))
            if all(
                item["status"] == "complete" for item in (await service.get("s")).items
            ):
                break
            await asyncio.sleep(0.01)
        queue = await service.get("s")
        assert [item["status"] for item in queue.items] == ["complete", "complete"], (
            queue.items
        )
        assert len({item["turn_id"] for item in queue.items}) == 2
        assert (await store.count(ChatTurn)) == 2
        assert (await store.count(ChatMessage)) == 4
        await service.chat.shutdown()

    (await run())


async def test_follow_up_runs_on_the_model_and_effort_picked_after_it_was_queued(
    tmp_path,
):
    store, service, request = await setup_queue(tmp_path)
    profile = await store.get(ProviderProfile, request.provider_id)
    profile = await store.update(
        ProviderProfile,
        profile.id,
        {"model_allowlist": ["model-a", "model-b"]},
        expected_revision=profile.revision,
    )
    provider = service.chat.provider_factory(profile)
    provider.config.model_allowlist.append("model-b")

    async def run():
        (
            await enqueue(
                service, request.model_copy(update={"reasoning_effort": "high"})
            )
        )
        # The operator switches while the follow-up waits; it is the next turn.
        session = await store.get(ChatSession, "s")
        service.chat.apply_runtime_switch(
            "s",
            ChatRuntimeSwitchRequest(
                provider_id=profile.id,
                model="model-b",
                expected_session_revision=session.revision,
            ),
        )
        session = await store.get(ChatSession, "s")
        (
            await store.update(
                ChatSession,
                session.id,
                {"metadata": {**session.metadata, "reasoning_effort": "low"}},
                expected_revision=session.revision,
            )
        )
        for _ in range(100):
            await service.step((await service.get("s")))
            if (await service.get("s")).items[0]["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        item = (await service.get("s")).items[0]
        assert item["status"] == "complete", item
        assert (await store.get(ChatTurn, item["turn_id"])).model == "model-b"
        answered = [r for r in provider.requests if not r.metadata.get("operation")]
        assert [(r.model, r.reasoning_effort) for r in answered] == [("model-b", "low")]
        await service.chat.shutdown()

    (await run())


async def test_follow_up_is_reviewed_when_the_conversation_moves_provider(tmp_path):
    store, service, request = await setup_queue(tmp_path)
    other = await store.create(
        _profile(local=True).model_copy(update={"id": "provider-b", "name": "B"})
    )

    async def run():
        (await enqueue(service, request))
        session = await store.get(ChatSession, "s")
        (
            await store.update(
                ChatSession,
                session.id,
                {"provider_profile_id": other.id},
                expected_revision=session.revision,
            )
        )
        await service.step((await service.get("s")))
        queue = await service.get("s")
        assert queue.paused
        assert queue.items[0]["status"] == "needs_review"
        assert "another provider" in queue.items[0]["detail"]
        assert (await store.count(ChatTurn)) == 0

    (await run())


async def test_restart_and_capability_change_never_blindly_replay(tmp_path):
    store, service, request = await setup_queue(tmp_path)

    async def run():
        queue = await enqueue(service, request)
        items = queue.items
        items[0]["status"] = "claiming"
        queue = await store.update(
            ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
        )
        await service.step(queue, recovering=True)
        assert (await service.get("s")).items[0]["status"] == "needs_review"
        assert (await store.count(ChatTurn)) == 0
        queue = await service.get("s")
        queue = await service.write(
            "s",
            QueueWrite(
                action="retry",
                item_id=queue.items[0]["id"],
                expected_revision=queue.revision,
            ),
        )
        from nebula.v3.domain import ProviderProfile

        profile = await store.get(ProviderProfile, request.provider_id)
        (
            await store.update(
                ProviderProfile,
                profile.id,
                {"enabled": False},
                expected_revision=profile.revision,
            )
        )
        queue = await service.write(
            "s", QueueWrite(action="resume", expected_revision=queue.revision)
        )
        await service.step(queue)
        assert (await service.get("s")).items[-1]["status"] == "needs_review"
        assert (await store.count(ChatTurn)) == 0

    (await run())


async def test_two_devices_cannot_overwrite_same_revision(tmp_path):

    store, service, request = await setup_queue(tmp_path)
    queue = await enqueue(service, request, paused=True)

    async def update(key):
        try:
            return await enqueue(service, request, queue.revision, key)
        except ConflictError:
            return None

    results = list(
        (await asyncio.gather(*(update(_item) for _item in ["device-a", "device-b"])))
    )
    assert sum(result is not None for result in results) == 1
    assert len((await service.get("s")).items) == 2


async def test_pending_approval_failure_and_revocation_pause_dispatch(tmp_path):
    from datetime import timedelta
    from nebula.v3.domain import PairedDeviceSession, utc_now

    store, service, request = await setup_queue(tmp_path)

    async def run():
        queue = await enqueue(service, request)
        turn = await store.create(
            ChatTurn(
                engagement_id="p",
                session_id="s",
                model="model-a",
                provider_profile_id="provider-a",
                status="waiting_approval",
            )
        )
        await service.step(queue)
        assert (await service.get("s")).items[0]["status"] == "queued"
        (
            await store.update(
                ChatTurn, turn.id, {"status": "failed"}, expected_revision=turn.revision
            )
        )
        await service.step((await service.get("s")))
        assert (await service.get("s")).paused
        queue = await service.get("s")
        queue = await service.write(
            "s", QueueWrite(action="clear", expected_revision=queue.revision)
        )
        device = await store.create(
            PairedDeviceSession(
                name="Revoked phone",
                token_sha256="0" * 64,
                csrf_sha256="1" * 64,
                idle_expires_at=utc_now() + timedelta(days=1),
                absolute_expires_at=utc_now() + timedelta(days=2),
                revoked_at=utc_now(),
            )
        )
        queue = await service.write(
            "s",
            QueueWrite(
                action="enqueue",
                expected_revision=queue.revision,
                idempotency_key="revoked",
                request=request,
            ),
            device.id,
        )
        queue = await service.write(
            "s", QueueWrite(action="resume", expected_revision=queue.revision)
        )
        await service.step(queue)
        assert (await service.get("s")).items[-1]["status"] == "needs_review"
        assert (await store.count(ChatTurn)) == 1

    (await run())


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
async def test_restart_after_durable_turn_creation_reconciles_without_replay(
    tmp_path, status, expected
):
    store, service, request = await setup_queue(tmp_path)
    queue = await enqueue(service, request)
    turn = await store.create(
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
    queue = await store.update(
        ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
    )
    (await service.step(queue, recovering=True))
    assert (await service.get("s")).items[0]["status"] == expected
    if status == "cancelled":
        assert (await service.get("s")).paused
        (await service.step((await service.get("s"))))
        assert (await service.get("s")).items[0]["status"] == "cancelled"
    if status == "waiting_callback":
        # The background command's results webhook finishes this same turn later.
        assert not (await service.get("s")).paused
        (
            await store.update(
                ChatTurn,
                turn.id,
                {"status": "complete"},
                expected_revision=turn.revision,
            )
        )
        (await service.step((await service.get("s"))))
        assert (await service.get("s")).items[0]["status"] == "complete"
    assert (await store.count(ChatTurn)) == 1


async def test_core_update_reclaimed_provider_turn_keeps_linked_follow_up_running(
    tmp_path,
):
    from nebula.v3.providers import ModelRequest, ModelStreamEvent, StreamEventType

    class WaitingProvider(FakeProvider):
        async def stream(self, request):
            del request
            yield ModelStreamEvent(type=StreamEventType.STARTED)
            await asyncio.Event().wait()

    async def scenario():
        store, service, request = await setup_queue(tmp_path)
        profile = await store.get(ProviderProfile, request.provider_id)
        service.chat.provider_factory = lambda _: WaitingProvider(
            profile.id, local=True
        )
        queue = await enqueue(service, request)
        reason = "Core stopped before this response completed. Review and resume it."
        turn = await store.create(
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
        (
            await store.update(
                ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
            )
        )

        await service.chat.startup()
        assert service.chat.resume_turns_stopped_by_core() == [turn.id]
        await service.startup()
        linked = await service.get("s")
        assert linked.items[0]["status"] == "sending"
        assert not linked.paused
        assert service.chat.has_active_provider_turn(turn.id)

        current = await store.get(ChatTurn, turn.id)
        (
            await store.update(
                ChatTurn,
                turn.id,
                {"status": "complete"},
                expected_revision=current.revision,
            )
        )
        await service.step((await service.get("s")))
        settled = await service.get("s")
        assert settled.items[0]["status"] == "complete"
        assert not settled.paused
        assert (await store.count(ChatTurn)) == 1
        await service.shutdown()
        await service.chat.shutdown()

    (await scenario())


async def test_harness_queue_uses_durable_turn_and_selected_context(tmp_path):
    import hashlib
    from tests.v3.test_harnesses import _runtime
    from nebula.v3.domain import HarnessTurn

    async def run():
        store, project, profile, _, _, runtime = await _runtime(tmp_path)
        chat, _, first = await runtime.prepare_chat(
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
        queue = await service.write(
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
            await service.step((await service.get(chat.id)))
            if (await service.get(chat.id)).items[0]["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        item = (await service.get(chat.id)).items[0]
        assert item["status"] == "complete", item
        assert (
            selected in (await store.get(HarnessTurn, item["harness_turn_id"])).prompt
        )
        assert (await store.get(ChatTurn, item["turn_id"])).harness_turn_id == item[
            "harness_turn_id"
        ]
        await runtime.shutdown()

    (await run())


async def test_queued_butler_follow_up_dispatches_after_worker_reconnect(tmp_path):
    from tests.v3.test_harnesses import _runtime
    from nebula.v3.domain import HarnessSession, HarnessTurn

    async def run():
        store, project, profile, _, _, runtime = await _runtime(tmp_path)
        chat, _, first = await runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model=None,
            prompt="First Butler question",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            control_assistant=True,
        )
        await runtime.start_chat_turn(first.id)
        service = ChatQueueService(store, None, runtime)
        queued = await service.write(
            chat.id,
            QueueWrite(
                action="enqueue",
                expected_revision=0,
                idempotency_key="butler-follow-up",
                request=ChatCompletionRequest(
                    backend="harness",
                    control_assistant=True,
                    harness_profile_id=profile.id,
                    engagement_id=project.id,
                    session_id=chat.id,
                    model=chat.model,
                    messages=[{"role": "user", "content": "Second Butler question"}],
                    include_knowledge=False,
                ),
            ),
        )
        assert queued.items[0]["status"] == "queued"
        # A new drainer must be able to use the saved request without browser state.
        reconnected = ChatQueueService(store, None, runtime)
        for _ in range(100):
            await reconnected.step((await reconnected.get(chat.id)))
            if (await reconnected.get(chat.id)).items[0]["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        item = (await reconnected.get(chat.id)).items[0]
        assert item["status"] == "complete", item
        assert not (await reconnected.get(chat.id)).paused
        turn = await store.get(HarnessTurn, item["harness_turn_id"])
        assert "You are Butler, the second-in-command" in turn.prompt
        assert (await store.get(HarnessSession, turn.harness_session_id)).metadata[
            "control_assistant"
        ] is True
        assert (await store.get(ChatTurn, item["turn_id"])).status.value == "complete"
        assert len((await store.find_entities(ChatTurn, {"session_id": chat.id}))) == 2
        await runtime.shutdown()

    (await run())


async def test_butler_queue_rejects_ordinary_follow_up_at_enqueue(tmp_path):
    from tests.v3.test_harnesses import _runtime

    store, project, profile, _, _, runtime = await _runtime(tmp_path)
    chat, _, _ = await runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model=None,
        prompt="First Butler question",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        control_assistant=True,
    )
    service = ChatQueueService(store, None, runtime)
    with pytest.raises(HTTPException, match="Butler conversation"):
        (
            await service.write(
                chat.id,
                QueueWrite(
                    action="enqueue",
                    expected_revision=0,
                    idempotency_key="wrong-assistant",
                    request=ChatCompletionRequest(
                        backend="harness",
                        harness_profile_id=profile.id,
                        engagement_id=project.id,
                        session_id=chat.id,
                        model=chat.model,
                        messages=[{"role": "user", "content": "Wrong assistant mode"}],
                    ),
                ),
            )
        )
    assert (await service.get(chat.id)).items == []


async def test_harness_follow_up_can_carry_a_newly_picked_model(tmp_path):
    from tests.v3.test_harnesses import _runtime

    async def run():
        store, project, profile, _, _, runtime = await _runtime(tmp_path)
        chat, _, first = await runtime.prepare_chat(
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
        queue = await service.write(
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
            await service.step((await service.get(chat.id)))
            if (await service.get(chat.id)).items[0]["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        assert queue.items[0]["status"] == "queued"
        item = (await service.get(chat.id)).items[0]
        assert item["status"] == "complete", item
        assert (await store.get(ChatSession, chat.id)).model == "test-model-2"
        await runtime.shutdown()

    (await run())


async def test_saved_cancelled_review_is_reconciled_without_replay(tmp_path):
    store, service, request = await setup_queue(tmp_path)
    queue = await enqueue(service, request)
    turn = await store.create(
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
    queue = await store.update(
        ChatQueue,
        queue.id,
        {"items": items, "paused": True},
        expected_revision=queue.revision,
    )
    (await service.step(queue, recovering=True))
    assert (await service.get("s")).items[0]["status"] == "cancelled"
    assert (await service.get("s")).paused
    assert (await store.count(ChatTurn)) == 1


async def test_saved_uncertain_review_is_reconciled_when_its_turn_completes(tmp_path):
    store, service, request = await setup_queue(tmp_path)
    queue = await enqueue(service, request)
    turn = await store.create(
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
    queue = await store.update(
        ChatQueue,
        queue.id,
        {"items": items, "paused": True},
        expected_revision=queue.revision,
    )
    (await service.step(queue))
    assert (await service.get("s")).items[0]["status"] == "needs_review"
    (
        await store.update(
            ChatTurn, turn.id, {"status": "complete"}, expected_revision=turn.revision
        )
    )
    (await service.step((await service.get("s"))))
    item = (await service.get("s")).items[0]
    assert item["status"] == "complete", item
    assert "uncertain" not in (item.get("detail") or "")
    assert (await store.count(ChatTurn)) == 1


async def test_follow_up_losing_a_race_to_a_direct_send_waits_instead_of_parking(
    tmp_path,
):
    store, service, request = await setup_queue(tmp_path)

    async def run():
        queue = await enqueue(service, request)
        real_prepare = service.chat.prepare_async
        direct = {}

        async def prepare_after_direct_send(request):
            # The operator's direct Send persists its turn while the drainer prepares.
            if not direct:
                direct["turn"] = await store.create(
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
        queue = await service.get("s")
        assert queue.items[0]["status"] == "queued", queue.items
        assert not queue.paused
        assert (await store.count(ChatTurn)) == 1
        await service.step(queue)
        assert (await service.get("s")).items[0]["status"] == "queued"
        turn = direct["turn"]
        (
            await store.update(
                ChatTurn,
                turn.id,
                {"status": "complete"},
                expected_revision=turn.revision,
            )
        )
        for _ in range(100):
            await service.step((await service.get("s")))
            if (await service.get("s")).items[0]["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        item = (await service.get("s")).items[0]
        assert item["status"] == "complete", item
        assert item["turn_id"] != turn.id
        assert (await store.count(ChatTurn)) == 2
        await service.chat.shutdown()

    (await run())


async def test_orphaned_claim_is_reviewed_on_the_next_poll_without_a_restart(tmp_path):
    store, service, request = await setup_queue(tmp_path)

    async def run():
        queue = await enqueue(service, request)

        async def explode(request):
            raise RuntimeError("provider unavailable")

        service.chat.prepare_async = explode
        real_review = service.review
        lost = []

        async def review_losing_once(queue, item_id, detail):
            if not lost:
                lost.append(item_id)
                raise ConflictError("Queue changed on another device")
            return await real_review(queue, item_id, detail)

        service.review = review_losing_once
        with pytest.raises(ConflictError):
            await service.step(queue)
        assert (await service.get("s")).items[0]["status"] == "claiming"
        for _ in range(3):
            await service.step((await service.get("s")))
        queue = await service.get("s")
        assert queue.items[0]["status"] == "needs_review", queue.items
        assert queue.paused
        assert (await store.count(ChatTurn)) == 0

    (await run())


async def test_stuck_claim_can_be_removed_or_cleared_but_not_while_dispatching(
    tmp_path,
):
    store, service, request = await setup_queue(tmp_path)

    async def run():
        queue = await enqueue(service, request)
        items = queue.items
        items[0]["status"] = "claiming"
        queue = await store.update(
            ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
        )
        queue = await service.write(
            "s",
            QueueWrite(
                action="remove",
                item_id=items[0]["id"],
                expected_revision=queue.revision,
            ),
        )
        assert queue.items[0]["status"] == "cancelled"
        queue = await enqueue(service, request, queue.revision, "second")
        items = queue.items
        items[-1]["status"] = "claiming"
        queue = await store.update(
            ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
        )
        queue = await service.write(
            "s", QueueWrite(action="clear", expected_revision=queue.revision)
        )
        assert [item["status"] for item in queue.items] == ["cancelled", "cancelled"]

        queue = await enqueue(service, request, queue.revision, "third")
        gate = asyncio.Event()
        real_prepare = service.chat.prepare_async

        async def prepare_when_released(request):
            await gate.wait()
            return await real_prepare(request)

        service.chat.prepare_async = prepare_when_released
        dispatch = asyncio.create_task(service.step(queue))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if (await service.get("s")).items[-1]["status"] == "claiming":
                break
        queue = await service.get("s")
        assert queue.items[-1]["status"] == "claiming"
        with pytest.raises(ConflictError):
            (
                await service.write(
                    "s",
                    QueueWrite(
                        action="remove",
                        item_id=queue.items[-1]["id"],
                        expected_revision=queue.revision,
                    ),
                )
            )
        gate.set()
        await dispatch
        for _ in range(100):
            await service.step((await service.get("s")))
            if (await service.get("s")).items[-1]["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        assert (await service.get("s")).items[-1]["status"] == "complete"
        await service.chat.shutdown()

    (await run())


pytestmark = pytest.mark.anyio
