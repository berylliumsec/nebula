"""Event history goes with the record that owns it; live history stays put.

Run and operation events used to be kept forever, even after the conversation,
Mission or project that wrote them was deleted: on the live database 856 MB
of harness-turn events and 189 MB of run events belonged to records that no
longer existed. The ledgers stay append-only while their owner exists, and
the history of a deleted owner goes with it, or with the next prune.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

import nebula.v3.event_history as event_history_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.cli import app as cli_app
from nebula.v3.database import Database, DatabaseInUseError, vacuum_sqlite
from nebula.v3.domain import (
    AgentRun,
    Asset,
    BrowserAutomationLease,
    BrowserCrawlJob,
    ChatBackend,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    ENTITY_MODEL_BY_KIND,
    Engagement,
    EngagementStatus,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    RiskClass,
    RunStatus,
    utc_now,
)
from nebula.v3.event_history import (
    OPERATION_EVENT_OWNER_KINDS,
    prune_orphaned_event_history,
)
from nebula.v3.exporter import export_engagement
from nebula.v3.storage import NebulaStore, NotFoundError
from nebula.v3.terminal_history import CapturedTerminalCommand, TerminalCommandHistory

MIGRATION = (
    Path(__file__).parents[2]
    / "src"
    / "nebula"
    / "v3"
    / "migrations"
    / "versions"
    / "0018_event_owner_retention.py"
)
AN_HOUR_AGO = timedelta(hours=1)


@pytest.fixture
def store(tmp_path):
    return NebulaStore(Database(tmp_path / "nebula.db"))


def _harness_turn(
    store: NebulaStore,
    project: Engagement,
    *,
    session: HarnessSession | None = None,
    chat: ChatSession | None = None,
    chat_turn: ChatTurn | None = None,
    run: AgentRun | None = None,
) -> HarnessTurn:
    session = session or store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id="harness-1",
            model="test-model",
        )
    )
    return store.create(
        HarnessTurn(
            engagement_id=project.id,
            harness_session_id=session.id,
            origin=HarnessTurnOrigin.MISSION if run else HarnessTurnOrigin.CHAT,
            run_id=run.id if run else None,
            chat_session_id=chat.id if chat else None,
            chat_turn_id=chat_turn.id if chat_turn else None,
            status=HarnessTurnStatus.COMPLETE,
            prompt="fixture",
        )
    )


def _conversation(
    store: NebulaStore, project: Engagement, title: str
) -> tuple[ChatSession, ChatTurn, HarnessTurn]:
    session = store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id="harness-1",
            model="test-model",
        )
    )
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title=title,
            backend=ChatBackend.HARNESS,
            harness_profile_id="harness-1",
            harness_session_id=session.id,
            model="test-model",
        )
    )
    turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            backend=ChatBackend.HARNESS,
            model="test-model",
            status=ChatTurnStatus.COMPLETE,
        )
    )
    harness_turn = _harness_turn(
        store, project, session=session, chat=chat, chat_turn=turn
    )
    for index in range(3):
        store.append_operation_event(
            harness_turn.id,
            "harness_turn",
            project.id,
            "harness.message_delta",
            {"index": index},
        )
        # Chat-origin tool calls key their run events by the chat turn, and a
        # harness tool call without one by the harness turn.
        store.append_event(turn.id, "tool.proposed", {"index": index})
        store.append_event(harness_turn.id, "tool.proposed", {"index": index})
    return chat, turn, harness_turn


def _operation_events(store: NebulaStore, operation_id: str) -> int:
    return len(store.replay_operation_events(operation_id))


def _run_events(store: NebulaStore, run_id: str) -> int:
    return len(store.replay_events(run_id))


def _raw(store: NebulaStore, statement: str, **values) -> int:
    with store.database.engine.begin() as connection:
        return connection.execute(text(statement), values).rowcount


def test_trigger_owner_kinds_are_the_retention_map_and_name_real_records():
    spec = importlib.util.spec_from_file_location("event_owner_retention", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert set(migration._OWNED_OPERATION_KINDS) == set(OPERATION_EVENT_OWNER_KINDS)
    assert set(OPERATION_EVENT_OWNER_KINDS.values()) <= set(ENTITY_MODEL_BY_KIND)
    # These keep their events while the project exists: the id is not a
    # record, or surviving records still read the ledger.
    for kept in (
        "container_terminal",
        "terminal_recording_policy",
        "browser_assessment",
    ):
        assert kept not in OPERATION_EVENT_OWNER_KINDS


def test_database_refuses_deleting_history_while_its_owner_exists(store):
    project = store.create(Engagement(name="Live history"))
    chat, turn, harness_turn = _conversation(store, project, "Live")
    live = store.append_operation_event(
        harness_turn.id, "harness_turn", project.id, "harness.completed"
    )
    terminal = store.append_operation_event(
        "terminal-session-1",
        "container_terminal",
        project.id,
        "container_terminal.command",
    )
    orphan = store.append_operation_event(
        "deleted-turn", "harness_turn", project.id, "harness.completed"
    )
    live_run_event = store.append_event(turn.id, "tool.complete")
    orphan_run_event = store.append_event("deleted-chat-turn", "tool.complete")

    for event_id in (live.id, terminal.id):
        with pytest.raises(DBAPIError, match="immutable while their record exists"):
            _raw(store, "DELETE FROM operation_events WHERE id = :id", id=event_id)
    with pytest.raises(DBAPIError, match="append-only while their record exists"):
        _raw(store, "DELETE FROM run_events WHERE id = :id", id=live_run_event.id)
    # History of a deleted record is still never rewritten.
    with pytest.raises(DBAPIError, match="immutable"):
        _raw(
            store,
            "UPDATE operation_events SET event_type = 'rewritten' WHERE id = :id",
            id=orphan.id,
        )
    with pytest.raises(DBAPIError, match="append-only"):
        _raw(
            store,
            "UPDATE run_events SET event_type = 'rewritten' WHERE id = :id",
            id=orphan_run_event.id,
        )

    assert _raw(store, "DELETE FROM operation_events WHERE id = :id", id=orphan.id) == 1
    assert (
        _raw(store, "DELETE FROM run_events WHERE id = :id", id=orphan_run_event.id)
        == 1
    )
    assert store.replay_operation_events(harness_turn.id)[-1] == live
    assert store.replay_operation_events("terminal-session-1") == [terminal]

    # Once the project is gone every event it owned may go, whatever its kind.
    _raw(store, "DELETE FROM entities WHERE id = :id", id=project.id)
    assert (
        _raw(store, "DELETE FROM operation_events WHERE id = :id", id=terminal.id) == 1
    )


def _backlog(store: NebulaStore) -> dict[str, object]:
    """History left behind by deletes from before this policy."""

    old = utc_now() - AN_HOUR_AGO
    project = store.create(Engagement(name="Kept project"))
    _chat, live_turn, live_harness_turn = _conversation(store, project, "Kept")
    for index in range(5):
        store.append_operation_event(
            "deleted-harness-turn",
            "harness_turn",
            project.id,
            "harness.message_delta",
            {"index": index},
            occurred_at=old,
        )
        store.append_event(
            "deleted-chat-turn", "tool.proposed", {"index": index}, occurred_at=old
        )
    kept = [
        store.append_operation_event(
            "terminal-session-1",
            "container_terminal",
            project.id,
            "container_terminal.command",
            occurred_at=old,
        ),
        store.append_operation_event(
            f"terminal-recording-policy:{project.id}",
            "terminal_recording_policy",
            project.id,
            "terminal.recording_policy.updated",
            occurred_at=old,
        ),
        store.append_operation_event(
            "deleted-assessment",
            "browser_assessment",
            project.id,
            "browser_assessment.validation.granted",
            occurred_at=old,
        ),
    ]
    deleted_project = store.create(Engagement(name="Deleted project"))
    for operation_id, kind in (
        ("gone-project-turn", "harness_turn"),
        ("gone-project-terminal", "container_terminal"),
        ("gone-project-terminal", "container_terminal"),
    ):
        store.append_operation_event(
            operation_id, kind, deleted_project.id, "fixture", occurred_at=old
        )
    store.delete(Engagement, deleted_project.id)
    return {
        "project": project,
        "live_turn": live_turn,
        "live_harness_turn": live_harness_turn,
        "kept": kept,
    }


def test_prune_removes_only_history_of_deleted_records_and_is_idempotent(store):
    backlog = _backlog(store)
    live_turn = backlog["live_turn"]
    live_harness_turn = backlog["live_harness_turn"]
    live_operations = store.replay_operation_events(live_harness_turn.id)
    live_runs = store.replay_events(live_turn.id)
    pauses: list[None] = []

    report = prune_orphaned_event_history(
        store.database,
        batch_size=2,
        min_age=timedelta(0),
        pause=lambda: pauses.append(None),
    )

    assert report.as_dict() == {
        "operation_events": 5 + 3,
        "run_events": 5,
        "projects": 1,
        "owners": 2,
        # 5 rows in batches of 2, twice, and the deleted project's 3.
        "batches": 3 + 3 + 2,
        "skipped_owners": 0,
    }
    assert len(pauses) == report.batches
    assert _operation_events(store, "deleted-harness-turn") == 0
    assert _run_events(store, "deleted-chat-turn") == 0
    assert _operation_events(store, "gone-project-turn") == 0
    assert _operation_events(store, "gone-project-terminal") == 0
    assert store.replay_operation_events(live_harness_turn.id) == live_operations
    assert store.replay_events(live_turn.id) == live_runs
    for event in backlog["kept"]:
        assert store.replay_operation_events(event.operation_id) == [event]

    again = prune_orphaned_event_history(store.database, min_age=timedelta(0))
    assert again.operation_events == again.run_events == again.batches == 0
    assert store.replay_operation_events(live_harness_turn.id) == live_operations


def test_prune_leaves_fresh_history_and_stops_between_batches(store):
    project = store.create(Engagement(name="Fresh history"))
    store.append_operation_event(
        "just-deleted-turn", "harness_turn", project.id, "harness.completed"
    )
    for index in range(4):
        store.append_event(
            "old-chat-turn",
            "tool.proposed",
            {"index": index},
            occurred_at=utc_now() - AN_HOUR_AGO,
        )
    stop = threading.Event()

    # A ledger written in the last ten minutes may belong to a record another
    # writer has yet to commit, so the default age leaves it.
    first = prune_orphaned_event_history(
        store.database, batch_size=1, pause=stop.set, stop=stop
    )
    assert (first.run_events, first.batches) == (1, 1)
    assert _run_events(store, "old-chat-turn") == 3

    rest = prune_orphaned_event_history(store.database, batch_size=1)
    assert rest.run_events == 3
    assert _run_events(store, "old-chat-turn") == 0
    assert _operation_events(store, "just-deleted-turn") == 1


def test_deleting_a_conversation_removes_its_turns_history(store):
    project = store.create(Engagement(name="Conversation delete"))
    deleted, deleted_turn, deleted_harness_turn = _conversation(store, project, "Gone")
    _kept, kept_turn, kept_harness_turn = _conversation(store, project, "Kept")

    store.delete_chat_session(deleted.id)
    assert store.event_history.wait_idle(10)

    assert _operation_events(store, deleted_harness_turn.id) == 0
    assert _run_events(store, deleted_turn.id) == 0
    assert _run_events(store, deleted_harness_turn.id) == 0
    assert _operation_events(store, kept_harness_turn.id) == 3
    assert _run_events(store, kept_turn.id) == 3
    assert _run_events(store, kept_harness_turn.id) == 3


def test_a_delete_returns_before_its_history_is_removed(store, monkeypatch):
    project = store.create(Engagement(name="Deferred history"))
    deleted, _turn, harness_turn = _conversation(store, project, "Gone")
    started = threading.Event()
    release = threading.Event()
    purge = event_history_module.purge_event_history

    def gated(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return purge(*args, **kwargs)

    monkeypatch.setattr(event_history_module, "purge_event_history", gated)

    # A long history must not hold the write lock or the caller for its length.
    store.delete_chat_session(deleted.id)
    assert started.wait(10)
    with pytest.raises(NotFoundError):
        store.get(ChatSession, deleted.id)
    assert _operation_events(store, harness_turn.id) == 3

    release.set()
    assert store.event_history.wait_idle(10)
    assert _operation_events(store, harness_turn.id) == 0


def test_deleting_a_mission_removes_its_history(store):
    project = store.create(Engagement(name="Mission delete"))
    runs = [
        store.create(
            AgentRun(
                engagement_id=project.id, objective=name, status=RunStatus.COMPLETE
            )
        )
        for name in ("Deleted", "Kept")
    ]
    leases = []
    for run in runs:
        store.append_event(run.id, "run.completed", {})
        turn = _harness_turn(store, project, run=run)
        store.append_event(run.id, "harness.completed", {"harness_turn_id": turn.id})
        lease = store.create(
            BrowserAutomationLease(
                engagement_id=project.id,
                run_id=run.id,
                session_id="browser-session-1",
                identity_id="identity-1",
                scope_policy_id="scope-1",
                scope_policy_revision=1,
                target_urls=["https://target.example.test/"],
                allowed_risk_classes=[RiskClass.LOCAL_READ],
                expires_at=utc_now() + timedelta(hours=1),
            )
        )
        store.append_operation_event(
            lease.id, "browser_automation_lease", project.id, "lease.created"
        )
        leases.append(lease)

    store.delete_run(runs[0].id)
    assert store.event_history.wait_idle(10)

    assert _run_events(store, runs[0].id) == 0
    assert _operation_events(store, leases[0].id) == 0
    assert _run_events(store, runs[1].id) == 2
    assert _operation_events(store, leases[1].id) == 1


def test_deleting_an_archived_project_removes_all_its_history(store):
    project = store.create(Engagement(name="Archived"))
    other = store.create(Engagement(name="Other"))
    for owner in (project, other):
        _conversation(store, owner, "Chat")
        run = store.create(
            AgentRun(engagement_id=owner.id, objective="Run", status=RunStatus.COMPLETE)
        )
        store.append_event(run.id, "run.completed", {})
        store.append_operation_event(
            f"terminal-{owner.id}", "container_terminal", owner.id, "fixture"
        )
        store.append_operation_event(
            f"terminal-recording-policy:{owner.id}",
            "terminal_recording_policy",
            owner.id,
            "terminal.recording_policy.updated",
        )
    project = store.update(
        Engagement,
        project.id,
        {"status": EngagementStatus.ARCHIVED},
        expected_revision=project.revision,
    )

    def history(project_id: str) -> tuple[int, int]:
        with store.database.engine.connect() as connection:
            operations = connection.execute(
                text("SELECT count(*) FROM operation_events WHERE engagement_id = :p"),
                {"p": project_id},
            ).scalar_one()
            runs = connection.execute(
                text(
                    "SELECT count(*) FROM run_events WHERE run_id IN "
                    "(SELECT id FROM entities WHERE engagement_id = :p)"
                ),
                {"p": project_id},
            ).scalar_one()
        return operations, runs

    assert history(project.id) == (5, 7)
    assert history(other.id) == (5, 7)
    run_ids = [
        row.id for row in store.list_entities(AgentRun, engagement_id=project.id)
    ]

    store.delete_archived_engagement(project.id, expected_revision=project.revision)
    assert store.event_history.wait_idle(10)

    with store.database.engine.connect() as connection:
        leftover = connection.execute(
            text("SELECT count(*) FROM operation_events WHERE engagement_id = :p"),
            {"p": project.id},
        ).scalar_one()
    assert leftover == 0
    assert [_run_events(store, run_id) for run_id in run_ids] == [0]
    assert history(other.id) == (5, 7)


def test_deleting_one_record_removes_its_operation_events(store):
    project = store.create(Engagement(name="Single record delete"))
    crawls = [
        store.create(
            BrowserCrawlJob(
                engagement_id=project.id,
                session_id="browser-session-1",
                identity_id="identity-1",
                start_url="https://target.example.test/",
            )
        )
        for _ in range(2)
    ]
    for crawl in crawls:
        store.append_operation_event(
            crawl.id, "browser_crawl_jobs", project.id, "browser_crawl.created"
        )
        store.append_operation_event(
            crawl.id, "browser_crawl", project.id, "browser_crawl.complete"
        )
    asset = store.create(Asset(engagement_id=project.id, name="Not an owner"))

    store.delete(BrowserCrawlJob, crawls[0].id)
    with store.transaction() as transaction:
        transaction.delete(BrowserCrawlJob, crawls[1].id)
    store.delete(Asset, asset.id)
    assert store.event_history.wait_idle(10)

    assert [_operation_events(store, crawl.id) for crawl in crawls] == [0, 0]


def test_terminal_audit_and_export_still_read_the_history_that_is_kept(
    tmp_path, monkeypatch
):
    async def unavailable(_self):
        return False, "not configured"

    monkeypatch.setattr("nebula.v3.cli.ContainerSandboxRunner.available", unavailable)
    data_dir = tmp_path / "data"
    store = NebulaStore(data_dir / "nebula.db")
    artifacts = ArtifactStore(data_dir / "artifacts")
    project = store.create(Engagement(name="Audited"))
    store.create(
        HarnessProfile(
            id="harness-1",
            name="Harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
        )
    )
    now = utc_now()
    output = b"kept terminal output"
    TerminalCommandHistory(
        store.database, store=store, artifact_store=artifacts
    ).record_capture(
        engagement_id=project.id,
        session_id="audited-terminal",
        operator_id="operator-1",
        capture=CapturedTerminalCommand(
            shell_sequence="1",
            command="printf kept",
            cwd="/workspace",
            status="completed",
            exit_code=0,
            started_at=now,
            completed_at=now - AN_HOUR_AGO,
            output=output,
            observed_output_bytes=len(output),
            output_sha256=hashlib.sha256(output).hexdigest(),
            output_truncated=False,
        ),
    )
    _chat, live_turn, live_harness_turn = _conversation(store, project, "Exported")
    store.append_operation_event(
        "deleted-turn",
        "harness_turn",
        project.id,
        "harness.completed",
        occurred_at=now - AN_HOUR_AGO,
    )

    pruned = CliRunner().invoke(
        cli_app, ["maintenance", "prune-events", "--data-dir", str(data_dir)]
    )
    assert pruned.exit_code == 0, pruned.stdout
    assert json.loads(pruned.stdout)["operation_events"] == 1

    doctor = CliRunner().invoke(
        cli_app, ["doctor", "--json", "--data-dir", str(data_dir)]
    )
    assert doctor.exit_code == 0, doctor.stdout
    assert json.loads(doctor.stdout)["terminal_audit"]["errors"] == 0

    manifest = export_engagement(
        engagement_id=project.id,
        destination=tmp_path / "bundle.zip",
        store=store,
        artifact_store=artifacts,
    )
    # The terminal session and the live harness turn: nothing of the deleted one.
    assert manifest.operation_event_count == 1 + 3
    assert {
        event.operation_id for event in store.list_operation_events(project.id)
    } == {"audited-terminal", live_harness_turn.id}
    assert _run_events(store, live_turn.id) == 3


def test_core_startup_prunes_history_of_deleted_records(store):
    project = store.create(Engagement(name="Startup prune"))
    _chat, live_turn, live_harness_turn = _conversation(store, project, "Live")
    for index in range(3):
        store.append_operation_event(
            "deleted-turn",
            "harness_turn",
            project.id,
            "harness.message_delta",
            {"index": index},
            occurred_at=utc_now() - AN_HOUR_AGO,
        )

    with TestClient(create_app(store, auth_token="test-token")):
        deadline = time.monotonic() + 10
        while _operation_events(store, "deleted-turn") and time.monotonic() < deadline:
            time.sleep(0.05)

    assert _operation_events(store, "deleted-turn") == 0
    assert _operation_events(store, live_harness_turn.id) == 3
    assert _run_events(store, live_turn.id) == 3


def test_vacuum_returns_freed_pages_and_refuses_while_the_database_is_open(tmp_path):
    data_dir = tmp_path / "data"
    store = NebulaStore(data_dir / "nebula.db")
    project = store.create(Engagement(name="Vacuum"))
    for index in range(200):
        store.append_operation_event(
            "deleted-turn",
            "harness_turn",
            project.id,
            "harness.message_delta",
            {"text": f"{index:04d}" * 2_000},
            occurred_at=utc_now() - AN_HOUR_AGO,
        )
    assert prune_orphaned_event_history(store.database).operation_events == 200
    database_path = data_dir / "nebula.db"

    holder = sqlite3.connect(database_path)
    holder.execute("SELECT count(*) FROM entities").fetchone()
    try:
        with pytest.raises(DatabaseInUseError, match="stop Nebula Core"):
            vacuum_sqlite(database_path)
        refused = CliRunner().invoke(
            cli_app, ["maintenance", "vacuum", "--data-dir", str(data_dir)]
        )
        assert refused.exit_code == 1
        assert "stop Nebula Core" in refused.stderr
    finally:
        holder.close()
    store.database.dispose()

    vacuumed = CliRunner().invoke(
        cli_app, ["maintenance", "vacuum", "--data-dir", str(data_dir)]
    )
    assert vacuumed.exit_code == 0, vacuumed.stdout
    result = json.loads(vacuumed.stdout)
    assert result["free_pages_before"] > 0
    assert result["bytes_after"] < result["bytes_before"]
    assert database_path.stat().st_mode & 0o777 == 0o600
    reopened = NebulaStore(database_path)
    assert reopened.get(Engagement, project.id).name == "Vacuum"
