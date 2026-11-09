"""One aggregate reservation for a kernel-managed, persistent search service.

Image provisioning is deliberately separate: a memory limit on a Docker client
does not contain the daemon's pull/build work. Only verified cached images are
usable until that daemon-side runner contract is available.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
from uuid import uuid4

from sqlalchemy import select

from .database import MemoryJobRow
from .domain import WebSearchEngine, WebSearchRuntimeState
from .memory_policy import (
    MIB,
    MemoryBudgetError,
    MemoryLimits,
    require_memory_enforcement,
    select_memory_policy,
)
from .memory_recovery import runner_fingerprint, runner_identity
from .memory_runtime import MemoryComputeController, probe_memory_runner
from .runner_profiles import container_runner_profile
from .sandbox import (
    ContainerSandboxRunner,
    SandboxError,
    SandboxExecutionKind,
    SandboxLimits,
    SandboxNetwork,
    SandboxRequest,
    _unsafe_for_mount,
)
from .web_search import (
    CONTAINER_PORT,
    EXPECTED_REPOSITORY,
    SOURCE_REFERENCE,
    SearchRuntime,
    SearchRuntimeUnavailable,
    WebSearchError,
    _free_port,
    _normalized_repository,
    load_settings,
)


@dataclass(frozen=True)
class CachedSearchImage:
    reference: str
    digest: str
    command: list[str]
    working_directory: str


class ManagedSearchUnavailable(SearchRuntimeUnavailable):
    def __init__(self, detail: str, *, job=None):
        super().__init__(detail)
        self.memory_job_id = job.id if job is not None else None
        self.memory_state = job.state if job is not None else None
        self.memory_outcome = job.outcome if job is not None else None
        self.memory_enforcement = (
            job.evidence.model_dump(mode="json")
            if job is not None and job.evidence is not None
            else None
        )


class SearchContainerRunner(ContainerSandboxRunner):
    """Fixed service publication; the normal worker port policy stays closed."""

    def __init__(
        self, *, configuration: Path, port: int, image: CachedSearchImage, **kwargs
    ):
        super().__init__(**kwargs)
        self.configuration, self.port, self.search_image = configuration, port, image
        self.log_directory = (
            configuration.parent.parent.parent / "logs" / configuration.parent.name
        )

    async def run_stream(self, request, *, on_chunk=None, **kwargs):
        written = {"stdout": 0, "stderr": 0}

        async def capture(stream, chunk):
            remaining = max(0, 100_000 - written[stream])
            if remaining:
                payload = chunk[:remaining]
                with (self.log_directory / (stream + ".log")).open("ab") as output:
                    output.write(payload)
                written[stream] += len(payload)
            if on_chunk is not None:
                await on_chunk(stream, chunk)

        return await super().run_stream(request, on_chunk=capture, **kwargs)

    def _argv(
        self,
        request,
        workspace,
        *,
        network_mode=None,
        interactive=False,
        tty=False,
        **kwargs,
    ):
        if (
            network_mode is not None
            or workspace is not None
            or tty
            or not interactive
            or request.image != self.search_image.reference
            or request.command != self.search_image.command
            or request.network != SandboxNetwork.UNRESTRICTED
            or self.configuration.is_symlink()
            or not self.configuration.is_file()
            or _unsafe_for_mount(self.configuration.resolve(strict=True))
            or not 1024 <= self.port <= 65_535
        ):
            raise SearchRuntimeUnavailable(
                "The managed search launch boundary is invalid"
            )
        argv = super()._argv(
            request, workspace, interactive=interactive, tty=tty, **kwargs
        )
        argv[argv.index("--workdir=/tmp")] = (
            "--workdir=" + self.search_image.working_directory
        )
        index = len(argv) - len(request.command) - 1
        argv[index:index] = [
            f"--publish=127.0.0.1:{self.port}:{CONTAINER_PORT}",
            f"--mount=type=bind,src={self.configuration.resolve()},dst=/etc/searxng/settings.yml,readonly=true",
            "--tmpfs=/run:rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532",
            "--tmpfs=/var/cache/searxng:rw,noexec,nosuid,nodev,size=64m,uid=65532,gid=65532",
        ]
        return argv


class ManagedSearchRuntime(SearchRuntime):
    def __init__(
        self, *, store, data_root: Path, memory_compute: MemoryComputeController
    ):
        super().__init__(store=store, data_root=data_root.expanduser().resolve())
        self.memory_compute = memory_compute
        self.resource_id = (
            "web-search:"
            + hashlib.sha256(str(self.config_root).encode()).hexdigest()[:32]
        )
        self._job = self._runner_instance = None
        self._active_controller: MemoryComputeController | None = None
        self._configuration_export: Path | None = None

    @property
    def _owned_controller(self) -> MemoryComputeController:
        if self._active_controller is None:
            raise SearchRuntimeUnavailable(
                "The original search controller is unavailable; capacity remains charged"
            )
        return self._active_controller

    def _runner(self, profile, *, configuration=None, port=0, image=None):
        kwargs = dict(
            profile=container_runner_profile(profile),
            memory_compute=self.memory_compute,
            memory_profile_id=profile.id,
            allowed_environment={"HOME", "SEARXNG_SETTINGS_PATH"},
        )
        if image is None:
            return ContainerSandboxRunner(**kwargs)
        return SearchContainerRunner(
            configuration=configuration, port=port, image=image, **kwargs
        )

    async def _cached_image(self, runner) -> CachedSearchImage:
        settings = await load_settings(self.store)
        accepted = settings.image_digest if settings else None
        reference = (
            f"{EXPECTED_REPOSITORY}@{accepted}" if accepted else SOURCE_REFERENCE
        )
        output, error, code = await runner._capture(
            "image", "inspect", reference, "--format", "{{json .}}"
        )
        if code:
            raise SearchRuntimeUnavailable(
                "No verified cached search image is available. Image download/build needs a memory-managed daemon runner; Nebula will not launch an unrestricted provisioning client. "
                + error[-300:]
            )
        document = json.loads(output)
        if isinstance(document, list):
            document = document[0] if len(document) == 1 else {}
        digests = set()
        for value in document.get("RepoDigests", []):
            if isinstance(value, str) and "@" in value:
                repository, digest = value.rsplit("@", 1)
                if _normalized_repository(
                    repository
                ) == EXPECTED_REPOSITORY and re.fullmatch(
                    r"sha256:[0-9a-f]{64}", digest
                ):
                    digests.add(digest)
        if not digests or (accepted is not None and accepted not in digests):
            raise SearchRuntimeUnavailable(
                "The cached image does not prove the accepted official SearXNG digest"
            )
        digest = accepted or sorted(digests)[0]
        config = document.get("Config") or {}
        entrypoint, command = config.get("Entrypoint") or [], config.get("Cmd") or []
        if (
            not isinstance(entrypoint, list)
            or not isinstance(command, list)
            or not 1 <= len(entrypoint + command) <= 100
            or any(
                not isinstance(value, str)
                or not value
                or "\x00" in value
                or len(value) > 8192
                for value in entrypoint + command
            )
            or sum(map(len, entrypoint + command)) > 32768
        ):
            raise SearchRuntimeUnavailable(
                "The cached search image has no bounded default command"
            )
        working = config.get("WorkingDir") or "/"
        if (
            not isinstance(working, str)
            or not PurePosixPath(working).is_absolute()
            or "\x00" in working
            or len(working) > 2048
        ):
            raise SearchRuntimeUnavailable(
                "The cached search image has an invalid working directory"
            )
        return CachedSearchImage(
            f"{EXPECTED_REPOSITORY}@{digest}", digest, entrypoint + command, working
        )

    async def initialization_capability(self, *, action: str = "start") -> dict:
        if action not in {"install", "start", "test", "reconfigure"}:
            return {
                "supported": False,
                "detail": "Unknown search initialization action",
            }
        try:
            runner = self._runner(await self._profile())
            healthy, detail = await runner.available()
            if not healthy:
                raise SearchRuntimeUnavailable(detail)
            await self._cached_image(runner)
            if action != "install":
                capability = await probe_memory_runner(
                    runner, profile_id=runner.memory_profile_id
                )
                status = await self.memory_compute.service.status()
                selected = select_memory_policy(
                    status.policy,
                    status.revision,
                    status.administrator_limits,
                    capability,
                ).limits
                maximum = min(selected.max_bytes, 512 * MIB)
                require_memory_enforcement(
                    MemoryLimits(
                        max_bytes=maximum,
                        swap_bytes=0,
                        high_bytes=min(selected.high_bytes, maximum)
                        if selected.high_bytes is not None
                        else None,
                    ),
                    capability,
                )
            return {
                "supported": True,
                "detail": "Verified cached image; one persistent managed service reservation, at most 512 MiB and zero swap. No per-query charge or additional Core-reserve charge. Running readiness still requires kernel and loopback readback.",
            }
        except (
            WebSearchError,
            MemoryBudgetError,
            SandboxError,
            OSError,
            ValueError,
        ) as error:
            # diagnostic-expected: unavailable image/runner/policy returns explicit unsupported capability without starting work.
            return {"supported": False, "detail": str(error)}

    async def install(self) -> str:
        runner = self._runner(await self._profile())
        healthy, detail = await runner.available()
        if not healthy:
            raise SearchRuntimeUnavailable(detail)
        image = await self._cached_image(runner)
        await self.record(image_digest=image.digest)
        return image.digest

    async def _pending(self):
        async with self.store.database.session() as session:
            return await session.scalar(
                select(MemoryJobRow.id)
                .where(
                    MemoryJobRow.resource_kind == "web_search_service",
                    MemoryJobRow.resource_id == self.resource_id,
                    MemoryJobRow.state.in_(
                        (
                            "reserved",
                            "starting",
                            "running",
                            "updating",
                            "uncertain",
                            "queued",
                        )
                    ),
                )
                .limit(1)
            )

    async def _observed(self):
        job = await self._owned_controller.service.get_job(self._job.job_id)
        runner = self._runner_instance
        owner = job.launch_owner
        if (
            owner is None
            or runner_fingerprint(runner) != owner.runner_fingerprint
            or await runner_identity(runner) != owner.runner_identity
        ):
            raise SearchRuntimeUnavailable(
                "The original search runner identity is unavailable or changed; its reservation remains charged"
            )
        group = self._job.guard.container_id
        if not group or job.evidence is None or job.evidence.group_id != group:
            raise SearchRuntimeUnavailable(
                "The search service has no verified immutable group"
            )
        output, error, code = await runner._capture(
            "inspect", group, "--format", "{{json .}}"
        )
        if code:
            raise SearchRuntimeUnavailable(
                "The owned search group could not be inspected: " + error[-300:]
            )
        document = json.loads(output)
        bindings = (
            document.get("NetworkSettings", {})
            .get("Ports", {})
            .get(f"{CONTAINER_PORT}/tcp")
        )
        config = document.get("HostConfig", {})
        if (
            document.get("Id") != group
            or document.get("Config", {}).get("Labels", {}).get("nebula.memory.job")
            != job.id
            or document.get("State", {}).get("Running") is not True
            or bindings
            != [{"HostIp": "127.0.0.1", "HostPort": str(self._runner_instance.port)}]
            or config.get("Memory") != job.evidence.applied.max_bytes
            or config.get("MemorySwap") != job.evidence.applied.reservation_bytes
            or document.get("Config", {}).get("User") != "65532:65532"
            or config.get("ReadonlyRootfs") is not True
        ):
            raise SearchRuntimeUnavailable(
                "Search loopback publication, identity or applied limits differ from the admitted service"
            )
        return job

    async def state(self):
        if self._job is None:
            if await self._pending():
                return (
                    WebSearchRuntimeState.FAILED,
                    "A prior search launch awaits verified memory reconciliation; no existing container is adopted by name",
                )
            settings = await load_settings(self.store)
            return (
                WebSearchRuntimeState.STOPPED
                if settings
                else WebSearchRuntimeState.ABSENT
            ), "No owned managed search service is running"
        if self._job.task.done():
            job = await self._owned_controller.service.get_job(self._job.job_id)
            return WebSearchRuntimeState.FAILED, "Managed search ended: " + (
                job.outcome or job.detail or job.state
            )
        try:
            await self._observed()
            return (
                WebSearchRuntimeState.READY,
                "Search is running in its verified memory group on host loopback",
            )
        except (WebSearchError, MemoryBudgetError, ValueError) as error:
            # diagnostic-expected: read-only health exposes failed verification and retains the charged group.
            return WebSearchRuntimeState.FAILED, str(error)

    async def ensure_ready(self) -> int:
        async with self._lock:
            if self._job is not None:
                if self._job.task.done():
                    ended = await self._owned_controller.service.get_job(
                        self._job.job_id
                    )
                    await self._stop_locked()
                    raise ManagedSearchUnavailable(
                        "The previous search service ended; inspect its memory outcome and explicitly retry",
                        job=ended,
                    )
                await self._observed()
                if await self._probe(self._runner_instance.port):
                    self._touch()
                    return self._runner_instance.port
                await self._stop_locked()
                raise SearchRuntimeUnavailable(
                    "The owned search service stopped answering; it was stopped without an automatic retry"
                )
            if await self._pending():
                raise SearchRuntimeUnavailable(
                    "A prior search launch remains charged; wait for verified reconciliation before retrying"
                )
            profile = await self._profile()
            inspector = self._runner(profile)
            healthy, detail = await inspector.available()
            if not healthy:
                raise SearchRuntimeUnavailable(detail)
            image = await self._cached_image(inspector)
            settings = await load_settings(self.store)
            port = settings.port if settings and settings.port else _free_port()
            source = self._write_configuration(
                settings.engines if settings else list(WebSearchEngine)[:3], port
            )
            job_id = "search-" + uuid4().hex
            export = self.config_root / "managed" / job_id
            export.mkdir(parents=True, mode=0o700)
            export.chmod(0o700)
            configuration = export / "settings.yml"
            configuration.write_bytes(source.read_bytes())
            configuration.chmod(0o444)
            logs = self.config_root / "logs" / job_id
            logs.mkdir(parents=True, mode=0o700)
            logs.chmod(0o700)
            for name in ("stdout.log", "stderr.log"):
                path = logs / name
                path.write_bytes(b"")
                path.chmod(0o600)
            runner = self._runner(
                profile, configuration=configuration, port=port, image=image
            )
            request = SandboxRequest(
                image=image.reference,
                command=image.command,
                workspace=export,
                execution_kind=SandboxExecutionKind.NETWORK_TOOL,
                network=SandboxNetwork.UNRESTRICTED,
                environment={
                    "HOME": "/tmp",
                    "SEARXNG_SETTINGS_PATH": "/etc/searxng/settings.yml",
                },
                retain_output=False,
                limits=SandboxLimits(
                    memory_mb=512, memory_swap_mb=0, pids=256, output_bytes=100_000
                ),
            )
            self._configuration_export = export
            try:
                self._job = await self.memory_compute.open_persistent(
                    runner,
                    request,
                    job_id=job_id,
                    project_id=None,
                    resource_kind="web_search_service",
                    resource_id=self.resource_id,
                    container_name="nebula-" + job_id,
                )
                self._runner_instance, self._active_controller = (
                    runner,
                    self.memory_compute,
                )
                await self._observed()
                if not await self._wait_ready(port):
                    raise SearchRuntimeUnavailable(
                        "The memory-managed search service did not become ready; no unrestricted fallback was attempted"
                    )
                await self.record(
                    image_digest=image.digest,
                    port=port,
                    container_state=WebSearchRuntimeState.READY,
                )
                self._touch()
                self._schedule_idle_stop()
                return port
            except BaseException as error:
                # diagnostic-expected: failure/cancellation keeps the common controller's verified-cleanup boundary.
                if self._job is not None:
                    await self._stop_locked()
                else:
                    try:
                        current = await self.memory_compute.service.get_job(job_id)
                    except MemoryBudgetError:
                        # diagnostic-expected: no durable job means the common controller never admitted this launch.
                        # No durable launch record means computation never entered the controller.
                        current = None
                    if (
                        current is None
                        or current.state == "finished"
                        or current.reserved_bytes == 0
                    ):
                        shutil.rmtree(export)
                        self._configuration_export = None
                if isinstance(error, (WebSearchError, MemoryBudgetError, SandboxError)):
                    try:
                        current = await self.memory_compute.service.get_job(job_id)
                    except MemoryBudgetError:
                        # diagnostic-expected: absent optional job evidence cannot replace the original launch error below.
                        current = None
                    raise ManagedSearchUnavailable(str(error), job=current) from error
                raise

    async def _stop_locked(self):
        if self._job is None:
            if await self._pending():
                raise SearchRuntimeUnavailable(
                    "A prior owned search group requires verified reconciliation; no name-based removal is permitted"
                )
            return
        # Pin the original daemon before invoking its immutable-group cleanup.
        current = await self._owned_controller.service.get_job(self._job.job_id)
        owner = current.launch_owner
        if (
            owner is None
            or runner_fingerprint(self._runner_instance) != owner.runner_fingerprint
            or await runner_identity(self._runner_instance) != owner.runner_identity
        ):
            raise SearchRuntimeUnavailable(
                "The original search daemon is unavailable; capacity remains charged"
            )
        await self._job.close()
        current = await self._owned_controller.service.get_job(self._job.job_id)
        if current.state != "finished" or current.reserved_bytes:
            raise SearchRuntimeUnavailable(
                "Search cleanup is unverified; capacity remains charged"
            )
        if self._configuration_export is not None:
            shutil.rmtree(self._configuration_export)
        self._job = self._runner_instance = self._active_controller = (
            self._configuration_export
        ) = None

    async def stop(self):
        async with self._lock:
            await self._stop_locked()
            await self.record(container_state=WebSearchRuntimeState.STOPPED)

    async def remove(self):
        async with self._lock:
            await self._stop_locked()
            await self.record(container_state=WebSearchRuntimeState.ABSENT)

    async def close(self):
        task = self._idle_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            had_job = self._job is not None
            await self._stop_locked()
            if had_job:
                await self.record(container_state=WebSearchRuntimeState.STOPPED)
