"""Discarded runtime failures stay observable without retaining private inputs."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
from types import SimpleNamespace

from fastapi import FastAPI
from pydantic import SecretStr
import pytest

from nebula.v3 import diagnostics, execution_worker, runtime_http
from nebula.v3.database import Database
from nebula.v3.diagnostics import DiagnosticManager, diagnostic_context
from nebula.v3.execution_scope import execution_context
from nebula.v3.execution_worker import ProjectExecution
from nebula.v3.remote_knowledge import RemoteKnowledgeIndex
from nebula.v3.runtime_coordination import LeaseLost, ProjectLease, RuntimeCoordination
from nebula.v3.runtime_delivery import HttpIntent, RuntimeDelivery
from nebula.v3.runtime_host_delivery import HostEffectDelivery
from nebula.v3.runtime_http import HttpUpstream, RuntimeHttpProxy
from nebula.v3.runtime_ipc import RuntimeRpcServer
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.storage import NebulaStore

pytestmark = pytest.mark.anyio
PRIVATE = "synthetic-private-credential-and-content"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    value = DiagnosticManager(tmp_path / "diagnostics", watch_settings=False)
    monkeypatch.setattr(diagnostics, "_manager", value)
    try:
        yield value
    finally:
        value.close()


def errors(manager):
    assert manager.flush()
    encoded = (manager.log_dir / "errors.log").read_text()
    assert PRIVATE not in encoded
    return [json.loads(line) for line in encoded.splitlines()]


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("direction", ["incoming", "outgoing"])
async def test_websocket_pump_failure_is_recorded_without_private_payload(
    direction, manager, monkeypatch
):
    class Upstream:
        subprotocol = None
        close_code = None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if direction == "outgoing":
                raise RuntimeError(PRIVATE)
            await asyncio.Event().wait()

        async def send(self, data):
            assert data == PRIVATE
            raise RuntimeError(PRIVATE)

    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        yield Upstream()

    monkeypatch.setattr(runtime_http, "unix_connect", connect)
    messages = []
    connected = False

    async def receive():
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        if direction == "incoming":
            return {"type": "websocket.receive", "text": PRIVATE}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    proxy = RuntimeHttpProxy(None)
    await proxy._websocket(
        HttpUpstream("unix:///synthetic.sock", b"k" * 32),
        {
            "type": "websocket",
            "path": "/synthetic",
            "headers": [(b"authorization", PRIVATE.encode())],
        },
        receive,
        send,
    )
    assert messages == [
        {"type": "websocket.accept", "subprotocol": None},
        {"type": "websocket.close", "code": 1001},
    ]
    assert not proxy.websocket_tasks and not proxy.websockets
    assert [row["event_code"] for row in errors(manager)] == [
        "api.runtime.websocket_pump_failed"
    ]


@pytest.mark.parametrize("ending", ["disconnect", "transport", "cancel"])
async def test_expected_websocket_endings_remain_quiet(ending, manager, monkeypatch):
    class Upstream:
        subprotocol = None
        close_code = None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if ending == "transport":
                raise OSError(PRIVATE)
            await asyncio.Event().wait()

    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        yield Upstream()

    monkeypatch.setattr(runtime_http, "unix_connect", connect)
    connected = False
    accepted = asyncio.Event()

    async def receive():
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        if ending == "disconnect":
            return {"type": "websocket.disconnect"}
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "websocket.accept":
            accepted.set()

    proxy = RuntimeHttpProxy(None)
    task = asyncio.create_task(
        proxy._websocket(
            HttpUpstream("unix:///synthetic.sock", b"k" * 32),
            {"type": "websocket", "path": "/synthetic"},
            receive,
            send,
        )
    )
    await asyncio.wait_for(accepted.wait(), 3)
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert not proxy.websocket_tasks and not proxy.websockets
    assert errors(manager) == []


@pytest.fixture
async def effect(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'control.sqlite3'}")
    await database.initialize()
    store = NebulaStore(database)
    delivery = RuntimeDelivery(
        RuntimeCoordination(database), RuntimeVault(tmp_path / "payloads")
    )
    value = HostEffectDelivery(delivery, SimpleNamespace(store=store))
    try:
        yield value
    finally:
        await value.close()
        await store.event_history.close()
        await database.dispose()


def intent():
    return HttpIntent.capture("POST", "/api/v1/synthetic", b"", [], PRIVATE.encode())


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_host_effect_keeps_uncertainty_and_never_logs_private_failure(
    failure, effect, manager, monkeypatch
):
    calls = []

    async def fail(_intent, _actor):
        calls.append(True)
        raise failure(PRIVATE)

    monkeypatch.setattr(effect, "_response", fail)
    receipt = await effect.execute(
        intent(), actor_id="operator", idempotency_key="synthetic"
    )
    response = await effect.response("operator", receipt.command_id)
    page = await effect.delivery.page("operator", receipt.command_id)
    assert response.status_code == 409
    assert page.state == "uncertain" and not page.frames
    assert (
        await effect.execute(intent(), actor_id="operator", idempotency_key="synthetic")
        == receipt
    )
    assert calls == [True]
    assert [row["event_code"] for row in errors(manager)] == (
        ["runtime.host_setting.uncertain"] if failure is RuntimeError else []
    )


async def test_host_effect_uncertainty_persistence_failure_is_supervised(
    effect, manager, monkeypatch
):
    @asynccontextmanager
    async def unavailable():
        raise RuntimeError(PRIVATE)
        yield

    write = effect.delivery.coordination._write

    async def fail(_intent, _actor):
        monkeypatch.setattr(effect.delivery.coordination, "_write", unavailable)
        raise asyncio.CancelledError(PRIVATE)

    monkeypatch.setattr(effect, "_response", fail)
    try:
        await effect.execute(intent(), actor_id="operator", idempotency_key="synthetic")
        await until(lambda: not effect.tasks)
        assert [row["event_code"] for row in errors(manager)] == [
            "runtime.host_setting.failed"
        ]
    finally:
        monkeypatch.setattr(effect.delivery.coordination, "_write", write)


async def test_remote_index_background_failure_is_safe_and_correlated(
    tmp_path, manager
):
    async def fail(*_args, **_kwargs):
        raise RuntimeError(PRIVATE)

    remote = RemoteKnowledgeIndex(SimpleNamespace(call=fail), tmp_path)
    try:
        with (
            execution_context(ProjectLease("project", "runtime", 1)),
            diagnostic_context(operation_id="operation_parent"),
        ):
            remote._background("prepare_reranker")
        await remote.close()
        await asyncio.sleep(0)
        rows = errors(manager)
        assert [row["event_code"] for row in rows] == [
            "knowledge.remote.background.failed"
        ]
        assert rows[0]["parent_operation_id"] == "operation_parent"
        assert rows[0]["operation_id"] != "operation_parent"
        assert not remote._background_tasks
    finally:
        await remote.close()


@pytest.mark.parametrize("failure", [RuntimeError, LeaseLost])
async def test_lease_watcher_preserves_fencing_and_classifies_unexpected_failure(
    failure, tmp_path, manager, monkeypatch
):
    probes = 0

    @asynccontextmanager
    async def fenced(_lease):
        nonlocal probes
        probes += 1
        if probes >= 3:
            raise failure(PRIVATE)
        yield

    @asynccontextmanager
    async def application(context, _lease):
        try:
            yield FastAPI()
        finally:
            context.restore_cleanup_completed = True

    class Runner:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run_one(self, *_args, **_kwargs):
            return False

    monkeypatch.setattr(execution_worker, "HEARTBEAT_SECONDS", 0.001)
    monkeypatch.setattr(execution_worker, "DurableHttpRunner", Runner)
    context = SimpleNamespace(
        bootstrap=SimpleNamespace(
            data_root=str(tmp_path),
            identity=SimpleNamespace(runtime_id="runtime"),
            operator_token=SecretStr(PRIVATE),
        ),
        coordinator=SimpleNamespace(coordination=SimpleNamespace(fenced=fenced)),
        rpc=RuntimeRpcServer(b"k" * 32),
    )
    execution = ProjectExecution(context, application_factory=application)
    try:
        await execution.attach(asdict(ProjectLease("project", "runtime", 1)))
        await asyncio.wait_for(execution.task, 3)
        assert execution.state == "fenced" and execution._stop.is_set()
        assert execution.cleanup_complete
        assert [row["event_code"] for row in errors(manager)] == (
            ["runtime.project.lease_unavailable"] if failure is RuntimeError else []
        )
    finally:
        await execution.close()
