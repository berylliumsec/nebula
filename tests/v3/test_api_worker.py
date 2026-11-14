"""Public authentication and real SQL delivery without an execution factory."""

import asyncio
from datetime import timedelta
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3.api_worker import create_api_worker
from nebula.v3.database import Database, RuntimeCommandRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import Engagement, PairedDeviceSession, utc_now
from nebula.v3.runtime_coordination import RuntimeCoordination
from nebula.v3.runtime_delivery import DurableHttpRunner, RuntimeDelivery
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
_TOKEN = "synthetic-api-worker-operator"
_AUTH = {"Authorization": "Bearer " + _TOKEN, "Idempotency-Key": "synthetic-key"}
_POOL = PoolSettings(5, 12, 5, 2)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def boundary(location, tmp_path):
    host = NebulaStore(Database(location, pool_settings=_POOL))
    await host.create(Engagement(id="project", name="Synthetic API project"))
    coordination = RuntimeCoordination(host.database)
    await coordination.register_runtime("execution", "v1")
    lease = await coordination.claim_project("project", "execution")
    vault = RuntimeVault(tmp_path / "private")
    stores, clients, apps = [], [], []

    async def project_for(_intent):
        return "project"

    async def upstream_for(_scope):
        pytest.fail(
            "this selected journey must not fall back to a private HTTP invocation"
        )

    def api(**options):
        store = NebulaStore(Database(location, bootstrap=False, pool_settings=_POOL))
        stores.append(store)
        delivery = RuntimeDelivery(
            RuntimeCoordination(store.database), RuntimeVault(vault.root)
        )
        app = create_api_worker(
            store,
            delivery,
            operator_token=_TOKEN,
            project_for=project_for,
            upstream_for=upstream_for,
            **{"command_start_timeout": 0.03, "poll_interval": 0.01, **options},
        )
        apps.append(app)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://synthetic.test"
        )
        clients.append(client)
        return app, client

    try:
        yield SimpleNamespace(
            host=host,
            coordination=coordination,
            lease=lease,
            api=api,
            delivery=RuntimeDelivery(coordination, vault),
        )
    finally:
        for client in clients:
            await client.aclose()
        for app in apps:
            await app.state.viewer_proxy.close()
        for store in [*stores, host]:
            await store.event_history.close()
            await store.database.dispose()


async def reply(_scope, _receive, send):
    await send(
        {
            "type": "http.response.start",
            "status": 201,
            "headers": [
                (b"content-type", b"application/json"),
                (b"set-cookie", b"one=synthetic"),
                (b"set-cookie", b"two=synthetic"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b'{"saved":true}'})


async def test_api_replacement_replays_saved_response_without_reexecution(boundary):
    first_app, first = boundary.api()
    accepted = await first.post(
        "/api/v1/synthetic", headers=_AUTH, json={"text": "inert"}
    )
    assert accepted.status_code == 202
    command = accepted.json()["receipt"]["command_id"]
    # The first API closes before any execution begins.
    await first.aclose()
    await first_app.state.viewer_proxy.close()
    calls = []

    async def execution(scope, receive, send):
        calls.append(scope["state"]["runtime_command_id"])
        await reply(scope, receive, send)

    runner = DurableHttpRunner(boundary.delivery, execution, operator_token=_TOKEN)
    assert await runner.run_one(boundary.lease, lane="work")
    _, replacement = boundary.api()
    response = await replacement.get(
        f"/api/v1/runtime/requests/{command}/response", headers=_AUTH
    )
    assert response.status_code == 201 and response.json() == {"saved": True}
    assert response.headers.get_list("set-cookie") == ["one=synthetic", "two=synthetic"]
    retry = await replacement.post(
        "/api/v1/synthetic", headers=_AUTH, json={"text": "inert"}
    )
    assert retry.status_code == 201 and retry.headers["x-nebula-command-id"] == command
    assert not await runner.run_one(boundary.lease, lane="work")
    assert calls == [command]
    changed = await replacement.post(
        "/api/v1/synthetic", headers=_AUTH, json={"text": "different"}
    )
    assert changed.status_code == 409
    beyond = await replacement.get(
        f"/api/v1/runtime/requests/{command}/response?after=3", headers=_AUTH
    )
    assert beyond.status_code == 201 and beyond.content == b""


async def test_live_memory_apply_uses_control_while_original_work_is_running(boundary):
    _, client = boundary.api()
    work_entered, release_work = asyncio.Event(), asyncio.Event()
    handled = []

    async def application(scope, _receive, send):
        handled.append(scope["path"])
        if scope["path"] == "/api/v1/synthetic":
            work_entered.set()
            await release_work.wait()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"observed":true}'})

    accepted = await client.post(
        "/api/v1/synthetic", headers={**_AUTH, "Idempotency-Key": "allocator"}, json={}
    )
    assert accepted.status_code == 202
    runner = DurableHttpRunner(boundary.delivery, application, operator_token=_TOKEN)
    work = asyncio.create_task(runner.run_one(boundary.lease, lane="work"))
    try:
        await asyncio.wait_for(work_entered.wait(), timeout=3)
        accepted = await client.post(
            "/api/v1/memory-budgets/jobs/original-job/apply",
            headers={**_AUTH, "Idempotency-Key": "apply"},
            json={"acknowledged": True},
        )
        assert accepted.status_code == 202
        command = accepted.json()["receipt"]["command_id"]
        async with boundary.host.database.session() as session:
            row = await session.get(RuntimeCommandRow, command)
            assert row.lane == "control"
        assert await runner.run_one(boundary.lease, lane="control")
        response = await client.get(
            f"/api/v1/runtime/requests/{command}/response", headers=_AUTH
        )
        assert response.status_code == 200 and response.json() == {"observed": True}
        assert not work.done()
        assert handled == [
            "/api/v1/synthetic",
            "/api/v1/memory-budgets/jobs/original-job/apply",
        ]
    finally:
        release_work.set()
        await work


async def test_authentication_csrf_and_key_checks_precede_acceptance(boundary):
    _, client = boundary.api()
    assert (await client.post("/api/v1/synthetic", json={})).status_code == 401
    assert (
        await client.post(
            "/api/v1/synthetic", headers={"Authorization": "Bearer " + _TOKEN}, json={}
        )
    ).status_code == 428
    assert (await client.get("/api/v1/deployment/status")).status_code == 401
    assert (
        await client.get("/api/v1/deployment/status", headers=_AUTH)
    ).status_code == 200
    now = utc_now()
    await boundary.host.create(
        PairedDeviceSession(
            id="browser",
            name="Synthetic paired browser",
            token_sha256=hashlib.sha256(b"device-token").hexdigest(),
            csrf_sha256=hashlib.sha256(b"csrf-token").hexdigest(),
            last_used_at=now,
            idle_expires_at=now + timedelta(days=1),
            absolute_expires_at=now + timedelta(days=2),
        )
    )
    client.cookies.set("nebula_device", "device-token")
    client.cookies.set("nebula_csrf", "csrf-token")
    assert (
        await client.post(
            "/api/v1/synthetic",
            headers={"Idempotency-Key": "forged", "X-Nebula-CSRF": "csrf-token"},
            json={},
        )
    ).status_code == 403
    async with boundary.host.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 0
        )
    accepted = await client.post(
        "/api/v1/synthetic",
        headers={
            "Idempotency-Key": "paired",
            "X-Nebula-CSRF": "csrf-token",
            "Origin": "https://synthetic.test",
        },
        json={},
    )
    assert accepted.status_code == 202
    command = accepted.json()["receipt"]["command_id"]
    async with boundary.host.database.session() as session:
        row = await session.get(RuntimeCommandRow, command)
        assert row.actor_id == "device:browser"
    # A second authenticated device cannot read another device's request body.
    operator = await client.get(f"/api/v1/runtime/requests/{command}", headers=_AUTH)
    assert operator.status_code == 404


async def test_viewer_cancellation_does_not_cancel_execution_or_block_controls(
    boundary,
):
    _, client = boundary.api(maximum_waiting_views=1)
    accepted = await client.post("/api/v1/synthetic", headers=_AUTH, json={})
    command = accepted.json()["receipt"]["command_id"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def producer(_scope, _receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
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
        await release.wait()
        await send({"type": "http.response.body", "body": b"data: done\n\n"})

    worker = asyncio.create_task(
        DurableHttpRunner(boundary.delivery, producer, operator_token=_TOKEN).run_one(
            boundary.lease, lane="work"
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    reader = asyncio.create_task(
        client.get(f"/api/v1/runtime/requests/{command}/response", headers=_AUTH)
    )
    await asyncio.sleep(0.02)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert not worker.done()
    control = await client.post(
        "/api/v1/chat/turns/synthetic/cancel",
        headers={**_AUTH, "Idempotency-Key": "stop"},
        json={},
    )
    assert control.status_code == 202
    assert await DurableHttpRunner(
        boundary.delivery, reply, operator_token=_TOKEN
    ).run_one(boundary.lease, lane="control")
    release.set()
    assert await asyncio.wait_for(worker, 5)
    reconnect = await client.get(
        f"/api/v1/runtime/requests/{command}/response", headers=_AUTH
    )
    assert (
        reconnect.status_code == 200
        and reconnect.content == b"data: first\n\ndata: done\n\n"
    )


async def test_api_factory_rejects_migration_ownership(boundary):
    with pytest.raises(ValueError, match="never migrate"):
        create_api_worker(
            boundary.host,
            boundary.delivery,
            operator_token=_TOKEN,
            project_for=None,
            upstream_for=None,
        )


@pytest.mark.parametrize(
    "path",
    [
        "harness-interactions/synthetic/decision",
        "browser-companion/synthetic/actions/synthetic-action",
        "browser-intercepts/synthetic/decision",
        "browser-actions/synthetic/decision",
        "harness-turns/synthetic/steer",
        "runs/synthetic/steer",
    ],
)
async def test_decisions_and_revocation_bypass_running_work(boundary, path):
    _, client = boundary.api()
    work = await client.post("/api/v1/synthetic", headers=_AUTH, json={})
    assert work.status_code == 202
    running = await boundary.coordination.claim_next(boundary.lease, lane="work")
    assert running is not None
    control = await client.request(
        "DELETE" if path.startswith("auth/devices/") else "POST",
        "/api/v1/" + path,
        headers={**_AUTH, "Idempotency-Key": "decision"},
        json={},
    )
    assert control.status_code == 202
    calls = []

    async def resolve(scope, receive, send):
        calls.append(scope["path"])
        await reply(scope, receive, send)

    assert await DurableHttpRunner(
        boundary.delivery, resolve, operator_token=_TOKEN
    ).run_one(boundary.lease, lane="control")
    assert calls == ["/api/v1/" + path]
    assert await boundary.coordination.claim_next(boundary.lease, lane="work") is None


def _storage_report(event_id="observation-1"):
    return {
        "schema_version": 1,
        "event_id": event_id,
        "observed_at": utc_now().isoformat(),
        "kind": "snapshot",
        "device": {"id": "nas-1", "label": "NAS One"},
        "volume": {"id": "data", "label": "Data", "path": "/data"},
        "state": "warning",
        "free_bytes": 20,
        "total_bytes": 100,
    }


async def test_storage_sender_reports_reach_their_project_without_operator_credentials(
    boundary,
):
    from nebula.v3.authentication import current_runtime_principal
    from nebula.v3.domain import StorageSource
    from nebula.v3.runtime_delivery import HttpIntent

    source = await boundary.host.create(
        StorageSource(
            name="Synthetic NAS",
            engagement_id="project",
            profile_id="provider",
            model="model",
            enabled=True,
            token_sha256=hashlib.sha256(b"sender-secret").hexdigest(),
        )
    )
    sender = {"Authorization": f"Bearer {source.id}.sender-secret"}
    _, client = boundary.api()
    tested = await client.post(
        "/api/v1/storage/test", headers=sender, json=_storage_report()
    )
    assert tested.status_code == 200, tested.text
    assert tested.json() == {"valid": True, "source_id": source.id}
    invalid = await client.post(
        "/api/v1/storage/test", headers=sender, json={"schema_version": 1}
    )
    assert invalid.status_code == 422
    # The operator token is not a source token, and neither is a guess.
    for headers in (_AUTH, {"Authorization": f"Bearer {source.id}.guess"}, {}):
        refused = await client.post(
            "/api/v1/storage/events", headers=headers, json=_storage_report()
        )
        assert refused.status_code == 401, refused.text
    remote = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=boundary.api()[0], client=("192.0.2.10", 40000)
        ),
        base_url="http://synthetic.test",
    )
    async with remote:
        plain = await remote.post(
            "/api/v1/storage/events", headers=sender, json=_storage_report()
        )
    assert plain.status_code == 403
    # Core refuses a report it could not record before saving or acknowledging it.
    for path, body in (
        (
            "/api/v1/storage/events",
            {**_storage_report(), "observed_at": "2026-11-14T10:00:00"},
        ),
        ("/api/v1/storage/inventory", {}),
    ):
        refused = await client.post(path, headers=sender, json=body)
        assert refused.status_code == 422, refused.text
    async with boundary.host.database.session() as session:
        assert await session.scalar(select(func.count(RuntimeCommandRow.id))) == 0

    # The sender's contract has no Idempotency-Key; its retry is the same report.
    report = _storage_report()
    saved = await client.post("/api/v1/storage/events", headers=sender, json=report)
    assert saved.status_code == 202, saved.text
    assert saved.json()["accepted"] is True and saved.json()["queued"] is True
    retried = await client.post("/api/v1/storage/events", headers=sender, json=report)
    assert retried.json()["command_id"] == saved.json()["command_id"]
    async with boundary.host.database.session() as session:
        command = await session.get(RuntimeCommandRow, saved.json()["command_id"])
    assert command.project_id == "project"
    assert command.name == "storage.report/v1" and command.chat_start is False
    assert command.actor_id == f"storage-source:{source.id}:{source.token_sha256}"
    intent, _ = await boundary.delivery.request(
        SimpleNamespace(
            receipt=SimpleNamespace(command_id=command.id),
            name=command.name,
            payload=command.payload,
        )
    )
    assert isinstance(intent, HttpIntent)
    assert "sender-secret" not in intent.model_dump_json()

    seen = []

    async def execution(scope, receive, send):
        headers = dict(scope["headers"])
        seen.append((current_runtime_principal(), headers[b"authorization"]))
        await send(
            {
                "type": "http.response.start",
                "status": 202,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"accepted":true,"duplicate":false,"current":true}',
            }
        )

    runner = DurableHttpRunner(boundary.delivery, execution, operator_token=_TOKEN)
    assert await runner.run_one(boundary.lease, lane="work")
    assert seen == [(command.actor_id, b"Bearer " + _TOKEN.encode())]
    replay = await client.post("/api/v1/storage/events", headers=sender, json=report)
    assert replay.status_code == 202
    assert replay.json() == {"accepted": True, "duplicate": False, "current": True}

    # Revoking the source before a saved report runs stops that report.
    later_report = _storage_report("observation-2")
    later = await client.post(
        "/api/v1/storage/events", headers=sender, json=later_report
    )
    assert later.status_code == 202
    current = await boundary.host.get(StorageSource, source.id)
    await boundary.host.update(
        StorageSource,
        source.id,
        {"enabled": False},
        expected_revision=current.revision,
    )
    assert await runner.run_one(boundary.lease, lane="work")
    assert len(seen) == 1
    async with boundary.host.database.session() as session:
        stopped = await session.get(RuntimeCommandRow, later.json()["command_id"])
        saved = await session.scalar(select(func.count(RuntimeCommandRow.id)))
    assert stopped.result == {"http_status": 401}
    # A disabled source is refused before anything is saved.
    for path, body in (
        ("/api/v1/storage/events", _storage_report("observation-3")),
        ("/api/v1/storage/inventory", {"schema_version": 1, "devices": []}),
    ):
        disabled = await client.post(path, headers=sender, json=body)
        assert disabled.status_code == 403, disabled.text
    async with boundary.host.database.session() as session:
        assert await session.scalar(select(func.count(RuntimeCommandRow.id))) == saved
    # Enabled again, the same report runs again instead of replaying that refusal.
    current = await boundary.host.get(StorageSource, source.id)
    await boundary.host.update(
        StorageSource,
        source.id,
        {"enabled": True},
        expected_revision=current.revision,
    )
    again = await client.post(
        "/api/v1/storage/events", headers=sender, json=later_report
    )
    assert again.status_code == 202, again.text
    assert again.json()["command_id"] != later.json()["command_id"]
    assert await runner.run_one(boundary.lease, lane="work")
    assert len(seen) == 2


async def _storage_source(boundary, *, project="project", secret=b"sender-secret"):
    from nebula.v3.domain import StorageSource

    source = await boundary.host.create(
        StorageSource(
            name="Synthetic NAS",
            engagement_id=project,
            profile_id="provider",
            model="model",
            enabled=True,
            token_sha256=hashlib.sha256(secret).hexdigest(),
        )
    )
    return source, {"Authorization": f"Bearer {source.id}.{secret.decode()}"}


async def _accepted(
    status=202, body=b'{"accepted":true,"duplicate":false,"current":true}'
):
    async def execution(_scope, _receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return execution


async def test_storage_senders_never_take_operator_viewer_capacity(
    boundary, monkeypatch
):
    import nebula.v3.api_worker as api_worker

    # Senders wait this long for the project's worker, in their own pool.
    monkeypatch.setattr(api_worker, "STORAGE_ACK_WAIT_SECONDS", 3.0)
    _, fast = boundary.api()
    operator = await fast.post("/api/v1/synthetic", headers=_AUTH, json={"text": "x"})
    command = operator.json()["receipt"]["command_id"]
    _, headers = await _storage_source(boundary)
    _, client = boundary.api(maximum_waiting_views=2, command_start_timeout=3.5)
    reports = [
        asyncio.create_task(
            client.post(
                "/api/v1/storage/events",
                headers=headers,
                json=_storage_report(f"observation-{index}"),
            )
        )
        for index in range(6)
    ]
    # Once every report is saved, the senders that wait are holding a slot.
    for _ in range(400):
        async with boundary.host.database.session() as session:
            saved = await session.scalar(
                select(func.count(RuntimeCommandRow.id)).where(
                    RuntimeCommandRow.name == "storage.report/v1"
                )
            )
        if saved == len(reports):
            break
        await asyncio.sleep(0.02)
    assert saved == len(reports)
    # Every operator viewer slot is still the operator's.
    viewed = await client.get(
        f"/api/v1/runtime/requests/{command}/response", headers=_AUTH
    )
    assert viewed.status_code != 429, viewed.text
    for answer in await asyncio.gather(*reports):
        assert answer.status_code == 202, answer.text
        assert answer.json()["accepted"] is True and answer.json()["queued"] is True


async def test_a_storage_report_is_acknowledged_well_before_a_sender_gives_up(
    boundary, monkeypatch
):
    import nebula.v3.api_worker as api_worker

    monkeypatch.setattr(api_worker, "STORAGE_ACK_WAIT_SECONDS", 0.5)
    _, headers = await _storage_source(boundary)
    # Operator viewers keep the long wait; a sender gets the short one.
    _, client = boundary.api(command_start_timeout=30)
    started = asyncio.get_running_loop().time()
    saved = await asyncio.wait_for(
        client.post("/api/v1/storage/events", headers=headers, json=_storage_report()),
        timeout=5,
    )
    assert asyncio.get_running_loop().time() - started < 3
    assert saved.status_code == 202 and saved.json()["queued"] is True


async def test_an_interrupted_or_failed_storage_report_runs_again_without_blocking_work(
    boundary,
):
    from nebula.v3.runtime_coordination import STORAGE_REPORT_INTERRUPTED

    _, headers = await _storage_source(boundary)
    _, client = boundary.api()
    report = _storage_report()
    first = await client.post("/api/v1/storage/events", headers=headers, json=report)
    assert first.status_code == 202, first.text

    async def stopped(_scope, _receive, _send):
        raise RuntimeError("the execution worker stopped mid-report")

    with pytest.raises(RuntimeError):
        await DurableHttpRunner(
            boundary.delivery, stopped, operator_token=_TOKEN
        ).run_one(boundary.lease, lane="work")
    async with boundary.host.database.session() as session:
        interrupted = await session.get(RuntimeCommandRow, first.json()["command_id"])
    # Re-running a report is safe, so it fails retryably instead of uncertain.
    assert interrupted.state == "failed"
    assert interrupted.result == STORAGE_REPORT_INTERRUPTED
    resent = await client.post("/api/v1/storage/events", headers=headers, json=report)
    assert resent.status_code == 202
    assert resent.json()["command_id"] != first.json()["command_id"]
    broken = DurableHttpRunner(
        boundary.delivery,
        await _accepted(500, b'{"detail":"boom"}'),
        operator_token=_TOKEN,
    )
    assert await broken.run_one(boundary.lease, lane="work")
    # A saved 500 is not replayed to the sender either; the retry runs again.
    third = await client.post("/api/v1/storage/events", headers=headers, json=report)
    assert third.json()["command_id"] not in {
        first.json()["command_id"],
        resent.json()["command_id"],
    }
    good = DurableHttpRunner(
        boundary.delivery, await _accepted(), operator_token=_TOKEN
    )
    assert await good.run_one(boundary.lease, lane="work")
    settled = await client.post("/api/v1/storage/events", headers=headers, json=report)
    assert settled.status_code == 202
    assert settled.json() == {"accepted": True, "duplicate": False, "current": True}

    # A report left running by a lost owner fails retryably; the lane moves on.
    fourth = await client.post(
        "/api/v1/storage/events", headers=headers, json=_storage_report("observation-4")
    )
    async with boundary.host.database.session() as session:
        row = await session.get(RuntimeCommandRow, fourth.json()["command_id"])
        row.state, row.runtime_id, row.fencing_token = "running", "lost-owner", 999
        await session.commit()
    assert await boundary.coordination.claim_next(boundary.lease, lane="work") is None
    async with boundary.host.database.session() as session:
        lost = await session.get(RuntimeCommandRow, fourth.json()["command_id"])
    assert (lost.state, lost.result) == ("failed", STORAGE_REPORT_INTERRUPTED)
    operator = await client.post("/api/v1/synthetic", headers=_AUTH, json={"text": "x"})
    assert operator.status_code == 202
    claimed = await boundary.coordination.claim_next(boundary.lease, lane="work")
    assert claimed is not None
    assert claimed.receipt.command_id == operator.json()["receipt"]["command_id"]


async def test_storage_reports_wait_for_a_worker_release_that_can_run_them(boundary):
    _, headers = await _storage_source(boundary)
    asked = []

    async def unsupported(project_id):
        asked.append(project_id)
        return False

    _, client = boundary.api(storage_reports_for=unsupported)
    for path, body in (
        ("/api/v1/storage/events", _storage_report()),
        ("/api/v1/storage/inventory", {"schema_version": 1, "devices": []}),
    ):
        held = await client.post(path, headers=headers, json=body)
        assert held.status_code == 503, held.text
        assert held.headers["retry-after"]
    assert asked == ["project", "project"]
    async with boundary.host.database.session() as session:
        assert await session.scalar(select(func.count(RuntimeCommandRow.id))) == 0
    tested = await client.post(
        "/api/v1/storage/test", headers=headers, json=_storage_report()
    )
    assert tested.status_code == 200


async def test_storage_report_keys_follow_the_source_project_and_inventory_is_admitted(
    boundary,
):
    from nebula.v3.domain import StorageSource
    from nebula.v3.runtime_coordination import project_worker_commands

    source, headers = await _storage_source(boundary)
    await boundary.host.create(Engagement(id="other", name="Synthetic other project"))
    _, client = boundary.api()
    report = _storage_report()
    first = await client.post("/api/v1/storage/events", headers=headers, json=report)
    current = await boundary.host.get(StorageSource, source.id)
    await boundary.host.update(
        StorageSource,
        source.id,
        {"engagement_id": "other"},
        expected_revision=current.revision,
    )
    # The same report for the source's new project is its own command there.
    moved = await client.post("/api/v1/storage/events", headers=headers, json=report)
    assert moved.status_code == 202, moved.text
    assert moved.json()["command_id"] != first.json()["command_id"]
    inventory = {"schema_version": 1, "devices": [{"id": "nas", "label": "NAS"}]}
    listed = [
        (
            await client.post(
                "/api/v1/storage/inventory", headers=headers, json=inventory
            )
        )
        for _ in range(2)
    ]
    # Inventory has no report identity; each request runs, and safely so.
    assert listed[0].json()["command_id"] != listed[1].json()["command_id"]
    async with boundary.host.database.session() as session:
        rows = {
            row.id: row
            for row in (await session.scalars(select(RuntimeCommandRow))).all()
        }
        # A queued report demands its project's worker like any project command.
        assert await session.scalar(select(project_worker_commands("other")))
    assert rows[moved.json()["command_id"]].project_id == "other"
    assert {rows[item.json()["command_id"]].project_id for item in listed} == {"other"}


async def test_a_storage_principal_submits_only_storage_reports(boundary):
    from nebula.v3.runtime_delivery import HttpIntent
    from nebula.v3.storage_sender import storage_source_actor

    source, _ = await _storage_source(boundary)
    actor = storage_source_actor(source)
    report = json.dumps(_storage_report()).encode()
    for method, path, options in (
        ("POST", "/api/v1/chat/completions", {}),
        ("POST", "/api/v1/storage/test", {}),
        ("DELETE", "/api/v1/storage/events", {}),
        ("POST", "/api/v1/storage/events", {"chat_start": True}),
    ):
        intent = HttpIntent.capture(method, path, b"", [], report)
        with pytest.raises(ValueError, match="storage reports|chat start"):
            await boundary.delivery.accept(
                intent,
                project_id="project",
                actor_id=actor,
                idempotency_key="refused-" + method + path,
                **options,
            )
    async with boundary.host.database.session() as session:
        assert await session.scalar(select(func.count(RuntimeCommandRow.id))) == 0


def test_api_role_import_does_not_import_the_execution_factory_or_chat_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import nebula.v3.api_worker; "
                "assert 'nebula.v3.execution_kernel' not in sys.modules; "
                "assert 'nebula.v3.chat' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
