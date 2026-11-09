"""One immutable project incarnation and its execution-owned application tasks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from starlette.responses import JSONResponse

from .async_utils import finish_cleanup
from .database import Database
from .database_pool import PoolSettings
from .diagnostics import record_caught_exception
from .execution_memory import ExecutionMemoryBudget
from .execution_scope import execution_context
from .runtime_capacity import RuntimeCapacity
from .runtime_coordination import (
    CoordinationConflict,
    HEARTBEAT_SECONDS,
    LeaseLost,
    ProjectLease,
)
from .runtime_delivery import DurableHttpRunner, RuntimeDelivery
from .runtime_host_services import host_service_client
from .runtime_ipc import RuntimeIpcError
from .runtime_vault import RuntimeVault
from .shared_memory_rpc import MemoryAuthority, RemoteButlerMemory


@asynccontextmanager
async def project_application(context, lease) -> AsyncIterator[object]:
    """Construct controllers only after SQL grants this exact project lease.

    The coordination pool is deliberately separate from the application's
    fenced pool. Shared services use the committed private peer identity; this
    process never constructs a Chroma or legacy Butler-memory owner.
    """
    from .artifacts import ArtifactStore
    from .execution_kernel import create_execution_kernel
    from .remote_knowledge import RemoteKnowledgeIndex
    from .runtime_platform import default_runtime_platform
    from .runtime_chat_stream import RuntimeChatJournal
    from .runtime_chat_acceptance import CHAT_ACCEPT_FEATURE
    from .runtime_project_admission import HANDOFF_FEATURE, PARTICIPANT_FEATURE
    from .runtime_project_handoff import ProjectHandoffAgent, restored_checkpoint
    from .runtime_memory_control import RemoteMemorySettings
    from .runtime_host_credentials import RemoteSessionCredentials
    from .runtime_host_pressure import RemoteHostLoadGate
    from .runtime_integration_credentials import RemoteExecutionCredentials
    from .runtime_host_settings_bridge import RemoteHostSettings
    from .runtime_host_search import RemoteProjectSearch
    from .web_search import UnavailableProjectSearch
    from .runtime_browser_bridge import RemoteBrowserBridge
    from .storage import NebulaStore

    bootstrap = context.bootstrap
    if bootstrap.operator_token is None:
        raise RuntimeIpcError("Execution requires the private operator bootstrap")
    root = Path(bootstrap.data_root)
    controllers = root / "runtime" / "workers" / bootstrap.identity.runtime_id
    controllers.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = host_service_client(bootstrap, context.secret)
    settings = (
        PoolSettings(**bootstrap.pool_settings) if bootstrap.pool_settings else None
    )
    database = Database(
        bootstrap.database,
        bootstrap=False,
        pool_settings=settings,
        execution_runtime_id=lease.runtime_id,
    )
    store = NebulaStore(database)
    knowledge = None
    try:
        if (
            PARTICIPANT_FEATURE not in context.manifest.features
            and await context.coordinator.project_requires_participants(
                lease.project_id
            )
        ):
            raise RuntimeIpcError(
                "This release cannot restore the project's durable participants"
            )
        services = await client.call("host.services/v1", {})
        if (
            not isinstance(services, dict)
            or services.get("protocol") != "nebula.host-services/v1"
            or services.get("memory") is not True
            or services.get("capacity") is not True
            or type(services.get("knowledge")) is not bool
        ):
            raise RuntimeIpcError("Required host services are not ready")
        if services["knowledge"]:
            knowledge = RemoteKnowledgeIndex(client, root)
            await knowledge.refresh()
        artifacts = ArtifactStore(root / "artifacts")
        platform = default_runtime_platform(
            store=store,
            artifact_store=artifacts,
            data_root=root,
            search_runtime=RemoteProjectSearch(client, store, lease)
            if services.get("web_search") is True
            else UnavailableProjectSearch(),
        )
        restore_digest = (
            await restored_checkpoint(context.coordinator, lease)
            if HANDOFF_FEATURE in context.manifest.features
            else None
        )
        recovery = await context.coordinator.recovery_status(lease.project_id)
        app = create_execution_kernel(
            store,
            artifact_store=artifacts,
            credential_store=RemoteExecutionCredentials(client)
            if services.get("integration_credentials") is True
            else None,
            auth_token=bootstrap.operator_token.get_secret_value(),
            execution_data_root=root,
            controller_root=controllers,
            tool_platform=platform,
            mission_checkpoint_path=root / "mission-checkpoints.db",
            knowledge_index=knowledge,
            butler_memory_service=RemoteButlerMemory(client),
            runtime_capacity=RuntimeCapacity(context.coordinator.coordination),
            memory_budget_service=ExecutionMemoryBudget(
                context.coordinator.coordination, lease
            ),
            stream_journal=RuntimeChatJournal(context.coordinator.coordination, store),
            durable_chat_acceptance=CHAT_ACCEPT_FEATURE in context.manifest.features,
            durable_chat_resume="chat.resume/v1" in context.manifest.features,
            settings_memory_request=RemoteMemorySettings(client).request
            if services.get("memory_control") is True
            else None,
            settings_host_request=RemoteHostSettings(client, services["host_settings"])
            if "host_settings" in services
            else None,
            provider_session_resolver=RemoteSessionCredentials(client).resolve
            if services.get("provider_credentials") is True
            else None,
            host_load_gate=RemoteHostLoadGate(client)
            if services.get("host_pressure") is True
            else None,
        )
        if services.get("browser_bridge") is True:
            app.state.browser_companion.bind_bridge(RemoteBrowserBridge(client).resolve)
        if HANDOFF_FEATURE in context.manifest.features:
            app.state.runtime_recovery_operation_id = (
                recovery["operation_id"]
                if recovery and recovery["lease"] == asdict(lease)
                else None
            )
            app.state.control_assistant_work.chat.restoring_handoff = (
                restore_digest is not None
            )
            app.state.runtime_handoff = ProjectHandoffAgent(
                context, lease, app, restored_digest=restore_digest
            )
        async with AsyncExitStack() as lifetime:

            async def activate():
                await lifetime.enter_async_context(app.router.lifespan_context(app))

            # Restoration is prepared without starting schedulers, recovery
            # scans, callbacks or goal producers. The host first commits the
            # matching checkpoint verification, then explicitly activates it.
            if restore_digest is not None:
                app.state.runtime_activate = activate
            else:
                await activate()
            yield app
    finally:
        if knowledge is not None:
            await finish_cleanup(knowledge.close())
        await finish_cleanup(client.close())
        await finish_cleanup(store.event_history.close())
        await finish_cleanup(database.dispose())
        context.restore_cleanup_completed = True


class ProjectExecution:
    """A worker can be attached once; a later token needs a new process.

    Startup, recovery loops, producers and their children inherit the original
    ContextVar. Neither a new request nor a supervisor restart can retoken them.
    Work and control have independent consumers so a held work request cannot
    prevent its stop, approval or user-input decision from being delivered.
    """

    def __init__(self, context, *, application_factory: Callable = project_application):
        self.context, self.application_factory = context, application_factory
        self.lease: ProjectLease | None = None
        self.application = None
        self.state = "unassigned"
        self.task: asyncio.Task | None = None
        self.closed = False
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._close_task: asyncio.Task | None = None
        self.restored_checkpoint_digest = None
        self.recovery_operation_id = None
        self._activate = asyncio.Event()
        self.activation_started = False
        self.cleanup_complete = False
        self.quiesce_proof = None
        context.rpc.register("execution.attach/v1", self.attach)
        context.rpc.register("execution.status/v1", self.status)
        context.rpc.register("execution.activate/v1", self.activate)
        context.rpc.register("execution.relinquish/v1", self.relinquish)
        context.rpc.register("execution.quiesce/v1", self.quiesce)

    async def status(self, payload):
        if payload:
            raise RuntimeIpcError("Execution status accepts no arguments")
        return {
            "runtime_id": self.context.bootstrap.identity.runtime_id,
            "state": self.state,
            "lease": asdict(self.lease) if self.lease is not None else None,
            "restored_checkpoint_digest": self.restored_checkpoint_digest,
            "recovery_operation_id": self.recovery_operation_id,
            "activation_started": self.activation_started,
            "cleanup_complete": self.cleanup_complete,
            "quiesce_checkpoint": self.quiesce_proof,
        }

    async def quiesce(self, payload):
        if payload:
            raise RuntimeIpcError("Project quiescence accepts no arguments")
        async with self._lock:
            if self.lease is None:
                raise RuntimeIpcError("The project has no ownership grant")
            if self.state == "closed" and self.cleanup_complete and self.quiesce_proof:
                await self.context.coordinator.release_quiesced_project(
                    self.lease, checkpoint=self.quiesce_proof
                )
                return await self.status({})
            try:
                self.quiesce_proof = await self._prepare_quiescence()
            except (CoordinationConflict, RuntimeIpcError) as error:
                # diagnostic-expected: a refused quiescence returns its blockers before any controller cleanup begins.
                # A signed refusal proves no controller cleanup began. This
                # lets the host reopen admission so the blocker can be resolved.
                return {
                    **await self.status({}),
                    "quiesce_blockers": [str(error)[:4000]],
                }
            self.closed, self.state = True, "quiescing"
            self._stop.set()
            if self.task is not None:
                await asyncio.gather(self.task, return_exceptions=True)
            if not self.cleanup_complete or self.state != "closed":
                raise RuntimeIpcError(
                    "Project cleanup is unverified; its ownership is retained"
                )
            await self.context.coordinator.release_quiesced_project(
                self.lease, checkpoint=self.quiesce_proof
            )
            return await self.status({})

    async def _prepare_quiescence(self):
        if self.state != "ready" or self.application is None:
            raise RuntimeIpcError("Only a ready idle project can quiesce")
        agent = getattr(
            getattr(self.application, "state", None), "runtime_handoff", None
        )
        if agent is None:
            raise RuntimeIpcError("This worker cannot prove its shutdown inventory")
        local = list(agent.handles().values())
        chat = agent.chat
        if any(not runtime.done for runtime in chat._active_provider_turns.values()):
            local.append("A provider response is still active")
        if any(not task.done() for task in chat._naming_tasks) or any(
            not item.task.done() for item in chat._precompactions.values()
        ):
            local.append("Conversation maintenance is still active")
        return await self.context.coordinator.quiesce_checkpoint(
            self.lease, local_blockers=local
        )

    async def relinquish(self, payload):
        if payload:
            raise RuntimeIpcError(
                "Failed restoration relinquishment accepts no arguments"
            )
        async with self._lock:
            if (
                self.state != "failed"
                or self.lease is None
                or self.activation_started
                or not self.cleanup_complete
            ):
                raise RuntimeIpcError(
                    "Only a failed, unactivated and cleaned restoration may relinquish"
                )
            await self.context.coordinator.relinquish_failed_restoration(self.lease)
            self.state, self.closed = "fenced", True
            self._stop.set()
            return await self.status({})

    async def activate(self, payload):
        if payload:
            raise RuntimeIpcError("Execution activation accepts no arguments")
        async with self._lock:
            if self.state == "ready":
                return await self.status({})
            if self.state != "prepared" or self.lease is None:
                raise RuntimeIpcError("The project has not prepared its restoration")
            from .runtime_project_handoff import activation_allowed

            await activation_allowed(self.context.coordinator, self.lease)
            self._activate.set()
            return await self.status({})

    async def attach(self, payload):
        try:
            lease = ProjectLease(**MemoryAuthority.model_validate(payload).model_dump())
        except (TypeError, ValueError):
            raise RuntimeIpcError(
                "A valid project ownership grant is required"
            ) from None
        if lease.runtime_id != self.context.bootstrap.identity.runtime_id:
            raise RuntimeIpcError("The grant belongs to another process incarnation")
        async with self._lock:
            if self.closed:
                raise RuntimeIpcError("This execution process is detaching")
            if self.lease is not None:
                if lease != self.lease:
                    raise RuntimeIpcError(
                        "An execution process cannot change its project grant"
                    )
                return await self.status({})
            async with self.context.coordinator.coordination.fenced(lease):
                pass
            self.lease, self.state = lease, "starting"
            with execution_context(lease):
                # diagnostic-expected: the owned lifetime records unexpected failures, updates status, and is joined on close.
                self.task = asyncio.create_task(
                    self._lifetime(lease), name="nebula-project-execution"
                )
        return await self.status({})

    async def _lifetime(self, lease):
        tasks: list[asyncio.Task] = []
        self.context.restore_cleanup_completed = False
        try:
            with execution_context(lease):
                async with self.application_factory(self.context, lease) as app:
                    async with self.context.coordinator.coordination.fenced(lease):
                        pass
                    self.application = app
                    handoff_agent = getattr(
                        getattr(app, "state", None), "runtime_handoff", None
                    )
                    if handoff_agent is not None:
                        self.restored_checkpoint_digest = handoff_agent.restored_digest
                    self.recovery_operation_id = getattr(
                        getattr(app, "state", None),
                        "runtime_recovery_operation_id",
                        None,
                    )
                    deferred = getattr(
                        getattr(app, "state", None), "runtime_activate", None
                    )
                    if deferred is not None:
                        self.state = "prepared"
                        # diagnostic-expected: this Event waiter is cancelled and joined in the immediately enclosing finally.
                        activate_wait = asyncio.create_task(self._activate.wait())
                        # diagnostic-expected: this Event waiter is cancelled and joined in the immediately enclosing finally.
                        stop_wait = asyncio.create_task(self._stop.wait())
                        try:
                            while (
                                not self._activate.is_set() and not self._stop.is_set()
                            ):
                                await asyncio.wait(
                                    [activate_wait, stop_wait],
                                    timeout=HEARTBEAT_SECONDS,
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                async with self.context.coordinator.coordination.fenced(
                                    lease
                                ):
                                    pass
                        finally:
                            activate_wait.cancel()
                            stop_wait.cancel()
                            await asyncio.gather(
                                activate_wait, stop_wait, return_exceptions=True
                            )
                        if self._stop.is_set():
                            return
                        from .runtime_project_handoff import activation_allowed

                        await activation_allowed(self.context.coordinator, lease)
                        self.activation_started = True
                        await deferred()
                        state = getattr(app, "state", None)
                        work = getattr(state, "control_assistant_work", None)
                        chat = getattr(work, "chat", None)
                        if chat is not None:
                            chat.restoring_handoff = False
                    self.state = "ready"
                    delivery = RuntimeDelivery(
                        self.context.coordinator.coordination,
                        RuntimeVault(
                            Path(self.context.bootstrap.data_root)
                            / "runtime"
                            / "payloads"
                        ),
                    )
                    runner = DurableHttpRunner(
                        delivery,
                        app,
                        operator_token=self.context.bootstrap.operator_token.get_secret_value(),
                    )

                    async def consume(lane):
                        while not self._stop.is_set():
                            try:
                                delivered = await runner.run_one(lease, lane=lane)
                            except LeaseLost:
                                # diagnostic-expected: ordinary lease loss stops this consumer and fences the project.
                                self.state = "fenced"
                                self._stop.set()
                                return
                            except Exception as error:
                                # The runner already records uncertain effects.
                                # Keep the other lane and later explicit operator
                                # reconciliation available; never replay here.
                                record_caught_exception(
                                    "runtime",
                                    "runtime.command.uncertain",
                                    "A command needs explicit reconciliation.",
                                    error,
                                    stage="delivery",
                                )
                                delivered = False
                            if not delivered:
                                await asyncio.sleep(0.1)

                    async def watch_lease():
                        while not self._stop.is_set():
                            await asyncio.sleep(HEARTBEAT_SECONDS)
                            try:
                                async with self.context.coordinator.coordination.fenced(
                                    lease
                                ):
                                    pass
                            except LeaseLost:
                                # diagnostic-expected: ordinary lease loss fences and stops the owned project.
                                self.state = "fenced"
                                self._stop.set()
                            except Exception as error:
                                record_caught_exception(
                                    "runtime",
                                    "runtime.project.lease_unavailable",
                                    "Project ownership could not be verified; admission is fenced.",
                                    error,
                                    stage="lease",
                                )
                                self.state = "fenced"
                                self._stop.set()

                    async def watch_handoff():
                        while not self._stop.is_set():
                            try:
                                await handoff_agent.tick()
                            except LeaseLost:
                                # diagnostic-expected: ordinary lease loss stops handoff work and fences the project.
                                self.state = "fenced"
                                self._stop.set()
                                return
                            except Exception as error:
                                record_caught_exception(
                                    "runtime",
                                    "runtime.handoff.waiting",
                                    "Project transfer is waiting for its current resources.",
                                    error,
                                    stage="handoff",
                                )
                            await asyncio.sleep(1)

                    tasks = [
                        # diagnostic-expected: the work consumer classifies operation failures and is cancelled and joined below.
                        asyncio.create_task(
                            consume("work"), name="nebula-project-work"
                        ),
                        # diagnostic-expected: the control consumer classifies operation failures and is cancelled and joined below.
                        asyncio.create_task(
                            consume("control"), name="nebula-project-control"
                        ),
                        # diagnostic-expected: the lease watcher fences and classifies failures and is cancelled and joined below.
                        asyncio.create_task(watch_lease(), name="nebula-project-lease"),
                    ]
                    if handoff_agent is not None:
                        tasks.append(
                            # diagnostic-expected: the handoff watcher classifies operation failures and is cancelled and joined below.
                            asyncio.create_task(
                                watch_handoff(), name="nebula-project-handoff"
                            )
                        )
                    try:
                        await self._stop.wait()
                    finally:
                        self.application = None
                        for task in tasks:
                            task.cancel()

                        async def join_consumers():
                            await asyncio.gather(*tasks, return_exceptions=True)

                        await finish_cleanup(join_consumers())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.state != "fenced":
                self.state = "failed"
            record_caught_exception(
                "runtime",
                "runtime.project.unavailable",
                "The project execution process could not continue.",
                error,
                stage="execution",
            )
        finally:
            self.application = None
            self.cleanup_complete = bool(
                getattr(self.context, "restore_cleanup_completed", False)
            )
            if self.state not in {"failed", "fenced"}:
                self.state = "closed"

    async def app(self, scope, receive, send):
        if self.state != "ready" or self.lease is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4413})
            elif scope["type"] == "http":
                await JSONResponse(
                    {
                        "detail": "Project execution is reconnecting; accepted work remains saved"
                    },
                    status_code=503,
                )(scope, receive, send)
            return
        if scope["type"] == "http" and scope["method"] not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            await JSONResponse(
                {"detail": "Mutations require a durable API receipt"}, status_code=405
            )(scope, receive, send)
            return
        with execution_context(self.lease):
            try:
                async with self.context.coordinator.coordination.fenced(self.lease):
                    pass
            except LeaseLost:
                # diagnostic-expected: ordinary lease loss fences admission and returns the existing reconnect response.
                self.state = "fenced"
                self._stop.set()
                await self.app(scope, receive, send)
                return
            await self.application(scope, receive, send)

    async def close(self):
        if self._close_task is None:
            self.closed = True
            # diagnostic-expected: this cleanup task is shielded and joined by finish_cleanup below.
            self._close_task = asyncio.create_task(self._close())

        async def join():
            await self._close_task

        await finish_cleanup(join())

    async def _close(self):
        async with self._lock:
            self._stop.set()
            if self.task is not None:
                # This is a physical worker shutdown, never an API/viewer
                # disconnect. Join controller cleanup before releasing pools.
                await asyncio.gather(self.task, return_exceptions=True)
            else:
                self.state = "closed"


@asynccontextmanager
async def execution_application(context):
    execution = ProjectExecution(context)
    try:
        yield execution.app
    finally:
        await execution.close()
