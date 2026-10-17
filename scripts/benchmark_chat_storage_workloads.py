"""Compare current Core SQLite and a correctness-matched LMDB message store.

This is a disposable experiment, not a production LMDB adapter. It measures
random conversation pages, older-page cursors, full reads, point reads, writes,
mixed readers/writer activity, and authenticated FastAPI requests. The LMDB
candidate keeps session metadata in a separate SQLite database and stores
messages by ID plus a (session, creation time, ID) index.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import platform
import random
import sqlite3
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter_ns

from fastapi.testclient import TestClient
import lmdb
from sqlalchemy import event, insert

from nebula.v3.api import create_app
from nebula.v3.chat import ChatHistoryConflict, ChatService
from nebula.v3.database import EntityRow
from nebula.v3.domain import ChatMessage, ChatRole, ChatSession, Engagement, utc_now
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError


PROJECT_ID = "storage-benchmark"
AUTH = {"Authorization": "Bearer benchmark-only"}
BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _session_id(number: int) -> str:
    return f"chat-{number:04d}"


def _message_id(number: int, sequence: int) -> str:
    return f"message-{number:04d}-{sequence:06d}"


def _fake_text(key: str, length: int) -> str:
    return hashlib.shake_256(key.encode()).hexdigest((length + 1) // 2)[:length]


def _index_key(session_id: str, created_at: datetime, message_id: str) -> bytes:
    timestamp = created_at.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"{session_id}\0{timestamp}\0{message_id}".encode()


def _file_bytes(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _sqlite_bytes(path: Path) -> int:
    return sum(
        _file_bytes(item) for item in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
    )


def _summary(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)

    def percentile(percent: int) -> float:
        position = max(0, (percent * len(ordered) + 99) // 100 - 1)
        return round(ordered[position], 3)

    return {
        "count": len(samples),
        "median_ms": round(median(samples), 3),
        "p95_ms": percentile(95),
        "p99_ms": percentile(99),
        "max_ms": round(ordered[-1], 3),
    }


class HybridMessages:
    """Read/write LMDB messages with durable session identity still in SQLite."""

    def __init__(self, path: Path, metadata: NebulaStore, *, durability: str) -> None:
        self.path = path
        self.metadata = metadata
        sync = durability != "buffered"
        metasync = durability == "full"
        self.environment = lmdb.open(
            str(path),
            subdir=False,
            max_dbs=2,
            map_size=4 << 30,
            sync=sync,
            metasync=metasync,
        )
        self.payloads = self.environment.open_db(b"messages_by_id")
        self.index = self.environment.open_db(b"messages_by_session_created")

    def seed_batch(self, records: list[tuple[bytes, bytes, bytes]]) -> None:
        with self.environment.begin(write=True) as transaction:
            for message_id, index_key, payload in records:
                assert transaction.put(
                    message_id, payload, db=self.payloads, overwrite=False
                )
                assert transaction.put(
                    index_key, message_id, db=self.index, overwrite=False
                )

    def get(self, message_id: str) -> ChatMessage:
        with self.environment.begin() as transaction:
            payload = transaction.get(message_id.encode(), db=self.payloads)
        if payload is None:
            raise NotFoundError(f"chat_messages entity not found: {message_id}")
        return ChatMessage.model_validate_json(payload)

    def page(
        self, session_id: str, *, limit: int, before_message_id: str | None = None
    ) -> list[ChatMessage]:
        self.metadata.get(ChatSession, session_id)
        before = self.get(before_message_id) if before_message_id else None
        if before is not None and before.session_id != session_id:
            raise ChatHistoryConflict(
                "history cursor does not belong to this conversation"
            )
        prefix = f"{session_id}\0".encode()
        upper = (
            _index_key(session_id, before.created_at, before.id)
            if before is not None
            else prefix + b"\xff"
        )
        messages: list[ChatMessage] = []
        with self.environment.begin() as transaction:
            cursor = transaction.cursor(db=self.index)
            valid = cursor.prev() if cursor.set_range(upper) else cursor.last()
            while valid and len(messages) < limit:
                if not cursor.key().startswith(prefix):
                    break
                payload = transaction.get(cursor.value(), db=self.payloads)
                if payload is None:
                    raise AssertionError("LMDB message index has no payload")
                messages.append(ChatMessage.model_validate_json(payload))
                valid = cursor.prev()
        return sorted(
            messages, key=lambda item: (item.sequence, item.created_at, item.id)
        )

    def full(self, session_id: str) -> list[ChatMessage]:
        self.metadata.get(ChatSession, session_id)
        prefix = f"{session_id}\0".encode()
        messages: list[ChatMessage] = []
        with self.environment.begin() as transaction:
            cursor = transaction.cursor(db=self.index)
            if cursor.set_range(prefix):
                for key, message_id in cursor:
                    if not key.startswith(prefix):
                        break
                    payload = transaction.get(message_id, db=self.payloads)
                    if payload is None:
                        raise AssertionError("LMDB message index has no payload")
                    messages.append(ChatMessage.model_validate_json(payload))
        return sorted(
            messages, key=lambda item: (item.sequence, item.created_at, item.id)
        )

    def create(self, message: ChatMessage) -> None:
        payload = message.model_dump_json().encode()
        with self.environment.begin(write=True) as transaction:
            if not transaction.put(
                message.id.encode(), payload, db=self.payloads, overwrite=False
            ):
                raise ConflictError(
                    f"chat_messages entity already exists: {message.id}"
                )
            transaction.put(
                _index_key(message.session_id, message.created_at, message.id),
                message.id.encode(),
                db=self.index,
            )

    def update_content(self, message_id: str, content: str) -> ChatMessage:
        with self.environment.begin(write=True) as transaction:
            payload = transaction.get(message_id.encode(), db=self.payloads)
            if payload is None:
                raise NotFoundError(f"chat_messages entity not found: {message_id}")
            previous = ChatMessage.model_validate_json(payload)
            updated = ChatMessage.model_validate(
                {
                    **previous.model_dump(),
                    "content": content,
                    "revision": previous.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            transaction.put(
                message_id.encode(),
                updated.model_dump_json().encode(),
                db=self.payloads,
            )
        return updated

    def delete(self, message_id: str) -> None:
        with self.environment.begin(write=True) as transaction:
            payload = transaction.get(message_id.encode(), db=self.payloads)
            if payload is None:
                raise NotFoundError(f"chat_messages entity not found: {message_id}")
            message = ChatMessage.model_validate_json(payload)
            transaction.delete(message_id.encode(), db=self.payloads)
            transaction.delete(
                _index_key(message.session_id, message.created_at, message.id),
                db=self.index,
            )

    def close(self) -> None:
        self.environment.close()


def _seed(
    sqlite_store: NebulaStore,
    metadata_store: NebulaStore,
    hybrid: HybridMessages,
    *,
    sessions: int,
    messages_per_session: int,
) -> float:
    project = Engagement(id=PROJECT_ID, name="Disposable chat storage benchmark")
    sqlite_store.create(project)
    metadata_store.create(project)
    chat_sessions = [
        ChatSession(
            id=_session_id(number),
            engagement_id=PROJECT_ID,
            title=f"Fake chat {number}",
            provider_profile_id="inert-provider",
            model="inert-model",
        )
        for number in range(sessions)
    ]
    sqlite_store.create_many(chat_sessions)
    metadata_store.create_many(chat_sessions)
    template = ChatMessage(
        engagement_id=PROJECT_ID,
        session_id=_session_id(0),
        sequence=1,
        role=ChatRole.USER,
        content="seed",
    ).model_dump(mode="json")
    started = perf_counter_ns()
    rows: list[dict] = []
    records: list[tuple[bytes, bytes, bytes]] = []

    def flush() -> None:
        if not rows:
            return
        with sqlite_store.database.engine.begin() as connection:
            connection.execute(insert(EntityRow), rows)
        hybrid.seed_batch(records)
        rows.clear()
        records.clear()

    for sequence in range(1, messages_per_session + 1):
        for number in range(sessions):
            session_id = _session_id(number)
            message_id = _message_id(number, sequence)
            created_at = BASE_TIME + timedelta(
                microseconds=(sequence - 1) * sessions + number
            )
            # Imported or replayed messages need not have sequence-ordered times.
            if sequence % 41 == 0:
                created_at -= timedelta(microseconds=15 * sessions)
            content_length = 4096 if sequence % 10 == 0 else 512
            reasoning_length = 32768 if sequence % 100 == 0 else 0
            payload = {
                **template,
                "id": message_id,
                "session_id": session_id,
                "sequence": sequence,
                "role": "user" if sequence % 2 else "assistant",
                "content": _fake_text(message_id, content_length),
                "reasoning": _fake_text(f"reasoning-{message_id}", reasoning_length),
                "created_at": created_at.isoformat(),
                "updated_at": created_at.isoformat(),
                "metadata": {"retracted_at": created_at.isoformat()}
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
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            )
            records.append(
                (
                    message_id.encode(),
                    _index_key(session_id, created_at, message_id),
                    json.dumps(payload, separators=(",", ":")).encode(),
                )
            )
            if len(rows) == 1_000:
                flush()
    flush()
    hybrid.environment.sync()
    return round((perf_counter_ns() - started) / 1_000_000_000, 3)


def _measure_pair(sqlite_operation, lmdb_operation, inputs: list, seed: int) -> dict:
    samples = {"sqlite": [], "lmdb": []}
    order = random.Random(seed)
    for item in inputs:
        candidates = [("sqlite", sqlite_operation), ("lmdb", lmdb_operation)]
        order.shuffle(candidates)
        results = {}
        for name, operation in candidates:
            started = perf_counter_ns()
            result = operation(item)
            samples[name].append((perf_counter_ns() - started) / 1_000_000)
            results[name] = result
        if results["sqlite"] != results["lmdb"]:
            raise AssertionError(f"backend result mismatch for {item}")
    return {name: _summary(values) for name, values in samples.items()}


def _ids(messages: list[ChatMessage]) -> list[str]:
    return [message.id for message in messages]


def _read_workloads(
    sqlite_store: NebulaStore,
    hybrid: HybridMessages,
    *,
    sessions: int,
    messages_per_session: int,
    read_count: int,
) -> dict:
    service = ChatService(sqlite_store)
    rng = random.Random(20261017)
    chosen = [_session_id(rng.randrange(sessions)) for _ in range(read_count)]
    page_size = min(61, messages_per_session)
    pages = _measure_pair(
        lambda sid: _ids(service.session_messages_page(sid, limit=page_size)),
        lambda sid: _ids(hybrid.page(sid, limit=page_size)),
        chosen,
        1,
    )
    cursors = []
    for sid in chosen:
        page = service.session_messages_page(sid, limit=page_size)
        cursors.append((sid, page[0].id))
    older = _measure_pair(
        lambda item: _ids(
            service.session_messages_page(
                item[0], limit=page_size, before_message_id=item[1]
            )
        ),
        lambda item: _ids(
            hybrid.page(item[0], limit=page_size, before_message_id=item[1])
        ),
        cursors,
        2,
    )
    full = _measure_pair(
        lambda sid: _ids(service.session_messages(sid, include_replaced=True)),
        lambda sid: _ids(hybrid.full(sid)),
        chosen[: max(10, read_count // 4)],
        3,
    )
    message_ids = [
        _message_id(rng.randrange(sessions), rng.randrange(1, messages_per_session + 1))
        for _ in range(read_count)
    ]
    points = _measure_pair(
        lambda message_id: sqlite_store.get(ChatMessage, message_id).content,
        lambda message_id: hybrid.get(message_id).content,
        message_ids,
        4,
    )
    return {
        "recent_page": pages,
        "older_page": older,
        "full_chat": full,
        "point_get": points,
    }


def _write_workloads(
    sqlite_store: NebulaStore,
    hybrid: HybridMessages,
    *,
    sessions: int,
    messages_per_session: int,
    write_count: int,
) -> dict:
    rng = random.Random(101)
    appends = []
    for number in range(write_count):
        session_number = rng.randrange(sessions)
        appends.append(
            ChatMessage(
                id=f"append-{number:06d}",
                engagement_id=PROJECT_ID,
                session_id=_session_id(session_number),
                sequence=messages_per_session + number + 1,
                role=ChatRole.ASSISTANT,
                content=_fake_text(f"append-{number}", 1024),
                created_at=BASE_TIME + timedelta(days=1, microseconds=number),
            )
        )
    append = _measure_pair(
        lambda message: sqlite_store.create(message).id,
        lambda message: (hybrid.create(message), message.id)[1],
        appends,
        5,
    )
    chosen = [
        _message_id(rng.randrange(sessions), rng.randrange(1, messages_per_session + 1))
        for _ in range(write_count)
    ]
    # Unique IDs keep both optimistic revision paths comparable.
    chosen = list(dict.fromkeys(chosen))
    update = _measure_pair(
        lambda message_id: (
            sqlite_store.update(
                ChatMessage,
                message_id,
                {"content": "edited fake message"},
                expected_revision=1,
            ).content
        ),
        lambda message_id: (
            hybrid.update_content(message_id, "edited fake message").content
        ),
        chosen,
        6,
    )
    delete = _measure_pair(
        lambda message_id: (sqlite_store.delete(ChatMessage, message_id), message_id)[
            1
        ],
        lambda message_id: (hybrid.delete(message_id), message_id)[1],
        [message.id for message in appends[: max(1, write_count // 4)]],
        7,
    )
    for message_id in chosen[:10]:
        left = sqlite_store.get(ChatMessage, message_id)
        right = hybrid.get(message_id)
        assert (left.content, left.revision) == (right.content, right.revision)
    return {"append": append, "update": update, "delete": delete}


def _concurrent_workloads(
    sqlite_store: NebulaStore,
    hybrid: HybridMessages,
    *,
    sessions: int,
    messages_per_session: int,
    read_count: int,
    write_count: int,
) -> dict:
    service = ChatService(sqlite_store)
    page_size = min(61, messages_per_session)

    def run_backend(name: str) -> dict:
        def read_worker(worker: int) -> list[float]:
            rng = random.Random(300 + worker)
            durations = []
            for _ in range(read_count // 4):
                sid = _session_id(rng.randrange(sessions))
                started = perf_counter_ns()
                if name == "sqlite":
                    service.session_messages_page(sid, limit=page_size)
                else:
                    hybrid.page(sid, limit=page_size)
                durations.append((perf_counter_ns() - started) / 1_000_000)
            return durations

        def write_worker() -> list[float]:
            durations = []
            for number in range(write_count):
                message = ChatMessage(
                    id=f"mixed-{number:06d}",
                    engagement_id=PROJECT_ID,
                    session_id=_session_id(number % sessions),
                    sequence=messages_per_session + 100_000 + number,
                    role=ChatRole.USER,
                    content="concurrent synthetic append",
                    created_at=BASE_TIME + timedelta(days=2, microseconds=number),
                )
                started = perf_counter_ns()
                if name == "sqlite":
                    sqlite_store.create(message)
                else:
                    hybrid.create(message)
                durations.append((perf_counter_ns() - started) / 1_000_000)
            return durations

        started = perf_counter_ns()
        with ThreadPoolExecutor(max_workers=5) as pool:
            readers = [pool.submit(read_worker, worker) for worker in range(4)]
            writer = pool.submit(write_worker)
            read_samples = [duration for task in readers for duration in task.result()]
            write_samples = writer.result()
        elapsed = (perf_counter_ns() - started) / 1_000_000_000
        return {
            "wall_seconds": round(elapsed, 3),
            "reads": _summary(read_samples),
            "writes": _summary(write_samples),
        }

    # Reverse the order on reruns rather than running both engines concurrently.
    order = ["sqlite", "lmdb"]
    random.Random(88).shuffle(order)
    return {name: run_backend(name) for name in order}


def _api_workload(
    sqlite_store: NebulaStore,
    metadata_store: NebulaStore,
    hybrid: HybridMessages,
    *,
    sessions: int,
    messages_per_session: int,
    read_count: int,
) -> dict:
    original = ChatService.session_messages_page

    def dispatch(self, session_id, *, limit, before_message_id=None):
        if self.store is metadata_store:
            return hybrid.page(
                session_id, limit=limit, before_message_id=before_message_id
            )
        return original(
            self, session_id, limit=limit, before_message_id=before_message_id
        )

    ChatService.session_messages_page = dispatch
    samples = {"sqlite": [], "lmdb": []}
    combined = {"sqlite": [], "lmdb": []}
    rng = random.Random(808)
    order = random.Random(909)
    try:
        sqlite_app = create_app(sqlite_store, auth_token="benchmark-only")
        hybrid_app = create_app(metadata_store, auth_token="benchmark-only")
        with (
            TestClient(sqlite_app) as sqlite_client,
            TestClient(hybrid_app) as hybrid_client,
        ):
            clients = {"sqlite": sqlite_client, "lmdb": hybrid_client}
            page_size = min(61, messages_per_session)
            for _ in range(read_count):
                sid = _session_id(rng.randrange(sessions))
                pair = list(clients)
                order.shuffle(pair)
                bodies = {}
                for name in pair:
                    client = clients[name]
                    started = perf_counter_ns()
                    response = client.get(
                        f"/api/v1/chat/sessions/{sid}/messages",
                        params={"include_replaced": "true", "limit": page_size},
                        headers=AUTH,
                    )
                    page_ms = (perf_counter_ns() - started) / 1_000_000
                    assert response.status_code == 200, response.text
                    samples[name].append(page_ms)
                    pending = client.get(
                        f"/api/v1/chat/sessions/{sid}/pending-turn", headers=AUTH
                    )
                    assert pending.status_code == 200, pending.text
                    combined[name].append((perf_counter_ns() - started) / 1_000_000)
                    body = response.json()
                    for message in body:
                        # The same edit is timed separately in each backend.
                        message.pop("updated_at", None)
                    bodies[name] = (body, pending.json())
                if bodies["sqlite"] != bodies["lmdb"]:
                    sqlite_messages, sqlite_pending = bodies["sqlite"]
                    lmdb_messages, lmdb_pending = bodies["lmdb"]
                    differences = [
                        (
                            index,
                            [
                                key
                                for key in set(left) | set(right)
                                if left.get(key) != right.get(key)
                            ],
                        )
                        for index, (left, right) in enumerate(
                            zip(sqlite_messages, lmdb_messages)
                        )
                        if left != right
                    ]
                    raise AssertionError(
                        f"Core API response mismatch for {sid}: "
                        f"message counts {len(sqlite_messages)}/{len(lmdb_messages)}, "
                        f"fields {differences[:3]}, pending {sqlite_pending != lmdb_pending}"
                    )
    finally:
        ChatService.session_messages_page = original
    return {
        "messages_request": {
            name: _summary(values) for name, values in samples.items()
        },
        "messages_then_pending_turn": {
            name: _summary(values) for name, values in combined.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=200)
    parser.add_argument("--messages-per-session", type=int, default=500)
    parser.add_argument("--reads", type=int, default=200)
    parser.add_argument("--writes", type=int, default=80)
    parser.add_argument(
        "--sqlite-synchronous",
        choices=("normal", "full"),
        default="normal",
        help="SQLite WAL commit synchronization for the message store",
    )
    parser.add_argument(
        "--lmdb-durability",
        choices=("full", "metadata-deferred", "buffered"),
        default="full",
        help="full sync, data-only sync, or no commit sync (experimental)",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.sessions, args.messages_per_session, args.reads, args.writes) < 1:
        parser.error("all workload sizes must be positive")

    with TemporaryDirectory(prefix="nebula-chat-storage-expanded-") as temporary:
        root = Path(temporary)
        sqlite_path = root / "sqlite-core.db"
        metadata_path = root / "lmdb-metadata.db"
        lmdb_path = root / "messages.lmdb"
        sqlite_store = NebulaStore(sqlite_path)
        if args.sqlite_synchronous == "full":
            # Core installs NORMAL on connect. Override it for this disposable
            # comparison, then discard connections opened during bootstrap.
            def sync_wal_on_commit(dbapi_connection, _connection_record) -> None:
                dbapi_connection.execute("PRAGMA synchronous=FULL")

            event.listen(sqlite_store.database.engine, "connect", sync_wal_on_commit)
            sqlite_store.database.engine.dispose()
        with sqlite_store.database.engine.connect() as connection:
            sqlite_journal_mode = connection.exec_driver_sql(
                "PRAGMA journal_mode"
            ).scalar_one()
            sqlite_synchronous = connection.exec_driver_sql(
                "PRAGMA synchronous"
            ).scalar_one()
        expected_synchronous = 2 if args.sqlite_synchronous == "full" else 1
        if sqlite_journal_mode != "wal" or sqlite_synchronous != expected_synchronous:
            raise AssertionError(
                f"SQLite WAL sync mismatch: {sqlite_journal_mode=} {sqlite_synchronous=}"
            )
        metadata_store = NebulaStore(metadata_path)
        hybrid = HybridMessages(
            lmdb_path, metadata_store, durability=args.lmdb_durability
        )
        try:
            seed_seconds = _seed(
                sqlite_store,
                metadata_store,
                hybrid,
                sessions=args.sessions,
                messages_per_session=args.messages_per_session,
            )
            reads = _read_workloads(
                sqlite_store,
                hybrid,
                sessions=args.sessions,
                messages_per_session=args.messages_per_session,
                read_count=args.reads,
            )
            writes = _write_workloads(
                sqlite_store,
                hybrid,
                sessions=args.sessions,
                messages_per_session=args.messages_per_session,
                write_count=args.writes,
            )
            concurrent = _concurrent_workloads(
                sqlite_store,
                hybrid,
                sessions=args.sessions,
                messages_per_session=args.messages_per_session,
                read_count=args.reads,
                write_count=args.writes,
            )
            api = _api_workload(
                sqlite_store,
                metadata_store,
                hybrid,
                sessions=args.sessions,
                messages_per_session=args.messages_per_session,
                read_count=min(args.reads, 100),
            )
            hybrid.environment.sync()
            report = {
                "base_commit": subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"], text=True
                ).strip(),
                "corpus": {
                    "sessions": args.sessions,
                    "messages_per_session": args.messages_per_session,
                    "project_messages": args.sessions * args.messages_per_session,
                    "content_bytes": {"usual": 512, "every_tenth": 4096},
                    "reasoning_bytes_every_hundredth": 32768,
                    "out_of_sequence_created_at_every": 41,
                },
                "environment": {
                    "python": sys.version.split()[0],
                    "sqlite": sqlite3.sqlite_version,
                    "lmdb_python": lmdb.__version__,
                    "platform": platform.platform(),
                },
                "lmdb_durability": args.lmdb_durability,
                "sqlite_durability": {
                    "journal_mode": sqlite_journal_mode,
                    "synchronous": args.sqlite_synchronous,
                    "pragma_value": sqlite_synchronous,
                },
                "seed_both_seconds": seed_seconds,
                "files_bytes": {
                    "sqlite_current_total": _sqlite_bytes(sqlite_path),
                    "lmdb_data": _file_bytes(lmdb_path),
                    "lmdb_metadata_sqlite_total": _sqlite_bytes(metadata_path),
                },
                "measurements": {
                    "random_reads": reads,
                    "isolated_writes": writes,
                    "four_readers_one_writer": concurrent,
                    "authenticated_core_api": api,
                },
            }
        finally:
            hybrid.close()
            sqlite_store.database.engine.dispose()
            metadata_store.database.engine.dispose()

    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
