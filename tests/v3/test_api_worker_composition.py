"""Exercise the production API factory's transaction and cleanup ownership."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from nebula.v3.database import Database, RuntimeCommandRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.domain import Engagement
from nebula.v3.runtime_coordination import RuntimeCoordination
from nebula.v3.runtime_delivery import RuntimeDelivery
from nebula.v3.runtime_global_api import RuntimeGlobalApi
from nebula.v3.runtime_global_delivery import GlobalSqlDelivery
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.runtime_worker import api_application
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
POOL = PoolSettings(8, 12, 5, 3)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_actual_api_factory_keeps_receipts_and_global_writes_atomic(
    location, tmp_path, monkeypatch
):
    host = Database(location, pool_settings=POOL)
    await host.initialize()
    database = Database(location, bootstrap=False, pool_settings=POOL)
    ui = tmp_path / "release" / "ui"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<title>Synthetic retained UI</title>")
    context = SimpleNamespace(
        database=database,
        coordinator=DeploymentCoordinator(database),
        manifest=SimpleNamespace(ui_directory="ui"),
        bootstrap=SimpleNamespace(
            operator_token=SecretStr("composition-test-token"),
            credential_directory=str(tmp_path / "credentials"),
            data_root=str(tmp_path),
            release_root=str(ui.parent),
        ),
    )
    try:
        async with api_application(context) as app:
            assert app.state.store.database is database
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="https://synthetic.test",
                headers={"Authorization": "Bearer composition-test-token"},
            ) as client:
                first = await client.post(
                    "/api/v1/engagements",
                    json={"name": "Atomic project"},
                    headers={"Idempotency-Key": "first"},
                )
                assert first.status_code == 201, first.text
                replay = await client.post(
                    "/api/v1/engagements",
                    json={"name": "Atomic project"},
                    headers={"Idempotency-Key": "first"},
                )
                assert replay.json() == first.json()
                assert (
                    replay.headers["x-nebula-command-id"]
                    == first.headers["x-nebula-command-id"]
                )
                original = GlobalSqlDelivery._response

                async def fail_after_sql(self, intent, actor):
                    await original(self, intent, actor)
                    raise RuntimeError("Synthetic failure after insert, before receipt")

                monkeypatch.setattr(GlobalSqlDelivery, "_response", fail_after_sql)
                failed = await client.post(
                    "/api/v1/engagements",
                    json={"name": "Must roll back"},
                    headers={"Idempotency-Key": "failed"},
                )
                assert failed.status_code == 500
                assert await app.state.store.count(Engagement) == 1
                async with database.session() as session:
                    assert (
                        await session.scalar(
                            select(func.count()).select_from(RuntimeCommandRow)
                        )
                        == 1
                    )
        # api_application closes its stores, while the worker still owns this
        # shared database until its heartbeat and private server have stopped.
        assert database._initialized
        assert await NebulaStore(database).count(Engagement) == 1
    finally:
        await database.dispose()
        await host.dispose()


async def test_global_delivery_rejects_a_distinct_transaction_owner(location, tmp_path):
    first = Database(location, pool_settings=POOL)
    second = Database(location, pool_settings=POOL)
    try:
        await first.initialize()
        application = RuntimeGlobalApi(second)
        delivery = RuntimeDelivery(
            RuntimeCoordination(first), RuntimeVault(Path(tmp_path) / "vault")
        )
        with pytest.raises(ValueError, match="one database transaction owner"):
            GlobalSqlDelivery(delivery, application)
        await application.close()
    finally:
        await first.dispose()
        await second.dispose()
