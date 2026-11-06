"""Stress one disposable authenticated Core with explicitly injected CPU workers.

This fixture owns all data and processes. It cannot target an existing Core.
Workers=0 keeps the ordinary threaded renderer; 1–3 enable the experiment.
No provider, command or security tool executes. Fault endpoints exist only in
this script's owned app and operate only on its own render tasks/child handles.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time


def summary(values):
    ordered = sorted(values)

    def percentile(p):
        return (
            round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1)], 3)
            if ordered
            else None
        )

    return {
        "count": len(values),
        "p50": percentile(0.5),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": round(max(values), 3) if values else None,
    }


def rss_bytes(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except FileNotFoundError:
        return 0
    return 0


async def serve(args):
    from datetime import datetime, timezone
    from fastapi import Depends, Header, HTTPException
    from keyring.backends.null import Keyring
    import uvicorn
    from nebula.v3.api import create_app
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.cpu_workers import CpuWorkerPool
    from nebula.v3.butler_capabilities import ButlerCapabilities
    from nebula.v3.credentials import CredentialStore
    from nebula.v3.domain import (
        Engagement,
        HarnessProfile,
        Observation,
        Report,
    )
    from nebula.v3.harnesses import (
        HarnessAdapter,
        HarnessConnection,
        HarnessEvent,
        HarnessRuntimeService,
    )
    from nebula.v3.reporting import ReportRenderService
    from nebula.v3.storage import NebulaStore

    root = Path(args.directory)
    if not root.is_dir() or any(root.iterdir()):
        raise ValueError("Require a newly created empty synthetic directory")
    store = NebulaStore(root / "core.db")
    fixed = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    project = await store.create(
        Engagement(
            id="cpu-project",
            name="Synthetic CPU worker project",
            created_at=fixed,
            updated_at=fixed,
        )
    )
    paragraph = (
        "Synthetic saved report content with stable Unicode Γειά and exact literal <markup>. "
        * 55
        + "\n"
    ) * 2
    notes = [
        Observation(
            id=f"note-{i}",
            engagement_id=project.id,
            observation_type="note",
            title=f"Synthetic note {i}",
            body=paragraph,
            created_at=fixed,
            updated_at=fixed,
        )
        for i in range(48)
    ]
    (await store.create_many(notes))
    credentials = CredentialStore(keyring_backend=Keyring())
    artifacts = ArtifactStore(root / "artifacts")
    pool = (
        CpuWorkerPool(workers=args.workers, queued=args.queued, shutdown_timeout=2)
        if args.workers
        else None
    )
    renders = ReportRenderService(
        store=store, artifact_store=artifacts, cpu_workers=pool
    )
    profile = await store.create(
        HarnessProfile(
            id="inert-harness",
            name="Inert mixed-load harness",
            kind="codex_app_server",
            executable="/bin/true",
            default_model="fixture",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )

    class Connection(HarnessConnection):
        adapter_version = "cpu-experiment/inert"

        def __init__(self, request):
            self.external_session_id = "synthetic-" + request.session.id
            self.interrupted = False

        async def run_turn(self, prompt, *, model, images=None):
            yield HarnessEvent(
                type="started", external_session_id=self.external_session_id
            )
            for index in range(40):
                if self.interrupted:
                    yield HarnessEvent(
                        type="interrupted", message="Synthetic interruption"
                    )
                    return
                for kind in ("tool_started", "tool_completed"):
                    yield HarnessEvent(
                        type=kind,
                        server_id="codex",
                        tool_name="synthetic.noop",
                        item_id=f"tool-{index}",
                        payload={
                            "arguments": {"synthetic": True},
                            "result": "Inert mixed-load output",
                        },
                    )
                    await asyncio.sleep(0.005)
            yield HarnessEvent(
                type="completed", message="Synthetic mixed-load harness completed"
            )

        async def steer(self, text):
            pass

        async def interrupt(self):
            self.interrupted = True

        async def close(self):
            pass

    class Adapter(HarnessAdapter):
        kind = profile.kind

        async def probe(self, profile, credential_store):
            raise AssertionError("No external probe is allowed")

        async def open(self, request):
            return Connection(request)

    runtime = HarnessRuntimeService(
        store,
        credential_store=credentials,
        artifact_store=artifacts,
        workspace_resolver=lambda _: root,
        adapter_factory=lambda _: Adapter(),
    )
    token = os.environ.pop("NEBULA_CPU_EXPERIMENT_TOKEN")
    app = create_app(
        store,
        artifact_store=artifacts,
        credential_store=credentials,
        harness_runtime_service=runtime,
        report_render_service=renders,
        auth_token=token,
        execution_data_root=root / "execution",
    )

    butler = ButlerCapabilities(
        store, artifacts, project.id, work=app.state.control_assistant_work
    )

    async def authorized(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401)

    lag = []
    retired_pids = set()
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):

            async def monitor():
                while True:
                    started = time.perf_counter()
                    await asyncio.sleep(0.01)
                    lag.append(max(0, (time.perf_counter() - started - 0.01) * 1000))
                    if pool:
                        retired_pids.update(
                            worker["pid"] for worker in pool.snapshot()["workers"]
                        )

            task = asyncio.create_task(monitor())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        args.output.with_suffix(".server.json").write_text(
            json.dumps(
                {
                    "coordinator_pid": os.getpid(),
                    "pool": pool.snapshot() if pool else None,
                    "all_worker_pids": sorted(retired_pids),
                    "all_workers_stopped": all(
                        rss_bytes(pid) == 0 for pid in retired_pids
                    ),
                },
                indent=2,
            )
        )

    app.router.lifespan_context = lifespan

    @app.get("/__cpu_experiment__/metrics", dependencies=[Depends(authorized)])
    async def metrics(reset: bool = False):
        state = pool.snapshot() if pool else None
        pids = [os.getpid()] + (
            [worker["pid"] for worker in state["workers"]] if state else []
        )
        value = {
            "coordinator_pid": os.getpid(),
            "core_cpu_s": time.process_time(),
            "worker_cpu_s": sum(w["cpu_seconds"] for w in state["workers"])
            if state
            else 0,
            "rss_bytes": {str(pid): rss_bytes(pid) for pid in pids},
            "loop_lag_ms": summary(lag),
            "pool": state,
        }
        if reset:
            lag.clear()
        return value

    @app.get("/__cpu_experiment__/butler-host-load", dependencies=[Depends(authorized)])
    async def butler_host_load():
        return await butler.invoke(
            {"operation": "nebula.work.host_load", "arguments": {}}, change=False
        )

    @app.post("/__cpu_experiment__/seed", dependencies=[Depends(authorized)])
    async def seed(payload: dict):
        count = int(payload["count"])
        if not 1 <= count <= 128:
            raise HTTPException(400)
        size = payload["size"]
        if size not in {"small", "large"}:
            raise HTTPException(400)
        identities = [f"{payload['prefix']}-{index}" for index in range(count)]
        (
            await store.create_many(
                [
                    Report(
                        id=key,
                        engagement_id=project.id,
                        title=f"Synthetic report {key}",
                        executive_summary="Saved CPU experiment",
                        observation_ids=[
                            n.id for n in notes[: (1 if size == "small" else 48)]
                        ],
                        created_at=fixed,
                        updated_at=fixed,
                    )
                    for key in identities
                ]
            )
        )
        return identities

    @app.post("/__cpu_experiment__/kill-worker", dependencies=[Depends(authorized)])
    async def kill_worker():
        if pool is None:
            raise HTTPException(409)
        for worker in pool._workers:
            if worker.running and worker.process.is_alive():
                worker.process.kill()
                return {"killed_pid": worker.pid}
        raise HTTPException(409, detail="No owned worker is active")

    @app.post(
        "/__cpu_experiment__/cancel/{identity}", dependencies=[Depends(authorized)]
    )
    async def cancel(identity: str):
        task = renders._tasks.get(identity)
        if task is None:
            raise HTTPException(409)
        task.cancel()
        return {"cancel_requested": True}

    (
        await uvicorn.Server(
            uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                access_log=False,
                log_level="warning",
            )
        ).serve()
    )
    (await store.database.dispose())


async def drive(args, token, process):
    import httpx
    from pypdf import PdfReader

    origin = f"http://{args.host}:{args.port}"
    startup = time.perf_counter()
    async with httpx.AsyncClient(
        base_url=origin,
        headers={"Authorization": "Bearer " + token},
        timeout=60,
        trust_env=False,
        limits=httpx.Limits(max_connections=160, max_keepalive_connections=160),
    ) as client:
        for _ in range(240):
            if process.poll() is not None:
                raise RuntimeError("Owned Core exited; inspect the retained log")
            try:
                response = await client.get("/api/v1/auth/devices", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Owned Core did not become ready")
        startup_s = time.perf_counter() - startup
        unauthorized = await client.get(
            "/__cpu_experiment__/metrics",
            headers={"Authorization": "Bearer invalid-synthetic-token"},
        )
        assert unauthorized.status_code == 401
        report_counter = 0
        transport_retries = []

        async def seed(label, count, size):
            nonlocal report_counter
            prefix = f"{label}-{report_counter}"
            report_counter += 1
            response = await client.post(
                "/__cpu_experiment__/seed",
                json={"prefix": prefix, "count": count, "size": size},
            )
            response.raise_for_status()
            return response.json()

        async def metrics(reset=False):
            response = await client.get(
                "/__cpu_experiment__/metrics", params={"reset": str(reset).lower()}
            )
            response.raise_for_status()
            return response.json()

        async def submit(identity):
            response = await client.post(
                f"/api/v1/reports/{identity}/renders", json={"report_revision": 1}
            )
            assert response.status_code == 202, response.text
            return response.json()["id"]

        async def finish(identity, *, allow_failure=False, validate_text=False):
            deadline = time.monotonic() + 120
            while True:
                try:
                    response = await client.get(f"/api/v1/report-renders/{identity}")
                except httpx.TransportError as exc:
                    transport_retries.append(type(exc).__name__)
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.05)
                    continue
                assert response.status_code == 200, response.text
                saved = response.json()
                if saved["status"] in {"completed", "failed", "interrupted"}:
                    break
                assert time.monotonic() < deadline, saved
                await asyncio.sleep(0.025)
            if saved["status"] != "completed":
                assert allow_failure, saved
                response = await client.get(f"/api/v1/report-renders/{identity}/pdf")
                assert response.status_code == 409, response.text
                return {
                    "status": saved["status"],
                    "error": saved.get("error_detail"),
                    "render_id": identity,
                    "report_id": saved["report_id"],
                }
            response = await client.get(f"/api/v1/report-renders/{identity}/pdf")
            assert response.status_code == 200 and response.content.startswith(
                b"%PDF-"
            ), response.text[:200]
            digest = hashlib.sha256(response.content).hexdigest()
            if validate_text:
                reader = PdfReader(io.BytesIO(response.content))
                assert len(reader.pages) >= 2
                assert "Synthetic" in (reader.pages[0].extract_text() or "")
            cached = await client.post(
                f"/api/v1/reports/{saved['report_id']}/renders",
                json={"report_revision": saved["report_revision"]},
            )
            assert cached.status_code == 202 and cached.json()["id"] == identity
            return {
                "status": "completed",
                "sha256": digest,
                "bytes": len(response.content),
                "render_id": identity,
                "report_id": saved["report_id"],
            }

        async def harness():
            started = time.perf_counter()
            response = await client.post(
                "/api/v1/chat/completions",
                json={
                    "backend": "harness",
                    "engagement_id": "cpu-project",
                    "harness_profile_id": "inert-harness",
                    "model": "fixture",
                    "messages": [
                        {"role": "user", "content": "Run inert mixed-load events"}
                    ],
                },
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert (
                body["message"]["content"] == "Synthetic mixed-load harness completed"
            )
            response = await client.get(
                f"/api/v1/harness-turns/{body['harness_turn_id']}/events"
            )
            response.raise_for_status()
            events = response.json()["events"]
            calls = {}
            for event in events:
                if event["type"] in {"tool_started", "tool_completed"}:
                    calls.setdefault(event["tool_call_id"], []).append(event["type"])
            assert len(calls) == 40 and all(
                kinds == ["tool_started", "tool_completed"] for kinds in calls.values()
            )
            sequence = [event["sequence"] for event in events]
            assert sequence == sorted(set(sequence))
            replay = await client.get(
                f"/api/v1/harness-turns/{body['harness_turn_id']}/events",
                params={"after": sequence[-1]},
            )
            assert replay.status_code == 200 and not replay.json()["events"]
            return {
                "wall_s": time.perf_counter() - started,
                "tool_calls": 40,
                "ordered_replay": True,
            }

        async def batch(
            label, count, size, concurrency, *, allow_failure=False, mixed=False
        ):
            ids = await seed(label, count, size)
            before = await metrics(True)
            stopped = asyncio.Event()
            probes = []
            probe_timeouts = []
            probe_errors = []
            host_reads = []

            async def sentinel():
                while not stopped.is_set():
                    start = time.perf_counter()
                    try:
                        response = await client.get(
                            "/api/v1/engagements/cpu-project/assistant-defaults",
                            timeout=15,
                        )
                        assert response.status_code == 200, response.text
                        probes.append((time.perf_counter() - start) * 1000)
                    except httpx.TimeoutException:
                        probe_timeouts.append((time.perf_counter() - start) * 1000)
                    except httpx.TransportError as exc:
                        probe_errors.append(type(exc).__name__)
                    if len(probes) % 8 == 1:
                        host = await client.get(
                            "/__cpu_experiment__/butler-host-load", timeout=30
                        )
                        host.raise_for_status()
                        value = host.json()
                        assert {
                            "cpu_capacity",
                            "cpu_ratio",
                            "memory_available_ratio",
                            "busy",
                            "measurements_available",
                            "sample_age_seconds",
                        } <= value.keys()
                        host_reads.append(value)
                    await asyncio.sleep(0.02)

            watcher = asyncio.create_task(sentinel())
            mixed_task = asyncio.create_task(harness()) if mixed else None
            gate = asyncio.Semaphore(concurrency)
            latencies = []

            async def one(index, identity):
                async with gate:
                    started = time.perf_counter()
                    receipt = await submit(identity)
                    result = await finish(
                        receipt, allow_failure=allow_failure, validate_text=index == 0
                    )
                    latencies.append((time.perf_counter() - started) * 1000)
                    return result

            start = time.perf_counter()
            try:
                values = await asyncio.gather(
                    *(one(i, identity) for i, identity in enumerate(ids))
                )
                elapsed = time.perf_counter() - start
                mixed_result = await mixed_task if mixed_task else None
            finally:
                stopped.set()
                await watcher
            after = await metrics()
            completed = sum(value["status"] == "completed" for value in values)
            failures = [value for value in values if value["status"] != "completed"]
            if failures:
                assert args.workers and all(
                    "queue is full" in value["error"] for value in failures
                ), failures
            core_cpu = after["core_cpu_s"] - before["core_cpu_s"]
            worker_cpu = after["worker_cpu_s"] - before["worker_cpu_s"]
            result = {
                "label": label,
                "size": size,
                "requests": count,
                "concurrency": concurrency,
                "wall_s": round(elapsed, 3),
                "completed": completed,
                "rejected": len(failures),
                "completed_rps": round(completed / elapsed, 3),
                "core_cpu_s": round(core_cpu, 3),
                "worker_cpu_s": round(worker_cpu, 3),
                "effective_cpus": round((core_cpu + worker_cpu) / elapsed, 3),
                "render_ms": summary(latencies),
                "ordinary_http_ms": summary(probes),
                "ordinary_http_timeouts_at_15s": len(probe_timeouts),
                "ordinary_http_transport_errors": probe_errors,
                "butler_host_reads": len(host_reads),
                "loop_lag_ms": after["loop_lag_ms"],
                "rss_bytes": after["rss_bytes"],
                "pool": after["pool"],
                "pdf_sha256": {
                    v["report_id"]: v["sha256"]
                    for v in values
                    if v["status"] == "completed"
                },
                "failures": failures,
                "mixed_harness": mixed_result,
            }
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in result.items()
                        if k not in {"pdf_sha256", "pool", "failures", "rss_bytes"}
                    }
                ),
                flush=True,
            )
            args.output.with_suffix(f".{label}.phase.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
            return result

        phases = []
        phases.append(
            await batch("cold", max(1, args.workers), "small", max(1, args.workers))
        )
        for trial in range(args.rounds):
            for size in ("small", "large"):
                phases.append(
                    await batch(
                        f"{size}-{trial}",
                        args.jobs,
                        size,
                        args.concurrency,
                        mixed=size == "large",
                    )
                )
        if args.burst:
            phases.append(
                await batch(
                    "overload",
                    args.burst,
                    "large",
                    args.burst,
                    allow_failure=bool(args.workers),
                )
            )
        faults = []
        if args.faults and args.workers:
            for trial in range(args.faults):
                ids = await seed(f"fault-{trial}", args.workers + 1, "large")
                receipts = await asyncio.gather(*(submit(identity) for identity in ids))
                for _ in range(200):
                    killed = await client.post("/__cpu_experiment__/kill-worker")
                    if killed.status_code == 200:
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("No worker became active for fault injection")
                results = await asyncio.gather(
                    *(finish(identity, allow_failure=True) for identity in receipts)
                )
                failed = [value for value in results if value["status"] == "failed"]
                assert len(failed) == 1 and "worker exited" in failed[0]["error"], (
                    results
                )
                retry = await submit(failed[0]["report_id"])
                retried = await finish(retry, validate_text=True)
                assert retry != failed[0]["render_id"]
                faults.append(
                    {
                        "killed_pid": killed.json()["killed_pid"],
                        "failed_once": True,
                        "retry_completed": retried["status"] == "completed",
                        "pool": (await metrics())["pool"],
                    }
                )
        soak = []
        deadline = time.monotonic() + args.soak_seconds
        while time.monotonic() < deadline:
            soak.append(
                await batch(
                    f"soak-{len(soak)}",
                    args.jobs,
                    "large",
                    args.concurrency,
                    mixed=True,
                )
            )
        final = await metrics()
        if final["pool"]:
            pool = final["pool"]
            assert (
                pool["max_active"] <= args.workers
                and pool["max_admitted"] <= args.workers + args.queued
            )
            assert pool["admitted"] == 0 and pool["input_bytes"] == 0
            assert pool["max_input_bytes"] <= 32 * 1024 * 1024
            assert (
                pool["worker_exits"] == args.faults and pool["restarts"] == args.faults
            )
        return {
            "origin": origin,
            "workers": args.workers,
            "queued": args.queued,
            "startup_to_authenticated_health_s": round(startup_s, 3),
            "auth_enabled": True,
            "unauthorized_status": unauthorized.status_code,
            "phases": phases,
            "faults": faults,
            "soak": soak,
            "soak_requested_s": args.soak_seconds,
            "final_metrics": final,
            "render_poll_transport_retries": transport_retries,
        }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--directory")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--queued", type=int, default=6)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--burst", type=int, default=32)
    parser.add_argument("--faults", type=int, default=2)
    parser.add_argument("--soak-seconds", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.serve:
        return await serve(args)
    address = ipaddress.ip_address(args.host)
    if not address.is_private or address.is_loopback or address.is_unspecified:
        parser.error("Use a private non-loopback test LAN address")
    if (
        not 0 <= args.workers <= 3
        or args.rounds < 0
        or args.jobs < 1
        or args.concurrency < 1
    ):
        parser.error("Require 0–3 workers and positive workload bounds")
    if args.burst > 128 or args.jobs > 128 or args.soak_seconds < 0:
        parser.error("Workload exceeds the owned fixture bounds")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as selected:
        selected.bind((args.host, 0))
        args.port = selected.getsockname()[1]
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="nebula-cpu-stress-") as directory:
        env = dict(
            os.environ,
            NEBULA_CPU_EXPERIMENT_TOKEN=token,
            XDG_DATA_HOME=directory + "/data",
            XDG_CONFIG_HOME=directory + "/config",
            XDG_CACHE_HOME=directory + "/cache",
        )
        command = [
            sys.executable,
            __file__,
            "--serve",
            "--directory",
            directory,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--workers",
            str(args.workers),
            "--queued",
            str(args.queued),
            "--output",
            str(args.output),
        ]
        with args.output.with_suffix(".log").open("w") as log:
            process = subprocess.Popen(command, env=env, stdout=log, stderr=log)
            try:
                result = await drive(args, token, process)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        server = json.loads(args.output.with_suffix(".server.json").read_text())
        assert process.returncode == 0 and server["all_workers_stopped"], server
        result["shutdown"] = server
        result["server_exit_code"] = process.returncode
    result["synthetic_data_removed"] = not Path(directory).exists()
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "workers": args.workers,
                "phases": len(result["phases"]),
                "soak_batches": len(result["soak"]),
                "faults": len(result["faults"]),
                "all_workers_stopped": server["all_workers_stopped"],
                "synthetic_data_removed": result["synthetic_data_removed"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
