"""Inert adapters around the actual retained Core factories.

Copied into acceptance bundles; imports no repository test modules. The only
synthetic behavior is provider/harness output, allocator selection and explicit
barriers. Production authentication, SQL, transport and orchestration stay real.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

from fastapi import HTTPException, Request


def record(root: Path, event: str, **values) -> None:
    with (root / "acceptance-events.jsonl").open("a") as output:
        output.write(json.dumps({"event": event, "pid": os.getpid(), **values}) + "\n")


async def wait_barrier(root: Path, name: str) -> None:
    while (root / name).exists():
        await asyncio.sleep(0.02)


@asynccontextmanager
async def project(context, lease):
    import nebula.v3.execution_kernel as kernel
    from nebula.v3.execution_worker import project_application
    from nebula.v3.providers import (
        ModelCapabilities, ModelProvider, ModelResponse, ModelStreamEvent,
        ModelUsage, ProviderConfig, ProviderHealth, ProviderKind, StreamEventType,
    )

    root = Path(context.bootstrap.data_root)
    version = context.bootstrap.identity.version

    class InertProvider(ModelProvider):
        def __init__(self, provider_id):
            super().__init__(ProviderConfig(
                id=provider_id, kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:1/v1", default_model="model-a",
                model_allowlist=["model-a"], local=True,
                capabilities=ModelCapabilities(streaming=True),
            ))

        async def health(self):
            return ProviderHealth(provider_id=self.config.id, healthy=True, models=["model-a"])

        async def complete(self, request):
            return ModelResponse(
                provider_id=self.config.id, model="model-a", text="First last",
                usage=ModelUsage(input_tokens=4, output_tokens=2, total_tokens=6),
                finish_reason="stop", provider_request_id="inert-" + lease.project_id,
            )

        async def stream(self, request):
            record(root, "provider-start", version=version, project_id=lease.project_id)
            yield ModelStreamEvent(type=StreamEventType.STARTED)
            yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="First ")
            await wait_barrier(root, "hold-provider")
            yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="last")
            yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=await self.complete(request))

    original = kernel.create_execution_kernel

    def configured(*args, **kwargs):
        app = original(*args, **kwargs)
        chat = app.state.control_assistant_work.chat
        chat.provider_factory = lambda profile: InertProvider(profile.id)
        chat.provider_scheduler.host_load = None
        from acceptance_allocator import attach_allocator
        attach_allocator(app, root, lease.project_id)

        @app.post("/api/v1/engagements/{selected_project}/acceptance-effect")
        async def held_effect(selected_project: str):
            if selected_project != lease.project_id:
                raise HTTPException(404)
            record(root, "effect-entered", version=version, project_id=lease.project_id)
            await wait_barrier(root, "hold-effect")
            return {"effect": "synthetic-finished"}
        record(root, "project-constructed", version=version, project_id=lease.project_id,
               fencing_token=lease.fencing_token, source=kernel.__file__)
        return app

    kernel.create_execution_kernel = configured
    try:
        async with project_application(context, lease) as app:
            activate = getattr(app.state, "runtime_activate", None)
            if activate is not None:
                async def deferred_activation():
                    record(root, "activation-wait", version=version, project_id=lease.project_id)
                    await wait_barrier(root, "hold-activation")
                    return await activate()
                app.state.runtime_activate = deferred_activation
            agent = getattr(app.state, "runtime_handoff", None)
            if agent is not None:
                original_tick = agent.tick

                async def tick():
                    from nebula.v3.database import ProjectHandoffRow
                    async with context.database.session() as session:
                        row = await session.get(ProjectHandoffRow, lease.project_id)
                        phase = row.phase if row is not None else None
                    if phase is not None:
                        await wait_barrier(root, "hold-phase-" + phase)
                    return await original_tick()

                agent.tick = tick
            yield app
    finally:
        kernel.create_execution_kernel = original


@asynccontextmanager
async def execution(context):
    from nebula.v3.execution_worker import ProjectExecution
    worker = ProjectExecution(context, application_factory=project)
    try:
        yield worker.app
    finally:
        await worker.close()


async def serve(config):
    import uvicorn
    from nebula.v3.database import Database, RuntimeProcessRow
    from nebula.v3.database_pool import PoolSettings
    from nebula.v3.deployment_protocol import ReleaseManifest
    from nebula.v3.deployments import DeploymentCoordinator
    from nebula.v3.memory_budget import MemoryBudgetService
    from nebula.v3.memory_policy import MemoryAdministratorLimits, MemoryBudgetError
    from nebula.v3.runtime_memory import RuntimeMemoryAdmission
    from nebula.v3.runtime_processes import RuntimeProcessHost
    from nebula.v3.supervisor import RuntimeSupervisor
    from sqlalchemy import select

    root = Path(config["root"])
    settings = PoolSettings(**config["pool_settings"])
    database = Database(config["database"], pool_settings=settings)
    coordinator = DeploymentCoordinator(database)
    budgets = MemoryBudgetService(
        database, administrator=lambda: MemoryAdministratorLimits.model_validate(config["administrator"])
    )
    host = RuntimeProcessHost(coordinator, root, operator_token=config["token"], pool_settings=settings)
    options = {"worker_admission": RuntimeMemoryAdmission(budgets)}
    if config.get("allow_insecure_device_pairing"):
        options["allow_insecure_device_pairing"] = True
    supervisor = RuntimeSupervisor(host, operator_token=config["token"], **options)
    for bundle in config["bundles"]:
        manifest = ReleaseManifest.model_validate_json((Path(bundle["path"]) / "nebula-runtime.json").read_text())
        await coordinator.stage(manifest, Path(bundle["path"]), bundle["manifest_sha256"])
    await supervisor.start()
    from acceptance_allocator import runner_for
    allocator = runner_for(root)
    if allocator is not None:
        supervisor.shared_services.memory_control.api.runners["continuity-allocator"] = allocator
    selected = config.get("recovery_version")
    if selected:
        selector = getattr(supervisor, "select_recovery_release", None)
        if selector is None:
            raise RuntimeError("candidate lacks explicit verified host recovery release selection")
        await selector(selected)
    original_advance = supervisor._advance_project_handoff
    original_transfer = coordinator.transfer

    async def transfer(project_id):
        lease = await original_transfer(project_id)
        await wait_barrier(root, "hold-phase-transferred")
        return lease

    coordinator.transfer = transfer

    async def advance(row):
        await wait_barrier(root, "hold-phase-" + row.phase)
        return await original_advance(row)

    supervisor._advance_project_handoff = advance
    app = supervisor.app.app

    @app.post("/__acceptance/control")
    async def control(request: Request):
        if request.headers.get("authorization") != "Bearer " + config["token"]:
            raise HTTPException(401)
        body = await request.json()
        operation = body["operation"]
        if operation == "api":
            try:
                identity = await supervisor.prepare_api(body["version"])
            except MemoryBudgetError as error:
                raise HTTPException(409, str(error)) from error
            await supervisor.promote_api(identity.runtime_id, expected_revision=(await coordinator.status()).revision)
            return identity.model_dump(mode="json")
        if operation == "execution":
            identity = await supervisor.prepare_execution(body["version"])
            await supervisor.attach_execution(body["project_id"], identity.runtime_id)
            return identity.model_dump(mode="json")
        if operation == "handoff":
            target = await supervisor.prepare_execution(body["version"])
            await supervisor.request_project_handoff(body["project_id"], target.runtime_id)
            return target.model_dump(mode="json")
        if operation == "retire":
            await host.retire(body["runtime_id"], timeout=10)
            return {"retired": True}
        if operation == "recovery-release":
            await supervisor.select_recovery_release(body["version"])
            return {"selected": body["version"]}
        if operation == "execution-status":
            client = await host.rpc(body["runtime_id"])
            try:
                return await client.call("execution.status/v1", {})
            finally:
                await client.close()
        if operation == "inspect":
            async with database.session() as session:
                processes = list(await session.scalars(select(RuntimeProcessRow)))
            return {
                "supervisor_pid": os.getpid(),
                "processes": [{"id": row.runtime_id, "pid": row.pid, "birth": row.process_birth,
                               "version": row.version, "role": row.role, "state": row.state} for row in processes],
                "events": [json.loads(line) for line in (root / "acceptance-events.jsonl").read_text().splitlines()]
                if (root / "acceptance-events.jsonl").exists() else [],
            }
        raise HTTPException(400, "unknown synthetic fixture control")

    # Controls precede the production catch-all, and are authenticated with the
    # fixture-only operator token. The real application routes are not replaced.
    route = app.router.routes.pop()
    app.router.routes.insert(0, route)
    try:
        await uvicorn.Server(uvicorn.Config(
            supervisor.app, host=config["host"], port=config["port"],
            log_level="warning", access_log=False,
        )).serve()
    finally:
        await supervisor.close()
        await database.dispose()


def main():
    if sys.argv[1:2] == ["acceptance-probe"]:
        import chromadb
        import nebula.v3.runtime_worker as worker
        import sqlalchemy
        print(json.dumps({"python": sys.executable, "core": worker.__file__,
                          "sqlalchemy": sqlalchemy.__file__, "chroma": chromadb.__file__}))
    elif sys.argv[1:2] == ["acceptance-supervisor"]:
        config = json.loads(Path(sys.argv[2]).read_text())
        asyncio.run(serve(config))
    elif sys.argv[1:2] == ["runtime-worker"]:
        from nebula.v3.runtime_processes import read_worker_bootstrap
        from nebula.v3.runtime_worker import run_worker
        asyncio.run(run_worker(read_worker_bootstrap(sys.stdin.buffer), execution_factory=execution))
    else:
        # The final service/desktop driver enters the actual CLI. Its workers
        # come back through the branch above with only inert adapters injected.
        from nebula.v3.cli import main as cli_main
        cli_main()
