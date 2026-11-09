#!/usr/bin/env python3
"""Synthetic old/new Core load for the active chat-turn status lookup.

Run ``--help`` for subcommands. ``run`` creates only synthetic data, starts a
fresh authenticated LAN Core and authenticated MCP gateway per load level, and
removes its temporary databases and processes in a ``finally`` block.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)

    def at(fraction: float) -> float:
        position = fraction * (len(ordered) - 1)
        low = int(position)
        high = min(low + 1, len(ordered) - 1)
        return round(
            ordered[low] + (ordered[high] - ordered[low]) * (position - low), 2
        )

    return {
        "p50": at(0.5),
        "p95": at(0.95),
        "p99": at(0.99),
        "max": round(ordered[-1], 2),
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


async def _seed(args: argparse.Namespace) -> None:
    from nebula.v3.domain import (
        ChatSession,
        ChatTurn,
        ChatTurnStatus,
        Engagement,
        EngagementStatus,
        HarnessKind,
        HarnessProfile,
        ProviderProfile,
        WorkItem,
    )
    from nebula.v3.storage import NebulaStore
    from nebula.v3.work import WorkService
    from nebula.v3.chat_agent_messages import AgentMessageService

    root = args.root
    root.mkdir(parents=True, exist_ok=True)
    db = root / "nebula.db"
    store = NebulaStore(db)
    project = await store.create(
        Engagement(id="bench-project", name="Synthetic active turns")
    )
    archived = await store.create(
        Engagement(
            id="bench-archived",
            name="Synthetic archive",
            status=EngagementStatus.ARCHIVED,
        )
    )
    provider = await store.create(
        ProviderProfile(
            id="bench-inert-provider",
            name="Inert local fixture",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["fixture-model"],
            metadata={
                "default_model": "fixture-model",
                "endpoint": "http://127.0.0.1:9/v1",
            },
        )
    )
    harness = await store.create(
        HarnessProfile(
            id="bench-inert-harness",
            name="Inert harness fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="fixture-model",
            privacy={"local_only": True, "permits_sensitive_data": False},
        )
    )
    session_count = args.interrupted + args.active
    session_ids = []
    for index in range(session_count):
        chat = await store.create(
            ChatSession(
                id=f"bench-session-{index:04d}",
                engagement_id=project.id,
                title=f"Synthetic agent {index:04d}",
                provider_profile_id=provider.id,
                model="fixture-model",
                metadata={"allow_agent_messaging": True},
            )
        )
        session_ids.append(chat.id)
    for extra_index, (name, metadata, engagement) in enumerate(
        (
            ("Temporary", {"temporary_assistant": True}, project),
            ("Side", {"side_chat": True}, project),
            ("Archived session", {"archived_at": "2026-11-04T00:00:00Z"}, project),
            ("Archived project", {}, archived),
        )
    ):
        chat = await store.create(
            ChatSession(
                id=f"bench-extra-session-{extra_index}",
                engagement_id=engagement.id,
                title=name,
                provider_profile_id=provider.id,
                model="fixture-model",
                metadata={"allow_agent_messaging": True, **metadata},
            )
        )
        (
            await store.create(
                ChatTurn(
                    id=f"bench-extra-turn-{extra_index}",
                    engagement_id=engagement.id,
                    session_id=chat.id,
                    provider_profile_id=provider.id,
                    model="fixture-model",
                    status=ChatTurnStatus.ROUTING,
                )
            )
        )
    for index in range(args.active):
        (
            await store.create(
                WorkItem(
                    id=f"bench-work-{index:04d}",
                    engagement_id=project.id,
                    title=f"Synthetic work {index:04d}",
                    status="in_progress",
                    assignee_session_id=session_ids[args.interrupted + index],
                )
            )
        )

    started = time.monotonic()
    stamp = datetime(2026, 11, 4, tzinfo=timezone.utc)
    template = ChatTurn(
        id="bench-turn-template",
        engagement_id=project.id,
        session_id=session_ids[0],
        provider_profile_id=provider.id,
        model="fixture-model",
        status=ChatTurnStatus.COMPLETE,
        created_at=stamp,
        updated_at=stamp,
    ).model_dump(mode="json")
    filler = "x" * args.payload_chars
    inserted_bytes = 0
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        rows = []
        for index in range(args.turns):
            status = (
                "interrupted"
                if index < args.interrupted
                else "routing"
                if index < session_count
                else "complete"
            )
            session_id = (
                session_ids[index]
                if index < session_count
                else session_ids[index % session_count]
            )
            payload = dict(template)
            payload["id"] = f"bench-turn-{index:05d}"
            payload["session_id"] = session_id
            payload["status"] = status
            payload["content"] = filler
            if index < args.interrupted:
                recovery = (
                    {"required": True, "cause": "operator"}
                    if index < args.interrupted // 2
                    else {"automatic_retry_pending": True}
                    if index < args.interrupted * 4 // 5
                    else {}
                )
                payload["request_snapshot"] = {"recovery": recovery}
            moment = (stamp + timedelta(seconds=index)).isoformat()
            payload["created_at"] = moment
            payload["updated_at"] = moment
            serialized = json.dumps(payload, separators=(",", ":"))
            inserted_bytes += len(serialized.encode())
            rows.append(
                (
                    payload["id"],
                    "chat_turns",
                    project.id,
                    1,
                    serialized,
                    session_id,
                    moment,
                    moment,
                )
            )
            if len(rows) == 100:
                connection.executemany(
                    "INSERT INTO entities (id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
                connection.commit()
                rows.clear()
        if rows:
            connection.executemany(
                "INSERT INTO entities (id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    expected_results = {
        "http": [
            item.model_dump(mode="json")
            for item in (await WorkService(store).active_agents())
        ],
        "work.agents": [
            item.model_dump(mode="json")
            for item in (await WorkService(store).agent_roster(project.id))
        ],
        "agent.list": (
            await AgentMessageService(store).peers(
                ChatSession(
                    engagement_id=project.id,
                    title="Benchmark gateway",
                    provider_profile_id=provider.id,
                    model="fixture-model",
                    metadata={"allow_agent_messaging": True},
                )
            )
        ),
    }
    _write_json(
        root / "fixture.json",
        {
            "turns": args.turns + 4,
            "large_turns": args.turns,
            "interrupted": args.interrupted,
            "active_routing": args.active + 4,
            "turn_payload_bytes": inserted_bytes,
            "db_bytes": db.stat().st_size,
            "seed_seconds": round(time.monotonic() - started, 2),
            "project_id": project.id,
            "harness_profile_id": harness.id,
            "writer_turn_id": f"bench-turn-{args.interrupted + 1:05d}",
            "expected_digests": {
                name: _digest(rows) for name, rows in expected_results.items()
            },
            "expected_results": expected_results,
        },
    )
    print(
        json.dumps(
            {
                "turns": args.turns + 4,
                "turn_payload_bytes": inserted_bytes,
                "expected_digests": {
                    name: _digest(rows) for name, rows in expected_results.items()
                },
            }
        )
    )


async def _serve(args: argparse.Namespace) -> None:
    import uvicorn
    from nebula.v3.api import create_app
    from nebula.v3.chat import ChatService
    from nebula.v3.chat_agent_messages import AgentMessageService
    from nebula.v3.credentials import CredentialStore
    from nebula.v3.domain import HarnessSession
    from nebula.v3.harnesses import HarnessRuntimeService
    from nebula.v3.mcp import McpGatewaySession
    from nebula.v3.storage import NebulaStore

    store = NebulaStore(args.db)
    fixture = json.loads(args.fixture.read_text())

    # The fixture contains deliberately orphaned unfinished turns. Suppress
    # automatic replay in this inert Core so every read sees the same status
    # snapshot; the real HTTP and MCP handlers remain unchanged.
    async def no_recovery_tick(self: ChatService) -> None:
        return None

    ChatService.recovery_tick = no_recovery_tick
    ChatService.resume_turns_stopped_by_core = lambda self, candidates=None: []
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: args.db.parent,
        adapter_factory=lambda _: None,
    )
    runtime.bind_agent_messages(AgentMessageService(store))
    app = create_app(
        store,
        auth_token=args.token,
        static_dir=args.static_dir,
        harness_runtime_service=runtime,
        execution_data_root=args.db.parent,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level="error",
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        if server_task.done():
            await server_task
            raise RuntimeError("Core stopped before startup completed")
        await asyncio.sleep(0.1)
    # Startup interrupts orphaned synthetic routing turns. Restore those once
    # before accepting benchmark calls; the inert provider is never called.
    from nebula.v3.domain import ChatTurn, ChatTurnStatus

    for index in range(
        fixture["interrupted"], fixture["interrupted"] + fixture["active_routing"] - 4
    ):
        (
            await store.update(
                ChatTurn,
                f"bench-turn-{index:05d}",
                {"status": ChatTurnStatus.ROUTING, "request_snapshot": {}},
            )
        )
    for index in range(4):
        (
            await store.update(
                ChatTurn,
                f"bench-extra-turn-{index}",
                {"status": ChatTurnStatus.ROUTING, "request_snapshot": {}},
            )
        )
    gateways: list[McpGatewaySession] = []
    gateway_details = []
    for _ in range(args.clients):
        chat, _, turn = await runtime.prepare_chat(
            engagement_id=fixture["project_id"],
            profile_id=fixture["harness_profile_id"],
            model=None,
            prompt="Synthetic gateway lookup",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            title="Benchmark gateway",
        )
        session = await store.get(HarnessSession, turn.harness_session_id)
        runtime._active[session.id] = SimpleNamespace(
            turn_id=turn.id, connection=None, task=None
        )
        gateway = McpGatewaySession(
            list_tools=lambda params, session=session, turn=turn: (
                runtime._gateway_catalog(session, params, turn=turn)
            ),
            call_tool=lambda name, arguments, session=session: runtime._gateway_call(
                session, name, arguments
            ),
        )
        launch = await gateway.start()
        gateways.append(gateway)
        gateway_details.append(
            {
                "socket": str(launch.socket_path),
                "gateway_token": launch.token,
                "gateway_chat_session_id": chat.id,
            }
        )
    _write_json(
        args.ready,
        {
            "pid": os.getpid(),
            "origin": f"http://{args.host}:{args.port}",
            "gateways": gateway_details,
        },
    )

    last_heartbeat = [time.monotonic()]
    stop_lag = threading.Event()

    async def heartbeat() -> None:
        while True:
            last_heartbeat[0] = time.monotonic()
            await asyncio.sleep(0.02)

    def sample_lag() -> None:
        # Sample outside the event loop. An in-loop timer cannot report a stall
        # that lasts until the process is stopped at the end of a load phase.
        with args.lag.open("w", buffering=1) as output:
            while not stop_lag.is_set():
                now = time.monotonic()
                age = max(0.0, now - last_heartbeat[0] - 0.02)
                output.write(f"{now:.6f},{age * 1000:.3f}\n")
                stop_lag.wait(0.02)

    lag_task = asyncio.create_task(heartbeat())
    lag_thread = threading.Thread(target=sample_lag, daemon=True)
    lag_thread.start()
    try:
        await server_task
    finally:
        stop_lag.set()
        lag_thread.join(timeout=2)
        lag_task.cancel()
        await asyncio.gather(lag_task, return_exceptions=True)
        await asyncio.gather(*(gateway.close() for gateway in gateways))


class _MultiplexGateway:
    """One authenticated MCP connection for one simulated client."""

    def __init__(self, socket: Path, token: str) -> None:
        self.socket = socket
        self.token = token
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.next_id = 1
        self.write_lock = asyncio.Lock()
        self.read_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(
            str(self.socket),
            limit=4 * 1024 * 1024 + 1,
        )
        self.writer.write(
            json.dumps(
                {"id": 0, "method": "authenticate", "token": self.token}
            ).encode()
            + b"\n"
        )
        await self.writer.drain()
        hello = json.loads(await self.reader.readline())
        if hello.get("result", {}).get("authenticated") is not True:
            raise RuntimeError(f"MCP gateway authentication failed: {hello}")
        self.read_task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.reader is not None
        try:
            while line := await self.reader.readline():
                response = json.loads(line)
                future = self.pending.pop(response.get("id"), None)
                if future is not None and not future.done():
                    if "error" in response:
                        future.set_exception(RuntimeError(str(response["error"])))
                    else:
                        future.set_result(response.get("result"))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("MCP gateway disconnected"))
            self.pending.clear()

    async def call(self, name: str, timeout: float) -> Any:
        assert self.writer is not None
        async with self.write_lock:
            request_id = self.next_id
            self.next_id += 1
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            self.writer.write(
                json.dumps(
                    {
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": {}},
                    }
                ).encode()
                + b"\n"
            )
            await self.writer.drain()
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        finally:
            self.pending.pop(request_id, None)

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
        if self.read_task is not None:
            self.read_task.cancel()
            await asyncio.gather(self.read_task, return_exceptions=True)


def _process_stats(pid: int) -> dict[str, int | float]:
    fields = Path(f"/proc/{pid}/stat").read_text().split()
    ticks = os.sysconf("SC_CLK_TCK")
    io = {}
    for line in Path(f"/proc/{pid}/io").read_text().splitlines():
        key, value = line.split(": ", 1)
        io[key] = int(value)
    return {
        "cpu_seconds": (int(fields[13]) + int(fields[14])) / ticks,
        "rss_bytes": int(fields[23]) * os.sysconf("SC_PAGE_SIZE"),
        **{key: io[key] for key in ("rchar", "wchar", "read_bytes", "write_bytes")},
    }


def _lag_stats(path: Path, start: float, end: float) -> dict[str, Any]:
    values = []
    if path.exists():
        for line in path.read_text().splitlines():
            timestamp, lag = line.split(",", 1)
            if start <= float(timestamp) <= end:
                values.append(float(lag))
    return {"samples": len(values), "ms": _percentiles(values)}


def _probe(args: argparse.Namespace) -> None:
    """Measure unrelated routes in a separate process from the load driver."""

    import httpx

    deadline = time.monotonic() + args.duration
    results: dict[str, dict[str, Any]] = {}

    def sample(name: str) -> None:
        latencies: list[float] = []
        timeouts = 0
        errors: list[str] = []
        with httpx.Client(
            base_url=args.origin,
            headers={"Authorization": f"Bearer {args.token}"},
            timeout=args.timeout,
            trust_env=False,
        ) as client:
            while time.monotonic() < deadline:
                begun = time.monotonic()
                try:
                    response = client.get("/api/v1/health" if name == "health" else "/")
                    response.raise_for_status()
                    if name == "health" and response.json().get("status") not in {
                        "ok",
                        "degraded",
                    }:
                        raise RuntimeError("health response has no status")
                    if name == "shell" and "<html" not in response.text.lower():
                        raise RuntimeError("app shell is not HTML")
                    latencies.append((time.monotonic() - begun) * 1000)
                except httpx.TimeoutException:
                    timeouts += 1
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
                remaining = 0.25 - (time.monotonic() - begun)
                if remaining > 0:
                    time.sleep(remaining)
        results[name] = {
            "success": len(latencies),
            "timeouts": timeouts,
            "errors": errors[:10],
            "mismatches": 0,
            "throughput_per_second": round(len(latencies) / args.duration, 3),
            "latency_ms": _percentiles(latencies),
        }

    threads = [
        threading.Thread(target=sample, args=(name,)) for name in ("health", "shell")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _write_json(args.output, results)


async def _load(args: argparse.Namespace) -> None:
    import httpx
    from nebula.v3.domain import ChatTurn
    from nebula.v3.storage import NebulaStore

    ready = json.loads(args.ready.read_text())
    fixture = json.loads(args.fixture.read_text())
    gateways = [
        _MultiplexGateway(Path(detail["socket"]), detail["gateway_token"])
        for detail in ready["gateways"]
    ]
    await asyncio.gather(*(gateway.start() for gateway in gateways))
    paths = (args.path,)
    records: dict[str, list[float]] = {name: [] for name in paths}
    timeouts = {name: 0 for name in records}
    errors: dict[str, list[str]] = {name: [] for name in records}
    mismatches = {name: 0 for name in paths}
    mismatch_examples: dict[str, list[dict[str, Any]]] = {name: [] for name in paths}
    write_counts = {"turn_updates": 0, "events": 0, "errors": []}
    stop_writes = threading.Event()
    probe_output = Path(str(args.output) + ".probe")
    probe_output.unlink(missing_ok=True)
    probe_process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "probe",
            "--origin",
            ready["origin"],
            "--token",
            args.token,
            "--duration",
            str(args.duration),
            "--timeout",
            str(args.timeout),
            "--output",
            str(probe_output),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started = time.monotonic()
    deadline = started + args.duration
    before = _process_stats(ready["pid"])

    async def update_turns() -> None:
        store = NebulaStore(args.db)
        iteration = 0
        while not stop_writes.is_set() and time.monotonic() < deadline:
            try:
                (
                    await store.update(
                        ChatTurn,
                        fixture["writer_turn_id"],
                        {"error": f"synthetic update {iteration}"},
                    )
                )
                write_counts["turn_updates"] += 1
            except Exception as exc:
                write_counts["errors"].append(f"turn: {type(exc).__name__}: {exc}")
            iteration += 1
            stop_writes.wait(0.5)

    async def append_events() -> None:
        store = NebulaStore(args.db)
        iteration = 0
        while not stop_writes.is_set() and time.monotonic() < deadline:
            try:
                (
                    await store.append_event(
                        "synthetic-benchmark-stream", "benchmark.tick", {"n": iteration}
                    )
                )
                write_counts["events"] += 1
            except Exception as exc:
                write_counts["errors"].append(f"event: {type(exc).__name__}: {exc}")
            iteration += 1
            stop_writes.wait(0.2)

    threads = [
        threading.Thread(target=update_turns),
        threading.Thread(target=append_events),
    ]
    for thread in threads:
        thread.start()

    async with httpx.AsyncClient(
        base_url=ready["origin"],
        headers={"Authorization": f"Bearer {args.token}"},
        timeout=args.timeout,
        trust_env=False,
        limits=httpx.Limits(max_connections=256),
    ) as client:

        async def request(name: str, client_index: int = 0) -> None:
            begun = time.monotonic()
            try:
                if name == "http":
                    response = await client.get("/api/v1/work/agents")
                    response.raise_for_status()
                    value = response.json()
                elif name in {"work.agents", "agent.list"}:
                    response = await gateways[client_index].call(name, args.timeout)
                    if response.get("isError"):
                        raise RuntimeError(
                            f"{name} returned an MCP error: {response.get('content')}"
                        )
                    value = response["structuredContent"]["agents"]
                records[name].append((time.monotonic() - begun) * 1000)
                normalized = [
                    row for row in value if row.get("title") != "Benchmark gateway"
                ]
                if _digest(normalized) != fixture["expected_digests"][name]:
                    mismatches[name] += 1
                    if len(mismatch_examples[name]) < 3:
                        expected = fixture["expected_results"][name]
                        first = next(
                            (
                                index
                                for index, (actual, wanted) in enumerate(
                                    zip(normalized, expected)
                                )
                                if actual != wanted
                            ),
                            min(len(normalized), len(expected)),
                        )
                        mismatch_examples[name].append(
                            {
                                "actual_count": len(normalized),
                                "expected_count": len(expected),
                                "first_difference": first,
                                "actual": normalized[first]
                                if first < len(normalized)
                                else None,
                                "expected": expected[first]
                                if first < len(expected)
                                else None,
                            }
                        )
            except (asyncio.TimeoutError, httpx.TimeoutException):
                timeouts[name] += 1
            except Exception as exc:
                errors[name].append(f"{type(exc).__name__}: {str(exc)[:200]}")

        async def reader(index: int) -> None:
            while time.monotonic() < deadline:
                await asyncio.gather(*(request(name, index) for name in paths))
                if time.monotonic() < deadline:
                    await asyncio.sleep(2)

        await asyncio.gather(*(reader(index) for index in range(args.clients)))
    stop_writes.set()
    for thread in threads:
        thread.join(timeout=10)
    await asyncio.gather(*(gateway.close() for gateway in gateways))
    probe_process.wait(timeout=args.duration + args.timeout + 10)
    if probe_process.returncode or not probe_output.exists():
        raise RuntimeError("independent health and shell probe failed")
    probes = json.loads(probe_output.read_text())
    probe_output.unlink()
    ended = time.monotonic()
    after = _process_stats(ready["pid"])
    delta = {
        key: round(after[key] - before[key], 3)
        for key in ("cpu_seconds", "rchar", "wchar", "read_bytes", "write_bytes")
    }
    _write_json(
        args.output,
        {
            "clients_per_path": args.clients,
            "reader_path": args.path,
            "simultaneous_reader_tasks": args.clients,
            "mcp_gateway_sessions": args.clients,
            "duration_seconds": args.duration,
            "wall_seconds": round(ended - started, 2),
            "request_timeout_seconds": args.timeout,
            "think_seconds": 2,
            "paths": {
                **{
                    name: {
                        "success": len(values),
                        "timeouts": timeouts[name],
                        "errors": errors[name][:10],
                        "mismatches": mismatches.get(name, 0),
                        "mismatch_examples": mismatch_examples.get(name, []),
                        "throughput_per_second": round(
                            len(values) / (ended - started), 3
                        ),
                        "latency_ms": _percentiles(values),
                    }
                    for name, values in records.items()
                },
                **probes,
            },
            "event_loop_lag": _lag_stats(args.lag, started, ended),
            "core_process": {"delta": delta, "rss_bytes_end": after["rss_bytes"]},
            "writers": write_counts,
        },
    )


def _query_plans(db: Path, project_id: str, *, indexed: bool) -> dict[str, list[str]]:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        old_where = "kind='chat_turns' AND json_extract(payload, '$.status') IN ('routing','interrupted')"
        new_where = (
            "kind='chat_turns' AND chat_turn_status IN ('routing','interrupted')"
        )
        columns = (
            "id,engagement_id,created_at,chat_session_id,chat_turn_status,"
            "chat_turn_recovery_visible,chat_turn_core_resume_pending"
            if indexed
            else "*"
        )
        where = new_where if indexed else old_where
        return {
            scope: [
                row[3]
                for row in connection.execute(
                    f"EXPLAIN QUERY PLAN SELECT {columns} FROM entities WHERE {where} "
                    + ("AND engagement_id=? " if scope == "project" else "")
                    + ("" if indexed else "ORDER BY created_at,id"),
                    (project_id,) if scope == "project" else (),
                )
            ]
            for scope in ("global", "project")
        }
    finally:
        connection.close()


async def _migrate(args: argparse.Namespace) -> None:
    from nebula.v3.storage import NebulaStore

    started = time.monotonic()
    store = NebulaStore(args.db)
    (await store.database.engine.dispose())
    print(json.dumps({"migration_seconds": round(time.monotonic() - started, 2)}))


def _copy_db(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "--reflink=auto", str(source), str(target)], check=True)


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((host, 0))
        return listener.getsockname()[1]


def _command(
    script: Path,
    interpreter: Path,
    source: Path,
    *arguments: str,
    timeout: float,
) -> str:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source)
    environment["NEBULA_V3_KNOWLEDGE_RERANKER"] = "off"
    result = subprocess.run(
        [str(interpreter), str(script), *arguments],
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"{' '.join(arguments[:2])} failed ({result.returncode}): "
            f"{result.stderr[-2000:]} {result.stdout[-1000:]}"
        )
    return result.stdout


def _candidate_identity(repo: Path) -> dict[str, str | None]:
    source_paths = (
        "src/nebula/v3/api.py",
        "src/nebula/v3/execution_kernel.py",
        "src/nebula/v3/authentication.py",
        "src/nebula/v3/memory_budget.py",
        "src/nebula/v3/runtime_memory.py",
        "src/nebula/v3/chat_agent_messages.py",
        "src/nebula/v3/database.py",
        "src/nebula/v3/harnesses.py",
        "src/nebula/v3/mcp.py",
        "src/nebula/v3/migrations/versions/0027_chat_turn_status_lookup.py",
        "src/nebula/v3/storage.py",
        "src/nebula/v3/work.py",
    )
    digest = hashlib.sha256()
    for name in source_paths:
        digest.update(name.encode() + b"\0")
        digest.update((repo / name).read_bytes())
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    dirty = (
        subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", *source_paths], cwd=repo
        ).returncode
        != 0
    )
    return {
        "candidate_commit": None if dirty else head,
        "candidate_base_commit": head,
        "candidate_source_sha256": digest.hexdigest(),
    }


def _run(args: argparse.Namespace) -> None:
    address = ipaddress.ip_address(args.host)
    if address.is_loopback or address.is_unspecified or address.version != 4:
        raise ValueError("--host must be a non-loopback IPv4 LAN address")
    if args.turns < 17_000 or args.interrupted < 500 or args.active < 60:
        raise ValueError(
            "fixture must contain at least 17,000 turns, 500 interrupted, and 60 routing"
        )
    if not (args.static_dir / "index.html").is_file():
        raise ValueError("--static-dir must be a built UI bundle")
    script = Path(__file__).resolve()
    repo = script.parents[2]
    interpreter = repo / ".venv" / "bin" / "python"
    root = Path(tempfile.mkdtemp(prefix="nebula-chat-turn-status-"))
    root.chmod(0o700)
    old_checkout = root / "old-checkout"
    receipt: dict[str, Any] = {
        "fixture_only": True,
        "baseline_commit": args.baseline,
        **_candidate_identity(repo),
        "lan_host": args.host,
        "ui_index_sha256": hashlib.sha256(
            (args.static_dir / "index.html").read_bytes()
        ).hexdigest(),
        "configuration": {
            "turns": args.turns,
            "payload_chars": args.payload_chars,
            "interrupted": args.interrupted,
            "routing": args.active,
            "clients_per_path": [1, 16, 32, 64],
            "mcp_gateway_sessions_per_client": 1,
            "reader_paths_run_separately": True,
            "health_and_shell_probe_process": "independent of the load driver",
            "duration_seconds": args.duration,
            "request_timeout_seconds": args.timeout,
            "think_seconds": 2,
            "turn_update_interval_seconds": 0.5,
            "event_write_interval_seconds": 0.2,
            "health_and_shell_probe_interval_seconds": 0.25,
            "fixture_recovery_runner": "paused to keep orphaned synthetic turns stable; active routing turns restored after startup",
        },
        "phases": {},
    }
    server_process: subprocess.Popen[str] | None = None
    worktree_added = False
    try:
        print("Creating isolated baseline checkout", flush=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(old_checkout), args.baseline],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        worktree_added = True
        sources = {"old": old_checkout / "src", "new": repo / "src"}
        old_base = root / "old-base"
        old_base.mkdir()
        print("Seeding synthetic turn payloads", flush=True)
        _command(
            script,
            interpreter,
            sources["old"],
            "seed",
            "--root",
            str(old_base),
            "--turns",
            str(args.turns),
            "--interrupted",
            str(args.interrupted),
            "--active",
            str(args.active),
            "--payload-chars",
            str(args.payload_chars),
            timeout=600,
        )
        fixture_file = old_base / "fixture.json"
        fixture = json.loads(fixture_file.read_text())
        receipt["fixture"] = {
            **{
                key: value
                for key, value in fixture.items()
                if key != "expected_results"
            },
            "expected_result_counts": {
                name: len(rows) for name, rows in fixture["expected_results"].items()
            },
        }
        new_base = root / "new-base"
        new_base.mkdir()
        _copy_db(old_base / "nebula.db", new_base / "nebula.db")
        print("Backfilling candidate status index", flush=True)
        receipt["migration"] = json.loads(
            _command(
                script,
                interpreter,
                sources["new"],
                "migrate",
                "--db",
                str(new_base / "nebula.db"),
                timeout=600,
            )
        )
        receipt["plans"] = {
            "old": _query_plans(
                old_base / "nebula.db", fixture["project_id"], indexed=False
            ),
            "new": _query_plans(
                new_base / "nebula.db", fixture["project_id"], indexed=True
            ),
        }
        for build in ("old", "new"):
            receipt["phases"][build] = {}
            base = old_base if build == "old" else new_base
            for clients, path_name in (
                (clients, path_name)
                for clients in (1, 16, 32, 64)
                for path_name in ("http", "work.agents", "agent.list")
            ):
                print(f"Load {build} Core: {path_name}, {clients} clients", flush=True)
                run_root = root / "run"
                run_root.mkdir(exist_ok=True)
                run_db = run_root / "nebula.db"
                _copy_db(base / "nebula.db", run_db)
                ready = root / "ready.json"
                lag = root / "lag.csv"
                output = root / "phase.json"
                for path in (ready, lag, output):
                    path.unlink(missing_ok=True)
                token = hashlib.sha256(os.urandom(32)).hexdigest()
                port = _free_port(args.host)
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(sources[build])
                environment["NEBULA_V3_KNOWLEDGE_RERANKER"] = "off"
                log_name = f"{build}-{clients}-{path_name.replace('.', '-')}.log"
                log = (root / log_name).open("w")
                server_process = subprocess.Popen(
                    [
                        str(interpreter),
                        "-u",
                        str(script),
                        "serve",
                        "--db",
                        str(run_db),
                        "--fixture",
                        str(fixture_file),
                        "--static-dir",
                        str(args.static_dir),
                        "--host",
                        args.host,
                        "--port",
                        str(port),
                        "--clients",
                        str(clients),
                        "--token",
                        token,
                        "--ready",
                        str(ready),
                        "--lag",
                        str(lag),
                    ],
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                try:
                    start_wait = time.monotonic()
                    while not ready.exists():
                        if server_process.poll() is not None:
                            raise RuntimeError(
                                f"Core startup failed: {(root / log_name).read_text()[-2500:]}"
                            )
                        if time.monotonic() - start_wait > 180:
                            raise TimeoutError("Core startup exceeded 180 seconds")
                        time.sleep(0.1)
                    _command(
                        script,
                        interpreter,
                        sources[build],
                        "load",
                        "--db",
                        str(run_db),
                        "--fixture",
                        str(fixture_file),
                        "--ready",
                        str(ready),
                        "--lag",
                        str(lag),
                        "--output",
                        str(output),
                        "--token",
                        token,
                        "--clients",
                        str(clients),
                        "--path",
                        path_name,
                        "--duration",
                        str(args.duration),
                        "--timeout",
                        str(args.timeout),
                        timeout=args.duration + args.timeout + 90,
                    )
                    phase = json.loads(output.read_text())
                    receipt["phases"][build].setdefault(str(clients), {})[path_name] = (
                        phase
                    )
                    if build == "new":
                        failures = {
                            name: {
                                "success": path["success"],
                                "timeouts": path["timeouts"],
                                "errors": path["errors"][:2],
                                "mismatches": path["mismatches"],
                            }
                            for name, path in phase["paths"].items()
                            if not path["success"]
                            or path["timeouts"]
                            or path["errors"]
                            or path["mismatches"]
                        }
                        if failures:
                            raise RuntimeError(
                                f"candidate {path_name} load failed at {clients} clients: {failures}"
                            )
                finally:
                    server_process.send_signal(signal.SIGTERM)
                    try:
                        server_process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        server_process.kill()
                        server_process.wait(timeout=5)
                    server_process = None
                    log.close()
                    if ready.exists():
                        for detail in json.loads(ready.read_text())["gateways"]:
                            gateway_root = Path(detail["socket"]).parent
                            if gateway_root.name.startswith("nebula-mcp-gateway-"):
                                shutil.rmtree(gateway_root, ignore_errors=True)
                    for path in (
                        run_db,
                        Path(str(run_db) + "-wal"),
                        Path(str(run_db) + "-shm"),
                    ):
                        path.unlink(missing_ok=True)
        receipt["fixture_only"] = False
        _write_json(args.receipt, receipt)
        print(f"Receipt: {args.receipt}", flush=True)
    except BaseException as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        _write_json(args.receipt, receipt)
        raise
    finally:
        if server_process is not None:
            server_process.kill()
            server_process.wait(timeout=5)
        if worktree_added:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(old_checkout)],
                cwd=repo,
                check=False,
                capture_output=True,
            )
        shutil.rmtree(root, ignore_errors=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed")
    seed.add_argument("--root", type=Path, required=True)
    seed.add_argument("--turns", type=int, required=True)
    seed.add_argument("--interrupted", type=int, required=True)
    seed.add_argument("--active", type=int, required=True)
    seed.add_argument("--payload-chars", type=int, required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--db", type=Path, required=True)
    serve.add_argument("--fixture", type=Path, required=True)
    serve.add_argument("--static-dir", type=Path, required=True)
    serve.add_argument("--host", required=True)
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--clients", type=int, required=True)
    serve.add_argument("--token", required=True)
    serve.add_argument("--ready", type=Path, required=True)
    serve.add_argument("--lag", type=Path, required=True)
    load = commands.add_parser("load")
    load.add_argument("--db", type=Path, required=True)
    load.add_argument("--fixture", type=Path, required=True)
    load.add_argument("--ready", type=Path, required=True)
    load.add_argument("--lag", type=Path, required=True)
    load.add_argument("--output", type=Path, required=True)
    load.add_argument("--token", required=True)
    load.add_argument("--clients", type=int, required=True)
    load.add_argument(
        "--path", choices=("http", "work.agents", "agent.list"), required=True
    )
    load.add_argument("--duration", type=float, required=True)
    load.add_argument("--timeout", type=float, required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--origin", required=True)
    probe.add_argument("--token", required=True)
    probe.add_argument("--duration", type=float, required=True)
    probe.add_argument("--timeout", type=float, required=True)
    probe.add_argument("--output", type=Path, required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--db", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--baseline", default="884d862")
    run.add_argument("--host", required=True)
    run.add_argument("--static-dir", type=Path, required=True)
    run.add_argument("--receipt", type=Path, required=True)
    run.add_argument("--turns", type=int, default=17_000)
    run.add_argument("--interrupted", type=int, default=500)
    run.add_argument("--active", type=int, default=60)
    run.add_argument("--payload-chars", type=int, default=120_000)
    run.add_argument("--duration", type=float, default=12)
    run.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    if args.command == "seed":
        (await _seed(args))
    elif args.command == "serve":
        (await _serve(args))
    elif args.command == "load":
        (await _load(args))
    elif args.command == "probe":
        _probe(args)
    elif args.command == "migrate":
        (await _migrate(args))
    else:
        _run(args)


if __name__ == "__main__":
    asyncio.run(main())
