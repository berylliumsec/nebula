"""Own exactly one disposable source-runtime continuity installation."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
from uuid import uuid4

import httpx
from sqlalchemy import select

from nebula.v3.database import Database, RuntimeProcessRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import Engagement, ProviderProfile, RunnerProfile
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import GIB, MemoryAdministratorLimits, MemoryBudgetUpdate
from nebula.v3.process_identity import process_birth, same_process, signal_process
from nebula.v3.storage import NebulaStore
from tests.v3.runtime_continuity_bundle import AcceptanceBundle


async def completed_response(client, response, *, timeout=90):
    """Read one accepted command's outcome without another effect submission."""
    if response.status_code == 202:
        document = response.json()
        command_id = document.get("command_id") or document["receipt"]["command_id"]
        async with asyncio.timeout(timeout):
            while response.status_code == 202:
                response = await client.get(
                    f"/api/v1/runtime/requests/{command_id}/response"
                )
                if response.status_code == 202:
                    await asyncio.sleep(0.05)
    assert response.status_code in {200, 201}, response.text
    return response


class ContinuityFixture:
    def __init__(
        self,
        root: Path,
        database,
        bundles: list[AcceptanceBundle],
        *,
        host="127.0.0.1",
        port=None,
        listen_host=None,
    ):
        self.root, self.bundles, self.host = root, bundles, host
        self.listen_host = listen_host or host
        self.pool_settings = PoolSettings(8, 12, 8, 3)
        self.database = Database(database, pool_settings=self.pool_settings)
        self.database_ready = False
        self.store = NebulaStore(self.database)
        self.token = "synthetic-continuity-" + uuid4().hex
        self.administrator = MemoryAdministratorLimits(
            capacity_bytes=16 * GIB,
            maximum_job_bytes=8 * GIB,
            maximum_aggregate_bytes=14 * GIB,
            minimum_reserve_bytes=2 * GIB,
            maximum_swap_bytes=2 * GIB,
        )
        self.budgets = MemoryBudgetService(
            self.database, administrator=lambda: self.administrator
        )
        if port is None:
            with socket.socket() as listener:
                listener.bind((host, 0))
                port = listener.getsockname()[1]
        self.port = port
        self.origin = f"http://{host}:{self.port}"
        self.process: subprocess.Popen | None = None
        self.birth: str | None = None
        self.client = httpx.AsyncClient(
            base_url=self.origin,
            headers={"Authorization": "Bearer " + self.token},
            timeout=90,
            trust_env=False,
        )

    async def initialize(
        self, *, cold=False, pairing=False, memory=False, scenario=None
    ):
        self.root.mkdir(parents=True)
        if scenario is not None:
            if scenario not in {
                "provider-tools-context",
                "standing-instructions-native",
            }:
                raise ValueError("unknown isolated acceptance scenario")
            scenario_file = self.root / "acceptance-scenario.json"
            scenario_file.write_text(json.dumps({"scenario": scenario}))
            scenario_file.chmod(0o600)
        await self.database.initialize()
        self.database_ready = True
        await self.store.create(
            Engagement(id="continuity-project", name="Synthetic continuity")
        )
        await self.store.create(
            Engagement(id="pinned-project", name="Synthetic pinned work")
        )
        await self.store.create(
            ProviderProfile(
                id="continuity-provider",
                name="Inert continuity provider",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a"],
            )
        )
        if memory:
            from tests.v3.runtime_continuity_allocator import prepare_allocator

            runtime = os.environ["NEBULA_TEST_MEMORY_RUNTIME"]
            image = os.environ["NEBULA_TEST_MEMORY_IMAGE"]
            socket = os.environ.get(
                "NEBULA_TEST_MEMORY_SOCKET", "unix:///var/run/docker.sock"
            )
            await asyncio.to_thread(
                prepare_allocator,
                self.root,
                runtime=runtime,
                image=image,
                socket=socket,
            )
            await self.store.create(
                RunnerProfile(
                    id="continuity-allocator",
                    name="Disposable continuity allocator",
                    runtime="docker",
                    executable=runtime,
                    socket=socket,
                    platform="linux/amd64",
                    isolation="rootless",
                    healthy=True,
                )
            )
        status = await self.budgets.status()
        await self.budgets.save(
            MemoryBudgetUpdate(
                expected_revision=status.revision,
                policy=status.policy.model_copy(
                    update={
                        "reserve_bytes": (2 if cold else 4) * GIB,
                        "aggregate_bytes": 8 * GIB,
                    }
                ),
            ),
            actor_id="fixture",
        )
        config = {
            "root": str(self.root),
            "database": self.database.url,
            "token": self.token,
            "host": self.listen_host,
            "port": self.port,
            "administrator": self.administrator.model_dump(mode="json"),
            "pool_settings": asdict(self.pool_settings),
            "allow_insecure_device_pairing": pairing,
            "bundles": [
                {**asdict(bundle), "path": str(bundle.path)} for bundle in self.bundles
            ],
        }
        if cold:
            config["recovery_version"] = self.bundles[0].version
        self.config = self.root / "supervisor.json"
        self.config.write_text(json.dumps(config))
        self.config.chmod(0o600)
        await self.start()

    async def start(self):
        environment = {
            name: value
            for name, value in os.environ.items()
            if name in {"HOME", "PATH", "LANG", "LC_ALL", "TZ", "TMPDIR"}
        }
        self.output = (self.root / "supervisor.log").open("ab")
        self.process = subprocess.Popen(
            [
                str(self.bundles[-1].path / "nebula-core"),
                "acceptance-supervisor",
                str(self.config),
            ],
            stdout=self.output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=environment,
        )
        self.birth = process_birth(self.process.pid)
        async with asyncio.timeout(90):
            while True:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        "fixture supervisor exited:\n"
                        + (self.root / "supervisor.log").read_text()[-12000:]
                    )
                try:
                    response = await self.client.get(
                        "/api/v1/deployment/status", timeout=1
                    )
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)

    async def control(self, operation: str, **values):
        response = await self.client.post(
            "/__acceptance/control", json={"operation": operation, **values}
        )
        if response.is_error:
            raise AssertionError(
                f"fixture control {operation}: {response.status_code} {response.text}"
            )
        return response.json()

    async def ready(self):
        source = await self.control(
            "execution",
            version=self.bundles[0].version,
            project_id="continuity-project",
        )
        api = await self.control("api", version=self.bundles[0].version)
        return source, api

    async def kill_supervisor(self):
        assert self.process is not None and self.birth is not None
        assert signal_process(self.process.pid, self.birth, signal.SIGKILL)
        await asyncio.to_thread(self.process.wait, timeout=10)
        self.output.close()

    def hold(self, name: str):
        (self.root / name).write_text("Synthetic deterministic acceptance barrier\n")

    def release(self, name: str):
        (self.root / name).unlink(missing_ok=True)

    async def wait_project(self, phase: str, *, timeout=90):
        async with asyncio.timeout(timeout):
            while True:
                response = await self.client.get("/api/v1/deployment/status")
                response.raise_for_status()
                project = next(
                    item
                    for item in response.json()["projects"]
                    if item["project_id"] == "continuity-project"
                )
                if project["phase"] == phase:
                    return project
                await asyncio.sleep(0.03)

    async def capture(self, key: str, *, until="delta", session_id=None):
        body = {
            "provider_id": "continuity-provider",
            "engagement_id": "continuity-project",
            "model": "model-a",
            "include_knowledge": False,
            "stream": True,
            "messages": [{"role": "user", "content": key}],
        }
        if session_id:
            body["session_id"] = session_id
        async with self.client.stream(
            "POST",
            "/api/v1/chat/completions",
            json=body,
            headers={"Idempotency-Key": key},
        ) as response:
            assert response.status_code == 200, await response.aread()
            frames = []
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
                    if frames[-1].get("type") == until:
                        return response.headers["x-nebula-command-id"], frames
            raise AssertionError(f"stream ended before {until}: {frames}")

    async def close(self):
        self.release("hold-provider")
        for file in self.root.glob("hold-*"):
            file.unlink()
        if self.process is not None and self.process.poll() is None:
            signal_process(self.process.pid, self.birth, signal.SIGTERM)
            try:
                await asyncio.to_thread(self.process.wait, timeout=15)
            except subprocess.TimeoutExpired:
                signal_process(self.process.pid, self.birth, signal.SIGKILL)
                await asyncio.to_thread(self.process.wait, timeout=5)
        if hasattr(self, "output"):
            self.output.close()
        rows = []
        if self.database_ready:
            async with self.database.session() as session:
                rows = list(await session.scalars(select(RuntimeProcessRow)))
        # Only exact process births registered in this fixture's database can be
        # signalled. Deliberately retained children must also be cleaned up.
        for row in rows:
            if same_process(row.pid, row.process_birth):
                signal_process(row.pid, row.process_birth, signal.SIGTERM)
        async with asyncio.timeout(15):
            while any(same_process(row.pid, row.process_birth) for row in rows):
                await asyncio.sleep(0.05)
        await self.client.aclose()
        await self.store.event_history.close()
        await self.database.dispose()


async def serve_browser_fixture(
    root: Path, bundles: list[AcceptanceBundle], *, host: str
):
    """The browser runner owns this process and its complete synthetic data."""
    fixture = ContinuityFixture(root / "data", root / "core.db", bundles, host=host)
    try:
        await fixture.initialize()
        await fixture.ready()
        await asyncio.Event().wait()
    finally:
        await fixture.close()
