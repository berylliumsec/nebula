from __future__ import annotations

import base64
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.database import Database
from nebula.v3.domain import Engagement
from nebula.v3.exporter import export_engagement
from nebula.v3.storage import NebulaStore, NotFoundError
from nebula.v3.terminal_history import (
    Osc633CommandParser,
    TerminalCommandHistory,
)


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
def command_history(tmp_path):
    database = Database(tmp_path / "history.db")
    store = NebulaStore(database)
    project = store.create(Engagement(name="Command history"))
    clock = MutableClock(datetime(2026, 1, 3, tzinfo=timezone.utc))
    try:
        yield TerminalCommandHistory(database, clock=clock), project
    finally:
        database.dispose()


def test_history_is_default_on_exact_searchable_and_paginated(command_history):
    history, project = command_history
    first = history.record(
        engagement_id=project.id,
        session_id="terminal-1",
        command="  printf 'one_%'  \n",
        cwd="/workspace/first dir",
        exit_code=7,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    second = history.record(
        engagement_id=project.id,
        session_id="terminal-1",
        command="whoami",
        cwd="/workspace",
        exit_code=0,
        occurred_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert first is not None
    assert second is not None
    assert first.command == "  printf 'one_%'  \n"
    assert first.cwd == "/workspace/first dir"
    status = history.status(project.id)
    assert status.enabled is True
    assert status.record_count == 2
    assert status.retention_days == 90
    assert status.max_records == 10_000

    page = history.list(project.id, limit=1)
    assert [record.command for record in page.records] == ["whoami"]
    assert page.total == 2
    assert page.next_offset == 1
    next_page = history.list(project.id, offset=page.next_offset, limit=1)
    assert [record.command for record in next_page.records] == [
        "  printf 'one_%'  \n"
    ]
    assert next_page.next_offset is None
    assert history.list(project.id, search="ONE_%").records == [first]


def test_disable_stops_new_records_without_implicitly_clearing(command_history):
    history, project = command_history
    assert history.record(
        engagement_id=project.id,
        session_id="terminal-1",
        command="pwd",
        cwd="/workspace",
        exit_code=0,
    )

    disabled = history.set_enabled(project.id, enabled=False)
    assert disabled.enabled is False
    assert history.record(
        engagement_id=project.id,
        session_id="terminal-1",
        command="should-not-persist",
        cwd="/workspace",
        exit_code=0,
    ) is None
    assert [record.command for record in history.list(project.id).records] == ["pwd"]
    assert history.clear(project.id) == 1
    assert history.status(project.id).record_count == 0
    assert history.set_enabled(project.id, enabled=True).enabled is True


def test_history_enforces_age_and_per_project_count_retention(tmp_path):
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    database = Database(tmp_path / "retention.db")
    store = NebulaStore(database)
    project = store.create(Engagement(name="Bounded history"))
    history = TerminalCommandHistory(
        database,
        max_records=3,
        retention_days=90,
        clock=clock,
    )
    try:
        for index in range(4):
            clock.current = datetime(2026, 1, index + 1, tzinfo=timezone.utc)
            history.record(
                engagement_id=project.id,
                session_id="terminal-retention",
                command=f"command-{index}",
                cwd="/workspace",
                exit_code=index,
            )

        assert [record.command for record in history.list(project.id).records] == [
            "command-3",
            "command-2",
            "command-1",
        ]
        clock.current += timedelta(days=91)
        assert history.status(project.id).record_count == 0
        expired = history.record(
            engagement_id=project.id,
            session_id="terminal-retention",
            command="already-expired",
            cwd="/workspace",
            exit_code=0,
            occurred_at=clock.current - timedelta(days=91),
        )
        assert expired is None
    finally:
        database.dispose()


def test_history_requires_a_real_project_and_cascades_on_delete(tmp_path):
    database = Database(tmp_path / "projects.db")
    store = NebulaStore(database)
    project = store.create(Engagement(name="Deleted project"))
    history = TerminalCommandHistory(database)
    history.record(
        engagement_id=project.id,
        session_id="terminal-1",
        command="hostname",
        cwd="/workspace",
        exit_code=0,
    )
    store.delete(Engagement, project.id, expected_revision=project.revision)

    with database.engine.connect() as connection:
        count = connection.exec_driver_sql(
            "SELECT count(*) FROM terminal_command_records"
        ).scalar_one()
    assert count == 0
    with pytest.raises(NotFoundError):
        history.status(project.id)
    database.dispose()


def test_command_history_is_not_in_engagement_exports(tmp_path):
    database = Database(tmp_path / "export.db")
    store = NebulaStore(database)
    project = store.create(Engagement(name="Local-only history"))
    secret_command = "printf terminal-history-must-remain-local"
    TerminalCommandHistory(database).record(
        engagement_id=project.id,
        session_id="terminal-export",
        command=secret_command,
        cwd="/workspace",
        exit_code=0,
    )
    destination = tmp_path / "project.nebula.zip"
    export_engagement(
        engagement_id=project.id,
        destination=destination,
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
    )

    with zipfile.ZipFile(destination) as archive:
        assert all("terminal" not in name for name in archive.namelist())
        assert all(
            secret_command.encode() not in archive.read(name)
            for name in archive.namelist()
        )
    database.dispose()


def _osc_frame(
    command: str,
    *,
    cwd: str = "/workspace",
    exit_code: int = 0,
    terminator: bytes = b"\x07",
) -> bytes:
    encoded_cwd = base64.b64encode(cwd.encode())
    encoded_command = base64.b64encode(command.encode())
    return (
        b"\x1b]633;NebulaCommand;"
        + str(exit_code).encode()
        + b";"
        + encoded_cwd
        + b";"
        + encoded_command
        + terminator
    )


@pytest.mark.parametrize("terminator", [b"\x07", b"\x1b\\"])
def test_osc_parser_handles_every_two_chunk_boundary(terminator):
    frame = _osc_frame("printf 'snowman ☃'\nnext", exit_code=13, terminator=terminator)
    raw = b"output-before\r\n" + frame + b"prompt-after"

    for boundary in range(len(raw) + 1):
        parser = Osc633CommandParser()
        first = parser.feed(raw[:boundary])
        second = parser.feed(raw[boundary:])
        flushed = parser.flush()
        assert first.passthrough + second.passthrough + flushed.passthrough == (
            b"output-before\r\nprompt-after"
        )
        records = first.records + second.records + flushed.records
        assert len(records) == 1
        assert records[0].command == "printf 'snowman ☃'\nnext"
        assert records[0].cwd == "/workspace"
        assert records[0].exit_code == 13
        assert parser.pending_bytes == 0


def test_osc_parser_preserves_non_nebula_malformed_and_incomplete_bytes():
    ordinary = b"normal\x1b]633;Prompt;A\x07 bytes"
    malformed = b"\x1b]633;NebulaCommand;wat;@@@;@@@\x07"
    parser = Osc633CommandParser()
    result = parser.feed(ordinary + malformed + b"tail\x1b]633;Nebula")
    flushed = parser.flush()

    assert result.records == ()
    assert result.passthrough + flushed.passthrough == (
        ordinary + malformed + b"tail\x1b]633;Nebula"
    )


def test_osc_parser_buffer_is_bounded_and_models_never_contain_output():
    parser = Osc633CommandParser(max_frame_bytes=64)
    oversized = b"\x1b]633;NebulaCommand;0;" + b"A" * 100
    result = parser.feed(oversized)

    assert result.passthrough == oversized
    assert result.records == ()
    assert parser.pending_bytes == 0
    assert "output" not in result.__dataclass_fields__


def test_schema_contains_command_metadata_but_no_output_column(tmp_path):
    database = Database(tmp_path / "columns.db")
    columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("terminal_command_records")
    }
    assert columns == {
        "id",
        "engagement_id",
        "session_id",
        "command",
        "cwd",
        "exit_code",
        "occurred_at",
    }
    database.dispose()


def test_authenticated_terminal_history_api_status_list_set_and_clear(tmp_path):
    store = NebulaStore(tmp_path / "api-history.db")
    project = store.create(Engagement(name="History API"))
    app = create_app(store, auth_token="history-token")
    history = app.state.terminal_command_history
    history.record(
        engagement_id=project.id,
        session_id="terminal-api",
        command="printf 'api command'",
        cwd="/workspace",
        exit_code=0,
    )
    base = f"/api/v1/engagements/{project.id}/terminal/commands"
    headers = {"Authorization": "Bearer history-token"}

    with TestClient(app) as client:
        assert client.get(f"{base}/status").status_code == 401
        assert client.get(base).status_code == 401
        assert client.put(f"{base}/status", json={"enabled": False}).status_code == 401
        assert client.delete(base).status_code == 401

        status = client.get(f"{base}/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["enabled"] is True
        assert status.json()["record_count"] == 1

        page = client.get(
            base,
            headers=headers,
            params={"search": "API COMMAND", "offset": 0, "limit": 10},
        )
        assert page.status_code == 200
        assert page.json()["total"] == 1
        assert page.json()["records"][0]["command"] == "printf 'api command'"

        disabled = client.put(
            f"{base}/status",
            headers=headers,
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert history.record(
            engagement_id=project.id,
            session_id="terminal-api",
            command="not-recorded",
            cwd="/workspace",
            exit_code=0,
        ) is None

        cleared = client.delete(base, headers=headers)
        assert cleared.status_code == 200
        assert cleared.json() == {"engagement_id": project.id, "cleared": 1}
        assert client.get(base, headers=headers).json()["records"] == []

        missing = client.get(
            "/api/v1/engagements/missing/terminal/commands/status",
            headers=headers,
        )
        assert missing.status_code == 404
