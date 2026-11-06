"""Core persistence contracts exercised against an isolated PostgreSQL schema."""

from tests.v3.async_test_support import (
    inspect_database,
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from concurrent.futures import TimeoutError
from datetime import timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import asyncio
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from nebula.v3.database import (
    BootstrapStateRow,
    CURRENT_SCHEMA_VERSION,
    Database,
    EntityRow,
    SCRATCH_PROJECT_BOOTSTRAP_KEY,
)
from nebula.v3.domain import (
    Asset,
    ChatSession,
    ChatTurn,
    Engagement,
    EngagementStatus,
    utc_now,
)
from nebula.v3.session_state import session_state
from nebula.v3.setup import bootstrap_scratch_project
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError


@pytest.fixture
def postgresql_url(monkeypatch):
    for name, value in {
        "ADMIN_RESERVE": "5",
        "OTHER_CONNECTIONS": "12",
        "PEAK_PROCESSES": "6",
        "POOL_SIZE": "2",
    }.items():
        monkeypatch.setenv("NEBULA_V3_DB_" + name, value)
    configured = os.getenv("NEBULA_TEST_POSTGRES_URL")
    if not configured:
        pytest.skip("NEBULA_TEST_POSTGRES_URL is required for PostgreSQL coverage")
    url = make_url(Database._normalize_url(configured))
    assert url.get_backend_name() == "postgresql"
    schema = f"nebula_test_{uuid4().hex}"
    engine = create_engine(url, poolclass=NullPool)
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    options = " ".join(
        value
        for value in (url.query.get("options", ""), f"-csearch_path={schema}")
        if value
    )
    scoped = url.update_query_dict({"options": options})
    try:
        yield scoped.render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


@pytest.fixture
async def postgresql_store(postgresql_url):
    store = NebulaStore(postgresql_url)
    try:
        yield store
    finally:
        (await store.event_history.close())
        (await store.database.dispose())


@pytest.mark.parametrize("scheme", ["postgresql", "postgres", "postgresql+psycopg"])
def test_postgresql_url_selects_packaged_driver(scheme):
    location = f"{scheme}://nebula:p%40ss@localhost/example?sslmode=require"
    normalized = Database._normalize_url(location)
    assert normalized == (
        "postgresql+psycopg://nebula:p%40ss@localhost/example?sslmode=require"
    )
    assert make_url(normalized).password == "p@ss"


def test_postgresql_driver_is_required_in_frozen_core():
    from scripts.build_nebula_core import REQUIRED_RUNTIME_IMPORTS
    from scripts.package_audit import (
        ArtifactAuditError,
        REQUIRED_MEMBERS,
        validate_members,
    )

    required = {
        "psycopg",
        "psycopg_binary",
        "sqlalchemy.dialects.postgresql.psycopg",
    }
    assert required <= set(REQUIRED_RUNTIME_IMPORTS)
    members = {alternatives[0] for alternatives in REQUIRED_MEMBERS}
    for missing in required:
        with pytest.raises(ArtifactAuditError, match="required members absent"):
            validate_members(members - {missing})
    build_script = (
        Path(__file__).resolve().parents[2] / "scripts/build_nebula_core.py"
    ).read_text()
    for package in ("psycopg", "psycopg_binary"):
        assert f'"--collect-all",\n        "{package}"' in build_script


async def test_postgresql_bootstrap_is_atomic_between_processes(postgresql_url):
    code = """
import asyncio
import os
import sys
from nebula.v3.async_utils import close_store_resources
from nebula.v3.database import Database
from nebula.v3.setup import bootstrap_scratch_project
from nebula.v3.storage import NebulaStore
print('ready', flush=True)
sys.stdin.readline()
async def main():
    store = NebulaStore(Database(os.environ['NEBULA_TEST_POSTGRES_URL']))
    try:
        print(await bootstrap_scratch_project(store), flush=True)
    finally:
        await close_store_resources([store])
asyncio.run(main())
"""
    environment = {
        **os.environ,
        "NEBULA_TEST_POSTGRES_URL": postgresql_url,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
    }
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            assert stdout.strip() == "scratch-project"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    store = NebulaStore(postgresql_url)
    try:
        assert (await store.count(Engagement)) == 1
        assert (await store.database.health()) == {
            "database": "ok",
            "dialect": "postgresql",
            "schema_version": CURRENT_SCHEMA_VERSION,
        }
        async with store.database.session() as session:
            marker = await session.get(BootstrapStateRow, SCRATCH_PROJECT_BOOTSTRAP_KEY)
            assert marker.status == "complete"
            assert marker.engagement_id == "scratch-project"
        assert "session_projections" in (
            await inspect_database(store.database, "get_table_names")
        )
    finally:
        (await store.event_history.close())
        (await store.database.dispose())


async def test_postgresql_run_event_sequence_and_retry_are_atomic(postgresql_store):
    store = postgresql_store

    async def append(number):
        return await store.append_event("parallel-run", "tick", {"number": number})

    events = list((await asyncio.gather(*(append(_item) for _item in range(18)))))
    assert sorted(item.sequence for item in events) == list(range(1, 19))
    first = await store.append_event(
        "parallel-run", "once", {"text": 'π\\"_%'}, idempotency_key="once"
    )
    assert (
        await store.append_event(
            "parallel-run", "once", {"text": 'π\\"_%'}, idempotency_key="once"
        )
    ) == first
    assert [
        item.sequence for item in (await store.replay_events("parallel-run"))
    ] == list(range(1, 20))


async def test_postgresql_alembic_environment_accepts_bare_url(
    postgresql_url, monkeypatch
):
    from alembic import command
    from alembic.config import Config

    bare_url = postgresql_url.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("NEBULA_V3_DATABASE_URL", bare_url)
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[2] / "src/nebula/v3/migrations"),
    )
    command.upgrade(config, "head")
    database = Database(bare_url)
    try:
        assert (await database.health())["schema_version"] == CURRENT_SCHEMA_VERSION
        async with database.session() as session:
            marker = await session.get(BootstrapStateRow, SCRATCH_PROJECT_BOOTSTRAP_KEY)
            assert marker.status == "complete"
    finally:
        (await database.dispose())


async def test_postgresql_transaction_and_direct_operation_writers_share_lock(
    postgresql_store,
):
    store = postgresql_store
    project = await store.create(Engagement(name="Operation stream"))
    attempted = asyncio.Event()
    operation_id = "mixed-stream"

    def before(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            attempted.set()

    async def direct():
        return await store.append_operation_event(
            operation_id, "fixture", project.id, "direct", idempotency_key="direct"
        )

    async with store.transaction() as transaction:
        first = await transaction.append_operation_event(
            operation_id, "fixture", project.id, "state", idempotency_key="state"
        )
        assert (
            await transaction.append_operation_event(
                operation_id, "fixture", project.id, "state", idempotency_key="state"
            )
            == first
        )
        second = await transaction.append_operation_event(
            operation_id, "fixture", project.id, "second"
        )
        assert (first.sequence, second.sequence) == (1, 2)
        event.listen(store.database.engine.sync_engine, "before_cursor_execute", before)
        future = asyncio.create_task(direct())
        try:
            await asyncio.wait_for(attempted.wait(), 5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(future), 0.1)
        finally:
            event.remove(
                store.database.engine.sync_engine, "before_cursor_execute", before
            )
    assert (await asyncio.wait_for(future, 5)).sequence == 3
    assert [
        item.sequence for item in (await store.replay_operation_events(operation_id))
    ] == [
        1,
        2,
        3,
    ]


async def test_postgresql_json_filters_references_and_timezones_survive_reopen(
    postgresql_store, postgresql_url
):
    store = postgresql_store
    project = await store.create(Engagement(name="Unicode π and quotes ' \""))
    instant = utc_now().astimezone(timezone(timedelta(hours=5, minutes=30)))
    target = "item_π%'\\\""
    asset = await store.create(
        Asset(
            engagement_id=project.id,
            name=target,
            tags=[target, "other"],
            metadata={"empty": None, "count": 7, "quote": target},
            created_at=instant,
            updated_at=instant,
        )
    )
    assert (await store.find_entities(Asset, {"metadata.quote": target})) == [asset]
    assert (await store.find_entities(Asset, {"metadata.count": 7})) == [asset]
    assert (await store.find_entities(Asset, {"metadata.empty": None})) == [asset]
    assert (await store.find_entities(Asset, {"metadata.absent": None})) == [asset]
    assert list(
        [
            item
            async for item in store.iter_reference_candidates(
                Asset, [("tags", target, True)]
            )
        ]
    ) == [asset]
    async with store.database.session() as session:
        stored_time = await session.scalar(
            select(EntityRow.created_at).where(EntityRow.id == asset.id)
        )
    assert stored_time == instant
    assert stored_time.tzinfo is not None
    reopened = NebulaStore(postgresql_url)
    try:
        assert (await reopened.get(Asset, asset.id)) == asset
        assert (await bootstrap_scratch_project(reopened)) is None
    finally:
        (await reopened.event_history.close())
        (await reopened.database.dispose())


async def test_postgresql_archive_delete_is_revision_guarded(postgresql_store):
    store = postgresql_store
    project = await store.create(
        Engagement(name="Archive", status=EngagementStatus.ARCHIVED)
    )
    asset = await store.create(Asset(engagement_id=project.id, name="archived child"))
    with pytest.raises(ConflictError, match="Project changed"):
        (await store.delete_archived_engagement(project.id, expected_revision=2))
    assert (await store.get(Asset, asset.id)) == asset
    (
        await store.delete_archived_engagement(
            project.id, expected_revision=project.revision
        )
    )
    with pytest.raises(NotFoundError):
        (await store.get(Engagement, project.id))
    assert (await store.find_entities(Asset, {}, engagement_id=project.id)) == []


async def test_postgresql_session_projection_retries_share_one_revision(
    postgresql_store,
):
    store = postgresql_store
    project = await store.create(Engagement(name="Durable chat"))
    chat = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Persisted",
            provider_profile_id="inert-fixture",
            model="fixture",
        )
    )
    states = list(
        (
            await asyncio.gather(
                *((lambda _: session_state(store, chat))(_item) for _item in range(12))
            )
        )
    )
    assert {state["revision"] for state in states} == {1}
    turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            provider_profile_id="inert-fixture",
            model="fixture",
        )
    )
    states = list(
        (
            await asyncio.gather(
                *((lambda _: session_state(store, chat))(_item) for _item in range(12))
            )
        )
    )
    assert {state["revision"] for state in states} == {2}
    assert (await store.list_session_entities(ChatTurn, chat.id)) == [turn]


pytestmark = pytest.mark.anyio
