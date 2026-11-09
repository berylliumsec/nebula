"""Disposable authenticated production-UI Core and bounded real OCI allocator."""

import asyncio
import hashlib
import os
from pathlib import Path
import signal
import sys
import subprocess
import tempfile
from uuid import uuid4

from fastapi import HTTPException, Request
from keyring.backends.null import Keyring
import uvicorn

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import Engagement, RunnerProfile, RunnerRuntime, RunnerIsolation
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import (
    GIB,
    MIB,
    MemoryAdministratorLimits,
    MemoryBudgetUpdate,
    MemoryLimits,
)
from nebula.v3.sandbox import (
    RunnerProfile as SandboxProfile,
    RunnerPlatform,
    RunnerIsolationMode,
    ContainerRuntimeType,
    SandboxRequest,
    SandboxLimits,
    SandboxWorkspaceAccess,
)
from nebula.v3.storage import NebulaStore
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.v3.test_memory_enforcement import IsolatedKernelFixtureRunner, _SOURCE

TOKEN = "memory-budget-test-token"


async def serve(root: Path):
    store = NebulaStore(root / "core.db")
    await store.create(
        Engagement(id="memory-project", name="Synthetic memory settings")
    )
    runtime, image = (
        os.environ["NEBULA_TEST_MEMORY_RUNTIME"],
        os.environ["NEBULA_TEST_MEMORY_IMAGE"],
    )
    socket = os.environ.get("NEBULA_TEST_MEMORY_SOCKET", "unix:///var/run/docker.sock")
    workspace = root / "workspace"
    workspace.mkdir()
    workspace.chmod(0o755)
    source = root / "allocator.c"
    source.write_text(_SOURCE)
    subprocess.run(
        ["gcc", "-O0", "-static", str(source), "-o", str(workspace / "allocator")],
        check=True,
        capture_output=True,
        timeout=30,
    )
    (workspace / "allocator").chmod(0o555)
    runner = IsolatedKernelFixtureRunner(
        profile=SandboxProfile(
            runtime_type=ContainerRuntimeType.DOCKER,
            executable=Path(runtime),
            platform=RunnerPlatform.LINUX,
            isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
            socket=socket,
        ),
        allow_unpinned_images=True,
        workspace_roots=[workspace],
    )
    profile = await store.create(
        RunnerProfile(
            id="kernel-fixture",
            name="Disposable kernel fixture",
            runtime=RunnerRuntime.DOCKER,
            executable=runtime,
            socket=socket,
            platform="linux/amd64",
            isolation=RunnerIsolation.ROOTLESS,
            healthy=True,
        )
    )
    admin = MemoryAdministratorLimits(
        capacity_bytes=16 * GIB,
        maximum_job_bytes=8 * GIB,
        maximum_aggregate_bytes=14 * GIB,
        minimum_reserve_bytes=2 * GIB,
        maximum_swap_bytes=2 * GIB,
    )
    service = MemoryBudgetService(store.database, administrator=lambda: admin)
    bundle = Path(__file__).resolve().parents[2] / "ui/dist"
    index_sha = hashlib.sha256((bundle / "index.html").read_bytes()).hexdigest()
    app = create_app(
        store,
        artifact_store=ArtifactStore(root / "artifacts"),
        auth_token=TOKEN,
        static_dir=bundle,
        execution_data_root=root,
        memory_budget_service=service,
        credential_store=CredentialStore(keyring_backend=Keyring()),
        allow_insecure_device_pairing=True,
    )
    app.state.memory_api.runners[profile.id] = runner
    app.state.memory_compute.runners[profile.id] = runner
    mount = app.router.routes.pop()
    assert getattr(mount, "name", None) == "workspace"

    def auth(request):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise HTTPException(401)

    @app.post("/__memory_fixture/reset")
    async def reset(request: Request):
        auth(request)
        current = await service.status()
        await service.save(
            MemoryBudgetUpdate(
                expected_revision=current.revision,
                policy=current.policy.model_copy(
                    update={
                        "default_job": MemoryLimits(max_bytes=2 * GIB),
                        "aggregate_bytes": 12 * GIB,
                    }
                ),
            ),
            actor_id="fixture",
        )
        return {"index_sha256": index_sha, "core_pid": os.getpid()}

    @app.post("/__memory_fixture/allocation")
    async def allocation(request: Request):
        auth(request)
        body = await request.json()
        identity = uuid4().hex
        result = await app.state.memory_compute.run_sandbox(
            runner,
            SandboxRequest(
                image=image,
                command=[
                    "/workspace/allocator",
                    "128" if body.get("oversized") else "4",
                    "2",
                ],
                workspace=workspace,
                workspace_access=SandboxWorkspaceAccess.READ,
                limits=SandboxLimits(memory_mb=32, timeout_seconds=15),
            ),
            job_id=identity,
            project_id="memory-project",
            resource_kind="synthetic_allocator",
            resource_id=identity,
            profile_id=profile.id,
            requested=MemoryLimits(max_bytes=32 * MIB),
        )
        return {
            "job_id": identity,
            "exit_code": result.exit_code,
            "outcome": result.memory_outcome,
        }

    @app.get("/__memory_fixture/help-index")
    async def help_index(request: Request):
        auth(request)
        from nebula.v3.knowledge_index import ChromaKnowledgeIndex

        index = ChromaKnowledgeIndex(root / "help-index")
        first = await asyncio.to_thread(index.sync_product_knowledge)
        return {
            **first,
            "observed_digest": index._product_collection.metadata["source_digest"],
        }

    app.router.routes.append(mount)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.getenv("NEBULA_MODEL_TEST_PORT", "19486")),
        log_level="warning",
        access_log=False,
    )
    try:
        await uvicorn.Server(config).serve()
    finally:
        await store.database.dispose()


def main():
    with tempfile.TemporaryDirectory(prefix="nebula-memory-ui-") as directory:

        def stop(_signum, _frame):
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)
        asyncio.run(serve(Path(directory)))


if __name__ == "__main__":
    main()
