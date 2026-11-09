"""Global Work streams observe committed producers without process-local feeds."""

import asyncio
from contextlib import asynccontextmanager
import sys
from urllib.parse import urlsplit

from fastapi import HTTPException
import pytest

from nebula.v3.api_worker import create_api_worker
from nebula.v3.domain import (
    ChatSession,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    WorkItem,
)
from nebula.v3.runtime_coordination import RuntimeCoordination
from nebula.v3.runtime_delivery import RuntimeDelivery
from nebula.v3.runtime_global_api import RuntimeGlobalApi
from nebula.v3.runtime_routing import RuntimeRouting
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.runtime_work_observer import SqlWorkObserver, WorkObservationUnavailable
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_global_reads import reads as reads
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
TOKEN = "isolated-global-read-token"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@asynccontextmanager
async def stream(app, path="/api/v1/work/events", *, authorized=True):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue(maxsize=4)
    await incoming.put({"type": "http.request", "body": b"", "more_body": False})
    target = urlsplit(path)
    headers = [(b"authorization", ("Bearer " + TOKEN).encode())] if authorized else []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": target.path,
        "raw_path": target.path.encode(),
        "query_string": target.query.encode(),
        "headers": headers,
        "server": ("synthetic.test", 443),
        "client": ("127.0.0.1", 1234),
        "root_path": "",
    }
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    try:
        head = await asyncio.wait_for(outgoing.get(), 3)
        assert head["type"] == "http.response.start"

        async def frame():
            response = await asyncio.wait_for(outgoing.get(), 5)
            assert response["type"] == "http.response.body"
            return response.get("body", b"").decode()

        yield head, frame
    finally:
        await incoming.put({"type": "http.disconnect"})
        try:
            await asyncio.wait_for(task, 3)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def change(frame):
    for _ in range(100):
        value = await frame()
        if "event: change" in value:
            return value
    pytest.fail("Work stream did not observe a committed change")


def tune(observer):
    observer.poll_interval = 0.02
    observer.heartbeat_interval = 0.08
    observer.scan_timeout = 1


async def test_public_work_stream_auth_ready_heartbeat_and_disconnect_release_capacity(
    reads,
):
    observer = reads.global_api.work_observer
    tune(observer)
    observer.max_subscribers = 1
    async with stream(reads.app, authorized=False) as (head, _):
        assert head["status"] == 401
    async with stream(reads.app) as (head, frame):
        assert head["status"] == 200
        assert dict(head["headers"])[b"cache-control"] == b"no-store"
        ready = await frame()
        assert "event: ready\ndata: {}" in ready and ready.startswith("id: v1:")
        assert await frame() == ": keep-alive\n\n"
        async with stream(reads.app) as (busy, _):
            assert busy["status"] == 429
    async with stream(reads.app) as (head, frame):
        assert head["status"] == 200 and "event: ready" in await frame()


async def test_work_stream_sees_distinct_store_update_delete_and_project_precedence(
    reads,
):
    project = await reads.store.create(Engagement(name="Original"))
    item = await reads.store.create(
        WorkItem(engagement_id=project.id, title="Original")
    )
    writer = NebulaStore(reads.database)
    tune(reads.global_api.work_observer)
    try:
        async with stream(reads.app) as (_, frame):
            await frame()
            await writer.update(WorkItem, item.id, {"title": "Same count, new content"})
            assert '"kind": "work"' in await change(frame)
            async with writer.transaction() as transaction:
                await transaction.update(
                    WorkItem, item.id, {"title": "Changed together"}
                )
                await transaction.update(Engagement, project.id, {"name": "Renamed"})
            assert '"kind": "projects"' in await change(frame)
            await writer.delete(WorkItem, item.id)
            assert '"kind": "work"' in await change(frame)
    finally:
        await writer.event_history.close()


async def test_work_stream_reads_only_committed_changes_from_a_real_other_process(
    reads,
):
    project = await reads.store.create(Engagement(name="Process project"))
    item = await reads.store.create(
        WorkItem(engagement_id=project.id, title="Before commit")
    )
    tune(reads.global_api.work_observer)
    code = """import asyncio,sys
from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import WorkItem
from nebula.v3.storage import NebulaStore
async def main():
    database=Database(sys.argv[1],bootstrap=False,pool_settings=PoolSettings(5,12,5,2))
    store=NebulaStore(database)
    try:
        async with store.transaction() as transaction:
            await transaction.update(WorkItem,sys.argv[2],{"title":"Committed by other process"})
            print("uncommitted",flush=True)
            await asyncio.to_thread(sys.stdin.readline)
    finally:
        await store.event_history.close()
        await database.dispose()
asyncio.run(main())
"""
    process = None
    try:
        async with stream(reads.app) as (_, frame):
            await frame()
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                str(reads.database.url),
                item.id,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert (
                await asyncio.wait_for(process.stdout.readline(), 10)
                == b"uncommitted\n"
            )
            assert (await reads.store.get(WorkItem, item.id)).title == "Before commit"
            assert await frame() == ": keep-alive\n\n"
            process.stdin.write(b"commit\n")
            await process.stdin.drain()
            _, errors = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 0, errors.decode()
            assert '"kind": "work"' in await change(frame)
            assert (
                await reads.store.get(WorkItem, item.id)
            ).title == "Committed by other process"
            assert await reads.store.count(WorkItem) == 1
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def test_work_observation_filters_explicit_project_and_ignores_token_checkpoint_noise(
    reads,
):
    project = await reads.store.create(Engagement(name="Selected"))
    other = await reads.store.create(Engagement(name="Other"))
    chat = await reads.store.create(
        ChatSession(
            engagement_id=project.id,
            title="Agent",
            provider_profile_id="inert",
            model="synthetic",
        )
    )
    turn = await reads.store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            provider_profile_id="inert",
            model="synthetic",
            status=ChatTurnStatus.ROUTING,
        )
    )
    observer = reads.global_api.work_observer
    tune(observer)
    async with stream(reads.app, "/api/v1/work/events?project_id=" + project.id) as (
        _,
        frame,
    ):
        ready = await frame()
        await reads.store.create(WorkItem(engagement_id=other.id, title="Foreign work"))
        await reads.store.update(
            ChatTurn,
            turn.id,
            {"usage": ChatTokenUsage(input_tokens=5, output_tokens=9, total_tokens=14)},
        )
        await reads.store.update(
            ChatSession,
            chat.id,
            {"metadata": {"unrelated_checkpoint": "streamed text"}},
        )
        assert await frame() == ": keep-alive\n\n"
        await reads.store.update(
            ChatSession, chat.id, {"title": "Renamed visible agent"}
        )
        assert '"kind": "work"' in await change(frame)
        await reads.store.update(ChatTurn, turn.id, {"status": ChatTurnStatus.COMPLETE})
        assert '"kind": "work"' in await change(frame)
        assert "id: v1:" in ready


async def test_observer_shares_scan_bounds_subscribers_and_does_no_idle_polling(
    reads, monkeypatch
):
    observer = SqlWorkObserver(reads.store)
    tune(observer)
    observer.max_subscribers = 2
    calls = []
    original = observer._scan

    async def scan(scopes):
        calls.append(scopes)
        return await original(scopes)

    monkeypatch.setattr(observer, "_scan", scan)
    assert calls == []
    first, second = await asyncio.gather(observer.subscribe(), observer.subscribe())
    assert first.baseline == second.baseline and len(calls) == 1
    with pytest.raises(HTTPException) as busy:
        await observer.subscribe()
    assert busy.value.status_code == 429
    await first.close()
    await second.close()
    await asyncio.sleep(observer.poll_interval * 2)
    assert len(calls) == 1
    await observer.close()


async def test_failed_scan_keeps_last_complete_cursor_and_shares_failure_cooldown(
    reads, monkeypatch
):
    observer = SqlWorkObserver(reads.store)
    tune(observer)
    subscription = await observer.subscribe()
    original = observer._scan
    calls = []

    async def fail(scopes):
        calls.append(scopes)
        raise TimeoutError("Synthetic partial scan timeout")

    monkeypatch.setattr(observer, "_scan", fail)
    await asyncio.sleep(observer.poll_interval)
    with pytest.raises(WorkObservationUnavailable):
        await observer.current(None)
    for _ in range(3):
        with pytest.raises(WorkObservationUnavailable):
            await observer.current(None)
    assert len(calls) == 1
    await reads.store.create(Engagement(name="Committed during failure"))
    monkeypatch.setattr(observer, "_scan", original)
    await asyncio.sleep(observer.poll_interval)
    recovered = await observer.current(None)
    assert recovered.change_from(subscription.baseline) == "projects"
    await observer.close()


async def test_work_stream_api_replacement_reads_existing_cursor_and_new_producer_changes(
    reads, tmp_path
):
    project = await reads.store.create(Engagement(name="Survives replacement"))
    tune(reads.global_api.work_observer)
    async with stream(reads.app) as (_, frame):
        before = (await frame()).splitlines()[0]
    replacement = RuntimeGlobalApi(reads.database)
    tune(replacement.work_observer)
    coordination = RuntimeCoordination(reads.database)
    routing = RuntimeRouting(
        coordination, tmp_path / "replacement-credentials", request_admission=True
    )
    delivery = RuntimeDelivery(
        coordination, RuntimeVault(tmp_path / "replacement-payloads")
    )

    async def forbidden_owner(_scope):
        pytest.fail("Global replacement contacted an execution owner")

    app = create_api_worker(
        reads.store,
        delivery,
        operator_token=TOKEN,
        project_for=routing.project_for,
        upstream_for=forbidden_owner,
        global_api=replacement,
    )
    try:
        async with stream(app) as (_, frame):
            assert (await frame()).splitlines()[0] == before
            await reads.store.create(
                WorkItem(engagement_id=project.id, title="Written after replacement")
            )
            assert '"kind": "work"' in await change(frame)
    finally:
        await app.state.viewer_proxy.close()
        await replacement.close()
