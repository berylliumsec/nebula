"""Model 50 existing Core chats under heavy owned background CPU load.

Compare --policy admission-only with --policy shared-budget. Every execution
creates its own authenticated LAN Core, mode-0700 data directory and networkless
Docker compute container. Neither mode changes shipped Core behavior.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

from scripts.benchmarks.core_budget_model import (
    BudgetFull,
    ComputeClient,
    FairBudget,
    PdfGateway,
    attached_chat_owner,
    compute_server,
)
from scripts.benchmarks.core_cpu_workers import rss_bytes, summary

REPO = Path(__file__).resolve().parents[2]
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]


async def read_core_observation(
    client, path, on_failure, *, retry_status=False, **kwargs
):
    """Retry only explicitly selected, idempotent render-status observations."""
    import httpx

    attempts = 3 if retry_status else 1
    if retry_status and (
        not path.startswith("/api/v1/report-renders/") or path.endswith("/pdf")
    ):
        raise ValueError("Retries are restricted to model render-status reads")
    for attempt in range(attempts):
        try:
            response = await client.get(path, **kwargs)
        except httpx.TransportError as exc:
            # diagnostic-expected: preserve every failed observation, including
            # retries. No mutation or ordinary latency probe uses this retry.
            on_failure(
                {
                    "path": path,
                    "type": type(exc).__name__,
                    "attempt": attempt + 1,
                    "retried": attempt + 1 < attempts,
                }
            )
            if attempt + 1 == attempts:
                raise
            await asyncio.sleep(0.05)
            continue
        response.raise_for_status()
        return response.json()


def serve(args):
    from fastapi import Depends, Header, HTTPException
    from keyring.backends.null import Keyring
    import uvicorn
    from nebula.v3.api import create_app
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.butler_capabilities import ButlerCapabilities
    from nebula.v3.credentials import CredentialStore
    from nebula.v3.domain import (
        ChatSession,
        Engagement,
        HarnessProfile,
        HarnessSession,
        HarnessTurn,
        Observation,
        Report,
        ToolCall,
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
    token = os.environ.pop("NEBULA_RESOURCE_MODEL_TOKEN")
    store = NebulaStore(root / "core.db")
    artifacts = ArtifactStore(root / "artifacts")
    credentials = CredentialStore(keyring_backend=Keyring())
    project = store.create(
        Engagement(
            id="budget-project",
            name="Synthetic fifty-chat resource model",
            workspace_path=str(root),
        )
    )
    profile = store.create(
        HarnessProfile(
            id="inert-budget-harness",
            name="Inert CPU model",
            kind="codex_app_server",
            executable="/bin/true",
            default_model="fixture",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    owners = {}
    for i in range(args.chats):
        session = store.create(
            HarnessSession(
                id=f"harness-{i}",
                engagement_id=project.id,
                harness_profile_id=profile.id,
                model="fixture",
            )
        )
        chat = store.create(
            ChatSession(
                id=f"chat-{i}",
                engagement_id=project.id,
                title=f"Synthetic chat {i}",
                backend="harness",
                harness_profile_id=profile.id,
                harness_session_id=session.id,
                model="fixture",
                metadata={"initial_title_state": "operator"},
            )
        )
        owners[session.id] = chat.id
        if args.history:
            store.create_many(
                [
                    ToolCall(
                        id=f"historical-{i}-{j}",
                        engagement_id=project.id,
                        run_id="history",
                        origin="chat",
                        chat_session_id=chat.id,
                        tool_name="vendor:codex:synthetic.cpu",
                        status="complete",
                        risk_class="local_read",
                        result="synthetic saved history " * 5,
                        metadata={"harness_turn_id": "historical-turn"},
                    )
                    for j in range(args.history)
                ]
            )
    note_ids = []
    for i in range(48):
        note = store.create(
            Observation(
                id=f"note-{i}",
                engagement_id=project.id,
                observation_type="note",
                title=f"Synthetic compute note {i}",
                body=(
                    "Synthetic report CPU content with Unicode Γειά and literal <markup>. "
                    * 150
                ),
            )
        )
        note_ids.append(note.id)
    for i in range(args.pdfs):
        store.create(
            Report(
                id=f"report-{i}",
                engagement_id=project.id,
                title=f"Synthetic budget PDF {i}",
                observation_ids=note_ids,
            )
        )

    compute = ComputeClient(root / "compute.sock", token)
    budget = FairBudget(compute.run) if args.policy == "shared-budget" else None
    gateway = PdfGateway(compute, budget, REPO)
    renders = ReportRenderService(
        store=store, artifact_store=artifacts, cpu_workers=gateway
    )
    barrier = asyncio.Event()
    ready = set()
    lag = []
    counts = {
        "tool_completed": 0,
        "turn_completed": 0,
        "active_connections": 0,
        "max_active_connections": 0,
    }

    class Connection(HarnessConnection):
        adapter_version = "owned-resource-model/1"

        def __init__(self, request, owner):
            self.owner = owner
            self.external_session_id = "synthetic-" + request.session.id
            self.interrupted = False

        async def run_turn(self, prompt, *, model, images=None):
            yield HarnessEvent(
                type="started", external_session_id=self.external_session_id
            )
            if self.owner is None:
                # Auxiliary naming turns are not one of the measured chats.
                yield HarnessEvent(type="completed", message="Synthetic title")
                return
            measured = self.owner in owners.values()
            cancelling = "Cancel this synthetic turn" in prompt
            counts["active_connections"] += 1
            counts["max_active_connections"] = max(
                counts["max_active_connections"], counts["active_connections"]
            )
            try:
                if measured:
                    ready.add(self.owner)
                    await barrier.wait()
                for i in range(args.calls if measured else 3 if cancelling else 1):
                    if self.interrupted:
                        yield HarnessEvent(
                            type="interrupted", message="Synthetic cancellation"
                        )
                        return
                    yield HarnessEvent(
                        type="tool_started",
                        server_id="codex",
                        tool_name="synthetic.cpu",
                        item_id=f"item-{i}",
                        payload={"arguments": {"synthetic": True}},
                    )
                    payload = {
                        "kind": "cpu",
                        "seconds": args.cpu_seconds
                        if measured
                        else 1.75
                        if cancelling
                        else 0.05,
                    }
                    if budget:
                        value = await budget.run(self.owner, payload)
                    else:
                        value = await compute.run(payload)
                    counts["tool_completed"] += 1
                    yield HarnessEvent(
                        type="tool_completed",
                        server_id="codex",
                        tool_name="synthetic.cpu",
                        item_id=f"item-{i}",
                        payload={
                            "result": {"synthetic": True, "cpu_s": value["cpu_s"]}
                        },
                    )
                counts["turn_completed"] += 1
                yield HarnessEvent(
                    type="completed", message="Synthetic CPU work complete"
                )
            finally:
                counts["active_connections"] -= 1

        async def steer(self, text):
            pass

        async def interrupt(self):
            self.interrupted = True

        async def close(self):
            pass

    class Adapter(HarnessAdapter):
        kind = profile.kind

        async def probe(self, profile, credential_store):
            raise AssertionError(
                "External harness probes are prohibited in this fixture"
            )

        async def open(self, request):
            return Connection(request, attached_chat_owner(store, request.session.id))

    runtime = HarnessRuntimeService(
        store,
        credential_store=credentials,
        artifact_store=artifacts,
        workspace_resolver=lambda _: root,
        adapter_factory=lambda _: Adapter(),
    )
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

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):

            async def watch():
                while True:
                    start = time.monotonic()
                    await asyncio.sleep(0.01)
                    lag.append(max(0, (time.monotonic() - start - 0.01) * 1000))

            watcher = asyncio.create_task(watch())
            try:
                yield
            finally:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
        if budget:
            await budget.shutdown()
        args.output.with_suffix(".server.json").write_text(
            json.dumps(
                {
                    "budget": budget.snapshot() if budget else None,
                    "dispatch_trace": budget.trace if budget else [],
                    "counts": counts,
                    "loop_lag_ms": summary(lag),
                },
                indent=2,
            )
        )

    app.router.lifespan_context = lifespan

    @app.get("/__resource_model__/metrics", dependencies=[Depends(authorized)])
    async def metrics(reset: bool = False):
        turns = store.find_entities(
            HarnessTurn, {}, engagement_id=project.id, limit=200
        )
        value = {
            "ready": len(ready),
            "counts": dict(counts),
            "core_cpu_s": time.process_time(),
            "core_rss": rss_bytes(os.getpid()),
            "budget": budget.snapshot() if budget else None,
            "loop_lag_ms": summary(lag),
            "turns": [
                {
                    "id": t.id,
                    "chat": t.chat_session_id,
                    "status": t.status.value,
                    "host_wait": t.metadata.get("host_load_wait"),
                }
                for t in turns
            ],
        }
        if reset:
            lag.clear()
        return value

    @app.post("/__resource_model__/release", dependencies=[Depends(authorized)])
    async def release():
        if len(ready) != args.chats:
            raise HTTPException(
                409, detail="Every synthetic chat must reach its real turn first"
            )
        barrier.set()
        return {"released": len(ready)}

    @app.get("/__resource_model__/butler-host-load", dependencies=[Depends(authorized)])
    async def host_load():
        return await butler.invoke(
            {"operation": "nebula.work.host_load", "arguments": {}}, change=False
        )

    @app.post("/__resource_model__/fairness", dependencies=[Depends(authorized)])
    async def fairness():
        if budget is None or budget.jobs:
            raise HTTPException(409)
        start = len(budget.trace)
        jobs = [
            asyncio.create_task(budget.run("noisy", {"kind": "cpu", "seconds": 0.05}))
            for _ in range(24)
        ]
        jobs += [
            asyncio.create_task(
                budget.run(f"quiet-{i}", {"kind": "cpu", "seconds": 0.05})
            )
            for i in range(160)
        ]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        trace = budget.trace[start:]
        return {
            "rejected": sum(isinstance(value, BudgetFull) for value in results),
            "unexpected_errors": [
                type(value).__name__
                for value in results
                if isinstance(value, Exception) and not isinstance(value, BudgetFull)
            ],
            "trace": trace,
            "budget": budget.snapshot(),
        }

    uvicorn.run(
        app, host=args.host, port=args.port, access_log=False, log_level="warning"
    )
    store.database.dispose()


async def drive(args, token, core):
    import httpx
    from pypdf import PdfReader
    import io

    compute = ComputeClient(Path(args.directory) / "compute.sock", token)
    origin = f"http://{args.host}:{args.port}"
    progress = args.output.with_suffix(".progress.json")
    poll_failures = []
    async with httpx.AsyncClient(
        base_url=origin,
        headers={"Authorization": "Bearer " + token},
        timeout=900,
        trust_env=False,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=200),
    ) as client:

        async def get(path, **kwargs):
            record = (
                poll_failures.append
                if kwargs.get("retry_status")
                else lambda failure: None
            )
            return await read_core_observation(client, path, record, **kwargs)

        deadline = time.monotonic() + 180
        while True:
            if core.poll() is not None:
                raise RuntimeError("Owned Core exited during startup")
            try:
                initial = await get("/__resource_model__/metrics", timeout=1)
                break
            except httpx.HTTPError:
                # diagnostic-expected: this owned Core is still starting.
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.25)
        assert (
            await client.get(
                "/__resource_model__/metrics",
                headers={"Authorization": "Bearer invalid"},
            )
        ).status_code == 401
        compute_initial = await compute.request("metrics")
        quota, period = map(int, compute_initial["cpu_max"].split())
        assert quota / period == (2 if args.policy == "shared-budget" else 4)
        if args.policy == "shared-budget":
            assert int(compute_initial["memory_max"]) == 512 * 1024 * 1024
            assert int(compute_initial["pids_max"]) == 128
        host_initial = await get("/__resource_model__/butler-host-load")

        async def chat(index):
            started = time.monotonic()
            response = await client.post(
                "/api/v1/chat/completions",
                json={
                    "backend": "harness",
                    "engagement_id": "budget-project",
                    "harness_profile_id": "inert-budget-harness",
                    "model": "fixture",
                    "session_id": f"chat-{index}" if isinstance(index, int) else None,
                    "messages": [
                        {
                            "role": "user",
                            "content": "Cancel this synthetic turn"
                            if index == "cancel"
                            else "Run inert synthetic CPU work",
                        }
                    ],
                },
            )
            if index == "cancel":
                return {
                    "http_status": response.status_code,
                    "wall_s": time.monotonic() - started,
                }
            response.raise_for_status()
            value = response.json()
            assert value["message"]["content"] == "Synthetic CPU work complete", value
            return {"body": value, "wall_s": time.monotonic() - started}

        turns = [asyncio.create_task(chat(i)) for i in range(args.chats)]
        deadline = time.monotonic() + 180
        while True:
            before = await get("/__resource_model__/metrics")
            if before["ready"] == args.chats:
                break
            if any(task.done() for task in turns):
                await asyncio.gather(*(task for task in turns if task.done()))
                raise AssertionError("A measured chat ended before the load barrier")
            assert time.monotonic() < deadline, before
            progress.write_text(
                json.dumps(
                    {
                        "phase": "starting-chats",
                        "ready": before["ready"],
                        "expected": args.chats,
                    }
                )
            )
            await asyncio.sleep(0.25)
        assert before["counts"]["active_connections"] == args.chats
        assert sum(t["status"] == "running" for t in before["turns"]) == args.chats, (
            before
        )
        await get("/__resource_model__/metrics", params={"reset": "true"})
        load_start = time.monotonic()
        phase = "load"
        probes = []
        errors = []
        hosts = []
        samples = []
        stopped = asyncio.Event()

        async def sentinel(index):
            while not stopped.is_set():
                start = time.monotonic()
                try:
                    await get(
                        "/api/v1/engagements/budget-project/assistant-defaults",
                        timeout=2,
                    )
                    probes.append(
                        {
                            "at_s": time.monotonic() - load_start,
                            "ms": (time.monotonic() - start) * 1000,
                            "phase": phase,
                        }
                    )
                except httpx.HTTPError as exc:
                    # diagnostic-expected: overload failures are measured, never
                    # hidden by render-submission retries or labelled as success.
                    errors.append(
                        {
                            "at_s": time.monotonic() - load_start,
                            "type": type(exc).__name__,
                            "phase": phase,
                            "reader": index,
                        }
                    )
                await asyncio.sleep(0.05)

        async def monitor():
            while not stopped.is_set():
                start = time.monotonic()
                metric, cg, host = await asyncio.gather(
                    get("/__resource_model__/metrics", timeout=15),
                    compute.request("metrics"),
                    get("/__resource_model__/butler-host-load", timeout=15),
                )
                sample = {
                    "at_s": time.monotonic() - load_start,
                    "phase": phase,
                    "core_cpu_s": metric["core_cpu_s"],
                    "core_rss": metric["core_rss"],
                    "loop_lag_ms": metric["loop_lag_ms"],
                    "counts": metric["counts"],
                    "budget": metric["budget"],
                    "cgroup": cg,
                }
                samples.append(sample)
                hosts.append({"at_s": sample["at_s"], **host})
                progress.write_text(json.dumps(sample, indent=2))
                await asyncio.sleep(max(0.05, 1 - (time.monotonic() - start)))

        sentinels = [asyncio.create_task(sentinel(i)) for i in range(2)]
        watcher = asyncio.create_task(monitor())
        pdf_slots = asyncio.Semaphore(2)

        async def pdf(index):
            async with pdf_slots:
                response = await client.post(
                    f"/api/v1/reports/report-{index}/renders",
                    json={"report_revision": 1},
                )
                assert response.status_code == 202, response.text
                identity = response.json()["id"]
                while True:
                    saved = await get(
                        f"/api/v1/report-renders/{identity}",
                        retry_status=True,
                        timeout=5,
                    )
                    if saved["status"] in {"completed", "failed", "interrupted"}:
                        break
                    await asyncio.sleep(0.1)
                assert saved["status"] == "completed", saved
                response = await client.get(f"/api/v1/report-renders/{identity}/pdf")
                response.raise_for_status()
                assert response.content.startswith(b"%PDF-")
                reader = PdfReader(io.BytesIO(response.content))
                assert "Synthetic" in reader.pages[0].extract_text()
                cached = await client.post(
                    f"/api/v1/reports/report-{index}/renders",
                    json={"report_revision": 1},
                )
                assert cached.status_code == 202 and cached.json()["id"] == identity
                return {
                    "render_id": identity,
                    "sha256": hashlib.sha256(response.content).hexdigest(),
                    "bytes": len(response.content),
                }

        response = await client.post("/__resource_model__/release")
        response.raise_for_status()
        pdf_tasks = [asyncio.create_task(pdf(i)) for i in range(args.pdfs)]
        try:
            completed = await asyncio.gather(*turns)
            pdfs = await asyncio.gather(*pdf_tasks)
            load_wall = time.monotonic() - load_start
            phase = "drain"
            # Preserve the exact durable ledger and reconnect cursor, not only
            # adapter counters or successful HTTP completion bodies.
            for value in completed:
                identity = value["body"]["harness_turn_id"]
                events = (await get(f"/api/v1/harness-turns/{identity}/events"))[
                    "events"
                ]
                pairs = {}
                for event in events:
                    if event["type"] in {"tool_started", "tool_completed"}:
                        pairs.setdefault(event["tool_call_id"], []).append(
                            event["type"]
                        )
                assert len(pairs) == args.calls and all(
                    pair == ["tool_started", "tool_completed"]
                    for pair in pairs.values()
                )
                sequence = [event["sequence"] for event in events]
                assert sequence == sorted(set(sequence))
                replay = await get(
                    f"/api/v1/harness-turns/{identity}/events",
                    params={"after": sequence[-1]},
                )
                assert not replay["events"]
            await asyncio.sleep(2)
            final = await get("/__resource_model__/metrics")
            assert final["counts"]["turn_completed"] == args.chats
            assert final["counts"]["tool_completed"] == args.chats * args.calls
            if watcher.done():
                await watcher
            args.output.with_suffix(".load.json").write_text(
                json.dumps(
                    {
                        "policy": args.policy,
                        "chats": args.chats,
                        "calls_per_chat": args.calls,
                        "load_wall_s": load_wall,
                        "completed_chats": len(completed),
                        "completed_pdfs": pdfs,
                        "source_sha256": args.source_hashes,
                        "final_core": final,
                        "samples": samples,
                        "host_load_reads": hosts,
                        "http_failures": errors,
                        "raw_read_latencies": probes,
                    },
                    indent=2,
                )
            )
            faults = {}
            if args.faults and args.policy == "shared-budget":
                phase = "fairness"
                response = await client.post("/__resource_model__/fairness")
                response.raise_for_status()
                fairness = response.json()
                assert (
                    fairness["rejected"] == 56 and not fairness["unexpected_errors"]
                ), fairness
                owners_seen = [entry["owner"] for entry in fairness["trace"]]
                second_noisy = [
                    i for i, owner in enumerate(owners_seen) if owner == "noisy"
                ][1]
                assert all(
                    owners_seen.index(f"quiet-{i}") < second_noisy for i in range(20)
                ), owners_seen
                faults["fairness"] = fairness
                phase = "memory-fault"
                before_oom = await compute.request("metrics")
                try:
                    await compute.run({"kind": "memory", "mib": 768})
                except RuntimeError as exc:
                    # diagnostic-expected: one deliberately oversized owned job
                    # must fail inside its container, while Core stays alive.
                    fault = type(exc).__name__ + ":" + str(exc)
                else:
                    raise AssertionError(
                        "The bounded memory fault unexpectedly succeeded"
                    )
                after_oom = await compute.request("metrics")
                assert (
                    after_oom["memory_events"]["oom_kill"]
                    > before_oom["memory_events"]["oom_kill"]
                )
                await get(
                    "/api/v1/engagements/budget-project/assistant-defaults", timeout=2
                )
                assert (await compute.request("reset"))["reset"]
                assert "checksum" in await compute.run({"kind": "cpu", "seconds": 0.05})
                faults["memory"] = {
                    "error": fault,
                    "before": before_oom,
                    "after": after_oom,
                    "explicit_reset_recovered": True,
                }
            phase = "recovery"
            recovered = await asyncio.wait_for(chat("recovery"), timeout=180)
            if args.faults and args.policy == "shared-budget":
                phase = "cancel-running"
                known = {
                    turn["id"]
                    for turn in (await get("/__resource_model__/metrics"))["turns"]
                }
                cancelling = asyncio.create_task(chat("cancel"))
                async with asyncio.timeout(10):
                    while True:
                        current = await get("/__resource_model__/metrics")
                        targets = [
                            turn
                            for turn in current["turns"]
                            if turn["id"] not in known and turn["status"] == "running"
                        ]
                        if targets and current["budget"]["active_by_owner"].get(
                            targets[0]["chat"]
                        ):
                            break
                        await asyncio.sleep(0.01)
                response = await client.post(
                    f"/api/v1/harness-turns/{targets[0]['id']}/stop",
                    json={"reason": "Synthetic cancellation under resource model"},
                )
                response.raise_for_status()
                assert response.json()["status"] == "cancelled", response.text
                held = (await get("/__resource_model__/metrics"))["budget"]
                assert held["active"] == held["admitted"] == 1, held
                await asyncio.wait_for(cancelling, 10)
                async with asyncio.timeout(10):
                    while (await get("/__resource_model__/metrics"))["budget"][
                        "admitted"
                    ]:
                        await asyncio.sleep(0.05)
                settled = (await get("/__resource_model__/metrics"))["budget"]
                assert settled["cancelled_running"] == 1 and settled["discarded"] == 1
                faults["cancellation"] = {
                    "turn_id": targets[0]["id"],
                    "saved_status": response.json()["status"],
                    "held_until_compute_finished": held,
                    "settled": settled,
                }
            await asyncio.sleep(2)
            final = await get("/__resource_model__/metrics")
            compute_final = await compute.request("metrics")
            if watcher.done():
                await watcher
            assert samples and probes
        except Exception as exc:
            # diagnostic-expected: a failed observation must leave usable partial
            # measurements without turning an incomplete run into a passing one.
            args.output.with_suffix(".incomplete.json").write_text(
                json.dumps(
                    {
                        "outcome": "incomplete",
                        "error_type": type(exc).__name__,
                        "phase": phase,
                        "policy": args.policy,
                        "chats": args.chats,
                        "source_sha256": args.source_hashes,
                        "samples": samples,
                        "host_load_reads": hosts,
                        "http_failures": errors,
                        "poll_transport_failures": poll_failures,
                        "raw_read_latencies": probes,
                    },
                    indent=2,
                )
            )
            raise
        finally:
            stopped.set()
            for task in sentinels + [watcher]:
                task.cancel()
            await asyncio.gather(*sentinels, watcher, return_exceptions=True)
            for task in turns + pdf_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*turns, *pdf_tasks, return_exceptions=True)
        if final["budget"]:
            state = final["budget"]
            assert state["max_active"] <= 2 and state["max_owner_active"] == 1
            assert state["max_admitted"] <= 128 and state["max_bytes"] <= 32 << 20
            assert state["admitted"] == state["input_bytes"] == 0
        return {
            "policy": args.policy,
            "origin": origin,
            "auth_enabled": True,
            "chats": args.chats,
            "calls_per_chat": args.calls,
            "cpu_seconds_per_call": args.cpu_seconds,
            "historical_calls_per_chat": args.history,
            "load_wall_s": load_wall,
            "all_chats_running_before_release": True,
            "completed_chats": len(completed),
            "exact_tool_ledgers_and_replay": True,
            "completed_pdfs": pdfs,
            "ordinary_reads_by_phase": {
                name: summary([p["ms"] for p in probes if p["phase"] == name])
                for name in sorted({p["phase"] for p in probes})
            },
            "http_failures": errors,
            "poll_transport_failures": poll_failures,
            "host_load_reads": hosts,
            "samples": samples,
            "raw_read_latencies": probes,
            "turn_wall_ms": summary([v["wall_s"] * 1000 for v in completed]),
            "initial_core": initial,
            "before_release": before,
            "initial_compute": compute_initial,
            "initial_host_load": host_initial,
            "final_core": final,
            "final_compute": compute_final,
            "recovery_turn_s": recovered["wall_s"],
            "faults": faults,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--compute", action="store_true")
    parser.add_argument("--directory")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--policy", choices=["admission-only", "shared-budget"], default="shared-budget"
    )
    parser.add_argument("--chats", type=int, default=50)
    parser.add_argument("--calls", type=int, default=12)
    parser.add_argument("--history", type=int, default=200)
    parser.add_argument("--cpu-seconds", type=float, default=0.75)
    parser.add_argument("--pdfs", type=int, default=8)
    parser.add_argument("--faults", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.compute:
        return asyncio.run(compute_server(args.directory, args.workers))
    if args.serve:
        return serve(args)
    if not args.host or not args.output:
        parser.error("Require a private LAN host and an output path")
    address = ipaddress.ip_address(args.host)
    if not address.is_private or address.is_loopback or address.is_unspecified:
        parser.error("Use a private non-loopback LAN address")
    if (
        not 1 <= args.chats <= 100
        or not 1 <= args.calls <= 40
        or not 0 < args.cpu_seconds <= 2
        or not 0 <= args.history <= 500
        or not 0 <= args.pdfs <= 16
    ):
        parser.error("Workload exceeds the owned model bounds")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    source_paths = [Path(__file__), Path(__file__).with_name("core_budget_model.py")]
    source_paths += [
        REPO / "src/nebula/v3" / name
        for name in ("api.py", "harnesses.py", "reporting.py", "host_load.py")
    ]
    args.source_hashes = {
        str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_paths
    }
    with socket.socket() as chosen:
        chosen.bind((args.host, 0))
        args.port = chosen.getsockname()[1]
    docker_env = dict(os.environ)
    for key in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        docker_env.pop(key, None)
    token = secrets.token_urlsafe(32)
    docker_env["NEBULA_RESOURCE_MODEL_TOKEN"] = token
    container = "nebula-resource-model-" + secrets.token_hex(6)
    protected = args.policy == "shared-budget"
    with tempfile.TemporaryDirectory(prefix="nebula-resource-model-") as directory:
        args.directory = directory
        os.chmod(directory, 0o700)
        env = dict(
            os.environ,
            NEBULA_RESOURCE_MODEL_TOKEN=token,
            XDG_DATA_HOME=directory + "/data",
            XDG_CONFIG_HOME=directory + "/config",
            XDG_CACHE_HOME=directory + "/cache",
        )
        command = DOCKER + [
            "run",
            "--detach",
            "--name",
            container,
            "--network",
            "none",
            "--cpus",
            "2" if protected else "4",
            "--memory",
            "512m" if protected else "2g",
            "--memory-swap",
            "512m" if protected else "2g",
            "--pids-limit",
            "128" if protected else "256",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,src={REPO},dst=/repo,readonly",
            "--mount",
            f"type=bind,src={directory},dst=/exchange",
            "--env",
            "NEBULA_RESOURCE_MODEL_TOKEN",
            "--env",
            "PYTHONPATH=/repo:/repo/src:/repo/.venv/lib/python3.12/site-packages",
            "--env",
            "OPENBLAS_NUM_THREADS=1",
            "--env",
            "OMP_NUM_THREADS=1",
            "--env",
            "MKL_NUM_THREADS=1",
            "python:3.12-slim",
            "python",
            "/repo/scripts/benchmarks/core_resource_budget.py",
            "--compute",
            "--directory",
            "/exchange",
            "--workers",
            "2" if protected else str(args.chats + 2),
        ]
        subprocess.run(
            command, env=docker_env, check=True, capture_output=True, text=True
        )
        core = None
        result = None
        try:
            deadline = time.monotonic() + 30
            while not (Path(directory) / "compute.sock").exists():
                if time.monotonic() > deadline:
                    logs = subprocess.run(
                        DOCKER + ["logs", container],
                        env=docker_env,
                        capture_output=True,
                        text=True,
                    )
                    raise RuntimeError(
                        "Compute container did not start: " + logs.stderr[-2000:]
                    )
                time.sleep(0.1)
            core_command = [
                sys.executable,
                __file__,
                "--serve",
                "--directory",
                directory,
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--policy",
                args.policy,
                "--chats",
                str(args.chats),
                "--calls",
                str(args.calls),
                "--history",
                str(args.history),
                "--cpu-seconds",
                str(args.cpu_seconds),
                "--pdfs",
                str(args.pdfs),
                "--output",
                str(args.output),
            ]
            with args.output.with_suffix(".log").open("w") as log:
                core = subprocess.Popen(core_command, env=env, stdout=log, stderr=log)
                try:
                    result = asyncio.run(drive(args, token, core))
                finally:
                    if core.poll() is None:
                        core.send_signal(signal.SIGINT)
                    try:
                        core.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        # diagnostic-expected: only the driver's own Core is killed
                        # after its graceful shutdown deadline has expired.
                        core.kill()
                        core.wait()
        finally:
            logs = subprocess.run(
                DOCKER + ["logs", container],
                env=docker_env,
                capture_output=True,
                text=True,
            )
            args.output.with_suffix(".compute.log").write_text(
                logs.stdout + logs.stderr
            )
            removed = subprocess.run(
                DOCKER + ["rm", "--force", container],
                env=docker_env,
                capture_output=True,
                text=True,
            )
            if removed.returncode:
                raise RuntimeError("Owned model container cleanup failed")
        if result is not None:
            result["core_exit_code"] = core.returncode
            result["shutdown"] = json.loads(
                args.output.with_suffix(".server.json").read_text()
            )
            result["owned_container_removed"] = removed.returncode == 0
            assert core.returncode == 0
    result["synthetic_data_removed"] = not Path(directory).exists()
    result["source_sha256"] = args.source_hashes
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "policy": args.policy,
                "chats": args.chats,
                "load_wall_s": result["load_wall_s"],
                "reads": result["ordinary_reads_by_phase"],
                "http_failures": len(result["http_failures"]),
                "cleanup": result["synthetic_data_removed"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
