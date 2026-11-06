"""Compare synthetic control-plane reads through authenticated disposable LAN Core.

Use baseline/candidate source on PYTHONPATH. Each run owns its data and process.
The diagnostic POST invokes real Butler capability gateways, while production
GET routes exercise chat inspection, children, MCP candidates and search status.
"""

from __future__ import annotations

import argparse
import asyncio
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
import time


def summary(values):
    ordered = sorted(values)
    return {
        "count": len(values),
        "median_ms": round(statistics.median(values), 3) if values else 0,
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3)
        if values
        else 0,
        "max_ms": round(max(values), 3) if values else 0,
    }


async def serve(args):
    from contextlib import asynccontextmanager
    import uvicorn
    from fastapi import Header, HTTPException
    from nebula.v3.api import create_app
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.butler_authority import policy_id
    from nebula.v3.butler_capabilities import ButlerCapabilities
    from nebula.v3.butler_global_capabilities import ButlerGlobalActions
    from nebula.v3.domain import (
        ButlerAuthorityPolicy,
        ChatGoal,
        ChatMessage,
        ChatSession,
        Engagement,
        McpCapabilitySnapshot,
        McpServerProfile,
        McpToolSnapshot,
        ProviderProfile,
        ScopePolicy,
        utc_now,
    )
    from nebula.v3.storage import NebulaStore

    root = args.directory
    workspace = root / "workspace"
    workspace.mkdir()
    store = NebulaStore(root / "core.db")
    project = await store.create(
        Engagement(
            id="audit-project",
            name="Synthetic read audit",
            workspace_path=str(workspace),
        )
    )
    other = await store.create(
        Engagement(id="audit-other", name="Synthetic unrelated history")
    )
    provider = await store.create(
        ProviderProfile(
            id="audit-provider",
            name="Inert fixture",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["fixture-model"],
        )
    )
    chat = await store.create(
        ChatSession(
            id="audit-chat",
            engagement_id=project.id,
            title="Synthetic main chat",
            provider_profile_id=provider.id,
            model="fixture-model",
        )
    )
    (
        await store.create(
            ButlerAuthorityPolicy(
                id=policy_id(project.id), engagement_id=project.id, authority="act"
            )
        )
    )
    (
        await store.create(
            ChatGoal(
                engagement_id=project.id,
                session_id=chat.id,
                objective="Synthetic parent",
                completion_criteria=["Read only"],
            )
        )
    )
    tools = [
        McpToolSnapshot(
            name=f"read_{index}",
            description="Synthetic metadata " + "d" * 100,
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "q" * 100}},
            },
            read_only=True,
        )
        for index in range(8)
    ]
    (
        await store.create_many(
            [
                McpServerProfile(
                    id=f"audit-mcp-{index:04}",
                    name=f"fixture-{index:04}",
                    transport="stdio",
                    command="/bin/true",
                    enabled=index == 2534,
                    trusted_stdio=True,
                    capabilities=McpCapabilitySnapshot(
                        checked_at=utc_now(), tools=tools
                    ),
                )
                for index in range(2535)
            ]
        )
    )
    (
        await store.create_many(
            [
                ChatGoal(
                    engagement_id=other.id,
                    session_id=f"unrelated-{index}",
                    objective="Unrelated draft",
                    completion_criteria=["Read only"],
                )
                for index in range(2535)
            ]
        )
    )
    async with store.transaction() as transaction:
        (
            await transaction.add_chat_messages(
                [
                    ChatMessage(
                        engagement_id=project.id,
                        session_id=chat.id,
                        sequence=index + 1,
                        role="assistant",
                        content="Synthetic " + "c" * 1024,
                    )
                    for index in range(5000)
                ]
            )
        )
    (
        await store.create_many(
            [
                ScopePolicy(engagement_id=f"unrelated-{index}", web_search=False)
                for index in range(2535)
            ]
        )
    )
    (
        await store.create_many(
            [
                ChatSession(
                    engagement_id=other.id,
                    title=f"Unrelated {index}",
                    provider_profile_id=provider.id,
                    model="fixture-model",
                )
                for index in range(2535)
            ]
        )
    )
    artifacts = ArtifactStore(root / "artifacts")
    app = create_app(
        store,
        artifact_store=artifacts,
        auth_token=os.environ["NEBULA_READ_AUDIT_TOKEN"],
        execution_data_root=root / "execution",
    )
    work = app.state.control_assistant_work
    interactive = ButlerCapabilities(store, artifacts, project.id, work=work)
    global_actions = ButlerGlobalActions(
        store, work, root, "read-only-fixture", runtime_local=True
    )
    lag = []
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):

            async def heartbeat():
                while True:
                    started = time.perf_counter()
                    await asyncio.sleep(0.01)
                    lag.append(max(0, (time.perf_counter() - started - 0.01) * 1000))

            monitor = asyncio.create_task(heartbeat())
            try:
                yield
            finally:
                monitor.cancel()
                args.output.with_suffix(".lag.json").write_text(
                    json.dumps(summary(lag))
                )

    app.router.lifespan_context = lifespan

    @app.post("/__synthetic_read_audit__", include_in_schema=False)
    async def inspect(payload: dict, authorization: str = Header(default="")):
        if not secrets.compare_digest(
            authorization, "Bearer " + os.environ["NEBULA_READ_AUDIT_TOKEN"]
        ):
            raise HTTPException(401)
        arguments = {"session_id": chat.id}
        if payload.get("entry") == "global":
            return await global_actions.work_read(
                {
                    "project_id": project.id,
                    "operation": "nebula.work.chat_settings",
                    "arguments": arguments,
                }
            )
        return await interactive.invoke(
            {"operation": "nebula.work.chat_settings", "arguments": arguments},
            change=False,
        )

    (
        await uvicorn.Server(
            uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
        ).serve()
    )
    (await store.database.dispose())


async def drive(args, token, process):
    import httpx

    origin = f"http://{args.host}:{args.port}"
    async with httpx.AsyncClient(
        base_url=origin,
        headers={"Authorization": "Bearer " + token},
        timeout=180,
        limits=httpx.Limits(max_connections=256, max_keepalive_connections=256),
        trust_env=False,
    ) as client:
        for _ in range(480):
            if process.poll() is not None:
                raise RuntimeError("Disposable Core exited; inspect the retained log")
            try:
                ready = await client.get("/api/v1/auth/devices", timeout=0.5)
                if ready.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Disposable Core did not become ready")
        unauthorized = await client.get(
            "/api/v1/auth/devices",
            headers={"Authorization": "Bearer invalid-synthetic-token"},
        )
        assert unauthorized.status_code == 401
        measured, outputs = {}, {}
        paths = {
            "chat_preview": "/api/v1/voice/work/chats/audit-chat?engagement_id=audit-project",
            "goal_children": "/api/v1/chat/sessions/audit-chat/goal/children",
            "scope_candidates": "/api/v1/engagements/audit-project/scope/tool-candidates",
            "search_status": "/api/v1/integrations/web-search",
        }
        for name, path in paths.items():
            samples = []
            for _ in range(3):
                started = time.perf_counter()
                response = await client.get(path)
                response.raise_for_status()
                samples.append((time.perf_counter() - started) * 1000)
                value = response.json()
            measured[name] = summary(samples)
            outputs[name] = (
                {"recent_messages": len(value["recent_messages"])}
                if name == "chat_preview"
                else {
                    "count": len(value),
                    "late_server_visible": any(
                        row["server_id"] == "audit-mcp-2534" for row in value
                    ),
                }
                if name == "scope_candidates"
                else {"count": len(value)}
                if name == "goal_children"
                else {"projects_using": value["projects_using"]}
            )
        durations = {"settings_reads": [], "ordinary_requests": []}
        timeouts = 0

        async def settings(index):
            started = time.perf_counter()
            response = await client.post(
                "/__synthetic_read_audit__",
                json={"entry": "global" if index % 2 else "project"},
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Synthetic capability failed: {response.status_code}: {response.text}"
                )
            assert response.json()["session_id"] == "audit-chat"
            durations["settings_reads"].append((time.perf_counter() - started) * 1000)

        reads = [asyncio.create_task(settings(index)) for index in range(args.reads)]
        await asyncio.sleep(0.05)
        while (
            not all(read.done() for read in reads)
            or len(durations["ordinary_requests"]) < 20
        ):
            started = time.perf_counter()
            try:
                response = await client.get("/api/v1/auth/devices", timeout=10)
                response.raise_for_status()
                durations["ordinary_requests"].append(
                    (time.perf_counter() - started) * 1000
                )
            except httpx.TimeoutException:
                timeouts += 1
            await asyncio.sleep(0.02)
        await asyncio.gather(*reads)
        return {
            "label": args.label,
            "origin": origin,
            "source": os.environ.get("PYTHONPATH"),
            "synthetic": True,
            "auth_enabled": True,
            "unauthorized_status": unauthorized.status_code,
            "fixture": {
                "mcp_profiles": 2535,
                "tools_per_profile": 8,
                "enabled_profiles": 1,
                "messages": 5000,
                "unrelated_goals": 2535,
                "nonmatching_policies": 2535,
                "ordinary_chats": 2536,
            },
            "operations": measured,
            "outputs": outputs,
            "concurrent_reads": args.reads,
            "latencies": {name: summary(values) for name, values in durations.items()},
            "ordinary_timeouts_at_10_seconds": timeouts,
        }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--label", choices=["baseline", "candidate"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reads", type=int, default=140)
    args = parser.parse_args()
    if args.serve:
        return await serve(args)
    if args.host in {"127.0.0.1", "localhost", "0.0.0.0", "::"}:
        parser.error("Use the executor's non-loopback LAN address")
    if not ipaddress.ip_address(args.host).is_private:
        parser.error("Bind only to a private test LAN address")
    with socket.socket() as selected:
        selected.bind((args.host, 0))
        args.port = selected.getsockname()[1]
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="nebula-read-audit-") as directory:
        root = Path(directory)
        environment = dict(
            os.environ,
            NEBULA_READ_AUDIT_TOKEN=token,
            XDG_DATA_HOME=str(root / "data"),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_CACHE_HOME=str(root / "cache"),
        )
        with args.output.with_suffix(".log").open("w") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    __file__,
                    "--serve",
                    "--directory",
                    directory,
                    "--host",
                    args.host,
                    "--port",
                    str(args.port),
                    "--label",
                    args.label,
                    "--output",
                    str(args.output),
                ],
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                result = await drive(args, token, process)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            result["loop_lag"] = json.loads(
                args.output.with_suffix(".lag.json").read_text()
            )
            result["server_exit_code"] = process.returncode
        result["temporary_directory"] = directory
    result["temporary_data_removed"] = not Path(directory).exists()
    result["server_stopped"] = process.poll() is not None
    source = Path(os.environ["PYTHONPATH"]) / "nebula" / "v3"
    result["source_files_sha256"] = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        if (source / name).is_file()
        else None
        for name in (
            "api.py",
            "storage.py",
            "control_assistant_work.py",
            "harnesses.py",
            "butler_global_capabilities.py",
            "temporary_chat.py",
            "read_workers.py",
        )
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    if args.label == "candidate":
        assert result["ordinary_timeouts_at_10_seconds"] == 0, result
        assert result["latencies"]["ordinary_requests"]["max_ms"] < 1000, result
        assert result["loop_lag"]["max_ms"] < 1000, result
        assert result["outputs"]["scope_candidates"]["late_server_visible"], result


if __name__ == "__main__":
    asyncio.run(main())
