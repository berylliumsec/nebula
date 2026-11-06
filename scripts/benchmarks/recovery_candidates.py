"""Measure full recovery passes against an explicitly disposable synthetic DB.

Run the same script with PYTHONPATH pointing to baseline/candidate src trees.
Use a fresh --root and an owned disposable PostgreSQL database for --url.
No live Core, provider, or external tool is contacted. The production 15-second
scheduler is unchanged; this probe invokes its complete recovery_tick directly.
"""

from __future__ import annotations
import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import threading
import time
from datetime import datetime, timezone
import inspect
from sqlalchemy import event, insert, text
from sqlalchemy.dialects.postgresql.psycopg import PGDialectAsync_psycopg
from nebula.v3.chat import ChatService
from nebula.v3.database import Database, EntityRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.storage import NebulaStore, _entity_lookup_fields, _entity_order
import nebula.v3.storage as storage_module

N = 620
TARGET = 21 * 1024 * 1024


def shape(statement, dialect):
    compiled = statement.compile(dialect=dialect)
    return {
        "hash": hashlib.sha256(compiled.string.encode()).hexdigest()[:12],
        "sql": compiled.string,
    }


def fingerprints():
    result = {}
    for status in [ChatTurnStatus.INTERRUPTED, ChatTurnStatus.WAITING_CALLBACK]:
        base = NebulaStore._entity_statement(ChatTurn, {"status": status.value})
        for name, statement in [
            (
                "ids",
                base.with_only_columns(EntityRow.id).order_by(*_entity_order(False)),
            ),
            ("payload_page", base.where(EntityRow.id.in_(["synthetic-0"]))),
        ]:
            result[f"{status.value}_{name}"] = shape(
                statement, PGDialectAsync_psycopg()
            )
    return result


async def scenario(mode, args):
    path = args.root / f"{mode}.db"
    database = Database(
        args.url or path, pool_settings=PoolSettings(5, 12, 2, requested_size=4)
    )
    store = NebulaStore(database)
    await store.database.initialize()
    template = ChatTurn(
        id="synthetic-0000",
        engagement_id="synthetic-project",
        session_id="synthetic-session",
        provider_profile_id="inert-provider",
        model="inert-model",
        status=ChatTurnStatus.INTERRUPTED,
        request_snapshot={
            "recovery": {"required": False, "automatic_retry_pending": False}
        },
        created_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
        updated_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
    )
    payload = template.model_dump(mode="json")
    if mode == "text":
        payload["content"] = "x" * (TARGET // N - len(json.dumps(payload)))
    else:
        # Same serialized size, many small legacy history objects. Neither
        # synthetic structure claims to match the unavailable live JSON.
        empty_size = len(json.dumps(payload))
        payload["tool_history"] = [{"v": 0}] * ((TARGET // N - empty_size) // 10)
    records = []
    payload_bytes = 0
    fixture_digest = hashlib.sha256()
    for i in range(N):
        model = template.model_copy(update={"id": f"synthetic-{i:04}"})
        item = dict(payload, id=model.id)
        serialized = json.dumps(item, sort_keys=True)
        payload_bytes += len(serialized)
        fixture_digest.update(serialized.encode())
        records.append(
            {
                "id": model.id,
                "kind": model.entity_kind,
                "engagement_id": model.engagement_id,
                "revision": model.revision,
                "payload": item,
                "created_at": model.created_at,
                "updated_at": model.updated_at,
                **_entity_lookup_fields(model),
            }
        )
    async with store.database.engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM entities WHERE id LIKE 'synthetic-%'")
        )
        await connection.execute(insert(EntityRow), records)
    del records
    captured = []
    active = None
    decoder_stats = Counter()
    validation_stats = Counter()
    raw_loads = json.loads
    raw_validate = storage_module._row_to_entity
    main_thread = threading.get_ident()

    def decode(value):
        started = time.perf_counter()
        result = raw_loads(value)
        if active is not None:
            decoder_stats["rows"] += 1
            decoder_stats["bytes"] += len(value)
            decoder_stats["ms"] += (time.perf_counter() - started) * 1000
            decoder_stats["loop_thread_rows"] += threading.get_ident() == main_thread
        return result

    def validate(row, model=None):
        started = time.perf_counter()
        result = raw_validate(row, model)
        if active is not None:
            validation_stats["rows"] += 1
            validation_stats["ms"] += (time.perf_counter() - started) * 1000
            validation_stats["loop_thread_rows"] += threading.get_ident() == main_thread
        return result

    store.database.engine.sync_engine.dialect._json_deserializer = decode
    storage_module._row_to_entity = validate

    @event.listens_for(store.database.engine.sync_engine, "connect")
    def on_connect(connection, _record):
        if store.database.engine.dialect.name == "postgresql":
            from psycopg.types.json import set_json_loads

            set_json_loads(decode, connection.driver_connection)

    @event.listens_for(store.database.engine.sync_engine, "before_cursor_execute")
    def before(_conn, _cursor, statement, params, context, _many):
        if active is not None:
            context._probe_started = time.perf_counter()
            compiled = context.compiled
            context._probe_shape = (
                compiled.string if compiled is not None else statement
            )

    @event.listens_for(store.database.engine.sync_engine, "after_cursor_execute")
    def after(_conn, _cursor, _statement, _params, context, _many):
        if active is not None and hasattr(context, "_probe_started"):
            compiled_shape = context._probe_shape
            captured.append(
                {
                    "hash": hashlib.sha256(compiled_shape.encode()).hexdigest()[:12],
                    "ms": round(
                        (time.perf_counter() - context._probe_started) * 1000, 3
                    ),
                    "payload_selected": "entities.payload" in compiled_shape,
                    "sql": compiled_shape,
                }
            )

    await store.database.engine.dispose()

    service = ChatService(store)

    async def recovery_pass():
        candidates = []
        scan = service._stopped_turn_candidates

        async def counted():
            found = await scan()
            candidates.extend(found)
            return found

        service._stopped_turn_candidates = counted
        try:
            await service.recovery_tick()
        finally:
            service._stopped_turn_candidates = scan
        return candidates

    async def projection_control():
        headers = await store.chat_turn_status_rows([ChatTurnStatus.INTERRUPTED])
        return [row for row in headers if row.recovery_visible]

    async def measure(name, function):
        nonlocal active
        captured.clear()
        decoder_stats.clear()
        validation_stats.clear()
        lags = []
        keep_running = True

        async def heartbeat():
            loop = asyncio.get_running_loop()
            while keep_running:
                due = loop.time() + 0.005
                await asyncio.sleep(0.005)
                lags.append(max(0, loop.time() - due) * 1000)

        ticker = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.01)
        active = name
        started = time.perf_counter()
        result = await function()
        elapsed = (time.perf_counter() - started) * 1000
        active = None
        await asyncio.sleep(0.01)
        keep_running = False
        await ticker
        return {
            "name": name,
            "result_count": len(result),
            "elapsed_ms": round(elapsed, 3),
            "max_loop_lag_ms": round(max(lags, default=0), 3),
            "json_decode": dict(decoder_stats),
            "entity_validation": dict(validation_stats),
            "statements": list(captured),
        }

    try:
        await projection_control()
        results = [
            await measure(f"recovery_pass_{index + 1}", recovery_pass)
            for index in range(args.passes)
        ]
        results.append(await measure("projection_control", projection_control))
        sql_shapes = {}
        for measurement in results:
            for statement in measurement["statements"]:
                sql_shapes[statement["hash"]] = statement.pop("sql")
        return {
            "mode": mode,
            "rows": N,
            "payload_bytes": payload_bytes,
            "fixture_sha256": fixture_digest.hexdigest(),
            "dialect": store.database.engine.dialect.name,
            "sql_shapes": sql_shapes,
            "measurements": results,
        }
    finally:
        await service.shutdown()
        storage_module._row_to_entity = raw_validate
        await store.event_history.close()
        await store.database.dispose()
        if not args.url:
            for candidate in [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]:
                candidate.unlink(missing_ok=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Owned disposable PostgreSQL database only")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--passes", type=int, default=5)
    parser.add_argument("--label", default="baseline")
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["text", "structured"],
        default=["text", "structured"],
    )
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    if args.passes < 1:
        parser.error("--passes must be positive")
    output = {
        "label": args.label,
        "postgresql_fingerprints": fingerprints(),
        "scenarios": [],
        "source_sha256": {
            Path(p).name: hashlib.sha256(Path(p).read_bytes()).hexdigest()
            for p in [storage_module.__file__, inspect.getsourcefile(ChatService)]
        },
    }

    for mode in args.modes:
        output["scenarios"].append(await scenario(mode, args))
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    summary = {
        "postgresql_hashes": {
            k: v["hash"] for k, v in output["postgresql_fingerprints"].items()
        },
        "scenarios": [],
    }
    for s in output["scenarios"]:
        summary["scenarios"].append(
            {
                **{k: v for k, v in s.items() if k != "measurements"},
                "measurements": [
                    {
                        **r,
                        "statements": [
                            {k: v for k, v in stmt.items() if k != "sql"}
                            for stmt in r["statements"]
                        ],
                    }
                    for r in s["measurements"]
                ],
            }
        )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
