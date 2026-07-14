from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.database import Database
from nebula.v3.domain import Artifact, Engagement
from nebula.v3.exporter import export_engagement
from nebula.v3.storage import NebulaStore, NotFoundError
from nebula.v3.terminal_history import (
    CapturedTerminalCommand,
    Osc633CommandParser,
    TerminalAuditImmutableError,
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
        yield TerminalCommandHistory(
            database,
            store=store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            clock=clock,
        ), project
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
    assert status.capture_mode == "required"
    assert status.record_count == 2
    assert status.retention_days is None
    assert status.max_records is None

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


def test_audit_cannot_be_disabled_or_cleared(command_history):
    history, project = command_history
    assert history.record(
        engagement_id=project.id,
        session_id="terminal-1",
        command="pwd",
        cwd="/workspace",
        exit_code=0,
    )

    with pytest.raises(TerminalAuditImmutableError):
        history.set_enabled(project.id, enabled=False)
    with pytest.raises(TerminalAuditImmutableError):
        history.clear(project.id)
    assert [record.command for record in history.list(project.id).records] == ["pwd"]
    assert history.set_enabled(project.id, enabled=True).enabled is True


def test_audit_records_have_project_lifetime_without_age_or_count_pruning(tmp_path):
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
            "command-3", "command-2", "command-1", "command-0"
        ]
        clock.current += timedelta(days=91)
        assert history.status(project.id).record_count == 4
        retained = history.record(
            engagement_id=project.id,
            session_id="terminal-retention",
            command="already-expired",
            cwd="/workspace",
            exit_code=0,
            occurred_at=clock.current - timedelta(days=91),
        )
        assert retained.status == "legacy_metadata_only"
        assert history.status(project.id).record_count == 5
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


def test_terminal_audit_and_raw_result_are_in_sensitive_engagement_exports(tmp_path):
    database = Database(tmp_path / "export.db")
    store = NebulaStore(database)
    project = store.create(Engagement(name="Local-only history"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    secret_command = "printf terminal-audit-exported"
    output = b"password=exported-secret-value\n"
    now = datetime.now(timezone.utc)
    TerminalCommandHistory(database, store=store, artifact_store=artifacts).record_capture(
        engagement_id=project.id,
        session_id="terminal-export",
        operator_id="system",
        capture=CapturedTerminalCommand(
            shell_sequence="2",
            command=secret_command,
            cwd="/workspace",
            status="completed",
            exit_code=0,
            started_at=now,
            completed_at=now,
            output=output,
            observed_output_bytes=len(output),
            output_sha256=hashlib.sha256(output).hexdigest(),
            output_truncated=False,
        ),
    )
    destination = tmp_path / "project.nebula.zip"
    export_engagement(
        engagement_id=project.id,
        destination=destination,
        store=store,
        artifact_store=artifacts,
    )

    with zipfile.ZipFile(destination) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format_version"] == 3
        assert manifest["terminal_command_count"] == 1
        records = json.loads(archive.read("terminal_commands.json"))
        assert records[0]["command"] == secret_command
        assert output in [archive.read(name) for name in archive.namelist()]
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


def _audit_frame(kind: str, nonce: str, sequence: str, *parts: bytes) -> bytes:
    return (
        f"\x1b]633;NebulaCommand{kind};{nonce};{sequence};".encode()
        + b";".join(parts)
        + b"\x07"
    )


def test_nonce_bound_frames_capture_only_command_result_and_detect_truncation():
    nonce = "terminalauditnonce123"
    clock = MutableClock(datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc))
    parser = Osc633CommandParser(nonce=nonce, max_output_bytes=4, clock=clock)
    start = _audit_frame(
        "Start",
        nonce,
        "8",
        base64.b64encode(b"/workspace"),
        base64.b64encode(b"printf result"),
    )
    end = _audit_frame("End", nonce, "8", b"3")
    spoofed = _audit_frame(
        "Start", "differentnonce123", "9", base64.b64encode(b"/"), base64.b64encode(b"whoami")
    )

    first = parser.feed(b"prompt$ printf result\r\n" + start + b"abcdef")
    clock.current += timedelta(seconds=2)
    second = parser.feed(end + b"prompt$ " + spoofed)

    assert len(second.captures) == 1
    capture = second.captures[0]
    assert capture.command == "printf result"
    assert capture.cwd == "/workspace"
    assert capture.exit_code == 3
    assert capture.output == b"abcd"
    assert capture.observed_output_bytes == 6
    assert capture.output_truncated is True
    assert capture.output_sha256 == hashlib.sha256(b"abcdef").hexdigest()
    assert capture.completed_at - capture.started_at == timedelta(seconds=2)
    assert first.passthrough.startswith(b"prompt$ printf result")
    assert spoofed in second.passthrough


def test_audit_parser_preserves_multiline_binary_results_and_async_output_boundaries():
    nonce = "terminalmixedoutput12"
    parser = Osc633CommandParser(nonce=nonce)
    command = "printf 'one\\n' |\n  tr a-z A-Z"
    start = _audit_frame(
        "Start",
        nonce,
        "21",
        base64.b64encode(b"/workspace/audit"),
        base64.b64encode(command.encode()),
    )
    end = _audit_frame("End", nonce, "21", b"130")
    result_bytes = b"\x1b[31mONE\x1b[0m\r\n\x00\xff"

    first = parser.feed(start[:17])
    second = parser.feed(start[17:] + result_bytes[:5])
    third = parser.feed(result_bytes[5:] + end + b"background result\r\nprompt$ ")

    assert first.passthrough == b""
    assert second.passthrough == result_bytes[:5]
    assert len(third.captures) == 1
    capture = third.captures[0]
    assert capture.command == command
    assert capture.cwd == "/workspace/audit"
    assert capture.exit_code == 130
    assert capture.output == result_bytes
    assert capture.output_sha256 == hashlib.sha256(result_bytes).hexdigest()
    assert third.passthrough == result_bytes[5:] + b"background result\r\nprompt$ "

    empty_start = _audit_frame(
        "Start",
        nonce,
        "22",
        base64.b64encode(b"/workspace"),
        base64.b64encode(b"true"),
    )
    empty_end = _audit_frame("End", nonce, "22", b"0")
    empty = parser.feed(empty_start + empty_end)
    assert empty.captures[0].output == b""
    assert empty.captures[0].output_sha256 == hashlib.sha256(b"").hexdigest()


def test_audit_parser_marks_a_missing_completion_frame_as_framing_loss():
    nonce = "terminalframingloss1"
    parser = Osc633CommandParser(nonce=nonce)
    first_start = _audit_frame(
        "Start",
        nonce,
        "31",
        base64.b64encode(b"/workspace"),
        base64.b64encode(b"unset PROMPT_COMMAND"),
    )
    second_start = _audit_frame(
        "Start",
        nonce,
        "32",
        base64.b64encode(b"/workspace"),
        base64.b64encode(b"whoami"),
    )

    parsed = parser.feed(first_start + b"hook changed\n" + second_start)

    assert len(parsed.captures) == 1
    assert parsed.captures[0].status == "framing_lost"
    assert parsed.captures[0].output == b"hook changed\n"
    assert "prior completion marker" in (parsed.captures[0].capture_error or "")


def test_durable_spool_recovers_an_interrupted_command_after_core_restart(tmp_path):
    store = NebulaStore(tmp_path / "recovery.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = store.create(Engagement(name="Spool recovery"))
    history = TerminalCommandHistory(
        store.database, store=store, artifact_store=artifacts
    )
    nonce = "terminalrecoverynonce12"
    parser = history.new_parser(
        nonce=nonce,
        engagement_id=project.id,
        session_id="interrupted-session",
        operator_id="system",
    )
    start = _audit_frame(
        "Start",
        nonce,
        "12",
        base64.b64encode(b"/workspace"),
        base64.b64encode(b"long-running-scan"),
    )
    parser.feed(start + b"partial result\n")

    restarted = TerminalCommandHistory(
        store.database, store=store, artifact_store=artifacts
    )
    assert restarted.recover_spools() == 1
    records = restarted.list(project.id).records
    assert len(records) == 1
    assert records[0].status == "interrupted"
    assert records[0].capture_error == "Core restarted before the command completion marker"
    assert restarted.status(project.id).degraded_count == 1
    assert restarted.output_bytes(project.id, records[0].id, raw=True)[0] == b"partial result\n"
    assert list(restarted.spool_root.glob("*")) == []


def test_spool_recovery_retains_full_stream_hash_after_capture_truncation(tmp_path):
    store = NebulaStore(tmp_path / "truncated-recovery.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = store.create(Engagement(name="Truncated spool recovery"))
    history = TerminalCommandHistory(
        store.database, store=store, artifact_store=artifacts
    )
    assert history.spool_root is not None
    nonce = "terminaltruncate1234"
    parser = Osc633CommandParser(
        nonce=nonce,
        max_output_bytes=4,
        spool_root=history.spool_root,
        spool_context={
            "engagement_id": project.id,
            "session_id": "truncated-session",
            "operator_id": "operator-1",
        },
    )
    start = _audit_frame(
        "Start",
        nonce,
        "18",
        base64.b64encode(b"/workspace"),
        base64.b64encode(b"printf abcdef"),
    )
    parser.feed(start + b"abcdef")

    restarted = TerminalCommandHistory(
        store.database, store=store, artifact_store=artifacts
    )
    assert restarted.recover_spools() == 1
    record = restarted.list(project.id).records[0]
    assert record.output_truncated is True
    assert record.observed_output_bytes == 6
    assert record.captured_output_bytes == 4
    assert record.output_sha256 == hashlib.sha256(b"abcdef").hexdigest()
    assert restarted.output_bytes(project.id, record.id, raw=True)[0] == b"abcd"


def test_failed_spool_recovery_emits_one_visible_audit_gap(tmp_path):
    store = NebulaStore(tmp_path / "failed-recovery.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = store.create(Engagement(name="Failed spool recovery"))
    history = TerminalCommandHistory(
        store.database, store=store, artifact_store=artifacts
    )
    nonce = "terminalbadspool123"
    parser = history.new_parser(
        nonce=nonce,
        engagement_id=project.id,
        session_id="failed-spool-session",
        operator_id="operator-1",
    )
    parser.feed(
        _audit_frame(
            "Start",
            nonce,
            "19",
            base64.b64encode(b"/workspace"),
            base64.b64encode(b"printf pending"),
        )
        + b"pending output"
    )
    assert history.spool_root is not None
    metadata_path = next(history.spool_root.glob("*.json"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["output_sha256"] = "invalid"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    restarted = TerminalCommandHistory(
        store.database, store=store, artifact_store=artifacts
    )
    assert restarted.recover_spools() == 0
    assert restarted.recover_spools() == 0
    assert restarted.status(project.id).audit_gap_count == 1
    gaps = [
        event
        for event in store.replay_operation_events("failed-spool-session")
        if event.event_type == "container_terminal.audit_gap"
    ]
    assert len(gaps) == 1
    assert gaps[0].payload["error"] == "spool_recovery_ValueError"


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


def test_osc_parser_buffer_is_bounded_and_legacy_models_do_not_contain_output():
    parser = Osc633CommandParser(max_frame_bytes=64)
    oversized = b"\x1b]633;NebulaCommand;0;" + b"A" * 100
    result = parser.feed(oversized)

    assert result.passthrough == oversized
    assert result.records == ()
    assert parser.pending_bytes == 0
    assert result.captures == ()


def test_schema_contains_durable_audit_and_output_reference_columns(tmp_path):
    database = Database(tmp_path / "columns.db")
    columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("terminal_command_records")
    }
    assert {
        "id",
        "engagement_id",
        "session_id",
        "command",
        "cwd",
        "exit_code",
        "occurred_at",
        "operator_id",
        "status",
        "started_at",
        "completed_at",
        "raw_output_artifact_id",
        "redacted_output_artifact_id",
        "output_sha256",
        "output_truncated",
    } <= columns
    database.dispose()


def test_audit_health_counts_truncation_and_persistence_gaps(command_history):
    history, project = command_history
    observed = b"abcdef"
    now = datetime.now(timezone.utc)
    history.record_capture(
        engagement_id=project.id,
        session_id="terminal-health",
        operator_id="operator-1",
        capture=CapturedTerminalCommand(
            shell_sequence="9",
            command="printf abcdef",
            cwd="/workspace",
            status="completed",
            exit_code=0,
            started_at=now,
            completed_at=now,
            output=observed[:4],
            observed_output_bytes=len(observed),
            output_sha256=hashlib.sha256(observed).hexdigest(),
            output_truncated=True,
        ),
    )
    assert history.store is not None
    history.store.append_operation_event(
        "terminal-health",
        "container_terminal",
        project.id,
        "container_terminal.audit_gap",
        {"status": "capture_failed"},
        actor_id="operator-1",
    )

    status = history.status(project.id)
    assert status.truncated_count == 1
    assert status.audit_gap_count == 1


def test_audit_row_artifact_entities_and_command_event_commit_atomically(command_history):
    history, project = command_history
    assert history.store is not None
    record_id = "atomic-terminal-record"
    session_id = "atomic-terminal-session"
    idempotency_key = f"container-terminal:{session_id}:command:{record_id}"
    history.store.append_operation_event(
        session_id,
        "container_terminal",
        project.id,
        "container_terminal.command",
        {"record_id": "different-record"},
        actor_id="operator-1",
        idempotency_key=idempotency_key,
    )
    now = datetime.now(timezone.utc)
    output = b"atomic result"

    with pytest.raises(IntegrityError):
        history.record_capture(
            engagement_id=project.id,
            session_id=session_id,
            operator_id="operator-1",
            capture=CapturedTerminalCommand(
                record_id=record_id,
                shell_sequence="1",
                command="printf atomic",
                cwd="/workspace",
                status="completed",
                exit_code=0,
                started_at=now,
                completed_at=now,
                output=output,
                observed_output_bytes=len(output),
                output_sha256=hashlib.sha256(output).hexdigest(),
                output_truncated=False,
            ),
        )

    assert history.list(project.id).records == []
    assert history.store.list_entities(Artifact) == []


def test_authenticated_terminal_audit_api_is_read_only_and_protects_raw_output(tmp_path):
    store = NebulaStore(tmp_path / "api-history.db")
    project = store.create(Engagement(name="History API"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    app = create_app(store, artifact_store=artifacts, auth_token="history-token")
    history = app.state.terminal_command_history
    output = "password=super-secret-value\n☃\n".encode()
    now = datetime.now(timezone.utc)
    record = history.record_capture(
        engagement_id=project.id,
        session_id="terminal-api",
        operator_id="system",
        capture=CapturedTerminalCommand(
            shell_sequence="4", command="printf 'api command'", cwd="/workspace",
            status="completed", exit_code=0, started_at=now, completed_at=now,
            output=output, observed_output_bytes=len(output),
            output_sha256=hashlib.sha256(output).hexdigest(), output_truncated=False,
        ),
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
        assert status.json()["capture_mode"] == "required"
        assert status.json()["truncated_count"] == 0
        assert status.json()["audit_gap_count"] == 0

        page = client.get(
            base,
            headers=headers,
            params={"search": "API COMMAND", "offset": 0, "limit": 10},
        )
        assert page.status_code == 200
        assert page.json()["total"] == 1
        assert page.json()["records"][0]["command"] == "printf 'api command'"
        filtered = client.get(
            base,
            headers=headers,
            params={
                "operator_id": "system",
                "session_id": "terminal-api",
                "status": "completed",
                "exit_code": 0,
                "date_from": (now - timedelta(seconds=1)).isoformat(),
                "date_to": (now + timedelta(seconds=1)).isoformat(),
            },
        )
        assert filtered.status_code == 200
        assert [item["id"] for item in filtered.json()["records"]] == [record.id]

        disabled = client.put(
            f"{base}/status",
            headers=headers,
            json={"enabled": False},
        )
        assert disabled.status_code == 409
        assert disabled.json()["code"] == "immutable_audit_history"

        cleared = client.delete(base, headers=headers)
        assert cleared.status_code == 409
        assert cleared.json()["code"] == "immutable_audit_history"

        output_url = f"{base}/{record.id}/output"
        expected_redacted = "password=[REDACTED]\n☃\n".encode()
        redacted = client.get(output_url, headers=headers)
        assert redacted.content == expected_redacted
        assert b"super-secret-value" not in redacted.content
        snowman_offset = expected_redacted.index("☃".encode())
        first_page = client.get(
            output_url,
            headers=headers,
            params={"limit": snowman_offset + 1},
        )
        assert first_page.content == expected_redacted[:snowman_offset]
        assert first_page.headers["X-Nebula-Output-Next"] == str(snowman_offset)
        second_page = client.get(
            output_url,
            headers=headers,
            params={"offset": snowman_offset, "limit": 10},
        )
        assert second_page.content == expected_redacted[snowman_offset:]
        assert client.get(
            output_url,
            headers=headers,
            params={"offset": snowman_offset + 1},
        ).status_code == 416
        assert client.get(output_url, headers=headers, params={"raw": True}).status_code == 428
        raw = client.get(
            output_url,
            headers={**headers, "X-Nebula-Sensitive-Data-Acknowledged": "true"},
            params={"raw": True},
        )
        assert raw.status_code == 200
        assert raw.content == output
        assert raw.headers["Cache-Control"] == "private, no-store"
        assert raw.headers["X-Nebula-Sensitive-Data"] == "unredacted"
        assert "attachment" in raw.headers["Content-Disposition"]

        missing = client.get(
            "/api/v1/engagements/missing/terminal/commands/status",
            headers=headers,
        )
        assert missing.status_code == 404
