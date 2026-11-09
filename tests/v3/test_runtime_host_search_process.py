"""An archived execution worker queries the sole host service over its real socket."""

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from nebula.v3.deployment_protocol import ReleaseManifest
from nebula.v3.domain import ChatSession, ChatTurn, Engagement, ScopePolicy, ToolCall
from nebula.v3.release_store import MANIFEST_NAME, ReleaseStore
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_host_search import InertHostSearch
from tests.v3.test_runtime_host_settings import settings as settings
from tests.v3.test_runtime_processes import managed as managed

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="Linux retained-process fixture"
    ),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


# The retained child imports only its checksummed archived Nebula package and
# installed dependencies. It has no reference to this checkout or tests package.
_WORKER = r"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'python'))
import asyncio, os
import uvicorn
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_coordination import HEARTBEAT_SECONDS, ProjectLease
from nebula.v3.runtime_host_search import RemoteProjectSearch
from nebula.v3.runtime_host_services import host_service_client
from nebula.v3.runtime_ipc import RuntimeRpcServer
from nebula.v3.runtime_platform import default_runtime_platform
from nebula.v3.runtime_processes import read_worker_bootstrap, validate_worker_binding
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import ToolInvocation
import nebula.v3.runtime_host_search as query_module

async def main():
    bootstrap=read_worker_bootstrap(sys.stdin.buffer)
    pools=PoolSettings(**bootstrap.pool_settings) if bootstrap.pool_settings else None
    database=Database(bootstrap.database, bootstrap=False, pool_settings=pools)
    coordinator=DeploymentCoordinator(database)
    key=await validate_worker_binding(bootstrap,coordinator)
    project_database=Database(bootstrap.database, bootstrap=False,
        execution_runtime_id=bootstrap.identity.runtime_id, pool_settings=pools)
    store=NebulaStore(project_database)
    host_client=host_service_client(bootstrap,key)
    rpc=RuntimeRpcServer(key)
    server=uvicorn.Server(uvicorn.Config(rpc.app,uds=bootstrap.identity.endpoint[7:],
        access_log=False,log_level='error',lifespan='off'))
    async def identity(_payload):
        return bootstrap.identity.model_dump(mode='json')
    async def shutdown(_payload):
        server.should_exit=True
        return {'stopping':True}
    async def query(payload):
        lease=ProjectLease(**payload['lease'])
        capabilities=await host_client.call('host.services/v1',{})
        if capabilities.get('web_search') is not True:
            raise RuntimeError('The host query capability is unavailable')
        remote=RemoteProjectSearch(host_client,store,lease)
        root=Path(bootstrap.data_root)
        platform=default_runtime_platform(store=store,
            artifact_store=ArtifactStore(root/'artifacts'),data_root=root,
            search_runtime=remote)
        with execution_context(lease):
            turn_id=payload['turn_id']
            components=await platform.chat_components(engagement_id=lease.project_id,
                turn_id=turn_id,provider=None,model='inert')
            result=await components.broker.execute(ToolInvocation(
                id='query-call:'+lease.project_id,engagement_id=lease.project_id,
                run_id=turn_id,origin='chat',chat_turn_id=turn_id,
                chat_session_id=payload['session_id'],tool_name='web.search',
                arguments={'query':'public advisory','count':1,'freshness':'week'},
                workspace=components.workspace),components.scope)
        await remote.close()
        return {'pid':os.getpid(),'module':query_module.__file__,
            'exit_code':result.exit_code,'output':result.output,
            'result_artifact_id':result.result_artifact_id,'lease':payload['lease']}
    rpc.register('worker.identity',identity)
    rpc.register('worker.shutdown',shutdown)
    rpc.register('worker.search',query)
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
        await host_client.close()
        await store.event_history.close()
        await project_database.dispose()
        await database.dispose()

asyncio.run(main())
"""


async def _stage(managed, tmp_path):
    source = tmp_path / "query-source"
    source.mkdir()
    package = Path(__file__).resolve().parents[2] / "src" / "nebula"
    shutil.copytree(
        package,
        source / "python" / "nebula",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (source / "nebula-core").write_text("#!" + sys.executable + "\n" + _WORKER)
    (source / "nebula-core").chmod(0o755)
    (source / "ui").mkdir()
    (source / "ui" / "index.html").write_text("<html>inert retained search</html>")
    manifest = ReleaseManifest(
        version="search-query-v1",
        schema_family=5,
        schema_min=5,
        schema_max=5,
        additive_migrations=True,
        migration_head=json.loads(
            (package / "v3" / "runtime-compatibility.json").read_text()
        )["manifest"]["migration_head"],
        core_executable="nebula-core",
        ui_directory="ui",
        features=["host.web-search-query/v1"],
        files={
            str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(source.rglob("*"))
            if path.is_file()
        },
    )
    raw = manifest.model_dump_json().encode()
    (source / MANIFEST_NAME).write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    retained = ReleaseStore(tmp_path / "query-retained")
    observed, installed = retained.install(source, expected_manifest_sha256=digest)
    await managed.coordinator.stage(observed, installed, digest)
    # The child cannot accidentally import from the source we staged.
    shutil.rmtree(source)
    return retained, installed


async def test_retained_worker_queries_one_host_owner_for_two_project_grants(
    settings, tmp_path
):
    managed = settings.managed
    owner = InertHostSearch()
    settings.host.observations.search = owner
    for project_id in ("admitted-project", "synthetic-project"):
        scope_id = "scope:" + project_id
        await managed.store.create(
            ScopePolicy(id=scope_id, engagement_id=project_id, web_search=True)
        )
        await managed.store.update(
            Engagement, project_id, {"scope_policy_id": scope_id}
        )
        await managed.store.create(
            ChatSession(
                id="session:" + project_id,
                engagement_id=project_id,
                title="Synthetic public research",
                provider_profile_id="inert-provider",
                model="inert",
            )
        )
        await managed.store.create(
            ChatTurn(
                id="turn:" + project_id,
                engagement_id=project_id,
                session_id="session:" + project_id,
                provider_profile_id="inert-provider",
                model="inert",
                tools_enabled=True,
                max_tool_calls=1,
            )
        )
    retained, installed = await _stage(managed, tmp_path)
    worker = None
    try:
        identity = await managed.host.spawn("search-query-v1", "execution")
        worker = await managed.host.rpc(identity.runtime_id)
        for project_id in ("admitted-project", "synthetic-project"):
            lease = await managed.coordinator.coordination.claim_project(
                project_id, identity.runtime_id
            )
            response = await worker.call(
                "worker.search",
                {
                    "lease": asdict(lease),
                    "session_id": "session:" + project_id,
                    "turn_id": "turn:" + project_id,
                },
            )
            assert response["pid"] != os.getpid()
            assert Path(response["module"]).is_relative_to(installed / "python")
            assert response["lease"] == asdict(lease)
            assert response["exit_code"] == 0
            observations = response["output"]["observations"]
            assert (
                next(item for item in observations if item["kind"] == "web_result")[
                    "url"
                ]
                == "https://public.example/advisory"
            )
            calls = await managed.store.list_entities(
                ToolCall, engagement_id=project_id
            )
            assert len(calls) == 1
            assert calls[0].status.value == "complete"
            assert calls[0].result == response["output"]
            assert calls[0].result_artifact_id == response["result_artifact_id"]
        assert owner.calls == [("public advisory", 1, "week")] * 2
        assert settings.host.observations.search is owner
        assert not owner.closed
    finally:
        if worker is not None:
            await worker.close()
        for directory in retained.root.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o700)
