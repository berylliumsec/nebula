"""One private host-memory service shared by independently retained workers."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
from typing import TYPE_CHECKING

import uvicorn
from sqlalchemy import select
from starlette.responses import JSONResponse

from .async_utils import finish_cleanup
from .database import RuntimeInstanceRow, RuntimeProcessRow
from .diagnostics import (
    create_diagnostic_task,
    record_caught_exception,
    record_diagnostic,
)
from .knowledge_index import KnowledgeIndexError
from .provider_scheduler import ProviderSchedulerConfig
from .runtime_capacity import RuntimeCapacity
from .runtime_ipc import RPC_PATH, RuntimeIpcError, RuntimeRpcClient, RuntimeRpcServer
from .runtime_memory_recovery import HostMemoryRecovery
from .runtime_memory_control import HostMemoryControl
from .runtime_browser_bridge import HostBrowserBridge
from .runtime_host_settings import HostSettings
from .runtime_host_credentials import register_session_credentials
from .runtime_host_pressure import register_host_pressure
from .runtime_integration_credentials import register_integration_credentials
from .runtime_host_search import register_host_search
from .authentication import secret_matches
from .shared_butler_memory import SharedButlerMemory
from .shared_memory_rpc import register_shared_memory
from .shared_memory_import import CompatibilityMemoryImport
from .shared_knowledge_rpc import SharedKnowledge

if TYPE_CHECKING:
    from .runtime_work_batches import RuntimeWorkBatches

HOST_SOCKET_NAME = "host-services.sock"
PEER_HEADER = b"x-nebula-ipc-worker"


def create_host_knowledge_index(data_root: Path):
    """Open the production index only inside the exclusive host startup boundary."""
    from .knowledge_index import ChromaKnowledgeIndex

    path = os.getenv("NEBULA_V3_KNOWLEDGE_INDEX_DIR")
    return ChromaKnowledgeIndex(Path(path) if path else data_root / "knowledge-index")


def host_service_client(bootstrap, secret: bytes) -> RuntimeRpcClient:
    if not bootstrap.identity.endpoint.startswith("unix:///"):
        raise RuntimeIpcError("Host services require a private local runtime endpoint")
    endpoint = Path(bootstrap.identity.endpoint[7:]).parent / HOST_SOCKET_NAME
    client = RuntimeRpcClient("unix://" + str(endpoint), secret)
    client.client.headers[PEER_HEADER.decode()] = bootstrap.identity.runtime_id
    return client


class _HostServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The stable public listener owns supervisor signal handling.
        yield


class HostSharedServices:
    def __init__(
        self,
        host,
        *,
        knowledge_index=None,
        knowledge_index_factory=None,
        capacity_limits=None,
        memory_budgets=None,
        cleanup_runner_factory=None,
    ):
        if knowledge_index is not None and knowledge_index_factory is not None:
            raise ValueError("Supply a knowledge index or its deferred factory")
        self.host = host
        self._knowledge_index_factory = knowledge_index_factory
        self._owned_knowledge_index = None
        self.coordination = host.coordinator.coordination
        self.memory = SharedButlerMemory(
            self.coordination,
            host.data_root / "butler-memory-index",
            embedding_index=knowledge_index,
        )
        self.capacity = RuntimeCapacity(self.coordination)
        self.recovery = HostMemoryRecovery(
            host, budgets=memory_budgets, runner_factory=cleanup_runner_factory
        )
        self.knowledge = (
            SharedKnowledge(host, knowledge_index)
            if knowledge_index is not None
            else None
        )
        self.capacity_limits = capacity_limits
        self.endpoint: str | None = None
        self.server = None
        self.task = None
        self.closed = False
        self._close_task = None
        self._start_lock = asyncio.Lock()
        self._peers = {}
        self._peer_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(8)
        self._requests: set[asyncio.Task] = set()
        self._socket_identity = None
        self.product_guidance = {"state": "unavailable"}
        self.memory_control = None
        self.browser_bridge = HostBrowserBridge(self.coordination)
        self.host_settings = HostSettings(host)
        self.work_batches: RuntimeWorkBatches | None = None
        if getattr(host, "operator_token", None):
            self.configure_memory_control(host.operator_token)

    def configure_memory_control(self, token):
        if self.memory_control is None:
            self.memory_control = HostMemoryControl(self.host, self.recovery, token)
        elif not secret_matches(self.memory_control.token, token):
            raise ValueError(
                "Host memory controls must use the supervisor's Core authentication"
            )

    async def _peer(self, runtime_id):
        async with self._peer_lock:
            async with self.coordination.database.session() as session:
                process = await session.get(RuntimeProcessRow, runtime_id)
                runtime = await session.get(RuntimeInstanceRow, runtime_id)
                now = await self.coordination._clock_ms(session)
                if (
                    process is None
                    or runtime is None
                    or process.role not in {"execution", "api"}
                    or runtime.role != process.role
                    or process.state != "ready"
                    or runtime.lease_expires_at_ms <= now
                ):
                    return None
                if runtime_id not in self._peers:
                    # A retired/expired incarnation cannot revive. Prune only
                    # such peers, retaining live nonce windows through cache use.
                    live = set(
                        await session.scalars(
                            select(RuntimeInstanceRow.id).where(
                                RuntimeInstanceRow.id.in_(tuple(self._peers)),
                                RuntimeInstanceRow.lease_expires_at_ms > now,
                            )
                        )
                    )
                    self._peers = {
                        key: value for key, value in self._peers.items() if key in live
                    }
                    if len(self._peers) >= 16:
                        raise RuntimeIpcError("Shared host peer capacity is busy")
                    secret = self.host.credentials.read(process.credential_reference)
                    rpc = RuntimeRpcServer(secret)

                    if process.role == "api":
                        from .runtime_work_batches import WORK_BATCH_CAPABILITY

                        async def work_batch_capability(payload):
                            if payload:
                                raise RuntimeIpcError(
                                    "Work batch capability accepts no arguments"
                                )
                            if self.work_batches is None:
                                return {
                                    "protocol": "nebula.work-batches/v1",
                                    "feature": "project.work-batch/v1",
                                    "ready": False,
                                }
                            return self.work_batches.capability()

                        # API peers can inspect only this one capability. They
                        # receive none of the execution-only host operations.
                        rpc.register(WORK_BATCH_CAPABILITY, work_batch_capability)
                        self._peers[runtime_id] = rpc
                        return rpc

                    async def capabilities(payload):
                        if payload:
                            raise RuntimeIpcError(
                                "Host capabilities accept no arguments"
                            )
                        return {
                            "protocol": "nebula.host-services/v1",
                            "memory": True,
                            "capacity": True,
                            "memory_control": self.memory_control is not None,
                            "browser_bridge": True,
                            "provider_credentials": True,
                            "host_pressure": True,
                            "integration_credentials": True,
                            "web_search": True,
                            "host_settings": self.host_settings.assistant_bridge.routes(),
                            "knowledge": self.knowledge is not None,
                            "product_guidance": dict(self.product_guidance),
                        }

                    rpc.register("host.services/v1", capabilities)
                    register_shared_memory(rpc, self.memory, runtime_id=runtime_id)
                    if self.memory_control is not None:
                        self.memory_control.register(rpc, runtime_id)
                    self.browser_bridge.register(rpc, runtime_id)
                    register_session_credentials(
                        rpc,
                        runtime_id,
                        self.coordination,
                        self.host_settings.credentials,
                    )
                    register_host_pressure(
                        rpc,
                        runtime_id,
                        self.coordination,
                        self.host_settings.observations.gate,
                    )
                    register_integration_credentials(
                        rpc,
                        runtime_id,
                        self.coordination,
                        self.host_settings.credentials,
                    )
                    self.host_settings.assistant_bridge.register(rpc, runtime_id)
                    register_host_search(
                        rpc,
                        runtime_id,
                        self.coordination,
                        self.host_settings.observations.search,
                    )
                    if self.knowledge is not None:
                        self.knowledge.register(rpc, runtime_id)
                    self._peers[runtime_id] = rpc
                return self._peers[runtime_id]

    async def app(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4404})
            return
        if scope["path"] != RPC_PATH or scope["method"] != "POST":
            await JSONResponse({"detail": "Not found"}, status_code=404)(
                scope, receive, send
            )
            return
        if (
            self.closed
            or self.host.closed
            or self.host.lock.descriptor is None
            or self._slots.locked()
        ):
            await JSONResponse(
                {"detail": "Shared host services are reconnecting or busy"},
                status_code=503,
            )(scope, receive, send)
            return
        peers = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == PEER_HEADER
        ]
        if len(peers) != 1 or re.fullmatch(rb"[A-Za-z0-9_-]{1,200}", peers[0]) is None:
            await JSONResponse(
                {"detail": "Authenticated runtime peer required"}, status_code=401
            )(scope, receive, send)
            return
        task = asyncio.current_task()
        self._requests.add(task)
        await self._slots.acquire()
        try:
            try:
                peer = await self._peer(peers[0].decode("ascii"))
            except (RuntimeIpcError, OSError):
                # diagnostic-expected: a missing or rejected private peer yields a bounded unavailable response.
                await JSONResponse(
                    {"detail": "Shared host peer is unavailable"}, status_code=503
                )(scope, receive, send)
                return
            if peer is None:
                await JSONResponse(
                    {"detail": "A live execution peer is required"}, status_code=401
                )(scope, receive, send)
                return
            async with asyncio.timeout(30):
                await peer.app(scope, receive, send)
        finally:
            self._slots.release()
            self._requests.discard(task)

    async def start(self):
        async with self._start_lock:
            if self.closed:
                raise RuntimeError(
                    "Create a new host-services instance after detachment"
                )
            if self.task is not None:
                return
            if (
                self.host.closed
                or self.host.lock.descriptor is None
                or self.host.socket_root is None
            ):
                raise RuntimeError(
                    "Shared services require exclusive supervisor ownership"
                )
            if self.host.shared_services not in (None, self):
                raise RuntimeIpcError(
                    "Another shared-service owner is already active on this host"
                )
            self.host.shared_services = self
            try:
                await self._start_owned()
            except BaseException:
                self.closed = True
                await finish_cleanup(self._stop_owned())
                raise

    async def _start_owned(self):
        await CompatibilityMemoryImport(self.host).run()
        path = self.host.socket_root / HOST_SOCKET_NAME
        if path.exists() or path.is_symlink():
            details = path.lstat()
            if not stat.S_ISSOCK(details.st_mode) or details.st_uid != os.getuid():
                raise RuntimeIpcError(
                    "Shared-service endpoint is not an owned Unix socket"
                )
        if (await self.capacity.status())["revision"] == 0:
            config = ProviderSchedulerConfig.from_environment()
            await self.capacity.configure(
                self.capacity_limits
                if self.capacity_limits is not None
                else {
                    "provider": config.provider_limit,
                    "background": config.background_limit,
                    "tool": config.global_tool_limit,
                    "cpu": max(1, min(4096, (os.cpu_count() or 1) - 1)),
                }
            )
        await self._initialize_knowledge()
        if self.knowledge is not None:
            sync = getattr(self.knowledge.index, "sync_product_knowledge", None)
            if callable(sync):
                try:
                    result = await finish_cleanup(asyncio.to_thread(sync))
                    self.product_guidance = {"state": "ready", **result}
                except KnowledgeIndexError:
                    self.product_guidance = {"state": "degraded"}
                    record_diagnostic(
                        "warning",
                        "knowledge",
                        "knowledge.product_chroma_sync_failed",
                        "Packaged product guidance remains available; restart shared services to retry its Chroma mirror.",
                        outcome="degraded",
                        stage="startup",
                        retryable=True,
                    )
        if self.closed or self.host.closed:
            raise RuntimeError("Shared-service startup was detached")
        await self.recovery.start()
        await self.host_settings.start()
        from .runtime_work_batches import RuntimeWorkBatches

        self.work_batches = RuntimeWorkBatches(
            self.host_settings.effect.delivery, host=self.host
        )
        await self.work_batches.start()
        self.endpoint = "unix://" + str(path)
        self.server = _HostServer(
            uvicorn.Config(
                self.app,
                interface="asgi3",
                uds=str(path),
                lifespan="off",
                access_log=False,
                log_level="error",
                timeout_graceful_shutdown=None,
            )
        )
        self.task = create_diagnostic_task(
            self.server.serve(),
            feature="runtime",
            event_code="runtime.host_services",
            failure_message="The private shared host listener could not continue.",
            name="nebula-host-services",
        )
        async with asyncio.timeout(10):
            while not self.server.started:
                if self.task.done():
                    await self.task
                    raise RuntimeIpcError("Shared host listener did not start")
                await asyncio.sleep(0.01)
        details = path.stat()
        self._socket_identity = (details.st_dev, details.st_ino)

    async def _initialize_knowledge(self):
        if self._knowledge_index_factory is None:
            return

        async def initialize():
            try:
                index = await asyncio.to_thread(
                    self._knowledge_index_factory, self.host.data_root
                )
            except (ImportError, OSError, KnowledgeIndexError) as error:
                self.product_guidance = {"state": "degraded"}
                record_caught_exception(
                    "knowledge",
                    "knowledge.host_index_initialization_failed",
                    "Local knowledge indexing is unavailable; packaged product help, keyword memory and host recovery remain available. Restart Core to retry.",
                    error,
                    stage="startup",
                )
                return
            # Retain ownership inside the joined operation: caller cancellation
            # cannot discard a physically opened client before cleanup sees it.
            self._owned_knowledge_index = index
            self.knowledge = SharedKnowledge(self.host, index)
            self.memory.embedding_index = index

        await finish_cleanup(initialize())

    async def _stop_owned(self):
        if self.work_batches is not None:
            await self.work_batches.close()
        await self.host_settings.close()
        await self.recovery.close()
        if self.server is not None:
            self.server.should_exit = True
        try:
            if self.task is not None:
                await self.task
        finally:
            # Accepted host work ends before another owner can bind its socket
            # or load this host's model/index files.
            if self._requests:
                await asyncio.gather(*tuple(self._requests), return_exceptions=True)
            if self.endpoint and self._socket_identity:
                path = Path(self.endpoint[7:])
                if path.exists():
                    details = path.lstat()
                    if (details.st_dev, details.st_ino) != self._socket_identity:
                        raise RuntimeIpcError(
                            "Shared-service socket changed during cleanup"
                        )
                    path.unlink()
            if self._owned_knowledge_index is not None:
                await finish_cleanup(
                    asyncio.to_thread(self._owned_knowledge_index.close)
                )
                self._owned_knowledge_index = None
                self.knowledge = None
                self.memory.embedding_index = None
            if self.host.shared_services is self:
                self.host.shared_services = None

    async def close(self):
        if self._close_task is None:
            self.closed = True
            self.browser_bridge.close()

            async def cleanup():
                async with self._start_lock:
                    await self._stop_owned()

            # diagnostic-expected: this detach task is shielded and joined by finish_cleanup below.
            self._close_task = asyncio.create_task(
                cleanup(), name="nebula-host-services-detach"
            )
        await finish_cleanup(self._join_close())

    async def _join_close(self):
        await self._close_task
