"""Real inert workers survive their launcher; only disposable data is used."""

import asyncio
from io import BytesIO
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from nebula.v3.database import Database, RuntimeProcessRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.domain import Engagement
from nebula.v3.process_identity import process_birth, same_process, signal_process
from nebula.v3.release_store import ReleaseStore
from nebula.v3.runtime_coordination import CoordinationConflict, StartAborted
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.runtime_http import internal_headers
from nebula.v3.runtime_processes import RuntimeProcessHost, read_worker_bootstrap
from nebula.v3.storage import NebulaStore
from tests.v3.test_release_store import bundle
from tests.v3.test_runtime_coordination import location as location

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="Linux detached-process/pidfd fixture",
    ),
]
_SETTINGS = PoolSettings(5, 12, 5, 2)
_TOKEN = "synthetic-operator-private-bootstrap-token"
_API_WORKER = "from nebula.v3.runtime_worker import main\nmain()\n"


@pytest.fixture
def anyio_backend():
    return "asyncio"


_WORKER = r"""
import asyncio, sys
import uvicorn
from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.runtime_coordination import HEARTBEAT_SECONDS
from nebula.v3.runtime_ipc import RuntimeRpcServer
from nebula.v3.runtime_processes import read_worker_bootstrap, validate_worker_binding

async def main():
    bootstrap=read_worker_bootstrap(sys.stdin.buffer)
    database=Database(bootstrap.database, bootstrap=False, pool_settings=PoolSettings(**bootstrap.pool_settings) if bootstrap.pool_settings else None)
    coordinator=DeploymentCoordinator(database)
    key=await validate_worker_binding(bootstrap,coordinator)
    if bootstrap.identity.version.startswith('fail-'):
        await database.dispose()
        return
    rpc=RuntimeRpcServer(key)
    server=uvicorn.Server(uvicorn.Config(rpc.app,uds=bootstrap.identity.endpoint[7:],access_log=False,log_level='error',lifespan='off'))
    async def identity(_payload):
        return bootstrap.identity.model_dump(mode='json')
    async def shutdown(_payload):
        server.should_exit=True
        return {'stopping':True}
    rpc.register('worker.identity',identity)
    rpc.register('worker.shutdown',shutdown)
    async def beat():
        while True:
            await coordinator.coordination.heartbeat(bootstrap.identity.runtime_id)
            await asyncio.sleep(HEARTBEAT_SECONDS)
    heartbeat=asyncio.create_task(beat())
    try:
        await server.serve()
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat,return_exceptions=True)
        await database.dispose()

asyncio.run(main())
"""


_PARENT = r"""
import asyncio,json,sys
from pathlib import Path
from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.runtime_processes import RuntimeProcessHost

async def main():
    config=json.loads(Path(sys.argv[1]).read_text())
    settings=PoolSettings(**config['settings'])
    database=Database(config['database'],pool_settings=settings)
    host=RuntimeProcessHost(DeploymentCoordinator(database),Path(config['root']),pool_settings=settings,operator_token=config['token'])
    await host.start()
    identity=await host.spawn('v1','execution')
    # Publish atomically: the test reads the file as soon as it exists.
    staged=Path(config['ready']+'.tmp')
    staged.write_text(identity.model_dump_json())
    staged.replace(config['ready'])
    if config.get('graceful_exit'):
        await host.close()
        await database.dispose()
        return
    await asyncio.Event().wait()

asyncio.run(main())
"""


@pytest.fixture
async def managed(location, tmp_path):
    database = Database(location, pool_settings=_SETTINGS)
    store = NebulaStore(database)
    for project in ("admitted-project", "synthetic-project"):
        await store.create(Engagement(id=project, name="Synthetic process fixture"))
    coordinator = DeploymentCoordinator(database)
    data = tmp_path / "core"
    retained = ReleaseStore(tmp_path / "retained")
    host = RuntimeProcessHost(
        coordinator, data, pool_settings=_SETTINGS, operator_token=_TOKEN
    )
    hosts = [host]
    await host.start()

    async def stage(version, program=None, *, features=()):
        source = tmp_path / ("application-" + version)
        digest = bundle(
            source,
            version=version,
            code=(
                "#!"
                + sys.executable
                + "\n"
                + (program if program is not None else _WORKER)
            ).encode(),
            features=features,
        )
        manifest, installed = retained.install(source, expected_manifest_sha256=digest)
        await coordinator.stage(manifest, installed, digest)
        return source, installed

    def new_host():
        replacement = RuntimeProcessHost(
            coordinator, data, pool_settings=_SETTINGS, operator_token=_TOKEN
        )
        hosts.append(replacement)
        return replacement

    try:
        yield SimpleNamespace(
            host=host,
            hosts=hosts,
            coordinator=coordinator,
            data=data,
            stage=stage,
            new_host=new_host,
            database=database,
            store=store,
        )
    finally:
        async with database.session() as session:
            rows = list(await session.scalars(select(RuntimeProcessRow)))
        for row in rows:
            if not row.pid or not same_process(row.pid, row.process_birth):
                continue
            client = await host.rpc(row.runtime_id)
            try:
                await client.call("worker.shutdown", {}, timeout=2)
            except Exception:
                signal_process(row.pid, row.process_birth, signal.SIGKILL)
            finally:
                await client.close()
            async with asyncio.timeout(5):
                while same_process(row.pid, row.process_birth):
                    await asyncio.sleep(0.02)
        for current in hosts:
            for child in current.children.values():
                await asyncio.to_thread(child.wait, timeout=5)
            await current.close()
        sockets = host.socket_root
        if sockets is not None:
            shutil.rmtree(sockets)
        for path in retained.root.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
        await store.event_history.close()
        await database.dispose()


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b'{"operator_token":"synthetic-secret","identity":{}}\n',
        b"x" * (64 * 1024 + 1),
    ],
    ids=["empty", "invalid-model", "oversized"],
)
def test_invalid_private_bootstrap_is_bounded_and_does_not_report_credentials(frame):
    with pytest.raises(RuntimeIpcError) as error:
        read_worker_bootstrap(BytesIO(frame))
    assert "synthetic-secret" not in str(error.value)
    assert "no execution was started" in str(error.value)


async def test_only_one_supervisor_holds_a_data_directory(managed):
    replacement = managed.new_host()
    with pytest.raises(BlockingIOError):
        await replacement.start()
    assert managed.host.lock.descriptor is not None


async def test_worker_survives_host_close_and_is_rediscovered_with_retained_assets(
    managed,
):
    source, installed = await managed.stage("v1")
    identity = await managed.host.spawn("v1", "execution")
    assert identity is not None
    assert identity.process_birth == process_birth(identity.pid)
    arguments = Path(f"/proc/{identity.pid}/cmdline").read_bytes()
    assert _TOKEN.encode() not in arguments
    assert b"runtime-worker" in arguments
    shutil.rmtree(source)
    await managed.host.close()
    assert same_process(identity.pid, identity.process_birth)
    replacement = managed.new_host()
    await replacement.start()
    observed = await replacement.rediscover()
    assert len(observed) == 1 and observed[0].reachable
    assert observed[0].identity == identity and observed[0].process_state == "alive"
    assert (installed / "nebula-core").is_file()


@pytest.mark.parametrize("role", ["api", "execution"])
async def test_graceful_retirement_reaps_only_an_empty_worker(managed, role):
    await managed.stage("v1")
    identity = await managed.host.spawn("v1", role)
    assert await managed.host.retire(identity.runtime_id, timeout=5) == identity
    assert not same_process(identity.pid, identity.process_birth)
    async with managed.database.session() as session:
        row = await session.get(RuntimeProcessRow, identity.runtime_id)
        assert row.state == "retired"


async def test_real_api_role_requires_both_private_channel_and_public_auth(managed):
    await managed.stage("v1", _API_WORKER)
    identity = await managed.host.spawn("v1", "api")
    rpc = await managed.host.rpc(identity.runtime_id)
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=identity.endpoint[7:]),
            base_url="http://private-runtime",
            trust_env=False,
        ) as client:
            assert (
                await client.get(
                    "/api/v1/deployment/status",
                    headers={"Authorization": "Bearer " + _TOKEN},
                )
            ).status_code == 401
            assert (
                await client.get(
                    "/api/v1/deployment/status",
                    headers=internal_headers(rpc.secret, "http"),
                )
            ).status_code == 401
            headers = [
                *internal_headers(rpc.secret, "http"),
                (b"authorization", ("Bearer " + _TOKEN).encode()),
            ]
            response = await client.get("/api/v1/deployment/status", headers=headers)
            assert response.status_code == 200
            assert response.json()["protocol"] == "nebula.runtime/v1"
    finally:
        await rpc.close()
    await managed.host.retire(identity.runtime_id, timeout=5)


async def test_real_api_promotion_keeps_old_worker_until_replacement_is_ready(managed):
    await managed.stage("v1", _API_WORKER)
    await managed.stage("v2", _API_WORKER)
    first = await managed.host.spawn("v1", "api")
    status = await managed.coordinator.status()
    await managed.coordinator.promote_api(
        first.runtime_id, expected_revision=status.revision
    )
    with pytest.raises(CoordinationConflict, match="promote another API"):
        await managed.host.retire(first.runtime_id, timeout=5)
    assert same_process(first.pid, first.process_birth)
    second = await managed.host.spawn("v2", "api")
    status = await managed.coordinator.status()
    await managed.coordinator.promote_api(
        second.runtime_id, expected_revision=status.revision
    )
    await managed.host.retire(first.runtime_id, timeout=5)
    assert same_process(second.pid, second.process_birth)
    assert (await managed.coordinator.status()).api_version == "v2"
    # The fixture has no public supervisor listener. Clearing its synthetic
    # promotion explicitly allows normal teardown of the last API process.
    from nebula.v3.database import DeploymentHostRow

    async with managed.database.session() as session:
        host = await session.get(DeploymentHostRow, "host")
        host.api_runtime_id = None
    await managed.host.retire(second.runtime_id, timeout=5)


async def test_two_failed_real_starts_do_not_prevent_a_later_version(managed):
    for version in ("fail-one", "fail-two"):
        await managed.stage(version)
        with pytest.raises(StartAborted, match="startup exited"):
            await managed.host.spawn(version, "execution", timeout=5)
    await managed.stage("v3")
    assert await managed.host.spawn("v3", "execution") is not None
    assert (await managed.coordinator.status()).resident_versions == ["v3"]


async def test_published_worker_survives_cancellation_after_committed_readiness(
    managed, monkeypatch
):
    await managed.stage("v1")
    publish = managed.coordinator.worker_ready
    admitted = []

    async def lose_response(identity):
        await publish(identity)
        lease = await managed.coordinator.coordination.claim_project(
            "admitted-project", identity.runtime_id
        )
        admitted.append((identity, lease))
        raise asyncio.CancelledError()

    monkeypatch.setattr(managed.coordinator, "worker_ready", lose_response)
    with pytest.raises(asyncio.CancelledError):
        await managed.host.spawn("v1", "execution")
    identity, lease = admitted[0]
    assert same_process(identity.pid, identity.process_birth)
    assert not await managed.coordinator.begin_start_abort(identity.runtime_id)
    async with managed.coordinator.coordination.fenced(lease):
        pass


async def test_refused_third_version_does_not_create_an_orphan_credential(managed):
    for version in ("v1", "v2", "v3"):
        await managed.stage(version)
    await managed.host.spawn("v1", "execution")
    await managed.host.spawn("v2", "execution")
    before = set(managed.host.credentials.directory.iterdir())
    assert await managed.host.spawn("v3", "execution") is None
    assert set(managed.host.credentials.directory.iterdir()) == before
    api = await managed.host.spawn("v3", "api")
    status = await managed.coordinator.status()
    await managed.coordinator.promote_api(
        api.runtime_id, expected_revision=status.revision
    )
    result = await managed.coordinator.status()
    assert result.api_version == "v3" and result.resident_versions == ["v1", "v2"]


async def test_nonreading_bootstrap_child_cannot_block_supervisor_timeouts(
    managed, monkeypatch
):
    await managed.stage("slow", program="import time; time.sleep(5)\n")
    original = subprocess.Popen

    def small_pipe(*args, **kwargs):
        return original(*args, **{**kwargs, "pipesize": 4096})

    monkeypatch.setattr("nebula.v3.runtime_processes.subprocess.Popen", small_pipe)
    managed.host.operator_token = "synthetic-" + "x" * (48 * 1024)
    before = time.monotonic()
    tick = asyncio.create_task(asyncio.sleep(0.05))
    try:
        # The timed-out start is aborted and proven gone, so it is reported
        # as a no-effect refusal that callers may retry.
        with pytest.raises(StartAborted) as aborted:
            await managed.host.spawn("slow", "execution", timeout=0.2)
        assert isinstance(aborted.value.__cause__, TimeoutError)
        await tick
        assert time.monotonic() - before < 2
        assert (await managed.coordinator.status()).resident_versions == []
    finally:
        await tick


@pytest.mark.parametrize("graceful_exit", [False, True])
async def test_actual_supervisor_process_exit_leaves_worker_reachable(
    managed, tmp_path, graceful_exit
):
    from dataclasses import asdict
    from nebula.v3.deployment_protocol import WorkerIdentity

    await managed.stage("v1")
    await managed.host.close()
    config = tmp_path / "parent.json"
    ready = tmp_path / "parent-ready.json"
    config.write_text(
        json.dumps(
            {
                "root": str(managed.data),
                "database": managed.database.url,
                "settings": asdict(_SETTINGS),
                "token": _TOKEN,
                "ready": str(ready),
                "graceful_exit": graceful_exit,
            }
        )
    )
    config.chmod(0o600)
    script = tmp_path / "supervisor.py"
    script.write_text(_PARENT)
    parent = None
    with (tmp_path / "parent.log").open("wb") as output:
        try:
            parent = subprocess.Popen(
                [sys.executable, str(script), str(config)],
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            birth = process_birth(parent.pid)
            deadline = time.monotonic() + 15
            while not ready.exists():
                assert parent.poll() is None, (tmp_path / "parent.log").read_text()
                assert time.monotonic() < deadline, (
                    tmp_path / "parent.log"
                ).read_text()
                await asyncio.sleep(0.05)
            identity = WorkerIdentity.model_validate_json(ready.read_bytes())
            if not graceful_exit:
                assert signal_process(parent.pid, birth, signal.SIGKILL)
            await asyncio.to_thread(parent.wait, timeout=5)
            assert same_process(identity.pid, identity.process_birth)
            replacement = managed.new_host()
            await replacement.start()
            observed = await replacement.rediscover()
            assert (
                len(observed) == 1
                and observed[0].reachable
                and observed[0].identity == identity
            )
            lease = await managed.coordinator.coordination.claim_project(
                "synthetic-project", identity.runtime_id
            )
            async with managed.coordinator.coordination.fenced(lease):
                pass
        finally:
            if parent is not None and parent.poll() is None:
                signal_process(parent.pid, process_birth(parent.pid), signal.SIGKILL)
                await asyncio.to_thread(parent.wait, timeout=5)
