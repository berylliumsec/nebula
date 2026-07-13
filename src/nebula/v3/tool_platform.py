"""Local tool-pack facade shared by API, CLI, and supervised missions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast
from uuid import uuid4

import httpx
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field

from .agent_tooling import BrokeredToolSpecialist, ToolMissionSupervisor, role_for_tool
from .artifacts import ArtifactStore
from .domain import (
    AgentRun,
    Engagement,
    EngagementToolAssignment,
    RunnerIsolation,
    RunnerProfile as StoredRunnerProfile,
    ScopePolicy,
    ToolPackInstallation,
    ToolPackInstallationStatus,
    utc_now,
)
from .missions import MissionComponents, MissionConfigurationError
from .policy import PolicyEngine
from .providers import ModelProvider
from .sandbox import (
    ContainerEgressController,
    ContainerRuntimeType,
    ContainerSandboxRunner,
    ContainerToolPackRuntimeAdapter,
    NoEgressController,
    RunnerIsolationMode,
    RunnerPlatform,
    RunnerProfile,
)
from .storage import NebulaStore
from .toolpack_sdk import ToolPackSDKError, read_tool_pack
from .toolpacks import (
    Ed25519Keyring,
    ImmutableManifestStore,
    SignatureEnvelope,
    ToolCatalogClient,
    ToolCatalogEntry,
    ToolPackInstaller,
    ToolPackManifestV1,
    ToolPackRuntimeAdapter,
    build_tool_registry,
    canonical_manifest_json,
    default_tool_pack_root,
    fetch_bounded_https,
    manifest_digest,
)
from .toolparsers import SandboxParserExecutor
from .tool_interfaces import (
    MAX_INTERFACE_CATALOG_BYTES,
    ToolInterfaceCatalog,
    load_interface_catalog,
    load_interface_catalog_file,
)
from .tools import (
    StoreToolEvidenceRecorder,
    StoreToolLedger,
    ToolBroker,
    ToolRegistry,
)


DEFAULT_CATALOG_URL = "https://berylliumsec.github.io/nebula/toolbox/catalog-v1.json"
DEFAULT_CATALOG_SIGNATURE_URL = (
    "https://berylliumsec.github.io/nebula/toolbox/catalog-v1.json.signature.json"
)
MAX_REMOTE_MANIFEST_BYTES = 2_000_000
MAX_LOCAL_BUNDLE_BYTES = 100_000_000
DEFAULT_EVENT_RETENTION = 256
MAX_EVENT_RETENTION = 10_000


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


def _load_public_keys(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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
        self.events = ToolPackEventJournal(event_retention)
        self.catalog_client = ToolCatalogClient(
            catalog_url=catalog_url,
            signature_url=catalog_signature_url,
            verifier=self.keyring,
            cache_path=self.tool_pack_root / "catalog-cache.json",
        )

    async def catalog(self) -> list[dict[str, Any]]:
        if not self.has_trusted_keys:
            return []
        try:
            loaded = await self.catalog_client.fetch()
        except Exception as exc:
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
                specs = manifest.tool_specs(self._installation_platform(installation))
            except Exception as exc:
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

    def validate_assignment(
        self, manifest_digest_value: str, tool_names: list[str]
    ) -> None:
        """Require every engagement grant to name a tool in the exact pack."""

        manifest = self.manifests.get(manifest_digest_value)
        declared = {tool.name for tool in manifest.tools}
        unknown = sorted(set(tool_names) - declared)
        if unknown:
            raise ToolPlatformError(
                f"tools are not declared by the selected pack: {unknown}"
            )

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
            if progress is not None:
                progress.emit("failed", result_status=ToolPackInstallationStatus.FAILED)
            for installation in reversed(created):
                try:
                    self.disable(installation.id)
                except Exception:
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
            progress.emit("failed", result_status=ToolPackInstallationStatus.FAILED)
            raise ToolPlatformError(str(exc)) from exc
        except Exception:
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
            progress.bind_installation(installation)
            progress.emit("ready", result_status=installation.status)
            return installation
        except Exception as exc:
            progress.emit("failed", result_status=ToolPackInstallationStatus.FAILED)
            if isinstance(exc, ToolPlatformError):
                raise
            raise ToolPlatformError(str(exc)) from exc

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
        except Exception:
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
        if not self.execution_enabled:
            raise MissionConfigurationError(
                "executable tool missions remain release-gated until the "
                "runner-isolation acceptance suite passes"
            )
        selected = run.metadata.get("tool_names")
        if not isinstance(selected, list) or not selected:
            raise MissionConfigurationError("tool mission has no selected tools")
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
        if any(name not in digest_by_tool for name in selected):
            raise MissionConfigurationError("tool assignment changed before execution")
        digests = sorted({digest_by_tool[name] for name in selected})
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
        runtime_ids = {item.runtime_profile_id for item in installations}
        if len(runtime_ids) != 1:
            raise MissionConfigurationError("one mission cannot span local runners")
        profile = self.store.get(StoredRunnerProfile, runtime_ids.pop())
        if not profile.enabled or not profile.healthy:
            raise MissionConfigurationError("selected runner is not verified healthy")
        runner = self._runner(profile)
        runner_platform = cast(Literal["linux/amd64", "linux/arm64"], profile.platform)
        parser_executor = SandboxParserExecutor(
            runner=runner, parser_root=self.parser_root
        )
        registry_all = build_tool_registry(
            installations,
            platform=runner_platform,
            manifests=self.manifests,
            parser_executor=parser_executor,
        )
        registry = ToolRegistry()
        for name in selected:
            registry.register(registry_all.get(name))
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
                if role_for_tool(name) == role
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
            supervisor=ToolMissionSupervisor(specs),
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

    def workspace_for(self, engagement_id: str) -> Path:
        component = hashlib.sha256(engagement_id.encode("utf-8")).hexdigest()
        workspace = self.workspace_root / component
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        # OCI workers run as an unmapped non-root UID. The engagement directory
        # is writable to that UID while its 0700 parent keeps it invisible to
        # every other host user.
        workspace.chmod(0o777)
        return workspace

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
                "not_before": scope.not_before.isoformat()
                if scope.not_before
                else None,
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
            raise ToolPlatformError("tool-pack download failed") from exc
        try:
            manifest = ToolPackManifestV1.model_validate_json(manifest_response)
            signature = SignatureEnvelope.model_validate_json(signature_response)
            self.keyring.verify_publisher(
                canonical_manifest_json(manifest),
                signature,
                manifest.metadata.publisher,
            )
        except Exception as exc:
            raise ToolPlatformError("downloaded tool pack failed verification") from exc
        return manifest, signature


def default_tool_platform(
    *,
    store: NebulaStore,
    artifact_store: ArtifactStore,
    data_root: Path,
) -> ToolPlatform:
    key_path = os.getenv("NEBULA_TOOL_PACK_PUBLIC_KEYS")
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
    )


__all__ = [
    "DEFAULT_EVENT_RETENTION",
    "MAX_EVENT_RETENTION",
    "ToolPackEventJournal",
    "ToolPackEventReplay",
    "ToolPlatform",
    "ToolPlatformError",
    "ToolPackProgressEvent",
    "default_tool_platform",
]
