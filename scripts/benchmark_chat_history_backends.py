"""Compare throwaway conversation-history reads from SQLite and LMDB.

The primary SQLite path calls Core's real ChatService.session_messages
implementation. A payload-only SQLite query estimates the cost of dropping
SQLAlchemy ORM row construction without changing the database engine.
The direct JSON SQLite query uses the same indexed table with Pydantic's
JSON-validation path, matching the LMDB decoder more closely.
The LMDB path is a proposed hybrid read index: session metadata remains in
SQLite, while messages are stored under ordered (session, sequence, id) keys.
Both paths materialize and validate the same ChatMessage models. The legacy
project scan is included to show the cost of the lookup that preceded the
indexed Core implementation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import gc
import hashlib
import json
from math import ceil
from pathlib import Path
import platform
import random
import sqlite3
from statistics import median
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

import lmdb
from sqlalchemy import insert, select
import sqlalchemy

from nebula.v3.chat import ChatService
from nebula.v3.database import EntityRow
from nebula.v3.domain import ChatMessage, ChatRole, ChatSession, Engagement
from nebula.v3.storage import NebulaStore


PROJECT_ID = "benchmark-project"
TARGET_SESSION = "chat-0000"


def _message_key(session_id: str, sequence: int, message_id: str) -> bytes:
    return f"{session_id}\0{sequence:010d}\0{message_id}".encode()


def _seed(
    store: NebulaStore,
    environment: lmdb.Environment,
    *,
    sessions: int,
    messages_per_session: int,
    content_bytes: int,
) -> float:
    store.create(Engagement(id=PROJECT_ID, name="Throwaway benchmark"))
    store.create_many(
        [
            ChatSession(
                id=f"chat-{number:04d}",
                engagement_id=PROJECT_ID,
                title=f"Fake chat {number}",
                provider_profile_id="fake-provider",
                model="fake-model",
            )
            for number in range(sessions)
        ]
    )
    started = perf_counter()
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    template = ChatMessage(
        engagement_id=PROJECT_ID,
        session_id=TARGET_SESSION,
        sequence=1,
        role=ChatRole.USER,
        content="seed",
    ).model_dump(mode="json")
    rows: list[dict] = []
    pairs: list[tuple[bytes, bytes]] = []

    def flush() -> None:
        if not rows:
            return
        with store.database.engine.begin() as connection:
            connection.execute(insert(EntityRow), rows)
        with environment.begin(write=True) as transaction:
            for key, value in pairs:
                transaction.put(key, value)
        rows.clear()
        pairs.clear()

    # Interleave sessions, as the previous project-wide Core scan did.
    for sequence in range(1, messages_per_session + 1):
        for number in range(sessions):
            session_id = f"chat-{number:04d}"
            message_id = f"message-{number:04d}-{sequence:06d}"
            timestamp = base_time + timedelta(
                microseconds=(sequence - 1) * sessions + number
            )
            digest = hashlib.blake2b(message_id.encode(), digest_size=32).hexdigest()
            content = (f"{message_id} " + digest * ceil(content_bytes / len(digest)))[
                :content_bytes
            ]
            payload = {
                **template,
                "id": message_id,
                "session_id": session_id,
                "sequence": sequence,
                "role": "user" if sequence % 2 else "assistant",
                "content": content,
                "created_at": timestamp.isoformat(),
                "updated_at": timestamp.isoformat(),
                "metadata": {"retracted_at": timestamp.isoformat()}
                if sequence % 97 == 0
                else {},
            }
            rows.append(
                {
                    "id": message_id,
                    "kind": ChatMessage.entity_kind,
                    "engagement_id": PROJECT_ID,
                    "chat_session_id": session_id,
                    "revision": 1,
                    "payload": payload,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            pairs.append(
                (
                    _message_key(session_id, sequence, message_id),
                    json.dumps(payload, separators=(",", ":")).encode(),
                )
            )
            if len(rows) == 1_000:
                flush()
    flush()
    return perf_counter() - started


def _legacy_project_scan(store: NebulaStore) -> list[ChatMessage]:
    session = store.get(ChatSession, TARGET_SESSION)
    selected: list[ChatMessage] = []
    offset = 0
    while True:
        page = store.list_entities(
            ChatMessage,
            engagement_id=session.engagement_id,
            offset=offset,
            limit=1_000,
        )
        selected.extend(message for message in page if message.session_id == session.id)
        if len(page) < 1_000:
            break
        offset += len(page)
    return sorted(selected, key=lambda item: (item.sequence, item.created_at, item.id))


def _lmdb_history(
    store: NebulaStore,
    environment: lmdb.Environment,
    page_size: int | None = None,
) -> list[ChatMessage]:
    # The hybrid path still validates the authoritative session in SQLite.
    session = store.get(ChatSession, TARGET_SESSION)
    prefix = f"{session.id}\0".encode()
    messages: list[ChatMessage] = []
    with environment.begin() as transaction:
        cursor = transaction.cursor()
        if page_size is None and cursor.set_range(prefix):
            for key, value in cursor:
                if not key.startswith(prefix):
                    break
                messages.append(ChatMessage.model_validate_json(value))
        elif page_size is not None:
            valid = (
                cursor.prev() if cursor.set_range(prefix + b"\xff") else cursor.last()
            )
            while valid and len(messages) < page_size:
                if not cursor.key().startswith(prefix):
                    break
                messages.append(ChatMessage.model_validate_json(cursor.value()))
                valid = cursor.prev()
    return sorted(messages, key=lambda item: (item.sequence, item.created_at, item.id))


def _sqlite_payload_history(
    store: NebulaStore, page_size: int | None = None
) -> list[ChatMessage]:
    session = store.get(ChatSession, TARGET_SESSION)
    statement = select(EntityRow.payload).where(
        EntityRow.kind == ChatMessage.entity_kind,
        EntityRow.chat_session_id == session.id,
    )
    if page_size is None:
        statement = statement.order_by(EntityRow.created_at, EntityRow.id)
    else:
        statement = statement.order_by(
            EntityRow.created_at.desc(), EntityRow.id.desc()
        ).limit(page_size)
    with store.database.engine.connect() as connection:
        messages = [
            ChatMessage.model_validate(payload)
            for payload in connection.execute(statement).scalars()
        ]
    return sorted(messages, key=lambda item: (item.sequence, item.created_at, item.id))


def _sqlite_json_history(
    store: NebulaStore,
    connection: sqlite3.Connection,
    page_size: int | None = None,
) -> list[ChatMessage]:
    session = store.get(ChatSession, TARGET_SESSION)
    order = "DESC" if page_size is not None else "ASC"
    limit = " LIMIT ?" if page_size is not None else ""
    parameters = (ChatMessage.entity_kind, session.id)
    if page_size is not None:
        parameters += (page_size,)
    rows = connection.execute(
        "SELECT payload FROM entities "
        f"WHERE kind = ? AND chat_session_id = ? ORDER BY created_at {order}, id {order}{limit}",
        parameters,
    )
    messages = [ChatMessage.model_validate_json(row[0]) for row in rows]
    return sorted(messages, key=lambda item: (item.sequence, item.created_at, item.id))


def _summarize(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "median_ms": round(median(samples), 3),
        "p95_ms": round(ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)], 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


def _sample(operation) -> tuple[float, int]:
    collections_before = sum(item["collections"] for item in gc.get_stats())
    started = perf_counter()
    operation()
    duration_ms = (perf_counter() - started) * 1_000
    collections_after = sum(item["collections"] for item in gc.get_stats())
    return duration_ms, collections_after - collections_before


def _summarize_with_gc(
    samples: list[float],
    gc_samples: list[float],
    no_gc_samples: list[float],
    collections: int,
) -> dict[str, float | int | None]:
    return {
        **_summarize(samples),
        "gc_collections": collections,
        "gc_sample_count": len(gc_samples),
        "max_gc_ms": round(max(gc_samples), 3) if gc_samples else None,
        "max_no_gc_ms": round(max(no_gc_samples), 3) if no_gc_samples else None,
    }


def _measure(operation, repeats: int) -> dict[str, float | int | None]:
    samples = []
    gc_samples = []
    no_gc_samples = []
    collections = 0
    for _ in range(repeats):
        duration_ms, count = _sample(operation)
        samples.append(duration_ms)
        collections += count
        if count:
            gc_samples.append(duration_ms)
        else:
            no_gc_samples.append(duration_ms)
    return _summarize_with_gc(samples, gc_samples, no_gc_samples, collections)


def _measure_competitors(
    operations, repeats: int
) -> dict[str, dict[str, float | int | None]]:
    samples: dict[str, list[float]] = {name: [] for name in operations}
    gc_samples: dict[str, list[float]] = {name: [] for name in operations}
    no_gc_samples: dict[str, list[float]] = {name: [] for name in operations}
    collections = {name: 0 for name in operations}
    rng = random.Random(42)
    for _ in range(repeats):
        order = list(operations)
        rng.shuffle(order)
        for name in order:
            duration_ms, count = _sample(operations[name])
            samples[name].append(duration_ms)
            collections[name] += count
            if count:
                gc_samples[name].append(duration_ms)
            else:
                no_gc_samples[name].append(duration_ms)
    return {
        name: _summarize_with_gc(
            values,
            gc_samples[name],
            no_gc_samples[name],
            collections[name],
        )
        for name, values in samples.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=200)
    parser.add_argument("--messages-per-session", type=int, default=500)
    parser.add_argument("--content-bytes", type=int, default=512)
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--legacy-repeats", type=int, default=0)
    parser.add_argument("--page-size", type=int)
    parser.add_argument("--disable-gc-during-measure", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        min(args.sessions, args.messages_per_session, args.content_bytes, args.repeats)
        < 1
        or args.legacy_repeats < 0
    ):
        parser.error(
            "numeric arguments must be positive, except legacy repeats may be zero"
        )
    if args.page_size is not None and not 1 <= args.page_size <= 200:
        parser.error("page size must be between 1 and the API's 200-message limit")

    with TemporaryDirectory(prefix="nebula-chat-backends-") as temporary:
        temporary_path = Path(temporary)
        sqlite_path = temporary_path / "history.sqlite"
        lmdb_path = temporary_path / "history.lmdb"
        store = NebulaStore(sqlite_path)
        environment = lmdb.open(str(lmdb_path), subdir=False, map_size=4 << 30)
        sqlite_connection = None
        try:
            seed_seconds = _seed(
                store,
                environment,
                sessions=args.sessions,
                messages_per_session=args.messages_per_session,
                content_bytes=args.content_bytes,
            )
            sqlite_connection = sqlite3.connect(sqlite_path)
            service = ChatService(store)
            operations = {
                "sqlite_indexed": (
                    (
                        lambda: service.session_messages_page(
                            TARGET_SESSION, limit=args.page_size
                        )
                    )
                    if args.page_size is not None
                    else (
                        lambda: service.session_messages(
                            TARGET_SESSION, include_replaced=True
                        )
                    )
                ),
                "sqlite_payload_only": lambda: _sqlite_payload_history(
                    store, args.page_size
                ),
                "sqlite_json_direct": lambda: _sqlite_json_history(
                    store, sqlite_connection, args.page_size
                ),
                "lmdb_hybrid": lambda: _lmdb_history(
                    store, environment, args.page_size
                ),
            }
            if args.legacy_repeats:
                operations["sqlite_legacy_scan"] = lambda: (
                    _legacy_project_scan(store)[-args.page_size :]
                    if args.page_size
                    else _legacy_project_scan(store)
                )
            expected = operations["sqlite_indexed"]()
            expected_ids = [
                (message.id, message.content, message.metadata) for message in expected
            ]
            for name, operation in operations.items():
                actual = [
                    (message.id, message.content, message.metadata)
                    for message in operation()
                ]
                if actual != expected_ids:
                    raise AssertionError(f"{name} returned a different transcript")

            gc_was_enabled = gc.isenabled()
            if args.disable_gc_during_measure and gc_was_enabled:
                gc.disable()
            try:
                # Shuffle the candidates each round to limit order effects.
                results = _measure_competitors(
                    {
                        name: operations[name]
                        for name in (
                            "sqlite_indexed",
                            "sqlite_payload_only",
                            "sqlite_json_direct",
                            "lmdb_hybrid",
                        )
                    },
                    args.repeats,
                )
                if args.legacy_repeats:
                    results["sqlite_legacy_scan"] = _measure(
                        operations["sqlite_legacy_scan"],
                        args.legacy_repeats,
                    )
            finally:
                if args.disable_gc_during_measure and gc_was_enabled:
                    gc.enable()
            environment.sync()
            with store.database.engine.connect() as connection:
                order = "DESC" if args.page_size is not None else "ASC"
                limit = " LIMIT ?" if args.page_size is not None else ""
                parameters = ("chat_messages", TARGET_SESSION)
                if args.page_size is not None:
                    parameters += (args.page_size,)
                query_plan = [
                    str(row)
                    for row in connection.exec_driver_sql(
                        "EXPLAIN QUERY PLAN SELECT payload FROM entities "
                        f"WHERE kind = ? AND chat_session_id = ? ORDER BY created_at {order}, id {order}{limit}",
                        parameters,
                    ).all()
                ]
            sqlite_files = {
                name: path.stat().st_size if path.exists() else 0
                for name, path in {
                    "database": sqlite_path,
                    "wal": Path(f"{sqlite_path}-wal"),
                    "shm": Path(f"{sqlite_path}-shm"),
                }.items()
            }
            report = {
                "corpus": {
                    "sessions": args.sessions,
                    "messages_per_session": args.messages_per_session,
                    "project_messages": args.sessions * args.messages_per_session,
                    "selected_messages": len(expected),
                    "page_size": args.page_size,
                    "content_bytes": args.content_bytes,
                    "replaced_interval": 97,
                },
                "environment": {
                    "python": sys.version.split()[0],
                    "platform": platform.platform(),
                    "sqlite": sqlite3.sqlite_version,
                    "sqlalchemy": sqlalchemy.__version__,
                    "lmdb_python": lmdb.__version__,
                },
                "seed_seconds": round(seed_seconds, 3),
                "files_bytes": {
                    "sqlite_total": sum(sqlite_files.values()),
                    "sqlite_components": sqlite_files,
                    "lmdb_data": lmdb_path.stat().st_size,
                },
                "sqlite_query_plan": query_plan,
                "measurement": "warm local process reads; full ChatMessage validation; no HTTP or browser rendering",
                "gc_disabled_during_measure": args.disable_gc_during_measure,
                "repeats": {"nonlegacy": args.repeats, "legacy": args.legacy_repeats},
                "results": results,
            }
        finally:
            if sqlite_connection is not None:
                sqlite_connection.close()
            environment.close()
            store.database.engine.dispose()

    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
