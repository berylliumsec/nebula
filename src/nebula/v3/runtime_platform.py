"""Prepared Kali runtime, workspaces, runner health, and MCP capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, cast

from .artifacts import ArtifactStore
from .diagnostics import gather_diagnostic, record_caught_exception
from .domain import (
    Engagement,
    McpServerProfile,
    RunnerIsolation,
    RunnerProfile as StoredRunnerProfile,
    ScopePolicy,
    utc_now,
)
from .kali_tool_inventory import TOOL_NAME_PATTERN
from .mcp import McpProbeService, build_mcp_tool_plugins
from .policy import PolicyEngine
from .providers import ModelProvider
from .sandbox import (
    AnalysisOnlyRunner,
    ContainerEgressController,
    ContainerImagePreparer,
    ContainerRuntimeType,
    ContainerSandboxRunner,
    NoEgressController,
    PreparedContainerImage,
    RunnerIsolationMode,
    RunnerPlatform,
    RunnerProfile,
    SandboxError,
)
from .storage import NebulaStore
from .tool_results import ToolOutputService
from .tools import (
    StoreToolEvidenceRecorder,
    StoreToolLedger,
    ToolBroker,
    ToolRegistry,
    ToolSpec,
    register_artifact_retrieval_tools,
)


LOGGER = logging.getLogger(__name__)

DEFAULT_KALI_SOURCE_IMAGE = "docker.io/kalilinux/kali-rolling:latest"
DEFAULT_KALI_REPOSITORY = "docker.io/kalilinux/kali-rolling"
KALI_RUNTIME_METADATA_SCHEMA = "nebula.kali-runtime/v1"

# Compatibility name used by the human-terminal surface. Both the human and
# agent runtimes are deliberately prepared from this one image.
DEFAULT_HUMAN_TERMINAL_SOURCE_IMAGE = DEFAULT_KALI_SOURCE_IMAGE


class RuntimePlatformError(RuntimeError):
    """The local Kali runtime or its runner cannot satisfy a request."""


def _runner_profile_fingerprint(profile: StoredRunnerProfile) -> str:
    """Bind preparation to runner security configuration, not health telemetry."""

    payload = {
        "runtime": profile.runtime.value,
        "executable": profile.executable,
        "context": profile.context,
        "socket": profile.socket,
        "platform": profile.platform,
        "isolation": profile.isolation.value,
        "seccomp_profile": profile.seccomp_profile,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class RuntimeToolComponents:
    broker: ToolBroker
    scope: ScopePolicy
    workspace: Path
    specs: Mapping[str, ToolSpec]
    runtime_digest: str = ""


@dataclass(frozen=True)
class HumanTerminalRuntimeResolution:
    profile: StoredRunnerProfile
    runner: ContainerSandboxRunner
    workspace: Path
    image: PreparedContainerImage


@dataclass(frozen=True)
class OperatorRuntimeCommand:
    interpreter: str
    arguments: list[str]


@dataclass(frozen=True)
class OperatorRuntimeResolution:
    canonical_language: str
    runtime: OperatorRuntimeCommand
    profile: StoredRunnerProfile
    image: str
    runtime_digest: str
    runner: ContainerSandboxRunner
    workspace: Path


class RuntimePlatform:
    """Own the one prepared Kali image and the non-catalog runtime services."""

    def __init__(
        self,
        *,
        store: NebulaStore,
        artifact_store: ArtifactStore,
        data_root: Path,
        execution_enabled: bool = False,
        kali_source_image: str = DEFAULT_KALI_SOURCE_IMAGE,
        kali_repository: str = DEFAULT_KALI_REPOSITORY,
    ) -> None:
        self.store = store
        self.artifact_store = artifact_store
        self.data_root = data_root.expanduser().resolve()
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_root.chmod(0o700)
        self.workspace_root = self.data_root / "engagement-workspaces"
        self.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace_root.chmod(0o700)
        self.parser_root = self.data_root / "parser-workspaces"
        self.parser_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.parser_root.chmod(0o700)
        self.execution_enabled = execution_enabled
        self.kali_source_image = kali_source_image
        self.kali_repository = kali_repository
        self.runtime_metadata_path = self.data_root / "kali-runtime.json"
        self._prepared_images: dict[tuple[str, int], PreparedContainerImage] = {}
        self._image_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self.mcp_service: McpProbeService | None = None

    def bind_mcp_service(self, service: McpProbeService) -> None:
        if service.store is not self.store:
            raise ValueError("MCP service must use the runtime platform store")
        self.mcp_service = service

    def chat_components(
        self,
        *,
        engagement_id: str,
        turn_id: str,
        provider: ModelProvider,
        model: str,
        mcp_profiles: tuple[McpServerProfile, ...] = (),
        include_oci: bool = False,
        allow_empty: bool = False,
        **_obsolete_snapshot: Any,
    ) -> RuntimeToolComponents:
        """Build MCP-only components; OCI command execution is fixed elsewhere."""

        del turn_id, provider, model
        if include_oci:
            raise RuntimePlatformError(
                "OCI capabilities are available only through run_command"
            )
        engagement = self.store.get(Engagement, engagement_id)
        scope = (
            self.store.get(ScopePolicy, engagement.scope_policy_id)
            if engagement.scope_policy_id
            else ScopePolicy(
                id=f"scope:{engagement.id}", engagement_id=engagement.id
            )
        )
        if not mcp_profiles and not allow_empty:
            raise RuntimePlatformError("no MCP server was selected")
        registry = ToolRegistry()
        if mcp_profiles:
            if self.mcp_service is None:
                raise RuntimePlatformError("Core MCP execution service is unavailable")
            try:
                for plugin in build_mcp_tool_plugins(self.mcp_service, mcp_profiles):
                    registry.register(plugin)
            except Exception as exc:
                raise RuntimePlatformError(str(exc)) from exc
        register_artifact_retrieval_tools(
            registry,
            output_service=ToolOutputService(self.store, self.artifact_store),
        )
        broker = ToolBroker(
            registry=registry,
            policy_engine=PolicyEngine(),
            runner=AnalysisOnlyRunner(),
            ledger=StoreToolLedger(self.store),
            workspace_resolver=lambda owner_engagement_id: self.workspace_for(
                owner_engagement_id
            ),
            evidence_recorder=StoreToolEvidenceRecorder(
                self.store, self.artifact_store
            ),
        )
        return RuntimeToolComponents(
            broker=broker,
            scope=scope,
            workspace=self.workspace_for(engagement_id),
            specs={spec.name: spec for spec in registry.specs()},
        )

    def workspace_for(self, engagement_id: str) -> Path:
        component = hashlib.sha256(engagement_id.encode("utf-8")).hexdigest()
        workspace = self.workspace_root / component
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The OCI worker uses a fixed unmapped non-root UID. The 0700 parent
        # keeps engagement workspaces private from other host users.
        workspace.chmod(0o777)
        return workspace

    async def verify_runner(self, profile_id: str) -> StoredRunnerProfile:
        stored = self.store.get(StoredRunnerProfile, profile_id)
        healthy, detail = await self._runner(stored).available()
        return self.store.update(
            StoredRunnerProfile,
            stored.id,
            {
                "healthy": healthy,
                "last_health_at": utc_now(),
                "last_health_detail": detail,
            },
            expected_revision=stored.revision,
        )

    async def resolve_human_terminal_runtime(
        self,
        engagement_id: str,
        *,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> HumanTerminalRuntimeResolution:
        """Prepare and return the one Kali image shared with agent automation."""

        profile = self.resolve_human_terminal_profile(engagement_id)
        key = (profile.id, profile.revision)
        image = self._prepared_images.get(key)
        if image is None:
            lock = self._image_locks.setdefault(key, asyncio.Lock())
            async with lock:
                image = self._prepared_images.get(key)
                if image is None:
                    try:
                        image = await ContainerImagePreparer(
                            runner=self._runner(profile),
                            platform=cast(
                                Literal["linux/amd64", "linux/arm64"],
                                profile.platform,
                            ),
                            source_reference=self.kali_source_image,
                            expected_repository=self.kali_repository,
                            on_progress=on_progress,
                        ).prepare()
                    except (SandboxError, ValueError) as exc:
                        record_caught_exception(
                            "runtime",
                            "runtime.prepare.failed",
                            "The Kali runtime could not be prepared.",
                            exc,
                            stage="prepare",
                        )
                        raise RuntimePlatformError(str(exc)) from exc
                    self._persist_runtime_metadata(profile, image)
                    self._prepared_images[key] = image
        return HumanTerminalRuntimeResolution(
            profile=profile,
            runner=self._runner(profile),
            workspace=self.workspace_for(engagement_id),
            image=image,
        )

    def _persist_runtime_metadata(
        self, profile: StoredRunnerProfile, image: PreparedContainerImage
    ) -> None:
        payload = {
            "schema": KALI_RUNTIME_METADATA_SCHEMA,
            "verified_at": utc_now().isoformat(),
            "runner_profile_id": profile.id,
            "runner_profile_revision": profile.revision,
            "runner_profile_fingerprint": _runner_profile_fingerprint(profile),
            "source_reference": image.source_reference,
            "source_is_digest_pinned": "@sha256:" in image.source_reference,
            "base_resolved_reference": image.base_resolved_reference,
            "base_digest": image.base_digest,
            "resolved_reference": image.resolved_reference,
            "image_digest": image.digest,
            "platform": image.platform,
            "installed_packages": list(image.installed_packages),
            "security_tools": list(image.security_tools),
            "security_tool_packages": list(image.security_tool_packages),
            "security_tool_provenance": {
                tool: list(packages)
                for tool, packages in image.security_tool_provenance
            },
            "security_tool_manifest_sha256": image.security_tool_manifest_sha256,
            "binary_inventory": [
                {"name": name, "path": path, "version": version}
                for name, path, version in image.binary_inventory
            ],
            "registry_refreshed": image.refreshed,
        }
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".kali-runtime-",
                suffix=".tmp",
                dir=self.data_root,
                delete=False,
            ) as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                temporary = Path(stream.name)
            temporary.chmod(0o600)
            temporary.replace(self.runtime_metadata_path)
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise RuntimePlatformError(
                "verified Kali runtime metadata could not be persisted"
            ) from exc

    def _runtime_metadata(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.runtime_metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            # diagnostic-expected: absent/invalid cached metadata means no verified inventory.
            return None
        return payload if isinstance(payload, dict) else None

    def last_human_terminal_security_inventory(
        self,
    ) -> tuple[str, str, tuple[str, ...]] | None:
        payload = self._runtime_metadata()
        if payload is None:
            return None
        image_digest = payload.get("image_digest")
        inventory_digest = payload.get("security_tool_manifest_sha256")
        tools = payload.get("security_tools")
        if (
            payload.get("schema") != KALI_RUNTIME_METADATA_SCHEMA
            or not isinstance(image_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None
            or not isinstance(inventory_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", inventory_digest) is None
            or not isinstance(tools, list)
            or not tools
            or tools != sorted(set(tools))
            or any(
                not isinstance(tool, str) or TOOL_NAME_PATTERN.fullmatch(tool) is None
                for tool in tools
            )
        ):
            return None
        return image_digest, inventory_digest, tuple(tools)

    def last_automation_runtime_metadata(self) -> dict[str, Any] | None:
        payload = self._runtime_metadata()
        if payload is None:
            return None
        image_digest = payload.get("image_digest")
        runner_id = payload.get("runner_profile_id")
        runner_revision = payload.get("runner_profile_revision")
        runner_fingerprint = payload.get("runner_profile_fingerprint")
        inventory = payload.get("binary_inventory")
        if (
            payload.get("schema") != KALI_RUNTIME_METADATA_SCHEMA
            or not isinstance(image_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None
            or not isinstance(runner_id, str)
            or not isinstance(runner_revision, int)
            or not isinstance(inventory, list)
            or not inventory
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("version"), str)
                for item in inventory
            )
        ):
            return None
        return {
            "image": image_digest,
            "digest": image_digest,
            "runner_profile_id": runner_id,
            "runner_profile_revision": runner_revision,
            "runner_profile_fingerprint": (
                runner_fingerprint
                if isinstance(runner_fingerprint, str)
                and re.fullmatch(r"[0-9a-f]{64}", runner_fingerprint)
                else None
            ),
            "inventory": inventory,
            "detail": "using the verified prepared Kali headless image",
        }

    def resolve_human_terminal_profile(self, engagement_id: str) -> StoredRunnerProfile:
        if not self.execution_enabled:
            raise RuntimePlatformError("operator execution is disabled in this Core")
        self.store.get(Engagement, engagement_id)
        profiles = [
            profile
            for profile in self.store.list_entities(StoredRunnerProfile, limit=1_000)
            if profile.enabled and profile.healthy
        ]
        local = next((profile for profile in profiles if profile.id == "local"), None)
        if local is not None:
            return local
        if len(profiles) == 1:
            return profiles[0]
        if not profiles:
            raise RuntimePlatformError(
                "Kali runtime requires an enabled, verified healthy runner profile"
            )
        raise RuntimePlatformError(
            "Kali runtime runner is ambiguous; name the preferred profile 'local'"
        )

    async def cleanup_operator_terminals(self) -> None:
        profiles = self.store.list_entities(StoredRunnerProfile, limit=1_000)
        seen: set[tuple[str, str | None, str, str, str | None]] = set()
        runners: list[ContainerSandboxRunner] = []
        for profile in profiles:
            if not profile.enabled or not profile.healthy or profile.last_health_at is None:
                continue
            identity = (
                profile.executable,
                profile.context,
                profile.runtime.value,
                profile.isolation.value,
                profile.seccomp_profile,
            )
            if identity in seen:
                continue
            try:
                runner = self._runner(profile)
                eligible, detail = runner.terminal_cleanup_eligibility()
                if not eligible:
                    LOGGER.warning(
                        "Skipped orphan terminal cleanup for runner %s: %s",
                        profile.id,
                        detail,
                    )
                    continue
                runners.append(runner)
                seen.add(identity)
            except (ValueError, RuntimePlatformError) as exc:
                # diagnostic-expected: warning is emitted and cleanup continues with other runners.
                LOGGER.warning(
                    "Skipped orphan terminal cleanup for runner %s: %s",
                    profile.id,
                    str(exc)[:1_000],
                )
        if runners:
            await gather_diagnostic(
                *(runner.cleanup_terminal_containers() for runner in runners),
                feature="sandbox",
                event_code="sandbox.orphan_cleanup.runner_failed",
                failure_message="A runner could not complete orphan cleanup.",
                stage="orphan-cleanup",
            )

    def resolve_operator_runtime(
        self, engagement_id: str, language: str, *, network: bool
    ) -> OperatorRuntimeResolution:
        if not self.execution_enabled:
            raise RuntimePlatformError("operator execution is disabled in this Core")
        self.store.get(Engagement, engagement_id)
        metadata = self.last_automation_runtime_metadata()
        if metadata is None:
            raise RuntimePlatformError("prepare the Kali automation runtime first")
        profile = self.store.get(StoredRunnerProfile, metadata["runner_profile_id"])
        prepared_fingerprint = metadata.get("runner_profile_fingerprint")
        runner_changed = (
            _runner_profile_fingerprint(profile) != prepared_fingerprint
            if prepared_fingerprint is not None
            else profile.revision != metadata["runner_profile_revision"]
        )
        if runner_changed:
            raise RuntimePlatformError("the prepared Kali runtime runner has changed")
        if not profile.enabled or not profile.healthy:
            raise RuntimePlatformError("selected runner is not verified healthy")
        normalized = language.casefold()
        aliases = {"shell": "bash", "python3": "python", "py": "python"}
        canonical = aliases.get(normalized, normalized)
        runtimes = {
            "bash": OperatorRuntimeCommand(
                "/bin/bash", ["--noprofile", "--norc", "-eu", "-o", "pipefail"]
            ),
            "sh": OperatorRuntimeCommand(
                "/bin/bash",
                ["--noprofile", "--norc", "--posix", "-eu", "-o", "pipefail"],
            ),
            "python": OperatorRuntimeCommand(
                "/usr/bin/python3", ["-E", "-s", "-u"]
            ),
        }
        runtime = runtimes.get(canonical)
        if runtime is None:
            raise RuntimePlatformError(f"Kali does not expose runtime {language!r}")
        image = metadata["image"]
        return OperatorRuntimeResolution(
            canonical_language=canonical,
            runtime=runtime,
            profile=profile,
            image=image,
            runtime_digest=metadata["digest"],
            runner=self._runner(
                profile,
                egress_helper_image=image if network else None,
            ),
            workspace=self.workspace_for(engagement_id),
        )

    def _runner(
        self,
        stored: StoredRunnerProfile,
        *,
        egress_helper_image: str | None = None,
    ) -> ContainerSandboxRunner:
        if stored.isolation == RunnerIsolation.ROOTLESS:
            host = RunnerPlatform.LINUX
            isolation = RunnerIsolationMode.LINUX_ROOTLESS
            machine = None
        elif stored.isolation == RunnerIsolation.PODMAN_MACHINE:
            host = RunnerPlatform.MACOS
            isolation = RunnerIsolationMode.PODMAN_MACHINE
            machine = stored.context or "podman-machine-default"
        else:
            host = RunnerPlatform.MACOS
            isolation = RunnerIsolationMode.DOCKER_DESKTOP_VM
            machine = None
        profile = RunnerProfile(
            runtime_type=ContainerRuntimeType(stored.runtime.value),
            executable=Path(stored.executable),
            platform=host,
            isolation_mode=isolation,
            context=stored.context,
            machine_name=machine,
            seccomp_profile=(
                Path(stored.seccomp_profile) if stored.seccomp_profile else None
            ),
        )
        egress = (
            ContainerEgressController(helper_image=egress_helper_image)
            if egress_helper_image is not None
            else NoEgressController()
        )
        return ContainerSandboxRunner(
            profile=profile,
            egress_controller=egress,
            workspace_roots=[self.workspace_root, self.parser_root],
        )


def default_runtime_platform(
    *, store: NebulaStore, artifact_store: ArtifactStore, data_root: Path
) -> RuntimePlatform:
    source = os.getenv("NEBULA_KALI_SOURCE_IMAGE") or os.getenv(
        "NEBULA_HUMAN_TERMINAL_SOURCE_IMAGE", DEFAULT_KALI_SOURCE_IMAGE
    ) or DEFAULT_KALI_SOURCE_IMAGE
    if source != DEFAULT_KALI_SOURCE_IMAGE and re.fullmatch(
        re.escape(DEFAULT_KALI_REPOSITORY) + r"@sha256:[0-9a-f]{64}", source
    ) is None:
        raise RuntimePlatformError(
            "NEBULA_KALI_SOURCE_IMAGE must reference the official Kali repository "
            "and be pinned by digest"
        )
    return RuntimePlatform(
        store=store,
        artifact_store=artifact_store,
        data_root=data_root,
        execution_enabled=True,
        kali_source_image=source,
    )


__all__ = [
    "DEFAULT_HUMAN_TERMINAL_SOURCE_IMAGE",
    "DEFAULT_KALI_REPOSITORY",
    "DEFAULT_KALI_SOURCE_IMAGE",
    "HumanTerminalRuntimeResolution",
    "KALI_RUNTIME_METADATA_SCHEMA",
    "OperatorRuntimeResolution",
    "RuntimePlatform",
    "RuntimePlatformError",
    "RuntimeToolComponents",
    "default_runtime_platform",
]
