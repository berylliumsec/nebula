"""Measure concurrent real-Core lists, authenticated HTTP, and loop lag.

Requires a directory created by entity_lookups.py seed. The optional slow-read
probe holds each artifact SQL read in its owning thread after Core startup.
"""

import argparse
import asyncio
import json
import os
import secrets
import signal
import socket
import statistics
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path


def summary(values):
    ordered = sorted(values)
    return {
        "count": len(values),
        "median_ms": statistics.median(values),
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max_ms": max(values),
    }


async def serve(args):
    import uvicorn
    from fastapi import Header, HTTPException
    from sqlalchemy import event
    from nebula.v3.api import create_app
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.chat import ChatService
    from nebula.v3.storage import NebulaStore

    store = NebulaStore(args.database)
    app = create_app(
        store,
        artifact_store=ArtifactStore(args.output.parent / "artifacts"),
        auth_token=os.environ["NEBULA_BENCH_TOKEN"],
        execution_data_root=args.output.parent / "runtime",
    )
    original = app.router.lifespan_context
    ready = False

    @event.listens_for(store.database.engine.sync_engine, "before_cursor_execute")
    def slow_read(conn, cursor, statement, parameters, context, executemany):
        if (
            ready
            and args.slow_read_ms
            and statement.startswith("SELECT")
            and "FROM entities" in statement
            and "artifacts" in parameters
        ):
            conn.connection.dbapi_connection.run_async(
                lambda _driver: asyncio.sleep(args.slow_read_ms / 1000)
            )

    lag = []

    @asynccontextmanager
    async def lifespan(app):
        nonlocal ready
        async with original(app):
            ready = True

            async def monitor():
                while True:
                    start = time.perf_counter()
                    await asyncio.sleep(0.01)
                    lag.append(max(0, (time.perf_counter() - start - 0.01) * 1000))

            monitor_task = asyncio.create_task(monitor())
            try:
                yield
            finally:
                monitor_task.cancel()
                args.output.with_suffix(".lag.json").write_text(
                    json.dumps(summary(lag))
                )

    app.router.lifespan_context = lifespan

    @app.post("/__synthetic_recovery__", include_in_schema=False)
    async def recovery(authorization: str = Header()):
        if not secrets.compare_digest(
            authorization, "Bearer " + os.environ["NEBULA_BENCH_TOKEN"]
        ):
            raise HTTPException(status_code=401)
        return await ChatService(store).reconcile_stale_approved_tool_calls()

    (
        await uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
        ).serve()
    )
    (await store.database.dispose())


async def drive(args, token):
    import httpx

    base = f"http://127.0.0.1:{args.port}"
    async with httpx.AsyncClient(
        base_url=base,
        headers={"Authorization": "Bearer " + token},
        timeout=120,
        trust_env=False,
    ) as client:
        for _ in range(240):
            try:
                response = await client.get("/api/v1/auth/devices")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Core did not become ready")
        durations = {"listing": [], "lightweight_http": [], "recovery": []}

        async def timed(name, path, method="GET"):
            started = time.perf_counter()
            response = await client.request(method, path)
            response.raise_for_status()
            durations[name].append((time.perf_counter() - started) * 1000)
            if name == "listing":
                assert len(response.json()) == 1000

        for _ in range(3):
            listing = [
                asyncio.create_task(
                    timed(
                        "listing",
                        "/api/v1/artifacts?engagement_id=bench-project&limit=1000",
                    )
                )
                for _ in range(args.listing_requests)
            ]
            recover = asyncio.create_task(
                timed("recovery", "/__synthetic_recovery__", "POST")
            )
            for _ in range(20):
                await timed("lightweight_http", "/api/v1/auth/devices")
                await asyncio.sleep(0.01)
            await asyncio.gather(*listing, recover)
        return {
            "origin": base,
            "listing_requests_per_wave": args.listing_requests,
            "latencies": {k: summary(v) for k, v in durations.items()},
        }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--slow-read-ms", type=int, default=0)
    parser.add_argument("--listing-requests", type=int, default=8)
    args = parser.parse_args()
    assert (
        json.loads((args.database.parent / "synthetic.json").read_text())["synthetic"]
        is True
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.serve:
        return await serve(args)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        args.port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    env = dict(
        os.environ,
        NEBULA_BENCH_TOKEN=token,
        XDG_DATA_HOME=str(args.output.parent / "data"),
        XDG_CONFIG_HOME=str(args.output.parent / "config"),
        XDG_CACHE_HOME=str(args.output.parent / "cache"),
    )
    with args.output.with_suffix(".log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                __file__,
                "--serve",
                "--database",
                str(args.database),
                "--output",
                str(args.output),
                "--port",
                str(args.port),
                "--slow-read-ms",
                str(args.slow_read_ms),
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            result = await drive(args, token)
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    result["loop_lag"] = json.loads(args.output.with_suffix(".lag.json").read_text())
    if args.database.name == "restored.db":
        assert result["latencies"]["lightweight_http"]["max_ms"] < 1000, result
        assert result["loop_lag"]["max_ms"] < 1000, result
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
