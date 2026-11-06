"""Compare synthetic Core workloads across source trees and bounded pools.

NEBULA_TEST_POSTGRES_URL must name a disposable PostgreSQL database. Each run
creates an isolated schema and removes only that schema on exit. Providers and
tools are inert local fixtures. Request timings use the actual Core ASGI app;
they exclude TCP and browser rendering and do not replace LAN browser evidence.
"""


def main() -> int:
    import argparse, asyncio, contextvars, inspect, json, os, pathlib, sys, time, statistics, uuid
    import hashlib
    import platform
    import subprocess

    def resident_kib():
        try:
            for line in pathlib.Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        except OSError:
            return None

    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--pool", type=int, default=15)
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument("--rounds", type=int, default=10)
    p.add_argument("--output", required=True)
    p.add_argument("--seconds", type=float)
    a = p.parse_args()
    source_root = pathlib.Path(a.source).resolve()
    source_digest = hashlib.sha256()
    for source_file in sorted((source_root / "src/nebula/v3").rglob("*.py")):
        source_digest.update(str(source_file.relative_to(source_root)).encode())
        source_digest.update(source_file.read_bytes())
    provenance = {
        "revision": subprocess.check_output(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True
        ).strip(),
        "runtime_source_sha256": source_digest.hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "available_cpus": len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else os.cpu_count(),
        "fixture": "12 harness sessions, 2400 matching historical turns, one inert 20 ms provider, persisted no-op local read tools",
        "boundary": "Core ASGI requests; excludes TCP and browser rendering",
    }
    sys.path.insert(0, str(pathlib.Path(a.source) / "src"))
    import httpx
    from sqlalchemy import create_engine, insert, text
    from sqlalchemy.engine import make_url
    import nebula.v3.database as dbmod
    from nebula.v3.database import Database, EntityRow
    from nebula.v3.storage import NebulaStore
    from nebula.v3.api import create_app
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.domain import (
        Engagement,
        ProviderProfile,
        HarnessSession,
        HarnessTurn,
        HarnessTurnOrigin,
        ScopePolicy,
        RiskClass,
    )
    from nebula.v3.providers import (
        ModelProvider,
        ModelResponse,
        ModelUsage,
        ProviderConfig,
        ProviderKind,
        ModelCapabilities,
        ProviderHealth,
    )
    from nebula.v3.tools import (
        ToolRegistry,
        ToolSpec,
        AnalysisTool,
        ToolBroker,
        StoreToolLedger,
        ToolInvocation,
    )
    from nebula.v3.policy import PolicyEngine
    from nebula.v3.sandbox import AnalysisOnlyRunner
    import nebula.v3.chat as chatmod

    url = make_url(os.environ["NEBULA_TEST_POSTGRES_URL"])
    schema = "bench_" + uuid.uuid4().hex
    admin = create_engine(url, pool_size=1, max_overflow=0)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    admin.dispose()
    scoped = url.update_query_dict(
        {"options": f"-csearch_path={schema}"}
    ).render_as_string(hide_password=False)
    async_native = (
        inspect.iscoroutinefunction(Database.initialize)
        if hasattr(Database, "initialize")
        else False
    )
    if async_native:
        from nebula.v3.database_pool import PoolSettings

        database = Database(scoped, pool_settings=PoolSettings(5, 12, 2, a.pool))
    else:
        original = dbmod.create_engine

        def bounded(*args, **kwargs):
            return original(
                *args, **kwargs, pool_size=a.pool, max_overflow=0, pool_timeout=30
            )

        dbmod.create_engine = bounded
        database = Database(scoped)

    async def call(function, *args, **kwargs):
        value = function(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value

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

    active = contextvars.ContextVar("sample", default=None)

    async def main():
        store = NebulaStore(database)
        project = await call(
            store.create, Engagement(id="fixture-project", name="Mixed benchmark")
        )
        await call(
            store.create,
            ProviderProfile(
                id="fixture-provider",
                name="Inert",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a"],
                metadata={"default_model": "model-a"},
            ),
        )
        harnesses = await call(
            store.create_many,
            [
                HarnessSession(
                    id=f"harness-{i}",
                    engagement_id=project.id,
                    harness_profile_id="inert-harness",
                    model="fixture",
                )
                for i in range(12)
            ],
        )
        rows = []
        for hs in harnesses:
            for i in range(200):
                entity = HarnessTurn(
                    engagement_id=project.id,
                    harness_session_id=hs.id,
                    origin=HarnessTurnOrigin.ANALYSIS,
                    prompt=f"Synthetic turn {i}",
                )
                rows.append(
                    dict(
                        id=entity.id,
                        kind=entity.entity_kind,
                        engagement_id=entity.engagement_id,
                        revision=1,
                        payload=entity.model_dump(mode="json"),
                        harness_session_id=hs.id,
                        created_at=entity.created_at,
                        updated_at=entity.updated_at,
                    )
                )
        if async_native:
            async with database.session() as session:
                await session.execute(insert(EntityRow), rows)
        else:
            with database.session() as session:
                session.execute(insert(EntityRow), rows)
        provider = FixtureProvider()
        chatmod.provider_from_profile = lambda _: provider
        registry = ToolRegistry()

        async def safe_read(_):
            await asyncio.sleep(0.001)
            return {"value": "synthetic"}

        registry.register(
            AnalysisTool(
                ToolSpec(
                    name="fixture_read",
                    description="Read synthetic fixture",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    risk_class=RiskClass.LOCAL_READ,
                ),
                safe_read,
            )
        )
        broker = ToolBroker(
            registry=registry,
            policy_engine=PolicyEngine(),
            runner=AnalysisOnlyRunner(),
            ledger=StoreToolLedger(store, enforce_run_budget=False),
            workspace_resolver=lambda _: pathlib.Path(a.output).parent,
        )
        app = create_app(
            store,
            artifact_store=ArtifactStore(
                pathlib.Path(a.output).parent / (a.label + "-artifacts")
            ),
            auth_token="synthetic-benchmark-token",
        )

        async def traced(scope, receive, send):
            async def observe(message):
                sample = active.get()
                if sample is not None and message["type"] == "http.response.body":
                    chunk = message.get("body", b"")
                    if b"event: admitted" in chunk and "admission" not in sample:
                        sample["admission"] = time.perf_counter() - sample["start"]
                    if b"event: delta" in chunk and "first_token" not in sample:
                        sample["first_token"] = time.perf_counter() - sample["start"]
                await send(message)

            await app(scope, receive, observe)

        metrics = {
            "chat": [],
            "admission": [],
            "first_token": [],
            "listing": [],
            "auth": [],
            "tool": [],
            "lag": [],
        }
        errors = []
        running = True

        async def heartbeat():
            due = time.perf_counter() + 0.01
            while running:
                await asyncio.sleep(max(0, due - time.perf_counter()))
                now = time.perf_counter()
                metrics["lag"].append(max(0, now - due))
                due = now + 0.01

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=traced),
                base_url="http://fixture.test",
                headers={"Authorization": "Bearer synthetic-benchmark-token"},
                timeout=60,
            ) as client:

                async def chat():
                    sample = {"start": time.perf_counter()}
                    token = active.set(sample)
                    try:
                        response = await client.post(
                            "/api/v1/chat/completions",
                            json={
                                "engagement_id": project.id,
                                "provider_id": "fixture-provider",
                                "model": "model-a",
                                "include_knowledge": False,
                                "stream": True,
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "Return the fixture response.",
                                    }
                                ],
                            },
                        )
                        if (
                            response.status_code != 200
                            or "event: done" not in response.text
                        ):
                            errors.append(
                                {
                                    "kind": "chat",
                                    "status": response.status_code,
                                    "body": response.text[-400:],
                                }
                            )
                        else:
                            metrics["chat"].append(
                                time.perf_counter() - sample["start"]
                            )
                            for name in ("admission", "first_token"):
                                if name in sample:
                                    metrics[name].append(sample[name])
                    finally:
                        active.reset(token)

                async def get(kind, path):
                    start = time.perf_counter()
                    response = await client.get(path)
                    if response.status_code == 200:
                        metrics[kind].append(time.perf_counter() - start)
                    else:
                        errors.append(
                            {
                                "kind": kind,
                                "status": response.status_code,
                                "body": response.text[-200:],
                            }
                        )

                async def tool(n):
                    start = time.perf_counter()
                    try:
                        await broker.execute(
                            ToolInvocation(
                                engagement_id=project.id,
                                run_id=f"fixture-run-{n}",
                                tool_name="fixture_read",
                                workspace=pathlib.Path(a.output).parent,
                                arguments={},
                                idempotency_key=f"fixture-{n}",
                            ),
                            ScopePolicy(engagement_id=project.id),
                        )
                        metrics["tool"].append(time.perf_counter() - start)
                    except Exception as exc:
                        errors.append({"kind": "tool", "error": repr(exc)})

                # Warm bootstrap, startup recovery and query compilation before measurement.
                await chat()
                await get(
                    "listing", "/api/v1/harness-sessions?engagement_id=fixture-project"
                )
                await get("auth", "/api/v1/auth/devices")
                await tool("warmup")
                for values in metrics.values():
                    values.clear()
                starting_rss = resident_kib()
                monitor = asyncio.create_task(heartbeat())
                start = time.perf_counter()

                async def worker(index):
                    step = 0
                    while (
                        (time.perf_counter() - start < a.seconds)
                        if a.seconds
                        else (step < a.rounds)
                    ):
                        await asyncio.gather(
                            chat(),
                            get(
                                "listing",
                                "/api/v1/harness-sessions?engagement_id=fixture-project",
                            ),
                            get("auth", "/api/v1/auth/devices"),
                            tool(f"{index}-{step}"),
                        )
                        step += 1

                await asyncio.gather(*(worker(i) for i in range(a.concurrency)))
                elapsed = time.perf_counter() - start
                running = False
                await monitor
                if async_native:
                    pool = database.pool_snapshot()
                else:
                    pool = {
                        "configured_size": database.engine.pool.size(),
                        "checked_out": database.engine.pool.checkedout(),
                        "max_overflow": database.engine.pool._max_overflow,
                    }

                def summary(values):
                    ordered = sorted(values)
                    return {
                        "n": len(values),
                        "p50_ms": round(statistics.median(values) * 1000, 3)
                        if values
                        else None,
                        "p95_ms": round(
                            ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
                            * 1000,
                            3,
                        )
                        if values
                        else None,
                        "max_ms": round(max(values) * 1000, 3) if values else None,
                    }

                result = {
                    "label": a.label,
                    "source": a.source,
                    "pool_size": a.pool,
                    "concurrency": a.concurrency,
                    "rounds": a.rounds,
                    "requested_seconds": a.seconds,
                    "elapsed_seconds": elapsed,
                    "metrics": {k: summary(v) for k, v in metrics.items()},
                    "errors": errors[:20],
                    "error_count": len(errors),
                    "rejection_count": sum(
                        error.get("status") in (429, 503) for error in errors
                    ),
                    "provenance": provenance,
                    "rss_kib_start": starting_rss,
                    "rss_kib_end": resident_kib(),
                    "database_telemetry": database.performance.snapshot(),
                    "pool": pool,
                }
                pathlib.Path(a.output).write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result), flush=True)

    async def run():
        try:
            await main()
            result = json.loads(pathlib.Path(a.output).read_text())
            result["pool"]["checked_out_after_shutdown"] = (
                database.pool_snapshot()["checked_out"]
                if async_native
                else database.engine.pool.checkedout()
            )
            pathlib.Path(a.output).write_text(json.dumps(result, indent=2) + "\n")
        finally:
            await call(database.dispose)

    try:
        asyncio.run(run())
    finally:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
