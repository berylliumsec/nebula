"""Prepared Kali runtime, workspaces, runner health, and MCP capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Protocol, cast

from .artifacts import ArtifactStore
from .context_retrieval import DenseEncoder
from .conversation_search import ConversationSearchTool
from .knowledge_search import KnowledgeSearcher, KnowledgeSearchTool
from .diagnostics import gather_diagnostic, record_caught_exception
from .domain import (
    Approval,
    AutomationApprovalPolicy,
    AutomationProjectPolicy,
    ChatMessage,
    Engagement,
    McpServerProfile,
    RunnerIsolation,
    RunnerProfile as StoredRunnerProfile,
    ScopePolicy,
    SshEnvironment,
    utc_now,
)
from .environments import build_ssh_tool_plugins
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
from .runtime_prepared_catalog import (
    RuntimeCatalogError,
    persist_runtime_metadata,
    read_runtime_metadata,
    automation_runtime_metadata,
)
from .tool_results import ToolOutputService
from .tools import (
    InvalidToolArguments,
    StoreToolEvidenceRecorder,
    StoreToolLedger,
    ToolBroker,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
    ToolSpec,
    register_artifact_retrieval_tools,
)
from .web_search import SearchRuntime, WebSearchTool, web_search_enabled
from .working_notes import WriteNotesTool
from .settings_assistant_api import SettingsAssistantApi

if TYPE_CHECKING:
    from .automation_tools import AutomationToolComponents
    from .container_terminal import ContainerTerminalService
    from .workspace import WorkspaceService


LOGGER = logging.getLogger(__name__)

DEFAULT_KALI_SOURCE_IMAGE = "docker.io/kalilinux/kali-rolling:latest"
DEFAULT_KALI_REPOSITORY = "docker.io/kalilinux/kali-rolling"
KALI_RUNTIME_METADATA_SCHEMA = "nebula.kali-runtime/v1"

# Compatibility name used by the human-terminal surface. Both the human and
# agent runtimes are deliberately prepared from this one image.
DEFAULT_HUMAN_TERMINAL_SOURCE_IMAGE = DEFAULT_KALI_SOURCE_IMAGE


async def project_auto_approves_tools(store: NebulaStore, engagement_id: str) -> bool:
    policies = await store.list_entities(
        AutomationProjectPolicy, engagement_id=engagement_id, limit=1
    )
    return bool(
        policies and policies[0].approval_policy == AutomationApprovalPolicy.NEVER
    )


class RuntimePlatformError(RuntimeError):
    """The local Kali runtime or its runner cannot satisfy a request."""


class ToolExecutionBroker(Protocol):
    """Shared execution boundary for native, browser, and composite brokers."""

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Approval | None = None,
    ) -> ToolExecutionResult: ...


class _ControlAssistantScopeBroker:
    """Keep policy repair tools privileged without exempting ordinary tools."""

    def __init__(self, broker: ToolExecutionBroker, privileged_names: set[str]) -> None:
        self.broker = broker
        self.privileged_names = frozenset(privileged_names)

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Approval | None = None,
    ) -> ToolExecutionResult:
        effective_scope = (
            scope.model_copy(update={"bypass_permissions": True})
            if invocation.tool_name in self.privileged_names
            else scope
        )
        return await self.broker.execute(invocation, effective_scope, approval=approval)


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
    broker: ToolExecutionBroker
    scope: ScopePolicy
    workspace: Path
    specs: Mapping[str, ToolSpec]
    runtime_digest: str = ""


class _AllowedToolBroker:
    def __init__(self, broker: ToolExecutionBroker, names: frozenset[str]) -> None:
        self.broker = broker
        self.names = names

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Approval | None = None,
    ) -> ToolExecutionResult:
        if invocation.tool_name not in self.names:
            raise InvalidToolArguments(
                f"capability {invocation.tool_name!r} was not assigned to this subagent"
            )
        return await self.broker.execute(invocation, scope, approval=approval)


def restrict_tool_components(
    components: RuntimeToolComponents | AutomationToolComponents,
    names: list[str],
    *,
    required: frozenset[str] = frozenset(),
) -> RuntimeToolComponents:
    """Expose only assigned tools and enforce the same bound at execution."""

    selected = frozenset(names) | required
    missing = selected - components.specs.keys()
    if missing:
        raise InvalidToolArguments(
            "subagent tools are unavailable in this project: "
            + ", ".join(sorted(missing))
        )
    return RuntimeToolComponents(
        broker=_AllowedToolBroker(components.broker, selected),
        scope=components.scope,
        workspace=components.workspace,
        specs={
            name: spec for name, spec in components.specs.items() if name in selected
        },
        runtime_digest=components.runtime_digest,
    )


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


def conversation_search_components(
    store: NebulaStore,
    scope: ScopePolicy,
    workspace: Path,
    *,
    session_id: str,
    text_of: Callable[[ChatMessage], str],
    dense: Callable[[], DenseEncoder | None] | None = None,
) -> RuntimeToolComponents:
    """Searching one compacted conversation's archived messages.

    Offered only while older messages are served by a derived memory, so the
    model can recover their exact wording. Built from the caller's resolved
    scope and workspace with no runtime digest of its own, like the dashboard
    components above. A read-only analysis tool records no evidence.
    """

    registry = ToolRegistry()
    registry.register(
        ConversationSearchTool(store, session_id, text_of=text_of, dense=dense)
    )
    broker = ToolBroker(
        registry=registry,
        policy_engine=PolicyEngine(),
        runner=AnalysisOnlyRunner(),
        ledger=StoreToolLedger(store),
        workspace_resolver=lambda _engagement_id: workspace,
    )
    return RuntimeToolComponents(
        broker=broker,
        scope=scope,
        workspace=workspace,
        specs={spec.name: spec for spec in registry.specs()},
        runtime_digest="",
    )


def notes_components(
    store: NebulaStore, scope: ScopePolicy, workspace: Path
) -> RuntimeToolComponents:
    """``notes.write`` for any provider chat turn that has tools.

    Like the dashboard, the components are built from the caller's resolved
    scope and workspace and carry no runtime digest, so a turn recorded
    before the tool existed still resumes against the runtime it recorded.
    The tool is a trusted analysis tool: it writes one Core record and
    produces no evidence.
    """

    registry = ToolRegistry()
    registry.register(WriteNotesTool(store))
    broker = ToolBroker(
        registry=registry,
        policy_engine=PolicyEngine(),
        runner=AnalysisOnlyRunner(),
        ledger=StoreToolLedger(store),
        workspace_resolver=lambda _engagement_id: workspace,
    )
    return RuntimeToolComponents(
        broker=broker,
        scope=scope,
        workspace=workspace,
        specs={spec.name: spec for spec in registry.specs()},
        runtime_digest="",
    )


def knowledge_search_components(
    scope: ScopePolicy,
    workspace: Path,
    store: NebulaStore,
    *,
    engagement_id: str,
    searcher: KnowledgeSearcher,
    allow_local_only: bool,
) -> RuntimeToolComponents:
    """Searching the project's knowledge on demand, for one provider turn.

    ``allow_local_only`` is whether the turn's model runs on this host: a
    cloud model's searches exclude local-only sources and redact secrets.
    Built from the caller's resolved scope and workspace with no runtime
    digest of its own; a read-only analysis tool records no evidence.
    """

    registry = ToolRegistry()
    registry.register(
        KnowledgeSearchTool(engagement_id, searcher, allow_local_only=allow_local_only)
    )
    broker = ToolBroker(
        registry=registry,
        policy_engine=PolicyEngine(),
        runner=AnalysisOnlyRunner(),
        ledger=StoreToolLedger(store),
        workspace_resolver=lambda _engagement_id: workspace,
    )
    return RuntimeToolComponents(
        broker=broker,
        scope=scope,
        workspace=workspace,
        specs={spec.name: spec for spec in registry.specs()},
        runtime_digest="",
    )


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
        from .memory_budget import MemoryBudgetService
        from .memory_runtime import MemoryComputeController

        self.memory_compute = MemoryComputeController(
            MemoryBudgetService(store.database), self.data_root
        )
        self.butler_instructions_root: Path | None = None
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
        self.search_runtime = SearchRuntime(store=store, data_root=self.data_root)
        self._prepared_images: dict[tuple[str, int], PreparedContainerImage] = {}
        self._image_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self.mcp_service: McpProbeService | None = None
        self.settings_assistant_api: SettingsAssistantApi | None = None
        self.control_assistant_work: Any = None
        self.butler_memory: Any = None
        self.workspace_service: WorkspaceService | None = None
        self.container_terminal_service: ContainerTerminalService | None = None

    def bind_settings_assistant_api(self, gateway: SettingsAssistantApi) -> None:
        self.settings_assistant_api = gateway

    def bind_control_assistant_work(self, work: Any) -> None:
        self.control_assistant_work = work

    def bind_butler_memory(self, memory: Any) -> None:
        self.butler_memory = memory

    def bind_workspace_service(self, service: WorkspaceService) -> None:
        if service.store is not self.store:
            raise ValueError("workspace service must use the runtime platform store")
        self.workspace_service = service

    def bind_container_terminal_service(
        self, service: ContainerTerminalService
    ) -> None:
        if service.store is not self.store:
            raise ValueError("terminal service must use the runtime platform store")
        self.container_terminal_service = service

    def bind_mcp_service(self, service: McpProbeService) -> None:
        if service.store is not self.store:
            raise ValueError("MCP service must use the runtime platform store")
        self.mcp_service = service

    async def chat_components(
        self,
        *,
        engagement_id: str,
        turn_id: str,
        provider: ModelProvider | None,
        model: str,
        mcp_profiles: tuple[McpServerProfile, ...] = (),
        ssh_environments: tuple[SshEnvironment, ...] = (),
        include_oci: bool = False,
        allow_empty: bool = False,
        settings_assistant: bool = False,
        operator_message: str = "",
        background_case_id: str | None = None,
        **_obsolete_snapshot: Any,
    ) -> RuntimeToolComponents:
        """Build MCP and SSH-environment components; OCI commands are fixed elsewhere."""

        operator_turn_id = turn_id
        del model
        if include_oci:
            raise RuntimePlatformError(
                "OCI capabilities are available only through run_command"
            )
        engagement = await self.store.get(Engagement, engagement_id)
        scope = (
            (await self.store.get(ScopePolicy, engagement.scope_policy_id))
            if engagement.scope_policy_id
            else ScopePolicy(id=f"scope:{engagement.id}", engagement_id=engagement.id)
        )
        search_enabled = web_search_enabled(scope)
        if (
            not mcp_profiles
            and not ssh_environments
            and not search_enabled
            and not allow_empty
        ):
            raise RuntimePlatformError("no tool source was selected")
        registry = ToolRegistry()
        privileged_names: set[str] = set()
        if settings_assistant:
            from .butler_capabilities import ButlerCapabilities

            for plugin in (
                await ButlerCapabilities.create(
                    self.store,
                    self.artifact_store,
                    engagement_id,
                    settings_api=self.settings_assistant_api,
                    work=self.control_assistant_work,
                    memory=self.butler_memory,
                    operator_message=operator_message,
                    operator_turn_id=operator_turn_id,
                    background_case_id=background_case_id,
                    instructions_root=getattr(self, "butler_instructions_root", None),
                    background_runtime_local=bool(provider and provider.config.local),
                    notebooks=self.mcp_service.notebook_tools.notebooks
                    if self.mcp_service is not None
                    and self.mcp_service.notebook_tools is not None
                    else None,
                    workspace=self.workspace_service,
                    container_terminals=self.container_terminal_service,
                )
            ).plugins():
                registry.register(plugin)
                privileged_names.add(plugin.spec.name)
        if search_enabled and background_case_id is None:
            registry.register(WebSearchTool(self.search_runtime, scope))
        if mcp_profiles:
            if self.mcp_service is None:
                raise RuntimePlatformError("Core MCP execution service is unavailable")
            try:
                for plugin in await build_mcp_tool_plugins(
                    self.mcp_service,
                    mcp_profiles,
                    engagement_id=engagement_id,
                    bypass_permissions=scope.bypass_permissions,
                ):
                    registry.register(plugin)
            except Exception as exc:
                raise RuntimePlatformError(str(exc)) from exc
        for plugin in build_ssh_tool_plugins(
            ssh_environments, memory_compute=self.memory_compute
        ):
            registry.register(plugin)
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
            auto_approve=(await project_auto_approves_tools(self.store, engagement_id)),
        )
        return RuntimeToolComponents(
            broker=_ControlAssistantScopeBroker(broker, privileged_names)
            if settings_assistant
            else broker,
            # Only product-control tools bypass project policy. General tools
            # retain the saved scope and their ordinary approval requirements.
            scope=scope,
            workspace=(await self.workspace_for(engagement_id)),
            specs={spec.name: spec for spec in registry.specs()},
        )

    async def workspace_for(self, engagement_id: str) -> Path:
        engagement = await self.store.get(Engagement, engagement_id)
        if engagement.workspace_path:
            workspace = (
                Path(engagement.workspace_path).expanduser().resolve(strict=True)
            )
            if not workspace.is_dir():
                raise RuntimePlatformError(
                    "configured project workspace is not a directory"
                )
            return workspace
        component = hashlib.sha256(engagement_id.encode("utf-8")).hexdigest()
        workspace = self.workspace_root / component
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The OCI worker uses a fixed unmapped non-root UID. The 0700 parent
        # keeps engagement workspaces private from other host users.
        workspace.chmod(0o777)
        return workspace

    async def verify_runner(self, profile_id: str) -> StoredRunnerProfile:
        stored = await self.store.get(StoredRunnerProfile, profile_id)
        healthy, detail = await self._runner(stored).available()
        return await self.store.update(
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

        profile = await self.resolve_human_terminal_profile(engagement_id)
        workspace = await self.workspace_for(engagement_id)
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
            runner=self._runner(
                profile,
                egress_helper_image=image.resolved_reference,
                workspace=workspace,
            ),
            workspace=workspace,
            image=image,
        )

    def _persist_runtime_metadata(
        self, profile: StoredRunnerProfile, image: PreparedContainerImage
    ) -> None:
        try:
            persist_runtime_metadata(self.data_root, profile, image)
        except RuntimeCatalogError as error:
            raise RuntimePlatformError(str(error)) from error

    def _runtime_metadata(self) -> dict[str, Any] | None:
        return read_runtime_metadata(self.data_root)

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
        return automation_runtime_metadata(self.data_root)

    async def resolve_human_terminal_profile(
        self, engagement_id: str
    ) -> StoredRunnerProfile:
        if not self.execution_enabled:
            raise RuntimePlatformError("operator execution is disabled in this Core")
        (await self.store.get(Engagement, engagement_id))
        profiles = [
            profile
            for profile in (
                await self.store.list_entities(StoredRunnerProfile, limit=1_000)
            )
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
        # Runs at startup: an unreadable profile is skipped, not fatal.
        profiles = self.store.iter_readable_entities(StoredRunnerProfile)
        seen: set[tuple[str, str | None, str, str, str | None]] = set()
        runners: list[ContainerSandboxRunner] = []
        async for profile in profiles:
            if (
                not profile.enabled
                or not profile.healthy
                or profile.last_health_at is None
            ):
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

    async def resolve_operator_runtime(
        self, engagement_id: str, language: str, *, network: bool
    ) -> OperatorRuntimeResolution:
        if not self.execution_enabled:
            raise RuntimePlatformError("operator execution is disabled in this Core")
        (await self.store.get(Engagement, engagement_id))
        metadata = self.last_automation_runtime_metadata()
        if metadata is None:
            raise RuntimePlatformError("prepare the Kali automation runtime first")
        profile = await self.store.get(
            StoredRunnerProfile, metadata["runner_profile_id"]
        )
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
            "python": OperatorRuntimeCommand("/usr/bin/python3", ["-E", "-s", "-u"]),
        }
        runtime = runtimes.get(canonical)
        if runtime is None:
            raise RuntimePlatformError(f"Kali does not expose runtime {language!r}")
        image = metadata["image"]
        workspace = await self.workspace_for(engagement_id)
        return OperatorRuntimeResolution(
            canonical_language=canonical,
            runtime=runtime,
            profile=profile,
            image=image,
            runtime_digest=metadata["digest"],
            runner=self._runner(
                profile,
                egress_helper_image=image if network else None,
                workspace=workspace,
            ),
            workspace=workspace,
        )

    def _runner(
        self,
        stored: StoredRunnerProfile,
        *,
        egress_helper_image: str | None = None,
        workspace: Path | None = None,
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
            socket=stored.socket
            if stored.isolation == RunnerIsolation.ROOTLESS
            else None,
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
        workspace_roots = [self.workspace_root, self.parser_root]
        if workspace is not None:
            resolved_workspace = workspace.expanduser().resolve(strict=True)
            if not resolved_workspace.is_dir():
                raise RuntimePlatformError(
                    "configured project workspace is not a directory"
                )
            if resolved_workspace not in workspace_roots:
                # Admit the selected project's exact durable workspace, never its
                # parent. ContainerSandboxRunner resolves the path again at launch,
                # so a later symlink retarget fails closed.
                workspace_roots.append(resolved_workspace)
        return ContainerSandboxRunner(
            profile=profile,
            egress_controller=egress,
            workspace_roots=workspace_roots,
            memory_compute=self.memory_compute,
            memory_profile_id=stored.id,
        )


def default_runtime_platform(
    *, store: NebulaStore, artifact_store: ArtifactStore, data_root: Path
) -> RuntimePlatform:
    source = (
        os.getenv("NEBULA_KALI_SOURCE_IMAGE")
        or os.getenv("NEBULA_HUMAN_TERMINAL_SOURCE_IMAGE", DEFAULT_KALI_SOURCE_IMAGE)
        or DEFAULT_KALI_SOURCE_IMAGE
    )
    if (
        source != DEFAULT_KALI_SOURCE_IMAGE
        and re.fullmatch(
            re.escape(DEFAULT_KALI_REPOSITORY) + r"@sha256:[0-9a-f]{64}", source
        )
        is None
    ):
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
    "ToolExecutionBroker",
    "default_runtime_platform",
]
