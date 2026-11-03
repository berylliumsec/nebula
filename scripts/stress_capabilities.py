"""Synthetic saturation study of an owned, disposable, paired Nebula Core.

Never accepts a destination URL or existing data directory. Workloads only read
and save synthetic records/files; no security tools, models or commands execute.
Reports distinguish exercised API boundaries from untested runtime capabilities.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import resource
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import zipfile
from io import BytesIO

import httpx
from fastapi import HTTPException, Request


def client_limits(capacity: int = 100) -> httpx.Limits:
    # Equal client/server idle limits reproduce an EOF-before-headers race on
    # reused HTTP/1.1 sockets. Retire ours before Uvicorn's default five seconds;
    # do not retry requests or discard any resulting errors.
    return httpx.Limits(
        max_connections=capacity,
        max_keepalive_connections=capacity,
        keepalive_expiry=1.0,
    )


@dataclass(frozen=True)
class Scenario:
    name: str
    path: str
    params: tuple[tuple[str, Any], ...] = ()
    collection: str | None = None
    count: int | None = None


def scenarios(scale: int) -> list[Scenario]:
    project = "/api/v1/engagements/project-0"
    return [
        Scenario("overview-global", "/api/v1/overview"),
        Scenario("overview-project", project + "/overview"),
        Scenario(
            "search",
            "/api/v1/search",
            (("query", "Synthetic"), ("active_project", "project-0"), ("limit", 30)),
            "items",
            30,
        ),
        Scenario("work-global", "/api/v1/work/items", (("limit", 100),), count=100),
        Scenario("work-project", project + "/work", count=min(500, 200 * scale)),
        *[
            Scenario(
                kind,
                "/api/v1/" + kind,
                (("engagement_id", "project-0"), ("limit", 100)),
                count=min(100, count * scale),
            )
            for kind, count in (
                ("observations", 1000),
                ("assets", 200),
                ("findings", 100),
                ("reports", 10),
                ("artifacts", 4),
                ("chat-sessions", 10),
            )
        ],
        Scenario("note-detail", "/api/v1/observations/note-0-0"),
        Scenario(
            "chat-history",
            "/api/v1/chat/sessions/chat-0-0/messages",
            (("limit", 50), ("include_replaced", "true")),
            count=50,
        ),
        Scenario("chat-state", "/api/v1/chat/sessions/chat-0-0/state"),
        Scenario(
            "chat-activity",
            "/api/v1/chat/session-activity",
            (("engagement_id", "project-0"),),
        ),
        Scenario("runtime-usage", "/api/v1/runtime-usage"),
        Scenario(
            "event-replay",
            "/api/v1/runs/run-0/events",
            (("limit", 100),),
            "events",
            100,
        ),
        Scenario(
            "browser-history",
            project + "/browser-workspace",
            collection="sessions",
            count=10 * scale,
        ),
        Scenario(
            "workspace-list", project + "/workspace", (("limit", 100),), "entries", 100
        ),
        Scenario(
            "workspace-search",
            project + "/workspace/search",
            (("query", "synthetic"), ("mode", "text"), ("limit", 20)),
        ),
        Scenario(
            "workspace-preview",
            project + "/workspace/preview",
            (("path", "synthetic-00000.txt"),),
        ),
        Scenario("notebook-list", project + "/notebooks"),
        Scenario("artifact-download", "/api/v1/artifacts/blob-0-0/content"),
        Scenario("record-lifecycle", "/api/v1/observations"),
        Scenario("workspace-upload", project + "/workspace/file"),
        Scenario("bundle-export", project + "/export-bundle"),
    ]


def percentile(values: list[float], percent: int) -> float | None:
    if not values:
        return None
    return round(sorted(values)[max(0, math.ceil(len(values) * percent / 100) - 1)], 3)


def seed(data: Path, scale: int):
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.domain import (
        AgentRun,
        Artifact,
        Asset,
        BrowserIdentity,
        BrowserSession,
        ChatMessage,
        ChatSession,
        Engagement,
        Finding,
        Observation,
        ProviderProfile,
        Report,
        WorkItem,
    )
    from nebula.v3.storage import NebulaStore
    from nebula.v3.workspace import WorkspaceService

    store = NebulaStore(data / "nebula.db")
    artifacts = ArtifactStore(data / "artifacts")

    class SyntheticWorkspace:
        def workspace_for(self, engagement_id):
            root = data / "workspaces" / engagement_id
            root.mkdir(parents=True, exist_ok=True)
            return root

    workspace = WorkspaceService(
        store=store,
        artifact_store=artifacts,
        tool_platform=SyntheticWorkspace(),
        operator_id=lambda: "synthetic-operator",
    )
    counts: Counter[str] = Counter()

    def save(items):
        for start in range(0, len(items), 250):
            batch = items[start : start + 250]
            store.create_many(batch)
            counts.update(item.entity_kind for item in batch)

    save(
        [
            ProviderProfile(
                id="inert-provider",
                name="Synthetic disabled provider",
                provider_type="vllm",
                is_local=True,
                enabled=False,
                model_allowlist=["synthetic"],
            )
        ]
    )
    for project_index in range(3):
        project = f"project-{project_index}"
        root = workspace.tool_platform.workspace_for(project)
        save(
            [
                Engagement(
                    id=project,
                    name=f"Synthetic project {project_index}",
                    workspace_path=str(root),
                )
            ]
        )
        save(
            [
                Observation(
                    id=f"note-{project_index}-{i}",
                    engagement_id=project,
                    observation_type="note",
                    title=f"Synthetic note {i:06}",
                    body="Synthetic note λ. " * 100,
                )
                for i in range(1000 * scale)
            ]
        )
        save(
            [
                WorkItem(
                    id=f"work-{project_index}-{i}",
                    engagement_id=project,
                    title=f"Synthetic work {i:06}",
                    description="Synthetic progress. " * 40,
                )
                for i in range(200 * scale)
            ]
        )
        save(
            [
                Asset(
                    id=f"asset-{project_index}-{i}",
                    engagement_id=project,
                    name=f"Synthetic asset {i}",
                    address=f"asset-{i}.invalid",
                )
                for i in range(200 * scale)
            ]
        )
        save(
            [
                Finding(
                    id=f"finding-{project_index}-{i}",
                    engagement_id=project,
                    title=f"Synthetic finding {i}",
                )
                for i in range(100 * scale)
            ]
        )
        save(
            [
                Report(
                    id=f"report-{project_index}-{i}",
                    engagement_id=project,
                    title=f"Synthetic report {i}",
                    executive_summary="Synthetic summary. " * 50,
                    observation_ids=[f"note-{project_index}-{j}" for j in range(20)],
                )
                for i in range(10 * scale)
            ]
        )
        save(
            [
                ChatSession(
                    id=f"chat-{project_index}-{i}",
                    engagement_id=project,
                    title=f"Synthetic chat {i}",
                    provider_profile_id="inert-provider",
                    model="synthetic",
                )
                for i in range(10 * scale)
            ]
        )
        for chat in range(10 * scale):
            save(
                [
                    ChatMessage(
                        id=f"message-{project_index}-{chat}-{i}",
                        engagement_id=project,
                        session_id=f"chat-{project_index}-{chat}",
                        sequence=i + 1,
                        role="user" if i % 2 == 0 else "assistant",
                        content="Synthetic transcript λ. " * 80,
                    )
                    for i in range(200)
                ]
            )
        save(
            [
                BrowserIdentity(
                    id=f"identity-{project_index}",
                    engagement_id=project,
                    name="Synthetic identity",
                )
            ]
        )
        save(
            [
                BrowserSession(
                    id=f"browser-{project_index}-{i}",
                    engagement_id=project,
                    name=f"Synthetic browser {i}",
                    identity_id=f"identity-{project_index}",
                    status="closed",
                )
                for i in range(10 * scale)
            ]
        )
        save(
            [
                AgentRun(
                    id=f"run-{project_index}",
                    engagement_id=project,
                    objective="Synthetic event history",
                    status="complete",
                )
            ]
        )
        for i in range(100 * scale):
            store.append_event(
                f"run-{project_index}",
                "synthetic.progress",
                {"step": i},
                idempotency_key=f"seed-{i}",
            )
        blobs: list[Artifact] = []
        for i in range(4 * scale):
            artifact = artifacts.put_bytes(
                (f"Synthetic blob {project_index}-{i} λ.\n" * 60_000).encode(),
                engagement_id=project,
                filename=f"synthetic-{i}.txt",
                media_type="text/plain",
            )
            blobs.append(
                artifact.model_copy(update={"id": f"blob-{project_index}-{i}"})
            )
        save(blobs)
        root = workspace.tool_platform.workspace_for(project)
        for i in range(200 * scale):
            (root / f"synthetic-{i:05}.txt").write_text(f"synthetic file {i} λ\n" * 20)
        (root / "synthetic.ipynb").write_text(
            json.dumps(
                {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": []}
            )
        )
    return store, artifacts, workspace, dict(counts)


def serve(data: Path, port: int, scale: int):
    import uvicorn
    from keyring.backends.null import Keyring
    from sqlalchemy import event

    from nebula.v3.api import create_app
    from nebula.v3.credentials import CredentialStore

    if not data.is_dir() or any(data.iterdir()):
        raise ValueError("Core requires a newly created empty temporary directory")
    store, artifacts, workspace, counts = seed(data, scale)
    token = os.environ.pop("NEBULA_STRESS_TOKEN")
    app = create_app(
        store,
        artifact_store=artifacts,
        workspace_service=workspace,
        credential_store=CredentialStore(keyring_backend=Keyring()),
        auth_token=token,
        execution_data_root=data,
        allow_insecure_device_pairing=True,
    )
    loop_thread = threading.get_ident()
    lag: list[float] = []
    sql: dict[tuple[bool, str], list[float]] = defaultdict(lambda: [0, 0, 0])
    lock = threading.Lock()

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        context.stress_started = time.perf_counter()

    def after_sql(conn, cursor, statement, parameters, context, executemany):
        # SQL text has placeholders; values and authentication never enter reports.
        key = (threading.get_ident() == loop_thread, statement)
        with lock:
            elapsed = (time.perf_counter() - context.stress_started) * 1000
            values = sql[key]
            values[0] += 1
            values[1] += elapsed
            values[2] = max(values[2], elapsed)

    event.listen(store.database.engine, "before_cursor_execute", before_sql)
    event.listen(store.database.engine, "after_cursor_execute", after_sql)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):

            async def tick():
                while True:
                    start = time.perf_counter()
                    await asyncio.sleep(0.01)
                    lag.append(max(0, (time.perf_counter() - start - 0.01) * 1000))

            timer = asyncio.create_task(tick())
            try:
                yield
            finally:
                timer.cancel()
                with suppress(asyncio.CancelledError):
                    await timer

    app.router.lifespan_context = lifespan

    @app.get("/__stress/metrics")
    async def metrics(request: Request):
        if request.headers.get("Authorization") != f"Bearer {token}":
            raise HTTPException(401)
        with lock:
            statements = [
                {
                    "on_loop": key[0],
                    "sql": key[1],
                    "count": int(values[0]),
                    "total_ms": round(values[1], 3),
                    "max_ms": round(values[2], 3),
                }
                for key, values in sql.items()
            ]
            sql.clear()
        result = {
            "loop_p95_ms": percentile(lag, 95),
            "loop_max_ms": percentile(lag, 100),
            "rss_peak_mib": round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2
            ),
            "cpu_s": time.process_time(),
            "pool_checked_out": store.database.engine.pool.checkedout(),
            "sql_on_loop_count": sum(
                row["count"] for row in statements if row["on_loop"]
            ),
            "sql_on_loop_ms": round(
                sum(row["total_ms"] for row in statements if row["on_loop"]), 3
            ),
            "slow_sql": sorted(
                statements, key=lambda row: row["total_ms"], reverse=True
            )[:8],
            "fixture_counts": counts,
        }
        lag.clear()
        return result

    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False, log_level="warning")


async def execute(client: httpx.AsyncClient, scenario: Scenario, identity: str):
    if scenario.name == "record-lifecycle":
        created = await client.post(
            scenario.path,
            json={
                "engagement_id": "project-0",
                "observation_type": "note",
                "title": f"Synthetic concurrent write {identity} λ",
                "body": "Synthetic record. " * 200,
            },
        )
        created.raise_for_status()
        assert created.status_code == 201
        path = scenario.path + "/" + created.json()["id"]
        revision = created.json()["revision"]
        saved = await client.patch(
            path,
            json={
                "expected_revision": revision,
                "changes": {"title": "Synthetic saved revision λ"},
            },
        )
        saved.raise_for_status()
        assert saved.json()["revision"] == revision + 1
        stale = await client.patch(
            path,
            json={"expected_revision": revision, "changes": {"title": "Stale write"}},
        )
        assert stale.status_code == 409, stale.text[:200]
        observed = await client.get(path)
        observed.raise_for_status()
        assert observed.json()["title"] == "Synthetic saved revision λ"
        removed = await client.delete(path, headers={"If-Match": str(revision + 1)})
        assert removed.status_code == 204, removed.text[:200]
        assert (await client.get(path)).status_code == 404
        return
    if scenario.name == "workspace-upload":
        path = f"upload-{identity}.txt"
        content = (f"Synthetic upload {identity} λ\n" * 1000).encode()
        response = await client.put(
            scenario.path,
            params={"path": path},
            content=content,
            headers={"X-Content-SHA256": hashlib.sha256(content).hexdigest()},
        )
        response.raise_for_status()
        read = await client.get(
            scenario.path.rsplit("/", 1)[0] + "/download", params={"path": path}
        )
        read.raise_for_status()
        assert read.content == content
        removed = await client.delete(
            scenario.path.rsplit("/", 1)[0] + "/entry", params={"path": path}
        )
        removed.raise_for_status()
        return
    if scenario.name == "bundle-export":
        response = await client.post(
            scenario.path, headers={"X-Nebula-Sensitive-Data-Acknowledged": "true"}
        )
        response.raise_for_status()
        with zipfile.ZipFile(BytesIO(response.content)) as bundle:
            assert bundle.testzip() is None
            assert bundle.namelist()
        return
    response = await client.get(scenario.path, params=dict(scenario.params))
    response.raise_for_status()
    if scenario.name == "artifact-download":
        assert response.content.startswith(b"Synthetic blob 0-0 ")
        assert len(response.content) > 1_000_000
        return
    body = response.json()
    if scenario.count is not None:
        items = body[scenario.collection] if scenario.collection else body
        assert len(items) == scenario.count, (
            f"{scenario.name}: expected {scenario.count}, got {len(items)}"
        )
        if (
            isinstance(items, list)
            and items
            and isinstance(items[0], dict)
            and "id" in items[0]
        ):
            assert len({row["id"] for row in items}) == len(items)
    if scenario.name.startswith("overview"):
        assert body["counts"]["observations"] > 0
    if scenario.name == "note-detail":
        assert body["id"] == "note-0-0"
    if scenario.name == "chat-history":
        assert [row["sequence"] for row in body] == list(range(151, 201))
    if scenario.name == "workspace-preview":
        assert body["text"].startswith("synthetic file 0 λ")


async def phase(client, admin, selected, concurrency, waves, duration, label):
    before = (await admin.get("/__stress/metrics")).json()
    latencies: dict[str, list[float]] = defaultdict(list)
    failures: list[dict[str, Any]] = []
    error_count = 0
    probes: list[float] = []
    probe_errors = 0
    done = asyncio.Event()

    async def sentinel():
        nonlocal probe_errors
        async with httpx.AsyncClient(
            base_url=str(admin.base_url),
            headers=admin.headers,
            trust_env=False,
            timeout=30,
            limits=client_limits(),
        ) as probe:
            while not done.is_set():
                start = time.perf_counter()
                try:
                    response = await probe.get(
                        "/api/v1/engagements/project-0/assistant-defaults"
                    )
                    response.raise_for_status()
                except Exception as exc:
                    probe_errors += 1
                    if len(failures) < 20:
                        failures.append(
                            {
                                "scenario": "sentinel",
                                "type": type(exc).__name__,
                                "message": str(exc)[:300],
                            }
                        )
                probes.append((time.perf_counter() - start) * 1000)
                await asyncio.sleep(0.02)

    async def request(index):
        nonlocal error_count
        scenario = selected[index % len(selected)]
        start = time.perf_counter()
        try:
            await execute(client, scenario, f"{label}-{index}")
        except Exception as exc:
            error_count += 1
            if len(failures) < 20:
                failures.append(
                    {
                        "scenario": scenario.name,
                        "type": type(exc).__name__,
                        "message": str(exc)[:300],
                    }
                )
        latencies[scenario.name].append((time.perf_counter() - start) * 1000)

    watcher = asyncio.create_task(sentinel())
    start = time.perf_counter()
    index = 0
    try:
        while index < concurrency * waves or (
            duration > 0 and time.perf_counter() - start < duration
        ):
            await asyncio.gather(
                *(request(i) for i in range(index, index + concurrency))
            )
            index += concurrency
    finally:
        done.set()
        await watcher
    elapsed = time.perf_counter() - start
    after = (await admin.get("/__stress/metrics")).json()
    all_times = [value for values in latencies.values() for value in values]
    return {
        "scenario": label,
        "concurrency": concurrency,
        "operations": len(all_times),
        "error_count": error_count,
        "errors": failures,
        "seconds": round(elapsed, 3),
        "operations_per_second": round(len(all_times) / elapsed, 3),
        "p50_ms": percentile(all_times, 50),
        "p95_ms": percentile(all_times, 95),
        "p99_ms": percentile(all_times, 99),
        "max_ms": percentile(all_times, 100),
        "per_scenario": {
            name: {"operations": len(values), "p95_ms": percentile(values, 95)}
            for name, values in latencies.items()
        },
        "sentinel_count": len(probes),
        "sentinel_errors": probe_errors,
        "sentinel_p95_ms": percentile(probes, 95),
        "sentinel_max_ms": percentile(probes, 100),
        "cpu_delta_s": round(after["cpu_s"] - before["cpu_s"], 3),
        **{
            key: value
            for key, value in after.items()
            if key not in ("cpu_s", "fixture_counts")
        },
    }


async def run(args):
    source = args.source_tree.resolve()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind((args.lan_address, 0))
        port = listener.getsockname()[1]
    origin = f"http://{args.lan_address}:{port}"
    token = secrets.token_urlsafe(32)
    environment = {
        **os.environ,
        "NEBULA_STRESS_TOKEN": token,
        "PYTHONPATH": str(source / "src"),
    }
    selected = [
        item
        for item in scenarios(args.scale)
        if not args.scenario or item.name in args.scenario
    ]
    digest = hashlib.sha256()
    for path in sorted((source / "src/nebula/v3").glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    report: dict[str, Any] = {
        "origin": origin,
        "build": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip(),
        "source_sha256": digest.hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "host": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "configuration": {
            "scale": args.scale,
            "concurrency": args.concurrency,
            "waves": args.waves,
            "soak_seconds": args.soak_seconds,
            "load_model": "closed-loop waves; no fixed-arrival-rate capacity claim",
            "client_keepalive_expiry_s": 1.0,
        },
        "coverage": {
            "exercised": [item.name for item in selected],
            "excluded": [
                "live providers/harnesses and model generation",
                "security tool execution and third-party traffic",
                "container/SSH/VPN processes",
                "voice and physical devices",
                "UI rendering and native shell",
                "Chroma embeddings and PostgreSQL",
                "fixed-arrival-rate load and multi-hour soak",
            ],
        },
        "phases": [],
    }

    def save():
        temporary = output.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output)

    process = None
    data = Path(tempfile.mkdtemp(prefix="nebula-stress-"))
    try:
        with output.with_suffix(".server.log").open("w") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--source-tree",
                    str(source),
                    "--serve",
                    str(data),
                    str(port),
                    "--scale",
                    str(args.scale),
                ],
                cwd=source,
                env=environment,
                stdout=log,
                stderr=log,
            )
            async with httpx.AsyncClient(
                base_url=origin,
                headers={"Authorization": f"Bearer {token}"},
                trust_env=False,
                timeout=60,
                limits=client_limits(),
            ) as admin:
                deadline = time.monotonic() + 180
                while True:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("fixture startup failed; inspect server log")
                    try:
                        response = await admin.get("/__stress/metrics")
                        if response.status_code == 200:
                            report["fixture"] = response.json()["fixture_counts"]
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
                async with httpx.AsyncClient(trust_env=False) as anonymous:
                    report["unauthenticated_status"] = (
                        await anonymous.get(origin + "/api/v1/overview")
                    ).status_code
                    assert report["unauthenticated_status"] == 401
                offer = await admin.post(
                    f"http://127.0.0.1:{port}/api/v1/auth/pairings",
                    json={"name": "Synthetic stress fixture"},
                )
                offer.raise_for_status()
                pairing = offer.json()
                async with httpx.AsyncClient(
                    base_url=origin,
                    trust_env=False,
                    timeout=60,
                    limits=client_limits(max(args.concurrency) + 8),
                ) as client:
                    redeemed = await client.post(
                        "/api/v1/auth/pairings/redeem",
                        json={
                            "secret": pairing["secret"],
                            "confirmation_code": pairing["confirmation_code"],
                            "name": "Synthetic stress fixture",
                        },
                    )
                    redeemed.raise_for_status()
                    report["pairing_status"] = redeemed.status_code
                    client.headers.update(
                        {
                            "Origin": origin,
                            "X-Nebula-CSRF": redeemed.json()["csrf_token"],
                        }
                    )
                    for scenario in selected:
                        # Exports each read the whole database; test them separately
                        # with a bounded fan-out, then include one in the mixed wave.
                        levels = (
                            [1, min(4, max(args.concurrency))]
                            if scenario.name == "bundle-export"
                            else args.concurrency
                        )
                        for concurrency in dict.fromkeys(levels):
                            result = await phase(
                                client,
                                admin,
                                [scenario],
                                concurrency,
                                args.waves,
                                0,
                                f"{scenario.name}-c{concurrency}",
                            )
                            report["phases"].append(result)
                            save()
                            print(
                                json.dumps(
                                    {
                                        key: value
                                        for key, value in result.items()
                                        if key not in ("slow_sql", "per_scenario")
                                    }
                                ),
                                flush=True,
                            )
                    if args.mixed:
                        mixed = [
                            item for item in selected if item.name != "bundle-export"
                        ]
                        result = await phase(
                            client,
                            admin,
                            mixed,
                            max(args.concurrency),
                            max(
                                args.waves,
                                math.ceil(len(mixed) / max(args.concurrency)),
                            ),
                            args.soak_seconds,
                            "mixed-soak",
                        )
                        report["phases"].append(result)
                        save()
                        print(
                            json.dumps(
                                {
                                    key: value
                                    for key, value in result.items()
                                    if key not in ("slow_sql", "per_scenario")
                                }
                            ),
                            flush=True,
                        )
                    recovery = await phase(
                        client,
                        admin,
                        [
                            Scenario(
                                "recovery",
                                "/api/v1/engagements/project-0/assistant-defaults",
                            )
                        ],
                        1,
                        10,
                        0,
                        "recovery",
                    )
                    report["phases"].append(recovery)
                    # A new paired-device connection must still read saved data.
                    async with httpx.AsyncClient(
                        base_url=origin,
                        cookies=client.cookies,
                        trust_env=False,
                        timeout=60,
                    ) as reconnect:
                        await execute(
                            reconnect,
                            Scenario("note-detail", "/api/v1/observations/note-0-0"),
                            "reconnect",
                        )
                    report["reconnect"] = "saved note verified on a fresh connection"
    except BaseException as exc:
        report["fatal_error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        raise
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        shutil.rmtree(data)
        report["cleanup"] = {
            "server_exit": process.returncode if process else None,
            "temporary_data_removed": not data.exists(),
        }
        save()
    if any(item["error_count"] or item["sentinel_errors"] for item in report["phases"]):
        raise RuntimeError("stress operations failed; inspect the JSON receipt")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-tree", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(tempfile.gettempdir()) / "nebula-capability-stress.json",
    )
    parser.add_argument(
        "--lan-address", default=socket.gethostbyname(socket.gethostname())
    )
    parser.add_argument("--scale", type=int, default=1, choices=range(1, 21))
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 16, 64, 256])
    parser.add_argument("--waves", type=int, default=2)
    parser.add_argument("--mixed", action="store_true")
    parser.add_argument("--soak-seconds", type=float, default=0)
    parser.add_argument(
        "--scenario", action="append", choices=[item.name for item in scenarios(1)]
    )
    parser.add_argument("--serve", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if (
        args.waves < 1
        or any(not 1 <= level <= 512 for level in args.concurrency)
        or not 0 <= args.soak_seconds <= 3600
    ):
        parser.error("waves must be positive, concurrency 1..512, soak 0..3600 seconds")
    sys.path.insert(0, str(args.source_tree.resolve() / "src"))
    if args.serve:
        serve(Path(args.serve[0]), int(args.serve[1]), args.scale)
        return
    address = ipaddress.ip_address(args.lan_address)
    if address.version != 4 or address.is_loopback or address.is_unspecified:
        parser.error("--lan-address must be this host's IPv4 test-LAN address")
    args.lan_address = str(address)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
