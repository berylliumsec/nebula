"""Run bounded, fixed-arrival mixed load against an owned disposable Core.

The Core runs in a separate process so a blocked server cannot delay the load
clock. Only NEBULA_TEST_POSTGRES_URL is accepted; each run owns a temporary schema.
The controlled monitor is an explicitly labeled experiment, not production host
admission evidence. The original closed-loop ASGI harness remains in
scripts/benchmarks/async_database_asgi.py.
"""

import argparse
import asyncio
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


def distribution(values):
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50_ms": round(ordered[max(0, math.ceil(len(ordered) * 0.5) - 1)] * 1000, 3)
        if ordered
        else None,
        "p95_ms": round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] * 1000, 3)
        if ordered
        else None,
        "max_ms": round(ordered[-1] * 1000, 3) if ordered else None,
    }


def read_optional(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def source_identity(root):
    digest = hashlib.sha256()
    for path in sorted((root / "src/nebula/v3").rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return {
        "revision": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip(),
        "runtime_source_sha256": digest.hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "cpu_quota": read_optional("/sys/fs/cgroup/cpu.max"),
        "host_monitor": None,
    }


async def call(function, *args, **kwargs):
    result = function(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


async def serve_fixture(args):
    sys.path.insert(0, str(args.source / "src"))
    import uvicorn
    from fastapi import HTTPException, Request
    from sqlalchemy import insert
    import nebula.v3.database as database_module
    import nebula.v3.chat as chat_module
    from nebula.v3.api import create_app
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.database import Database, EntityRow
    from nebula.v3.domain import (
        Engagement,
        ProviderProfile,
        HarnessSession,
        HarnessTurn,
        HarnessTurnOrigin,
        HarnessTurnStatus,
        RiskClass,
        ScopePolicy,
    )
    from nebula.v3.host_load import HostLoadGate, HostLoadSnapshot
    from nebula.v3.policy import PolicyEngine
    from nebula.v3.providers import (
        ModelProvider,
        ModelResponse,
        ModelUsage,
        ProviderConfig,
        ProviderKind,
        ModelCapabilities,
        ProviderHealth,
    )
    from nebula.v3.sandbox import AnalysisOnlyRunner
    from nebula.v3.storage import NebulaStore
    from nebula.v3.tools import (
        ToolRegistry,
        ToolSpec,
        AnalysisTool,
        ToolBroker,
        StoreToolLedger,
        ToolInvocation,
    )

    native = inspect.iscoroutinefunction(getattr(Database, "initialize", None))
    if native:
        from nebula.v3.database_pool import PoolSettings

        database = Database(
            os.environ["NEBULA_BENCHMARK_SCOPED_URL"],
            pool_settings=PoolSettings(5, 12, 2, args.pool),
        )
    else:
        original = database_module.create_engine

        def bounded(*positional, **keyword):
            return original(
                *positional,
                **keyword,
                pool_size=args.pool,
                max_overflow=0,
                pool_timeout=30,
            )

        database_module.create_engine = bounded
        database = Database(os.environ["NEBULA_BENCHMARK_SCOPED_URL"])
    store = NebulaStore(database)
    root = args.fixture_dir
    try:
        await call(
            store.create, Engagement(id="fixture-project", name="Synthetic mixed load")
        )
        await call(
            store.create,
            ProviderProfile(
                id="fixture-provider",
                name="Inert provider",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a"],
                metadata={"default_model": "model-a"},
            ),
        )
        sessions = await call(
            store.create_many,
            [
                HarnessSession(
                    id=f"harness-{i}",
                    engagement_id="fixture-project",
                    harness_profile_id="fixture-harness",
                    model="fixture",
                )
                for i in range(12)
            ],
        )
        rows = []
        for session in sessions:
            for index in range(200):
                turn = HarnessTurn(
                    engagement_id="fixture-project",
                    harness_session_id=session.id,
                    origin=HarnessTurnOrigin.ANALYSIS,
                    status=HarnessTurnStatus.COMPLETE,
                    prompt=f"Historical fixture {index}",
                    response="Settled synthetic history.",
                )
                rows.append(
                    dict(
                        id=turn.id,
                        kind=turn.entity_kind,
                        engagement_id=turn.engagement_id,
                        revision=1,
                        payload=turn.model_dump(mode="json"),
                        harness_session_id=session.id,
                        created_at=turn.created_at,
                        updated_at=turn.updated_at,
                    )
                )
        if native:
            async with database.session() as session:
                await session.execute(insert(EntityRow), rows)
        else:
            with database.session() as session:
                session.execute(insert(EntityRow), rows)

        class FixtureProvider(ModelProvider):
            def __init__(self):
                super().__init__(
                    ProviderConfig(
                        id="fixture-provider",
                        kind=ProviderKind.OPENAI_COMPATIBLE,
                        base_url="http://127.0.0.1:8000/v1",
                        default_model="model-a",
                        model_allowlist=["model-a"],
                        local=True,
                        capabilities=ModelCapabilities(streaming=True),
                    )
                )

            async def complete(self, request):
                await asyncio.sleep(0.02)
                return ModelResponse(
                    provider_id=self.config.id,
                    model="model-a",
                    text="Synthetic response.",
                    finish_reason="stop",
                    usage=ModelUsage(input_tokens=4, output_tokens=3, total_tokens=7),
                )

            async def health(self):
                return ProviderHealth(
                    provider_id=self.config.id, healthy=True, models=["model-a"]
                )

        class ControlledMonitor:
            # Real FIFO/start-budget code remains in HostLoadGate. Eight starts
            # per 10 ms sample is finite and above the six-slot provider's 300/s
            # theoretical rate. Actual CPU and PostgreSQL contention still apply.
            interval = 0.01

            def cached(self):
                epoch = math.floor(time.monotonic() / self.interval) * self.interval
                return HostLoadSnapshot(epoch, 4.0, 0.0, 0.5, False, None, True)

            def sample(self):
                return self.cached()

        provider = FixtureProvider()
        chat_module.provider_from_profile = lambda _: provider
        registry = ToolRegistry()

        async def read(_):
            await asyncio.sleep(0.001)
            return {"value": "synthetic"}

        registry.register(
            AnalysisTool(
                ToolSpec(
                    name="fixture_read",
                    description="Read a synthetic fixture",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    risk_class=RiskClass.LOCAL_READ,
                ),
                read,
            )
        )
        broker = ToolBroker(
            registry=registry,
            policy_engine=PolicyEngine(),
            runner=AnalysisOnlyRunner(),
            ledger=StoreToolLedger(store, enforce_run_budget=False),
            workspace_resolver=lambda _: root,
        )
        monitor = (
            HostLoadGate(ControlledMonitor())
            if args.host_monitor == "controlled"
            else HostLoadGate()
        )
        app = create_app(
            store,
            artifact_store=ArtifactStore(root / "artifacts"),
            auth_token="synthetic-benchmark-token",
            host_load_gate=monitor,
            execution_data_root=root,
            mission_checkpoint_path=root / "mission-checkpoints.db",
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=args.port,
                log_level="warning",
                access_log=False,
            )
        )

        def authorize(request):
            if (
                request.headers.get("authorization")
                != "Bearer synthetic-benchmark-token"
            ):
                raise HTTPException(401, "Synthetic fixture authentication required")

        @app.post("/fixture/tool")
        async def execute_tool(request: Request):
            authorize(request)
            body = await request.json()
            result = await broker.execute(
                ToolInvocation(
                    engagement_id="fixture-project",
                    run_id="fixture-" + body["key"],
                    tool_name="fixture_read",
                    arguments={},
                    workspace=root,
                    idempotency_key="fixture-" + body["key"],
                ),
                ScopePolicy(engagement_id="fixture-project"),
            )
            return result.output

        @app.get("/fixture/observation")
        async def observation(request: Request):
            authorize(request)
            snapshot = await monitor.snapshot()
            return {
                "host": snapshot.public(),
                "host_waiters": len(monitor._waiters),
                "host_starts_in_epoch": monitor._starts,
                "host_epoch": monitor._epoch,
                "pool": database.pool_snapshot()
                if native
                else {"checked_out": database.engine.pool.checkedout()},
                "load_average": read_optional("/proc/loadavg"),
                "event_loop": type(asyncio.get_running_loop()).__module__
                + "."
                + type(asyncio.get_running_loop()).__qualname__,
            }

        @app.post("/fixture/measure")
        async def measure(request: Request):
            authorize(request)
            # Preserve the same telemetry object captured by SQL/middleware
            # callbacks while excluding setup and warmup from aggregate timings.
            performance = database.performance
            with performance._lock:
                performance._operations.clear()
                performance._slow.clear()
                performance._started = time.perf_counter()
            return {"measurement": "started"}

        @app.get("/fixture/performance")
        async def performance(request: Request):
            authorize(request)
            return database.performance.snapshot()

        @app.post("/fixture/stop")
        async def stop(request: Request):
            authorize(request)
            server.should_exit = True
            return {"stopping": True}

        await server.serve()
        result = {
            "checked_out_after_shutdown": database.pool_snapshot()["checked_out"]
            if native
            else database.engine.pool.checkedout()
        }
        (root / "shutdown.json").write_text(json.dumps(result))
    finally:
        await call(store.event_history.close)
        await call(database.dispose)


async def drive(args, origin, process):
    import httpx

    headers = {"Authorization": "Bearer synthetic-benchmark-token"}
    timeout = httpx.Timeout(90.0, connect=5.0, pool=30.0)
    metrics = {
        name: []
        for name in (
            "chat",
            "admission",
            "first_token",
            "listing",
            "auth",
            "tool",
            "issue_lag",
        )
    }
    errors = []
    rejections = {kind: [] for kind in ("chat", "listing", "auth", "tool")}
    observations = []
    successful_turn_ids = []
    async with httpx.AsyncClient(
        base_url=origin,
        headers=headers,
        trust_env=False,
        timeout=timeout,
        limits=httpx.Limits(max_connections=256, max_keepalive_connections=128),
    ) as client:
        deadline = time.monotonic() + 90
        while True:
            if process.poll() is not None:
                raise RuntimeError("Owned fixture exited during startup")
            try:
                ready = await client.get("/api/v1/health")
                if ready.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("Owned fixture did not become ready")
            await asyncio.sleep(0.1)

        async def chat(record=True):
            started = time.perf_counter()
            event = None
            payload = []
            first = None
            admitted = None
            completed = None
            failure = None
            async with client.stream(
                "POST",
                "/api/v1/chat/completions",
                json={
                    "engagement_id": "fixture-project",
                    "provider_id": "fixture-provider",
                    "model": "model-a",
                    "include_knowledge": False,
                    "stream": True,
                    "messages": [
                        {"role": "user", "content": "Return the fixture response."}
                    ],
                },
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise RuntimeError(
                        f"chat HTTP {response.status_code}: {response.text[:200]}"
                    )
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        payload.append(line[6:])
                    elif not line:
                        if payload:
                            data = json.loads("\n".join(payload))
                            if event == "admitted" and admitted is None:
                                admitted = time.perf_counter() - started
                            if event == "delta" and data.get("delta") and first is None:
                                first = time.perf_counter() - started
                            if event == "done":
                                completed = data
                            if event == "error":
                                failure = data
                        event, payload = None, []
            if (
                failure
                or not completed
                or completed.get("message", {}).get("content") != "Synthetic response."
            ):
                raise RuntimeError(
                    "Chat failed semantic completion: "
                    + str(failure or completed)[:300]
                )
            if record:
                metrics["chat"].append(time.perf_counter() - started)
                if admitted is not None:
                    metrics["admission"].append(admitted)
                if first is not None:
                    metrics["first_token"].append(first)
                successful_turn_ids.append(completed.get("turn_id"))

        async def simple(kind, path, key=None, record=True):
            started = time.perf_counter()
            response = (
                await client.get(path)
                if key is None
                else await client.post(path, json={"key": key})
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"{kind} HTTP {response.status_code}: {response.text[:200]}"
                )
            value = response.json()
            if kind == "tool" and value != {"value": "synthetic"}:
                raise RuntimeError("Tool returned an invalid fixture result")
            if record:
                metrics[kind].append(time.perf_counter() - started)

        await chat(False)
        await simple(
            "listing",
            "/api/v1/harness-sessions?engagement_id=fixture-project",
            record=False,
        )
        await simple("auth", "/api/v1/auth/devices", record=False)
        await simple("tool", "/fixture/tool", "warmup", False)
        await asyncio.sleep(0.2)
        response = await client.post("/fixture/measure")
        response.raise_for_status()
        start = time.perf_counter()
        sampling = True

        async def sample():
            while sampling:
                try:
                    response = await client.get("/fixture/observation")
                    observations.append(
                        {"at_seconds": time.perf_counter() - start, **response.json()}
                    )
                except httpx.HTTPError as exc:
                    observations.append({"error": type(exc).__name__})
                await asyncio.sleep(1)

        sampling_task = asyncio.create_task(sample())

        async def guarded(kind, operation):
            started = time.perf_counter()
            try:
                await operation
            except httpx.HTTPStatusError as exc:
                try:
                    code = str(exc.response.json().get("code", ""))
                except ValueError:
                    code = ""
                status = exc.response.status_code
                if status == 429 or (status == 503 and code.endswith("database_busy")):
                    rejections[kind].append((status, time.perf_counter() - started))
                else:
                    errors.append({"kind": kind, "error": str(exc)[:500]})
            except Exception as exc:
                errors.append({"kind": kind, "error": str(exc)[:500]})

        async def group(index, due):
            metrics["issue_lag"].append(max(0, time.perf_counter() - due))
            await asyncio.gather(
                guarded("chat", chat()),
                guarded(
                    "listing",
                    simple(
                        "listing",
                        "/api/v1/harness-sessions?engagement_id=fixture-project",
                    ),
                ),
                guarded("auth", simple("auth", "/api/v1/auth/devices")),
                guarded("tool", simple("tool", "/fixture/tool", str(index))),
            )

        groups = []
        for index in range(args.requests):
            due = start + index / args.rate
            await asyncio.sleep(max(0, due - time.perf_counter()))
            groups.append(asyncio.create_task(group(index, due)))
        await asyncio.gather(*groups)
        elapsed = time.perf_counter() - start
        sampling = False
        await sampling_task
        performance = (await client.get("/fixture/performance")).json()
        observation = (await client.get("/fixture/observation")).json()
        await client.post("/fixture/stop")
    return {
        "label": args.label,
        "pool_size": args.pool,
        "offered_groups_per_second": args.rate,
        "offered_requests_per_kind": args.requests,
        "elapsed_seconds": elapsed,
        "host_monitor": args.host_monitor,
        "host_monitor_fixture": {
            "capacity": 4,
            "cpu_ratio": 0,
            "memory_available_ratio": 0.5,
            "interval_seconds": 0.01,
            "finite_starts_per_second": 800,
        }
        if args.host_monitor == "controlled"
        else None,
        "metrics": {name: distribution(values) for name, values in metrics.items()},
        "error_count": len(errors),
        "rejections": {
            kind: {
                "count": len(values),
                "statuses": {
                    str(status): sum(item[0] == status for item in values)
                    for status in {item[0] for item in values}
                },
                "latency": distribution([item[1] for item in values]),
            }
            for kind, values in rejections.items()
        },
        "errors": errors[:30],
        "final_observation": observation,
        "observations": observations,
        "database_telemetry": performance,
        "semantic_validation": "exact final message, no SSE error, exact tool result",
    }


def run_parent(args):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    configured = os.environ.get("NEBULA_TEST_POSTGRES_URL")
    if not configured:
        raise ValueError(
            "NEBULA_TEST_POSTGRES_URL must identify an owned disposable database"
        )
    url = make_url(configured)
    schema = "nebula_bench_" + uuid4().hex
    engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=10)
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine.dispose()
    with tempfile.TemporaryDirectory(prefix="nebula-mixed-load-") as temporary:
        root = Path(temporary)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        environment = dict(os.environ)
        environment["NEBULA_BENCHMARK_SCOPED_URL"] = url.update_query_dict(
            {"options": f"-csearch_path={schema}"}
        ).render_as_string(hide_password=False)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--source",
            str(args.source),
            "--pool",
            str(args.pool),
            "--fixture-dir",
            str(root),
            "--port",
            str(port),
            "--host-monitor",
            args.host_monitor,
        ]
        log_path = args.output.with_suffix(".log")
        try:
            with log_path.open("w") as log:
                process = subprocess.Popen(
                    command, cwd=args.source, env=environment, stdout=log, stderr=log
                )
                try:
                    result = asyncio.run(
                        drive(args, f"http://127.0.0.1:{port}", process)
                    )
                    process.wait(timeout=30)
                    result["worker_exit_code"] = process.returncode
                    result["shutdown"] = json.loads(
                        (root / "shutdown.json").read_text()
                    )
                    result["provenance"] = source_identity(args.source)
                    result["provenance"]["host_monitor"] = args.host_monitor
                    result["provenance"]["event_loop"] = result["final_observation"][
                        "event_loop"
                    ]
                    result["boundary"] = (
                        "TCP Core; independent fixed-arrival load-generator process"
                    )
                    result["fixture"] = (
                        "12 sessions and 2400 COMPLETE historical harness turns; inert 20ms provider; persisted local no-op tool"
                    )
                    with engine.connect() as connection:
                        for name, kind in [
                            ("chat_turns", "chat_turns"),
                            ("tool_calls", "tool_calls"),
                        ]:
                            rows = connection.execute(
                                text(
                                    f"SELECT payload->>'status',count(*) FROM \"{schema}\".entities WHERE kind=:kind GROUP BY 1"
                                ),
                                {"kind": kind},
                            ).all()
                            result["stored_" + name] = dict(rows)
                    for kind, metric in (
                        ("chat_turns", "chat"),
                        ("tool_calls", "tool"),
                    ):
                        expected = result["metrics"][metric]["n"] + 1
                        actual = result["stored_" + kind].get("complete", 0)
                        if actual != expected:
                            result["errors"].append(
                                {
                                    "kind": "durability",
                                    "entity_kind": kind,
                                    "expected_complete": expected,
                                    "actual_complete": actual,
                                }
                            )
                            result["error_count"] += 1
                    args.output.write_text(json.dumps(result, indent=2) + "\n")
                    print(
                        json.dumps(
                            {
                                key: result[key]
                                for key in (
                                    "label",
                                    "metrics",
                                    "error_count",
                                    "shutdown",
                                )
                            }
                        ),
                        flush=True,
                    )
                    return (
                        0
                        if result["error_count"] == 0 and process.returncode == 0
                        else 1
                    )
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
        finally:
            with engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--pool", type=int, default=15)
    parser.add_argument("--rate", type=float, default=4)
    parser.add_argument("--requests", type=int, default=180)
    parser.add_argument(
        "--host-monitor", choices=["system", "controlled"], default="system"
    )
    parser.add_argument("--label", default="mixed-load")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.source = args.source.resolve()
    if args.worker:
        import uvicorn

        # Match the real CLI's default loop for both source revisions. Select
        # before constructing engines, sessions, or other async resources.
        uvicorn.Config("nebula.v3.api:create_app").setup_event_loop()
        asyncio.run(serve_fixture(args))
        return 0
    if (
        args.output is None
        or args.pool < 1
        or not math.isfinite(args.rate)
        or args.rate <= 0
        or not 1 <= args.requests <= 10000
    ):
        parser.error(
            "supply --output, a positive finite pool/rate, and 1..10000 requests per kind"
        )
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    return run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
