"""Disposable production-UI Core using its normal real PDF worker manager."""

import asyncio
import hashlib
import os
from pathlib import Path
import signal
import sys
import tempfile

from fastapi import HTTPException, Request
from keyring.backends.null import Keyring
import uvicorn

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.diagnostics import create_diagnostic_task
from nebula.v3.domain import ChatSession, Engagement, Observation, Report
from nebula.v3.host_cpu_budget import HostCpuUpdate
from nebula.v3.storage import NebulaStore

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.v3.cpu_worker_fixtures import hold


async def main():
    with tempfile.TemporaryDirectory(prefix="nebula-host-cpu-ui-") as directory:
        root = Path(directory)
        store = NebulaStore(root / "core.db")
        (
            await store.create(
                Engagement(id="cpu-project", name="Synthetic host CPU settings")
            )
        )
        (
            await store.create(
                Observation(
                    id="note",
                    engagement_id="cpu-project",
                    observation_type="note",
                    title="Saved source",
                    body="Synthetic PDF content. " * 200,
                )
            )
        )
        (
            await store.create(
                Report(
                    id="report",
                    engagement_id="cpu-project",
                    title="Synthetic worker PDF",
                    observation_ids=["note"],
                )
            )
        )
        bundle = Path(__file__).resolve().parents[2] / "ui/dist"
        digest = hashlib.sha256()
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                digest.update(
                    str(path.relative_to(bundle)).encode() + b"\0" + path.read_bytes()
                )
        app = create_app(
            store,
            artifact_store=ArtifactStore(root / "artifacts"),
            auth_token="cpu-budget-test-token",
            execution_data_root=root,
            static_dir=bundle,
            credential_store=CredentialStore(keyring_backend=Keyring()),
            allow_insecure_device_pairing=True,
        )
        service = app.state.host_cpu_budget
        executor = app.state.report_cpu_executor
        factory = executor.pool_factory
        support_reason = executor.support_reason
        held = None
        release, started = root / "release", root / "started"
        mount = app.router.routes.pop()
        assert getattr(mount, "name", None) == "workspace"

        def fail_factory(**kwargs):
            raise RuntimeError("Synthetic worker startup failure")

        @app.post("/__cpu_fixture/state")
        async def state(request: Request):
            nonlocal held
            if request.headers.get("Authorization") != "Bearer cpu-budget-test-token":
                raise HTTPException(401)
            body = await request.json()
            if body.get("release") or body.get("reset"):
                release.touch()
                if held is not None:
                    await asyncio.gather(held, return_exceptions=True)
                    held = None
            if "fail" in body:
                executor.pool_factory = fail_factory if body["fail"] else factory
            if "unsupported" in body:
                executor.support_reason = (
                    (lambda: "Synthetic host: Linux CPU affinity is unavailable.")
                    if body["unsupported"]
                    else support_reason
                )
            if body.get("reset"):
                executor.pool_factory, executor.support_reason = factory, support_reason
                current = await service.status()
                await service.update(
                    HostCpuUpdate(
                        host_id=current.host_id,
                        expected_revision=current.revision,
                        cpu_limit=None,
                        enabled=False,
                    )
                )
                async with asyncio.timeout(20):
                    while executor.state != "disabled":
                        await asyncio.sleep(0.02)
                release.unlink(missing_ok=True)
                started.unlink(missing_ok=True)
            if body.get("hold"):
                if executor.state != "running" or held is not None:
                    raise HTTPException(409, "Require an idle running fixture pool")
                release.unlink(missing_ok=True)
                started.unlink(missing_ok=True)
                held = create_diagnostic_task(
                    executor.run(hold, str(release), str(started)),
                    feature="reports",
                    event_code="reports.fixture.held_failed",
                    failure_message="Synthetic held work failed.",
                    name="held-pdf-worker",
                )
                async with asyncio.timeout(20):
                    while not started.exists():
                        await asyncio.sleep(0.02)
            if body.get("seed_reports"):
                count = int(body["seed_reports"])
                paragraphs = int(body.get("paragraphs", 2500))
                if not 1 <= count <= 50 or not 1 <= paragraphs <= 4000:
                    raise HTTPException(
                        422, "Require at most fifty bounded synthetic PDFs"
                    )
                text = "\n\n".join(
                    "Synthetic saved report paragraph: measured worker computation with no external tools or network access."
                    for _ in range(paragraphs)
                )
                (
                    await store.create(
                        Observation(
                            id="load-note",
                            engagement_id="cpu-project",
                            observation_type="note",
                            title="Synthetic load source",
                            body=text,
                        )
                    )
                )
                for index in range(count):
                    (
                        await store.create(
                            Report(
                                id=f"load-{index}",
                                engagement_id="cpu-project",
                                title=f"Synthetic load PDF {index}",
                                observation_ids=["load-note"],
                            )
                        )
                    )
                    (
                        await store.create(
                            ChatSession(
                                id=f"load-chat-{index}",
                                engagement_id="cpu-project",
                                title=f"Saved synthetic chat {index}",
                                model="synthetic-offline",
                                provider_profile_id="synthetic-offline-profile",
                            )
                        )
                    )
            return {
                "bundle_sha256": digest.hexdigest(),
                "core_pid": os.getpid(),
                "policy": (await service.status()).model_dump(),
                "pool_stats": executor.pool.snapshot()
                if executor.pool is not None
                else None,
            }

        app.router.routes.append(mount)

        def exit_signal(signum, frame):
            raise SystemExit(0)

        # Uvicorn re-raises captured signals after graceful shutdown. Preserve
        # Python cleanup for this owned fixture's database and temporary root.
        signal.signal(signal.SIGTERM, exit_signal)
        try:
            uvicorn.run(
                app,
                host="0.0.0.0",
                port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19484")),
                access_log=False,
                log_level="warning",
            )
        finally:
            (await store.database.dispose())


if __name__ == "__main__":
    main()
