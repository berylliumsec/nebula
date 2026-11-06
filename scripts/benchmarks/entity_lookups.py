"""Generate synthetic legacy data, measure actual reads, or rehearse migration.

Only use an isolated directory created by ``seed``. See the validation receipt
for the before/after checkout commands. No production database is accepted.
"""

from __future__ import annotations

import asyncio

import argparse
import hashlib
import json
import sqlite3
import statistics
import threading
import time
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event, create_engine
from sqlalchemy.pool import NullPool

from nebula.v3 import database as database_module
from nebula.v3.chat import ChatService
from nebula.v3.database import Database
from nebula.v3.domain import Artifact, Engagement, RiskClass, ToolCall
from nebula.v3.storage import NebulaStore


def seed(root, artifacts, calls):
    root.mkdir(mode=0o700, parents=True)
    engine = create_engine(
        Database._normalize_sync_url(root / "nebula.db"), poolclass=NullPool
    )
    config = Config()
    config.set_main_option(
        "script_location", str(Path(database_module.__file__).with_name("migrations"))
    )
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0023_workspace_provenance_ledger")
    engine.dispose()
    stamp = "2026-11-02 12:00:00.000000"
    with closing(sqlite3.connect(root / "nebula.db")) as connection:
        (connection.execute("PRAGMA journal_mode=WAL"))

        def insert(rows):
            connection.executemany(
                "INSERT INTO entities (id,kind,engagement_id,revision,payload,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            connection.commit()

        project = Engagement(id="bench-project", name="Synthetic benchmark")
        needle = ToolCall(
            id="needle",
            engagement_id=project.id,
            run_id="synthetic-owner",
            tool_name="synthetic.noop",
            risk_class=RiskClass.LOCAL_READ,
        )
        (
            insert(
                [
                    (
                        item.id,
                        item.entity_kind,
                        project.id if item is needle else None,
                        1,
                        item.model_dump_json(),
                        stamp,
                        stamp,
                    )
                    for item in (project, needle)
                ]
            )
        )
        for kind, total in (("artifacts", artifacts), ("tool_calls", calls)):
            payload = (
                Artifact(
                    engagement_id=project.id,
                    sha256="0" * 64,
                    size=128,
                    storage_path="synthetic/blob",
                    metadata={"kind": "stdout"},
                )
                if kind == "artifacts"
                else ToolCall(
                    engagement_id=project.id,
                    run_id="synthetic-owner",
                    tool_name="synthetic.noop",
                    risk_class=RiskClass.LOCAL_READ,
                    origin="chat",
                    status="complete",
                )
            ).model_dump(mode="json")
            started = time.perf_counter()
            for start in range(0, total, 5000):
                rows = []
                for number in range(start, min(total, start + 5000)):
                    key = f"{kind}-{number:09d}"
                    payload["id"] = key
                    if kind == "artifacts":
                        payload["metadata"]["tool_call_id"] = (
                            "needle" if number >= total - 3 else f"call-{number // 3}"
                        )
                    else:
                        payload["status"] = (
                            "approved" if number >= total - 3 else "complete"
                        )
                    rows.append(
                        (key, kind, project.id, 1, json.dumps(payload), stamp, stamp)
                    )
                (insert(rows))
            print(
                json.dumps(
                    {
                        "seeded": kind,
                        "rows": total,
                        "seconds": time.perf_counter() - started,
                    }
                ),
                flush=True,
            )
        (connection.execute("PRAGMA wal_checkpoint(TRUNCATE)"))
    (root / "synthetic.json").write_text(
        json.dumps({"synthetic": True, "artifacts": artifacts, "tool_calls": calls + 1})
    )


async def measure(root, label):
    path = root / ("restored.db" if label == "after" else "nebula.db")
    store = NebulaStore(Database(path, bootstrap=False))
    queries = {}

    def capture(conn, cursor, statement, parameters, context, executemany):
        if (
            statement.startswith("SELECT")
            and "FROM entities" in statement
            and statement not in queries
        ):
            queries[statement] = {
                "sql": statement,
                "parameters": parameters,
                "plan": cursor.connection.execute(
                    "EXPLAIN QUERY PLAN " + statement, parameters
                ).fetchall(),
            }

    event.listen(store.database.engine.sync_engine, "before_cursor_execute", capture)
    report = {}
    operations = {
        "artifact_hit": lambda: store.list_tool_call_artifacts(
            "bench-project", "needle"
        ),
        "artifact_miss": lambda: store.list_tool_call_artifacts(
            "bench-project", "missing"
        ),
        "recovery_candidates": lambda: list(
            store.iter_readable_entities(
                ToolCall, {"status": "approved", "origin": "chat"}
            )
        ),
        "recovery_pass": ChatService(store).reconcile_stale_approved_tool_calls,
        "project_list": lambda: store.list_entities(
            Artifact, engagement_id="bench-project", limit=1000
        ),
    }
    try:
        for name, operation in operations.items():
            durations = []
            for _ in range(5):
                started = time.perf_counter()
                result = operation()
                durations.append((time.perf_counter() - started) * 1000)
            report[name] = {
                "samples_ms": durations,
                "median_ms": statistics.median(durations),
                "rows": len(result),
            }
        report["queries"] = list(queries.values())
        report["code_root"] = database_module.__file__
        if label == "after":
            baseline = json.loads((root / "queries-before.json").read_text())
            report["speedups"] = {
                name: baseline[name]["median_ms"] / report[name]["median_ms"]
                for name in operations
            }
            assert all(value >= 10 for value in report["speedups"].values()), report[
                "speedups"
            ]
            for query in queries.values():
                plan = " ".join(row[-1] for row in query["plan"])
                assert "SCAN entities" not in plan and "TEMP B-TREE" not in plan, plan
        (root / f"queries-{label}.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "queries"}
            ),
            flush=True,
        )
    finally:
        (await store.database.dispose())


def fingerprint(connection):
    digest = hashlib.sha256()
    for row in connection.execute(
        "SELECT id,kind,engagement_id,revision,payload,created_at,updated_at FROM entities ORDER BY id"
    ):
        digest.update(json.dumps(row, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


async def rehearse(root):
    source_path, backup, restored = (
        root / name for name in ("nebula.db", "verified-backup.db", "restored.db")
    )
    assert not backup.exists() and not restored.exists(), (
        "refusing to overwrite a backup or rehearsal"
    )
    with (
        closing(sqlite3.connect(source_path)) as source,
        closing(sqlite3.connect(backup)) as destination,
    ):
        assert source.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        before = fingerprint(source)
        source.backup(destination)
        assert destination.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert fingerprint(destination) == before
    with (
        closing(sqlite3.connect(backup)) as source,
        closing(sqlite3.connect(restored)) as destination,
    ):
        source.backup(destination)
    stop = threading.Event()
    peak_bytes = 0

    def watch():
        nonlocal peak_bytes
        while not stop.wait(0.02):
            peak_bytes = max(
                peak_bytes,
                sum(p.stat().st_size for p in root.glob("restored.db*") if p.is_file()),
            )

    watcher = threading.Thread(target=watch)
    watcher.start()
    started = time.perf_counter()
    try:
        database = Database(restored)
        seconds = time.perf_counter() - started
        (await database.dispose())
    finally:
        stop.set()
        watcher.join()
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert fingerprint(connection) == before
        counts = dict(
            connection.execute("SELECT kind,count(*) FROM entities GROUP BY kind")
        )
        assert connection.execute(
            "SELECT count(*) FROM entities WHERE kind='artifacts' AND artifact_tool_call_id IS NULL"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM entities WHERE kind='tool_calls' AND (tool_call_status IS NULL OR tool_call_origin IS NULL)"
        ).fetchone() == (0,)
    report = {
        "migration_seconds": seconds,
        "legacy_bytes": source_path.stat().st_size,
        "restored_bytes": restored.stat().st_size,
        "peak_database_and_wal_bytes": peak_bytes,
        "counts": counts,
        "authoritative_sha256": before,
        "backup_verified": True,
        "restored_integrity": "ok",
        "authoritative_envelopes_unchanged": True,
    }
    (root / "migration.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["seed", "measure", "migrate"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--label", choices=["before", "after"], default="before")
    parser.add_argument("--artifacts", type=int, default=1_100_000)
    parser.add_argument("--tool-calls", type=int, default=350_000)
    args = parser.parse_args()
    root = args.directory.resolve()
    if args.mode == "seed":
        seed(root, args.artifacts, args.tool_calls)
    else:
        assert json.loads((root / "synthetic.json").read_text())["synthetic"] is True
        if args.mode == "measure":
            (await measure(root, args.label))
        else:
            (await rehearse(root))


if __name__ == "__main__":
    asyncio.run(main())
