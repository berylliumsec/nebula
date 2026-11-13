"""Retained worker launch and rediscovery without desktop-owned lifetimes."""

from __future__ import annotations

import asyncio
from builtins import BaseExceptionGroup
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
from typing import BinaryIO, IO, Literal, TYPE_CHECKING
from uuid import uuid4

import httpx
from pydantic import Field, SecretStr
from sqlalchemy import select

from .database import (
    DeploymentReceiptRow,
    ProjectOwnershipRow,
    RuntimeProcessRow,
    RuntimeReleaseRow,
)
from .async_utils import finish_cleanup
from .database_pool import PoolSettings
from .deployment_protocol import IPC_PROTOCOL, ReleaseManifest, WorkerIdentity
from .deployments import DeploymentCoordinator
from .domain import NebulaModel
from .process_identity import (
    process_birth,
    process_origin,
    same_process,
    signal_process,
)
from .release_store import ReleaseStore
from .runtime_coordination import CoordinationConflict, ProjectLease
from .runtime_ipc import RuntimeCredentials, RuntimeIpcError, RuntimeRpcClient

if TYPE_CHECKING:
    from .runtime_host_services import HostSharedServices


class WorkerBootstrap(NebulaModel):
    protocol: Literal["nebula.runtime/v1"] = IPC_PROTOCOL
    identity: WorkerIdentity
    data_root: str
    release_root: str
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    database: str = Field(repr=False)
    credential_reference: str
    credential_directory: str
    process_origin: str | None
    pool_settings: dict | None = None
    operator_token: SecretStr | None = Field(default=None, repr=False)

    def private_frame(self) -> bytes:
        values = self.model_dump(mode="json", exclude={"operator_token"})
        values["operator_token"] = (
            self.operator_token.get_secret_value() if self.operator_token else None
        )
        frame = (
            json.dumps(values, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        )
        if len(frame) > 64 * 1024:
            raise ValueError("worker bootstrap exceeds its private channel bound")
        return frame


class MigrationBootstrap(NebulaModel):
    release_root: str
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    database: str = Field(repr=False)
    expected_schema: int
    expected_head: str


def read_worker_bootstrap(source: BinaryIO) -> WorkerBootstrap:
    raw = source.readline(64 * 1024 + 1)
    try:
        if len(raw) > 64 * 1024 or not raw.endswith(b"\n"):
            raise ValueError("incomplete bootstrap")
        return WorkerBootstrap.model_validate_json(raw)
    except ValueError:
        # The frame may contain database and operator credentials. Do not let
        # a validation exception include its input in a diagnostic traceback.
        raise RuntimeIpcError(
            "Invalid private worker bootstrap; no execution was started"
        ) from None


async def validate_worker_binding(
    bootstrap: WorkerBootstrap, coordinator: DeploymentCoordinator
) -> bytes:
    """No service construction precedes the host's committed PID binding."""
    identity = bootstrap.identity
    if identity.pid != os.getpid() or not same_process(
        identity.pid, identity.process_birth
    ):
        raise RuntimeIpcError("worker bootstrap does not identify this OS process")
    if (
        bootstrap.process_origin is not None
        and bootstrap.process_origin != process_origin()
    ):
        raise RuntimeIpcError("worker bootstrap belongs to another process namespace")
    async with coordinator.database.session() as session:
        row = await session.get(RuntimeProcessRow, identity.runtime_id)
        if (
            row is None
            or row.state != "starting"
            or (
                row.pid,
                row.process_birth,
                row.version,
                row.role,
                row.endpoint,
                row.credential_reference,
            )
            != (
                identity.pid,
                identity.process_birth,
                identity.version,
                identity.role,
                identity.endpoint,
                bootstrap.credential_reference,
            )
        ):
            raise RuntimeIpcError(
                "worker incarnation was not bound or its startup was cancelled"
            )
        release = await session.get(RuntimeReleaseRow, identity.version)
        if (
            release is None
            or release.manifest_sha256 != bootstrap.manifest_sha256
            or Path(release.install_path) != Path(bootstrap.release_root)
        ):
            raise RuntimeIpcError(
                "worker bootstrap differs from the accepted retained release"
            )
    return RuntimeCredentials(Path(bootstrap.credential_directory)).read(
        bootstrap.credential_reference
    )


@dataclass(frozen=True)
class ObservedWorker:
    identity: WorkerIdentity | None
    runtime_id: str
    reachable: bool
    process_state: Literal["alive", "gone", "unknown", "unspawned"]
    detail: str


def detached_worker_environment(bundle: Path) -> dict[str, str]:
    values = dict(os.environ)
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "LD_LIBRARY_PATH",
        "LD_LIBRARY_PATH_ORIG",
        "DYLD_LIBRARY_PATH",
    ):
        values.pop(name, None)
    values["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    library = bundle / "lib"
    if library.is_dir():
        values["LD_LIBRARY_PATH"] = str(library)
    return values


async def _write_bootstrap(pipe: IO[bytes], frame: bytes) -> None:
    """Backpressure never blocks the host loop; Popen retains detached lifetime."""
    descriptor = pipe.fileno()
    os.set_blocking(descriptor, False)
    loop = asyncio.get_running_loop()
    remaining = memoryview(frame)
    while remaining:
        try:
            written = os.write(descriptor, remaining)
            remaining = remaining[written:]
        except BlockingIOError:
            # diagnostic-expected: nonblocking bootstrap backpressure waits for writability before retrying.
            ready = loop.create_future()

            def writable():
                if not ready.done():
                    ready.set_result(None)

            loop.add_writer(descriptor, writable)
            try:
                await ready
            finally:
                loop.remove_writer(descriptor)


class HostRuntimeLock:
    """Releasing the host lock deliberately leaves independent workers alive."""

    def __init__(self, root: Path):
        if root.is_symlink():
            raise ValueError("runtime roots cannot be symlinks")
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        details = self.root.stat()
        if details.st_mode & 0o077 or details.st_uid != os.getuid():
            raise ValueError("the host runtime directory must be owner-only")
        self.descriptor: int | None = None

    def acquire(self):
        import fcntl

        if self.descriptor is not None:
            return
        descriptor = os.open(
            self.root / "supervisor.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o077
            ):
                raise ValueError("the supervisor lock file is not private")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


class RuntimeProcessHost:
    def __init__(
        self,
        coordinator: DeploymentCoordinator,
        data_root: Path,
        *,
        pool_settings: PoolSettings | None = None,
        operator_token: str | None = None,
    ):
        self.coordinator = coordinator
        self.data_root = data_root.expanduser().resolve()
        self.root = self.data_root / "runtime"
        self.lock = HostRuntimeLock(self.root)
        self.credentials = RuntimeCredentials(self.root / "credentials")
        self.pool_settings, self.operator_token = pool_settings, operator_token
        self._launch = asyncio.Lock()
        self.children: dict[str, subprocess.Popen] = {}
        self.socket_root: Path | None = None
        self.launch_origin: str | None = None
        self.closed = False
        self.shared_services: HostSharedServices | None = None

    async def start(self):
        async with self._launch:
            if self.closed:
                raise RuntimeError("a closed supervisor cannot admit workers")
            if self.socket_root is not None:
                return
            self.lock.acquire()
            try:
                await self.coordinator.database.initialize()
                await self.coordinator.resume_after_shutdown()
                marker = self.root / "process-origin.json"
                if not marker.exists():
                    sockets = Path(tempfile.mkdtemp(prefix="nebula-ipc-", dir="/tmp"))
                    document = {
                        "process_origin": process_origin(),
                        "socket_root": str(sockets),
                    }
                    descriptor, temporary = tempfile.mkstemp(
                        prefix=".origin-", dir=self.root
                    )
                    try:
                        with os.fdopen(descriptor, "w") as output:
                            json.dump(document, output)
                            output.flush()
                            os.fsync(output.fileno())
                        os.link(temporary, marker)
                    finally:
                        os.unlink(temporary)
                descriptor = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "r") as source:
                    details = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(details.st_mode)
                        or details.st_uid != os.getuid()
                        or details.st_mode & 0o077
                    ):
                        raise ValueError("the process-origin marker is not private")
                    document = json.loads(source.read(4096))
                self.launch_origin = document["process_origin"]
                sockets = Path(document["socket_root"])
                details = sockets.stat()
                if (
                    sockets.is_symlink()
                    or not sockets.is_dir()
                    or details.st_uid != os.getuid()
                    or details.st_mode & 0o077
                ):
                    raise ValueError("worker sockets require an owner-only directory")
                self.socket_root = sockets
            except BaseException:
                self.lock.close()
                raise

    async def close(self):
        self.closed = True

        async def drain_launch():
            if self.shared_services is not None:
                await self.shared_services.close()
            async with self._launch:
                for identity, child in list(self.children.items()):
                    if child.poll() is not None:
                        child.wait()
                        self.children.pop(identity)
                # Neither viewer detach nor supervisor replacement signals children.
                self.lock.close()

        await finish_cleanup(drain_launch())

    async def rpc(self, runtime_id: str) -> RuntimeRpcClient:
        async with self.coordinator.database.session() as session:
            row = await session.get(RuntimeProcessRow, runtime_id)
            if row is None:
                raise CoordinationConflict("unknown worker incarnation")
            return RuntimeRpcClient(
                row.endpoint, self.credentials.read(row.credential_reference)
            )

    async def migrate(self, version: str, *, expected_revision: int) -> dict:
        """Run only an accepted release's migrator while this host owns admission."""
        async with self._launch:
            if self.closed or self.lock.descriptor is None:
                raise CoordinationConflict(
                    "migration requires the active supervisor lock"
                )
            plan = await self.coordinator.migration_preflight(
                version, expected_revision=expected_revision
            )
            async with self.coordinator.database.session() as session:
                release = await session.get(RuntimeReleaseRow, version)
                if release is None:
                    raise CoordinationConflict("stage the migration release first")
                bundle, digest = Path(release.install_path), release.manifest_sha256
            manifest = await asyncio.to_thread(
                ReleaseStore.verify, bundle, expected_manifest_sha256=digest
            )
            if manifest.migration_head == plan["current_head"]:
                return {**plan, "state": "current"}
            bootstrap = MigrationBootstrap(
                release_root=str(bundle),
                manifest_sha256=digest,
                database=self.coordinator.database.url,
                expected_schema=plan["schema"],
                expected_head=plan["current_head"],
            )
            child = await asyncio.create_subprocess_exec(
                str(bundle / manifest.core_executable),
                "runtime-migrate",
                "--bootstrap-stdin",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=detached_worker_environment(bundle),
            )

            # A cancelled request does not interrupt a migration halfway. The
            # operation ledger records uncertainty until authoritative readback.
            async def complete():
                output, _errors = await child.communicate(
                    bootstrap.model_dump_json().encode() + b"\n"
                )
                if child.returncode or len(output) > 64 * 1024:
                    raise CoordinationConflict(
                        "The accepted migration did not confirm completion; inspect current schema before retrying"
                    )
                result = json.loads(output.splitlines()[-1])
                observed = await self.coordinator.migration_preflight(
                    version, expected_revision=None
                )
                if observed["current_head"] != manifest.migration_head:
                    raise CoordinationConflict(
                        "Migration completion differs from the database head"
                    )
                return {**observed, "state": "migrated", "migration": result}

            return await finish_cleanup(complete())

    async def spawn(
        self, version: str, role: Literal["api", "execution"], *, timeout: float = 20
    ) -> WorkerIdentity | None:
        async with self._launch:
            if self.closed or self.lock.descriptor is None or self.socket_root is None:
                raise RuntimeError(
                    "acquire the active supervisor lock before launching workers"
                )
            if (
                not sys.platform.startswith("linux")
                or not hasattr(os, "pidfd_open")
                or not hasattr(signal, "pidfd_send_signal")
            ):
                raise CoordinationConflict(
                    "Detached startup requires verified pidfd cleanup on this host; configure a supported worker agent before admission"
                )
            own_birth = process_birth(os.getpid())
            if own_birth is None or not signal_process(os.getpid(), own_birth, 0):
                raise CoordinationConflict(
                    "This host cannot verify detached-process cleanup before admission"
                )
            if (
                self.launch_origin is not None
                and self.launch_origin != process_origin()
            ):
                raise CoordinationConflict(
                    "process namespace changed; retain resident slots for trusted recovery"
                )
            async with self.coordinator.database.session() as session:
                row = await session.get(RuntimeReleaseRow, version)
                if row is None:
                    raise CoordinationConflict(
                        "stage the immutable bundle before launch"
                    )
                bundle, digest = Path(row.install_path), row.manifest_sha256
                accepted = ReleaseManifest.model_validate(row.manifest)
            manifest = await asyncio.to_thread(
                ReleaseStore.verify, bundle, expected_manifest_sha256=digest
            )
            if manifest != accepted:
                raise CoordinationConflict(
                    "retained assets differ from the accepted release"
                )
            endpoint = "unix://" + str(
                self.socket_root / (uuid4().hex + ".control.sock")
            )
            reference = str(uuid4())
            runtime_id = await self.coordinator.reserve_worker(
                version, role=role, endpoint=endpoint, credential_reference=reference
            )
            if runtime_id is None:
                return None
            child = None
            identity = None
            try:
                self.credentials.create(reference=reference)
                descriptor = os.open(
                    self.root / (runtime_id + ".log"),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as output:
                    child = subprocess.Popen(
                        [
                            str(bundle / manifest.core_executable),
                            "runtime-worker",
                            "--bootstrap-stdin",
                        ],
                        stdin=subprocess.PIPE,
                        bufsize=0,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        close_fds=True,
                        env=detached_worker_environment(bundle),
                    )
                self.children[runtime_id] = child
                birth = process_birth(child.pid)
                if birth is None:
                    raise CoordinationConflict("the worker exited before bootstrap")
                identity = WorkerIdentity(
                    runtime_id=runtime_id,
                    version=version,
                    role=role,
                    pid=child.pid,
                    process_birth=birth,
                    endpoint=endpoint,
                )
                await self.coordinator.bind_spawn(identity)
                bootstrap = WorkerBootstrap(
                    identity=identity,
                    data_root=str(self.data_root),
                    release_root=str(bundle),
                    manifest_sha256=digest,
                    database=self.coordinator.database.url,
                    credential_reference=reference,
                    credential_directory=str(self.credentials.directory),
                    process_origin=self.launch_origin,
                    pool_settings=asdict(self.pool_settings)
                    if self.pool_settings
                    else None,
                    operator_token=SecretStr(self.operator_token)
                    if self.operator_token is not None
                    else None,
                )
                assert child.stdin is not None
                async with asyncio.timeout(timeout):
                    await _write_bootstrap(child.stdin, bootstrap.private_frame())
                    child.stdin.close()
                    client = await self.rpc(runtime_id)
                    try:
                        while True:
                            if child.poll() is not None:
                                raise CoordinationConflict(
                                    "worker startup exited; inspect its private diagnostic log"
                                )
                            try:
                                observed = WorkerIdentity.model_validate(
                                    await client.call("worker.identity", {}, timeout=1)
                                )
                            except (OSError, httpx.HTTPError, RuntimeIpcError):
                                # diagnostic-expected: the bounded startup socket probe retries before any operational command is admitted.
                                # No command is dispatched while the socket is starting.
                                await asyncio.sleep(0.05)
                                continue
                            if observed != identity:
                                raise CoordinationConflict(
                                    "readiness belongs to another worker incarnation"
                                )
                            await self.coordinator.worker_ready(identity)
                            return identity
                    finally:
                        await finish_cleanup(client.close())
            except BaseException:

                async def failed_start():
                    if not await self.coordinator.begin_start_abort(runtime_id):
                        # A committed ready worker can already own a project.
                        # Rediscover it rather than killing on a lost acknowledgement.
                        return
                    failures: list[BaseException] = []
                    if child is not None:
                        try:
                            if identity is not None and same_process(
                                identity.pid, identity.process_birth
                            ):
                                signal_process(
                                    identity.pid, identity.process_birth, signal.SIGKILL
                                )
                        except BaseException as error:
                            # diagnostic-expected: preserve this cleanup failure for the BaseExceptionGroup raised after all steps.
                            failures.append(error)
                        try:
                            if child.stdin is not None and not child.stdin.closed:
                                child.stdin.close()
                        except BaseException as error:
                            # diagnostic-expected: preserve this cleanup failure for the BaseExceptionGroup raised after all steps.
                            failures.append(error)
                        try:
                            await asyncio.to_thread(child.wait, timeout=5)
                        except BaseException as error:
                            # diagnostic-expected: preserve this cleanup failure for the BaseExceptionGroup raised after all steps.
                            failures.append(error)
                    try:
                        await self.coordinator.abort_start(runtime_id)
                    except BaseException as error:
                        # diagnostic-expected: preserve this cleanup failure for the BaseExceptionGroup raised after all steps.
                        failures.append(error)
                    if failures:
                        raise BaseExceptionGroup(
                            "Worker startup cleanup did not complete", failures
                        )

                await finish_cleanup(failed_start())
                raise

    async def reconcile_idle_project_retirements(self, *, limit: int = 8) -> list[str]:
        """Finish bounded idle-project retirements interrupted after quiescence."""
        if not 1 <= limit <= 32:
            raise ValueError("idle-project retirement limit is outside its bound")
        async with self.coordinator.database.session() as session:
            receipts = list(
                await session.scalars(
                    select(DeploymentReceiptRow)
                    .join(
                        RuntimeProcessRow,
                        RuntimeProcessRow.runtime_id
                        == DeploymentReceiptRow.result["runtime_id"].as_string(),
                    )
                    .where(
                        DeploymentReceiptRow.action == "project.quiesced",
                        DeploymentReceiptRow.result["reason"].as_string()
                        == "idle_chat_project",
                        RuntimeProcessRow.state != "retired",
                        ~select(ProjectOwnershipRow.project_id)
                        .where(
                            ProjectOwnershipRow.runtime_id
                            == RuntimeProcessRow.runtime_id
                        )
                        .exists(),
                    )
                    .order_by(DeploymentReceiptRow.occurred_at_ms.desc())
                    .limit(limit * 8)
                )
            )
            candidates = []
            seen = set()
            for receipt in receipts:
                runtime_id = receipt.result.get("runtime_id")
                project_id = receipt.result.get("project_id")
                fencing_token = receipt.result.get("fencing_token")
                if (
                    not isinstance(runtime_id, str)
                    or not runtime_id
                    or not isinstance(project_id, str)
                    or not project_id
                    or not isinstance(fencing_token, int)
                    or runtime_id in seen
                ):
                    continue
                seen.add(runtime_id)
                owner = await session.get(ProjectOwnershipRow, project_id)
                if owner is not None and (
                    owner.fencing_token < fencing_token
                    or owner.runtime_id == runtime_id
                ):
                    continue
                process = await session.get(RuntimeProcessRow, runtime_id)
                if process is None or process.state == "retired":
                    continue
                candidates.append((project_id, runtime_id, fencing_token, process))
                if len(candidates) >= limit:
                    break

        retired = []
        for project_id, runtime_id, fencing_token, process in candidates:
            blockers = await self.coordinator.retirement_blockers(runtime_id)
            if blockers:
                continue
            if same_process(process.pid, process.process_birth):
                client = await self.rpc(runtime_id)
                try:
                    status = await client.call("execution.status/v1", {}, timeout=5)
                except (httpx.HTTPError, OSError, RuntimeIpcError):
                    # diagnostic-expected: an unreachable process remains an unverified retirement candidate.
                    continue
                finally:
                    await client.close()
                lease = status.get("lease")
                if (
                    status.get("runtime_id") != runtime_id
                    or status.get("state") != "closed"
                    or status.get("cleanup_complete") is not True
                    or not isinstance(status.get("quiesce_checkpoint"), dict)
                    or lease is None
                    or lease.get("project_id") != project_id
                    or lease.get("fencing_token") != fencing_token
                ):
                    continue
            await self.retire(runtime_id)
            retired.append(runtime_id)
        return retired

    async def retire_idle_project(self, project_id: str) -> bool:
        """Release and retire this project's worker only after its last chat closes.

        The SQL hold fences new project work before the worker is asked to
        quiesce. Lost replies are reconciled from the durable quiescence receipt;
        any uncertain intermediate state retains the worker for review.
        """
        async with self.coordinator.database.session() as session:
            owner = await session.get(ProjectOwnershipRow, project_id)
            if owner is not None and owner.runtime_id is not None:
                lease = ProjectLease(
                    project_id=project_id,
                    runtime_id=owner.runtime_id,
                    fencing_token=owner.fencing_token,
                )
                runtime_id = owner.runtime_id
            else:
                lease = None
                receipt = await session.scalar(
                    select(DeploymentReceiptRow)
                    .where(
                        DeploymentReceiptRow.action == "project.quiesced",
                        DeploymentReceiptRow.result["project_id"].as_string()
                        == project_id,
                        DeploymentReceiptRow.result["reason"].as_string()
                        == "idle_chat_project",
                    )
                    .order_by(DeploymentReceiptRow.occurred_at_ms.desc())
                    .limit(1)
                )
                if receipt is None:
                    return False
                receipt_runtime_id = receipt.result.get("runtime_id")
                if not isinstance(receipt_runtime_id, str) or not receipt_runtime_id:
                    raise CoordinationConflict(
                        "Idle-project quiescence receipt lacks its runtime identity"
                    )
                runtime_id = receipt_runtime_id
                process = await session.get(RuntimeProcessRow, receipt_runtime_id)
                if process is None or process.state == "retired":
                    return True

        if lease is None:
            # The owner release committed before a lost retirement response.
            # The receipt is exact; begin_retirement still rechecks every
            # runtime owner, resource, command, and capacity reservation.
            await self.retire(runtime_id)
            return True

        if not await self.coordinator.request_project_retirement(lease):
            return False
        client = await self.rpc(runtime_id)
        try:
            status = await client.call("execution.quiesce/v1", {}, timeout=30)
        finally:
            await client.close()
        if status.get("quiesce_blockers"):
            await self.coordinator.abort_project_retirement(lease, worker_status=status)
            return False
        if (
            status.get("runtime_id") != runtime_id
            or status.get("state") != "closed"
            or status.get("cleanup_complete") is not True
            or not isinstance(status.get("quiesce_checkpoint"), dict)
        ):
            raise CoordinationConflict(
                "Project worker did not prove completed quiescence"
            )
        async with self.coordinator.database.session() as session:
            current = await session.get(ProjectOwnershipRow, project_id)
            if (
                current is None
                or (current.runtime_id, current.fencing_token)
                != (
                    None,
                    lease.fencing_token,
                )
                or current.retirement_requested
            ):
                raise CoordinationConflict(
                    "Project ownership release did not persist after quiescence"
                )
            receipt = await session.scalar(
                select(DeploymentReceiptRow)
                .where(
                    DeploymentReceiptRow.action == "project.quiesced",
                    DeploymentReceiptRow.result["project_id"].as_string() == project_id,
                    DeploymentReceiptRow.result["runtime_id"].as_string() == runtime_id,
                    DeploymentReceiptRow.result["fencing_token"].as_integer()
                    == lease.fencing_token,
                    DeploymentReceiptRow.result["reason"].as_string()
                    == "idle_chat_project",
                )
                .order_by(DeploymentReceiptRow.occurred_at_ms.desc())
                .limit(1)
            )
            if receipt is None:
                raise CoordinationConflict(
                    "Durable idle-project quiescence receipt is missing"
                )
        await self.retire(runtime_id)
        return True

    async def retire(self, runtime_id: str, *, timeout: float = 30) -> WorkerIdentity:
        """Request graceful retirement only after SQL fences every admission.

        An unreachable or slow worker keeps its process slot and retained assets.
        There is no forced termination deadline for a resident runtime.
        """
        if not 0.1 <= timeout <= 300:
            raise ValueError("runtime retirement timeout is outside its bound")
        async with self._launch:
            if self.closed or self.lock.descriptor is None:
                raise RuntimeError(
                    "retirement requires the active supervisor host lock"
                )
            if self.launch_origin is None or self.launch_origin != process_origin():
                raise RuntimeIpcError(
                    "The original process namespace cannot verify retirement"
                )
            identity = await self.coordinator.begin_retirement(runtime_id)
            client = await self.rpc(runtime_id)
            try:
                try:
                    await client.call("worker.shutdown", {}, timeout=min(timeout, 5))
                except (httpx.HTTPError, OSError, RuntimeIpcError):
                    # diagnostic-expected: a lost shutdown reply is inconclusive; the following OS identity check proves exit.
                    # A lost reply can follow successful shutdown. Only an OS
                    # identity check, never the missing reply, proves exit.
                    pass
            finally:
                await client.close()
            async with asyncio.timeout(timeout):
                while same_process(identity.pid, identity.process_birth):
                    await asyncio.sleep(0.05)
            child = self.children.get(runtime_id)
            if child is not None:
                await asyncio.to_thread(child.wait, timeout=5)
            await self.coordinator.retired(identity)
            return identity

    async def shutdown(self, *, expected_revision: int) -> dict:
        async with self._launch:
            if self.closed or self.lock.descriptor is None:
                raise CoordinationConflict(
                    "shutdown requires the active supervisor lock"
                )
            leases = await self.coordinator.prepare_shutdown(
                expected_revision=expected_revision
            )
        # A lost reply retains the durable admission hold. Explicit retries
        # inspect the same worker grant instead of restarting its application.
        for lease in leases:
            client = await self.rpc(lease.runtime_id)
            try:
                status = await client.call("execution.quiesce/v1", {}, timeout=30)
                if status.get("quiesce_blockers"):
                    await self.coordinator.abort_shutdown(
                        detail="Shutdown is blocked: "
                        + "; ".join(status["quiesce_blockers"])
                    )
                    raise CoordinationConflict("; ".join(status["quiesce_blockers"]))
                if (
                    status.get("lease") != asdict(lease)
                    or status.get("state") != "closed"
                    or status.get("cleanup_complete") is not True
                ):
                    raise CoordinationConflict(
                        "Idle project cleanup was not verified; retain its shutdown hold"
                    )
            finally:
                await client.close()
        async with self._launch:
            revision = (await self.coordinator.status()).revision
            runtimes = await self.coordinator.begin_shutdown(expected_revision=revision)
        stopped = []
        for runtime_id in runtimes:
            await self.retire(runtime_id)
            stopped.append(runtime_id)
        return {"state": "stopped", "retired_runtime_ids": stopped}

    async def reconcile_processes(self) -> dict:
        """Release only unspawned or positively absent, unowned incarnations."""
        async with self._launch:
            observed = await self.rediscover()
            retired, retained = [], []
            for worker in observed:
                try:
                    if worker.process_state == "unspawned":
                        await self.coordinator.abort_start(worker.runtime_id)
                        retired.append(worker.runtime_id)
                    elif worker.process_state == "gone" and worker.identity is not None:
                        if worker.identity.role == "api":
                            await self.coordinator.retire_missing_api(worker.identity)
                        else:
                            identity = await self.coordinator.begin_retirement(
                                worker.runtime_id
                            )
                            await self.coordinator.retired(identity)
                        retired.append(worker.runtime_id)
                    else:
                        retained.append(
                            {
                                "runtime_id": worker.runtime_id,
                                "state": worker.process_state,
                                "detail": worker.detail,
                            }
                        )
                except CoordinationConflict as error:
                    # diagnostic-expected: a retirement conflict is retained as an explicit reconciliation blocker.
                    retained.append(
                        {
                            "runtime_id": worker.runtime_id,
                            "state": worker.process_state,
                            "detail": str(error)[:4000],
                        }
                    )
            return {"retired_runtime_ids": retired, "retained": retained}

    async def rediscover(self) -> list[ObservedWorker]:
        if self.lock.descriptor is None or self.closed:
            raise RuntimeError("rediscovery requires the active supervisor host lock")
        async with self.coordinator.database.session() as session:
            rows = list(
                await session.scalars(
                    select(RuntimeProcessRow).where(
                        RuntimeProcessRow.state != "retired"
                    )
                )
            )
        result = []
        comparable = (
            self.launch_origin is not None and self.launch_origin == process_origin()
        )
        for row in rows:
            if not row.pid:
                result.append(
                    ObservedWorker(
                        None,
                        row.runtime_id,
                        False,
                        "unspawned",
                        "Startup reservation has no bound process; no bootstrap was released.",
                    )
                )
                continue
            identity = WorkerIdentity.model_validate(
                dict(
                    runtime_id=row.runtime_id,
                    version=row.version,
                    role=row.role,
                    pid=row.pid,
                    process_birth=row.process_birth,
                    endpoint=row.endpoint,
                )
            )
            client = await self.rpc(row.runtime_id)
            try:
                observed = WorkerIdentity.model_validate(
                    await client.call("worker.identity", {}, timeout=2)
                )
                reachable = observed == identity
            except (OSError, httpx.HTTPError, RuntimeIpcError):
                # diagnostic-expected: rediscovery records an unreachable channel and separately verifies OS process identity.
                reachable = False
            finally:
                await client.close()
            if reachable:
                result.append(
                    ObservedWorker(
                        identity,
                        row.runtime_id,
                        True,
                        "alive",
                        "Authenticated worker incarnation verified.",
                    )
                )
            elif not comparable:
                result.append(
                    ObservedWorker(
                        identity,
                        row.runtime_id,
                        False,
                        "unknown",
                        "Process namespace cannot be compared; retain ownership, assets and resident slot.",
                    )
                )
            else:
                alive = same_process(row.pid, row.process_birth)
                result.append(
                    ObservedWorker(
                        identity,
                        row.runtime_id,
                        False,
                        "alive" if alive else "gone",
                        "Control channel unavailable; retain SQL authority while its lease is live."
                        if alive
                        else "Original OS process is gone; project/resource recovery is required before retirement.",
                    )
                )
        return result
