"""Session-scoped Bash automation in a digest-pinned OCI environment.

The model-visible contract is intentionally fixed: ``run_command`` starts a
Bash process and ``process_io`` observes or controls it. Executables inside the
runtime are ordinary PATH entries; they are never represented as installable
Nebula tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .artifacts import ArtifactStore
from .diagnostics import create_diagnostic_task
from .domain import (
    Approval,
    ApprovalStatus,
    Artifact,
    AutomationApprovalPolicy,
    AutomationNetworkMode,
    AutomationProjectPolicy,
    AutomationSession,
    AutomationSessionStatus,
    CommandExecution,
    CommandExecutionStatus,
    Engagement,
    RiskClass,
    RunnerIsolation,
    RunnerProfile as StoredRunnerProfile,
    ScopePolicy,
    ToolCallOrigin,
    WorkspaceChange,
    utc_now,
)
from .redaction import redact_text
from .sandbox import (
    ContainerEgressController,
    ContainerRuntimeType,
    ContainerSandboxRunner,
    EgressLease,
    EgressRule,
    RunnerIsolationMode,
    RunnerPlatform,
    RunnerProfile,
    SandboxContainerUser,
    SandboxExecutionKind,
    SandboxLimits,
    SandboxNetwork,
    SandboxRequest,
    SandboxRootFilesystem,
    SandboxUnavailable,
    SandboxWorkspaceAccess,
    _runtime_environment,
)
from .storage import ConflictError, NebulaStore, NotFoundError


MAX_COMMAND_CHARACTERS = 200_000
MAX_PROCESS_INPUT_BYTES = 1_048_576
MAX_POLL_BYTES = 32_768
MAX_CAPTURE_BYTES = 100 * 1024 * 1024
_IMAGE_PATTERN = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@sha256:[0-9a-f]{64}"
)


class AutomationRuntimeError(RuntimeError):
    """Base class for safe automation runtime failures."""


class AutomationRuntimeUnavailable(AutomationRuntimeError):
    """The pinned local runtime cannot currently be used."""


class AutomationPolicyDenied(AutomationRuntimeError):
    """A hard project or sandbox boundary denied the request."""


class CommandApprovalRequired(AutomationRuntimeError):
    def __init__(self, approval: Approval) -> None:
        super().__init__("command execution requires operator approval")
        self.approval = approval


class RunCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARACTERS)
    cwd: str = Field(default=".", min_length=1, max_length=4_096)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=86_400_000)
    background: bool = False
    network: AutomationNetworkMode = AutomationNetworkMode.NONE

    @field_validator("command")
    @classmethod
    def command_has_no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("command cannot contain NUL bytes")
        return value

    @field_validator("cwd")
    @classmethod
    def cwd_is_workspace_relative(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("cwd must remain inside the project workspace")
        return path.as_posix() or "."


class ProcessIORequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["poll", "write", "terminate"] = "poll"
    input: str | None = Field(default=None, max_length=MAX_PROCESS_INPUT_BYTES)
    max_bytes: int = Field(default=MAX_POLL_BYTES, ge=1, le=MAX_POLL_BYTES)

    @model_validator(mode="after")
    def input_matches_action(self) -> "ProcessIORequest":
        if self.action == "write" and self.input is None:
            raise ValueError("write requires input")
        if self.action != "write" and self.input is not None:
            raise ValueError("input is only accepted by write")
        return self


class CommandResult(BaseModel):
    schema_: Literal["nebula.command-result/v1"] = Field(
        default="nebula.command-result/v1", alias="schema"
    )
    session_id: str
    process_id: str
    execution_id: str
    status: CommandExecutionStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    stdout_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    redacted_stdout_artifact_id: str | None = None
    redacted_stderr_artifact_id: str | None = None
    workspace_changes: list[WorkspaceChange] = Field(default_factory=list)
    network_granted: bool = False
    untrusted_data: bool = True


class AutomationRuntimeInfo(BaseModel):
    configured: bool
    ready: bool
    image: str | None = None
    digest: str | None = None
    runner_profile_id: str | None = None
    detail: str
    inventory: list[dict[str, str]] = Field(default_factory=list)


class RuntimeBackendProcess(ABC):
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader

    @abstractmethod
    async def write(self, data: bytes) -> None:
        raise NotImplementedError

    @abstractmethod
    async def wait(self) -> int:
        raise NotImplementedError

    @abstractmethod
    async def terminate(self) -> None:
        raise NotImplementedError


class RuntimeBackendSession(ABC):
    @property
    @abstractmethod
    def network_enabled(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def enable_network(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def run(
        self, process_id: str, command: str, cwd: str
    ) -> RuntimeBackendProcess:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class SessionLaunch:
    session_id: str
    runner: ContainerSandboxRunner
    image: str
    workspace: Path
    limits: SandboxLimits
    egress_rules: tuple[EgressRule, ...] = ()
    egress_domains: tuple[str, ...] = ()
    egress_ports: tuple[int, ...] = ()
    resolv_conf: Path | None = None
    network_granted: bool = False


class _ContainerProcess(RuntimeBackendProcess):
    def __init__(
        self,
        *,
        process: asyncio.subprocess.Process,
        session: "ContainerRuntimeSession",
        process_id: str,
        pid_file: str,
    ) -> None:
        if process.stdout is None or process.stderr is None:
            raise AutomationRuntimeUnavailable(
                "container process streams are unavailable"
            )
        self.process = process
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.session = session
        self.process_id = process_id
        self.pid_file = pid_file

    async def write(self, data: bytes) -> None:
        if self.process.stdin is None or self.process.returncode is not None:
            raise AutomationRuntimeError("process stdin is closed")
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def wait(self) -> int:
        return int(await self.process.wait())

    async def terminate(self) -> None:
        await self.session._signal_process(self.pid_file, "TERM")
        try:
            await asyncio.wait_for(self.process.wait(), timeout=2)
        except asyncio.TimeoutError:
            # diagnostic-expected: TERM escalation to KILL is bounded cleanup control flow.
            await self.session._signal_process(self.pid_file, "KILL")
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                # diagnostic-expected: the final local kill is the verified fallback.
                if self.process.returncode is None:
                    self.process.kill()
                    await self.process.wait()


class ContainerRuntimeSession(RuntimeBackendSession):
    """A persistent, non-PTY OCI container used only through argv APIs."""

    _PROCESS_WRAPPER = (
        "import os,sys;"
        "pid=os.getpid();"
        "os.setpgid(0,0) if os.getpgrp()!=pid else None;"
        "open(sys.argv[1],'w',encoding='ascii').write(str(os.getpgrp()));"
        "os.execv('/bin/bash',['/bin/bash','--noprofile','--norc','-c',sys.argv[2]])"
    )

    def __init__(
        self,
        *,
        runner: ContainerSandboxRunner,
        container_name: str,
        lease: EgressLease | None,
    ) -> None:
        self.runner = runner
        self.container_name = container_name
        self.lease = lease
        self._closed = False

    @staticmethod
    def name_for_session(session_id: str) -> str:
        return "nebula-runtime-" + re.sub(r"[^a-z0-9]", "", session_id.lower())[:40]

    @classmethod
    async def start(cls, launch: SessionLaunch) -> "ContainerRuntimeSession":
        healthy, detail = await launch.runner.available()
        if not healthy:
            raise AutomationRuntimeUnavailable(detail)
        has_boundary = bool(launch.egress_rules or launch.egress_domains)
        network = SandboxNetwork.SCOPED if has_boundary else SandboxNetwork.NONE
        request = SandboxRequest(
            image=launch.image,
            trusted_local_image=True,
            command=[
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                "while :; do sleep 3600 & wait $!; done",
            ],
            workspace=launch.workspace,
            workspace_access=SandboxWorkspaceAccess.WRITE,
            network=network,
            execution_kind=(
                SandboxExecutionKind.NETWORK_TOOL
                if has_boundary
                else SandboxExecutionKind.LOCAL_TOOL
            ),
            container_user=SandboxContainerUser.NON_ROOT,
            root_filesystem=SandboxRootFilesystem.READ_ONLY,
            egress_rules=list(launch.egress_rules),
            egress_domains=list(launch.egress_domains),
            egress_ports=list(launch.egress_ports),
            resolv_conf=launch.resolv_conf,
            start_egress_disabled=bool(has_boundary and not launch.network_granted),
            environment={
                "HOME": "/tmp",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "TERM": "dumb",
            },
            limits=launch.limits,
        )
        workspace = launch.runner._validate(request)
        container_name = cls.name_for_session(launch.session_id)
        lease: EgressLease | None = None
        if has_boundary:
            if not launch.runner.egress_controller.certified:
                raise AutomationRuntimeUnavailable(
                    "project networking requires the certified egress helper"
                )
            lease = await launch.runner.egress_controller.acquire(
                runtime_argv=launch.runner._runtime_argv(),
                runtime_environment=_runtime_environment(),
                request=request,
                container_name=container_name,
                seccomp_profile=(
                    launch.runner.profile.seccomp_profile
                    if launch.runner.profile is not None
                    else None
                ),
            )
        try:
            argv = launch.runner._argv(
                request,
                workspace,
                container_name=container_name,
                network_mode=lease.network_mode if lease is not None else None,
            )
            run_index = argv.index("run")
            argv.insert(run_index + 1, "--detach")
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_runtime_environment(),
                start_new_session=True,
            )
            stdout, stderr = await _communicate(process, timeout=30)
            if process.returncode != 0:
                detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
                raise AutomationRuntimeUnavailable(
                    "could not start automation container: "
                    + (detail or str(process.returncode))[:1_000]
                )
        except BaseException:
            await launch.runner._force_remove(container_name)
            if lease is not None:
                await lease.close()
            raise
        return cls(runner=launch.runner, container_name=container_name, lease=lease)

    @property
    def network_enabled(self) -> bool:
        return self.lease is not None and self.lease.enabled

    async def enable_network(self) -> None:
        if self.lease is None:
            raise AutomationPolicyDenied(
                "project-scoped networking is unavailable for this frozen session"
            )
        await self.lease.enable()

    async def run(
        self, process_id: str, command: str, cwd: str
    ) -> RuntimeBackendProcess:
        if self._closed:
            raise AutomationRuntimeUnavailable("automation session is closed")
        pid_file = f"/tmp/nebula-process-{process_id}.pid"
        argv = [
            *self.runner._runtime_argv(),
            "exec",
            "--interactive",
            f"--workdir=/workspace/{cwd}" if cwd != "." else "--workdir=/workspace",
            self.container_name,
            "/usr/bin/python3",
            "-c",
            self._PROCESS_WRAPPER,
            pid_file,
            command,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_runtime_environment(),
                start_new_session=True,
            )
        except OSError as exc:
            raise AutomationRuntimeUnavailable(
                f"could not start command process: {exc}"
            ) from exc
        return _ContainerProcess(
            process=process,
            session=self,
            process_id=process_id,
            pid_file=pid_file,
        )

    async def _signal_process(self, pid_file: str, signal_name: str) -> None:
        read = await asyncio.create_subprocess_exec(
            *self.runner._runtime_argv(),
            "exec",
            self.container_name,
            "/bin/cat",
            pid_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_runtime_environment(),
        )
        stdout, _ = await _communicate(read, timeout=5)
        if read.returncode != 0:
            return
        value = stdout.decode("ascii", errors="ignore").strip()
        if not value.isdigit() or int(value) < 2:
            return
        send = await asyncio.create_subprocess_exec(
            *self.runner._runtime_argv(),
            "exec",
            self.container_name,
            "/usr/bin/kill",
            f"-{signal_name}",
            "--",
            f"-{value}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_runtime_environment(),
        )
        try:
            await asyncio.wait_for(send.wait(), timeout=5)
        except asyncio.TimeoutError:
            # diagnostic-expected: helper shutdown escalates after the bounded wait.
            send.kill()
            await send.wait()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.runner._force_remove(self.container_name)
        finally:
            if self.lease is not None:
                await self.lease.close()
                self.lease = None


@dataclass
class _Capture:
    path: Path
    observed: int = 0
    retained: int = 0
    truncated: bool = False

    async def drain(self, stream: asyncio.StreamReader) -> None:
        with self.path.open("wb") as output:
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    break
                self.observed += len(chunk)
                remaining = max(0, MAX_CAPTURE_BYTES - self.retained)
                if remaining:
                    kept = chunk[:remaining]
                    output.write(kept)
                    output.flush()
                    self.retained += len(kept)
                if len(chunk) > remaining:
                    self.truncated = True
            output.flush()
            os.fsync(output.fileno())


@dataclass
class _ManagedProcess:
    execution: CommandExecution
    backend: RuntimeBackendProcess
    stdout: _Capture
    stderr: _Capture
    workspace: Path
    workspace_before: dict[str, tuple[str, int, int]]
    stdout_offset: int = 0
    stderr_offset: int = 0
    forced_status: CommandExecutionStatus | None = None
    drain_tasks: tuple[asyncio.Task[None], asyncio.Task[None]] | None = None
    final_task: asyncio.Task[CommandExecution] | None = None
    finalize_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _ManagedSession:
    entity: AutomationSession
    policy: AutomationProjectPolicy
    scope: ScopePolicy | None
    backend: RuntimeBackendSession
    workspace: Path
    processes: dict[str, _ManagedProcess] = field(default_factory=dict)
    used_approval_ids: set[str] = field(default_factory=set)
    scope_expiry_task: asyncio.Task[None] | None = None


SessionFactory = Callable[[SessionLaunch], Awaitable[RuntimeBackendSession]]
RuntimeResolver = Callable[[str], Awaitable[Any]]
CachedRuntimeProvider = Callable[[], dict[str, Any] | None]


class AutomationRuntimeManager:
    """Own runtime sessions, approvals, process I/O, and immutable output."""

    def __init__(
        self,
        *,
        store: NebulaStore,
        artifact_store: ArtifactStore,
        data_root: str | Path,
        workspace_resolver: Callable[[str], Path],
        runtime_image: str | None = None,
        runtime_resolver: RuntimeResolver | None = None,
        cached_runtime_provider: CachedRuntimeProvider | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        if runtime_image is None and runtime_resolver is None:
            raise ValueError("automation runtime requires an image or Kali resolver")
        if (
            runtime_image is not None
            and _IMAGE_PATTERN.fullmatch(runtime_image) is None
        ):
            raise ValueError(
                "automation runtime image must be repository@sha256:digest"
            )
        self.store = store
        self.artifact_store = artifact_store
        self.data_root = Path(data_root).expanduser().resolve()
        self.workspace_resolver = workspace_resolver
        self.runtime_image = runtime_image or ""
        self.runtime_digest = runtime_image.rsplit("@", 1)[1] if runtime_image else ""
        self.runtime_resolver = runtime_resolver
        self.cached_runtime_provider = cached_runtime_provider
        self.session_factory = session_factory or ContainerRuntimeSession.start
        self.capture_root = self.data_root / "automation-runtime" / "captures"
        self.capture_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.capture_root.chmod(0o700)
        self._sessions: dict[str, _ManagedSession] = {}
        self._owner_sessions: dict[tuple[str, str, str], str] = {}
        self._processes: dict[str, _ManagedProcess] = {}
        self._session_lock = asyncio.Lock()
        self._inventory: list[dict[str, str]] = []
        self._prepared_runner_profile_id: str | None = None
        self._prepared_runner_profile_revision: int | None = None
        self._refresh_cached_runtime()

    @property
    def binary_inventory(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(item) for item in self._inventory)

    def _refresh_cached_runtime(self) -> bool:
        if self.cached_runtime_provider is None:
            return bool(self.runtime_image)
        cached = self.cached_runtime_provider()
        if cached is None:
            return False
        image = cached.get("image")
        digest = cached.get("digest")
        inventory = cached.get("inventory")
        if (
            not isinstance(image, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None
            or digest != image
            or not isinstance(inventory, list)
        ):
            return False
        self.runtime_image = image
        self.runtime_digest = digest
        self._prepared_runner_profile_id = (
            str(cached.get("runner_profile_id") or "") or None
        )
        revision = cached.get("runner_profile_revision")
        self._prepared_runner_profile_revision = (
            revision if isinstance(revision, int) else None
        )
        self._inventory = [
            {str(key): str(value) for key, value in item.items()}
            for item in inventory
            if isinstance(item, dict)
        ]
        return True

    async def startup(self) -> None:
        active_sessions = [
            session
            for session in self._all_entities(AutomationSession)
            if session.status
            in {
                AutomationSessionStatus.STARTING,
                AutomationSessionStatus.READY,
                AutomationSessionStatus.CLOSING,
            }
        ]
        cleanup_limit = asyncio.Semaphore(8)

        async def cleanup(session: AutomationSession) -> str:
            async with cleanup_limit:
                return await self._cleanup_orphan_session(session)

        cleanup_details = await asyncio.gather(
            *(cleanup(item) for item in active_sessions)
        )
        for session, cleanup_detail in zip(
            active_sessions, cleanup_details, strict=True
        ):
            self.store.update(
                AutomationSession,
                session.id,
                {
                    "status": AutomationSessionStatus.INTERRUPTED,
                    "completed_at": utc_now(),
                    "failure_detail": cleanup_detail,
                },
                expected_revision=session.revision,
            )

        for execution in self._all_entities(CommandExecution):
            if execution.status in {
                CommandExecutionStatus.RUNNING,
                CommandExecutionStatus.WAITING_APPROVAL,
            }:
                self.store.update(
                    CommandExecution,
                    execution.id,
                    {
                        "status": CommandExecutionStatus.INTERRUPTED,
                        "completed_at": utc_now(),
                        "error": "Core restarted before the process completed",
                    },
                    expected_revision=execution.revision,
                )

    def _all_entities(self, model: type[Any]) -> list[Any]:
        entities: list[Any] = []
        offset = 0
        while True:
            page = self.store.list_entities(model, offset=offset, limit=1_000)
            entities.extend(page)
            if len(page) < 1_000:
                return entities
            offset += len(page)

    async def _cleanup_orphan_session(self, session: AutomationSession) -> str:
        try:
            profile = self.store.get(StoredRunnerProfile, session.runner_profile_id)
        except NotFoundError:
            # diagnostic-expected: deleted runner state is returned to startup recovery.
            return "Core restarted; runtime teardown skipped because its runner was removed"
        if profile.revision != session.runner_profile_revision:
            return "Core restarted; runtime teardown skipped because its runner changed"
        try:
            runner = self._runner(profile, helper_image=session.runtime_image)
            healthy, detail = await runner.available()
            if not healthy:
                return (
                    "Core restarted; runtime teardown skipped because its runner "
                    f"could not be re-verified: {detail[:2_000]}"
                )
            container_name = ContainerRuntimeSession.name_for_session(session.id)
            await runner._force_remove(container_name)
            await runner._force_remove(f"{container_name}-egress")
        except Exception as exc:
            # diagnostic-expected: orphan cleanup detail is durably returned to reconciliation.
            return f"Core restarted; runtime teardown failed: {str(exc)[:2_000]}"
        return "Core restarted; detached runtime teardown requested"

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(self.close_session(session_id) for session_id in list(self._sessions)),
            return_exceptions=True,
        )

    def project_policy(self, engagement_id: str) -> AutomationProjectPolicy:
        self.store.get(Engagement, engagement_id)
        policy_id = str(
            uuid5(NAMESPACE_URL, f"nebula:automation-policy:{engagement_id}")
        )
        try:
            return self.store.get(AutomationProjectPolicy, policy_id)
        except NotFoundError:
            # diagnostic-expected: first access creates the deterministic default policy.
            try:
                return self.store.create(
                    AutomationProjectPolicy(
                        id=policy_id,
                        engagement_id=engagement_id,
                    )
                )
            except ConflictError:
                # diagnostic-expected: a concurrent creator won; read its durable value.
                return self.store.get(AutomationProjectPolicy, policy_id)

    def update_project_policy(
        self,
        engagement_id: str,
        *,
        approval_policy: AutomationApprovalPolicy,
        network_enabled: bool,
        runner_profile_id: str | None,
        max_timeout_ms: int,
        expected_revision: int | None = None,
    ) -> AutomationProjectPolicy:
        current = self.project_policy(engagement_id)
        if runner_profile_id is not None:
            runner = self.store.get(StoredRunnerProfile, runner_profile_id)
            if not runner.enabled or not runner.healthy:
                raise AutomationRuntimeUnavailable(
                    "selected runner is not verified healthy"
                )
        return self.store.update(
            AutomationProjectPolicy,
            current.id,
            {
                "approval_policy": approval_policy,
                "network_enabled": network_enabled,
                "runner_profile_id": runner_profile_id,
                "max_timeout_ms": max_timeout_ms,
            },
            expected_revision=expected_revision or current.revision,
        )

    async def runtime_info(self) -> AutomationRuntimeInfo:
        prepared = self._refresh_cached_runtime()
        profiles = [
            item
            for item in self.store.list_entities(StoredRunnerProfile, limit=1_000)
            if item.enabled and item.healthy
        ]
        if not profiles:
            return AutomationRuntimeInfo(
                configured=True,
                ready=False,
                image=self.runtime_image or None,
                digest=self.runtime_digest or None,
                detail="no verified healthy container runner is configured",
            )
        profile = next(
            (
                item
                for item in profiles
                if item.id == self._prepared_runner_profile_id
                and item.revision == self._prepared_runner_profile_revision
            ),
            sorted(profiles, key=lambda item: (item.created_at, item.id))[0],
        )
        if not prepared:
            return AutomationRuntimeInfo(
                configured=True,
                ready=False,
                image=self.runtime_image or None,
                digest=self.runtime_digest or None,
                runner_profile_id=profile.id,
                detail="the existing Kali headless runtime has not been prepared",
                inventory=list(self._inventory),
            )
        runner = self._runner(profile)
        healthy, detail = await runner.available()
        return AutomationRuntimeInfo(
            configured=True,
            ready=healthy and prepared,
            image=self.runtime_image or None,
            digest=self.runtime_digest or None,
            runner_profile_id=profile.id,
            detail=(
                detail
                if not healthy
                else "verified prepared Kali headless image is ready"
                if prepared
                else "the existing Kali headless runtime has not been prepared"
            ),
            inventory=list(self._inventory),
        )

    async def prepare(self) -> AutomationRuntimeInfo:
        if self.runtime_resolver is not None:
            engagements = self.store.list_entities(Engagement, limit=1_000)
            if not engagements:
                return AutomationRuntimeInfo(
                    configured=True,
                    ready=False,
                    detail="create a project before preparing the Kali runtime",
                )
            resolution = await self.runtime_resolver(engagements[0].id)
            image = resolution.image
            self.runtime_image = image.resolved_reference
            self.runtime_digest = image.digest
            self._prepared_runner_profile_id = resolution.profile.id
            self._prepared_runner_profile_revision = resolution.profile.revision
            self._inventory = [
                {"name": name, "path": path, "version": version}
                for name, path, version in image.binary_inventory
            ]
            return AutomationRuntimeInfo(
                configured=True,
                ready=True,
                image=self.runtime_image,
                digest=self.runtime_digest,
                runner_profile_id=resolution.profile.id,
                detail=image.detail,
                inventory=list(self._inventory),
            )
        info = await self.runtime_info()
        if not info.ready or info.runner_profile_id is None:
            return info
        profile = self.store.get(StoredRunnerProfile, info.runner_profile_id)
        runner = self._runner(profile)
        stdout, stderr, code = await runner._capture(
            "image", "inspect", self.runtime_image, "--format", "{{json .}}"
        )
        if code != 0:
            process = await asyncio.create_subprocess_exec(
                *runner._runtime_argv(),
                "pull",
                self.runtime_image,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_runtime_environment(),
                start_new_session=True,
            )
            try:
                pulled_stdout, pulled_stderr = await _communicate(
                    process, timeout=1_800
                )
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
                raise
            if process.returncode != 0:
                detail = (pulled_stderr or pulled_stdout).decode(
                    "utf-8", errors="replace"
                )
                return info.model_copy(
                    update={
                        "ready": False,
                        "detail": "pinned automation image preparation failed: "
                        + (detail.strip() or str(process.returncode))[:1_000],
                    }
                )
            stdout, stderr, code = await runner._capture(
                "image", "inspect", self.runtime_image, "--format", "{{json .}}"
            )
            if code != 0:
                return info.model_copy(
                    update={
                        "ready": False,
                        "detail": "pinned image could not be verified after preparation: "
                        + (stderr or stdout or str(code))[:1_000],
                    }
                )
        request = SandboxRequest(
            image=self.runtime_image,
            command=["/bin/cat", "/opt/nebula/runtime-inventory.json"],
            workspace=self.data_root,
            workspace_access=SandboxWorkspaceAccess.NONE,
            limits=SandboxLimits(timeout_seconds=30, output_bytes=2_000_000),
        )
        try:
            result = await runner.run(request)
            payload = json.loads(result.stdout)
            inventory = payload.get("binaries", []) if isinstance(payload, dict) else []
            if not isinstance(inventory, list):
                raise ValueError("runtime inventory binaries must be an array")
            self._inventory = [
                {str(key): str(value) for key, value in item.items()}
                for item in inventory
                if isinstance(item, dict)
            ][:10_000]
        except (ValueError, json.JSONDecodeError, SandboxUnavailable) as exc:
            # diagnostic-expected: readiness reports the verified inventory failure to callers.
            return info.model_copy(
                update={
                    "ready": False,
                    "detail": f"runtime inventory verification failed: {exc}",
                }
            )
        return info.model_copy(
            update={
                "ready": True,
                "detail": "pinned runtime verified",
                "inventory": self._inventory,
            }
        )

    async def session(
        self,
        *,
        engagement_id: str,
        owner_kind: Literal["chat", "mission", "harness", "api"],
        owner_id: str,
    ) -> AutomationSession:
        managed = await self._get_or_create_session(
            engagement_id=engagement_id, owner_kind=owner_kind, owner_id=owner_id
        )
        return managed.entity

    async def run_command(
        self,
        *,
        engagement_id: str,
        owner_kind: Literal["chat", "mission", "harness", "api"],
        owner_id: str,
        request: RunCommandRequest,
        approval: Approval | None = None,
        requested_by: str = "agent",
        tool_call_id: str | None = None,
    ) -> CommandResult:
        managed = await self._get_or_create_session(
            engagement_id=engagement_id, owner_kind=owner_kind, owner_id=owner_id
        )
        timeout_ms = min(
            request.timeout_ms or managed.policy.max_timeout_ms,
            managed.policy.max_timeout_ms,
        )
        self._validate_network_request(managed, request.network)
        needs_approval = (
            managed.policy.approval_policy == AutomationApprovalPolicy.ALWAYS
        )
        if (
            managed.policy.approval_policy == AutomationApprovalPolicy.ON_BOUNDARY
            and request.network == AutomationNetworkMode.PROJECT_SCOPE
            and not managed.entity.network_granted
        ):
            needs_approval = True
        if needs_approval:
            approval = self._authorize_or_request(
                managed,
                request,
                approval=approval,
                requested_by=requested_by,
                owner_kind=owner_kind,
                owner_id=owner_id,
                tool_call_id=tool_call_id,
            )
        elif approval is not None:
            raise AutomationPolicyDenied(
                "an approval was supplied for an automatic command"
            )
        if (
            request.network == AutomationNetworkMode.PROJECT_SCOPE
            and not managed.entity.network_granted
        ):
            await managed.backend.enable_network()
            managed.entity = self.store.update(
                AutomationSession,
                managed.entity.id,
                {"network_granted": True},
                expected_revision=managed.entity.revision,
            )
        process_id = str(uuid4())
        execution = CommandExecution(
            id=self._execution_id(process_id),
            engagement_id=engagement_id,
            session_id=managed.entity.id,
            process_id=process_id,
            command=request.command,
            command_sha256=hashlib.sha256(request.command.encode()).hexdigest(),
            cwd=request.cwd,
            network=request.network,
            background=request.background,
            runtime_digest=self.runtime_digest,
            policy_revision=managed.policy.revision,
            scope_policy_revision=managed.scope.revision if managed.scope else None,
            metadata={
                "owner_kind": owner_kind,
                "owner_id": owner_id,
                "tool_call_id": tool_call_id,
                "approval_id": approval.id if approval is not None else None,
                "timeout_ms": timeout_ms,
            },
        )
        execution = self.store.create(execution)
        workspace_before = await asyncio.to_thread(
            _workspace_snapshot, managed.workspace
        )
        try:
            backend = await managed.backend.run(
                process_id, request.command, request.cwd
            )
        except Exception as exc:
            self.store.update(
                CommandExecution,
                execution.id,
                {
                    "status": CommandExecutionStatus.FAILED,
                    "completed_at": utc_now(),
                    "error": str(exc)[:4_000],
                },
                expected_revision=execution.revision,
            )
            raise
        directory = Path(
            tempfile.mkdtemp(prefix=f"{process_id}-", dir=self.capture_root)
        )
        process = _ManagedProcess(
            execution=execution,
            backend=backend,
            stdout=_Capture(directory / "stdout"),
            stderr=_Capture(directory / "stderr"),
            workspace=managed.workspace,
            workspace_before=workspace_before,
        )
        process.drain_tasks = (
            create_diagnostic_task(
                process.stdout.drain(backend.stdout),
                feature="runtime",
                event_code="runtime.stdout_drain",
                failure_message="Command stdout capture stopped unexpectedly.",
            ),
            create_diagnostic_task(
                process.stderr.drain(backend.stderr),
                feature="runtime",
                event_code="runtime.stderr_drain",
                failure_message="Command stderr capture stopped unexpectedly.",
            ),
        )
        process.final_task = create_diagnostic_task(
            self._finalize_process(process),
            feature="runtime",
            event_code="runtime.process_finalize",
            failure_message="Command finalization stopped unexpectedly.",
        )
        managed.processes[process_id] = process
        self._processes[process_id] = process
        create_diagnostic_task(
            self._timeout_process(process, timeout_ms / 1_000),
            feature="runtime",
            event_code="runtime.process_timeout",
            failure_message="Command timeout supervision stopped unexpectedly.",
        )
        if request.background:
            return self._result(managed, process, stdout="", stderr="")
        await process.final_task
        return await self._poll(managed, process, MAX_POLL_BYTES)

    async def process_io(
        self,
        process_id: str,
        request: ProcessIORequest,
        *,
        engagement_id: str | None = None,
        owner_id: str | None = None,
    ) -> CommandResult:
        process = self._processes.get(process_id)
        if process is None:
            execution = self.store.get(CommandExecution, self._execution_id(process_id))
            if engagement_id is not None and execution.engagement_id != engagement_id:
                raise AutomationPolicyDenied("command process is unavailable")
            if owner_id is not None and execution.metadata.get("owner_id") != owner_id:
                raise AutomationPolicyDenied("command process is unavailable")
            session = self.store.get(AutomationSession, execution.session_id)
            return CommandResult(
                session_id=session.id,
                process_id=process_id,
                execution_id=execution.id,
                status=execution.status,
                exit_code=execution.exit_code,
                stdout_artifact_id=execution.stdout_artifact_id,
                stderr_artifact_id=execution.stderr_artifact_id,
                redacted_stdout_artifact_id=execution.redacted_stdout_artifact_id,
                redacted_stderr_artifact_id=execution.redacted_stderr_artifact_id,
                workspace_changes=execution.workspace_changes,
                output_truncated=execution.stdout_truncated
                or execution.stderr_truncated,
                network_granted=session.network_granted,
            )
        managed = self._sessions.get(process.execution.session_id)
        if managed is None:
            raise AutomationRuntimeUnavailable("automation session is unavailable")
        if (
            engagement_id is not None
            and process.execution.engagement_id != engagement_id
        ):
            raise AutomationPolicyDenied("command process is unavailable")
        if (
            owner_id is not None
            and process.execution.metadata.get("owner_id") != owner_id
        ):
            raise AutomationPolicyDenied("command process is unavailable")
        if request.action == "write":
            assert request.input is not None
            data = request.input.encode("utf-8")
            if len(data) > MAX_PROCESS_INPUT_BYTES:
                raise AutomationRuntimeError("process input exceeds 1048576 bytes")
            await process.backend.write(data)
        elif request.action == "terminate":
            process.forced_status = CommandExecutionStatus.CANCELLED
            await process.backend.terminate()
            if process.final_task is not None:
                await process.final_task
        return await self._poll(managed, process, request.max_bytes)

    def list_processes(self, session_id: str) -> list[CommandExecution]:
        self.store.get(AutomationSession, session_id)
        return [
            item
            for item in self._all_entities(CommandExecution)
            if item.session_id == session_id
        ]

    async def close_session(self, session_id: str) -> AutomationSession:
        managed = self._sessions.get(session_id)
        if managed is None:
            return self.store.get(AutomationSession, session_id)
        current_task = asyncio.current_task()
        if (
            managed.scope_expiry_task is not None
            and managed.scope_expiry_task is not current_task
        ):
            managed.scope_expiry_task.cancel()
        if managed.entity.status == AutomationSessionStatus.READY:
            managed.entity = self.store.update(
                AutomationSession,
                session_id,
                {"status": AutomationSessionStatus.CLOSING},
                expected_revision=managed.entity.revision,
            )
        for process in list(managed.processes.values()):
            if process.final_task is not None and not process.final_task.done():
                process.forced_status = CommandExecutionStatus.CANCELLED
                await process.backend.terminate()
        await asyncio.gather(
            *(
                process.final_task
                for process in managed.processes.values()
                if process.final_task is not None
            ),
            return_exceptions=True,
        )
        await managed.backend.close()
        managed.entity = self.store.get(AutomationSession, session_id)
        if managed.entity.status != AutomationSessionStatus.CLOSED:
            managed.entity = self.store.update(
                AutomationSession,
                session_id,
                {
                    "status": AutomationSessionStatus.CLOSED,
                    "completed_at": utc_now(),
                },
                expected_revision=managed.entity.revision,
            )
        self._sessions.pop(session_id, None)
        self._owner_sessions.pop(
            (
                managed.entity.engagement_id,
                managed.entity.owner_kind,
                managed.entity.owner_id,
            ),
            None,
        )
        for process_id in managed.processes:
            self._processes.pop(process_id, None)
        return managed.entity

    async def _expire_session_at_scope_boundary(
        self, session_id: str, expires_at: datetime
    ) -> None:
        delay = max(0.0, (expires_at - utc_now()).total_seconds())
        try:
            await asyncio.sleep(delay)
            if session_id in self._sessions:
                await self.close_session(session_id)
        except asyncio.CancelledError:
            # diagnostic-expected: session shutdown cancels the scope-expiry timer.
            return

    async def _get_or_create_session(
        self, *, engagement_id: str, owner_kind: str, owner_id: str
    ) -> _ManagedSession:
        key = (engagement_id, owner_kind, owner_id)
        async with self._session_lock:
            existing_id = self._owner_sessions.get(key)
            if existing_id is not None and existing_id in self._sessions:
                return self._sessions[existing_id]
            engagement = self.store.get(Engagement, engagement_id)
            policy = self.project_policy(engagement_id)
            profile = self._select_runner(policy)
            if not self.runtime_image or not self.runtime_digest:
                raise AutomationRuntimeUnavailable(
                    "prepare the existing Kali headless runtime before starting an agent session"
                )
            if (
                self._prepared_runner_profile_id is not None
                and profile.id != self._prepared_runner_profile_id
            ):
                raise AutomationRuntimeUnavailable(
                    "the selected runner does not own the prepared Kali runtime"
                )
            workspace = (
                self.workspace_resolver(engagement_id).expanduser().resolve(strict=True)
            )
            if not workspace.is_dir():
                raise AutomationRuntimeUnavailable("project workspace is unavailable")
            scope = (
                self.store.get(ScopePolicy, engagement.scope_policy_id)
                if engagement.scope_policy_id is not None
                else None
            )
            rules, domains = self._network_boundary(policy, scope)
            has_network_boundary = bool(rules or domains)
            session = AutomationSession(
                engagement_id=engagement_id,
                owner_kind=owner_kind,
                owner_id=owner_id,
                runtime_image=self.runtime_image,
                runtime_digest=self.runtime_digest,
                runner_profile_id=profile.id,
                runner_profile_revision=profile.revision,
                policy_id=policy.id,
                policy_revision=policy.revision,
                scope_policy_id=scope.id if scope is not None else None,
                scope_policy_revision=scope.revision if scope is not None else None,
                # Even an automatic project never receives egress until a
                # command explicitly asks for project_scope.
                network_granted=False,
            )
            session = self.store.create(session)
            resolver_file: Path | None = None
            if domains:
                resolver_directory = self.capture_root / session.id
                resolver_directory.mkdir(mode=0o700)
                resolver_file = resolver_directory / "resolv.conf"
                resolver_file.write_text(
                    "nameserver 127.0.0.53\noptions attempts:1 timeout:2\n",
                    encoding="ascii",
                )
                # The file contains only the loopback resolver address and must
                # be readable by the non-root process inside the container.
                resolver_file.chmod(0o644)
            try:
                backend = await self.session_factory(
                    SessionLaunch(
                        session_id=session.id,
                        runner=self._runner(profile),
                        image=self.runtime_image,
                        workspace=workspace,
                        limits=SandboxLimits(
                            timeout_seconds=max(1, policy.max_timeout_ms // 1_000)
                        ),
                        egress_rules=tuple(rules),
                        egress_domains=tuple(domains),
                        egress_ports=tuple(scope.allowed_ports)
                        if scope is not None and domains
                        else (),
                        resolv_conf=resolver_file,
                        network_granted=session.network_granted,
                    )
                )
            except Exception as exc:
                self.store.update(
                    AutomationSession,
                    session.id,
                    {
                        "status": AutomationSessionStatus.FAILED,
                        "completed_at": utc_now(),
                        "failure_detail": str(exc)[:4_000],
                    },
                    expected_revision=session.revision,
                )
                raise
            session = self.store.update(
                AutomationSession,
                session.id,
                {"status": AutomationSessionStatus.READY},
                expected_revision=session.revision,
            )
            managed = _ManagedSession(
                entity=session,
                policy=policy,
                scope=scope,
                backend=backend,
                workspace=workspace,
            )
            self._sessions[session.id] = managed
            self._owner_sessions[key] = session.id
            if (
                scope is not None
                and scope.not_after is not None
                and has_network_boundary
            ):
                managed.scope_expiry_task = create_diagnostic_task(
                    self._expire_session_at_scope_boundary(session.id, scope.not_after),
                    feature="runtime",
                    event_code="runtime.scope_expiry",
                    failure_message="Scope-expiry supervision stopped unexpectedly.",
                )
            return managed

    def _select_runner(self, policy: AutomationProjectPolicy) -> StoredRunnerProfile:
        if policy.runner_profile_id is not None:
            profile = self.store.get(StoredRunnerProfile, policy.runner_profile_id)
            if profile.enabled and profile.healthy:
                return profile
            raise AutomationRuntimeUnavailable(
                "selected runner is not verified healthy"
            )
        profiles = [
            item
            for item in self.store.list_entities(StoredRunnerProfile, limit=1_000)
            if item.enabled and item.healthy
        ]
        if not profiles:
            raise AutomationRuntimeUnavailable(
                "no verified healthy container runner is configured"
            )
        return sorted(profiles, key=lambda item: (item.created_at, item.id))[0]

    def _runner(
        self, stored: StoredRunnerProfile, *, helper_image: str | None = None
    ) -> ContainerSandboxRunner:
        platform = (
            RunnerPlatform.LINUX
            if stored.isolation == RunnerIsolation.ROOTLESS
            else RunnerPlatform.MACOS
        )
        if stored.isolation == RunnerIsolation.ROOTLESS:
            isolation = RunnerIsolationMode.LINUX_ROOTLESS
            machine = None
        elif stored.isolation == RunnerIsolation.PODMAN_MACHINE:
            isolation = RunnerIsolationMode.PODMAN_MACHINE
            machine = stored.context or "podman-machine-default"
        else:
            isolation = RunnerIsolationMode.DOCKER_DESKTOP_VM
            machine = None
        profile = RunnerProfile(
            runtime_type=ContainerRuntimeType(stored.runtime.value),
            executable=Path(stored.executable),
            platform=platform,
            isolation_mode=isolation,
            context=stored.context,
            machine_name=machine,
            seccomp_profile=Path(stored.seccomp_profile)
            if stored.seccomp_profile
            else None,
        )
        return ContainerSandboxRunner(
            profile=profile,
            egress_controller=ContainerEgressController(
                helper_image=helper_image or self.runtime_image
            ),
            workspace_roots=[self.data_root],
        )

    @staticmethod
    def _network_boundary(
        policy: AutomationProjectPolicy, scope: ScopePolicy | None
    ) -> tuple[list[EgressRule], list[str]]:
        if not policy.network_enabled or scope is None:
            return [], []
        ports = scope.allowed_ports
        rules = [
            EgressRule(address=value, ports=ports, all_ports=not ports)
            for value in scope.allowed_cidrs
        ]
        return _dedupe_rules(rules), list(scope.allowed_domains)

    @staticmethod
    def _validate_network_request(
        managed: _ManagedSession, network: AutomationNetworkMode
    ) -> None:
        if network == AutomationNetworkMode.NONE:
            return
        if not managed.policy.network_enabled:
            raise AutomationPolicyDenied("project networking is disabled")
        if managed.scope is None:
            raise AutomationPolicyDenied("project has no scope policy")
        if managed.scope.not_before and managed.scope.not_before > utc_now():
            raise AutomationPolicyDenied("project scope is not active yet")
        if managed.scope.not_after and managed.scope.not_after <= utc_now():
            raise AutomationPolicyDenied("project scope has expired")
        if not managed.scope.allowed_cidrs and not managed.scope.allowed_domains:
            if managed.scope.allowed_urls:
                raise AutomationPolicyDenied(
                    "URL-only scope cannot authorize arbitrary shell networking"
                )
            raise AutomationPolicyDenied("project scope has no network destinations")
        if managed.backend.network_enabled:
            return

    def _authorize_or_request(
        self,
        managed: _ManagedSession,
        request: RunCommandRequest,
        *,
        approval: Approval | None,
        requested_by: str,
        owner_kind: str,
        owner_id: str,
        tool_call_id: str | None,
    ) -> Approval:
        exact_request = {
            "tool_name": "run_command",
            "arguments": request.model_dump(mode="json"),
            "session_id": managed.entity.id,
            "runtime_digest": self.runtime_digest,
            "policy_revision": managed.policy.revision,
            "argument_editing": False,
        }
        if approval is None:
            created = Approval(
                engagement_id=managed.entity.engagement_id,
                run_id=owner_id,
                origin=(
                    ToolCallOrigin.CHAT
                    if owner_kind == "chat"
                    else ToolCallOrigin.MISSION
                ),
                chat_session_id=owner_id if owner_kind == "chat" else None,
                tool_call_id=tool_call_id,
                risk_class=RiskClass.WORKSPACE_WRITE,
                exact_request=exact_request,
                expected_effects=[
                    "Run an arbitrary Bash command in the project automation container"
                ],
                policy_rationale=(
                    "the project requires approval for every command"
                    if managed.policy.approval_policy == AutomationApprovalPolicy.ALWAYS
                    else "the command requests the project network boundary"
                ),
                requested_by=requested_by,
            )
            created = self.store.create(created)
            raise CommandApprovalRequired(created)
        durable = self.store.get(Approval, approval.id)
        if (
            durable.exact_request != exact_request
            or durable.engagement_id != managed.entity.engagement_id
        ):
            raise AutomationPolicyDenied("approval does not match this exact command")
        if durable.id in managed.used_approval_ids or any(
            item.metadata.get("approval_id") == durable.id
            for item in self.store.list_entities(CommandExecution, limit=1_000)
        ):
            raise AutomationPolicyDenied("approval has already been consumed")
        if durable.status == ApprovalStatus.PENDING:
            raise CommandApprovalRequired(durable)
        if durable.status not in {ApprovalStatus.APPROVED, ApprovalStatus.EDITED}:
            raise AutomationPolicyDenied(f"approval was {durable.status.value}")
        if not durable.decided_by or durable.decided_at is None:
            raise AutomationPolicyDenied("approval is missing its operator decision")
        if durable.status == ApprovalStatus.EDITED:
            raise AutomationPolicyDenied(
                "command approvals cannot edit arbitrary shell text"
            )
        managed.used_approval_ids.add(durable.id)
        return durable

    async def _timeout_process(self, process: _ManagedProcess, seconds: float) -> None:
        if process.final_task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(process.final_task), timeout=seconds)
        except asyncio.TimeoutError:
            # diagnostic-expected: the supervised deadline deliberately terminalizes the process.
            process.forced_status = CommandExecutionStatus.TIMED_OUT
            await process.backend.terminate()

    async def _finalize_process(self, process: _ManagedProcess) -> CommandExecution:
        async with process.finalize_lock:
            exit_code = await process.backend.wait()
            if process.drain_tasks is not None:
                await asyncio.gather(*process.drain_tasks)
            status = process.forced_status or (
                CommandExecutionStatus.COMPLETED
                if exit_code == 0
                else CommandExecutionStatus.FAILED
            )
            metadata = {
                "command_execution_id": process.execution.id,
                "process_id": process.execution.process_id,
                "tool_call_id": process.execution.metadata.get("tool_call_id"),
            }
            stdout, redacted_stdout = self._store_stream_artifacts(
                process.stdout.path,
                engagement_id=process.execution.engagement_id,
                filename=f"command-{process.execution.id}.stdout",
                kind="stdout",
                metadata=metadata,
            )
            stderr, redacted_stderr = self._store_stream_artifacts(
                process.stderr.path,
                engagement_id=process.execution.engagement_id,
                filename=f"command-{process.execution.id}.stderr",
                kind="stderr",
                metadata=metadata,
            )
            workspace_after = await asyncio.to_thread(
                _workspace_snapshot, process.workspace
            )
            workspace_changes = _workspace_changes(
                process.workspace_before, workspace_after
            )
            current = self.store.get(CommandExecution, process.execution.id)
            process.execution = self.store.update(
                CommandExecution,
                current.id,
                {
                    "status": status,
                    "completed_at": utc_now(),
                    "exit_code": exit_code,
                    "stdout_artifact_id": stdout.id,
                    "stderr_artifact_id": stderr.id,
                    "redacted_stdout_artifact_id": redacted_stdout.id,
                    "redacted_stderr_artifact_id": redacted_stderr.id,
                    "observed_stdout_bytes": process.stdout.observed,
                    "observed_stderr_bytes": process.stderr.observed,
                    "stdout_truncated": process.stdout.truncated,
                    "stderr_truncated": process.stderr.truncated,
                    "workspace_changes": workspace_changes,
                    "error": (
                        "command timed out"
                        if status == CommandExecutionStatus.TIMED_OUT
                        else "command was cancelled"
                        if status == CommandExecutionStatus.CANCELLED
                        else None
                    ),
                },
                expected_revision=current.revision,
            )
            return process.execution

    def _store_stream_artifacts(
        self,
        path: Path,
        *,
        engagement_id: str,
        filename: str,
        kind: str,
        metadata: dict[str, Any],
    ) -> tuple[Artifact, Artifact]:
        raw = self.artifact_store.put_file(
            path,
            engagement_id=engagement_id,
            filename=filename,
            media_type="application/octet-stream"
            if b"\x00" in path.read_bytes()[:8_192]
            else "text/plain",
            source="automation-runtime",
            metadata={
                **metadata,
                "kind": kind,
                "searchable": b"\x00" not in path.read_bytes()[:8_192],
            },
        )
        raw = self.store.create(raw)
        visible = redact_text(
            path.read_bytes().decode("utf-8", errors="replace")
        ).encode()
        redacted = self.artifact_store.put_bytes(
            visible,
            engagement_id=engagement_id,
            filename=f"{filename}.redacted.txt",
            media_type="text/plain",
            source="automation-runtime-redaction",
            parent_artifact_id=raw.id,
            metadata={**metadata, "kind": f"redacted_{kind}", "searchable": True},
        )
        redacted = self.store.create(redacted.model_copy(update={"redacted": True}))
        return raw, redacted

    async def _poll(
        self, managed: _ManagedSession, process: _ManagedProcess, maximum: int
    ) -> CommandResult:
        await asyncio.sleep(0)
        stdout, process.stdout_offset = _read_increment(
            process.stdout.path, process.stdout_offset, maximum // 2
        )
        stderr, process.stderr_offset = _read_increment(
            process.stderr.path, process.stderr_offset, maximum - len(stdout)
        )
        if process.final_task is not None and process.final_task.done():
            process.execution = await process.final_task
        return self._result(
            managed,
            process,
            stdout=redact_text(stdout.decode("utf-8", errors="replace")),
            stderr=redact_text(stderr.decode("utf-8", errors="replace")),
        )

    @staticmethod
    def _result(
        managed: _ManagedSession,
        process: _ManagedProcess,
        *,
        stdout: str,
        stderr: str,
    ) -> CommandResult:
        execution = process.execution
        return CommandResult(
            session_id=managed.entity.id,
            process_id=execution.process_id,
            execution_id=execution.id,
            status=execution.status,
            exit_code=execution.exit_code,
            stdout=stdout,
            stderr=stderr,
            output_truncated=execution.stdout_truncated or execution.stderr_truncated,
            stdout_artifact_id=execution.stdout_artifact_id,
            stderr_artifact_id=execution.stderr_artifact_id,
            redacted_stdout_artifact_id=execution.redacted_stdout_artifact_id,
            redacted_stderr_artifact_id=execution.redacted_stderr_artifact_id,
            workspace_changes=execution.workspace_changes,
            network_granted=managed.entity.network_granted,
        )

    @staticmethod
    def _execution_id(process_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"nebula:automation-execution:{process_id}"))


async def _communicate(
    process: asyncio.subprocess.Process, *, timeout: float
) -> tuple[bytes, bytes]:
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise AutomationRuntimeUnavailable("container runtime operation timed out")
    return (stdout or b"")[:2_000_000], (stderr or b"")[:2_000_000]


def _dedupe_rules(rules: list[EgressRule]) -> list[EgressRule]:
    output: list[EgressRule] = []
    seen: set[str] = set()
    for rule in rules:
        key = rule.model_dump_json()
        if key not in seen:
            seen.add(key)
            output.append(rule)
    return output


def _read_increment(path: Path, offset: int, maximum: int) -> tuple[bytes, int]:
    if maximum <= 0 or not path.exists():
        return b"", offset
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(maximum)
        return data, offset + len(data)


def _workspace_snapshot(workspace: Path) -> dict[str, tuple[str, int, int]]:
    snapshot: dict[str, tuple[str, int, int]] = {}
    for root, directories, files in os.walk(workspace, followlinks=False):
        base = Path(root)
        directories[:] = sorted(
            name for name in directories if not (base / name).is_symlink()
        )
        for name in sorted([*directories, *files]):
            path = base / name
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                # diagnostic-expected: workspace files may disappear during a concurrent command.
                continue
            relative = path.relative_to(workspace).as_posix()
            kind = (
                "symlink"
                if path.is_symlink()
                else "directory"
                if path.is_dir()
                else "file"
            )
            snapshot[relative] = (kind, metadata.st_size, metadata.st_mtime_ns)
    return snapshot


def _workspace_changes(
    before: dict[str, tuple[str, int, int]],
    after: dict[str, tuple[str, int, int]],
) -> list[WorkspaceChange]:
    changes: list[WorkspaceChange] = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            changes.append(
                WorkspaceChange(path=path, change="added", size=after[path][1])
            )
        elif path not in after:
            changes.append(WorkspaceChange(path=path, change="deleted"))
        elif before[path] != after[path]:
            changes.append(
                WorkspaceChange(path=path, change="modified", size=after[path][1])
            )
    return changes[:1_000]


__all__ = [
    "AutomationPolicyDenied",
    "AutomationRuntimeError",
    "AutomationRuntimeInfo",
    "AutomationRuntimeManager",
    "AutomationRuntimeUnavailable",
    "CommandApprovalRequired",
    "CommandResult",
    "ContainerRuntimeSession",
    "ProcessIORequest",
    "RunCommandRequest",
    "RuntimeBackendProcess",
    "RuntimeBackendSession",
    "SessionLaunch",
]
