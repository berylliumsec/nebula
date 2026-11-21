"""One private host-memory service shared by independently retained workers."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import re
import socket
import stat
import time
from uuid import uuid4
from typing import TYPE_CHECKING

import httpx
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
from .runtime_ipc import (
    RPC_PATH,
    UNDISPATCHED_HEADER,
    RuntimeIpcError,
    RuntimeIpcUndispatched,
    RuntimeRpcClient,
    RuntimeRpcServer,
)
from .runtime_memory_recovery import HostMemoryRecovery
from .runtime_memory_control import HostMemoryControl
from .runtime_browser_bridge import (
    BRIDGE_OPERATION,
    BRIDGE_STATE_NAME,
    HostBrowserBridge,
)
from .runtime_host_settings import HostSettings
from .runtime_host_credentials import SESSION_CREDENTIAL, register_session_credentials
from .runtime_host_pressure import HOST_PRESSURE, register_host_pressure
from .runtime_integration_credentials import (
    INTEGRATION_CREDENTIAL,
    register_integration_credentials,
)
from .runtime_butler_actions import (
    BUTLER_ACTION_RESULT,
    BUTLER_SUBMIT_ACTION,
    register_butler_actions,
)
from .runtime_host_search import register_host_search
from .authentication import secret_matches
from .shared_butler_memory import SharedButlerMemory
from .shared_memory_rpc import register_shared_memory
from .shared_memory_import import CompatibilityMemoryImport
from .shared_knowledge_rpc import SharedKnowledge

if TYPE_CHECKING:
    from .runtime_work_batches import RuntimeWorkBatches

HOST_SOCKET_NAME = "host-services.sock"
# A call that reaches a generation still opening its services waits this long
# for them before it is answered as undispatched; it stays inside the
# caller's 30 s deadline, so a call queued during a swap is simply answered.
READY_WAIT_SECONDS = 20
# A call older than this when the gateway would dispatch it is answered as
# undispatched instead: its caller's 30 s deadline has (nearly) passed, so it
# may have given up, and a call it abandoned must never run later. The age
# comes from the call's authenticated whole-second timestamp.
QUEUED_CALL_LIMIT_SECONDS = 25
PEER_HEADER = b"x-nebula-ipc-worker"


def create_host_knowledge_index(data_root: Path):
    """Open the production index only inside the exclusive host startup boundary."""
    from .knowledge_index import ChromaKnowledgeIndex

    path = os.getenv("NEBULA_V3_KNOWLEDGE_INDEX_DIR")
    return ChromaKnowledgeIndex(Path(path) if path else data_root / "knowledge-index")


def inherited_host_services(descriptor: int) -> socket.socket:
    """Adopt the host-services listening socket held by a longer-lived owner.

    A holder that never restarts (a systemd socket unit) binds the private
    Unix socket once. Each supervisor generation serves it, and worker calls
    made while one generation replaces another wait in the kernel accept
    queue instead of finding no socket. The descriptor must be a listening
    Unix stream socket bound to an absolute path; the socket file must be
    owner-only and owned by this user, in a directory no other user can
    write. The descriptor is never modified or closed on rejection.
    """
    try:
        is_socket = stat.S_ISSOCK(os.fstat(descriptor).st_mode)
    except OSError as error:
        raise ValueError(
            "The inherited host-services descriptor is not open"
        ) from error
    if not is_socket:
        raise ValueError("The inherited host-services descriptor is not a socket")
    listener = socket.socket(fileno=descriptor)
    try:
        if (
            listener.family != socket.AF_UNIX
            or listener.type != socket.SOCK_STREAM
            or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
        ):
            raise ValueError(
                "The inherited host-services socket is not a listening Unix socket"
            )
        path = listener.getsockname()
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(
                "The inherited host-services socket must be bound to an absolute path"
            )
        try:
            details = os.lstat(path)
            parent = os.lstat(os.path.dirname(path))
        except OSError as error:
            raise ValueError(
                "The inherited host-services socket path is missing"
            ) from error
        if (
            not stat.S_ISSOCK(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
        ):
            raise ValueError(
                "The inherited host-services socket must be an owner-only socket "
                "owned by Core's user (SocketMode=0600)"
            )
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid not in (os.getuid(), 0)
            or parent.st_mode & 0o022
        ):
            raise ValueError(
                "The inherited host-services socket must be in a directory only "
                "Core's user or root can write"
            )
    except BaseException:
        listener.detach()
        raise
    listener.set_inheritable(False)
    return listener


class HostServiceClient(RuntimeRpcClient):
    """A worker's host observations wait out a supervisor replacement.

    When a holder keeps the host-services socket (see
    `inherited_host_services`), a call made during a supervisor replacement
    is accepted by the kernel and answered by the next generation; nothing
    here runs. Without a holder, replacing the supervisor removes its socket
    until the next generation listens again, so starting project work, which
    reads host observations (admission pressure, the browser bridge,
    credentials, capabilities), waits for the host here rather than fail the
    work. Only read-only or idempotent calls are resent, within one bounded
    window, after failures that show the host being replaced: a connection
    that was never established, the gateway's explicit undispatched 503, a
    connection the stopping gateway closed before it read the request, and a
    call that waited out its deadline in a holder's queue because no
    generation started. Every other operation and every operation error
    reach the caller unchanged.
    """

    reconnect_seconds = 120.0
    # A timed-out attempt may still be running on the host, so only calls
    # that are harmless to run twice at once are resent after a timeout. The
    # Butler action calls are idempotent only one after another.
    timeout_resendable = frozenset(
        {
            "host.services/v1",
            HOST_PRESSURE,
            BRIDGE_OPERATION,
            SESSION_CREDENTIAL,
            INTEGRATION_CREDENTIAL,
        }
    )
    resendable = frozenset(
        {
            "host.services/v1",
            HOST_PRESSURE,
            BRIDGE_OPERATION,
            SESSION_CREDENTIAL,
            INTEGRATION_CREDENTIAL,
            # A resend finds the same command by its idempotency key.
            BUTLER_SUBMIT_ACTION,
            BUTLER_ACTION_RESULT,
        }
    )

    async def call(self, operation, payload, *, request_id=None, timeout=30):
        if operation not in self.resendable:
            return await super().call(
                operation, payload, request_id=request_id, timeout=timeout
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.reconnect_seconds
        delay = 0.1
        while True:
            try:
                return await super().call(
                    operation, payload, request_id=request_id, timeout=timeout
                )
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                # A stopping gateway closes an accepted connection whose
                # request it never started; the call was not dispatched.
                httpx.RemoteProtocolError,
                RuntimeIpcUndispatched,
            ):
                # diagnostic-expected: the host is being replaced; the bounded wait re-raises once its window ends.
                if loop.time() + delay > deadline:
                    raise
            except httpx.ReadTimeout:
                # Queued in a holder's socket while no generation started.
                # The gateway never dispatches a call that old, but a slow
                # host may still be running it, so only calls harmless to run
                # twice at once are sent again.
                if (
                    operation not in self.timeout_resendable
                    or loop.time() + delay > deadline
                ):
                    raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)


# A guard on the peer table, not a capacity policy: memory admission decides
# how many workers a host runs, and every cached peer is a live worker.
MAX_HOST_PEERS = 1024


class HostPeersBusy(RuntimeIpcError):
    """The peer table is full; nothing was dispatched, so callers resend."""


def host_service_client(bootstrap, secret: bytes) -> RuntimeRpcClient:
    if not bootstrap.identity.endpoint.startswith("unix:///"):
        raise RuntimeIpcError("Host services require a private local runtime endpoint")
    endpoint = Path(bootstrap.identity.endpoint[7:]).parent / HOST_SOCKET_NAME
    client = HostServiceClient("unix://" + str(endpoint), secret)
    client.client.headers[PEER_HEADER.decode()] = bootstrap.identity.runtime_id
    return client


def _call_time(scope) -> float | None:
    """The call's whole-second send time, or None if it carries none.

    The header is authenticated by the call's MAC once dispatched; here it
    only lets an old call be refused early, never admits one.
    """
    for name, value in scope.get("headers", []):
        if name.lower() == b"x-nebula-ipc-time":
            try:
                return float(int(value.decode("ascii")))
            except (UnicodeDecodeError, ValueError):
                # diagnostic-expected: the RPC server rejects a malformed timestamp.
                return None
    return None


def _links_to_socket(path: Path) -> bool:
    """A link a holder-backed generation published: to a socket, or dangling."""
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except FileNotFoundError:
        # diagnostic-expected: the holder is gone; the stale link is replaced.
        return True


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
        listener: socket.socket | None = None,
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
        # A holder-owned socket outlives this generation (see
        # inherited_host_services); otherwise this instance binds its own.
        self.listener = listener
        # Requests wait (bounded) until every service is open.
        self.ready = False
        self._ready = asyncio.Event()
        self._accepting = True
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
                            select(RuntimeInstanceRow.id)
                            .join(
                                RuntimeProcessRow,
                                RuntimeProcessRow.runtime_id == RuntimeInstanceRow.id,
                            )
                            .where(
                                RuntimeInstanceRow.id.in_(tuple(self._peers)),
                                RuntimeInstanceRow.lease_expires_at_ms > now,
                                RuntimeProcessRow.state == "ready",
                            )
                        )
                    )
                    self._peers = {
                        key: value for key, value in self._peers.items() if key in live
                    }
                    # Every cached peer is now a live, ready worker, so the
                    # table is bounded by the workers memory admission let
                    # this host run. The fixed bound only guards the table
                    # itself; it is far above any host's real capacity.
                    if len(self._peers) >= MAX_HOST_PEERS:
                        raise HostPeersBusy(
                            f"Shared host services already serve {len(self._peers)} live workers"
                        )
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
                            "butler_actions": True,
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
                    # Butler's home worker sends its changes to other projects
                    # here; the RPCs refuse any other worker's grant.
                    register_butler_actions(rpc, runtime_id, self.coordination)
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
        sent = _call_time(scope)
        if not self._ready.is_set():
            remaining = READY_WAIT_SECONDS
            if sent is not None:
                remaining = min(
                    remaining, QUEUED_CALL_LIMIT_SECONDS - (time.time() - sent)
                )
            try:
                async with asyncio.timeout(max(0.0, remaining)):
                    await self._ready.wait()
            except TimeoutError:
                # diagnostic-expected: answered below as undispatched; the caller resends.
                pass
        if sent is not None and time.time() - sent > QUEUED_CALL_LIMIT_SECONDS:
            # Waited in a holder's queue across a swap, or for readiness: its
            # caller may have given up, so it is refused before anything runs.
            await JSONResponse(
                {"detail": "This call waited too long for host services"},
                status_code=503,
                headers={UNDISPATCHED_HEADER: "1"},
            )(scope, receive, send)
            return
        if (
            # A holder-backed gateway keeps serving what it already accepted
            # until it stops listening; see _stop_owned.
            (self.closed and (self.listener is None or not self._accepting))
            or not self.ready
            or self.host.closed
            or self.host.lock.descriptor is None
        ):
            # Only a host being replaced is marked: its caller resends the
            # call to the next generation, which never saw it.
            await JSONResponse(
                {"detail": "Shared host services are reconnecting"},
                status_code=503,
                headers={UNDISPATCHED_HEADER: "1"},
            )(scope, receive, send)
            return
        if self._slots.locked():
            # Nothing was dispatched: the caller resends instead of failing,
            # so a worker starting while others are busy does not die.
            await JSONResponse(
                {"detail": "Shared host services are busy"},
                status_code=503,
                headers={UNDISPATCHED_HEADER: "1"},
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
            except HostPeersBusy as error:
                record_caught_exception(
                    "runtime",
                    "runtime.host_services.peers_busy",
                    "Shared host services refused a new worker; it retries "
                    "while other workers retire.",
                    error,
                    stage="host-services",
                )
                await JSONResponse(
                    {"detail": "Shared host peer capacity is busy"},
                    status_code=503,
                    headers={UNDISPATCHED_HEADER: "1"},
                )(scope, receive, send)
                return
            except (RuntimeIpcError, OSError):
                # diagnostic-expected: a missing or rejected private peer yields a bounded unavailable response.
                # Peer capacity and credential failures do not heal by
                # waiting for a replacement; the caller fails at once.
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
                # Calls waiting for readiness are answered at once.
                self._ready.set()
                await finish_cleanup(self._stop_owned())
                raise

    async def _start_owned(self):
        await CompatibilityMemoryImport(self.host).run()
        path = self.host.socket_root / HOST_SOCKET_NAME
        if path.exists() or path.is_symlink():
            details = path.lstat()
            if details.st_uid != os.getuid() or not (
                stat.S_ISSOCK(details.st_mode)
                or (stat.S_ISLNK(details.st_mode) and _links_to_socket(path))
            ):
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
        if self.closed or self.host.closed:
            raise RuntimeError("Shared-service startup was detached")
        self.browser_bridge.restore(self.host.root / BRIDGE_STATE_NAME)
        # Listen before the slow startup work (the knowledge index); calls
        # queued in a holder's socket during the swap are read now and wait
        # for readiness here, within their deadline, rather than in the
        # accept queue.
        await self._listen(path)
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
        self.ready = True
        self._ready.set()

    async def _listen(self, path: Path):
        options = {}
        if self.listener is None:
            if path.is_symlink():
                # A holder-backed generation published this link; this one
                # binds its own socket in its place.
                path.unlink()
            options["uds"] = str(path)
        else:
            self._publish_link(path, self.listener.getsockname())
        self.endpoint = "unix://" + str(path)
        self.server = _HostServer(
            uvicorn.Config(
                self.app,
                interface="asgi3",
                lifespan="off",
                access_log=False,
                log_level="error",
                timeout_graceful_shutdown=None,
                **options,
            )
        )
        self.task = create_diagnostic_task(
            self.server.serve(
                sockets=[self.listener] if self.listener is not None else None
            ),
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
        if self.listener is None:
            details = path.stat()
            self._socket_identity = (details.st_dev, details.st_ino)

    @staticmethod
    def _publish_link(path: Path, target: str):
        """Point workers' fixed endpoint at the holder's socket.

        Workers derive the host endpoint from their own socket directory, so
        retained workers of every release keep finding it. The link outlives
        this generation, and connecting through it reaches the holder's
        accept queue while no generation serves.
        """
        try:
            if path.is_symlink() and os.readlink(path) == target:
                return
        except OSError as error:
            raise RuntimeIpcError("Shared-service endpoint is unreadable") from error
        staged = path.with_name(f".{HOST_SOCKET_NAME}.{uuid4().hex}")
        os.symlink(target, staged)
        try:
            os.replace(staged, path)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

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

    async def _stop_listening(self):
        if not self._accepting:
            return
        if self.server is not None:
            self.server.should_exit = True
        try:
            if self.task is not None:
                await self.task
        finally:
            self._accepting = False
            # Accepted host work ends before another owner can bind its socket
            # or load this host's model/index files.
            if self._requests:
                await asyncio.gather(*tuple(self._requests), return_exceptions=True)

    async def _stop_owned(self):
        failure = None
        if self.listener is not None:
            # With a holder, stop accepting before any service closes: later
            # calls wait in the holder's queue for the next generation instead
            # of being answered "reconnecting", and calls already accepted
            # finish against open services (the browser bridge included).
            try:
                await self._stop_listening()
            except BaseException as error:
                # diagnostic-expected: re-raised below once every service is closed.
                failure = error
            self.browser_bridge.close()
        if self.work_batches is not None:
            await self.work_batches.close()
        await self.host_settings.close()
        await self.recovery.close()
        try:
            if failure is not None:
                raise failure
            await self._stop_listening()
        finally:
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
            # Calls waiting for a startup that will not finish are answered.
            self._ready.set()
            if self.listener is None:
                # A holder-backed bridge closes once its gateway stops
                # accepting (_stop_owned).
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
