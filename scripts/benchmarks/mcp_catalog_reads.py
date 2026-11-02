"""Compare real MCP IPC clients and unrelated HTTP on disposable LAN Core.

Run the same script/interpreter with baseline or candidate src on PYTHONPATH.
Only synthetic profiles and inert harness bindings are created; no vendor runs.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace


def summary(values):
    ordered = sorted(values)
    return {
        "count": len(values),
        "median_ms": round(statistics.median(values), 3) if values else 0,
        "p95_ms": round(ordered[min(len(values) - 1, int(len(values) * 0.95))], 3)
        if values
        else 0,
        "max_ms": round(max(values), 3) if values else 0,
    }


def serve(args):
    import uvicorn
    from fastapi import Header, HTTPException
    from sqlalchemy import event
    from nebula.v3.api import create_app
    from nebula.v3 import storage
    from nebula.v3.domain import (
        ChatSession,
        ChatTurn,
        Engagement,
        HarnessProfile,
        HarnessSession,
        HarnessTurn,
        McpCapabilitySnapshot,
        McpServerProfile,
        McpToolSnapshot,
        ProviderProfile,
        utc_now,
    )
    from nebula.v3.mcp import McpGatewaySession, catalog_mcp_profiles
    from nebula.v3.mcp_gateway import GatewayClient
    from nebula.v3.notebook_mcp import NotebookMcpTools

    root = args.directory
    workspace = root / "workspace"
    workspace.mkdir()
    static = root / "static"
    static.mkdir()
    (static / "index.html").write_text(
        "<!doctype html><title>Synthetic Core probe</title>"
    )
    (static / "probe.txt").write_text("synthetic static asset\n")
    store = storage.NebulaStore(root / "core.db")
    project = store.create(
        Engagement(
            id="probe-project",
            name="Synthetic MCP probe",
            workspace_path=str(workspace),
        )
    )
    provider = store.create(
        ProviderProfile(
            id="probe-provider",
            name="Inert provider",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["inert"],
        )
    )
    harness = store.create(
        HarnessProfile(
            id="probe-harness",
            name="Inert harness",
            kind="codex_app_server",
            executable="/bin/true",
            default_model="inert",
            privacy={"local_only": True},
        )
    )
    base = utc_now() - timedelta(days=1)
    notebook = McpServerProfile(
        id="nebula-notebook-fixture",
        name="Notebook",
        transport="builtin",
        enabled=True,
        capabilities=NotebookMcpTools.capabilities(),
        metadata={"built_in": "notebook_v1", "engagement_id": "fixture"},
    )
    external = McpServerProfile(
        id="external",
        name="External",
        transport="streamable_http",
        url="https://fixture.invalid/mcp",
        enabled=True,
        capabilities=McpCapabilitySnapshot(
            checked_at=base, tools=[McpToolSnapshot(name="read", read_only=True)]
        ),
    )
    profiles = []
    for index in range(2535):
        timestamp = base + timedelta(seconds=index)
        values = {"created_at": timestamp, "updated_at": timestamp}
        if index < 8 or index == 2534:
            profiles.append(
                external.model_copy(
                    update={
                        **values,
                        "id": f"external-{index:04d}",
                        "name": f"External{index:04d}",
                    }
                )
            )
        else:
            owner = f"{index:036d}"
            profiles.append(
                notebook.model_copy(
                    update={
                        **values,
                        "id": f"nebula-notebook-{owner}",
                        "metadata": {"built_in": "notebook_v1", "engagement_id": owner},
                    }
                )
            )
    store.create_many(profiles)
    counts, guard = Counter(), threading.Lock()
    loop_thread = threading.get_ident()
    original_hydrate = storage._row_to_entity

    def counted(row, model):
        if model is McpServerProfile:
            with guard:
                counts["profiles_hydrated"] += 1
                counts["profiles_hydrated_on_loop"] += (
                    threading.get_ident() == loop_thread
                )
        return original_hydrate(row, model)

    storage._row_to_entity = counted
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        catalog = catalog_mcp_profiles(store, engagement_id=project.id)
        timings.append((time.perf_counter() - started) * 1000)
    isolation = {
        "latency": summary(timings),
        "profile_ids": [p.id for p in catalog],
        "materialized_per_call": counts["profiles_hydrated"] // 3,
    }
    app = create_app(
        store,
        auth_token=os.environ["NEBULA_MCP_PROBE_TOKEN"],
        static_dir=static,
        execution_data_root=root / "execution",
    )
    # Keep the catch-all static mount after the probe's authenticated route.
    static_mount = app.router.routes.pop()
    assert static_mount.name == "workspace"
    runtime = app.state.harness_runtime_service
    clients, gateways, socket_roots = [], [], []
    lag = []
    measuring = False

    @event.listens_for(store.database.engine, "before_cursor_execute")
    def delayed_read(conn, cursor, statement, parameters, context, executemany):
        if (
            measuring
            and args.slow_read_ms
            and statement.startswith("SELECT")
            and "mcp_servers" in parameters
        ):
            time.sleep(args.slow_read_ms / 1000)

    def authenticate(authorization):
        if not secrets.compare_digest(
            authorization, "Bearer " + os.environ["NEBULA_MCP_PROBE_TOKEN"]
        ):
            raise HTTPException(401)

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        nonlocal measuring
        async with original_lifespan(app):
            try:
                # Seed bindings after recovery. Dispatch uses the production
                # catalog, guards and receipts; the vendor remains inert.
                setting = {"provider_profile_id": provider.id, "model": "inert"}
                for index in range(args.clients):
                    session = store.create(
                        HarnessSession(
                            id=f"harness-{index}",
                            engagement_id=project.id,
                            harness_profile_id=harness.id,
                            model="inert",
                            status="running",
                            metadata={
                                "provider_subagent": setting,
                                "command_runtime_enabled": False,
                            },
                        )
                    )
                    chat = store.create(
                        ChatSession(
                            id=f"chat-{index}",
                            engagement_id=project.id,
                            title="Synthetic supervisor",
                            backend="harness",
                            harness_profile_id=harness.id,
                            harness_session_id=session.id,
                            model="inert",
                        )
                    )
                    parent = store.create(
                        ChatTurn(
                            id=f"parent-{index}",
                            engagement_id=project.id,
                            session_id=chat.id,
                            backend="harness",
                            model="inert",
                            request_snapshot={"provider_subagent": setting},
                        )
                    )
                    turn = store.create(
                        HarnessTurn(
                            id=f"turn-{index}",
                            engagement_id=project.id,
                            harness_session_id=session.id,
                            origin="chat",
                            chat_session_id=chat.id,
                            chat_turn_id=parent.id,
                            prompt="Inspect synthetic capabilities",
                            status="running",
                            metadata={"provider_subagent": setting},
                        )
                    )
                    runtime._active[session.id] = SimpleNamespace(
                        turn_id=turn.id, task=None
                    )

                    async def call(name, arguments, selected=session):
                        return await runtime._gateway_call(selected, name, arguments)

                    gateway = McpGatewaySession(
                        list_tools=lambda _: {"tools": []}, call_tool=call
                    )
                    launch = await gateway.start()
                    gateways.append(gateway)
                    socket_roots.append(str(gateway.root))
                    clients.append(GatewayClient(launch.socket_path, launch.token))

                async def heartbeat():
                    while True:
                        started = time.perf_counter()
                        await asyncio.sleep(0.005)
                        lag.append(
                            max(0, (time.perf_counter() - started - 0.005) * 1000)
                        )

                counts.clear()
                measuring = True
                monitor = asyncio.create_task(heartbeat())
                try:
                    yield
                finally:
                    measuring = False
                    monitor.cancel()
                    with suppress(asyncio.CancelledError):
                        await monitor
            finally:
                for client in clients:
                    await client.close()
                for gateway in gateways:
                    await gateway.close()
                runtime._active.clear()
                args.output.with_suffix(".server.json").write_text(
                    json.dumps(
                        {
                            "isolation": isolation,
                            "loop_lag": summary(lag),
                            "hydration": dict(counts),
                            "gateway_roots": socket_roots,
                            "gateway_directories_removed": all(
                                not Path(p).exists() for p in socket_roots
                            ),
                        }
                    )
                )

    app.router.lifespan_context = lifespan

    @app.post("/__synthetic_mcp_probe__", include_in_schema=False)
    async def invoke(payload: dict, authorization: str = Header(default="")):
        authenticate(authorization)
        index = payload.get("client", 0)
        if not isinstance(index, int) or not 0 <= index < len(clients):
            raise HTTPException(400)
        response = await clients[index].request(
            "tools/call", {"name": "subagent.capabilities", "arguments": {}}
        )
        if response.get("isError"):
            raise HTTPException(500, str(response))
        return response["structuredContent"]

    app.router.routes.append(static_mount)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    store.database.dispose()


async def drive(args, process, token):
    import httpx

    origin = f"http://{args.host}:{args.port}"
    async with httpx.AsyncClient(
        base_url=origin,
        headers={"Authorization": "Bearer " + token},
        timeout=90,
        trust_env=False,
        limits=httpx.Limits(max_connections=128, max_keepalive_connections=128),
    ) as client:
        for _ in range(360):
            if process.poll() is not None:
                raise RuntimeError("Disposable Core exited; inspect retained log")
            try:
                if (
                    await client.get("/api/v1/auth/devices", timeout=0.5)
                ).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Core did not become ready")
        denied = await client.post(
            "/__synthetic_mcp_probe__",
            json={"client": 0},
            headers={"Authorization": "Bearer invalid-fixture"},
        )
        assert denied.status_code == 401, (denied.status_code, denied.text)
        durations = {
            name: [] for name in ("capabilities", "ordinary_http", "404", "static")
        }
        late_visible, timeouts, transport_errors = [], Counter(), Counter()

        async def capabilities(index):
            for _ in range(args.rounds):
                started = time.perf_counter()
                response = await client.post(
                    "/__synthetic_mcp_probe__", json={"client": index}
                )
                response.raise_for_status()
                durations["capabilities"].append((time.perf_counter() - started) * 1000)
                ids = {p["id"] for p in response.json()["mcp_servers"]}
                late_visible.append("external-2534" in ids)

        requests = [asyncio.create_task(capabilities(i)) for i in range(args.clients)]
        paths = {
            "ordinary_http": ("/api/v1/auth/devices", 200),
            "404": ("/api/v1/mcp-probe-missing", 404),
            "static": ("/probe.txt", 200),
        }

        async def ordinary(name, path, status):
            while (
                not all(request.done() for request in requests)
                or len(durations[name]) < 20
            ):
                started = time.perf_counter()
                try:
                    response = await client.get(path, timeout=10)
                    assert response.status_code == status, response.text
                    if name == "static":
                        assert response.text == "synthetic static asset\n"
                    durations[name].append((time.perf_counter() - started) * 1000)
                except httpx.TimeoutException:
                    timeouts[name] += 1
                except httpx.HTTPError:
                    transport_errors[name] += 1
                await asyncio.sleep(0.01)

        started = time.perf_counter()
        await asyncio.gather(
            *requests, *(ordinary(name, *value) for name, value in paths.items())
        )
        return {
            "label": args.label,
            "origin": origin,
            "synthetic": True,
            "authentication_enabled": True,
            "unauthorized_status": denied.status_code,
            "clients": args.clients,
            "rounds": args.rounds,
            "slow_read_ms": args.slow_read_ms,
            "fixture": {
                "profiles": 2535,
                "notebook_profiles": 2526,
                "external_profiles": 9,
                "notebook_tools": 16,
            },
            "latencies": {name: summary(values) for name, values in durations.items()},
            "timeouts_at_10_seconds": dict(timeouts),
            "transport_errors": dict(transport_errors),
            "all_catalogs_include_late_server": all(late_visible),
            "wall_seconds": round(time.perf_counter() - started, 3),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", choices=["baseline", "candidate"], required=True)
    parser.add_argument("--clients", type=int, default=34)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--slow-read-ms", type=int, default=0)
    args = parser.parse_args()
    address = ipaddress.ip_address(args.host)
    if not address.is_private or address.is_loopback or address.is_unspecified:
        parser.error("Use only a private non-loopback test LAN address")
    if (
        not 1 <= args.clients <= 128
        or not 1 <= args.rounds <= 20
        or not 0 <= args.slow_read_ms <= 1000
    ):
        parser.error("Keep the synthetic workload bounded")
    if args.serve:
        return serve(args)
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as selected:
        selected.bind((args.host, 0))
        args.port = selected.getsockname()[1]
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="nebula-mcp-load-") as directory:
        root = Path(directory)
        environment = dict(
            os.environ,
            NEBULA_MCP_PROBE_TOKEN=token,
            XDG_DATA_HOME=str(root / "data"),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_CACHE_HOME=str(root / "cache"),
        )
        with args.output.with_suffix(".log").open("w") as log:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--serve",
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--directory",
                directory,
                "--output",
                str(args.output),
                "--label",
                args.label,
                "--clients",
                str(args.clients),
                "--rounds",
                str(args.rounds),
                "--slow-read-ms",
                str(args.slow_read_ms),
            ]
            process = subprocess.Popen(command, env=environment, stdout=log, stderr=log)
            try:
                result = asyncio.run(drive(args, process, token))
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            result.update(
                json.loads(args.output.with_suffix(".server.json").read_text())
            )
            result.update(
                server_exit_code=process.returncode, temporary_directory=directory
            )
    result.update(
        temporary_data_removed=not root.exists(),
        server_stopped=process.poll() is not None,
    )
    source = Path(os.environ["PYTHONPATH"]) / "nebula" / "v3"
    result["source_files_sha256"] = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in ("storage.py", "mcp.py", "chat_subagents.py", "harnesses.py")
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    if args.label == "candidate":
        assert not result["timeouts_at_10_seconds"]
        assert not result["transport_errors"]
        assert result["all_catalogs_include_late_server"]
        assert result["hydration"]["profiles_hydrated_on_loop"] == 0
        assert result["loop_lag"]["max_ms"] < 1000
        assert all(
            result["latencies"][name]["max_ms"] < 1000
            for name in ("ordinary_http", "404", "static")
        )
    assert result["server_exit_code"] == 0
    assert result["temporary_data_removed"] and result["gateway_directories_removed"]


if __name__ == "__main__":
    main()
