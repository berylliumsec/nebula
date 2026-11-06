"""Measure read contention on an owned, disposable, paired LAN Core.

Run with the project's Python environment. This always launches its own Core;
it cannot target a running installation. No model, command or security tool runs.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import ipaddress
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback

import httpx
from fastapi import HTTPException, Request


def percentile(values: list[float], percent: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[math.ceil(len(ordered) * percent / 100) - 1], 2)


async def serve_fixture(data_dir: Path, port: int) -> None:
    if not data_dir.is_dir() or any(data_dir.iterdir()):
        raise ValueError("fixture Core requires a newly created empty data directory")
    import uvicorn
    from keyring.backends.null import Keyring

    from nebula.v3.api import create_app
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.credentials import CredentialStore
    from nebula.v3.domain import Engagement, Observation, WorkItem
    from nebula.v3.storage import NebulaStore

    store = NebulaStore(data_dir / "nebula.db")
    (await store.create(Engagement(id="load-project", name="Synthetic load project")))
    for start in range(0, 12_000, 500):
        (
            await store.create_many(
                [
                    Observation(
                        id=f"note-{index:05}",
                        engagement_id="load-project",
                        observation_type="note",
                        title=f"Load note {index:05}",
                        body="Synthetic note content. " * 80,
                    )
                    for index in range(start, start + 500)
                ]
            )
        )
    for start in range(0, 2_000, 500):
        (
            await store.create_many(
                [
                    WorkItem(
                        id=f"work-{index:05}",
                        engagement_id="load-project",
                        title=f"Load work {index:05}",
                        description="Synthetic work. " * 100,
                    )
                    for index in range(start, start + 500)
                ]
            )
        )
    token = os.environ.pop("NEBULA_LOAD_TEST_TOKEN")
    app = create_app(
        store,
        artifact_store=ArtifactStore(data_dir / "artifacts"),
        credential_store=CredentialStore(keyring_backend=Keyring()),
        execution_data_root=data_dir,
        auth_token=token,
        allow_insecure_device_pairing=True,
    )
    lag: list[float] = []
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):

            async def tick():
                while True:
                    started = time.perf_counter()
                    await asyncio.sleep(0.01)
                    lag.append(max(0.0, (time.perf_counter() - started - 0.01) * 1000))

            timer = asyncio.create_task(tick())
            try:
                yield
            finally:
                timer.cancel()
                with suppress(asyncio.CancelledError):
                    await timer

    app.router.lifespan_context = lifespan

    @app.get("/__load_test/metrics")
    async def metrics(request: Request):
        if request.headers.get("Authorization") != f"Bearer {token}":
            raise HTTPException(401)
        result = {
            "loop_p95_ms": percentile(lag, 95),
            "loop_max_ms": round(max(lag, default=0), 2),
            "rss_peak_mib": round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1
            ),
            "cpu_s": round(time.process_time(), 3),
        }
        lag.clear()
        return result

    (
        await uvicorn.Server(
            uvicorn.Config(
                app, host="0.0.0.0", port=port, access_log=False, log_level="warning"
            )
        ).serve()
    )


async def measure_phase(client, admin, origin, headers, route, params, concurrency):
    await admin.get("/__load_test/metrics")
    latencies, errors, probes = [], [], []
    done = asyncio.Event()

    async def read():
        started = time.perf_counter()
        try:
            response = await client.get(route, params=params)
            response.raise_for_status()
            body = response.json()
            assert len(body["items"] if route.endswith("/search") else body) == (
                30 if route.endswith("/search") else 500
            ), "fixture response changed"
        except Exception as exc:
            errors.append({"type": type(exc).__name__, "trace": traceback.format_exc()})
        latencies.append((time.perf_counter() - started) * 1000)

    async def sentinel():
        async with httpx.AsyncClient(
            base_url=origin, headers=headers, trust_env=False, timeout=45
        ) as fast:
            while not done.is_set():
                started = time.perf_counter()
                try:
                    response = await fast.get(
                        "/api/v1/engagements/load-project/assistant-defaults"
                    )
                    response.raise_for_status()
                except Exception as exc:
                    errors.append(f"sentinel:{type(exc).__name__}")
                probes.append((time.perf_counter() - started) * 1000)
                await asyncio.sleep(0.02)

    watcher = asyncio.create_task(sentinel())
    started = time.perf_counter()
    try:
        for _ in range(2):
            await asyncio.gather(*(read() for _ in range(concurrency)))
    finally:
        done.set()
        await watcher
    elapsed = time.perf_counter() - started
    response = await admin.get("/__load_test/metrics")
    response.raise_for_status()
    return {
        "route": route,
        "concurrency": concurrency,
        "requests": len(latencies),
        "errors": errors,
        "seconds": round(elapsed, 3),
        "rps": round(len(latencies) / elapsed, 2),
        "request_p50_ms": percentile(latencies, 50),
        "request_p95_ms": percentile(latencies, 95),
        "request_max_ms": percentile(latencies, 100),
        "sentinel_count": len(probes),
        "sentinel_p95_ms": percentile(probes, 95),
        "sentinel_max_ms": percentile(probes, 100),
        **response.json(),
    }


async def run(output: Path, source: Path, lan_address: str):
    data_dir = Path(tempfile.mkdtemp(prefix="nebula-load-"))
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        port = listener.getsockname()[1]
    origin = f"http://{lan_address}:{port}"
    token = secrets.token_urlsafe(32)
    headers = {"Authorization": f"Bearer {token}"}
    env = {**os.environ, "NEBULA_LOAD_TEST_TOKEN": token}
    source_hash = hashlib.sha256()
    for name in (
        "api.py",
        "search.py",
        "action_registry.py",
        "storage.py",
        "read_workers.py",
    ):
        source_hash.update(name.encode() + b"\0")
        source_hash.update((source / "src" / "nebula" / "v3" / name).read_bytes())
    report = {
        "origin": origin,
        "build": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip(),
        "working_tree_dirty": subprocess.call(
            ["git", "diff", "--quiet", "HEAD"], cwd=source
        )
        != 0,
        "source_sha256": source_hash.hexdigest(),
        "fixture": {
            "notes": 12_000,
            "work_items": 2_000,
            "providers": "none",
            "authentication": "paired-device cookie",
        },
        "phases": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(report, indent=2) + "\n")

    process = None
    with output.with_suffix(".server.log").open("w") as log:
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--source-tree",
                    str(source),
                    "--serve",
                    str(data_dir),
                    str(port),
                ],
                cwd=source,
                env=env,
                stdout=log,
                stderr=log,
            )
            limits = httpx.Limits(max_connections=100, max_keepalive_connections=100)
            async with httpx.AsyncClient(
                base_url=origin,
                headers=headers,
                trust_env=False,
                timeout=45,
                limits=limits,
            ) as admin:
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("fixture Core exited; see server log")
                    try:
                        response = await admin.get(
                            "/api/v1/engagements/load-project/assistant-defaults"
                        )
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("fixture startup deadline exceeded")
                async with httpx.AsyncClient(trust_env=False) as anonymous:
                    report["unauthenticated_status"] = (
                        await anonymous.get(origin + "/api/v1/search")
                    ).status_code
                    assert report["unauthenticated_status"] == 401
                offer = await admin.post(
                    f"http://127.0.0.1:{port}/api/v1/auth/pairings",
                    json={"name": "Load fixture"},
                )
                offer.raise_for_status()
                pair = offer.json()
                async with httpx.AsyncClient(
                    base_url=origin, trust_env=False, timeout=45, limits=limits
                ) as client:
                    redeemed = await client.post(
                        "/api/v1/auth/pairings/redeem",
                        json={
                            "secret": pair["secret"],
                            "confirmation_code": pair["confirmation_code"],
                            "name": "Load fixture",
                        },
                    )
                    redeemed.raise_for_status()
                    report["pairing_status"] = redeemed.status_code
                    for route, params in (
                        (
                            "/api/v1/search",
                            {
                                "query": "Load",
                                "active_project": "load-project",
                                "limit": 30,
                            },
                        ),
                        ("/api/v1/work/items", {"limit": 500}),
                        ("/api/v1/engagements/load-project/work", {}),
                    ):
                        for concurrency in (1, 8, 32, 64):
                            phase = await measure_phase(
                                client,
                                admin,
                                origin,
                                headers,
                                route,
                                params,
                                concurrency,
                            )
                            report["phases"].append(phase)
                            save()
                            print(json.dumps(phase), flush=True)
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            shutil.rmtree(data_dir)
            report["cleanup"] = {
                "server_exit": process.returncode if process else None,
                "temporary_data_removed": not data_dir.exists(),
            }
            save()
    if any(phase["errors"] for phase in report["phases"]):
        raise RuntimeError("load requests failed; see report")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(tempfile.gettempdir()) / "nebula-core-load.json",
    )
    parser.add_argument(
        "--source-tree", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--lan-address", default=socket.gethostbyname(socket.gethostname())
    )
    parser.add_argument("--serve", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    source = args.source_tree.resolve()
    sys.path.insert(0, str(source / "src"))
    if args.serve:
        (await serve_fixture(Path(args.serve[0]), int(args.serve[1])))
        return
    address = ipaddress.ip_address(args.lan_address)
    if address.version != 4 or address.is_loopback or address.is_unspecified:
        parser.error(
            "--lan-address must be an IPv4 address of this host on the test LAN"
        )
    (await run(args.output.resolve(), source, str(address)))


if __name__ == "__main__":
    asyncio.run(main())
