"""Compare harness tool bookkeeping through an authenticated disposable LAN Core.

Run this same driver with baseline/candidate source on PYTHONPATH. All projects,
chat history and tool output are synthetic; the inert adapter executes no tools.
The driver owns and removes its Core process and temporary data directory.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import secrets
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def summary(values):
    v = sorted(values)
    return {
        "count": len(v),
        "median_ms": statistics.median(v) if v else 0,
        "p95_ms": v[min(len(v) - 1, int(len(v) * 0.95))] if v else 0,
        "max_ms": max(v) if v else 0,
    }


def serve(args):
    from contextlib import asynccontextmanager
    import uvicorn
    from sqlalchemy import event
    from nebula.v3.api import create_app
    from nebula.v3.domain import (
        ChatSession,
        Engagement,
        HarnessProfile,
        HarnessSession,
        ToolCall,
    )
    from nebula.v3.harnesses import (
        HarnessAdapter,
        HarnessConnection,
        HarnessEvent,
        HarnessRuntimeService,
    )
    from nebula.v3.credentials import CredentialStore
    from nebula.v3.storage import NebulaStore

    root = Path(args.directory)
    store = NebulaStore(root / "core.db")
    project = store.create(
        Engagement(
            id="eng-a", name="Synthetic tool event load", workspace_path=str(root)
        )
    )
    profile = store.create(
        HarnessProfile(
            id="harness-a",
            name="Inert synthetic harness",
            kind="codex_app_server",
            executable="/bin/true",
            default_model="test-model",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    from nebula.v3.artifacts import ArtifactStore

    for index in range(args.chats):
        hs = store.create(
            HarnessSession(
                id=f"harness-{index}",
                engagement_id=project.id,
                harness_profile_id=profile.id,
                model="test-model",
            )
        )
        store.create(
            ChatSession(
                id=f"chat-{index}",
                engagement_id=project.id,
                title="Synthetic history",
                backend="harness",
                harness_profile_id=profile.id,
                harness_session_id=hs.id,
                model="test-model",
            )
        )
        store.create_many(
            [
                ToolCall(
                    id=f"old-{index}-{i}",
                    engagement_id=project.id,
                    run_id="old",
                    origin="chat",
                    chat_session_id=f"chat-{index}",
                    tool_name="vendor:codex:synthetic.noop",
                    status="complete",
                    risk_class="local_read",
                    result="x" * 4096,
                    metadata={"harness_turn_id": "old-turn"},
                )
                for i in range(args.history)
            ]
        )

    class Connection(HarnessConnection):
        adapter_version = "synthetic-load"

        def __init__(self, request, measured):
            self.request = request
            self.measured = measured
            self.external_session_id = "synthetic-external-" + request.session.id
            self.interrupted = False

        async def run_turn(self, prompt, *, model, images=None):
            self.external_session_id = "synthetic-external-" + self.request.session.id
            yield HarnessEvent(
                type="started", external_session_id=self.external_session_id
            )
            for index in range(args.calls if self.measured else 0):
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
                        item_id=f"item-{index}",
                        payload={
                            "arguments": {"synthetic": True},
                            "result": "Inert synthetic result",
                        },
                    )
                    await asyncio.sleep(0)
            yield HarnessEvent(type="completed", message="Synthetic tools complete")

        async def steer(self, text):
            pass

        async def interrupt(self):
            self.interrupted = True

        async def close(self):
            pass

    class Adapter(HarnessAdapter):
        kind = profile.kind

        async def probe(self, profile, credential_store):
            raise AssertionError("This fixture must not probe an external harness")

        async def open(self, request):
            owners = store.find_entities(
                ChatSession, {"harness_session_id": request.session.id}
            )
            measured = any(
                chat.id in {f"chat-{index}" for index in range(args.chats)}
                for chat in owners
            )
            return Connection(request, measured)

    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: root,
        artifact_store=ArtifactStore(root / "artifacts"),
        adapter_factory=lambda _: Adapter(),
    )
    app = create_app(
        store,
        auth_token=os.environ["NEBULA_TOOL_BENCH_TOKEN"],
        harness_runtime_service=runtime,
        execution_data_root=root / "execution",
    )
    latencies = []
    original_record = runtime._record_tool_event

    def record(*params):
        start = time.perf_counter()
        try:
            return original_record(*params)
        finally:
            if params[0].chat_session_id in {
                f"chat-{index}" for index in range(args.chats)
            }:
                latencies.append((time.perf_counter() - start) * 1000)

    runtime._record_tool_event = record
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if (
            statement.startswith("SELECT")
            and "FROM entities" in statement
            and "tool_calls" in parameters
            and " IN (" in statement
            and not queries
        ):
            queries.append(
                {
                    "sql": statement,
                    "parameters": parameters,
                    "plan": cursor.connection.execute(
                        "EXPLAIN QUERY PLAN " + statement, parameters
                    ).fetchall(),
                }
            )

    event.listen(store.database.engine, "before_cursor_execute", capture)
    lag = []
    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def monitored(app):
        async with lifespan(app):

            async def monitor():
                while True:
                    start = time.perf_counter()
                    await asyncio.sleep(0.01)
                    lag.append(max(0, (time.perf_counter() - start - 0.01) * 1000))

            task = asyncio.create_task(monitor())
            try:
                yield
            finally:
                task.cancel()
                args.output.with_suffix(".server.json").write_text(
                    json.dumps(
                        {
                            "bookkeeping": summary(latencies),
                            "loop_lag": summary(lag),
                            "query": queries,
                        },
                        indent=2,
                    )
                )

    app.router.lifespan_context = monitored
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    store.database.dispose()


async def drive(args, token, process):
    import httpx

    origin = f"http://{args.host}:{args.port}"
    async with httpx.AsyncClient(
        base_url=origin,
        headers={"Authorization": "Bearer " + token},
        timeout=120,
        trust_env=False,
    ) as client:
        for _ in range(240):
            if process.poll() is not None:
                raise RuntimeError("Core exited; see log")
            try:
                r = await client.get("/api/v1/auth/devices", timeout=0.5)
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Core not ready")
        unauthorized = await client.get(
            "/api/v1/auth/devices", headers={"Authorization": "Bearer invalid"}
        )
        assert unauthorized.status_code == 401

        async def chat(index):
            start = time.perf_counter()
            response = await client.post(
                "/api/v1/chat/completions",
                json={
                    "backend": "harness",
                    "engagement_id": "eng-a",
                    "harness_profile_id": "harness-a",
                    "model": "test-model",
                    "session_id": f"chat-{index}",
                    "messages": [
                        {"role": "user", "content": "Run the inert synthetic tools"}
                    ],
                },
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["message"]["content"] == "Synthetic tools complete", body
            return {
                **body,
                "benchmark_elapsed_ms": (time.perf_counter() - start) * 1000,
            }

        tasks = [asyncio.create_task(chat(i)) for i in range(args.chats)]
        reads = []
        while not all(task.done() for task in tasks) or len(reads) < 20:
            start = time.perf_counter()
            response = await client.get("/api/v1/engagements/eng-a/assistant-defaults")
            assert response.status_code == 200, response.text
            reads.append((time.perf_counter() - start) * 1000)
            await asyncio.sleep(0.02)
        completed = await asyncio.gather(*tasks)
        for body in completed:
            response = await client.get(
                f"/api/v1/harness-turns/{body['harness_turn_id']}/events"
            )
            response.raise_for_status()
            events = response.json()["events"]
            tool_events = [
                item
                for item in events
                if item["type"] in ("tool_started", "tool_completed")
            ]
            assert len(tool_events) == args.calls * 2, len(tool_events)
            by_id = {}
            for item in tool_events:
                by_id.setdefault(item["tool_call_id"], []).append(item["type"])
            assert len(by_id) == args.calls
            assert all(
                kinds == ["tool_started", "tool_completed"] for kinds in by_id.values()
            )
            seq = [item["sequence"] for item in events]
            assert seq == sorted(set(seq))
            replay = await client.get(
                f"/api/v1/harness-turns/{body['harness_turn_id']}/events",
                params={"after": tool_events[-1]["sequence"]},
            )
            replay.raise_for_status()
            assert not any(
                item["type"].startswith("tool_") for item in replay.json()["events"]
            )
        return {
            "label": args.label,
            "origin": origin,
            "source": os.environ.get("PYTHONPATH"),
            "synthetic": True,
            "auth_enabled": True,
            "unauthorized_status": unauthorized.status_code,
            "history_per_chat": args.history,
            "chats": args.chats,
            "calls_per_chat": args.calls,
            "ordinary_http": summary(reads),
            "turns": summary([body["benchmark_elapsed_ms"] for body in completed]),
            "one_ledger_row_per_call": True,
            "ordered_durable_replay": True,
            "reconnect_cursor": True,
        }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--serve", action="store_true")
    p.add_argument("--directory")
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--history", type=int, default=4500)
    p.add_argument("--chats", type=int, default=3)
    p.add_argument("--calls", type=int, default=50)
    p.add_argument("--label", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.serve:
        return serve(args)
    address = ipaddress.ip_address(args.host)
    if address.is_loopback or address.is_unspecified or not address.is_private:
        p.error("Use a private non-loopback test LAN address")
    if args.history < 0 or args.chats < 1 or args.calls < 1:
        p.error("Require nonnegative history and positive chats/calls")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind((args.host, 0))
        args.port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="nebula-harness-lan-") as directory:
        env = dict(
            os.environ,
            NEBULA_TOOL_BENCH_TOKEN=token,
            XDG_DATA_HOME=directory + "/data",
            XDG_CONFIG_HOME=directory + "/config",
            XDG_CACHE_HOME=directory + "/cache",
        )
        with args.output.with_suffix(".log").open("w") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    __file__,
                    "--serve",
                    "--host",
                    args.host,
                    "--port",
                    str(args.port),
                    "--directory",
                    directory,
                    "--history",
                    str(args.history),
                    "--chats",
                    str(args.chats),
                    "--calls",
                    str(args.calls),
                    "--label",
                    args.label,
                    "--output",
                    str(args.output),
                ],
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                result = asyncio.run(drive(args, token, process))
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        result.update(json.loads(args.output.with_suffix(".server.json").read_text()))
        assert result["bookkeeping"]["count"] == args.chats * args.calls * 2, result
        result["server_exit_code"] = process.returncode
        assert process.returncode == 0, process.returncode
    result["synthetic_data_removed"] = not Path(directory).exists()
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
