"""Deployment status is a read: it never waits on coordination writes.

During project handoffs ``transfer`` holds the single ``deployment_host`` row
while it waits for project ownership. Status used to lock that same row, so
every status read queued behind the handoff and held one of the supervisor's
few pool connections until the pool ran dry.
"""

import asyncio

import pytest
from sqlalchemy import text

from nebula.v3.database import DeploymentHostRow
from tests.v3.test_deployment_lifecycle import host as host
from tests.v3.test_deployment_lifecycle import pair
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _hold_host_row(database, held: asyncio.Event, release: asyncio.Event):
    """Hold the host row the way a coordination write does, until released."""
    async with database.engine.connect() as connection:
        transaction = await connection.begin()
        if database.engine.dialect.name == "sqlite":
            # A coordination write on SQLite holds the database writer.
            await connection.exec_driver_sql("SELECT 1")
        await connection.execute(
            text("UPDATE deployment_host SET revision = revision WHERE key = 'host'")
        )
        held.set()
        await release.wait()
        await transaction.rollback()


async def test_status_answers_while_a_write_holds_the_host_row(host):
    coordinator, _store = host
    await pair(host)
    before = await coordinator.status()
    held, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_host_row(coordinator.database, held, release))
    reader = None
    try:
        await asyncio.wait_for(held.wait(), 10)
        # Never cancel a reader blocked on the row: its query only ends once
        # the holder releases, so a cancelled wait would hang the test.
        reader = asyncio.create_task(coordinator.status())
        done, _pending = await asyncio.wait({reader}, timeout=5)
        answered_while_held = reader in done
    finally:
        release.set()
        await holder
        if reader is not None:
            await reader
    assert answered_while_held, "status waited for the coordination write's host row"
    during = reader.result()
    assert during.revision == before.revision
    assert during.target_execution_version == before.target_execution_version
    assert [project.project_id for project in during.projects] == [
        project.project_id for project in before.projects
    ]


async def test_status_without_a_host_row_reports_the_defaults_a_write_creates(host):
    coordinator, _store = host
    async with coordinator.database.session() as session:
        await session.execute(text("DELETE FROM deployment_host"))
        await session.commit()
    status = await coordinator.status()
    async with coordinator.database.session() as session:
        # A read never creates the row; the first coordination write does.
        assert await session.get(DeploymentHostRow, "host") is None
    assert status.revision == 1
    assert status.api_version is None and status.target_execution_version is None
    assert status.detail == "No runtime has been promoted."
    async with coordinator.coordination._write() as session:
        created = await coordinator._host(session)
        assert created.revision == status.revision
