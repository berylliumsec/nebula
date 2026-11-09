"""A disposable authenticated CLI Core with retained source and inert providers."""

import asyncio
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys

import httpx
from sqlalchemy import select

from nebula.v3.database import (
    Database,
    DeploymentHostRow,
    ProjectOwnershipRow,
    RuntimeProcessRow,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployment_protocol import ReleaseManifest
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import MIB, MemoryBudgetUpdate
from nebula.v3.process_identity import process_birth, same_process, signal_process
from nebula.v3.release_store import MANIFEST_NAME, ReleaseStore
from nebula.v3.runtime_ipc import RuntimeCredentials, RuntimeRpcClient
from nebula.v3.runtime_launcher import discovery, operator_token
from nebula.v3.storage import NebulaStore

SETTINGS = PoolSettings(5, 12, 5, 2)


def candidate_bundle(root):
    source = root / "bundle-source"
    source.mkdir()
    package = Path(__file__).resolve().parents[2] / "src" / "nebula"
    shutil.copytree(
        package,
        source / "source" / "nebula",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name, target in (
        ("runtime_continuity_worker.py", "acceptance_worker.py"),
        ("runtime_continuity_allocator.py", "acceptance_allocator.py"),
        ("runtime_readmission_worker.py", "acceptance_readmission_worker.py"),
    ):
        shutil.copy2(Path(__file__).with_name(name), source / target)
    (source / "bin").mkdir()
    (source / "bin" / "nebula-core").write_text(
        "#!" + sys.executable + "\nfrom pathlib import Path\nimport sys\n"
        "root=Path(__file__).resolve().parent.parent\n"
        "sys.path[:0]=[str(root/'source'),str(root)]\n"
        "from acceptance_readmission_worker import main\nmain()\n"
    )
    (source / "bin" / "nebula-core").chmod(0o755)
    (source / "ui").mkdir()
    (source / "ui" / "index.html").write_text(
        "<html>synthetic readmission recovery</html>"
    )
    manifest = ReleaseManifest(
        **json.loads((package / "v3/runtime-compatibility.json").read_text())[
            "manifest"
        ],
        version="readmission-v1",
        core_executable="bin/nebula-core",
        ui_directory="ui",
        files={
            str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(source.rglob("*"))
            if path.is_file()
        },
    )
    raw = manifest.model_dump_json().encode()
    (source / MANIFEST_NAME).write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    _manifest, retained = ReleaseStore(root / "retained").install(
        source, expected_manifest_sha256=digest
    )
    retained.chmod(0o755)
    (retained / "nebula-runtime.sha256").write_text(digest + "\n")
    (retained / "nebula-runtime.sha256").chmod(0o444)
    retained.chmod(0o555)
    shutil.rmtree(source)
    return retained


class OwnedReadmissionCore:
    def __init__(self, root, location, bundle):
        self.root, self.bundle = root, bundle
        self.token = operator_token(root, "synthetic-readmission-core-operator-token")
        self.database = Database(location, pool_settings=SETTINGS)
        self.store = NebulaStore(self.database)
        self.client = None
        self.processes, self.outputs = [], []
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            if not ipaddress.ip_address(item[4][0]).is_loopback
        }
        assert addresses, "The Core fixture requires a non-loopback LAN address"
        self.host = sorted(addresses)[0]

    async def initialize(self):
        await self.database.initialize()
        budgets = MemoryBudgetService(self.database)
        before = await budgets.status()
        reserve = before.administrator_limits.minimum_reserve_bytes + 3 * 256 * MIB
        aggregate = min(
            before.administrator_limits.maximum_aggregate_bytes,
            before.administrator_limits.capacity_bytes - reserve,
        )
        assert aggregate >= 16 * MIB
        await budgets.save(
            MemoryBudgetUpdate(
                expected_revision=before.revision,
                policy=before.policy.model_copy(
                    update={
                        "reserve_bytes": reserve,
                        "aggregate_bytes": aggregate,
                        "default_job": before.policy.default_job.model_copy(
                            update={
                                "max_bytes": min(
                                    before.policy.default_job.max_bytes, aggregate
                                )
                            }
                        ),
                    }
                ),
            ),
            actor_id="fixture",
        )
        await self.start()

    async def start(self):
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"HOME", "PATH", "LANG", "LC_ALL", "TZ"}
        }
        environment.update(
            NEBULA_RUNTIME_BUNDLE=str(self.bundle),
            NEBULA_V3_DATABASE_URL=self.database.url,
            NEBULA_V3_DB_ADMIN_RESERVE="5",
            NEBULA_V3_DB_OTHER_CONNECTIONS="12",
            NEBULA_V3_DB_PEAK_PROCESSES="5",
            NEBULA_V3_DB_POOL_SIZE="2",
            NEBULA_V3_KNOWLEDGE_RERANKER="off",
        )
        log = self.root / ("core-" + str(len(self.processes)) + ".log")
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        output = os.fdopen(descriptor, "wb")
        process = subprocess.Popen(
            [
                str(self.bundle / "bin/nebula-core"),
                "serve",
                "--host",
                self.host,
                "--port",
                "0",
                "--allow-remote",
                "--data-dir",
                str(self.root),
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=environment,
        )
        self.processes.append((process, process_birth(process.pid)))
        self.outputs.append(output)
        async with asyncio.timeout(60):
            while True:
                assert process.poll() is None, f"Owned Core exited; inspect {log}"
                connection = discovery(self.root)
                if connection and connection["pid"] == process.pid:
                    break
                await asyncio.sleep(0.03)
            if self.client is not None:
                await self.client.aclose()
            self.origin = connection["endpoint"].removesuffix("/api/v1")
            self.client = httpx.AsyncClient(
                base_url=self.origin,
                headers={"Authorization": "Bearer " + self.token},
                trust_env=False,
                timeout=30,
            )
            while True:
                response = await self.client.get("/api/v1/health")
                if response.status_code == 200 and response.json()["api_ready"]:
                    break
                await asyncio.sleep(0.05)

    async def wait_for_chat_maintenance(self, project_id):
        async with self.database.session() as session:
            owner = await session.get(ProjectOwnershipRow, project_id)
            assert owner is not None and owner.runtime_id is not None
            process = await session.get(RuntimeProcessRow, owner.runtime_id)
            assert process is not None
        credentials = RuntimeCredentials(self.root / "runtime" / "credentials")
        client = RuntimeRpcClient(
            process.endpoint, credentials.read(process.credential_reference)
        )
        observed = None
        try:
            async with asyncio.timeout(30):
                while True:
                    observed = await client.call(
                        "acceptance.chat-maintenance/v1", {}, timeout=5
                    )
                    if observed == {"naming": 0, "precompactions": 0}:
                        return
                    await asyncio.sleep(0.02)
        except TimeoutError as error:
            raise AssertionError(
                f"Chat maintenance did not finish: {observed}"
            ) from error
        finally:
            await client.close()

    async def stop(self, key):
        status = (await self.client.get("/api/v1/deployment/status")).json()
        response = await self.client.post(
            "/api/v1/deployment/operations",
            json={
                "operation_id": key,
                "action": "shutdown",
                "parameters": {"expected_revision": status["revision"]},
            },
        )
        if response.status_code != 200 or response.json()["state"] != "succeeded":
            observed = await self.client.get("/api/v1/deployment/status")
            raise AssertionError(
                f"{response.text}; deployment status: {observed.text}; logs: {self.root}"
            )
        process, birth = self.processes[-1]
        await asyncio.to_thread(process.wait, timeout=30)
        assert not same_process(process.pid, birth)
        assert discovery(self.root) is None
        async with self.database.session() as session:
            host = await session.get(DeploymentHostRow, "host")
            assert host is not None
            assert host.shutdown_requested and host.api_runtime_id is None

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
        # Failure cleanup owns only this fixture's exact process identities.
        for process, birth in self.processes:
            if process.poll() is None:
                signal_process(process.pid, birth, signal.SIGTERM)
                try:
                    await asyncio.to_thread(process.wait, timeout=10)
                except subprocess.TimeoutExpired:
                    signal_process(process.pid, birth, signal.SIGKILL)
                    await asyncio.to_thread(process.wait, timeout=5)
        async with self.database.session() as session:
            rows = list(await session.scalars(select(RuntimeProcessRow)))
        for row in rows:
            if row.pid and same_process(row.pid, row.process_birth):
                signal_process(row.pid, row.process_birth, signal.SIGKILL)
        for output in self.outputs:
            output.close()
        await self.store.event_history.close()
        await self.database.dispose()
