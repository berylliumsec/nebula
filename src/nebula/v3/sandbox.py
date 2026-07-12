"""Disposable container execution with a fail-closed analysis-only fallback."""

from __future__ import annotations

import asyncio
import ipaddress
import os
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from pathlib import Path
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import utc_now


class SandboxError(RuntimeError):
    """Base class for normalized sandbox failures."""


class SandboxUnavailable(SandboxError):
    """No approved isolation boundary is available; host execution is forbidden."""


class SandboxNetwork(str, Enum):
    NONE = "none"
    SCOPED = "scoped"


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
        if self.network == SandboxNetwork.SCOPED and not self.network_name:
            raise ValueError("scoped network execution requires a network_name")
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


class SandboxRunner(ABC):
    @abstractmethod
    async def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    @abstractmethod
    async def run(self, request: SandboxRequest) -> SandboxResult:
        raise NotImplementedError


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


class ContainerSandboxRunner(SandboxRunner):
    """Execute argv directly in a rootless, resource-limited OCI container.

    Network-capable execution is accepted only when the operator has declared a
    pre-created network as egress-enforced.  Nebula does not pretend that a
    regular bridge network limits destinations.
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
        runtime: str | None = None,
        rootless_required: bool = True,
        egress_enforced_networks: set[str] | None = None,
        allow_unpinned_images: bool = False,
        allowed_environment: set[str] | None = None,
    ) -> None:
        configured_runtime = runtime or os.getenv("NEBULA_V3_CONTAINER_RUNTIME")
        self.runtime = self._resolve_runtime(configured_runtime)
        self.rootless_required = rootless_required
        self.egress_enforced_networks = egress_enforced_networks or set()
        self.allow_unpinned_images = allow_unpinned_images
        self.allowed_environment = allowed_environment or {
            "LANG",
            "LC_ALL",
            "TZ",
            "TERM",
            "NO_COLOR",
        }

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

    async def available(self) -> tuple[bool, str]:
        if not self.runtime:
            return False, "neither podman nor docker is installed"
        executable = Path(self.runtime).name
        try:
            if executable == "podman":
                command = [
                    self.runtime,
                    "info",
                    "--format",
                    "{{.Host.Security.Rootless}}",
                ]
            else:
                command = [
                    self.runtime,
                    "info",
                    "--format",
                    "{{json .SecurityOptions}}",
                ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, asyncio.TimeoutError) as exc:
            return False, f"container runtime health check failed: {exc}"
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            return False, detail or "container runtime is unavailable"
        description = stdout.decode(errors="replace").strip().lower()
        if self.rootless_required:
            is_rootless = (
                description == "true"
                if executable == "podman"
                else "rootless" in description
            )
            if not is_rootless:
                return False, "container runtime is not operating in rootless mode"
        return True, f"approved rootless {executable} runner is available"

    def _validate(self, request: SandboxRequest) -> Path | None:
        workspace: Path | None = None
        if request.workspace_access != SandboxWorkspaceAccess.NONE:
            workspace = request.workspace.expanduser().resolve(strict=True)
            if not workspace.is_dir():
                raise SandboxError("workspace must be an existing directory")
        if not self.allow_unpinned_images and "@sha256:" not in request.image:
            raise SandboxError("sandbox images must be pinned by sha256 digest")
        if request.network == SandboxNetwork.SCOPED:
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
        return workspace

    def _argv(
        self,
        request: SandboxRequest,
        workspace: Path | None,
        *,
        container_name: str = "nebula-tool",
    ) -> list[str]:
        if not self.runtime:
            raise SandboxUnavailable("no container runtime is configured")
        limits = request.limits
        argv = [
            self.runtime,
            "run",
            "--rm",
            f"--name={container_name}",
            "--pull=never",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--cpus={limits.cpu_count}",
            f"--memory={limits.memory_mb}m",
            f"--pids-limit={limits.pids}",
            "--user=65532:65532",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
        ]
        if workspace is None:
            argv.append("--workdir=/tmp")
        else:
            mode = (
                "ro"
                if request.workspace_access == SandboxWorkspaceAccess.READ
                else "rw"
            )
            argv.extend(
                [
                    f"--mount=type=bind,src={workspace},dst=/workspace,{mode}",
                    "--workdir=/workspace",
                ]
            )
        if request.network == SandboxNetwork.NONE:
            argv.append("--network=none")
        else:
            argv.append(f"--network={request.network_name}")
            for host, address in sorted(request.pinned_hosts.items()):
                argv.append(f"--add-host={host}:{address}")
        for name, value in sorted(request.environment.items()):
            argv.extend(["--env", f"{name}={value}"])
        argv.extend([request.image, *request.command])
        return argv

    async def run(self, request: SandboxRequest) -> SandboxResult:
        healthy, detail = await self.available()
        if not healthy:
            raise SandboxUnavailable(detail)
        workspace = self._validate(request)
        container_name = f"nebula-{uuid4().hex}"
        argv = self._argv(request, workspace, container_name=container_name)
        started_at = utc_now()
        started = monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_runtime_environment(),
            )
        except OSError as exc:
            raise SandboxUnavailable(
                f"could not start container runtime: {exc}"
            ) from exc

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            _read_limited(process.stdout, request.limits.output_bytes)
        )
        stderr_task = asyncio.create_task(
            _read_limited(process.stderr, request.limits.output_bytes)
        )
        timed_out = False
        try:
            await asyncio.wait_for(
                process.wait(), timeout=request.limits.timeout_seconds
            )
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()
            await self._force_remove(container_name)
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            await self._force_remove(container_name)
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

    async def _force_remove(self, container_name: str) -> None:
        if not self.runtime:
            return
        try:
            cleanup = await asyncio.create_subprocess_exec(
                self.runtime,
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


def _runtime_environment() -> dict[str, str]:
    retained = {
        "PATH",
        "HOME",
        "XDG_RUNTIME_DIR",
        "DOCKER_HOST",
        "CONTAINER_HOST",
    }
    return {
        name: value for name in retained if (value := os.environ.get(name)) is not None
    }


__all__ = [
    "AnalysisOnlyRunner",
    "ContainerSandboxRunner",
    "SandboxError",
    "SandboxLimits",
    "SandboxNetwork",
    "SandboxRequest",
    "SandboxResult",
    "SandboxRunner",
    "SandboxUnavailable",
    "SandboxWorkspaceAccess",
]
