"""Independent API connections and inert ASGI mutations use durable delivery."""

import asyncio
from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from nebula.v3.database import RuntimeCommandRow, RuntimeDeliveryRow
from nebula.v3.domain import PairedDeviceSession, utc_now
from nebula.v3.execution_scope import current_execution_lease
from nebula.v3.memory_actor import memory_actor
from nebula.v3.runtime_coordination import CoordinationConflict, LEASE_MS, LeaseLost
from nebula.v3.runtime_delivery import DurableHttpRunner, HttpIntent, RuntimeDelivery
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.storage import NotFoundError
from tests.v3.test_runtime_coordination import cluster as cluster
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def intent(body=b'{"message":"private synthetic text"}'):
    return HttpIntent.capture(
        "POST",
        "/api/v1/synthetic",
        b"",
        [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer token-never-persist"),
            (b"cookie", b"nebula_device=never-persist-cookie"),
            (b"x-nebula-csrf", b"never-persist-csrf"),
        ],
        body,
    )


async def accept(delivery, *, key="request", actor="operator", body=None, lane="work"):
    return await delivery.accept(
        intent() if body is None else intent(body),
        project_id="project",
        actor_id=actor,
        idempotency_key=key,
        lane=lane,
    )


async def reply(_scope, _receive, send):
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream"),
                (b"set-cookie", b"one=synthetic"),
                (b"set-cookie", b"two=synthetic"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b"synthetic response"})


async def test_lost_response_retry_and_api_replacement_preserve_one_mutation(
    cluster, tmp_path
):
    (first, second), _, _ = cluster
    vault = RuntimeVault(tmp_path / "payloads")
    api = RuntimeDelivery(first, vault)
    replacement = RuntimeDelivery(second, RuntimeVault(vault.root))
    accepted = await accept(api)
    lease = await first.claim_project("project", "old")
    entered, finish = asyncio.Event(), asyncio.Event()
    invocations = []

    async def app(scope, receive, send):
        invocations.append(
            (scope, await receive(), current_execution_lease(), memory_actor.get())
        )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"data: first\n\n",
                "more_body": True,
            }
        )
        entered.set()
        await finish.wait()
        await send({"type": "http.response.body", "body": b"data: done\n\n"})

    runner = DurableHttpRunner(api, app, operator_token="synthetic-private-token")
    worker = asyncio.create_task(runner.run_one(lease, lane="work"))
    await asyncio.wait_for(entered.wait(), 5)
    first_page = await api.page("operator", accepted.command_id)
    assert first_page.state == "running"
    assert await accept(replacement) == accepted
    # The first viewer disappears. No API-owned task runs or owns the mutation.
    finish.set()
    assert await asyncio.wait_for(worker, 5)
    resumed = await replacement.page(
        "operator", accepted.command_id, after=first_page.frames[-1].sequence
    )
    assert resumed.state == "succeeded"
    assert [(frame.kind, frame.content) for frame in resumed.frames] == [
        ("body", b"data: done\n\n"),
        ("end", b""),
    ]
    assert len(invocations) == 1
    scope, request, inherited, actor = invocations[0]
    assert inherited == lease and actor == "operator"
    assert request["body"] == b'{"message":"private synthetic text"}'
    assert (b"cookie", b"nebula_device=never-persist-cookie") not in scope["headers"]
    assert not await runner.run_one(lease, lane="work")
    async with first.database.session() as session:
        rows = list(await session.scalars(select(RuntimeCommandRow)))
        frames = list(await session.scalars(select(RuntimeDeliveryRow)))
    persisted = json.dumps([row.payload for row in rows] + [row.result for row in rows])
    assert "private synthetic text" not in persisted
    assert "never-persist" not in persisted
    assert len(rows) == 1
    assert [
        (frame.sequence, frame.kind)
        for frame in sorted(frames, key=lambda row: row.sequence)
    ] == [
        (1, "head"),
        (2, "body"),
        (3, "body"),
        (4, "end"),
    ]


async def test_changed_intent_conflicts_and_another_principal_cannot_read_receipt(
    cluster, tmp_path
):
    (first, second), _, _ = cluster
    api = RuntimeDelivery(first, RuntimeVault(tmp_path / "private"))
    other = RuntimeDelivery(second, RuntimeVault(api.vault.root))
    receipt = await accept(api)
    with pytest.raises(CoordinationConflict, match="different payload"):
        await accept(other, body=b'{"message":"different"}')
    with pytest.raises(NotFoundError):
        await other.page("device:unrelated", receipt.command_id)
    with pytest.raises(ValueError, match="principal"):
        await accept(api, actor="Bearer pretend-token")


async def test_cancelled_effect_is_uncertain_and_controls_bypass_held_work(
    cluster, tmp_path
):
    (first, second), _, _ = cluster
    delivery = RuntimeDelivery(first, RuntimeVault(tmp_path / "private"))
    receipt = await accept(delivery)
    await accept(delivery, key="queued-later")
    lease = await first.claim_project("project", "old")
    effect_started = asyncio.Event()
    effects = []

    async def interrupted(_scope, _receive, _send):
        effects.append("inert synthetic effect")
        effect_started.set()
        await asyncio.Event().wait()

    worker = asyncio.create_task(
        DurableHttpRunner(delivery, interrupted, operator_token="synthetic").run_one(
            lease, lane="work"
        )
    )
    await asyncio.wait_for(effect_started.wait(), 5)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert (await delivery.page("operator", receipt.command_id)).state == "uncertain"
    assert await second.claim_next(lease, lane="work") is None
    await accept(delivery, key="operator-stop", lane="control")
    controls = DurableHttpRunner(delivery, reply, operator_token="synthetic")
    assert await controls.run_one(lease, lane="control")
    assert effects == ["inert synthetic effect"]


@pytest.mark.parametrize("invalid", ["revoked", "expired", "deleted"])
async def test_device_is_revalidated_before_queued_work_executes(
    cluster, tmp_path, invalid
):
    (first, _), stores, _clock = cluster
    now = utc_now()
    device = PairedDeviceSession(
        id="device",
        name="Synthetic device",
        token_sha256="a" * 64,
        csrf_sha256="b" * 64,
        last_used_at=now,
        idle_expires_at=now + timedelta(days=1),
        absolute_expires_at=now + timedelta(days=2),
    )
    await stores[0].create(device)
    delivery = RuntimeDelivery(first, RuntimeVault(tmp_path / "private"))
    receipt = await accept(delivery, actor="device:device")
    if invalid == "deleted":
        await stores[0].delete(PairedDeviceSession, device.id)
    else:
        await stores[0].update(
            PairedDeviceSession,
            device.id,
            {
                "revoked_at": now if invalid == "revoked" else None,
                "idle_expires_at": now - timedelta(seconds=1)
                if invalid == "expired"
                else device.idle_expires_at,
            },
        )
    lease = await first.claim_project("project", "old")

    async def forbidden(*_args):
        pytest.fail("a revoked device must not execute its queued request")

    assert await DurableHttpRunner(
        delivery, forbidden, operator_token="synthetic"
    ).run_one(lease, lane="work")
    page = await delivery.page("device:device", receipt.command_id)
    assert page.state == "failed"
    assert json.loads(page.frames[0].content)["status"] == 401


async def test_response_publication_fences_lost_worker_and_preserves_cursor(
    cluster, tmp_path
):
    (first, second), _, clock = cluster
    delivery = RuntimeDelivery(first, RuntimeVault(tmp_path / "private"))
    receipt = await accept(delivery)
    old = await first.claim_project("project", "old")
    await first.claim_next(old, lane="work")
    await delivery.append(
        old, receipt.command_id, "head", b'{"status":200,"headers":[]}'
    )
    clock[0] += LEASE_MS + 1
    await second.register_runtime("replacement", "new-release")
    await second.claim_project("project", "replacement")
    with pytest.raises(LeaseLost):
        await delivery.append(old, receipt.command_id, "body", b"stale")
    fresh = RuntimeDelivery(second, RuntimeVault(delivery.vault.root))
    page = await fresh.page("operator", receipt.command_id, limit=1)
    assert len(page.frames) == 1 and page.frames[0].sequence == 1
    assert not (await fresh.page("operator", receipt.command_id, after=1)).frames
    with pytest.raises(ValueError):
        await fresh.page("operator", receipt.command_id, after=-1)


async def test_delivery_keeps_duplicate_headers_and_terminal_frame_order(
    cluster, tmp_path
):
    (first, _), _, _clock = cluster
    delivery = RuntimeDelivery(first, RuntimeVault(tmp_path / "private"))
    receipt = await accept(delivery)
    lease = await first.claim_project("project", "old")
    runner = DurableHttpRunner(delivery, reply, operator_token="synthetic")
    assert await runner.run_one(lease, lane="work")
    page = await delivery.page("operator", receipt.command_id)
    head = json.loads(page.frames[0].content)
    assert [value for name, value in head["headers"] if name == "set-cookie"] == [
        "one=synthetic",
        "two=synthetic",
    ]
    assert [frame.kind for frame in page.frames] == ["head", "body", "end"]
    with pytest.raises(CoordinationConflict):
        await delivery.append(lease, receipt.command_id, "body", b"too late")


@pytest.mark.parametrize("cancelled", [False, True])
async def test_final_send_does_not_publish_completion_before_app_returns(
    cluster, tmp_path, cancelled
):
    (first, second), _, _clock = cluster
    delivery = RuntimeDelivery(first, RuntimeVault(tmp_path / "private"))
    viewer = RuntimeDelivery(second, RuntimeVault(delivery.vault.root))
    receipt = await accept(delivery)
    lease = await first.claim_project("project", "old")
    sent, release = asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        await reply(scope, receive, send)
        sent.set()
        await release.wait()

    task = asyncio.create_task(
        DurableHttpRunner(delivery, app, operator_token="synthetic").run_one(
            lease, lane="work"
        )
    )
    await asyncio.wait_for(sent.wait(), 5)
    before = await viewer.page("operator", receipt.command_id)
    assert before.state == "running"
    assert [frame.kind for frame in before.frames] == ["head", "body"]
    if cancelled:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        assert await asyncio.wait_for(task, 5)
    after = await viewer.page("operator", receipt.command_id)
    assert after.state == ("uncertain" if cancelled else "succeeded")
    assert [frame.kind for frame in after.frames] == (
        ["head", "body"] if cancelled else ["head", "body", "end"]
    )
