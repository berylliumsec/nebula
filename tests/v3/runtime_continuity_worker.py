"""Inert adapters around the actual retained Core factories.

Copied into acceptance bundles; imports no repository test modules. The only
synthetic behavior is provider/harness output, allocator selection and explicit
barriers. Production authentication, SQL, transport and orchestration stay real.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib
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


async def initialize(store, *, root: Path, project_id: str, session_id: str):
    """Seed only inert profiles/chats; Work mutations use the public API."""
    from nebula.v3.domain import (
        ChatSession,
        Engagement,
        HarnessProfile,
        ProviderProfile,
    )

    if project_id != "continuity-project" or session_id != "continuity-chat":
        raise ValueError("the Work fixture owns only its synthetic conversations")
    original = await store.get(ProviderProfile, "continuity-provider")
    await store.update(
        ProviderProfile,
        original.id,
        {"endpoint": "http://127.0.0.1:1/v1"},
        expected_revision=original.revision,
    )
    chat = await store.get(ChatSession, session_id)
    await store.update(
        ChatSession,
        chat.id,
        {
            "metadata": {
                **chat.metadata,
                "tools_enabled": False,
                "planning_enabled": False,
            }
        },
        expected_revision=chat.revision,
    )
    second = await store.create(
        ProviderProfile(
            id="continuity-provider-two",
            name="Inert Work provider two",
            provider_type="vllm",
            is_local=True,
            endpoint="http://127.0.0.1:1/v1",
            model_allowlist=["model-a"],
        )
    )
    await store.create(
        ChatSession(
            id="pinned-chat",
            engagement_id="pinned-project",
            title="Second synthetic Work conversation",
            provider_profile_id=second.id,
            model="model-a",
            metadata={
                "initial_title_state": "operator",
                "tools_enabled": False,
                "planning_enabled": False,
            },
        )
    )
    projects = []
    for identity, selected_chat, profile in (
        (project_id, session_id, original),
        ("pinned-project", "pinned-chat", second),
    ):
        project = await store.get(Engagement, identity)
        await store.update(
            Engagement,
            identity,
            {"work_enabled": True},
            expected_revision=project.revision,
        )
        projects.append(
            {
                "project_id": identity,
                "name": project.name,
                "session_id": selected_chat,
                "profile_id": profile.id,
                "profile_name": profile.name,
            }
        )
    if await store.list_entities(HarnessProfile, limit=1):
        raise ValueError(
            "the inert usage fixture must contain no saved harness profiles"
        )
    return {
        "projects": projects,
        "model": "model-a",
        "expected_usage_per_profile": {
            "input_tokens": 4,
            "output_tokens": 2,
            "total_tokens": 6,
            "turns": 1,
        },
    }


def configure_app(app, *, root: Path, lease, version: str):
    """Observe real per-project Work writes without replacing their handlers."""
    prefix = f"/api/v1/engagements/{lease.project_id}/work"

    @app.middleware("http")
    async def observe_work(request: Request, call_next):
        response = await call_next(request)
        if (
            request.method in {"POST", "PATCH", "PUT"}
            and response.status_code < 300
            and (
                request.url.path == prefix or request.url.path.startswith(prefix + "/")
            )
        ):
            record(
                root,
                "work-mutation",
                version=version,
                project_id=lease.project_id,
                runtime_id=lease.runtime_id,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
            )
        return response


@asynccontextmanager
async def project(context, lease):
    import nebula.v3.execution_kernel as kernel
    from nebula.v3.execution_worker import project_application
    from nebula.v3.providers import (
        ModelCapabilities,
        ModelProvider,
        ModelResponse,
        ModelStreamEvent,
        ModelUsage,
        ProviderConfig,
        ProviderHealth,
        ProviderKind,
        StreamEventType,
    )

    root = Path(context.bootstrap.data_root)
    version = context.bootstrap.identity.version

    class InertProvider(ModelProvider):
        def __init__(self, provider_id):
            super().__init__(
                ProviderConfig(
                    id=provider_id,
                    kind=ProviderKind.OPENAI_COMPATIBLE,
                    base_url="http://127.0.0.1:1/v1",
                    default_model="model-a",
                    model_allowlist=["model-a"],
                    local=True,
                    capabilities=ModelCapabilities(streaming=True),
                )
            )

        async def health(self):
            return ProviderHealth(
                provider_id=self.config.id, healthy=True, models=["model-a"]
            )

        async def complete(self, request):
            return ModelResponse(
                provider_id=self.config.id,
                model="model-a",
                text="First last",
                usage=ModelUsage(input_tokens=4, output_tokens=2, total_tokens=6),
                finish_reason="stop",
                provider_request_id="inert-" + lease.project_id,
            )

        async def stream(self, request):
            record(root, "provider-start", version=version, project_id=lease.project_id)
            yield ModelStreamEvent(type=StreamEventType.STARTED)
            yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="First ")
            await wait_barrier(root, "hold-provider")
            yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="last")
            yield ModelStreamEvent(
                type=StreamEventType.COMPLETED, response=await self.complete(request)
            )

    original = kernel.create_execution_kernel

    def configured(*args, **kwargs):
        app = original(*args, **kwargs)
        chat = app.state.control_assistant_work.chat
        chat.provider_factory = lambda profile: InertProvider(profile.id)
        chat.provider_scheduler.host_load = None
        scenario_file = root / "acceptance-scenario.json"
        if scenario_file.exists():
            scenario = json.loads(scenario_file.read_text())["scenario"]
            module = {
                "provider-tools-context": "acceptance_provider_tools_context",
                "standing-instructions-native": "acceptance_standing_instructions_native",
                "work-settings": "acceptance_worker",
            }.get(scenario)
            if module is None:
                raise RuntimeError("unknown retained acceptance scenario")
            importlib.import_module(module).configure_app(
                app, root=root, lease=lease, version=version
            )
        from acceptance_allocator import attach_allocator

        attach_allocator(app, root, lease.project_id)

        @app.post("/api/v1/engagements/{selected_project}/acceptance-effect")
        async def held_effect(selected_project: str):
            if selected_project != lease.project_id:
                raise HTTPException(404)
            record(root, "effect-entered", version=version, project_id=lease.project_id)
            await wait_barrier(root, "hold-effect")
            return {"effect": "synthetic-finished"}

        record(
            root,
            "project-constructed",
            version=version,
            project_id=lease.project_id,
            fencing_token=lease.fencing_token,
            source=kernel.__file__,
        )
        return app

    kernel.create_execution_kernel = configured
    try:
        async with project_application(context, lease) as app:
            activate = getattr(app.state, "runtime_activate", None)
            if activate is not None:

                async def deferred_activation():
                    record(
                        root,
                        "activation-wait",
                        version=version,
                        project_id=lease.project_id,
                    )
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
    from nebula.v3.process_identity import same_process
    from nebula.v3.runtime_memory import RuntimeMemoryAdmission
    from nebula.v3.runtime_processes import RuntimeProcessHost
    from nebula.v3.supervisor import RuntimeSupervisor
    from sqlalchemy import select

    root = Path(config["root"])
    settings = PoolSettings(**config["pool_settings"])
    database = Database(config["database"], pool_settings=settings)
    coordinator = DeploymentCoordinator(database)
    budgets = MemoryBudgetService(
        database,
        administrator=lambda: MemoryAdministratorLimits.model_validate(
            config["administrator"]
        ),
    )
    host = RuntimeProcessHost(
        coordinator, root, operator_token=config["token"], pool_settings=settings
    )
    options = {"worker_admission": RuntimeMemoryAdmission(budgets)}
    if config.get("allow_insecure_device_pairing"):
        options["allow_insecure_device_pairing"] = True
    supervisor = RuntimeSupervisor(host, operator_token=config["token"], **options)
    for bundle in config["bundles"]:
        manifest = ReleaseManifest.model_validate_json(
            (Path(bundle["path"]) / "nebula-runtime.json").read_text()
        )
        await coordinator.stage(
            manifest, Path(bundle["path"]), bundle["manifest_sha256"]
        )
    await supervisor.start()
    from acceptance_allocator import runner_for

    allocator = runner_for(root)
    if allocator is not None:
        supervisor.shared_services.memory_control.api.runners[
            "continuity-allocator"
        ] = allocator
    selected = config.get("recovery_version")
    if selected:
        selector = getattr(supervisor, "select_recovery_release", None)
        if selector is None:
            raise RuntimeError(
                "candidate lacks explicit verified host recovery release selection"
            )
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
        if operation == "initialize-scenario":
            scenario_file = root / "acceptance-scenario.json"
            if not scenario_file.is_file():
                raise HTTPException(409, "select an explicit acceptance scenario")
            scenario = json.loads(scenario_file.read_text())["scenario"]
            module = {
                "provider-tools-context": "acceptance_provider_tools_context",
                "standing-instructions-native": "acceptance_standing_instructions_native",
                "work-settings": "acceptance_worker",
            }.get(scenario)
            if module is None:
                raise HTTPException(400, "unknown acceptance scenario")
            from nebula.v3.domain import ChatSession
            from nebula.v3.storage import NebulaStore

            observed_store = NebulaStore(database)
            try:
                owner = await observed_store.get(ChatSession, body["session_id"])
                if owner.engagement_id != "continuity-project":
                    raise HTTPException(
                        403, "the fixture owns only its synthetic project"
                    )
                return await importlib.import_module(module).initialize(
                    observed_store,
                    root=root,
                    project_id=owner.engagement_id,
                    session_id=owner.id,
                )
            finally:
                await observed_store.event_history.close()
        if operation == "api":
            try:
                identity = await supervisor.prepare_api(body["version"])
            except MemoryBudgetError as error:
                raise HTTPException(409, str(error)) from error
            await supervisor.promote_api(
                identity.runtime_id,
                expected_revision=(await coordinator.status()).revision,
            )
            return identity.model_dump(mode="json")
        if operation == "execution":
            identity = await supervisor.prepare_execution(body["version"])
            await supervisor.attach_execution(body["project_id"], identity.runtime_id)
            return identity.model_dump(mode="json")
        if operation == "handoff":
            target = await supervisor.prepare_execution(body["version"])
            await supervisor.request_project_handoff(
                body["project_id"], target.runtime_id
            )
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
        if operation == "tool-context":
            scenario = root / "acceptance-scenario.json"
            if (
                not scenario.is_file()
                or json.loads(scenario.read_text())["scenario"]
                != "provider-tools-context"
            ):
                raise HTTPException(409, "select the explicit provider-tools fixture")
            from nebula.v3.domain import ChatSession
            from nebula.v3.provider_tool_manifest import read_manifest
            from nebula.v3.storage import NebulaStore

            observed_store = NebulaStore(database)
            try:
                owner = await observed_store.get(ChatSession, body["session_id"])
                if owner.engagement_id != "continuity-project":
                    raise HTTPException(
                        403, "the fixture owns only its synthetic project"
                    )
                async with database.session() as session:
                    manifest = await read_manifest(
                        session, project_id=owner.engagement_id, session_id=owner.id
                    )
                return {
                    "manifest": {
                        **manifest.model_dump(mode="json"),
                        "digest": manifest.digest,
                    },
                    "reference": owner.metadata.get("provider_tool_manifest"),
                }
            finally:
                await observed_store.event_history.close()
        if operation == "native-evidence":
            scenario = root / "acceptance-scenario.json"
            if (
                not scenario.is_file()
                or json.loads(scenario.read_text())["scenario"]
                != "standing-instructions-native"
            ):
                raise HTTPException(
                    409, "select the explicit native instructions fixture"
                )
            from nebula.v3.domain import ChatSession, HarnessSession
            from nebula.v3.storage import NebulaStore

            observed_store = NebulaStore(database)
            try:
                owner = await observed_store.get(
                    ChatSession, body.get("session_id", "continuity-chat")
                )
                if owner.engagement_id != "continuity-project":
                    raise HTTPException(
                        403, "the fixture owns only its synthetic project"
                    )
                result = importlib.import_module(
                    "acceptance_standing_instructions_native"
                ).evidence(root)
                if owner.harness_session_id:
                    native = await observed_store.get(
                        HarnessSession, owner.harness_session_id
                    )
                    result["core_harness_session_id"] = native.id
                    result["channel_receipt"] = native.metadata.get(
                        "instruction_channel"
                    )
                    result["standing_snapshot"] = native.metadata.get(
                        "standing_instructions"
                    )
                return result
            finally:
                await observed_store.event_history.close()
        if operation == "work-evidence":
            from nebula.v3.domain import ChatTurn, WorkItem, WorkUpdate
            from nebula.v3.storage import NebulaStore

            scenario = root / "acceptance-scenario.json"
            if (
                not scenario.is_file()
                or json.loads(scenario.read_text())["scenario"] != "work-settings"
            ):
                raise HTTPException(409, "select the explicit Work fixture")
            observed_store = NebulaStore(database)
            result = {"items": [], "updates": [], "turns": []}
            try:
                for selected in ("continuity-project", "pinned-project"):
                    for key, model in (("items", WorkItem), ("updates", WorkUpdate)):
                        rows = await observed_store.list_entities(
                            model, engagement_id=selected, limit=100
                        )
                        result[key].extend(row.model_dump(mode="json") for row in rows)
                    turns = await observed_store.list_entities(
                        ChatTurn, engagement_id=selected, limit=100
                    )
                    result["turns"].extend(
                        {
                            "id": turn.id,
                            "session_id": turn.session_id,
                            "engagement_id": turn.engagement_id,
                            "status": turn.status.value,
                            "provider_profile_id": turn.provider_profile_id,
                            "usage": turn.usage.model_dump(mode="json"),
                        }
                        for turn in turns
                    )
                return result
            finally:
                await observed_store.event_history.close()
        if operation == "inspect":
            async with database.session() as session:
                processes = list(await session.scalars(select(RuntimeProcessRow)))
            return {
                "supervisor_pid": os.getpid(),
                "processes": [
                    {
                        "id": row.runtime_id,
                        "pid": row.pid,
                        "birth": row.process_birth,
                        "version": row.version,
                        "role": row.role,
                        "state": row.state,
                        "alive": bool(
                            row.pid
                            and row.process_birth
                            and same_process(row.pid, row.process_birth)
                        ),
                    }
                    for row in processes
                ],
                "events": [
                    json.loads(line)
                    for line in (root / "acceptance-events.jsonl")
                    .read_text()
                    .splitlines()
                ]
                if (root / "acceptance-events.jsonl").exists()
                else [],
            }
        raise HTTPException(400, "unknown synthetic fixture control")

    # Controls precede the production catch-all, and are authenticated with the
    # fixture-only operator token. The real application routes are not replaced.
    route = app.router.routes.pop()
    app.router.routes.insert(0, route)
    try:
        await uvicorn.Server(
            uvicorn.Config(
                supervisor.app,
                host=config["host"],
                port=config["port"],
                log_level="warning",
                access_log=False,
            )
        ).serve()
    finally:
        await supervisor.close()
        await database.dispose()


def main():
    if sys.argv[1:2] == ["acceptance-probe"]:
        import chromadb
        import nebula.v3.runtime_worker as worker
        import sqlalchemy

        print(
            json.dumps(
                {
                    "python": sys.executable,
                    "core": worker.__file__,
                    "sqlalchemy": sqlalchemy.__file__,
                    "chroma": chromadb.__file__,
                }
            )
        )
    elif sys.argv[1:2] == ["acceptance-supervisor"]:
        config = json.loads(Path(sys.argv[2]).read_text())
        asyncio.run(serve(config))
    elif sys.argv[1:2] == ["runtime-worker"]:
        from nebula.v3.runtime_processes import read_worker_bootstrap
        from nebula.v3.runtime_worker import run_worker

        asyncio.run(
            run_worker(
                read_worker_bootstrap(sys.stdin.buffer), execution_factory=execution
            )
        )
    else:
        # The final service/desktop driver enters the actual CLI. Its workers
        # come back through the branch above with only inert adapters injected.
        from nebula.v3.cli import main as cli_main

        cli_main()
