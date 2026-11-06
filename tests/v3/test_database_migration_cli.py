"""Offline migration entry point and a migrated PostgreSQL Core over a real LAN."""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.cli import app
from nebula.v3.domain import (
    AgentRun,
    ChatMessage,
    ChatSession,
    Engagement,
    ProviderProfile,
    RunBudget,
    RunStatus,
    utc_now,
)
from nebula.v3.missions import MissionService
from nebula.v3.storage import NebulaStore
from tests.v3.test_missions import RecordingProvider


def test_postgresql_migration_is_discoverable_in_cli_help():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "migrate-to-postgresql" in result.stdout
    help_result = CliRunner().invoke(
        app, ["migrate-to-postgresql", "--help"], terminal_width=160
    )
    assert help_result.exit_code == 0
    help_text = re.sub(r"\x1b\[[0-9;]*m", "", help_result.stdout)
    assert "--target-url-file" in help_text
    assert "--confirm-core-stopped" in help_text


def test_schema_migrate_uses_psycopg3_for_a_bare_postgresql_url(tmp_path, monkeypatch):
    observed = {}
    monkeypatch.setenv(
        "NEBULA_V3_DATABASE_URL",
        "postgresql://synthetic:hidden-password@localhost/synthetic",
    )
    monkeypatch.setattr("nebula.v3.cli._diagnostic_manager", lambda *_args: None)
    monkeypatch.setattr(
        "nebula.v3.cli.command.upgrade",
        lambda config, revision: observed.update(
            url=config.get_main_option("sqlalchemy.url"), revision=revision
        ),
    )
    result = CliRunner().invoke(app, ["migrate", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert observed["url"].startswith("postgresql+psycopg://")
    assert observed["revision"] == "head"
    assert "hidden-password" not in result.output


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_serve_preserves_local_mission_checkpoint_location(
    tmp_path, monkeypatch, dialect
):
    observed = {}
    store = SimpleNamespace(
        database=SimpleNamespace(
            engine=SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        )
    )
    monkeypatch.setattr(
        "nebula.v3.cli._services", lambda *_args, **_kwargs: (tmp_path, store, None)
    )
    monkeypatch.setattr(
        "nebula.v3.cli.CrossEncoderReranker",
        lambda *_args, **_kwargs: SimpleNamespace(start_if_present=lambda: None),
    )
    monkeypatch.setattr(
        "nebula.v3.cli.ChromaKnowledgeIndex", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "nebula.v3.cli.default_runtime_platform", lambda **_kwargs: None
    )

    def create(*_args, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(state=SimpleNamespace())

    monkeypatch.setattr("nebula.v3.cli.create_app", create)
    monkeypatch.setattr(
        "nebula.v3.cli.uvicorn.Server",
        lambda *_args, **_kwargs: SimpleNamespace(run=lambda **_options: None),
    )
    result = CliRunner().invoke(
        app,
        ["serve", "--data-dir", str(tmp_path), "--port", "0", "--token", "synthetic"],
    )
    assert result.exit_code == 0, result.output
    assert observed["mission_checkpoint_path"] == (
        tmp_path / "mission-checkpoints.db" if dialect == "postgresql" else None
    )


async def test_postgresql_inspect_reads_existing_sqlite_without_target_or_changes(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("NEBULA_V3_DATABASE_URL", raising=False)
    store = NebulaStore(tmp_path / "nebula.db")
    (
        await store.create(
            Engagement(id="inspect-project", name="Synthetic migration source")
        )
    )
    (await store.database.engine.dispose())
    before = (tmp_path / "nebula.db").read_bytes()
    result = await asyncio.to_thread(
        CliRunner().invoke,
        app,
        ["migrate-to-postgresql", "--data-dir", str(tmp_path), "--inspect"],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report
    assert (tmp_path / "nebula.db").read_bytes() == before


def test_postgresql_copy_requires_explicit_stopped_core_acknowledgment(tmp_path):
    result = CliRunner().invoke(
        app, ["migrate-to-postgresql", "--data-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "--confirm-core-stopped" in result.output
    assert "Stop every Core" in result.output


def test_postgresql_copy_explains_missing_target(tmp_path, monkeypatch):
    monkeypatch.delenv("NEBULA_V3_DATABASE_URL", raising=False)
    result = CliRunner().invoke(
        app,
        [
            "migrate-to-postgresql",
            "--data-dir",
            str(tmp_path),
            "--confirm-core-stopped",
        ],
    )
    assert result.exit_code == 1
    assert "NEBULA_V3_DATABASE_URL" in result.output
    assert "--target-url-file" in result.output


def test_postgresql_copy_uses_environment_and_creates_private_receipt(
    tmp_path, monkeypatch
):
    from nebula.v3 import database_migration

    target = "postgresql+psycopg://synthetic:very-private@localhost/synthetic"
    monkeypatch.setenv("NEBULA_V3_DATABASE_URL", target)
    observed = {}
    report = {"status": "verified", "tables": {"entities": {"rows": 1}}}

    def migrate(source, url, **kwargs):
        observed.update(source=source, url=url, **kwargs)
        return SimpleNamespace(as_dict=lambda: report)

    monkeypatch.setattr(database_migration, "migrate_sqlite_to_postgresql", migrate)
    receipt = tmp_path / "receipt.json"
    result = CliRunner().invoke(
        app,
        [
            "migrate-to-postgresql",
            "--data-dir",
            str(tmp_path),
            "--confirm-core-stopped",
            "--receipt",
            str(receipt),
        ],
    )
    assert result.exit_code == 0, result.output
    assert observed["source"] == tmp_path / "nebula.db"
    assert observed["url"] == target
    assert observed["confirm_core_stopped"] is True
    assert json.loads(result.stdout) == report
    assert json.loads(receipt.read_text()) == report
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert "very-private" not in result.output
    assert os.getenv("NEBULA_V3_DATABASE_URL") == target


def test_postgresql_url_file_overrides_environment_and_progress_is_separate(
    tmp_path, monkeypatch
):
    from nebula.v3 import database_migration

    monkeypatch.setenv("NEBULA_V3_DATABASE_URL", "postgresql://ignored")
    target = "postgresql+psycopg://synthetic:file-password@localhost/synthetic"
    url_file = tmp_path / "target-url"
    url_file.write_text(target + "\n")
    url_file.chmod(0o600)
    source = tmp_path / "snapshot.db"
    observed = {}

    def migrate(path, url, **kwargs):
        observed.update(path=path, url=url, batch_size=kwargs["batch_size"])
        kwargs["progress"]({"phase": "copy", "table": "entities", "rows": 2})
        return SimpleNamespace(as_dict=lambda: {"status": "verified"})

    monkeypatch.setattr(database_migration, "migrate_sqlite_to_postgresql", migrate)
    result = CliRunner().invoke(
        app,
        [
            "migrate-to-postgresql",
            "--source",
            str(source),
            "--target-url-file",
            str(url_file),
            "--confirm-core-stopped",
            "--batch-size",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert observed == {"path": source, "url": target, "batch_size": 2}
    assert json.loads(result.stdout) == {"status": "verified"}
    assert '"phase": "copy"' in result.stderr
    assert "file-password" not in result.output


def test_postgresql_receipt_refuses_overwrite_before_copy(tmp_path, monkeypatch):
    from nebula.v3 import database_migration

    receipt = tmp_path / "receipt.json"
    receipt.write_text("previous receipt")
    monkeypatch.setattr(
        database_migration,
        "migrate_sqlite_to_postgresql",
        lambda *_args, **_kwargs: pytest.fail("A refused receipt must not copy data"),
    )
    result = CliRunner().invoke(
        app,
        [
            "migrate-to-postgresql",
            "--confirm-core-stopped",
            "--receipt",
            str(receipt),
        ],
    )
    assert result.exit_code == 1
    assert "Receipt already exists" in result.output
    assert receipt.read_text() == "previous receipt"


@pytest.mark.parametrize("expected_error", [True, False])
def test_postgresql_errors_do_not_reveal_connection_credentials(
    tmp_path, monkeypatch, expected_error
):
    from nebula.v3 import database_migration

    target = "postgresql+psycopg://synthetic:connection-secret@localhost/synthetic"
    monkeypatch.setenv("NEBULA_V3_DATABASE_URL", target)

    def fail(*_args, **_kwargs):
        error = database_migration.MigrationError if expected_error else RuntimeError
        raise error(f"Could not connect to {target}: connection-secret")

    monkeypatch.setattr(database_migration, "migrate_sqlite_to_postgresql", fail)
    result = CliRunner().invoke(
        app,
        [
            "migrate-to-postgresql",
            "--data-dir",
            str(tmp_path),
            "--confirm-core-stopped",
        ],
    )
    assert result.exit_code == 1
    assert "connection-secret" not in result.output
    assert target not in result.output
    assert "Traceback" not in result.output


@asynccontextmanager
async def _migrated_lan_core(url, root, address):
    store = NebulaStore(url)
    core = create_app(
        store,
        auth_token="synthetic-migration-admin",
        artifact_store=ArtifactStore(root / "artifacts"),
        execution_data_root=root,
        mission_checkpoint_path=root / "mission-checkpoints.db",
        allow_internal_event_append=True,
    )
    with socket.socket() as probe:
        probe.bind((address, 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(core, host=address, port=port, log_level="error")
    )
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    client = httpx.Client(
        base_url=f"http://{address}:{port}/api/v1/",
        headers={"Authorization": "Bearer synthetic-migration-admin"},
        trust_env=False,
        timeout=20,
    )
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                if client.get("health").is_success:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("Disposable PostgreSQL LAN Core did not start")
        yield client
    finally:
        client.close()
        server.should_exit = True
        worker.join(timeout=40)
        assert not worker.is_alive(), "Disposable PostgreSQL LAN Core did not stop"
        (await store.database.engine.dispose())


@pytest.mark.skipif(
    not os.getenv("NEBULA_TEST_POSTGRES_CLI_URL"),
    reason="NEBULA_TEST_POSTGRES_CLI_URL is required for the real migration/LAN journey",
)
async def test_postgresql_cli_copy_preserves_chat_and_event_replay_after_lan_restart(
    tmp_path, monkeypatch, record_property
):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    assert not ipaddress.ip_address(address).is_loopback
    record_property("lan_address", address)
    base_url = os.environ["NEBULA_TEST_POSTGRES_CLI_URL"]
    administration = create_engine(base_url)
    schema = "migration_cli_" + uuid4().hex
    with administration.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    target = (
        make_url(base_url)
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    try:
        source = tmp_path / "nebula.db"
        store = NebulaStore(source)
        (
            await store.create(
                Engagement(id="migration-project", name="Migration Project")
            )
        )
        profile = await store.create(
            ProviderProfile(
                id="migration-provider",
                name="Synthetic",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["security-model"],
            )
        )
        session = await store.create(
            ChatSession(
                id="migration-chat",
                engagement_id="migration-project",
                title="Before migration",
                model="security-model",
                provider_profile_id="migration-provider",
            )
        )
        (
            await store.create(
                ChatMessage(
                    id="migration-message",
                    engagement_id="migration-project",
                    session_id=session.id,
                    sequence=1,
                    role="assistant",
                    content="Saved before migration: café 東京",
                )
            )
        )
        (
            await store.create(
                AgentRun(
                    id="migration-run",
                    engagement_id="migration-project",
                    objective="Synthetic saved event stream",
                    status=RunStatus.COMPLETE,
                )
            )
        )
        first_event = await store.append_event(
            "migration-run", "fixture.saved", {"saved": True}, idempotency_key="saved"
        )
        scheduled_for = utc_now() + timedelta(days=1)

        async def queue_existing_native_mission():
            service = MissionService(store, provider_factory=RecordingProvider)
            assert service.checkpoint_path == tmp_path / "mission-checkpoints.db"
            scheduled = await service.start_mission(
                engagement_id="migration-project",
                name="Pre-migration native mission",
                objective="Synthetic scheduled work",
                provider_id=profile.id,
                model="security-model",
                budget=RunBudget(max_tool_calls=0, max_delegation_depth=0),
                scheduled_for=scheduled_for,
            )
            await service.shutdown()
            return scheduled.id

        original_native_id = await queue_existing_native_mission()
        (await store.database.engine.dispose())
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        monkeypatch.setenv("NEBULA_V3_DATABASE_URL", target)
        copied = await asyncio.to_thread(
            CliRunner().invoke,
            app,
            [
                "migrate-to-postgresql",
                "--data-dir",
                str(tmp_path),
                "--confirm-core-stopped",
                "--batch-size",
                "2",
            ],
        )
        assert copied.exit_code == 0, copied.output
        assert json.loads(copied.stdout)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        monkeypatch.setattr("nebula.v3.api.provider_from_profile", RecordingProvider)

        async with _migrated_lan_core(target, tmp_path, address) as client:
            record_property("first_core_origin", str(client.base_url))
            assert client.get("health").json()["dialect"] == "postgresql"
            assert (
                client.get("health", headers={"Authorization": ""}).status_code == 401
            )
            messages = client.get(f"chat/sessions/{session.id}/messages")
            assert messages.is_success, messages.text
            assert [(m["id"], m["content"]) for m in messages.json()] == [
                ("migration-message", "Saved before migration: café 東京")
            ]
            updated = client.patch(
                f"chat-sessions/{session.id}",
                json={
                    "title": "After migration",
                    "expected_revision": session.revision,
                },
            )
            assert updated.is_success, updated.text
            assert (
                client.patch(
                    f"chat-sessions/{session.id}",
                    json={
                        "title": "Stale update",
                        "expected_revision": session.revision,
                    },
                ).status_code
                == 409
            )
            body = {
                "event_type": "fixture.after_copy",
                "payload": {"copied": True},
                "idempotency_key": "after-copy",
            }
            event = client.post("runs/migration-run/events", json=body)
            assert event.status_code == 201, event.text
            assert event.json()["sequence"] == first_event.sequence + 1
            assert (
                client.post("runs/migration-run/events", json=body).json()["id"]
                == event.json()["id"]
            )
            scheduled = client.post(
                "missions",
                json={
                    "engagement_id": "migration-project",
                    "name": "PostgreSQL native mission",
                    "objective": "Synthetic scheduled work after cutover",
                    "provider_id": profile.id,
                    "model": "security-model",
                    "max_tool_calls": 0,
                    "scheduled_for": scheduled_for.isoformat(),
                },
            )
            assert scheduled.status_code == 202, scheduled.text
            new_native_id = scheduled.json()["id"]

        async with _migrated_lan_core(target, tmp_path, address) as client:
            record_property("restarted_core_origin", str(client.base_url))
            selected = client.get(f"chat-sessions/{session.id}")
            assert selected.is_success, selected.text
            assert selected.json()["title"] == "After migration"
            replay = client.get("runs/migration-run/events", params={"after": 1})
            assert replay.is_success, replay.text
            assert replay.json()["next_sequence"] == 2
            assert replay.json()["events"][0]["id"] == event.json()["id"]
            assert (
                client.get(f"chat/sessions/{session.id}/messages").json()[0]["id"]
                == "migration-message"
            )
            for mission_id in (original_native_id, new_native_id):
                restored = client.get(f"runs/{mission_id}")
                assert restored.is_success, restored.text
                assert restored.json()["status"] == "queued"
                assert (
                    restored.json()["metadata"]["scheduled_for"]
                    == scheduled_for.isoformat()
                )
                stopped = client.post(
                    f"runs/{mission_id}/stop",
                    json={"reason": "Synthetic test complete"},
                )
                assert stopped.is_success, stopped.text
                assert stopped.json()["status"] == "cancelled"
    finally:
        with administration.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        administration.dispose()


pytestmark = pytest.mark.anyio
