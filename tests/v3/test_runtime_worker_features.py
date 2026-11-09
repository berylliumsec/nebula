"""Canonical release features cross the actual worker admission and main entry."""

from contextlib import asynccontextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from uuid import uuid4

import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import inspect, text

from nebula.v3.database import (
    Database,
    ProviderToolManifestRow,
    RuntimeProcessRow,
    RuntimeWorkBatchProjectRow,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployment_protocol import ReleaseManifest, WorkerIdentity
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.process_identity import process_birth
from nebula.v3.release_store import MANIFEST_NAME, ReleaseStore
from nebula.v3.runtime_context_requirements import (
    HOST_SEARCH_FEATURE,
    PROVIDER_TOOLS_FEATURE,
    PROVIDER_TOOLS_OWNERSHIP_FEATURE,
    STANDING_INSTRUCTIONS_FEATURE,
    WORK_BATCH_FEATURE,
)
from nebula.v3.runtime_http import internal_headers
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.runtime_processes import RuntimeProcessHost, WorkerBootstrap
from nebula.v3 import runtime_worker
from tests.v3.test_runtime_coordination import location as location

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="Private Linux worker entry"
    ),
]
SETTINGS = PoolSettings(5, 12, 5, 2)
TOKEN = "synthetic-canonical-worker-admission-token"
PACKAGE = Path(__file__).resolve().parents[2] / "src" / "nebula"
COMPATIBILITY = json.loads((PACKAGE / "v3" / "runtime-compatibility.json").read_text())[
    "manifest"
]
FEATURES = COMPATIBILITY["features"]
ENTRY = """from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'source'))
from nebula.v3 import runtime_worker
print(str(Path(runtime_worker.__file__).resolve()), flush=True)
runtime_worker.main()
"""


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def worker_host(location, tmp_path):
    database = Database(location, pool_settings=SETTINGS)
    coordinator = DeploymentCoordinator(database)
    host = RuntimeProcessHost(
        coordinator, tmp_path / "core", pool_settings=SETTINGS, operator_token=TOKEN
    )
    retained = ReleaseStore(tmp_path / "retained")
    await host.start()

    async def stage(version, *, features=FEATURES):
        source = tmp_path / ("source-" + version)
        source.mkdir()
        shutil.copytree(
            PACKAGE,
            source / "source" / "nebula",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (source / "nebula-core").write_text("#!" + sys.executable + "\n" + ENTRY)
        (source / "nebula-core").chmod(0o755)
        (source / "ui").mkdir()
        (source / "ui" / "index.html").write_text(
            "<html>synthetic worker admission</html>"
        )
        manifest = ReleaseManifest(
            **{**COMPATIBILITY, "features": list(features)},
            version=version,
            core_executable="nebula-core",
            ui_directory="ui",
            files={
                str(path.relative_to(source)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in sorted(source.rglob("*"))
                if path.is_file()
            },
        )
        raw = manifest.model_dump_json().encode()
        (source / MANIFEST_NAME).write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        accepted, installed = retained.install(source, expected_manifest_sha256=digest)
        await coordinator.stage(accepted, installed, digest)
        shutil.rmtree(source)
        return SimpleNamespace(manifest=manifest, installed=installed, digest=digest)

    try:
        yield SimpleNamespace(
            host=host, database=database, coordinator=coordinator, stage=stage
        )
    finally:
        # Only these subprocess children may be retired. Refusal tests bind this
        # current process as a valid pre-factory identity and never launch it.
        for runtime_id, child in tuple(host.children.items()):
            if child.poll() is None:
                await host.retire(runtime_id, timeout=10)
        await host.close()
        await database.dispose()
        for path in retained.root.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)


@pytest.mark.parametrize("role", ["api", "execution"])
async def test_actual_main_admits_canonical_features_for_each_role(worker_host, role):
    value = worker_host
    assert {
        PROVIDER_TOOLS_FEATURE,
        PROVIDER_TOOLS_OWNERSHIP_FEATURE,
        STANDING_INSTRUCTIONS_FEATURE,
        HOST_SEARCH_FEATURE,
        WORK_BATCH_FEATURE,
    } <= set(FEATURES)
    release = await value.stage("canonical-" + role)
    identity = await value.host.spawn(release.manifest.version, role)
    assert identity is not None and identity.pid != os.getpid()
    async with value.database.session() as session:
        row = await session.get(RuntimeProcessRow, identity.runtime_id)
        assert row.state == "ready"
    client = await value.host.rpc(identity.runtime_id)
    try:
        assert await client.call("worker.identity", {}) == identity.model_dump(
            mode="json"
        )
        if role == "execution":
            assert (await client.call("execution.status/v1", {}))[
                "state"
            ] == "unassigned"
        else:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=identity.endpoint[7:]),
                base_url="http://private",
                trust_env=False,
            ) as http:
                headers = [
                    *internal_headers(client.secret, "http"),
                    (b"authorization", ("Bearer " + TOKEN).encode()),
                ]
                response = await http.get("/api/v1/deployment/status", headers=headers)
                assert response.status_code == 200, response.text
                assert (
                    response.headers["x-nebula-api-version"] == release.manifest.version
                )
    finally:
        await client.close()
    log = (value.host.root / (identity.runtime_id + ".log")).read_text()
    assert (
        str(release.installed / "source" / "nebula" / "v3" / "runtime_worker.py") in log
    )
    assert not (release.installed / "source" / "tests").exists()


@pytest.mark.parametrize("role", ["api", "execution"])
@pytest.mark.parametrize(
    "refusal", ["unknown-feature", "missing-provider-table", "missing-work-batch-table"]
)
async def test_actual_worker_refuses_before_factory_or_readiness(
    worker_host, role, refusal, monkeypatch
):
    value = worker_host
    features = (
        [*FEATURES, "unsupported.future/v1"]
        if refusal == "unknown-feature"
        else FEATURES
    )
    release = await value.stage("refused-" + role, features=features)
    missing = {
        "missing-provider-table": ProviderToolManifestRow,
        "missing-work-batch-table": RuntimeWorkBatchProjectRow,
    }.get(refusal)
    if missing is not None:
        async with value.database.engine.begin() as connection:
            await connection.run_sync(lambda sync: missing.__table__.drop(sync))
    async with value.database.session() as session:
        head = await session.scalar(text("SELECT version_num FROM alembic_version"))
    reference = str(uuid4())
    endpoint = value.host.socket_root / (uuid4().hex + ".refused.sock")
    runtime_id = await value.coordinator.reserve_worker(
        release.manifest.version,
        role=role,
        endpoint="unix://" + str(endpoint),
        credential_reference=reference,
    )
    value.host.credentials.create(reference=reference)
    identity = WorkerIdentity(
        runtime_id=runtime_id,
        role=role,
        version=release.manifest.version,
        pid=os.getpid(),
        process_birth=process_birth(os.getpid()),
        endpoint="unix://" + str(endpoint),
    )
    await value.coordinator.bind_spawn(identity)
    bootstrap = WorkerBootstrap(
        identity=identity,
        data_root=str(value.host.data_root),
        release_root=str(release.installed),
        manifest_sha256=release.digest,
        database=value.database.url,
        credential_reference=reference,
        credential_directory=str(value.host.credentials.directory),
        process_origin=value.host.launch_origin,
        pool_settings=asdict(SETTINGS),
        operator_token=SecretStr(TOKEN),
    )
    factories = []

    @asynccontextmanager
    async def forbidden_factory(context):
        factories.append(context.bootstrap.identity.role)
        raise AssertionError("A refused worker crossed its factory boundary")
        yield  # pragma: no cover

    monkeypatch.setattr(runtime_worker, "api_application", forbidden_factory)
    detail = {
        "unknown-feature": "declared release features",
        "missing-provider-table": "additive provider tool migrations",
        "missing-work-batch-table": "additive Work batch migrations",
    }[refusal]
    with pytest.raises(RuntimeIpcError, match=detail):
        await runtime_worker.run_worker(bootstrap, execution_factory=forbidden_factory)
    assert not factories and not endpoint.exists()
    assert not (value.host.root / "workers" / runtime_id).exists()
    async with value.database.session() as session:
        row = await session.get(RuntimeProcessRow, runtime_id)
        assert row.state == "starting"
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == head
        )
    if missing is not None:
        async with value.database.engine.connect() as connection:
            assert not await connection.run_sync(
                lambda sync: inspect(sync).has_table(missing.__tablename__)
            )
