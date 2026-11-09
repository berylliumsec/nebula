"""LAN acceptance controller; each browser case gets a fresh real installation."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import sysconfig
import tempfile

if __package__ in {None, ""}:
    checkout = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(checkout), str(checkout / "src")]

from fastapi import FastAPI, HTTPException, Request
import uvicorn

from nebula.v3.domain import ChatSession
from nebula.v3.release_store import ReleaseStore
from tests.v3.runtime_continuity_bundle import (
    build_bundle,
    remove_owned_tree,
    retained_bundle,
)
from tests.v3.runtime_continuity_fixture import ContinuityFixture, completed_response
from tests.v3.runtime_continuity_allocator import release_allocator


async def serve(root: Path):
    host = os.environ.get("NEBULA_MODEL_TEST_HOST", "127.0.0.1")
    port = int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19489"))
    auth_file = Path(os.environ["NEBULA_MODEL_TEST_AUTH_FILE"])
    token = secrets.token_urlsafe(32)
    source = Path(
        os.environ.get(
            "NEBULA_ACCEPTANCE_SOURCE", str(Path(__file__).resolve().parents[2] / "src")
        )
    )
    ui = Path(
        os.environ.get(
            "NEBULA_ACCEPTANCE_UI",
            str(Path(__file__).resolve().parents[2] / "ui" / "dist"),
        )
    )
    dependencies = Path(
        os.environ.get("NEBULA_ACCEPTANCE_DEPENDENCIES", sysconfig.get_path("purelib"))
    )
    retained = ReleaseStore(root / "retained")
    bundles = []
    for version in ("acceptance-v1", "acceptance-v2"):
        replaceable = root / ("replaceable-" + version)
        built = await asyncio.to_thread(
            build_bundle,
            replaceable,
            version=version,
            source=source,
            ui=ui,
            dependencies=dependencies,
            runtime_template=bundles[0].path if bundles else None,
        )
        bundles.append(await asyncio.to_thread(retained_bundle, built, retained))
        await asyncio.to_thread(remove_owned_tree, replaceable)
    fixture = None
    allocations = {}
    serial = asyncio.Lock()

    async def finish_allocations():
        errors = []
        if fixture is not None:
            for identity in allocations:
                release_allocator(fixture.root, identity)
            for identity, task in allocations.items():
                try:
                    await completed_response(fixture.client, await task, timeout=30)
                except Exception as exc:
                    errors.append(f"{identity}: {type(exc).__name__}: {exc}")
        allocations.clear()
        return errors

    async def close_fixture():
        nonlocal fixture
        if fixture is not None:
            errors = await finish_allocations()
            try:
                await fixture.close()
            finally:
                fixture = None
            if errors:
                raise RuntimeError(
                    "Allocator cleanup was not confirmed: " + "; ".join(errors)
                )

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            try:
                await close_fixture()
            finally:
                auth_file.unlink(missing_ok=True)
                # Uvicorn re-delivers SIGTERM after its lifespan exits. Remove
                # private bundles before that default signal action.
                await asyncio.to_thread(remove_owned_tree, root)

    app = FastAPI(lifespan=lifespan)

    def authorize(request):
        if request.headers.get("authorization") != "Bearer " + token:
            raise HTTPException(401)

    @app.get("/health")
    async def health():
        return {"ready": True, "fixture": "source-runtime-continuity"}

    @app.post("/reset")
    async def reset(request: Request):
        nonlocal fixture
        authorize(request)
        body = await request.json()
        scenario = body.get("scenario")
        adapters = {
            "provider-tools-context": "acceptance_provider_tools_context.py",
            "standing-instructions-native": "acceptance_standing_instructions_native.py",
        }
        if scenario is not None and scenario not in adapters:
            raise HTTPException(400, "unknown isolated acceptance scenario")
        adapter_path = (
            bundles[-1].path / adapters[scenario] if scenario is not None else None
        )
        if adapter_path is not None and not adapter_path.is_file():
            raise HTTPException(
                503, "selected retained bundles do not contain this acceptance fixture"
            )
        async with serial:
            await close_fixture()
            selected = root / ("installation-" + secrets.token_hex(8))
            selected.mkdir()
            fixture = ContinuityFixture(
                selected / "data",
                selected / "core.db",
                bundles,
                host=host,
                port=port,
                listen_host="0.0.0.0",
            )
            await fixture.initialize(
                cold=body.get("cold", False),
                pairing=True,
                memory=body.get("memory", False),
                scenario=scenario,
            )
            await fixture.store.create(
                ChatSession(
                    id="continuity-chat",
                    engagement_id="continuity-project",
                    title="Synthetic continuity conversation",
                    provider_profile_id="continuity-provider",
                    model="model-a",
                    metadata={"initial_title_state": "operator"},
                )
            )
            scenario_receipt = None
            if scenario is not None:
                scenario_receipt = await fixture.control(
                    "initialize-scenario", session_id="continuity-chat"
                )
            if not body.get("cold", False):
                await fixture.ready()
            return {
                "token": fixture.token,
                "index_sha256": bundles[0].index_sha256,
                "source_sha256": bundles[0].source_sha256,
                "session_id": "continuity-chat",
                "origin": fixture.origin,
                "scenario": scenario,
                "scenario_receipt": scenario_receipt,
            }

    @app.post("/control")
    async def control(request: Request):
        authorize(request)
        if fixture is None:
            raise HTTPException(409, "reset the isolated fixture first")
        body = await request.json()
        operation = body.pop("operation")
        if operation == "diagnostics":
            health = await fixture.client.get("/api/v1/health", timeout=5)
            memory = await fixture.client.get("/api/v1/memory-budgets", timeout=5)
            tools = {}
            native = None
            scenario = fixture.root / "acceptance-scenario.json"
            if (
                scenario.is_file()
                and json.loads(scenario.read_text())["scenario"]
                == "provider-tools-context"
            ):
                for session_id in dict.fromkeys(
                    ["continuity-chat", body.get("session_id")]
                ):
                    if not session_id:
                        continue
                    observed = await fixture.client.post(
                        "/__acceptance/control",
                        json={
                            "operation": "tool-context",
                            "session_id": session_id,
                        },
                        timeout=5,
                    )
                    context = await fixture.client.get(
                        f"/api/v1/chat/sessions/{session_id}/context", timeout=5
                    )
                    tools[session_id] = {
                        "manifest_status": observed.status_code,
                        "manifest": observed.json()
                        if "application/json"
                        in observed.headers.get("content-type", "")
                        else observed.text[-4096:],
                        "context_status": context.status_code,
                        "context": context.json()
                        if "application/json" in context.headers.get("content-type", "")
                        else context.text[-4096:],
                    }
            if (
                scenario.is_file()
                and json.loads(scenario.read_text())["scenario"]
                == "standing-instructions-native"
            ):
                observed = await fixture.client.post(
                    "/__acceptance/control",
                    json={
                        "operation": "native-evidence",
                        "session_id": "continuity-chat",
                    },
                    timeout=5,
                )
                native = {
                    "status": observed.status_code,
                    "evidence": observed.json()
                    if "application/json" in observed.headers.get("content-type", "")
                    else observed.text[-4096:],
                }
            jobs = {}
            for identity, task in allocations.items():
                job = await fixture.client.get(
                    "/api/v1/memory-budgets/jobs/" + identity, timeout=5
                )
                jobs[identity] = {
                    "request_returned": task.done(),
                    "status": job.status_code,
                    "job": job.json(),
                }
            paths = [*fixture.root.glob("runtime/*.log")]
            paths += list(fixture.root.glob("runtime/workers/*/logs/diagnostics.log"))
            paths += list(fixture.root.glob("runtime/workers/*/logs/butler.log"))
            paths += list(fixture.root.glob("runtime/workers/*/logs/api.log"))
            logs = {}
            for path in paths[:16]:
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 8192))
                    logs[str(path.relative_to(fixture.root))] = stream.read(
                        8192
                    ).decode(errors="replace")
            return {
                "status": health.status_code,
                "health": health.json(),
                "runtime": await fixture.control("inspect"),
                "logs": logs,
                "memory": {"status": memory.status_code, "settings": memory.json()},
                "allocations": jobs,
                "provider_tools": tools,
                "native_instructions": native,
            }
        if operation in {"hold", "release"}:
            if body.get("name") not in {"hold-provider"}:
                raise HTTPException(400, "unknown fixture barrier")
            getattr(fixture, operation)(body["name"])
            return {"done": True}
        if operation == "start-allocation":
            identity = body["job_id"]
            if (
                identity in allocations
                or not (fixture.root / "allocator.json").exists()
            ):
                raise HTTPException(
                    409, "a fresh physical allocator fixture is required"
                )
            allocations[identity] = asyncio.create_task(
                fixture.client.post(
                    "/api/v1/engagements/continuity-project/acceptance-allocation",
                    json={"job_id": identity, "hold": True},
                    headers={"Idempotency-Key": identity},
                )
            )
            return {"started": True}
        if operation in {"release-allocation", "allocation-result", "physical-group"}:
            identity = body["job_id"]
            if identity not in allocations:
                raise HTTPException(404, "unknown fixture allocation")
            if operation == "release-allocation":
                release_allocator(fixture.root, identity)
                return {"released": True}
            if operation == "allocation-result":
                return (
                    await completed_response(
                        fixture.client, await allocations[identity], timeout=30
                    )
                ).json()
            response = await fixture.client.get(
                "/api/v1/memory-budgets/jobs/" + identity
            )
            response.raise_for_status()
            group_id = response.json()["evidence"]["group_id"]
            selected = json.loads((fixture.root / "allocator.json").read_text())
            environment = {
                name: value
                for name, value in os.environ.items()
                if name
                not in {
                    "DOCKER_HOST",
                    "DOCKER_CONTEXT",
                    "DOCKER_TLS",
                    "DOCKER_TLS_VERIFY",
                    "DOCKER_CERT_PATH",
                }
            }
            inspected = await asyncio.to_thread(
                subprocess.run,
                [
                    selected["runtime"],
                    "--host=" + selected["socket"],
                    "inspect",
                    group_id,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
            )
            group = json.loads(inspected.stdout)[0]
            if (
                group["Id"] != group_id
                or group["Config"]["Labels"].get("nebula.memory.job") != identity
            ):
                raise HTTPException(
                    409, "physical group identity did not match the fixture job"
                )
            return {
                "id": group["Id"],
                "memory_bytes": group["HostConfig"]["Memory"],
                "running": group["State"]["Running"],
                "oom_killed": group["State"]["OOMKilled"],
            }
        if operation == "restart-supervisor":
            await fixture.kill_supervisor()
            await fixture.start()
            return {"restarted": True}
        return await fixture.control(operation, **body)

    @app.post("/cleanup")
    async def cleanup(request: Request):
        authorize(request)
        async with serial:
            await close_fixture()
        return {"cleaned": True}

    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(
        json.dumps({"token": token, "control_origin": f"http://127.0.0.1:{port + 1}"})
    )
    auth_file.chmod(0o600)
    await uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port + 1, log_level="warning")
    ).serve()


def main():
    with tempfile.TemporaryDirectory(prefix="nebula-continuity-lan-") as directory:
        asyncio.run(serve(Path(directory)))


if __name__ == "__main__":
    main()
