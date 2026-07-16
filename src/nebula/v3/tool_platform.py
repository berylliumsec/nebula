"""Local tool-pack facade shared by API, CLI, and supervised missions."""

from __future__ import annotations

from .diagnostics import gather_diagnostic, record_caught_exception, record_diagnostic

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field

from .agent_tooling import BrokeredToolSpecialist, ToolMissionSupervisor, role_for_tool
from .artifacts import ArtifactStore
from .domain import (
    AgentRun,
    Engagement,
    EngagementToolAssignment,
    RiskClass,
    RunnerIsolation,
    RunnerProfile as StoredRunnerProfile,
    ScopePolicy,
    ToolPackInstallation,
    ToolPackInstallationStatus,
    ToolPackTrust,
    utc_now,
)
from .missions import MissionComponents, MissionConfigurationError
from .policy import PolicyEngine
from .providers import ModelProvider
from .sandbox import (
    AnalysisOnlyRunner,
    ContainerEgressController,
    ContainerImagePreparer,
    ContainerRuntimeType,
    ContainerSandboxRunner,
    ContainerToolPackRuntimeAdapter,
    NoEgressController,
    PreparedContainerImage,
    RunnerIsolationMode,
    RunnerPlatform,
    RunnerProfile,
    SandboxError,
)
from .mcp import McpProbeService, build_mcp_tool_plugins
from .domain import McpServerProfile
from .storage import NebulaStore
from .toolpack_sdk import ToolPackSDKError, read_tool_pack
from .toolpacks import (
    Ed25519Keyring,
    ImmutableManifestStore,
    SignatureEnvelope,
    ToolCatalogClient,
    ToolCatalogEntry,
    ToolPackInstallError,
    ToolPackInstaller,
    ToolPackManifestV1,
    ToolPackOperatorRuntime,
    ToolPackRuntimeAdapter,
    build_tool_registry,
    canonical_manifest_json,
    default_tool_pack_root,
    fetch_bounded_https,
    manifest_digest,
    parse_manifest_json,
)
from .kali_tool_inventory import TOOL_NAME_PATTERN
from .toolparsers import SandboxParserExecutor
from .tool_interfaces import (
    COMMAND_SELECTOR_INPUT_SCHEMA,
    COMMAND_SELECTOR_NAME,
    MAX_INTERFACE_CATALOG_BYTES,
    ToolInterfaceCatalog,
    load_interface_catalog,
    load_interface_catalog_file,
    select_command_interface,
)
from .tools import (
    InvocationAnalysisTool,
    StoreToolEvidenceRecorder,
    StoreToolLedger,
    ToolBroker,
    ToolRegistry,
    ToolSpec,
    UnknownTool,
    register_artifact_retrieval_tools,
)
from .tool_results import ToolOutputService


LOGGER = logging.getLogger(__name__)


DEFAULT_CATALOG_URL = "https://berylliumsec.github.io/nebula/toolbox/catalog-v1.json"
DEFAULT_CATALOG_SIGNATURE_URL = (
    "https://berylliumsec.github.io/nebula/toolbox/catalog-v1.json.signature.json"
)
DEFAULT_HUMAN_TERMINAL_SOURCE_IMAGE = "docker.io/kalilinux/kali-rolling:latest"
DEFAULT_HUMAN_TERMINAL_REPOSITORY = "docker.io/kalilinux/kali-rolling"
HUMAN_TERMINAL_IMAGE_METADATA_SCHEMA = "nebula.human-terminal-image/v2"
MAX_REMOTE_MANIFEST_BYTES = 2_000_000
MAX_LOCAL_BUNDLE_BYTES = 100_000_000
DEFAULT_EVENT_RETENTION = 256
MAX_EVENT_RETENTION = 10_000
IMPLICIT_ENVIRONMENT_TOOL_NAMES = frozenset(
    {"environment.shell_local", "environment.shell_network"}
)


@dataclass(frozen=True)
class ChatToolComponents:
    broker: ToolBroker
    scope: ScopePolicy
    workspace: Path
    specs: Mapping[str, ToolSpec]
    tool_pack_digests: tuple[str, ...]
    interface_catalog_digests: tuple[str, ...]
    interface_catalogs_by_manifest: Mapping[str, ToolInterfaceCatalog] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class HumanTerminalRuntimeResolution:
    profile: StoredRunnerProfile
    runner: ContainerSandboxRunner
    workspace: Path
    image: PreparedContainerImage


ToolPackOperation = Literal[
    "install_catalog",
    "install_collection",
    "install_local",
    "verify",
    "update",
    "disable",
]
ToolPackEventPhase = Literal["pending", "pulling", "verifying", "ready", "failed"]


class ToolPackProgressEvent(BaseModel):
    """Sanitized process-local progress metadata safe for operator replay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    occurred_at: datetime
    operation_id: str = Field(min_length=1, max_length=200)
    operation: ToolPackOperation
    phase: ToolPackEventPhase
    installation_id: str | None = Field(default=None, max_length=200)
    pack_identity: str | None = Field(default=None, max_length=500)
    manifest_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_status: ToolPackInstallationStatus | None = None


@dataclass(frozen=True)
class ToolPackEventReplay:
    events: tuple[ToolPackProgressEvent, ...]
    oldest_sequence: int
    latest_sequence: int
    truncated: bool


class ToolPackEventJournal:
    """Bounded monotonic journal; events intentionally do not survive restart."""

    def __init__(self, retention: int = DEFAULT_EVENT_RETENTION) -> None:
        if retention < 1 or retention > MAX_EVENT_RETENTION:
            raise ValueError(
                f"tool-pack event retention must be between 1 and {MAX_EVENT_RETENTION}"
            )
        self.retention = retention
        self._events: deque[ToolPackProgressEvent] = deque(maxlen=retention)
        self._next_sequence = 1
        self._lock = RLock()

    def append(
        self,
        *,
        operation_id: str,
        operation: ToolPackOperation,
        phase: ToolPackEventPhase,
        installation_id: str | None = None,
        pack_identity: str | None = None,
        manifest_digest: str | None = None,
        result_status: ToolPackInstallationStatus | None = None,
    ) -> ToolPackProgressEvent:
        with self._lock:
            event = ToolPackProgressEvent(
                sequence=self._next_sequence,
                occurred_at=utc_now(),
                operation_id=operation_id,
                operation=operation,
                phase=phase,
                installation_id=installation_id,
                pack_identity=pack_identity,
                manifest_digest=manifest_digest,
                result_status=result_status,
            )
            self._events.append(event)
            self._next_sequence += 1
            return event

    def replay(self, after_sequence: int = 0) -> ToolPackEventReplay:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        with self._lock:
            latest = self._next_sequence - 1
            oldest = self._events[0].sequence if self._events else self._next_sequence
            truncated = bool(self._events and after_sequence < oldest - 1)
            events = tuple(
                event for event in self._events if event.sequence > after_sequence
            )
        return ToolPackEventReplay(
            events=events,
            oldest_sequence=oldest,
            latest_sequence=latest,
            truncated=truncated,
        )


@dataclass
class _OperationProgress:
    journal: ToolPackEventJournal
    operation: ToolPackOperation
    operation_id: str = field(default_factory=lambda: str(uuid4()))
    installation_id: str | None = None
    pack_identity: str | None = None
    manifest_digest: str | None = None
    emitted_phases: set[ToolPackEventPhase] = field(default_factory=set)

    def bind_manifest(self, manifest: ToolPackManifestV1) -> None:
        self.pack_identity = manifest.identity
        self.manifest_digest = manifest_digest(manifest)

    def bind_installation(self, installation: ToolPackInstallation) -> None:
        self.installation_id = installation.id
        self.pack_identity = (
            f"{installation.publisher}/{installation.name}@{installation.version}"
        )
        self.manifest_digest = installation.manifest_digest

    def emit(
        self,
        phase: ToolPackEventPhase,
        *,
        result_status: ToolPackInstallationStatus | None = None,
    ) -> None:
        if phase in self.emitted_phases:
            return
        self.emitted_phases.add(phase)
        self.journal.append(
            operation_id=self.operation_id,
            operation=self.operation,
            phase=phase,
            installation_id=self.installation_id,
            pack_identity=self.pack_identity,
            manifest_digest=self.manifest_digest,
            result_status=result_status,
        )


class _ProgressRuntimeAdapter:
    def __init__(
        self, delegate: ToolPackRuntimeAdapter, progress: _OperationProgress
    ) -> None:
        self.delegate = delegate
        self.progress = progress

    async def pull(self, image: str) -> None:
        self.progress.emit("pulling")
        await self.delegate.pull(image)

    async def inspect(self, image: str) -> Any:
        self.progress.emit("verifying")
        return await self.delegate.inspect(image)

    async def smoke_test(
        self, *, image: str, command: list[str], timeout_seconds: int
    ) -> Any:
        self.progress.emit("verifying")
        return await self.delegate.smoke_test(
            image=image,
            command=command,
            timeout_seconds=timeout_seconds,
        )


class ToolPlatformError(RuntimeError):
    """Operator-safe tool-platform configuration or lifecycle failure."""


@dataclass(frozen=True)
class OperatorRuntimeResolution:
    canonical_language: str
    runtime: ToolPackOperatorRuntime
    installation: ToolPackInstallation
    manifest: ToolPackManifestV1
    profile: StoredRunnerProfile
    image: str
    runner: ContainerSandboxRunner
    workspace: Path
    trusted: bool


def _load_public_keys(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        record_caught_exception(
            "toolbox",
            "toolbox.tool_platform.caught_failure_001",
            "A handled toolbox operation raised an exception.",
            exc,
            stage="tool_platform",
        )
        raise ToolPlatformError("tool-pack public-key file is unreadable") from exc
    keys = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(keys, dict) or any(not isinstance(key, str) for key in keys):
        raise ToolPlatformError("tool-pack public-key file must contain a keys object")
    return dict(keys)


def _trusted_public_keys(configured_path: Path | None = None) -> dict[str, Any]:
    """Load the release-embedded keyring plus optional administrator keys."""

    embedded_path = (
        Path(__file__).resolve().parent
        / "tool_pack_assets"
        / "trust"
        / "berylliumsec.json"
    )
    embedded = _load_public_keys(embedded_path)
    if configured_path is None:
        return embedded
    configured = _load_public_keys(configured_path)
    collisions = sorted(set(embedded).intersection(configured))
    if collisions:
        raise ToolPlatformError(
            "administrator tool-pack keys cannot replace embedded release keys: "
            + ", ".join(collisions)
        )
    return {**embedded, **configured}


class ToolPlatform:
    """Own verified packs, local runners, workspaces, and mission components."""

    def __init__(
        self,
        *,
        store: NebulaStore,
        artifact_store: ArtifactStore,
        data_root: Path,
        tool_pack_root: Path | None = None,
        public_keys: Mapping[str, bytes | str | Mapping[str, Any]] | None = None,
        catalog_url: str = DEFAULT_CATALOG_URL,
        catalog_signature_url: str = DEFAULT_CATALOG_SIGNATURE_URL,
        developer_mode: bool = False,
        execution_enabled: bool = False,
        event_retention: int = DEFAULT_EVENT_RETENTION,
        human_terminal_source_image: str = DEFAULT_HUMAN_TERMINAL_SOURCE_IMAGE,
        human_terminal_repository: str = DEFAULT_HUMAN_TERMINAL_REPOSITORY,
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
        self.tool_pack_root = (
            (tool_pack_root or default_tool_pack_root()).expanduser().resolve()
        )
        self.tool_pack_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tool_pack_root.chmod(0o700)
        self.manifests = ImmutableManifestStore(self.tool_pack_root)
        self.has_trusted_keys = bool(public_keys)
        self.keyring = Ed25519Keyring(public_keys or {})
        self.developer_mode = developer_mode
        self.execution_enabled = execution_enabled
        self.human_terminal_source_image = human_terminal_source_image
        self.human_terminal_repository = human_terminal_repository
        self.human_terminal_image_metadata_path = (
            self.data_root / "human-terminal-image.json"
        )
        self.events = ToolPackEventJournal(event_retention)
        self._human_terminal_images: dict[tuple[str, int], PreparedContainerImage] = {}
        self._human_terminal_image_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self.catalog_client = ToolCatalogClient(
            catalog_url=catalog_url,
            signature_url=catalog_signature_url,
            verifier=self.keyring,
            cache_path=self.tool_pack_root / "catalog-cache.json",
        )
        self.mcp_service: McpProbeService | None = None

    def bind_mcp_service(self, service: McpProbeService) -> None:
        """Bind Core-owned MCP transport after credentials/workspaces are ready."""

        if service.store is not self.store:
            raise ValueError("MCP service must use the tool platform store")
        self.mcp_service = service

    async def catalog(self) -> list[dict[str, Any]]:
        if not self.has_trusted_keys:
            return []
        try:
            loaded = await self.catalog_client.fetch()
        except Exception as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_002",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tool_platform",
            )
            raise ToolPlatformError(str(exc)) from exc
        return [self._catalog_payload(entry) for entry in loaded.catalog.entries]

    def list_tools(self) -> list[dict[str, Any]]:
        profiles = {
            profile.id: profile
            for profile in self.store.list_entities(StoredRunnerProfile, limit=1_000)
        }
        result: list[dict[str, Any]] = []
        for installation in self.store.list_entities(ToolPackInstallation, limit=1_000):
            try:
                manifest = self.manifests.get(installation.manifest_digest)
                specs = manifest.tool_specs(
                    self._installation_platform(installation),
                    manifest_digest_value=installation.manifest_digest,
                )
            except Exception as exc:
                error_id = record_diagnostic(
                    "error",
                    "toolbox",
                    "toolbox.manifest.load_failed",
                    "An installed tool pack's immutable manifest could not be loaded.",
                    outcome="failure",
                    stage="manifest-load",
                    retryable=False,
                    safe_failure_cause=(
                        "The installed tool pack manifest failed local integrity or schema validation."
                    ),
                    exception=exc,
                    metadata={
                        "entity_id": installation.id,
                        "digest": installation.manifest_digest,
                    },
                )
                result.append(
                    {
                        "name": f"{installation.publisher}/{installation.name}",
                        "pack_id": installation.id,
                        "manifest_digest": installation.manifest_digest,
                        "description": "Installed pack metadata is unavailable",
                        "risk_class": "local_read",
                        "network_access": False,
                        "requires_approval": False,
                        "available": False,
                        "unavailable_reason": str(exc),
                        "error_id": error_id,
                    }
                )
                continue
            profile = profiles.get(installation.runtime_profile_id)
            for spec in specs:
                reason = self._tool_unavailable_reason(installation, profile, spec)
                result.append(
                    {
                        "name": spec.name,
                        "pack_id": installation.id,
                        "pack_identity": spec.pack_id,
                        "manifest_digest": installation.manifest_digest,
                        "description": spec.description,
                        "risk_class": spec.risk_class.value,
                        "network_access": spec.network_access,
                        "requires_approval": spec.requires_approval,
                        "available": reason is None,
                        "unavailable_reason": reason,
                    }
                )
        return sorted(result, key=lambda item: (item["name"], item["manifest_digest"]))

    def normalize_assignment(
        self, manifest_digest_value: str, tool_names: list[str]
    ) -> list[str]:
        """Validate a pack grant and include its standard shell capabilities."""

        manifest = self.manifests.get(manifest_digest_value)
        declared = {tool.name for tool in manifest.tools}
        unknown = sorted(set(tool_names) - declared)
        if unknown:
            raise ToolPlatformError(
                f"tools are not declared by the selected pack: {unknown}"
            )
        return sorted(set(tool_names) | (declared & IMPLICIT_ENVIRONMENT_TOOL_NAMES))

    def validate_assignment(
        self, manifest_digest_value: str, tool_names: list[str]
    ) -> None:
        """Require every engagement grant to name a tool in the exact pack."""

        self.normalize_assignment(manifest_digest_value, tool_names)

    def _assignment_allows_operator_shell(
        self, assignment: EngagementToolAssignment, capability: str
    ) -> bool:
        if not assignment.enabled:
            return False
        if capability in assignment.allowed_tool_names:
            return True
        if capability not in IMPLICIT_ENVIRONMENT_TOOL_NAMES:
            return False
        try:
            manifest = self.manifests.get(assignment.manifest_digest)
        except ToolPackInstallError as caught_error:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_004",
                "A handled toolbox operation raised an exception.",
                caught_error,
                stage="tool_platform",
            )
            return False
        return capability in {tool.name for tool in manifest.tools}

    async def install_catalog(
        self,
        catalog_id: str,
        *,
        runtime_profile_id: str,
        version: str | None = None,
    ) -> ToolPackInstallation:
        progress = self._begin_operation("install_catalog")
        try:
            return await self._install_catalog_operation(
                catalog_id,
                runtime_profile_id=runtime_profile_id,
                version=version,
                progress=progress,
            )
        except Exception as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_005",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tool_platform",
            )
            progress.emit("failed", result_status=ToolPackInstallationStatus.FAILED)
            if isinstance(exc, ToolPlatformError):
                raise
            raise ToolPlatformError(str(exc)) from exc

    async def install_collection(
        self, collection_id: str, *, runtime_profile_id: str
    ) -> list[ToolPackInstallation]:
        """Install one signed collection, disabling new members on failure."""

        if not self.has_trusted_keys:
            raise ToolPlatformError(
                "this build does not contain a curated tool-pack trust key"
            )
        loaded = await self.catalog_client.fetch()
        candidates = [
            entry
            for entry in loaded.catalog.entries
            if entry.collection_id == collection_id
        ]
        if not candidates:
            raise ToolPlatformError(
                "tool collection is not present in the signed catalog"
            )
        latest: dict[tuple[str, str], ToolCatalogEntry] = {}
        for entry in candidates:
            identity = (entry.publisher, entry.name)
            current = latest.get(identity)
            if current is None or Version(entry.version) > Version(current.version):
                latest[identity] = entry
        entries = sorted(
            latest.values(), key=lambda entry: (entry.collection_order, entry.name)
        )
        existing = {
            (item.manifest_digest, item.runtime_profile_id): item
            for item in self.store.list_entities(ToolPackInstallation, limit=1_000)
            if item.status == ToolPackInstallationStatus.READY
        }
        installed: list[ToolPackInstallation] = []
        created: list[ToolPackInstallation] = []
        progress: _OperationProgress | None = None
        try:
            for entry in entries:
                locked = existing.get((entry.manifest_digest, runtime_profile_id))
                if locked is not None:
                    installed.append(locked)
                    continue
                progress = self._begin_operation("install_collection")
                installation = await self._install_catalog_entry(
                    entry,
                    runtime_profile_id=runtime_profile_id,
                    progress=progress,
                )
                installed.append(installation)
                created.append(installation)
            return installed
        except Exception as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_006",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tool_platform",
            )
            if progress is not None:
                progress.emit("failed", result_status=ToolPackInstallationStatus.FAILED)
            for installation in reversed(created):
                try:
                    self.disable(installation.id)
                except Exception as caught_error:
                    record_caught_exception(
                        "toolbox",
                        "toolbox.tool_platform.caught_failure_007",
                        "A handled toolbox operation raised an exception.",
                        caught_error,
                        stage="tool_platform",
                    )
                    pass
            if isinstance(exc, ToolPlatformError):
                raise
            raise ToolPlatformError(str(exc)) from exc

    async def _install_catalog_operation(
        self,
        catalog_id: str,
        *,
        runtime_profile_id: str,
        version: str | None,
        progress: _OperationProgress,
    ) -> ToolPackInstallation:
        if not self.has_trusted_keys:
            raise ToolPlatformError(
                "this build does not contain a curated tool-pack trust key"
            )
        loaded = await self.catalog_client.fetch()
        entry = self._select_entry(loaded.catalog.entries, catalog_id, version)
        return await self._install_catalog_entry(
            entry, runtime_profile_id=runtime_profile_id, progress=progress
        )

    async def _install_catalog_entry(
        self,
        entry: ToolCatalogEntry,
        *,
        runtime_profile_id: str,
        progress: _OperationProgress,
    ) -> ToolPackInstallation:
        manifest, signature = await self._fetch_manifest(entry)
        if manifest_digest(manifest) != entry.manifest_digest:
            raise ToolPlatformError("catalog manifest digest does not match its entry")
        interface_catalog: ToolInterfaceCatalog | None = None
        interface_path: Path | None = None
        if entry.interface_catalog_url is not None:
            interface_catalog, raw_interface = await self._fetch_interface_catalog(
                entry
            )
            interface_path = self.manifests.put_interface_catalog(
                raw_interface, interface_catalog.digest
            )
        progress.bind_manifest(manifest)
        installation = await self._installer(
            runtime_profile_id, progress=progress
        ).install(
            manifest,
            source=f"catalog:{entry.publisher}/{entry.name}@{entry.version}",
            signature=signature,
        )
        if interface_catalog is not None and interface_path is not None:
            installation = self.store.update(
                ToolPackInstallation,
                installation.id,
                {
                    "interface_catalog_digest": interface_catalog.digest,
                    "interface_catalog_path": str(interface_path),
                },
                expected_revision=installation.revision,
            )
        progress.bind_installation(installation)
        progress.emit("ready", result_status=installation.status)
        return installation

    async def _fetch_interface_catalog(
        self, entry: ToolCatalogEntry
    ) -> tuple[ToolInterfaceCatalog, bytes]:
        assert entry.interface_catalog_url is not None
        assert entry.interface_catalog_digest is not None
        assert entry.interface_tool_count is not None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0), follow_redirects=False
            ) as client:
                payload = await fetch_bounded_https(
                    client,
                    entry.interface_catalog_url,
                    MAX_INTERFACE_CATALOG_BYTES,
                )
            catalog = load_interface_catalog(payload)
        except Exception as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_008",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tool_platform",
            )
            raise ToolPlatformError(
                "Toolbox interface catalog download failed"
            ) from exc
        if catalog.digest != entry.interface_catalog_digest:
            raise ToolPlatformError("Toolbox interface catalog digest mismatch")
        if len(catalog.tools) != entry.interface_tool_count:
            raise ToolPlatformError("Toolbox interface tool count mismatch")
        return catalog, payload

    async def install_local(
        self,
        bundle: bytes,
        *,
        runtime_profile_id: str,
        confirm_permissions: bool,
        assigned_by: str = "local-toolpack-install",
    ) -> ToolPackInstallation:
        progress = self._begin_operation("install_local")
        try:
            if len(bundle) > MAX_LOCAL_BUNDLE_BYTES:
                raise ToolPlatformError("local tool-pack bundle exceeds the API limit")
            incoming = self.tool_pack_root / "incoming"
            incoming.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(
                prefix="tool-pack-",
                suffix=".nebula-toolpack",
                dir=incoming,
                delete=False,
            ) as stream:
                stream.write(bundle)
                temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            archive = read_tool_pack(temporary)
        except (OSError, ToolPackSDKError) as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_009",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tool_platform",
            )
            progress.emit("failed", result_status=ToolPackInstallationStatus.FAILED)
            raise ToolPlatformError(str(exc)) from exc
        except Exception as caught_error:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_010",
                "A handled toolbox operation raised an exception.",
                caught_error,
                stage="tool_platform",
            )
            progress.emit("failed", result_status=ToolPackInstallationStatus.FAILED)
            raise
        finally:
            if "temporary" in locals():
                temporary.unlink(missing_ok=True)
        try:
            progress.bind_manifest(archive.manifest)
            local_interface = None
            local_interface_path = None
            archive_files = getattr(archive, "files", {})
            if raw_interface := archive_files.get("source/interface-catalog.json"):
                local_interface = load_interface_catalog(raw_interface)
                local_interface_path = self.manifests.put_interface_catalog(
                    raw_interface, local_interface.digest
                )
            installation = await self._installer(
                runtime_profile_id, progress=progress
            ).install(
                archive.manifest,
                source="local-upload",
                signature=None,
                local_file=True,
                confirm_unsigned_permissions=confirm_permissions,
            )
            if local_interface is not None and local_interface_path is not None:
                installation = self.store.update(
                    ToolPackInstallation,
                    installation.id,
                    {
                        "interface_catalog_digest": local_interface.digest,
                        "interface_catalog_path": str(local_interface_path),
                    },
                    expected_revision=installation.revision,
                )
            self.enable_local_pack_for_engagements(
                installation,
                assigned_by=assigned_by,
            )
            progress.bind_installation(installation)
            progress.emit("ready", result_status=installation.status)
            return installation
        except Exception as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_011",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tool_platform",
            )
            progress.emit("failed", result_status=ToolPackInstallationStatus.FAILED)
            if isinstance(exc, ToolPlatformError):
                raise
            raise ToolPlatformError(str(exc)) from exc

    def enable_local_pack_for_engagements(
        self,
        installation: ToolPackInstallation,
        *,
        assigned_by: str = "local-toolpack-install",
    ) -> list[EngagementToolAssignment]:
        """Grant a newly loaded local pack to existing engagements by default."""

        if (
            installation.trust != ToolPackTrust.LOCAL_TRUSTED
            or installation.status != ToolPackInstallationStatus.READY
        ):
            return []
        created: list[EngagementToolAssignment] = []
        for engagement in self.store.list_entities(Engagement, limit=1_000):
            created.extend(
                self.enable_default_local_packs(
                    engagement.id,
                    assigned_by=assigned_by,
                    installations=[installation],
                )
            )
        return created

    def enable_default_local_packs(
        self,
        engagement_id: str,
        *,
        assigned_by: str = "local-toolpack-install",
        installations: list[ToolPackInstallation] | None = None,
    ) -> list[EngagementToolAssignment]:
        """Create missing assignments for ready, locally trusted packs.

        Existing assignments stay untouched so explicit disablement or a narrower
        capability selection remains authoritative.
        """

        self.store.get(Engagement, engagement_id)
        candidates = installations or self.store.list_entities(
            ToolPackInstallation, limit=1_000
        )
        existing_digests = {
            assignment.manifest_digest
            for assignment in self.store.list_entities(
                EngagementToolAssignment,
                engagement_id=engagement_id,
                limit=1_000,
            )
        }
        created: list[EngagementToolAssignment] = []
        for installation in candidates:
            if (
                installation.trust != ToolPackTrust.LOCAL_TRUSTED
                or installation.status != ToolPackInstallationStatus.READY
                or installation.manifest_digest in existing_digests
            ):
                continue
            manifest = self.manifests.get(installation.manifest_digest)
            tool_names = self.normalize_assignment(
                installation.manifest_digest,
                [tool.name for tool in manifest.tools],
            )
            assignment = self.store.create(
                EngagementToolAssignment(
                    id=str(
                        uuid5(
                            NAMESPACE_URL,
                            "nebula:tool-assignment:"
                            f"{engagement_id}:{installation.manifest_digest}",
                        )
                    ),
                    engagement_id=engagement_id,
                    manifest_digest=installation.manifest_digest,
                    allowed_tool_names=tool_names,
                    enabled=True,
                    assigned_by=assigned_by,
                )
            )
            created.append(assignment)
            existing_digests.add(installation.manifest_digest)
        return created

    async def verify(self, installation_id: str) -> ToolPackInstallation:
        progress = self._begin_operation("verify")
        try:
            installation = self.store.get(ToolPackInstallation, installation_id)
            progress.bind_installation(installation)
            progress.emit("verifying")
            verified = await self._installer(
                installation.runtime_profile_id, progress=progress
            ).verify(installation.id)
            if verified.interface_catalog_digest or verified.interface_catalog_path:
                if not (
                    verified.interface_catalog_digest
                    and verified.interface_catalog_path
                ):
                    raise ToolPlatformError(
                        "installed interface catalog metadata is incomplete"
                    )
                load_interface_catalog_file(
                    Path(verified.interface_catalog_path),
                    verified.interface_catalog_digest,
                )
            progress.bind_installation(verified)
            progress.emit("ready", result_status=verified.status)
            return verified
        except Exception as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_012",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tool_platform",
            )
            progress.emit("failed", result_status=ToolPackInstallationStatus.FAILED)
            if isinstance(exc, ToolPlatformError):
                raise
            raise ToolPlatformError(str(exc)) from exc

    async def update(self, installation_id: str) -> ToolPackInstallation:
        progress = self._begin_operation("update")
        try:
            installation = self.store.get(ToolPackInstallation, installation_id)
            progress.bind_installation(installation)
            if installation.source.startswith("local-"):
                raise ToolPlatformError(
                    "local developer packs must be updated explicitly"
                )
            loaded = await self.catalog_client.fetch()
            candidates = [
                entry
                for entry in loaded.catalog.entries
                if entry.publisher == installation.publisher
                and entry.name == installation.name
                and Version(entry.version) > Version(installation.version)
            ]
            if not candidates:
                raise ToolPlatformError(
                    "no newer signed tool-pack version is available"
                )
            selected = max(candidates, key=lambda entry: Version(entry.version))
            return await self._install_catalog_operation(
                f"{selected.publisher}/{selected.name}@{selected.version}",
                runtime_profile_id=installation.runtime_profile_id,
                version=selected.version,
                progress=progress,
            )
        except Exception as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_013",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tool_platform",
            )
            progress.emit("failed", result_status=ToolPackInstallationStatus.FAILED)
            if isinstance(exc, ToolPlatformError):
                raise
            raise ToolPlatformError(str(exc)) from exc

    def disable(self, installation_id: str) -> ToolPackInstallation:
        progress = self._begin_operation("disable")
        try:
            installation = self.store.get(ToolPackInstallation, installation_id)
            progress.bind_installation(installation)
            if installation.status != ToolPackInstallationStatus.DISABLED:
                installation = self.store.update(
                    ToolPackInstallation,
                    installation.id,
                    {"status": ToolPackInstallationStatus.DISABLED},
                    expected_revision=installation.revision,
                )
            progress.bind_installation(installation)
            progress.emit("ready", result_status=installation.status)
            return installation
        except Exception as caught_error:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_014",
                "A handled toolbox operation raised an exception.",
                caught_error,
                stage="tool_platform",
            )
            progress.emit("failed", result_status=ToolPackInstallationStatus.FAILED)
            raise

    async def verify_runner(self, profile_id: str) -> StoredRunnerProfile:
        stored = self.store.get(StoredRunnerProfile, profile_id)
        runner = self._runner(stored)
        healthy, detail = await runner.available()
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

    def mission_components(
        self, run: AgentRun, provider: ModelProvider
    ) -> MissionComponents:
        selected = run.metadata.get("tool_names")
        if not isinstance(selected, list) or not selected:
            raise MissionConfigurationError("tool mission has no selected tools")
        selected_oci = run.metadata.get("oci_tool_names", selected)
        if not isinstance(selected_oci, list):
            raise MissionConfigurationError("tool mission OCI lock is malformed")
        if selected_oci and not self.execution_enabled:
            raise MissionConfigurationError(
                "executable OCI tool missions remain release-gated until the "
                "runner-isolation acceptance suite passes"
            )
        try:
            mcp_profiles = tuple(
                McpServerProfile.model_validate(item)
                for item in run.runtime_snapshot.get("mcp_snapshot", [])
            )
        except ValueError as exc:
            raise MissionConfigurationError("frozen MCP snapshot is invalid") from exc
        engagement = self.store.get(Engagement, run.engagement_id)
        if not engagement.scope_policy_id:
            raise MissionConfigurationError("tool mission has no scope policy")
        scope = self.store.get(ScopePolicy, engagement.scope_policy_id)
        assignments = [
            item
            for item in self.store.list_entities(EngagementToolAssignment, limit=1_000)
            if item.engagement_id == engagement.id and item.enabled
        ]
        digest_by_tool = {
            name: item.manifest_digest
            for item in assignments
            for name in item.allowed_tool_names
        }
        if any(name not in digest_by_tool for name in selected_oci):
            raise MissionConfigurationError("tool assignment changed before execution")
        digests = sorted({digest_by_tool[name] for name in selected_oci})
        if digests != sorted(run.tool_pack_digests):
            raise MissionConfigurationError("tool-pack lock changed before execution")
        all_installations = self.store.list_entities(ToolPackInstallation, limit=1_000)
        installations = [
            item
            for item in all_installations
            if item.manifest_digest in digests
            and item.status == ToolPackInstallationStatus.READY
        ]
        if {item.manifest_digest for item in installations} != set(digests):
            raise MissionConfigurationError("a locked tool pack is no longer ready")
        interface_catalogs = [
            load_interface_catalog_file(
                Path(item.interface_catalog_path), item.interface_catalog_digest
            )
            for item in installations
            if item.interface_catalog_path and item.interface_catalog_digest
        ]
        interface_digests = {catalog.digest for catalog in interface_catalogs}
        if sorted(interface_digests) != sorted(run.tool_interface_catalog_digests):
            raise MissionConfigurationError(
                "Toolbox interface-catalog lock changed before execution"
            )
        if len(interface_digests) > 1:
            raise MissionConfigurationError(
                "one mission cannot span different Toolbox interface catalogs"
            )
        interface_catalog = interface_catalogs[0] if interface_catalogs else None
        registry = ToolRegistry()
        runner: Any = AnalysisOnlyRunner()
        profile: StoredRunnerProfile | None = None
        if installations:
            runtime_ids = {item.runtime_profile_id for item in installations}
            if len(runtime_ids) != 1:
                raise MissionConfigurationError("one mission cannot span local runners")
            profile = self.store.get(StoredRunnerProfile, runtime_ids.pop())
            if not profile.enabled or not profile.healthy:
                raise MissionConfigurationError(
                    "selected runner is not verified healthy"
                )
            runner = self._runner(profile)
            runner_platform = cast(
                Literal["linux/amd64", "linux/arm64"], profile.platform
            )
            parser_executor = SandboxParserExecutor(
                runner=runner, parser_root=self.parser_root
            )
            registry_all = build_tool_registry(
                installations,
                platform=runner_platform,
                manifests=self.manifests,
                parser_executor=parser_executor,
            )
            for name in selected_oci:
                registry.register(registry_all.get(name))
        if mcp_profiles:
            if self.mcp_service is None:
                raise MissionConfigurationError(
                    "Core MCP execution service is unavailable"
                )
            try:
                for plugin in build_mcp_tool_plugins(self.mcp_service, mcp_profiles):
                    registry.register(plugin)
            except Exception as exc:
                raise MissionConfigurationError(str(exc)) from exc
        register_artifact_retrieval_tools(
            registry,
            output_service=ToolOutputService(self.store, self.artifact_store),
        )
        specs = {spec.name: spec for spec in registry.specs()}
        missing_specs = sorted(set(selected) - specs.keys())
        if missing_specs:
            raise MissionConfigurationError(
                f"frozen mission tools are unavailable: {missing_specs}"
            )
        action_specs = {name: specs[name] for name in selected}
        network_tools = sorted(
            spec.name for spec in specs.values() if spec.network_access
        )
        embedded_helper = next(
            (
                spec.image
                for spec in specs.values()
                if spec.network_access
                and spec.name.startswith("environment.")
                and spec.image is not None
            ),
            None,
        )
        if profile is not None:
            egress_helper_image = profile.egress_helper_image or embedded_helper
            if network_tools and not egress_helper_image:
                raise MissionConfigurationError(
                    "selected network tools require a certified digest-pinned "
                    f"egress helper: {network_tools}"
                )
            runner = self._runner(profile, egress_helper_image=egress_helper_image)
        workspace = self.workspace_for(engagement.id)
        broker = ToolBroker(
            registry=registry,
            policy_engine=PolicyEngine(),
            runner=runner,
            ledger=StoreToolLedger(self.store),
            workspace_resolver=lambda engagement_id: self.workspace_for(engagement_id),
            evidence_recorder=StoreToolEvidenceRecorder(
                self.store, self.artifact_store
            ),
        )
        specialists = {}
        for role in {role_for_tool(name) for name in selected}:
            role_specs = {
                name: spec
                for name, spec in specs.items()
                if spec.budget_class == "artifact_query"
                or (name in action_specs and role_for_tool(name) == role)
            }
            specialists[role] = BrokeredToolSpecialist(
                provider,
                role=role,
                broker=broker,
                scope=scope,
                workspace=workspace,
                specs=role_specs,
                model=run.supervisor_model,
                max_output_tokens=min(2_048, run.budget.max_tokens or 2_048),
                interface_catalog=interface_catalog,
            )
        return MissionComponents(
            supervisor=ToolMissionSupervisor(action_specs),
            specialists=specialists,
            context={
                "tool_names": selected,
                "scope_summary": self._scope_summary(scope),
                "pack_digests": digests,
                "interface_catalog_digest": (
                    interface_catalog.digest if interface_catalog is not None else None
                ),
            },
        )

    def chat_components(
        self,
        *,
        engagement_id: str,
        turn_id: str,
        provider: ModelProvider,
        model: str,
        mcp_profiles: tuple[McpServerProfile, ...] = (),
        include_oci: bool = True,
        allow_empty: bool = False,
        frozen_tool_names: tuple[str, ...] | None = None,
        frozen_pack_digests: tuple[str, ...] | None = None,
    ) -> ChatToolComponents:
        """Resolve OCI and frozen MCP tools into the same brokered runtime.

        Harness sessions pass immutable pack/tool snapshots on reconnect so an
        assignment change cannot silently alter an already-open agent runtime.
        """

        del turn_id, provider, model
        if (frozen_tool_names is None) != (frozen_pack_digests is None):
            raise ToolPlatformError(
                "frozen Toolbox tool names and pack digests must be supplied together"
            )
        if frozen_pack_digests is not None and not include_oci:
            raise ToolPlatformError("frozen Toolbox snapshots require OCI tools")
        engagement = self.store.get(Engagement, engagement_id)
        if not engagement.scope_policy_id:
            if not allow_empty:
                raise ToolPlatformError("engagement has no scope policy")
            scope = ScopePolicy(
                id=f"scope:{engagement.id}", engagement_id=engagement.id
            )
        else:
            scope = self.store.get(ScopePolicy, engagement.scope_policy_id)

        assignments = (
            [
                item
                for item in self.store.list_entities(
                    EngagementToolAssignment,
                    engagement_id=engagement_id,
                    limit=1_000,
                )
                if item.enabled
            ]
            if include_oci and frozen_pack_digests is None
            else []
        )
        ready_installations = [
            item
            for item in self.store.list_entities(ToolPackInstallation, limit=1_000)
            if item.status == ToolPackInstallationStatus.READY
        ]
        ready_by_digest = {item.manifest_digest: item for item in ready_installations}
        ready_assignments = [
            item for item in assignments if item.manifest_digest in ready_by_digest
        ]
        if assignments and not ready_assignments:
            raise ToolPlatformError("an assigned Toolbox pack is unavailable")
        if frozen_pack_digests is not None:
            missing = [
                digest
                for digest in frozen_pack_digests
                if digest not in ready_by_digest
            ]
            if missing:
                raise ToolPlatformError(
                    "a frozen Toolbox pack is unavailable: " + ", ".join(missing)
                )
            selected_oci = list(dict.fromkeys(frozen_tool_names or ()))
            digests = list(dict.fromkeys(frozen_pack_digests))
            installations = [ready_by_digest[digest] for digest in digests]
        else:
            selected_oci = list(
                dict.fromkeys(
                    name
                    for item in ready_assignments
                    for name in item.allowed_tool_names
                )
            )
            digests = sorted({item.manifest_digest for item in ready_assignments})
            installations = [ready_by_digest[digest] for digest in digests]
        if selected_oci and not self.execution_enabled:
            raise ToolPlatformError("Toolbox OCI execution is disabled in this Core")
        if not selected_oci and not mcp_profiles and not allow_empty:
            raise ToolPlatformError(
                "engagement has no enabled Toolbox assignment or selected MCP server"
            )
        interface_catalogs_by_manifest: dict[str, ToolInterfaceCatalog] = {}
        for installation in installations:
            if bool(installation.interface_catalog_path) != bool(
                installation.interface_catalog_digest
            ):
                raise ToolPlatformError("assigned interface catalog lock is incomplete")
            if installation.interface_catalog_path:
                assert installation.interface_catalog_digest is not None
                interface_catalogs_by_manifest[installation.manifest_digest] = (
                    load_interface_catalog_file(
                        Path(installation.interface_catalog_path),
                        installation.interface_catalog_digest,
                    )
                )
        interface_digests = sorted(
            {catalog.digest for catalog in interface_catalogs_by_manifest.values()}
        )
        registry = ToolRegistry()
        runner: Any = AnalysisOnlyRunner()
        runner_profile: StoredRunnerProfile | None = None
        if installations:
            runtime_ids = {item.runtime_profile_id for item in installations}
            if len(runtime_ids) != 1:
                raise ToolPlatformError(
                    "chat Toolbox assignments must use one local runner"
                )
            runner_profile = self.store.get(StoredRunnerProfile, runtime_ids.pop())
            if not runner_profile.enabled or not runner_profile.healthy:
                raise ToolPlatformError("selected Toolbox runner is unavailable")
            runner_platform = cast(
                Literal["linux/amd64", "linux/arm64"], runner_profile.platform
            )
            parser_executor = SandboxParserExecutor(
                runner=self._runner(runner_profile), parser_root=self.parser_root
            )
            registry_all = build_tool_registry(
                installations,
                platform=runner_platform,
                manifests=self.manifests,
                parser_executor=parser_executor,
            )
            for name in selected_oci:
                try:
                    registry.register(registry_all.get(name))
                except UnknownTool as exc:
                    raise ToolPlatformError(
                        f"frozen Toolbox tool {name!r} is unavailable"
                    ) from exc
        if mcp_profiles:
            if self.mcp_service is None:
                raise ToolPlatformError("Core MCP execution service is unavailable")
            try:
                for plugin in build_mcp_tool_plugins(self.mcp_service, mcp_profiles):
                    registry.register(plugin)
            except Exception as exc:
                raise ToolPlatformError(str(exc)) from exc
        register_artifact_retrieval_tools(
            registry,
            output_service=ToolOutputService(self.store, self.artifact_store),
        )
        if interface_catalogs_by_manifest:
            catalogs = tuple(interface_catalogs_by_manifest.values())

            async def select_interface(invocation: Any) -> dict[str, Any]:
                return select_command_interface(catalogs, invocation.arguments)

            registry.register(
                InvocationAnalysisTool(
                    ToolSpec(
                        name=COMMAND_SELECTOR_NAME,
                        description=(
                            "Select a compact exact command interface from the signed "
                            "Toolbox catalog before calling environment.run_*. This "
                            "does not execute the command."
                        ),
                        input_schema=COMMAND_SELECTOR_INPUT_SCHEMA,
                        output_schema={
                            "type": "object",
                            "additionalProperties": True,
                        },
                        risk_class=RiskClass.LOCAL_READ,
                        budget_class="artifact_query",
                    ),
                    select_interface,
                )
            )
        specs = {spec.name: spec for spec in registry.specs()}
        network_tools = sorted(
            spec.name for spec in specs.values() if spec.network_access
        )
        embedded_helper = next(
            (
                spec.image
                for spec in specs.values()
                if spec.network_access
                and spec.name.startswith("environment.")
                and spec.image is not None
            ),
            None,
        )
        if runner_profile is not None:
            egress_helper_image = runner_profile.egress_helper_image or embedded_helper
            if network_tools and not egress_helper_image:
                raise ToolPlatformError(
                    "assigned network tools require a certified digest-pinned egress helper"
                )
            runner = self._runner(
                runner_profile, egress_helper_image=egress_helper_image
            )
        broker = ToolBroker(
            registry=registry,
            policy_engine=PolicyEngine(),
            runner=runner,
            ledger=StoreToolLedger(self.store),
            workspace_resolver=lambda owner_engagement_id: self.workspace_for(
                owner_engagement_id
            ),
            evidence_recorder=StoreToolEvidenceRecorder(
                self.store, self.artifact_store
            ),
        )
        return ChatToolComponents(
            broker=broker,
            scope=scope,
            workspace=self.workspace_for(engagement_id),
            specs=specs,
            tool_pack_digests=tuple(digests),
            interface_catalog_digests=tuple(interface_digests),
            interface_catalogs_by_manifest=interface_catalogs_by_manifest,
        )

    def workspace_for(self, engagement_id: str) -> Path:
        component = hashlib.sha256(engagement_id.encode("utf-8")).hexdigest()
        workspace = self.workspace_root / component
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        # OCI workers run as an unmapped non-root UID. The engagement directory
        # is writable to that UID while its 0700 parent keeps it invisible to
        # every other host user.
        workspace.chmod(0o777)
        return workspace

    async def resolve_human_terminal_runtime(
        self, engagement_id: str
    ) -> HumanTerminalRuntimeResolution:
        """Resolve the fixed human-only Kali environment without a Toolbox grant."""

        profile = self.resolve_human_terminal_profile(engagement_id)
        key = (profile.id, profile.revision)
        image = self._human_terminal_images.get(key)
        if image is None:
            lock = self._human_terminal_image_locks.setdefault(key, asyncio.Lock())
            async with lock:
                image = self._human_terminal_images.get(key)
                if image is None:
                    runner = self._runner(profile)
                    try:
                        image = await ContainerImagePreparer(
                            runner=runner,
                            platform=cast(
                                Literal["linux/amd64", "linux/arm64"],
                                profile.platform,
                            ),
                            source_reference=self.human_terminal_source_image,
                            expected_repository=self.human_terminal_repository,
                        ).prepare()
                    except (SandboxError, ValueError) as exc:
                        record_caught_exception(
                            "toolbox",
                            "toolbox.tool_platform.caught_failure_015",
                            "A handled toolbox operation raised an exception.",
                            exc,
                            stage="tool_platform",
                        )
                        raise ToolPlatformError(str(exc)) from exc
                    self._persist_human_terminal_image_metadata(profile, image)
                    self._human_terminal_images[key] = image
        return HumanTerminalRuntimeResolution(
            profile=profile,
            runner=self._runner(profile),
            workspace=self.workspace_for(engagement_id),
            image=image,
        )

    def _persist_human_terminal_image_metadata(
        self,
        profile: StoredRunnerProfile,
        image: PreparedContainerImage,
    ) -> None:
        payload = {
            "schema": HUMAN_TERMINAL_IMAGE_METADATA_SCHEMA,
            "verified_at": utc_now().isoformat(),
            "runner_profile_id": profile.id,
            "runner_profile_revision": profile.revision,
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
            "registry_refreshed": image.refreshed,
        }
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".human-terminal-image-",
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
            temporary.replace(self.human_terminal_image_metadata_path)
        except OSError as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_016",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tool_platform",
            )
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ToolPlatformError(
                "verified human-workstation image metadata could not be persisted"
            ) from exc

    def last_human_terminal_security_inventory(
        self,
    ) -> tuple[str, str, tuple[str, ...]] | None:
        """Return the last verified image catalog without preparing an image."""

        try:
            payload = json.loads(
                self.human_terminal_image_metadata_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as caught_error:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_017",
                "A handled toolbox operation raised an exception.",
                caught_error,
                stage="tool_platform",
            )
            return None
        image_digest = (
            payload.get("image_digest") if isinstance(payload, dict) else None
        )
        manifest_sha256 = (
            payload.get("security_tool_manifest_sha256")
            if isinstance(payload, dict)
            else None
        )
        tools = payload.get("security_tools") if isinstance(payload, dict) else None
        if (
            payload.get("schema") != HUMAN_TERMINAL_IMAGE_METADATA_SCHEMA
            or not isinstance(image_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None
            or not isinstance(manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
            or not isinstance(tools, list)
            or not tools
            or tools != sorted(set(tools))
            or any(
                not isinstance(tool, str) or TOOL_NAME_PATTERN.fullmatch(tool) is None
                for tool in tools
            )
        ):
            return None
        return image_digest, manifest_sha256, tuple(tools)

    def resolve_human_terminal_profile(self, engagement_id: str) -> StoredRunnerProfile:
        """Select the one configured runner eligible for the human terminal."""

        if not self.execution_enabled:
            raise ToolPlatformError("operator execution is disabled in this Core")
        self.store.get(Engagement, engagement_id)
        profiles = [
            profile
            for profile in self.store.list_entities(StoredRunnerProfile, limit=1_000)
            if profile.enabled and profile.healthy
        ]
        local = next((profile for profile in profiles if profile.id == "local"), None)
        if local is not None:
            profile = local
        elif len(profiles) == 1:
            profile = profiles[0]
        elif not profiles:
            raise ToolPlatformError(
                "human terminal requires an enabled, verified healthy runner profile"
            )
        else:
            raise ToolPlatformError(
                "human terminal runner is ambiguous; name the preferred runner profile 'local'"
            )
        return profile

    async def cleanup_operator_terminals(self) -> None:
        """Best-effort cleanup for terminal containers orphaned by Core exit."""

        profiles = self.store.list_entities(StoredRunnerProfile, limit=1_000)
        seen: set[tuple[str, str | None, str, str, str | None]] = set()
        runners: list[ContainerSandboxRunner] = []
        for profile in profiles:
            if not profile.enabled:
                LOGGER.warning(
                    "Skipped orphan terminal cleanup for runner profile %s: "
                    "profile is disabled",
                    profile.id,
                )
                continue
            if not profile.healthy or profile.last_health_at is None:
                LOGGER.warning(
                    "Skipped orphan terminal cleanup for runner profile %s: "
                    "profile is not verified healthy",
                    profile.id,
                )
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
                        "Skipped orphan terminal cleanup for runner profile %s: %s",
                        profile.id,
                        detail,
                    )
                    continue
                runners.append(runner)
                seen.add(identity)
            except (ValueError, ToolPlatformError) as exc:
                record_caught_exception(
                    "toolbox",
                    "toolbox.tool_platform.caught_failure_018",
                    "A handled toolbox operation raised an exception.",
                    exc,
                    stage="tool_platform",
                )
                LOGGER.warning(
                    "Skipped orphan terminal cleanup for runner profile %s: %s",
                    profile.id,
                    str(exc)[:1_000],
                )
                continue
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
        """Resolve a signed runtime through the engagement's shell grant."""

        if not self.execution_enabled:
            raise ToolPlatformError("operator execution is disabled in this Core")
        self.store.get(Engagement, engagement_id)
        capability = (
            "environment.shell_network" if network else "environment.shell_local"
        )
        assignments = [
            item
            for item in self.store.list_entities(
                EngagementToolAssignment,
                engagement_id=engagement_id,
                limit=1_000,
            )
            if self._assignment_allows_operator_shell(item, capability)
        ]
        if not assignments:
            raise ToolPlatformError(f"engagement is not assigned {capability}")
        candidates: list[
            tuple[
                ToolPackInstallation,
                ToolPackManifestV1,
                ToolPackOperatorRuntime,
            ]
        ] = []
        normalized = language.casefold()
        installations = self.store.list_entities(ToolPackInstallation, limit=1_000)
        for assignment in assignments:
            for installation in installations:
                if (
                    installation.manifest_digest != assignment.manifest_digest
                    or installation.status != ToolPackInstallationStatus.READY
                ):
                    continue
                manifest = self.manifests.get(installation.manifest_digest)
                runtime = next(
                    (
                        item
                        for item in manifest.operator_runtimes
                        if normalized in item.aliases
                    ),
                    None,
                )
                if runtime is not None:
                    candidates.append((installation, manifest, runtime))
        if not candidates:
            raise ToolPlatformError(
                f"no ready assigned Toolbox declares runtime {language!r}"
            )
        if len(candidates) != 1:
            raise ToolPlatformError(
                f"runtime {language!r} is ambiguous across assigned environments"
            )
        installation, manifest, runtime = candidates[0]
        if (
            installation.trust == ToolPackTrust.LOCAL_UNSIGNED
            and not self.developer_mode
        ):
            raise ToolPlatformError(
                "release mode requires a signed, digest-pinned Toolbox runtime"
            )
        profile = self.store.get(StoredRunnerProfile, installation.runtime_profile_id)
        if not profile.enabled or not profile.healthy:
            raise ToolPlatformError("selected runner is not verified healthy")
        image = manifest.image_for(runtime.image, profile.platform).image
        runner = self._runner(
            profile,
            egress_helper_image=(
                (profile.egress_helper_image or image) if network else None
            ),
        )
        return OperatorRuntimeResolution(
            canonical_language=runtime.language,
            runtime=runtime,
            installation=installation,
            manifest=manifest,
            profile=profile,
            image=image,
            runner=runner,
            workspace=self.workspace_for(engagement_id),
            trusted=installation.trust != ToolPackTrust.LOCAL_UNSIGNED,
        )

    def _begin_operation(self, operation: ToolPackOperation) -> _OperationProgress:
        progress = _OperationProgress(self.events, operation)
        progress.emit("pending")
        return progress

    def _installer(
        self,
        runtime_profile_id: str,
        *,
        progress: _OperationProgress | None = None,
    ) -> ToolPackInstaller:
        stored = self.store.get(StoredRunnerProfile, runtime_profile_id)
        if not stored.enabled:
            raise ToolPlatformError("selected runner profile is disabled")
        runner = self._runner(stored)
        runner_platform = cast(Literal["linux/amd64", "linux/arm64"], stored.platform)
        runtime: ToolPackRuntimeAdapter = ContainerToolPackRuntimeAdapter(
            runner=runner, platform=runner_platform
        )
        if progress is not None:
            runtime = _ProgressRuntimeAdapter(runtime, progress)
        return ToolPackInstaller(
            store=self.store,
            manifests=self.manifests,
            runtime=runtime,
            runtime_profile_id=stored.id,
            platform=runner_platform,
            verifier=self.keyring,
            parser_executor=SandboxParserExecutor(
                runner=runner, parser_root=self.parser_root
            ),
            developer_mode=self.developer_mode,
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
        helper_image = egress_helper_image or stored.egress_helper_image
        egress = (
            ContainerEgressController(helper_image=helper_image)
            if helper_image is not None
            else NoEgressController()
        )
        return ContainerSandboxRunner(
            profile=profile,
            egress_controller=egress,
            workspace_roots=[self.workspace_root, self.parser_root],
        )

    def _installation_platform(self, installation: ToolPackInstallation) -> str:
        profile = self.store.get(StoredRunnerProfile, installation.runtime_profile_id)
        return profile.platform

    def _tool_unavailable_reason(
        self,
        installation: ToolPackInstallation,
        profile: StoredRunnerProfile | None,
        spec: Any,
    ) -> str | None:
        if not self.execution_enabled:
            return "executable tool missions are release-gated in this build"
        if installation.status != ToolPackInstallationStatus.READY:
            return f"pack is {installation.status.value}"
        if profile is None or not profile.enabled:
            return "runner profile is unavailable"
        if not profile.healthy:
            return profile.last_health_detail or "runner profile is not healthy"
        if (
            spec.network_access
            and not profile.egress_helper_image
            and not spec.name.startswith("environment.")
        ):
            return "certified egress helper is not configured"
        return None

    @staticmethod
    def _scope_summary(scope: ScopePolicy) -> str:
        return json.dumps(
            {
                "cidrs": scope.allowed_cidrs,
                "domains": scope.allowed_domains,
                "urls": scope.allowed_urls,
                "ports": scope.allowed_ports,
                "not_before": (
                    scope.not_before.isoformat() if scope.not_before else None
                ),
                "not_after": scope.not_after.isoformat() if scope.not_after else None,
                "prohibited_actions": scope.prohibited_actions,
            },
            sort_keys=True,
        )

    @staticmethod
    def _catalog_payload(entry: ToolCatalogEntry) -> dict[str, Any]:
        return {
            "id": f"{entry.publisher}/{entry.name}@{entry.version}",
            **entry.model_dump(mode="json"),
            "signed": True,
        }

    @staticmethod
    def _select_entry(
        entries: list[ToolCatalogEntry], catalog_id: str, version: str | None
    ) -> ToolCatalogEntry:
        candidates = [
            entry
            for entry in entries
            if catalog_id
            in {
                f"{entry.publisher}/{entry.name}",
                f"{entry.publisher}/{entry.name}@{entry.version}",
            }
            and (version is None or entry.version == version)
        ]
        if not candidates:
            raise ToolPlatformError("tool pack is not present in the signed catalog")
        return max(candidates, key=lambda entry: Version(entry.version))

    async def _fetch_manifest(
        self, entry: ToolCatalogEntry
    ) -> tuple[ToolPackManifestV1, SignatureEnvelope]:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0), follow_redirects=False
            ) as client:
                manifest_response, signature_response = await asyncio.gather(
                    fetch_bounded_https(
                        client, entry.manifest_url, MAX_REMOTE_MANIFEST_BYTES
                    ),
                    fetch_bounded_https(client, entry.signature_url, 100_000),
                )
        except Exception as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_019",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tool_platform",
            )
            raise ToolPlatformError("tool-pack download failed") from exc
        try:
            manifest = parse_manifest_json(manifest_response)
            signature = SignatureEnvelope.model_validate_json(signature_response)
            self.keyring.verify_publisher(
                canonical_manifest_json(manifest),
                signature,
                manifest.metadata.publisher,
            )
        except Exception as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tool_platform.caught_failure_020",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tool_platform",
            )
            raise ToolPlatformError("downloaded tool pack failed verification") from exc
        return manifest, signature


def default_tool_platform(
    *,
    store: NebulaStore,
    artifact_store: ArtifactStore,
    data_root: Path,
) -> ToolPlatform:
    key_path = os.getenv("NEBULA_TOOL_PACK_PUBLIC_KEYS")
    human_terminal_source = os.getenv("NEBULA_HUMAN_TERMINAL_SOURCE_IMAGE")
    if human_terminal_source is None:
        human_terminal_source = DEFAULT_HUMAN_TERMINAL_SOURCE_IMAGE
    elif not re.fullmatch(
        re.escape(DEFAULT_HUMAN_TERMINAL_REPOSITORY) + r"@sha256:[0-9a-f]{64}",
        human_terminal_source,
    ):
        raise ToolPlatformError(
            "NEBULA_HUMAN_TERMINAL_SOURCE_IMAGE must be a digest-pinned "
            "official human-workstation base image"
        )
    return ToolPlatform(
        store=store,
        artifact_store=artifact_store,
        data_root=data_root,
        public_keys=_trusted_public_keys(Path(key_path) if key_path else None),
        catalog_url=os.getenv("NEBULA_TOOL_CATALOG_URL", DEFAULT_CATALOG_URL),
        catalog_signature_url=os.getenv(
            "NEBULA_TOOL_CATALOG_SIGNATURE_URL",
            DEFAULT_CATALOG_SIGNATURE_URL,
        ),
        developer_mode=os.getenv("NEBULA_TOOL_DEVELOPER_MODE") == "1",
        execution_enabled=True,
        human_terminal_source_image=human_terminal_source,
    )


__all__ = [
    "DEFAULT_EVENT_RETENTION",
    "DEFAULT_HUMAN_TERMINAL_REPOSITORY",
    "DEFAULT_HUMAN_TERMINAL_SOURCE_IMAGE",
    "HUMAN_TERMINAL_IMAGE_METADATA_SCHEMA",
    "HumanTerminalRuntimeResolution",
    "MAX_EVENT_RETENTION",
    "OperatorRuntimeResolution",
    "ToolPackEventJournal",
    "ToolPackEventReplay",
    "ToolPlatform",
    "ToolPlatformError",
    "ToolPackProgressEvent",
    "default_tool_platform",
]
