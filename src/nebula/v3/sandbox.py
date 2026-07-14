"""Disposable container execution with a fail-closed analysis-only fallback."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import ipaddress
import json
import logging
import os
import platform as host_platform
import pty
import re
import signal
import struct
import termios
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import utc_now


LOGGER = logging.getLogger(__name__)


class SandboxError(RuntimeError):
    """Base class for normalized sandbox failures."""


class SandboxUnavailable(SandboxError):
    """No approved isolation boundary is available; host execution is forbidden."""


class SandboxNetwork(str, Enum):
    NONE = "none"
    SCOPED = "scoped"
    UNRESTRICTED = "unrestricted"


class SandboxExecutionKind(str, Enum):
    """The isolation contract selected for one disposable invocation."""

    LOCAL_TOOL = "local_tool"
    PARSER = "parser"
    NETWORK_TOOL = "network_tool"
    HUMAN_TERMINAL = "human_terminal"


class SandboxContainerUser(str, Enum):
    NON_ROOT = "65532:65532"
    ROOT = "0:0"


class SandboxRootFilesystem(str, Enum):
    READ_ONLY = "read_only"
    WRITABLE = "writable"


class EgressProtocol(str, Enum):
    TCP = "tcp"


class EgressRule(BaseModel):
    """One broker-approved destination; hostnames are deliberately excluded."""

    model_config = ConfigDict(extra="forbid")

    address: str
    ports: list[int] = Field(min_length=1)
    protocol: EgressProtocol = EgressProtocol.TCP

    @field_validator("address")
    @classmethod
    def valid_address(cls, value: str) -> str:
        return str(ipaddress.ip_address(value))

    @field_validator("ports")
    @classmethod
    def valid_ports(cls, value: list[int]) -> list[int]:
        if any(isinstance(port, bool) or port < 1 or port > 65_535 for port in value):
            raise ValueError("egress ports must be integers between 1 and 65535")
        return sorted(set(value))


class ContainerRuntimeType(str, Enum):
    PODMAN = "podman"
    DOCKER = "docker"


class RunnerPlatform(str, Enum):
    LINUX = "linux"
    MACOS = "macos"


class RunnerIsolationMode(str, Enum):
    LINUX_ROOTLESS = "linux_rootless"
    PODMAN_MACHINE = "podman_machine"
    DOCKER_DESKTOP_VM = "docker_desktop_vm"


def _current_runner_platform() -> RunnerPlatform:
    current = host_platform.system().lower()
    if current == "darwin":
        return RunnerPlatform.MACOS
    if current == "linux":
        return RunnerPlatform.LINUX
    raise ValueError(f"unsupported container runner platform: {current or 'unknown'}")


class RunnerProfile(BaseModel):
    """Explicit, certifiable container-runtime configuration.

    The executable and runtime connection are configuration, never ambient
    PATH/DOCKER_HOST/CONTAINER_HOST state.  Profiles intentionally cover only
    the local runtime arrangements Nebula has an isolation contract for.
    """

    model_config = ConfigDict(extra="forbid")

    runtime_type: ContainerRuntimeType
    executable: Path
    platform: RunnerPlatform = Field(default_factory=_current_runner_platform)
    isolation_mode: RunnerIsolationMode
    context: str | None = Field(default=None, min_length=1, max_length=128)
    machine_name: str | None = Field(default=None, min_length=1, max_length=128)
    seccomp_profile: Path | None = None

    @field_validator("executable")
    @classmethod
    def absolute_matching_executable(cls, value: Path) -> Path:
        candidate = value.expanduser()
        if not candidate.is_absolute():
            raise ValueError("container runtime executable must be an absolute path")
        if candidate.name not in {"docker", "podman"}:
            raise ValueError("container runtime executable must be docker or podman")
        return candidate

    @field_validator("seccomp_profile")
    @classmethod
    def absolute_seccomp_profile(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        candidate = value.expanduser()
        if not candidate.is_absolute():
            raise ValueError("seccomp profile must be an absolute path")
        return candidate

    @field_validator("context", "machine_name")
    @classmethod
    def safe_runtime_identifier(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise ValueError(
                "runtime identifiers may contain only letters, digits, ._- "
            )
        return value

    @model_validator(mode="after")
    def supported_combination(self) -> "RunnerProfile":
        if self.executable.name != self.runtime_type.value:
            raise ValueError("runtime_type must match the configured executable")
        if self.platform == RunnerPlatform.LINUX:
            if self.isolation_mode != RunnerIsolationMode.LINUX_ROOTLESS:
                raise ValueError("Linux runners must use linux_rootless isolation")
            if self.machine_name is not None:
                raise ValueError("machine_name is only valid for Podman Machine")
        elif self.runtime_type == ContainerRuntimeType.PODMAN:
            if self.isolation_mode != RunnerIsolationMode.PODMAN_MACHINE:
                raise ValueError(
                    "macOS Podman runners must use podman_machine isolation"
                )
            if not self.machine_name:
                raise ValueError("Podman Machine profiles require machine_name")
            if self.context is None:
                self.context = self.machine_name
        else:
            if self.isolation_mode != RunnerIsolationMode.DOCKER_DESKTOP_VM:
                raise ValueError(
                    "macOS Docker runners must use docker_desktop_vm isolation"
                )
            if self.machine_name is not None:
                raise ValueError("machine_name is only valid for Podman Machine")
        return self

    @classmethod
    def from_runtime(
        cls,
        executable: str | Path,
        *,
        platform: RunnerPlatform | None = None,
    ) -> "RunnerProfile":
        path = Path(executable).expanduser()
        runtime_type = ContainerRuntimeType(path.name)
        selected_platform = platform or _current_runner_platform()
        if selected_platform == RunnerPlatform.LINUX:
            mode = RunnerIsolationMode.LINUX_ROOTLESS
            machine_name = None
        elif runtime_type == ContainerRuntimeType.PODMAN:
            mode = RunnerIsolationMode.PODMAN_MACHINE
            machine_name = "podman-machine-default"
        else:
            mode = RunnerIsolationMode.DOCKER_DESKTOP_VM
            machine_name = None
        return cls(
            runtime_type=runtime_type,
            executable=path,
            platform=selected_platform,
            isolation_mode=mode,
            machine_name=machine_name,
        )


class SandboxWorkspaceAccess(str, Enum):
    NONE = "none"
    READ = "read"
    WRITE = "workspace_write"


class SandboxLimits(BaseModel):
    cpu_count: float = Field(default=1.0, gt=0, le=64)
    memory_mb: int = Field(default=512, ge=32, le=131_072)
    pids: int = Field(default=128, ge=1, le=32_768)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    output_bytes: int = Field(default=2_000_000, ge=1, le=100_000_000)


class SandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str
    command: list[str] = Field(min_length=1)
    workspace: Path
    workspace_access: SandboxWorkspaceAccess = SandboxWorkspaceAccess.NONE
    environment: dict[str, str] = Field(default_factory=dict)
    network: SandboxNetwork = SandboxNetwork.NONE
    execution_kind: SandboxExecutionKind | None = None
    container_user: SandboxContainerUser = SandboxContainerUser.NON_ROOT
    root_filesystem: SandboxRootFilesystem = SandboxRootFilesystem.READ_ONLY
    egress_rules: list[EgressRule] = Field(default_factory=list)
    # Kept for wire compatibility with the earlier prototype. A named bridge
    # is never sufficient authorization for run(); certified egress uses a
    # fresh helper namespace and egress_rules instead.
    network_name: str | None = None
    pinned_hosts: dict[str, str] = Field(default_factory=dict)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)

    @field_validator("command")
    @classmethod
    def command_has_no_nul(cls, values: list[str]) -> list[str]:
        if any("\x00" in value for value in values):
            raise ValueError("command arguments cannot contain NUL bytes")
        return values

    @field_validator("pinned_hosts")
    @classmethod
    def valid_pins(cls, values: dict[str, str]) -> dict[str, str]:
        return {
            host.rstrip(".").lower(): str(ipaddress.ip_address(address))
            for host, address in values.items()
        }

    @model_validator(mode="after")
    def scoped_network_has_boundary(self) -> "SandboxRequest":
        if self.execution_kind is None:
            self.execution_kind = (
                SandboxExecutionKind.NETWORK_TOOL
                if self.network == SandboxNetwork.SCOPED
                else SandboxExecutionKind.LOCAL_TOOL
            )
        if self.execution_kind == SandboxExecutionKind.NETWORK_TOOL:
            if self.network != SandboxNetwork.SCOPED:
                raise ValueError("network tools require scoped network execution")
            if not self.network_name and not self.egress_rules:
                raise ValueError(
                    "scoped network execution requires a legacy network_name "
                    "or certified egress rules"
                )
        elif self.execution_kind == SandboxExecutionKind.HUMAN_TERMINAL:
            if self.network != SandboxNetwork.UNRESTRICTED:
                raise ValueError(
                    "human terminals require unrestricted bridge networking"
                )
            if self.container_user != SandboxContainerUser.ROOT:
                raise ValueError("human terminals require the container root user")
            if self.root_filesystem != SandboxRootFilesystem.WRITABLE:
                raise ValueError("human terminals require a writable root filesystem")
        elif self.network != SandboxNetwork.NONE:
            raise ValueError("local tools and parsers must use network=none")
        if self.execution_kind != SandboxExecutionKind.HUMAN_TERMINAL:
            if self.container_user != SandboxContainerUser.NON_ROOT:
                raise ValueError("only human terminals may use the container root user")
            if self.root_filesystem != SandboxRootFilesystem.READ_ONLY:
                raise ValueError(
                    "only human terminals may use a writable root filesystem"
                )
        if (
            self.execution_kind == SandboxExecutionKind.PARSER
            and self.workspace_access
            not in {
                SandboxWorkspaceAccess.NONE,
                SandboxWorkspaceAccess.READ,
            }
        ):
            raise ValueError("parser containers cannot write to the workspace")
        if self.network == SandboxNetwork.NONE:
            if self.egress_rules:
                raise ValueError("offline execution cannot declare egress rules")
            if self.pinned_hosts:
                raise ValueError("offline execution cannot declare pinned hosts")
        if self.network == SandboxNetwork.UNRESTRICTED and any(
            (self.network_name, self.egress_rules, self.pinned_hosts)
        ):
            raise ValueError(
                "unrestricted human-terminal networking cannot declare scoped egress"
            )
        if self.egress_rules:
            allowed_addresses = {rule.address for rule in self.egress_rules}
            if any(
                address not in allowed_addresses
                for address in self.pinned_hosts.values()
            ):
                raise ValueError(
                    "pinned host addresses must be present in egress rules"
                )
        return self


class SandboxResult(BaseModel):
    command: list[str]
    image: str
    runtime: str
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0)
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False


class EgressLease(ABC):
    """A short-lived, policy-configured network namespace."""

    network_mode: str

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


class EgressController(ABC):
    """Creates a fresh egress boundary for exactly one tool invocation."""

    certified: bool = False

    @abstractmethod
    async def acquire(
        self,
        *,
        runtime_argv: list[str],
        runtime_environment: dict[str, str],
        request: SandboxRequest,
        container_name: str,
        seccomp_profile: Path | None,
    ) -> EgressLease:
        raise NotImplementedError


class NoEgressController(EgressController):
    async def acquire(
        self,
        *,
        runtime_argv: list[str],
        runtime_environment: dict[str, str],
        request: SandboxRequest,
        container_name: str,
        seccomp_profile: Path | None,
    ) -> EgressLease:
        del runtime_argv, runtime_environment, request, container_name, seccomp_profile
        raise SandboxUnavailable(
            "network tool execution requires a certified per-invocation egress helper"
        )


@dataclass
class _ContainerEgressLease(EgressLease):
    network_mode: str
    helper_name: str
    runtime_argv: list[str]
    runtime_environment: dict[str, str]
    process: asyncio.subprocess.Process
    drain_task: asyncio.Task[None]

    async def close(self) -> None:
        try:
            stop = await asyncio.create_subprocess_exec(
                *self.runtime_argv,
                "stop",
                "--time=0",
                self.helper_name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=self.runtime_environment,
            )
            await asyncio.wait_for(stop.wait(), timeout=10)
        except (OSError, asyncio.TimeoutError):
            if self.process.returncode is None:
                self.process.kill()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        self.drain_task.cancel()
        try:
            await self.drain_task
        except asyncio.CancelledError:
            pass


class ContainerEgressController(EgressController):
    """Run a digest-pinned helper and share only its filtered namespace.

    The certified helper contract is deliberately small: configure the exact
    IP/protocol/port rules passed as argv, print ``READY`` followed by a newline
    only after the rules are active, then remain alive and otherwise quiet.
    The tool container never receives NET_ADMIN and joins the helper's network
    namespace with ``--network=container:<helper>``.
    """

    certified = True
    _digest_pattern = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")

    def __init__(
        self,
        *,
        helper_image: str,
        helper_executable: str = "/usr/local/bin/nebula-egress",
        readiness_timeout_seconds: float = 10.0,
    ) -> None:
        if not self._digest_pattern.fullmatch(helper_image):
            raise ValueError("egress helper image must be pinned by sha256 digest")
        if "\x00" in helper_executable or not Path(helper_executable).is_absolute():
            raise ValueError("egress helper executable must be absolute")
        if readiness_timeout_seconds <= 0 or readiness_timeout_seconds > 60:
            raise ValueError("egress helper readiness timeout must be between 0 and 60")
        self.helper_image = helper_image
        self.helper_executable = helper_executable
        self.readiness_timeout_seconds = readiness_timeout_seconds

    async def acquire(
        self,
        *,
        runtime_argv: list[str],
        runtime_environment: dict[str, str],
        request: SandboxRequest,
        container_name: str,
        seccomp_profile: Path | None,
    ) -> EgressLease:
        if request.execution_kind != SandboxExecutionKind.NETWORK_TOOL:
            raise SandboxError("egress leases are only valid for network tools")
        if not request.egress_rules:
            raise SandboxUnavailable(
                "network execution requires at least one broker-approved egress rule"
            )
        helper_name = f"{container_name}-egress"
        argv = [
            *runtime_argv,
            "run",
            "--rm",
            f"--name={helper_name}",
            "--pull=never",
            "--read-only",
            "--cap-drop=ALL",
            "--cap-add=NET_ADMIN",
            "--security-opt=no-new-privileges",
            "--network=bridge",
            "--user=0:0",
            "--pids-limit=32",
            "--memory=64m",
            "--cpus=0.25",
            "--tmpfs=/run:rw,noexec,nosuid,nodev,size=4m",
            f"--entrypoint={self.helper_executable}",
        ]
        if seccomp_profile is not None:
            argv.append(f"--security-opt=seccomp={seccomp_profile}")
        argv.extend([self.helper_image, "serve"])
        for rule in request.egress_rules:
            for port in rule.ports:
                argv.extend(
                    [
                        "--allow",
                        f"{rule.protocol.value}://{_bracket_ip(rule.address)}:{port}",
                    ]
                )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=runtime_environment,
            )
        except OSError as exc:
            raise SandboxUnavailable(f"could not start egress helper: {exc}") from exc
        assert process.stdout is not None
        try:
            line = await asyncio.wait_for(
                process.stdout.readline(), timeout=self.readiness_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise SandboxUnavailable("egress helper did not become ready") from exc
        if line.rstrip(b"\r\n") != b"READY" or process.returncode is not None:
            process.kill()
            await process.wait()
            detail = line.decode("utf-8", errors="replace").strip()
            raise SandboxUnavailable(
                f"egress helper failed closed before readiness: {detail or 'no status'}"
            )
        drain_task = asyncio.create_task(_discard_stream(process.stdout))
        return _ContainerEgressLease(
            network_mode=f"container:{helper_name}",
            helper_name=helper_name,
            runtime_argv=runtime_argv,
            runtime_environment=runtime_environment,
            process=process,
            drain_task=drain_task,
        )


class SandboxRunner(ABC):
    @abstractmethod
    async def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    @abstractmethod
    async def run(self, request: SandboxRequest) -> SandboxResult:
        raise NotImplementedError

    async def run_stream(
        self,
        request: SandboxRequest,
        *,
        input_bytes: bytes = b"",
        on_chunk: Callable[[str, bytes], Awaitable[None]] | None = None,
        container_name: str | None = None,
    ) -> SandboxResult:
        if input_bytes:
            raise SandboxUnavailable("this sandbox runner does not accept source input")
        result = await self.run(request)
        if on_chunk is not None:
            if result.stdout:
                await on_chunk("stdout", result.stdout.encode("utf-8"))
            if result.stderr:
                await on_chunk("stderr", result.stderr.encode("utf-8"))
        return result


class AnalysisOnlyRunner(SandboxRunner):
    """Explicitly represents a deployment without executable isolation."""

    async def available(self) -> tuple[bool, str]:
        return False, "no approved rootless container runner is configured"

    async def run(self, request: SandboxRequest) -> SandboxResult:
        del request
        raise SandboxUnavailable(
            "tool execution is disabled: configure a rootless Docker/Podman runner; "
            "Nebula will never fall back to the host"
        )


@dataclass
class SandboxTerminalProcess:
    """A PTY attached only to one named OCI container-runtime process.

    The PTY is an I/O transport for ``docker/podman run --interactive --tty``.
    It never resolves or launches a host shell.  Closing the transport always
    removes the named container and releases any scoped-egress helper.
    """

    process: asyncio.subprocess.Process
    master_fd: int
    container_name: str
    runner: "ContainerSandboxRunner"
    egress_lease: EgressLease | None = None
    _closed: bool = False
    _close_lock: asyncio.Lock | None = None

    def __post_init__(self) -> None:
        os.set_blocking(self.master_fd, False)
        self._close_lock = asyncio.Lock()

    async def read(self, maximum_bytes: int = 32_768) -> bytes:
        if maximum_bytes < 1 or maximum_bytes > 32_768:
            raise ValueError("terminal reads must be between 1 and 32768 bytes")
        while not self._closed:
            try:
                return os.read(self.master_fd, maximum_bytes)
            except BlockingIOError:
                await _wait_for_fd(self.master_fd, writable=False)
            except OSError as exc:
                if exc.errno in {errno.EBADF, errno.EIO}:
                    return b""
                raise
        return b""

    async def write(self, data: bytes) -> None:
        if not data:
            return
        if len(data) > 1024 * 1024:
            raise ValueError("terminal input exceeds 1048576 bytes")
        view = memoryview(data)
        while view and not self._closed:
            try:
                written = os.write(self.master_fd, view)
                view = view[written:]
            except BlockingIOError:
                await _wait_for_fd(self.master_fd, writable=True)
            except OSError as exc:
                if exc.errno in {errno.EBADF, errno.EIO}:
                    return
                raise

    def resize(self, columns: int, rows: int) -> None:
        if not 1 <= columns <= 1_000 or not 1 <= rows <= 1_000:
            raise ValueError("terminal dimensions must be between 1 and 1000")
        if self._closed:
            return
        fcntl.ioctl(
            self.master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, columns, 0, 0),
        )

    async def wait(self) -> int:
        return int(await self.process.wait())

    async def close(self) -> None:
        assert self._close_lock is not None
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self.process.returncode is None:
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    except OSError:
                        try:
                            self.process.terminate()
                        except ProcessLookupError:
                            pass
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        try:
                            os.killpg(self.process.pid, signal.SIGKILL)
                        except OSError:
                            try:
                                self.process.kill()
                            except ProcessLookupError:
                                pass
                        await self.process.wait()
            finally:
                await self.runner._force_remove(self.container_name)
                try:
                    if self.egress_lease is not None:
                        await self.egress_lease.close()
                        self.egress_lease = None
                finally:
                    try:
                        os.close(self.master_fd)
                    except OSError:
                        pass


async def _wait_for_fd(file_descriptor: int, *, writable: bool) -> None:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()

    def mark_ready() -> None:
        if not ready.done():
            ready.set_result(None)

    register = loop.add_writer if writable else loop.add_reader
    remove = loop.remove_writer if writable else loop.remove_reader
    register(file_descriptor, mark_ready)
    try:
        await ready
    finally:
        remove(file_descriptor)


class ContainerSandboxRunner(SandboxRunner):
    """Execute argv directly in a rootless, resource-limited OCI container.

    Network-capable execution is accepted only through a certified egress
    controller which creates a fresh filtered namespace for each invocation.
    A named ordinary bridge is never considered an isolation boundary.
    """

    _forbidden_environment_fragments = (
        "API_KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "CREDENTIAL",
        "OPENAI",
        "ANTHROPIC",
        "GEMINI",
        "AZURE",
        "AWS_",
    )
    _human_terminal_only_environment = {"HISTFILE", "PROMPT_COMMAND", "PS0"}
    # Desktop applications do not reliably inherit a login shell's PATH. More
    # importantly, resolving an executable from an operator-controlled PATH is
    # the wrong trust boundary for the mandatory sandbox. Keep automatic
    # discovery to administrator- or package-manager-owned locations. A
    # non-standard installation remains possible through an explicit absolute
    # path in configuration.
    _trusted_runtime_paths = (
        Path("/usr/bin/podman"),
        Path("/usr/local/bin/podman"),
        Path("/opt/homebrew/bin/podman"),
        Path("/usr/bin/docker"),
        Path("/usr/local/bin/docker"),
        Path("/opt/homebrew/bin/docker"),
    )

    def __init__(
        self,
        *,
        profile: RunnerProfile | None = None,
        runtime: str | None = None,
        rootless_required: bool = True,
        egress_enforced_networks: set[str] | None = None,
        egress_controller: EgressController | None = None,
        allow_unpinned_images: bool = False,
        allowed_environment: set[str] | None = None,
        workspace_roots: list[Path] | None = None,
    ) -> None:
        if profile is not None and runtime is not None:
            raise ValueError("configure either profile or runtime, not both")
        if not rootless_required:
            raise ValueError("non-rootless container runners are not supported")
        configured_runtime = runtime or os.getenv("NEBULA_V3_CONTAINER_RUNTIME")
        resolved_runtime = (
            self._resolve_runtime(configured_runtime) if profile is None else None
        )
        self.profile = profile or (
            RunnerProfile.from_runtime(resolved_runtime) if resolved_runtime else None
        )
        self.runtime = str(self.profile.executable) if self.profile else None
        self.rootless_required = True
        # Compatibility-only validation for old SandboxRequest.network_name
        # callers. This set never authorizes run(); only egress_controller does.
        self.egress_enforced_networks = egress_enforced_networks or set()
        self.egress_controller = egress_controller or NoEgressController()
        self.allow_unpinned_images = allow_unpinned_images
        self.allowed_environment = allowed_environment or {
            "HISTFILE",
            "LANG",
            "LC_ALL",
            "TZ",
            "TERM",
            "NO_COLOR",
            "PS0",
            "PROMPT_COMMAND",
        }
        self.workspace_roots = (
            [root.expanduser().resolve(strict=True) for root in workspace_roots]
            if workspace_roots is not None
            else None
        )
        if self.workspace_roots is not None and any(
            not root.is_dir() for root in self.workspace_roots
        ):
            raise ValueError("configured workspace roots must be directories")

    @classmethod
    def _resolve_runtime(cls, configured: str | None) -> str | None:
        if configured:
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                raise ValueError(
                    "the configured container runtime must be an absolute path"
                )
            if candidate.name not in {"docker", "podman"}:
                raise ValueError("the configured runtime must be docker or podman")
            return str(candidate)

        for candidate in cls._trusted_runtime_paths:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    @classmethod
    def trusted_runtime_paths(cls) -> tuple[Path, ...]:
        """Return the fixed automatic-discovery allowlist.

        Callers must still run ``available()`` before trusting a candidate. The
        list deliberately ignores PATH and all container endpoint environment
        variables.
        """

        return cls._trusted_runtime_paths

    def terminal_cleanup_eligibility(self) -> tuple[bool, str]:
        """Validate the non-executing trust boundary for startup cleanup.

        Startup cleanup must not execute an arbitrary path recovered from the
        database.  Only the same fixed paths used by automatic discovery are
        eligible; the live runtime connection and isolation posture are still
        re-certified separately by :meth:`available` before any ``ps`` or
        ``rm`` operation.
        """

        if not self.runtime or self.profile is None:
            return False, "no explicit container runner profile is configured"
        executable = self.profile.executable
        if executable not in self.trusted_runtime_paths():
            return False, "runner executable is outside the fixed-path allowlist"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return False, "fixed-path runner executable is unavailable"
        return True, "fixed-path runner executable is eligible for re-verification"

    async def available(self) -> tuple[bool, str]:
        if not self.runtime or self.profile is None:
            return False, "neither podman nor docker is installed"
        try:
            return await self._validate_runtime_profile()
        except (
            OSError,
            asyncio.TimeoutError,
            json.JSONDecodeError,
            SandboxError,
        ) as exc:
            return False, f"container runtime health check failed: {exc}"

    async def _validate_runtime_profile(self) -> tuple[bool, str]:
        assert self.profile is not None
        endpoint_override = (
            os.environ.get("DOCKER_HOST")
            if self.profile.runtime_type == ContainerRuntimeType.DOCKER
            else os.environ.get("CONTAINER_HOST")
        )
        if endpoint_override and _is_remote_endpoint(endpoint_override):
            return False, "remote TCP/SSH container runtime endpoints are forbidden"
        if self.profile.seccomp_profile is not None:
            try:
                seccomp = self.profile.seccomp_profile.resolve(strict=True)
            except OSError as exc:
                return False, f"configured seccomp profile is unavailable: {exc}"
            if not seccomp.is_file():
                return False, "configured seccomp profile is not a regular file"

        if self.profile.runtime_type == ContainerRuntimeType.DOCKER:
            return await self._validate_docker_profile()
        return await self._validate_podman_profile()

    async def _validate_docker_profile(self) -> tuple[bool, str]:
        assert self.profile is not None
        context_name = self.profile.context or "default"
        context_output, context_error, return_code = await self._capture(
            "context", "inspect", context_name
        )
        if return_code != 0:
            return False, context_error or "Docker context is unavailable"
        context_document = _first_document(json.loads(context_output))
        endpoints = _mapping_get(context_document, "Endpoints", "endpoints")
        docker_endpoint = _mapping_get(endpoints, "docker", "Docker")
        endpoint = _mapping_get(docker_endpoint, "Host", "host")
        if not isinstance(endpoint, str) or not _is_local_unix_endpoint(endpoint):
            return False, "Docker context must use a local absolute Unix socket"

        info_output, info_error, return_code = await self._capture(
            "info", "--format", "{{json .}}"
        )
        if return_code != 0:
            return False, info_error or "Docker daemon is unavailable"
        info = json.loads(info_output)
        if str(_mapping_get(info, "OSType", "OsType")).lower() != "linux":
            return False, "Docker runner must execute Linux containers"
        security_options = _mapping_get(info, "SecurityOptions")
        if not isinstance(security_options, list):
            security_options = []
        if self.profile.platform == RunnerPlatform.LINUX:
            if not any(
                "rootless" in str(option).lower() for option in security_options
            ):
                return False, "Docker daemon is not operating in rootless mode"
            detail = "approved local rootless Docker runner is available"
        else:
            operating_system = str(_mapping_get(info, "OperatingSystem")).lower()
            if "docker desktop" not in operating_system:
                return False, "macOS Docker runner must be a local Docker Desktop VM"
            detail = "approved local Docker Desktop VM runner is available"
        return True, detail

    async def _validate_podman_profile(self) -> tuple[bool, str]:
        assert self.profile is not None
        if self.profile.platform == RunnerPlatform.MACOS:
            assert self.profile.machine_name is not None
            machine_output, machine_error, return_code = await self._capture(
                "machine", "inspect", self.profile.machine_name, "--format", "json"
            )
            if return_code != 0:
                return False, machine_error or "Podman Machine is unavailable"
            machine = _first_document(json.loads(machine_output))
            if str(_mapping_get(machine, "State", "state")).lower() != "running":
                return False, "Podman Machine is not running"
            if _mapping_get(machine, "Rootful", "rootful") is not False:
                return False, "Podman Machine rootless state could not be certified"

            connection_ok, connection_detail = await self._validate_podman_connection(
                machine=True
            )
            if not connection_ok:
                return False, connection_detail
        elif self.profile.context is not None:
            connection_ok, connection_detail = await self._validate_podman_connection(
                machine=False
            )
            if not connection_ok:
                return False, connection_detail

        info_output, info_error, return_code = await self._capture(
            "info", "--format", "json"
        )
        if return_code != 0:
            return False, info_error or "Podman service is unavailable"
        info = json.loads(info_output)
        host = _mapping_get(info, "host", "Host")
        security = _mapping_get(host, "security", "Security")
        if _mapping_get(security, "rootless", "Rootless") is not True:
            return False, "Podman service is not operating in rootless mode"
        host_os = str(_mapping_get(host, "os", "OS", "Os")).lower()
        if host_os and host_os != "linux":
            return False, "Podman runner must execute Linux containers"
        detail = (
            "approved rootless Podman Machine runner is available"
            if self.profile.platform == RunnerPlatform.MACOS
            else "approved local rootless Podman runner is available"
        )
        return True, detail

    async def _validate_podman_connection(self, *, machine: bool) -> tuple[bool, str]:
        assert self.profile is not None
        connections_output, connections_error, return_code = await self._capture(
            "system", "connection", "list", "--format", "json"
        )
        if return_code != 0:
            return False, connections_error or "Podman connection is unavailable"
        connections = json.loads(connections_output)
        if not isinstance(connections, list):
            return False, "Podman connection inspection returned invalid data"
        connection = next(
            (
                item
                for item in connections
                if isinstance(item, dict)
                and _mapping_get(item, "Name", "name") == self.profile.context
            ),
            None,
        )
        uri = _mapping_get(connection or {}, "URI", "Uri", "uri")
        valid = isinstance(uri, str) and (
            _is_local_machine_endpoint(uri) if machine else _is_local_unix_endpoint(uri)
        )
        if not valid:
            expected = (
                "terminate on localhost" if machine else "use a local Unix socket"
            )
            return False, f"Podman connection must {expected}"
        return True, "Podman connection is local"

    async def _capture(self, *arguments: str) -> tuple[str, str, int]:
        process = await asyncio.create_subprocess_exec(
            *self._runtime_argv(),
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_runtime_environment(),
        )
        stdout, stderr = await _communicate_limited(
            process, timeout_seconds=10, output_bytes=2_000_000
        )
        return (
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
            int(process.returncode or 0),
        )

    def _runtime_argv(self) -> list[str]:
        if self.profile is None:
            raise SandboxUnavailable("no container runtime is configured")
        argv = [str(self.profile.executable)]
        if self.profile.context:
            option = (
                "--context"
                if self.profile.runtime_type == ContainerRuntimeType.DOCKER
                else "--connection"
            )
            argv.extend([option, self.profile.context])
        return argv

    def _validate(self, request: SandboxRequest) -> Path | None:
        workspace: Path | None = None
        if request.workspace_access != SandboxWorkspaceAccess.NONE:
            workspace = request.workspace.expanduser().resolve(strict=True)
            if not workspace.is_dir():
                raise SandboxError("workspace must be an existing directory")
            if any(character in str(workspace) for character in {",", "\n", "\r"}):
                raise SandboxError(
                    "workspace path cannot be encoded as a safe OCI mount"
                )
            if self.workspace_roots is not None and not any(
                workspace == root or workspace.is_relative_to(root)
                for root in self.workspace_roots
            ):
                raise SandboxError(
                    "workspace is outside the configured workspace roots"
                )
        repository_digest = re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", request.image)
        local_image_id = re.fullmatch(r"sha256:[0-9a-f]{64}", request.image)
        if (
            not self.allow_unpinned_images
            and repository_digest is None
            and not (
                request.execution_kind == SandboxExecutionKind.HUMAN_TERMINAL
                and local_image_id is not None
            )
        ):
            raise SandboxError("sandbox images must be pinned by sha256 digest")
        if request.network_name is not None:
            if request.network_name not in self.egress_enforced_networks:
                raise SandboxUnavailable(
                    "scoped execution requires an operator-approved egress-enforced network"
                )
        for name in request.environment:
            upper = name.upper()
            if name not in self.allowed_environment or any(
                fragment in upper for fragment in self._forbidden_environment_fragments
            ):
                raise SandboxError(
                    f"environment variable {name!r} is not allowed in workers"
                )
            if (
                name in self._human_terminal_only_environment
                and request.execution_kind != SandboxExecutionKind.HUMAN_TERMINAL
            ):
                raise SandboxError(
                    f"environment variable {name!r} is reserved for human terminals"
                )
        return workspace

    def _argv(
        self,
        request: SandboxRequest,
        workspace: Path | None,
        *,
        container_name: str = "nebula-tool",
        network_mode: str | None = None,
        interactive: bool = False,
        tty: bool = False,
    ) -> list[str]:
        if not self.runtime or self.profile is None:
            raise SandboxUnavailable("no container runtime is configured")
        limits = request.limits
        argv = [
            *self._runtime_argv(),
            "run",
            "--rm",
            f"--name={container_name}",
            "--pull=never",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--cpus={limits.cpu_count}",
            f"--memory={limits.memory_mb}m",
            f"--pids-limit={limits.pids}",
            f"--user={request.container_user.value}",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
        ]
        if request.root_filesystem == SandboxRootFilesystem.READ_ONLY:
            argv.append("--read-only")
        if interactive:
            argv.append("--interactive")
        if tty:
            if not interactive:
                raise SandboxError(
                    "a terminal TTY requires interactive container input"
                )
            argv.append("--tty")
        if self.profile.seccomp_profile is not None:
            argv.append(f"--security-opt=seccomp={self.profile.seccomp_profile}")
        if workspace is None:
            argv.append("--workdir=/tmp")
        else:
            mount = f"--mount=type=bind,src={workspace},dst=/workspace"
            if request.workspace_access == SandboxWorkspaceAccess.READ:
                mount += ",readonly=true"
            argv.extend(
                [
                    mount,
                    "--workdir=/workspace",
                ]
            )
        if request.network == SandboxNetwork.NONE:
            argv.append("--network=none")
        elif request.network == SandboxNetwork.UNRESTRICTED:
            argv.append("--network=bridge")
        else:
            selected_network = network_mode or request.network_name
            if not selected_network:
                raise SandboxUnavailable(
                    "scoped execution requires an acquired egress namespace"
                )
            argv.append(f"--network={selected_network}")
            for host, address in sorted(request.pinned_hosts.items()):
                argv.append(f"--add-host={host}:{address}")
        for name, value in sorted(request.environment.items()):
            argv.extend(["--env", f"{name}={value}"])
        argv.extend([request.image, *request.command])
        return argv

    async def run(self, request: SandboxRequest) -> SandboxResult:
        return await self.run_stream(request)

    async def open_terminal(
        self,
        request: SandboxRequest,
        *,
        container_name: str,
        columns: int,
        rows: int,
    ) -> SandboxTerminalProcess:
        """Launch one fixed-command container with an interactive PTY."""

        healthy, detail = await self.available()
        if not healthy:
            raise SandboxUnavailable(detail)
        if (
            request.workspace_access != SandboxWorkspaceAccess.NONE
            and self.workspace_roots is None
        ):
            raise SandboxUnavailable(
                "workspace terminal execution requires explicitly configured workspace roots"
            )
        workspace = self._validate(request)
        if not re.fullmatch(
            r"nebula-terminal-[a-z0-9][a-z0-9_.-]{0,53}", container_name
        ):
            raise SandboxError(
                "terminal container name is outside the Nebula namespace"
            )
        if not 1 <= columns <= 1_000 or not 1 <= rows <= 1_000:
            raise SandboxError("terminal dimensions must be between 1 and 1000")

        lease: EgressLease | None = None
        if request.network == SandboxNetwork.SCOPED:
            if not request.egress_rules:
                raise SandboxUnavailable(
                    "network terminal execution requires explicit broker-approved egress rules"
                )
            if not self.egress_controller.certified:
                raise SandboxUnavailable(
                    "network terminal execution requires a certified per-invocation egress helper"
                )
            lease = await self.egress_controller.acquire(
                runtime_argv=self._runtime_argv(),
                runtime_environment=_runtime_environment(),
                request=request,
                container_name=container_name,
                seccomp_profile=self.profile.seccomp_profile if self.profile else None,
            )
        master_fd: int | None = None
        slave_fd: int | None = None
        try:
            argv = self._argv(
                request,
                workspace,
                container_name=container_name,
                network_mode=lease.network_mode if lease else None,
                interactive=True,
                tty=True,
            )
            master_fd, slave_fd = pty.openpty()
            fcntl.ioctl(
                slave_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, columns, 0, 0),
            )
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=_runtime_environment(),
                start_new_session=True,
            )
        except (OSError, SandboxError) as exc:
            if master_fd is not None:
                os.close(master_fd)
            if lease is not None:
                await lease.close()
            if isinstance(exc, SandboxError):
                raise
            raise SandboxUnavailable(
                f"could not start container terminal runtime: {exc}"
            ) from exc
        finally:
            if slave_fd is not None:
                os.close(slave_fd)
        assert master_fd is not None
        return SandboxTerminalProcess(
            process=process,
            master_fd=master_fd,
            container_name=container_name,
            runner=self,
            egress_lease=lease,
        )

    async def cleanup_terminal_containers(self) -> None:
        """Best-effort removal of terminals orphaned by a prior Core process."""

        eligible, eligibility_detail = self.terminal_cleanup_eligibility()
        if not eligible:
            LOGGER.warning("Skipped orphan terminal cleanup: %s", eligibility_detail)
            return
        healthy, health_detail = await self.available()
        if not healthy:
            LOGGER.warning(
                "Skipped orphan terminal cleanup after runner re-verification: %s",
                health_detail,
            )
            return
        try:
            stdout, _stderr, return_code = await self._capture(
                "ps", "--all", "--format", "{{.Names}}"
            )
        except (OSError, asyncio.TimeoutError, SandboxError):
            return
        if return_code != 0:
            return
        names = {
            line.strip()
            for line in stdout.splitlines()
            if re.fullmatch(r"nebula-terminal-[a-z0-9][a-z0-9_.-]{0,53}", line.strip())
        }
        for name in sorted(names):
            # A context or daemon can change after enumeration. Re-certify the
            # local/rootless/security boundary immediately before destructive
            # removal as well as before ``ps``.
            healthy, health_detail = await self.available()
            if not healthy:
                LOGGER.warning(
                    "Stopped orphan terminal cleanup after runner "
                    "re-verification failed: %s",
                    health_detail,
                )
                return
            await self._force_remove(name)

    async def run_stream(
        self,
        request: SandboxRequest,
        *,
        input_bytes: bytes = b"",
        on_chunk: Callable[[str, bytes], Awaitable[None]] | None = None,
        container_name: str | None = None,
    ) -> SandboxResult:
        healthy, detail = await self.available()
        if not healthy:
            raise SandboxUnavailable(detail)
        if (
            request.workspace_access != SandboxWorkspaceAccess.NONE
            and self.workspace_roots is None
        ):
            raise SandboxUnavailable(
                "workspace tool execution requires explicitly configured workspace roots"
            )
        workspace = self._validate(request)
        selected_name = container_name or f"nebula-{uuid4().hex}"
        if not re.fullmatch(r"nebula-[a-z0-9][a-z0-9_.-]{0,62}", selected_name):
            raise SandboxError("container name is outside the Nebula namespace")
        lease: EgressLease | None = None
        if request.network == SandboxNetwork.SCOPED:
            if not request.egress_rules:
                raise SandboxUnavailable(
                    "network tool execution requires explicit broker-approved egress rules"
                )
            if not self.egress_controller.certified:
                raise SandboxUnavailable(
                    "network tool execution requires a certified per-invocation egress helper"
                )
            lease = await self.egress_controller.acquire(
                runtime_argv=self._runtime_argv(),
                runtime_environment=_runtime_environment(),
                request=request,
                container_name=selected_name,
                seccomp_profile=self.profile.seccomp_profile if self.profile else None,
            )
        argv = self._argv(
            request,
            workspace,
            container_name=selected_name,
            network_mode=lease.network_mode if lease else None,
            interactive=bool(input_bytes),
        )
        started_at = utc_now()
        started = monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=(
                    asyncio.subprocess.PIPE
                    if input_bytes
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_runtime_environment(),
            )
        except OSError as exc:
            if lease is not None:
                await lease.close()
            raise SandboxUnavailable(
                f"could not start container runtime: {exc}"
            ) from exc

        assert process.stdout is not None
        assert process.stderr is not None
        if input_bytes:
            assert process.stdin is not None
            try:
                process.stdin.write(input_bytes)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
        stdout_task = asyncio.create_task(
            _read_limited_stream(
                process.stdout,
                request.limits.output_bytes,
                stream="stdout",
                on_chunk=on_chunk,
            )
        )
        stderr_task = asyncio.create_task(
            _read_limited_stream(
                process.stderr,
                request.limits.output_bytes,
                stream="stderr",
                on_chunk=on_chunk,
            )
        )
        timed_out = False
        try:
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=request.limits.timeout_seconds
                )
            except asyncio.TimeoutError:
                timed_out = True
                process.kill()
                await process.wait()
                await self._force_remove(selected_name)
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                await self._force_remove(selected_name)
                stdout_task.cancel()
                stderr_task.cancel()
                raise
            stdout, stdout_truncated = await stdout_task
            stderr, stderr_truncated = await stderr_task
            return SandboxResult(
                command=request.command,
                image=request.image,
                runtime=Path(self.runtime or "unknown").name,
                started_at=started_at,
                completed_at=utc_now(),
                duration_seconds=monotonic() - started,
                exit_code=None if timed_out else process.returncode,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                timed_out=timed_out,
                output_truncated=stdout_truncated or stderr_truncated,
            )
        finally:
            if lease is not None:
                await lease.close()

    async def _force_remove(self, container_name: str) -> None:
        if not self.runtime or self.profile is None:
            return
        try:
            cleanup = await asyncio.create_subprocess_exec(
                *self._runtime_argv(),
                "rm",
                "--force",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=_runtime_environment(),
            )
            await asyncio.wait_for(cleanup.wait(), timeout=10)
        except (OSError, asyncio.TimeoutError):
            return


@dataclass(frozen=True)
class PreparedContainerImage:
    source_reference: str
    base_resolved_reference: str
    base_digest: str
    resolved_reference: str
    digest: str
    platform: Literal["linux/amd64", "linux/arm64"]
    configured_user: str
    installed_packages: tuple[str, ...]
    refreshed: bool
    detail: str


@dataclass(frozen=True)
class _VerifiedBaseImage:
    resolved_reference: str
    digest: str


@dataclass(frozen=True)
class _VerifiedDerivedImage:
    image_id: str
    configured_user: str


class ContainerImagePreparer:
    """Verify official Kali and prepare a pinned local headless-tool image."""

    _derived_repository = "localhost/nebula-kali-headless"
    _recipe_version = "v2"
    _installed_packages = ("kali-linux-headless", "iputils-ping")
    _base_label = "org.nebula.human-terminal.base"
    _profile_label = "org.nebula.human-terminal.profile"
    _recipe_label = "org.nebula.human-terminal.recipe"

    def __init__(
        self,
        *,
        runner: ContainerSandboxRunner,
        platform: Literal["linux/amd64", "linux/arm64"],
        source_reference: str,
        expected_repository: str,
        pull_timeout_seconds: int = 900,
        build_timeout_seconds: int = 3600,
    ) -> None:
        if platform not in {"linux/amd64", "linux/arm64"}:
            raise ValueError(
                "container image platform must be linux/amd64 or linux/arm64"
            )
        if pull_timeout_seconds < 1 or pull_timeout_seconds > 3600:
            raise ValueError("pull timeout must be between 1 and 3600 seconds")
        if build_timeout_seconds < 1 or build_timeout_seconds > 7200:
            raise ValueError("build timeout must be between 1 and 7200 seconds")
        if runner.profile is None:
            raise ValueError("container image preparation requires an explicit runner")
        tagged_source = re.fullmatch(
            r"[a-z0-9.-]+(?::[0-9]+)?"
            r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+:[A-Za-z0-9_.-]+",
            source_reference,
        )
        pinned_source = re.fullmatch(
            r"[a-z0-9.-]+(?::[0-9]+)?"
            r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
            r"@sha256:[0-9a-f]{64}",
            source_reference,
        )
        if tagged_source is None and pinned_source is None:
            raise ValueError(
                "container image source must be a fully qualified tag or digest"
            )
        if "@" in expected_repository or ":" in expected_repository.rsplit("/", 1)[-1]:
            raise ValueError("expected image repository cannot contain a tag or digest")
        if _normalized_repository(expected_repository) != expected_repository:
            raise ValueError("expected image repository must be fully qualified")
        source_repository = (
            source_reference.rsplit("@", 1)[0]
            if pinned_source is not None
            else source_reference.rsplit(":", 1)[0]
        )
        if _normalized_repository(source_repository) != expected_repository:
            raise ValueError(
                "container image source must use the expected official repository"
            )
        self.runner = runner
        self.platform = platform
        self.source_reference = source_reference
        self.expected_repository = expected_repository
        self.expected_source_digest = (
            source_reference.rsplit("@", 1)[1] if pinned_source is not None else None
        )
        self.pull_timeout_seconds = pull_timeout_seconds
        self.build_timeout_seconds = build_timeout_seconds

    async def prepare(self) -> PreparedContainerImage:
        available, detail = await self.runner.available()
        if not available:
            raise SandboxUnavailable(detail)

        cached_base, cached_base_detail = await self._try_verified_base()
        if cached_base is not None:
            derived_tag = self._derived_tag(cached_base.digest)
            cached_derived, _ = await self._try_verified_derived(
                derived_tag, cached_base.resolved_reference
            )
            if cached_derived is not None:
                return self._prepared_result(
                    cached_base,
                    cached_derived,
                    refreshed=False,
                    detail=(
                        "using the fully verified cached human-workstation image; "
                        "no registry request or image build was required"
                    ),
                )
            return await self._build_and_verify(
                cached_base,
                derived_tag,
                refreshed=False,
                prefix="using a verified cached official base image; ",
            )

        pull_detail: str | None = None
        try:
            stdout, stderr, return_code = await self._runtime_command(
                "pull",
                f"--platform={self.platform}",
                self.source_reference,
                timeout_seconds=self.pull_timeout_seconds,
            )
            if return_code != 0:
                pull_detail = (stderr.strip() or stdout.strip() or str(return_code))[
                    :1000
                ]
        except (OSError, SandboxError) as exc:
            pull_detail = str(exc)[:1000]
        if pull_detail is not None:
            raise SandboxUnavailable(
                "human-workstation image pull failed "
                f"({pull_detail}); no verified cached base image is available "
                f"({cached_base_detail})"
            )

        base = await self._verified_base(required=True)
        assert base is not None
        derived_tag = self._derived_tag(base.digest)
        cached_derived, _ = await self._try_verified_derived(
            derived_tag, base.resolved_reference
        )
        if cached_derived is not None:
            return self._prepared_result(
                base,
                cached_derived,
                refreshed=True,
                detail=(
                    "pulled and verified the configured official base image; "
                    "using the verified cached human-workstation image"
                ),
            )
        return await self._build_and_verify(
            base,
            derived_tag,
            refreshed=True,
            prefix="pulled and verified the configured official base image; ",
        )

    async def _try_verified_base(
        self,
    ) -> tuple[_VerifiedBaseImage | None, str]:
        try:
            base = await self._verified_base(required=False)
        except SandboxUnavailable as exc:
            return None, str(exc)[:1000]
        if base is None:
            return None, "the configured base image is not present locally"
        return base, "verified cached base image"

    async def _verified_base(self, *, required: bool) -> _VerifiedBaseImage | None:
        document, detail = await self._inspect_image(self.source_reference)
        if document is None:
            if required:
                raise SandboxUnavailable(
                    f"configured base image could not be inspected: {detail}"
                )
            return None
        repo_digests = _mapping_get(document, "RepoDigests", "repoDigests")
        matching: list[str] = []
        if isinstance(repo_digests, list):
            for value in repo_digests:
                if not isinstance(value, str) or "@sha256:" not in value:
                    continue
                repository, digest = value.rsplit("@", 1)
                if _normalized_repository(repository) == self.expected_repository:
                    matching.append(digest)
        if not matching:
            raise SandboxUnavailable(
                "runtime did not prove that the base image belongs to the official "
                "repository"
            )
        digests = sorted(set(matching))
        if any(not re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in digests):
            raise SandboxUnavailable("runtime returned an invalid base image digest")
        if self.expected_source_digest is not None:
            if self.expected_source_digest not in digests:
                raise SandboxUnavailable(
                    "runtime did not prove the release-pinned base image digest"
                )
            digest = self.expected_source_digest
        else:
            digest = digests[0]
        self._verify_platform(document, label="base image")
        return _VerifiedBaseImage(
            resolved_reference=f"{self.expected_repository}@{digest}",
            digest=digest,
        )

    async def _try_verified_derived(
        self, derived_tag: str, base_resolved_reference: str
    ) -> tuple[_VerifiedDerivedImage | None, str]:
        try:
            derived = await self._verified_derived(
                derived_tag, base_resolved_reference, required=False
            )
        except SandboxUnavailable as exc:
            return None, str(exc)[:1000]
        if derived is None:
            return None, "the prepared image is not present locally"
        return derived, "verified cached prepared image"

    async def _verified_derived(
        self,
        derived_tag: str,
        base_resolved_reference: str,
        *,
        required: bool,
    ) -> _VerifiedDerivedImage | None:
        document, detail = await self._inspect_image(derived_tag)
        if document is None:
            if required:
                raise SandboxUnavailable(
                    f"prepared human-workstation image could not be inspected: {detail}"
                )
            return None
        image_id = str(_mapping_get(document, "Id", "ID", "id")).lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise SandboxUnavailable(
                "runtime returned an invalid human-workstation image ID"
            )
        self._verify_platform(document, label="human-workstation image")
        config = _mapping_get(document, "Config", "config")
        labels = _mapping_get(config, "Labels", "labels")
        if not isinstance(labels, dict):
            labels = _mapping_get(document, "Labels", "labels")
        if not isinstance(labels, dict) or (
            labels.get(self._base_label) != base_resolved_reference
            or labels.get(self._profile_label) != "kali-linux-headless"
            or labels.get(self._recipe_label) != self._recipe_version
        ):
            raise SandboxUnavailable(
                "runtime did not prove the human-workstation image build recipe"
            )
        user = _mapping_get(config, "User", "user")
        return _VerifiedDerivedImage(
            image_id=image_id,
            configured_user=user if isinstance(user, str) else "",
        )

    async def _inspect_image(self, reference: str) -> tuple[dict[str, Any] | None, str]:
        stdout, stderr, return_code = await self._runtime_command(
            "image",
            "inspect",
            reference,
            "--format",
            "{{json .}}",
            timeout_seconds=30,
        )
        if return_code != 0:
            return None, (stderr.strip() or stdout.strip() or str(return_code))[:1000]
        try:
            return _first_document(json.loads(stdout)), ""
        except json.JSONDecodeError as exc:
            raise SandboxUnavailable(
                f"image inspection returned invalid JSON for {reference}"
            ) from exc

    def _verify_platform(self, document: dict[str, Any], *, label: str) -> None:
        os_name = str(_mapping_get(document, "Os", "OS", "os")).lower()
        architecture = str(
            _mapping_get(document, "Architecture", "architecture", "Arch")
        ).lower()
        observed = f"{os_name}/{architecture}"
        if observed != self.platform:
            raise SandboxUnavailable(
                f"{label} platform mismatch: expected {self.platform}, observed {observed}"
            )

    async def _build_and_verify(
        self,
        base: _VerifiedBaseImage,
        derived_tag: str,
        *,
        refreshed: bool,
        prefix: str,
    ) -> PreparedContainerImage:
        build_detail: str | None = None
        try:
            stdout, stderr, return_code = await self._build_derived_image(
                base.resolved_reference, derived_tag
            )
            if return_code != 0:
                build_detail = (stderr.strip() or stdout.strip() or str(return_code))[
                    -1000:
                ]
        except (OSError, SandboxError) as exc:
            build_detail = str(exc)[-1000:]
        try:
            derived = await self._verified_derived(
                derived_tag, base.resolved_reference, required=True
            )
        except SandboxUnavailable as exc:
            if build_detail is not None:
                raise SandboxUnavailable(
                    "human-workstation image build failed "
                    f"({build_detail}); no verified prepared image is available "
                    f"({exc})"
                ) from exc
            raise
        assert derived is not None
        detail = (
            prefix + "prepared and verified the human-workstation image"
            if build_detail is None
            else prefix
            + "using the verified human-workstation image after rebuild failed: "
            + build_detail
        )
        return self._prepared_result(base, derived, refreshed=refreshed, detail=detail)

    def _prepared_result(
        self,
        base: _VerifiedBaseImage,
        derived: _VerifiedDerivedImage,
        *,
        refreshed: bool,
        detail: str,
    ) -> PreparedContainerImage:
        return PreparedContainerImage(
            source_reference=self.source_reference,
            base_resolved_reference=base.resolved_reference,
            base_digest=base.digest,
            resolved_reference=derived.image_id,
            digest=derived.image_id,
            platform=self.platform,
            configured_user=derived.configured_user,
            installed_packages=self._installed_packages,
            refreshed=refreshed,
            detail=detail,
        )

    def _derived_tag(self, digest: str) -> str:
        return (
            f"{self._derived_repository}:"
            f"{self._recipe_version}-{digest.removeprefix('sha256:')}"
        )

    async def _build_derived_image(
        self,
        base_resolved_reference: str,
        derived_tag: str,
    ) -> tuple[str, str, int]:
        dockerfile = f"""FROM {base_resolved_reference}
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \\
 && apt-get install -y {" ".join(self._installed_packages)} \\
 && if getcap /usr/lib/nmap/nmap | grep -q .; then setcap -r /usr/lib/nmap/nmap; fi \\
 && test -z "$(getcap /usr/lib/nmap/nmap)" \\
 && printf '%s\\n' 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99-nebula-terminal \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*
ENV NMAP_UNPRIVILEGED=1
LABEL {self._base_label}={base_resolved_reference}
LABEL {self._profile_label}=kali-linux-headless
LABEL {self._recipe_label}={self._recipe_version}
CMD ["/bin/bash"]
"""
        with tempfile.TemporaryDirectory(prefix="nebula-kali-") as directory:
            context = Path(directory)
            (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            return await self._runtime_command(
                "build",
                f"--platform={self.platform}",
                "--pull=false",
                "--quiet",
                f"--tag={derived_tag}",
                str(context),
                timeout_seconds=self.build_timeout_seconds,
            )

    async def _runtime_command(
        self, *arguments: str, timeout_seconds: int
    ) -> tuple[str, str, int]:
        process = await asyncio.create_subprocess_exec(
            *self.runner._runtime_argv(),
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_runtime_environment(),
        )
        try:
            stdout, stderr = await _communicate_limited(
                process, timeout_seconds=timeout_seconds, output_bytes=5_000_000
            )
        except asyncio.TimeoutError as exc:
            raise SandboxUnavailable("container runtime operation timed out") from exc
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            int(process.returncode or 0),
        )


def _normalized_repository(value: str) -> str:
    repository = value
    first = repository.split("/", 1)[0]
    if "." not in first and ":" not in first and first != "localhost":
        repository = f"docker.io/{repository}"
    if repository.startswith("index.docker.io/"):
        repository = "docker.io/" + repository.removeprefix("index.docker.io/")
    if repository.startswith("docker.io/library/") and value.startswith("library/"):
        return repository
    return repository


class ContainerToolPackRuntimeAdapter:
    """Installation-only OCI operations backed by a validated runner profile.

    Mission execution still uses ``--pull=never``. This adapter is handed only
    to the explicit tool-pack installer, which is the sole component allowed to
    pull images. Smoke tests consume a command already rendered from the signed
    manifest's typed bindings; the adapter never guesses how inputs map to argv.
    """

    def __init__(
        self,
        *,
        runner: ContainerSandboxRunner,
        platform: Literal["linux/amd64", "linux/arm64"],
        pull_timeout_seconds: int = 900,
    ) -> None:
        if platform not in {"linux/amd64", "linux/arm64"}:
            raise ValueError(
                "tool-pack runtime platform must be linux/amd64 or linux/arm64"
            )
        if pull_timeout_seconds < 1 or pull_timeout_seconds > 3600:
            raise ValueError("pull timeout must be between 1 and 3600 seconds")
        if runner.profile is None:
            raise ValueError(
                "tool-pack runtime adapter requires an explicit runner profile"
            )
        self.runner = runner
        self.platform = platform
        self.pull_timeout_seconds = pull_timeout_seconds

    async def pull(self, image: str) -> None:
        self._validate_image(image)
        await self._require_runner()
        stdout, stderr, return_code = await self._runtime_command(
            "pull",
            f"--platform={self.platform}",
            image,
            timeout_seconds=self.pull_timeout_seconds,
        )
        if return_code != 0:
            detail = stderr.strip() or stdout.strip()
            raise SandboxError(f"tool image pull failed: {detail or return_code}")

    async def inspect(self, image: str) -> Any:
        # Imported lazily to avoid sandbox -> toolpacks -> tools -> sandbox.
        from .toolpacks import RuntimeImageInfo

        expected_digest = self._validate_image(image)
        await self._require_runner()
        stdout, stderr, return_code = await self._runtime_command(
            "image",
            "inspect",
            image,
            "--format",
            "{{json .}}",
            timeout_seconds=30,
        )
        if return_code != 0:
            raise SandboxError(
                f"tool image inspection failed: {stderr.strip() or return_code}"
            )
        try:
            document = _first_document(json.loads(stdout))
        except json.JSONDecodeError as exc:
            raise SandboxError("tool image inspection returned invalid JSON") from exc
        observed_digests: set[str] = set()
        digest = _mapping_get(document, "Digest", "digest")
        if isinstance(digest, str):
            observed_digests.add(digest)
        repo_digests = _mapping_get(document, "RepoDigests", "repoDigests")
        if isinstance(repo_digests, list):
            observed_digests.update(
                value.rsplit("@", 1)[1]
                for value in repo_digests
                if isinstance(value, str) and "@sha256:" in value
            )
        if expected_digest not in observed_digests:
            raise SandboxError("runtime did not prove the requested image digest")
        os_name = str(_mapping_get(document, "Os", "OS", "os")).lower()
        architecture = str(
            _mapping_get(document, "Architecture", "architecture", "Arch")
        ).lower()
        observed_platform = f"{os_name}/{architecture}"
        if observed_platform != self.platform:
            raise SandboxError(
                f"tool image platform mismatch: expected {self.platform}, "
                f"observed {observed_platform}"
            )
        config = _mapping_get(document, "Config", "config")
        user = _mapping_get(config, "User", "user")
        if not isinstance(user, str):
            raise SandboxError("tool image did not declare a container user")
        return RuntimeImageInfo(
            image=image,
            digest=expected_digest,
            platform=self.platform,
            user=user,
        )

    async def smoke_test(
        self,
        *,
        image: str,
        command: list[str],
        timeout_seconds: int,
    ) -> Any:
        from .toolpacks import RuntimeSmokeResult

        self._validate_image(image)
        if (
            not command
            or not Path(command[0]).is_absolute()
            or any(not isinstance(value, str) or "\x00" in value for value in command)
        ):
            raise SandboxError("smoke-test command must be safe absolute argv")
        result = await self.runner.run(
            SandboxRequest(
                image=image,
                command=command,
                workspace=Path("/"),
                workspace_access=SandboxWorkspaceAccess.NONE,
                network=SandboxNetwork.NONE,
                execution_kind=SandboxExecutionKind.LOCAL_TOOL,
                limits=SandboxLimits(timeout_seconds=timeout_seconds),
            )
        )
        return RuntimeSmokeResult(
            exit_code=result.exit_code if result.exit_code is not None else 124,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    async def _require_runner(self) -> None:
        available, detail = await self.runner.available()
        if not available:
            raise SandboxUnavailable(detail)

    async def _runtime_command(
        self,
        *arguments: str,
        timeout_seconds: int,
    ) -> tuple[str, str, int]:
        process = await asyncio.create_subprocess_exec(
            *self.runner._runtime_argv(),
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_runtime_environment(),
        )
        try:
            stdout, stderr = await _communicate_limited(
                process, timeout_seconds=timeout_seconds, output_bytes=5_000_000
            )
        except asyncio.TimeoutError as exc:
            raise SandboxUnavailable("container runtime operation timed out") from exc
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            int(process.returncode or 0),
        )

    @staticmethod
    def _validate_image(image: str) -> str:
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
            raise SandboxError("tool images must be pinned by sha256 digest")
        return image.rsplit("@", 1)[1]


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    retained = 0
    truncated = False
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            break
        remaining = max(0, limit - retained)
        if remaining:
            chunks.append(chunk[:remaining])
            retained += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated = True
    return b"".join(chunks), truncated


async def _read_limited_stream(
    reader: asyncio.StreamReader,
    limit: int,
    *,
    stream: str,
    on_chunk: Callable[[str, bytes], Awaitable[None]] | None,
) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    retained = 0
    truncated = False
    while True:
        chunk = await reader.read(32_768)
        if not chunk:
            break
        remaining = max(0, limit - retained)
        captured = chunk[:remaining]
        if captured:
            chunks.append(captured)
            retained += len(captured)
            if on_chunk is not None:
                await on_chunk(stream, captured)
        if len(chunk) > remaining:
            truncated = True
    return b"".join(chunks), truncated


async def _communicate_limited(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: int,
    output_bytes: int,
) -> tuple[bytes, bytes]:
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(_read_limited(process.stdout, output_bytes))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, output_bytes))
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        stdout_task.cancel()
        stderr_task.cancel()
        raise
    stdout, stdout_truncated = await stdout_task
    stderr, stderr_truncated = await stderr_task
    if stdout_truncated or stderr_truncated:
        raise SandboxError("container runtime control output exceeded its limit")
    return stdout, stderr


async def _discard_stream(stream: asyncio.StreamReader) -> None:
    while await stream.read(65_536):
        pass


def _bracket_ip(address: str) -> str:
    parsed = ipaddress.ip_address(address)
    return f"[{parsed}]" if parsed.version == 6 else str(parsed)


def _mapping_get(value: Any, *keys: str) -> Any:
    if not isinstance(value, dict):
        return None
    for key in keys:
        if key in value:
            return value[key]
    return None


def _first_document(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        if not value or not isinstance(value[0], dict):
            raise json.JSONDecodeError("expected a JSON object", "", 0)
        return value[0]
    if not isinstance(value, dict):
        raise json.JSONDecodeError("expected a JSON object", "", 0)
    return value


def _is_remote_endpoint(endpoint: str) -> bool:
    scheme = urlsplit(endpoint).scheme.lower()
    return scheme in {"tcp", "http", "https", "ssh"}


def _is_local_unix_endpoint(endpoint: str) -> bool:
    parsed = urlsplit(endpoint)
    return parsed.scheme.lower() == "unix" and Path(parsed.path).is_absolute()


def _is_local_machine_endpoint(endpoint: str) -> bool:
    parsed = urlsplit(endpoint)
    if parsed.scheme.lower() == "unix":
        return Path(parsed.path).is_absolute()
    return parsed.scheme.lower() == "ssh" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


def _runtime_environment() -> dict[str, str]:
    # Endpoint variables are intentionally absent. Runtime connections are
    # selected only by an inspected RunnerProfile, preventing a desktop launch
    # environment from silently redirecting execution to a remote daemon.
    retained = {
        "PATH",
        "HOME",
        "XDG_RUNTIME_DIR",
    }
    return {
        name: value for name in retained if (value := os.environ.get(name)) is not None
    }


__all__ = [
    "AnalysisOnlyRunner",
    "ContainerEgressController",
    "ContainerImagePreparer",
    "ContainerRuntimeType",
    "ContainerSandboxRunner",
    "ContainerToolPackRuntimeAdapter",
    "EgressController",
    "EgressLease",
    "EgressProtocol",
    "EgressRule",
    "NoEgressController",
    "PreparedContainerImage",
    "RunnerIsolationMode",
    "RunnerPlatform",
    "RunnerProfile",
    "SandboxError",
    "SandboxContainerUser",
    "SandboxExecutionKind",
    "SandboxLimits",
    "SandboxNetwork",
    "SandboxRequest",
    "SandboxResult",
    "SandboxRootFilesystem",
    "SandboxRunner",
    "SandboxTerminalProcess",
    "SandboxUnavailable",
    "SandboxWorkspaceAccess",
]
