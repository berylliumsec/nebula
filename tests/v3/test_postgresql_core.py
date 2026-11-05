"""Core persistence contracts exercised against an isolated PostgreSQL schema."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.engine import make_url

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
def postgresql_url():
    configured = os.getenv("NEBULA_TEST_POSTGRES_URL")
    if not configured:
        pytest.skip("NEBULA_TEST_POSTGRES_URL is required for PostgreSQL coverage")
    url = make_url(Database._normalize_url(configured))
    assert url.get_backend_name() == "postgresql"
    schema = f"nebula_test_{uuid4().hex}"
    engine = create_engine(url)
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
def postgresql_store(postgresql_url):
    store = NebulaStore(postgresql_url)
    try:
        yield store
    finally:
        store.event_history.close()
        store.database.dispose()


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


def test_postgresql_bootstrap_is_atomic_between_processes(postgresql_url):
    code = """
import os
import sys
from nebula.v3.database import Database
from nebula.v3.setup import bootstrap_scratch_project
from nebula.v3.storage import NebulaStore
print('ready', flush=True)
sys.stdin.readline()
store = NebulaStore(Database(os.environ['NEBULA_TEST_POSTGRES_URL']))
print(bootstrap_scratch_project(store), flush=True)
store.event_history.close()
store.database.dispose()
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
        assert store.count(Engagement) == 1
        assert store.database.health() == {
            "database": "ok",
            "dialect": "postgresql",
            "schema_version": CURRENT_SCHEMA_VERSION,
        }
        with store.database.session() as session:
            marker = session.get(BootstrapStateRow, SCRATCH_PROJECT_BOOTSTRAP_KEY)
            assert marker.status == "complete"
            assert marker.engagement_id == "scratch-project"
        assert "session_projections" in inspect(store.database.engine).get_table_names()
    finally:
        store.event_history.close()
        store.database.dispose()


def test_postgresql_run_event_sequence_and_retry_are_atomic(postgresql_store):
    store = postgresql_store

    def append(number):
        return store.append_event("parallel-run", "tick", {"number": number})

    with ThreadPoolExecutor(max_workers=6) as pool:
        events = list(pool.map(append, range(18)))
    assert sorted(item.sequence for item in events) == list(range(1, 19))
    first = store.append_event(
        "parallel-run", "once", {"text": 'π\\"_%'}, idempotency_key="once"
    )
    assert (
        store.append_event(
            "parallel-run", "once", {"text": 'π\\"_%'}, idempotency_key="once"
        )
        == first
    )
    assert [item.sequence for item in store.replay_events("parallel-run")] == list(
        range(1, 20)
    )


def test_postgresql_alembic_environment_accepts_bare_url(postgresql_url, monkeypatch):
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
        assert database.health()["schema_version"] == CURRENT_SCHEMA_VERSION
        with database.session() as session:
            marker = session.get(BootstrapStateRow, SCRATCH_PROJECT_BOOTSTRAP_KEY)
            assert marker.status == "complete"
    finally:
        database.dispose()


def test_postgresql_transaction_and_direct_operation_writers_share_lock(
    postgresql_store,
):
    store = postgresql_store
    project = store.create(Engagement(name="Operation stream"))
    attempted = Event()
    operation_id = "mixed-stream"

    def before(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            attempted.set()

    def direct():
        return store.append_operation_event(
            operation_id, "fixture", project.id, "direct", idempotency_key="direct"
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.transaction() as transaction:
            first = transaction.append_operation_event(
                operation_id, "fixture", project.id, "state", idempotency_key="state"
            )
            assert (
                transaction.append_operation_event(
                    operation_id,
                    "fixture",
                    project.id,
                    "state",
                    idempotency_key="state",
                )
                == first
            )
            second = transaction.append_operation_event(
                operation_id, "fixture", project.id, "second"
            )
            assert (first.sequence, second.sequence) == (1, 2)
            event.listen(store.database.engine, "before_cursor_execute", before)
            try:
                future = pool.submit(direct)
                assert attempted.wait(5), (
                    "direct writer did not attempt its stream lock"
                )
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.1)
            finally:
                event.remove(store.database.engine, "before_cursor_execute", before)
        assert future.result(timeout=5).sequence == 3
    assert [item.sequence for item in store.replay_operation_events(operation_id)] == [
        1,
        2,
        3,
    ]


def test_postgresql_json_filters_references_and_timezones_survive_reopen(
    postgresql_store, postgresql_url
):
    store = postgresql_store
    project = store.create(Engagement(name="Unicode π and quotes ' \""))
    instant = utc_now().astimezone(timezone(timedelta(hours=5, minutes=30)))
    target = "item_π%'\\\""
    asset = store.create(
        Asset(
            engagement_id=project.id,
            name=target,
            tags=[target, "other"],
            metadata={"empty": None, "count": 7, "quote": target},
            created_at=instant,
            updated_at=instant,
        )
    )
    assert store.find_entities(Asset, {"metadata.quote": target}) == [asset]
    assert store.find_entities(Asset, {"metadata.count": 7}) == [asset]
    assert store.find_entities(Asset, {"metadata.empty": None}) == [asset]
    assert store.find_entities(Asset, {"metadata.absent": None}) == [asset]
    assert list(store.iter_reference_candidates(Asset, [("tags", target, True)])) == [
        asset
    ]
    with store.database.session() as session:
        stored_time = session.scalar(
            select(EntityRow.created_at).where(EntityRow.id == asset.id)
        )
    assert stored_time == instant
    assert stored_time.tzinfo is not None
    reopened = NebulaStore(postgresql_url)
    try:
        assert reopened.get(Asset, asset.id) == asset
        assert bootstrap_scratch_project(reopened) is None
    finally:
        reopened.event_history.close()
        reopened.database.dispose()


def test_postgresql_archive_delete_is_revision_guarded(postgresql_store):
    store = postgresql_store
    project = store.create(Engagement(name="Archive", status=EngagementStatus.ARCHIVED))
    asset = store.create(Asset(engagement_id=project.id, name="archived child"))
    with pytest.raises(ConflictError, match="Project changed"):
        store.delete_archived_engagement(project.id, expected_revision=2)
    assert store.get(Asset, asset.id) == asset
    store.delete_archived_engagement(project.id, expected_revision=project.revision)
    with pytest.raises(NotFoundError):
        store.get(Engagement, project.id)
    assert store.find_entities(Asset, {}, engagement_id=project.id) == []


def test_postgresql_session_projection_retries_share_one_revision(postgresql_store):
    store = postgresql_store
    project = store.create(Engagement(name="Durable chat"))
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Persisted",
            provider_profile_id="inert-fixture",
            model="fixture",
        )
    )
    with ThreadPoolExecutor(max_workers=6) as pool:
        states = list(pool.map(lambda _: session_state(store, chat), range(12)))
    assert {state["revision"] for state in states} == {1}
    turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            provider_profile_id="inert-fixture",
            model="fixture",
        )
    )
    with ThreadPoolExecutor(max_workers=6) as pool:
        states = list(pool.map(lambda _: session_state(store, chat), range(12)))
    assert {state["revision"] for state in states} == {2}
    assert store.list_session_entities(ChatTurn, chat.id) == [turn]
