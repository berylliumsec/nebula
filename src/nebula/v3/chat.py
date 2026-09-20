"""Provider-neutral, durable analyst chat for Nebula 3.

Command definitions are fixed by Core. Clients can enable that bounded runtime
but can never supply or broaden capabilities.
"""

from __future__ import annotations

from .diagnostics import (
    create_diagnostic_task,
    record_caught_exception,
    record_diagnostic,
)

import asyncio
import base64
import hashlib
import json
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import (
    PrivateAttr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .artifacts import ArtifactStore
from .browser_tools import BrowserToolPlatform, combine_tool_components
from .browser_companion_tools import attached_session, companion_components
from .application_model.tools import standalone_components
from .application_model.workflow import BROWSER_MODEL_WORKFLOW
from .browser_companion import BrowserCompanion
from .browser_engine import BrowserEngineRegistry
from .domain import BrowserSession

from .domain import (
    AgentRun,
    Approval,
    ApprovalStatus,
    Artifact,
    ChatCitation,
    ChatContentBlock,
    ChatBackend,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    ChatTurnStatus,
    ChatTokenUsage,
    CommandExecution,
    CommandExecutionStatus,
    ContextOwnerType,
    ContextSnapshot,
    ContextSnapshotStatus,
    ContextSourceReference,
    Engagement,
    KnowledgeSource,
    LibraryItem,
    McpServerProfile,
    NativeHookExecution,
    NebulaModel,
    message_is_replaced,
    ProviderProfile,
    RunBackend,
    SshEnvironment,
    ToolCallOrigin,
    ToolCall,
    ToolCallStatus,
    utc_now,
)
from .context import (
    ContextCallBudget,
    ContextCapacityError,
    ContextCompactionError,
    ContextCompactor,
    ContextSource,
    ContextStatus,
    estimate_messages,
    estimate_model_request,
    estimate_tokens,
    lexical_score,
    memory_text,
    resolve_context_limits,
)
from .model_catalog import (
    find_model_descriptor,
    route_discovery_model,
    route_limits_verified,
)
from .privacy import ProviderPrivacyViolation, validate_engagement_provider_privacy
from .environments import resolve_ssh_environments
from .mcp import McpProbeError, resolve_mcp_profiles
from .native_hooks import NativeHookError, NativeHookRunner, NativeHookSnapshot
from .operator_help import CORPUS_ID, search_operator_help
from .knowledge_index import KnowledgeIndex, KnowledgeIndexError
from .providers import (
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelToolResult,
    ProviderContextLengthError,
    StreamEventType,
    ToolCall as ModelToolCall,
    ToolChoice,
    ToolDefinition,
    provider_from_profile,
)
from .redaction import redact_text, sanitize_display_text
from .storage import ConflictError, NebulaStore, NotFoundError
from .tools import (
    ApprovalRequired,
    InvalidToolArguments,
    PolicyDenied,
    ToolInvocation,
)
from .tool_catalog import (
    CATALOG_CALL,
    CATALOG_DISCOVERY_NAMES,
    MAX_CATALOG_CALLS_PER_TURN,
    CatalogReceipt,
    ToolIndex,
    catalog_components,
    catalog_instructions,
    catalog_snapshot,
    deferrable_specs,
    discovery_calls,
    loaded_tool_names,
    on_demand_enabled,
    rank_for_request,
    unwrap_call,
)
from .tool_suggestions import (
    JevClient,
    public_suggestions,
    suggest_tools,
    suggestions_enabled,
)
from .skill_catalog import (
    SkillSelection,
    SkillSnapshot,
    discover_skills,
    native_skill_roots,
    skill_instructions,
    skill_resource_components,
    snapshot_skill,
)
from .project_instructions import (
    ProjectInstructions,
    ProjectInstructionsError,
    load_project_instructions,
    project_instructions_text,
)
from .chat_subagents import (
    SUBAGENT_CHILD_INSTRUCTIONS,
    SUBAGENT_ROUTING_INSTRUCTIONS,
    SubagentService,
    SubagentWaitPending,
    is_subagent_session,
    subagent_components,
)
from .tool_results import (
    ToolResultStatus,
    sanitize_model_history_result,
    serialize_model_result,
)

if TYPE_CHECKING:
    from .automation_tools import AutomationToolComponents, AutomationToolPlatform
    from .runtime_platform import RuntimePlatform, RuntimeToolComponents


class ChatError(RuntimeError):
    """Base class for a safe, operator-facing chat failure."""


class ChatConfigurationError(ChatError):
    """The selected provider/model cannot serve the requested chat."""


class ChatCompactionError(ChatError):
    """Required context compaction failed and the request may be retried."""


class ChatHistoryConflict(ChatError):
    """Client history diverged from the durable session transcript."""


class ChatPrivacyError(ChatError):
    """The selected provider would cross a declared local-only boundary."""


logger = logging.getLogger(__name__)


class ChatRequestMessage(NebulaModel):
    role: ChatRole
    content: str = Field(min_length=1, max_length=100_000)
    content_blocks: list[ChatContentBlock] = Field(default_factory=list, max_length=64)


class ChatContextAttachment(NebulaModel):
    source_kind: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9._-]+$")
    source_id: str | None = Field(default=None, max_length=200)
    source_label: str = Field(min_length=1, max_length=500)
    # Attachment integrity covers the exact operator-selected UTF-8 text. The
    # shared NebulaModel default trims strings, so opt this field out or Core
    # would compare the browser's hash against a silently modified value.
    text: Annotated[
        str,
        StringConstraints(strip_whitespace=False, min_length=1, max_length=20_000),
    ]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    truncated: bool = False

    @model_validator(mode="after")
    def exact_hash_matches_text(self) -> "ChatContextAttachment":
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if digest != self.sha256:
            raise ValueError("context attachment sha256 does not match its text")
        return self


def resolve_chat_model_content(
    store: NebulaStore,
    artifact_store: ArtifactStore | None,
    message: ChatRequestMessage,
    engagement_id: str | None,
) -> str | list[dict[str, Any]]:
    images = [block for block in message.content_blocks if block.type == "image"]
    if not images:
        return message.content
    if artifact_store is None or not engagement_id:
        raise ChatConfigurationError("image messages require durable artifact storage")
    parts: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
    for block in images:
        assert block.artifact_id is not None
        artifact = store.get(Artifact, block.artifact_id)
        if (
            artifact.engagement_id != engagement_id
            or artifact.metadata.get("chat_image_original") is not True
        ):
            raise ChatConfigurationError("chat image does not belong to this project")
        preview_id = block.metadata.get("preview_artifact_id")
        preview = (
            store.get(Artifact, preview_id)
            if isinstance(preview_id, str)
            else next(
                (
                    candidate
                    for candidate in store.list_entities(
                        Artifact, engagement_id=engagement_id, limit=1_000
                    )
                    if candidate.parent_artifact_id == artifact.id
                    and candidate.metadata.get("chat_image_preview") is True
                ),
                None,
            )
        )
        if (
            preview is None
            or preview.engagement_id != engagement_id
            or preview.parent_artifact_id != artifact.id
            or preview.metadata.get("chat_image_preview") is not True
            or preview.metadata.get("metadata_stripped") is not True
        ):
            raise ChatConfigurationError(
                "validated metadata-stripped chat image preview is unavailable"
            )
        data = artifact_store.read(preview)
        if (
            len(data) != preview.size
            or hashlib.sha256(data).hexdigest() != preview.sha256
        ):
            raise ChatConfigurationError(
                "Chat image preview failed integrity verification. Remove the attachment and upload it again."
            )
        parts.append(
            {
                "type": "image",
                "media_type": preview.media_type,
                "data": base64.b64encode(data).decode("ascii"),
                "alt": block.alt,
            }
        )
    return parts


class ChatCompletionRequest(NebulaModel):
    _queue_claim: tuple[str, int, str] | None = PrivateAttr(default=None)
    backend: ChatBackend = ChatBackend.PROVIDER
    provider_id: str | None = Field(default=None, min_length=1, max_length=200)
    harness_profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    harness_session_id: str | None = Field(default=None, min_length=1, max_length=200)
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=64)
    # None: every enabled SSH environment when command tools are on; a list
    # narrows the turn to those hosts (empty means Core only).
    ssh_environment_ids: list[str] | None = Field(default=None, max_length=64)
    hook_ids: list[str] = Field(default_factory=list, max_length=32)
    model: str | None = Field(default=None, max_length=500)
    engagement_id: str | None = Field(default=None, max_length=200)
    session_id: str | None = Field(default=None, max_length=200)
    goal_id: str | None = Field(default=None, max_length=200)
    skill: dict[str, str] | None = None
    messages: list[ChatRequestMessage] = Field(min_length=1, max_length=200)
    context_attachments: list[ChatContextAttachment] = Field(
        default_factory=list, max_length=20
    )
    # The exact model/route resolver applies the effective ceiling. This broad
    # transport bound prevents pathological integers without imposing one model's
    # output limit on every provider.
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    include_knowledge: bool = True
    allow_cloud_knowledge: bool = False
    tools_enabled: bool = False
    # Advertise start/wait/list/stop subagent tools. Ignored for subagent turns.
    allow_subagents: bool = False
    max_artifact_queries: int | None = Field(default=None, ge=0)
    allow_cloud_tool_results: bool = False
    # Optional vendor-native turn controls.  They are validated again against
    # the negotiated profile in HarnessRuntime.prepare_chat.
    harness_mode: str | None = Field(default=None, min_length=1, max_length=100)
    harness_reasoning_effort: str | None = Field(default=None, max_length=100)
    harness_service_tier: str | None = Field(default=None, max_length=100)
    harness_skill: dict[str, str] | None = None
    runtime_switch_confirmation: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    stream: bool = False

    @model_validator(mode="after")
    def conversation_is_bounded_and_actionable(self) -> "ChatCompletionRequest":
        if sum(len(message.content) for message in self.messages) > 250_000:
            raise ValueError("chat history exceeds the 250000 character limit")
        if any(message.role == ChatRole.SYSTEM for message in self.messages):
            raise ValueError("client-supplied system messages are not allowed")
        if self.messages[-1].role != ChatRole.USER:
            raise ValueError("the final chat message must have role=user")
        if sum(len(item.text) for item in self.context_attachments) > 20_000:
            raise ValueError("selected context exceeds the 20000 character limit")
        if self.backend == ChatBackend.PROVIDER:
            if not self.provider_id:
                raise ValueError("provider chat requires provider_id")
            if self.harness_profile_id or self.harness_session_id:
                raise ValueError("provider chat cannot include harness runtime fields")
        else:
            if not self.harness_profile_id or self.provider_id:
                raise ValueError(
                    "harness chat requires harness_profile_id and no provider_id"
                )
        return self


class ChatRuntimeSwitchPreflightRequest(NebulaModel):
    provider_id: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=500)
    tools_enabled: bool = False
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    expected_session_revision: int = Field(ge=1)


class ChatRuntimeSwitchPreflight(NebulaModel):
    session_id: str
    session_revision: int
    current_provider_id: str
    current_model: str
    target_provider_id: str
    target_model: str
    compatible: bool
    requires_compaction_confirmation: bool = False
    confirmation_token: str | None = None
    reason: str | None = None
    estimated_active_input_tokens: int = Field(default=0, ge=0)
    target_context_window: int | None = Field(default=None, ge=1)
    target_input_tokens: int | None = Field(default=None, ge=1)
    target_max_output_tokens: int | None = Field(default=None, ge=1)
    metadata_revision: str | None = None


class ChatResponseMessage(NebulaModel):
    id: str | None = Field(default=None, max_length=200)
    role: ChatRole = ChatRole.ASSISTANT
    content: str = ""
    reasoning: str = ""


class ChatCompletionResponse(NebulaModel):
    turn_id: str | None = None
    session_id: str | None = None
    backend: ChatBackend = ChatBackend.PROVIDER
    provider_id: str | None = None
    harness_profile_id: str | None = None
    harness_session_id: str | None = None
    harness_turn_id: str | None = None
    model: str
    message: ChatResponseMessage
    usage: ChatTokenUsage = Field(default_factory=ChatTokenUsage)
    context_usage: ChatTokenUsage | None = None
    # Set once the turn is persisted, so a streaming client shows the same
    # elapsed time the transcript keeps after a reload.
    elapsed_ms: int | None = None
    approval_wait_ms: int | None = None
    finish_reason: str | None = None
    provider_request_id: str | None = None
    citations: list[ChatCitation] = Field(default_factory=list)
    tool_suggestions: dict[str, Any] | None = None


@dataclass(frozen=True)
class HarnessKnowledgeContext:
    """Bounded retrieval context suitable for a runtime-managed harness turn."""

    text: str
    citations: list[ChatCitation]
    contains_local_only: bool


@dataclass(frozen=True)
class HarnessKnowledgeMatch:
    """One bounded, source-backed result returned through the harness gateway."""

    text: str
    citation: ChatCitation
    local_only: bool


@dataclass(frozen=True)
class HarnessKnowledgeSearchResult:
    """Structured retrieval results without exposing the underlying index."""

    matches: list[HarnessKnowledgeMatch]


@dataclass(frozen=True)
class _RetrievedChunk:
    citation: ChatCitation
    text: str
    local_only: bool
    score: float
    ordinal: int


def _reference_instructions(
    chunks: list[_RetrievedChunk], *, trusted_operator_help: bool
) -> str:
    if not chunks:
        return ""
    reference_data = [
        {
            "source_id": chunk.citation.source_id,
            "chunk_id": chunk.citation.chunk_id,
            "name": chunk.citation.name,
            "citation": chunk.citation.citation,
            "text": chunk.text,
        }
        for chunk in chunks
    ]
    if trusted_operator_help:
        return (
            "\n\nBEGIN NEBULA OPERATOR HELP (JSON)\n"
            + json.dumps(reference_data, ensure_ascii=False, separators=(",", ":"))
            + "\nEND NEBULA OPERATOR HELP"
            + "\nIf no help article matches an observed Nebula failure, report the "
            "exact error and say that no verified recovery procedure is available."
        )
    return (
        "\n\nBEGIN REFERENCE DATA (JSON)\n"
        + json.dumps(reference_data, ensure_ascii=False, separators=(",", ":"))
        + "\nEND REFERENCE DATA"
    )


class _RetrievalPlan(NebulaModel):
    """Bounded semantic searches proposed by the retrieval agent."""

    queries: list[str] = Field(min_length=1, max_length=4)

    @field_validator("queries")
    @classmethod
    def queries_are_distinct_and_bounded(cls, queries: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for query in queries:
            query = " ".join(query.split()).strip()[:500]
            folded = query.casefold()
            if query and folded not in seen:
                seen.add(folded)
                cleaned.append(query)
        if not cleaned:
            raise ValueError("retrieval plan must contain a non-empty query")
        return cleaned


@dataclass
class PreparedChat:
    provider: ModelProvider
    provider_profile: ProviderProfile
    model_request: ModelRequest
    resolved_model: str
    citations: list[ChatCitation]
    engagement_id: str | None
    session: ChatSession | None
    pending_session: ChatSession | None
    stored_messages: list[ChatMessage]
    new_messages: list[ChatRequestMessage]
    operator_decisions: list[dict[str, Any]] = field(default_factory=list)
    source_request: ChatCompletionRequest | None = None
    base_instructions: str = ""
    required_parameters: set[str] = field(default_factory=set)
    context_attachments: list[ChatContextAttachment] = field(default_factory=list)
    context_usage: ChatTokenUsage = field(default_factory=ChatTokenUsage)
    context_snapshot: ContextSnapshot | None = None
    tools_enabled: bool = False
    tool_components: RuntimeToolComponents | AutomationToolComponents | None = None
    # A turn carries its own start; a turnless completion still reports elapsed.
    started_at: datetime = field(default_factory=utc_now)
    turn: ChatTurn | None = None
    inputs_persisted: bool = False
    queue_claim: tuple[str, int, str] | None = None
    execution_claim_id: str | None = None
    hook_snapshots: list[Any] = field(default_factory=list)


@dataclass
class _ActiveProviderTurn:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task[None] | None = None
    cleanup_task: asyncio.Task[None] | None = None
    followers: int = 0
    done: bool = False
    error: BaseException | None = None


def _content_with_selected_context(
    content: str, attachments: list[ChatContextAttachment]
) -> str:
    payload = [item.model_dump(mode="json") for item in attachments]
    rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        content.rstrip()
        + "\n\nBEGIN SELECTED CONTEXT (JSON)\n"
        + rendered
        + "\nEND SELECTED CONTEXT"
    )


def _context_attachment_metadata(
    attachments: list[ChatContextAttachment],
) -> dict[str, Any]:
    if not attachments:
        return {}
    return {
        "context_attachments": [item.model_dump(mode="json") for item in attachments]
    }


_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{2,}")
_STOP_WORDS = {
    "about",
    "and",
    "are",
    "can",
    "could",
    "document",
    "documents",
    "for",
    "from",
    "have",
    "please",
    "summarize",
    "that",
    "the",
    "this",
    "what",
    "with",
}

_CHAT_BASE_INSTRUCTIONS = (
    "Answer the operator's request. Cite provided references with [source_id:chunk_id]."
)

_CHAT_INSTRUCTIONS = (
    """No tools are available in this turn. """ + _CHAT_BASE_INSTRUCTIONS
)

_CHAT_TOOL_INSTRUCTIONS = (
    """Call one or more supplied functions and return no prose. Request several
functions in the same response when they do not depend on each other; keep a
call that needs an earlier result for a later response. Nebula runs a batch one
call at a time, in the order you asked for, and replays every result. Use
finish_response when no tool is needed. Tool results can be inspected with
tool_output.search and tool_output.read."""
    + BROWSER_MODEL_WORKFLOW
)

_CHAT_TOOL_RESULT_INSTRUCTIONS = (
    """Answer the operator using the supplied tool results. """
    + _CHAT_BASE_INSTRUCTIONS
)

_RETRIEVAL_AGENT_INSTRUCTIONS = """Return a JSON `queries` array containing one
to four searches for the operator's request."""


def _routing_input_schema(spec: Any) -> dict[str, Any]:
    """Constrain Core-owned routing arguments instead of asking the model to guess."""

    schema = deepcopy(spec.input_schema)
    properties = schema.get("properties")
    if "cwd" in spec.path_arguments and isinstance(properties, dict):
        properties["cwd"] = {
            "type": "string",
            "const": ".",
            "description": "Engagement workspace root; supplied by Nebula Core.",
        }
    return schema


def _tool_inventory_instructions(specs: Any) -> str:
    """Expose runtime capability metadata to final synthesis."""

    inventory = [
        {
            "name": spec.name,
            "description": spec.description,
            "risk_class": spec.risk_class.value,
            "network_access": spec.network_access,
            "requires_approval": spec.requires_approval,
        }
        for spec in sorted(specs.values(), key=lambda item: item.name)
    ]
    return (
        "\n\nBEGIN COMMAND-RUNTIME CAPABILITIES (JSON)\n"
        + json.dumps(inventory, ensure_ascii=False, separators=(",", ":"))
        + "\nEND COMMAND-RUNTIME CAPABILITIES"
    )


def _normalize_routing_arguments(
    components: Any,
    spec: Any,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Copy model arguments before Core-owned path normalization."""

    del components, spec
    return dict(arguments)


def _decoded_result(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (
        json.JSONDecodeError
    ):  # diagnostic-expected: legacy tool history fails closed
        return None
    return decoded if isinstance(decoded, dict) else None


def unarchive_chat_session(store: NebulaStore, session_id: str) -> None:
    """Return an archived conversation to the active list when the operator writes to it."""
    for _ in range(3):
        current = store.get(ChatSession, session_id)
        if "archived_at" not in current.metadata:
            return
        try:
            store.update(
                ChatSession,
                session_id,
                {
                    "metadata": {
                        key: value
                        for key, value in current.metadata.items()
                        if key != "archived_at"
                    }
                },
                expected_revision=current.revision,
            )
            return
        except (
            ConflictError
        ):  # diagnostic-expected: concurrent writer won; re-read and retry
            continue


class ChatService:
    """Resolve profiles, isolate retrieval, and persist completed exchanges."""

    def __init__(
        self,
        store: NebulaStore,
        *,
        tool_platform: RuntimePlatform | None = None,
        automation_tool_platform: AutomationToolPlatform | None = None,
        provider_factory: Callable[[ProviderProfile], ModelProvider] | None = None,
        operator_id: Callable[[], str] | None = None,
        knowledge_index: KnowledgeIndex | None = None,
        artifact_store: ArtifactStore | None = None,
        workspace_resolver: Callable[[str], Path] | None = None,
        managed_skill_root: Path | None = None,
        worker_id: str | None = None,
        tool_suggestion_client: Callable[[], JevClient | None] | None = None,
    ) -> None:
        self.store = store
        self.tool_suggestion_client = (
            tool_suggestion_client or JevClient.from_environment
        )
        self.tool_platform = tool_platform
        self.automation_tool_platform = automation_tool_platform
        self.browser_tool_platform = BrowserToolPlatform(store)
        self.provider_factory = provider_factory or provider_from_profile
        self.operator_id = operator_id or (lambda: "system")
        self.knowledge_index = knowledge_index
        self._embedding_warmup: threading.Thread | None = None
        self.artifact_store = artifact_store
        self.workspace_resolver = workspace_resolver or self._workspace_unavailable
        self.managed_skill_root = managed_skill_root
        self.worker_id = worker_id or f"core-worker-{uuid4()}"
        self._active_provider_turns: dict[str, _ActiveProviderTurn] = {}
        self._naming_tasks: set[asyncio.Task[Any]] = set()
        self.subagents = SubagentService(store, self)
        self.shutting_down = False

    @staticmethod
    def _workspace_unavailable(engagement_id: str) -> Path:
        del engagement_id
        raise ChatConfigurationError(
            "skill selection requires an available project workspace"
        )

    def start_optional_naming(self, coroutine: Any) -> None:
        task = create_diagnostic_task(
            coroutine,
            feature="chat",
            event_code="chat.optional_naming",
            failure_message="Optional conversation naming failed; the saved reply remains available.",
            name="nebula-conversation-naming",
        )
        self._naming_tasks.add(task)
        task.add_done_callback(self._naming_tasks.discard)

    async def startup(self) -> None:
        """Pause turns orphaned by restart without replaying uncertain effects."""

        active_statuses = {
            ChatTurnStatus.ROUTING,
            ChatTurnStatus.FINALIZING,
        }
        turns: list[ChatTurn] = []
        calls: list[ToolCall] = []
        hook_executions: list[NativeHookExecution] = []
        offset = 0
        while page := self.store.list_entities(ChatTurn, offset=offset, limit=1_000):
            turns.extend(
                item
                for item in page
                if item.backend == ChatBackend.PROVIDER
                and item.status in active_statuses
            )
            offset += len(page)
        offset = 0
        while call_page := self.store.list_entities(
            ToolCall, offset=offset, limit=1_000
        ):
            calls.extend(call_page)
            offset += len(call_page)
        offset = 0
        while hook_page := self.store.list_entities(
            NativeHookExecution, offset=offset, limit=1_000
        ):
            hook_executions.extend(hook_page)
            offset += len(hook_page)
        for turn in turns:
            unknown = [
                call.id
                for call in calls
                if call.chat_turn_id == turn.id
                and call.status == ToolCallStatus.RUNNING
            ]
            unknown_hooks = [
                execution.id
                for execution in hook_executions
                if execution.chat_turn_id == turn.id
                and execution.status == "running"
                and execution.side_effects != "none"
            ]
            for execution in hook_executions:
                if execution.chat_turn_id == turn.id and execution.status == "running":
                    self.store.update(
                        NativeHookExecution,
                        execution.id,
                        {
                            "status": "interrupted",
                            "completed_at": utc_now(),
                            "error": "Core restarted before the hook outcome was known.",
                        },
                        expected_revision=execution.revision,
                    )
            detail = (
                "Core restarted while an effect outcome was unknown. Reconcile the "
                "listed tool or hook execution before resuming."
                if unknown or unknown_hooks
                else "Core restarted before this response completed. Review and resume it."
            )
            snapshot = {
                **turn.request_snapshot,
                "recovery": {
                    "required": True,
                    "unknown_tool_call_ids": unknown,
                    "unknown_hook_execution_ids": unknown_hooks,
                    "interrupted_at": utc_now().isoformat(),
                },
            }
            self.store.update(
                ChatTurn,
                turn.id,
                {
                    "status": ChatTurnStatus.INTERRUPTED,
                    "error": detail,
                    "request_snapshot": snapshot,
                    "execution_owner_id": None,
                    "execution_claim_id": None,
                    "execution_claimed_at": None,
                },
                expected_revision=turn.revision,
            )
            if turn.goal_id:
                goal = self.store.get(ChatGoal, turn.goal_id)
                if goal.status == ChatGoalStatus.RUNNING:
                    paused_at = utc_now()
                    self.store.update(
                        ChatGoal,
                        goal.id,
                        {
                            "status": ChatGoalStatus.PAUSED,
                            "paused_at": paused_at,
                            "active_since": None,
                            "elapsed_seconds": goal.active_elapsed_seconds(paused_at),
                            "blocked_reason": detail,
                            "execution_owner_id": None,
                            "execution_claim_id": None,
                            "execution_claimed_at": None,
                        },
                        expected_revision=goal.revision,
                    )
        offset = 0
        while goal_page := self.store.list_entities(
            ChatGoal, offset=offset, limit=1_000
        ):
            for goal in goal_page:
                if (
                    goal.status == ChatGoalStatus.RUNNING
                    and goal.execution_claim_id is not None
                ):
                    paused_at = utc_now()
                    self.store.update(
                        ChatGoal,
                        goal.id,
                        {
                            "status": ChatGoalStatus.PAUSED,
                            "paused_at": paused_at,
                            "active_since": None,
                            "elapsed_seconds": goal.active_elapsed_seconds(paused_at),
                            "blocked_reason": (
                                "Core restarted while this goal had an active worker. "
                                "Review its latest turn before resuming."
                            ),
                            "execution_owner_id": None,
                            "execution_claim_id": None,
                            "execution_claimed_at": None,
                        },
                        expected_revision=goal.revision,
                    )
            offset += len(goal_page)
        await self.subagents.reconcile_after_restart()

    def _claim_execution(self, prepared: PreparedChat) -> None:
        """Atomically fence one Core worker around a provider turn and its goal."""

        turn = prepared.turn
        if turn is None:
            return
        if prepared.execution_claim_id is not None:
            self._assert_execution_owner(prepared)
            return
        latest = self.store.get(ChatTurn, turn.id)
        if latest.execution_claim_id is not None:
            raise ChatHistoryConflict(
                "chat response is already owned by another Core worker"
            )
        if latest.status not in {
            ChatTurnStatus.ROUTING,
            ChatTurnStatus.WAITING_APPROVAL,
            ChatTurnStatus.WAITING_CALLBACK,
            ChatTurnStatus.FINALIZING,
        }:
            raise ChatHistoryConflict(
                f"chat turn cannot start from {latest.status.value}"
            )
        claim_id = str(uuid4())
        claimed_at = utc_now()
        goal = self.store.get(ChatGoal, latest.goal_id) if latest.goal_id else None
        if goal is not None and goal.execution_claim_id is not None:
            raise ChatHistoryConflict(
                "chat goal is already owned by another Core worker"
            )
        try:
            with self.store.transaction() as transaction:
                claimed_turn = transaction.update(
                    ChatTurn,
                    latest.id,
                    {
                        "execution_owner_id": self.worker_id,
                        "execution_claim_id": claim_id,
                        "execution_claimed_at": claimed_at,
                    },
                    expected_revision=latest.revision,
                )
                if goal is not None:
                    transaction.update(
                        ChatGoal,
                        goal.id,
                        {
                            "execution_owner_id": self.worker_id,
                            "execution_claim_id": claim_id,
                            "execution_claimed_at": claimed_at,
                        },
                        expected_revision=goal.revision,
                    )
        except ConflictError as exc:
            raise ChatHistoryConflict(
                "chat response ownership changed while it was starting"
            ) from exc
        prepared.turn = claimed_turn
        prepared.execution_claim_id = claim_id

    def _assert_execution_owner(self, prepared: PreparedChat) -> ChatTurn:
        turn = prepared.turn
        claim_id = prepared.execution_claim_id
        if turn is None or claim_id is None:
            raise ChatHistoryConflict("chat response has no active execution owner")
        latest = self.store.get(ChatTurn, turn.id)
        if (
            latest.execution_owner_id != self.worker_id
            or latest.execution_claim_id != claim_id
        ):
            raise ChatHistoryConflict(
                "chat response ownership changed; stale worker output was discarded"
            )
        if latest.goal_id:
            goal = self.store.get(ChatGoal, latest.goal_id)
            if (
                goal.execution_owner_id != self.worker_id
                or goal.execution_claim_id != claim_id
            ):
                raise ChatHistoryConflict(
                    "chat goal ownership changed; stale worker output was discarded"
                )
        prepared.turn = latest
        return latest

    def _release_execution(self, prepared: PreparedChat) -> None:
        turn = prepared.turn
        claim_id = prepared.execution_claim_id
        if turn is None or claim_id is None:
            return
        latest = self.store.get(ChatTurn, turn.id)
        if (
            latest.execution_owner_id != self.worker_id
            or latest.execution_claim_id != claim_id
        ):
            return
        goal = self.store.get(ChatGoal, latest.goal_id) if latest.goal_id else None
        with self.store.transaction() as transaction:
            prepared.turn = transaction.update(
                ChatTurn,
                latest.id,
                {
                    "execution_owner_id": None,
                    "execution_claim_id": None,
                    "execution_claimed_at": None,
                },
                expected_revision=latest.revision,
            )
            if (
                goal is not None
                and goal.execution_owner_id == self.worker_id
                and goal.execution_claim_id == claim_id
            ):
                transaction.update(
                    ChatGoal,
                    goal.id,
                    {
                        "execution_owner_id": None,
                        "execution_claim_id": None,
                        "execution_claimed_at": None,
                    },
                    expected_revision=goal.revision,
                )
        prepared.execution_claim_id = None

    def start_provider_turn(self, prepared: PreparedChat) -> str:
        turn = prepared.turn
        if turn is None:
            raise ChatError("provider chat is missing its durable turn")
        existing = self._active_provider_turns.get(turn.id)
        if existing is not None and not existing.done:
            raise ChatHistoryConflict("chat turn already has active work")
        self._claim_execution(prepared)
        if existing is not None and existing.cleanup_task is not None:
            existing.cleanup_task.cancel()
        runtime = _ActiveProviderTurn()
        self._active_provider_turns[turn.id] = runtime
        runtime.task = create_diagnostic_task(
            self._produce_provider_turn(prepared, runtime),
            feature="chat",
            event_code="chat.provider_turn",
            failure_message="A provider chat turn stopped unexpectedly.",
            name=f"nebula-provider-chat-{turn.id}",
        )
        return turn.id

    def has_active_provider_turn(self, turn_id: str) -> bool:
        runtime = self._active_provider_turns.get(turn_id)
        return runtime is not None and not runtime.done

    async def follow_provider_turn(
        self, turn_id: str, *, after_sequence: int = 0
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        runtime = self._active_provider_turns.get(turn_id)
        if runtime is None:
            raise ChatHistoryConflict("provider chat turn is not active")
        runtime.followers += 1
        cleanup_task = runtime.cleanup_task
        if cleanup_task is not None:
            cleanup_task.cancel()
            runtime.cleanup_task = None
            await asyncio.gather(cleanup_task, return_exceptions=True)
        index = after_sequence
        try:
            while True:
                async with runtime.condition:
                    await runtime.condition.wait_for(
                        lambda: index < len(runtime.events) or runtime.done
                    )
                    batch_start = index
                    batch = runtime.events[index:]
                    index = len(runtime.events)
                    done = runtime.done
                    error = runtime.error
                for offset, (event_type, payload) in enumerate(batch, batch_start + 1):
                    yield event_type, {**payload, "sequence": offset}
                if done and index >= len(runtime.events):
                    if error is not None:
                        raise error
                    return
        finally:
            runtime.followers -= 1
            if (
                runtime.done
                and runtime.followers == 0
                and self._active_provider_turns.get(turn_id) is runtime
            ):
                self._active_provider_turns.pop(turn_id, None)

    async def stop_provider_turn(self, turn_id: str) -> ChatTurn:
        runtime = self._active_provider_turns.get(turn_id)
        if runtime is not None and runtime.task is not None and not runtime.task.done():
            runtime.task.cancel()
            try:
                await runtime.task
            except asyncio.CancelledError as exc:
                record_caught_exception(
                    "chat",
                    "chat.provider_turn.cancelled_before_start",
                    "A provider chat turn was cancelled before it started.",
                    exc,
                    stage="provider-turn-stop",
                )
        cancelled = self.cancel_turn(turn_id)
        await self.subagents.stop_for_parent_turn(turn_id)
        return cancelled

    async def fire_due_schedules(self) -> None:
        """Run due provider-chat schedules without overlapping an active turn."""

        from .chat_goals import ChatGoalService
        from .chat_schedules import ChatScheduleService
        from .storage import NotFoundError

        schedules = ChatScheduleService(self.store)
        goals = ChatGoalService(self.store)
        for schedule in schedules.due():
            reason = schedules.revalidate(schedule)
            if reason:
                schedules.skip(schedule, reason)
                continue
            if self.pending_turn(schedule.session_id) is not None:
                schedules.skip(schedule, "Previous turn is still active.")
                continue
            try:
                goal = goals.get(schedule.session_id)
            except NotFoundError:  # diagnostic-expected: missing goal is recorded as a schedule skip receipt
                schedules.skip(schedule, "No conversation goal is available.")
                continue
            if goal.status != ChatGoalStatus.RUNNING:
                schedules.skip(
                    schedule,
                    f"Goal is {goal.status.value}; scheduled work waits for Start.",
                )
                continue
            try:
                prepared = self.prepare(
                    ChatCompletionRequest(
                        provider_id=schedule.provider_profile_id,
                        engagement_id=schedule.engagement_id,
                        session_id=schedule.session_id,
                        goal_id=goal.id,
                        model=schedule.model,
                        messages=[
                            ChatRequestMessage(
                                role=ChatRole.USER,
                                content="Continue the scheduled conversation goal.",
                            )
                        ],
                        include_knowledge=False,
                    )
                )
                completion = await self.complete(prepared)
                schedules.record_run(
                    schedule,
                    turn_id=completion.turn_id or "",
                    status="complete",
                )
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.schedule.failed",
                    "A scheduled provider chat occurrence failed.",
                    exc,
                    stage="schedule",
                )
                schedules.skip(
                    schedule,
                    "Scheduled occurrence failed; it was not retried overlapping.",
                )

    async def shutdown(self) -> None:
        # Cancellation below is Core stopping, not an operator stop.
        self.shutting_down = True
        tasks = [
            task
            for runtime in self._active_provider_turns.values()
            for task in (runtime.task, runtime.cleanup_task)
            if task is not None and not task.done()
        ]
        tasks.extend(self._naming_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_provider_turns.clear()

    async def _produce_provider_turn(
        self, prepared: PreparedChat, runtime: _ActiveProviderTurn
    ) -> None:
        try:
            async for event in self.stream(prepared):
                async with runtime.condition:
                    runtime.events.append(event)
                    runtime.condition.notify_all()
        except asyncio.CancelledError as exc:
            record_caught_exception(
                "chat",
                "chat.provider_turn.cancelled",
                "A provider chat turn was cancelled.",
                exc,
                stage="provider-turn-stream",
            )
            await self._run_terminal_native_hooks(
                prepared, "chat.turn.cancelled", "response stopped"
            )
            runtime.error = exc
        except BaseException as exc:
            record_caught_exception(
                "chat",
                "chat.provider_turn.failed",
                "A provider chat turn failed while streaming.",
                exc,
                stage="provider-turn-stream",
            )
            await self._run_terminal_native_hooks(
                prepared, "chat.turn.failed", str(exc)[:1_000]
            )
            runtime.error = exc
        finally:
            async with runtime.condition:
                runtime.done = True
                runtime.condition.notify_all()
            turn = prepared.turn
            if turn is not None and runtime.error is not None:
                latest = self.store.get(ChatTurn, turn.id)
                if (
                    latest.status
                    not in {
                        ChatTurnStatus.COMPLETE,
                        ChatTurnStatus.CANCELLED,
                        ChatTurnStatus.WAITING_APPROVAL,
                    }
                    and latest.execution_claim_id == prepared.execution_claim_id
                ):
                    status = (
                        ChatTurnStatus.CANCELLED
                        if isinstance(runtime.error, asyncio.CancelledError)
                        else ChatTurnStatus.FAILED
                    )
                    self.store.update(
                        ChatTurn,
                        latest.id,
                        {
                            "status": status,
                            "error": (
                                "response stopped"
                                if status == ChatTurnStatus.CANCELLED
                                else str(runtime.error)[:1_000]
                            ),
                        },
                        expected_revision=latest.revision,
                    )
                    self._release_execution(prepared)
            if turn is not None:
                try:
                    await self.subagents.turn_settled(turn.id)
                except Exception as exc:
                    record_caught_exception(
                        "chat",
                        "chat.subagent.settle_failed",
                        "Subagent state could not be updated after a response settled.",
                        exc,
                        stage="subagent-settle",
                    )
            if runtime.followers == 0:
                runtime.cleanup_task = create_diagnostic_task(
                    self._expire_provider_turn(turn.id if turn else "", runtime),
                    feature="chat",
                    event_code="chat.provider_turn_cleanup",
                    failure_message="A completed provider turn could not be expired.",
                    name=f"nebula-provider-chat-cleanup-{turn.id if turn else 'unknown'}",
                )

    async def _expire_provider_turn(
        self, turn_id: str, runtime: _ActiveProviderTurn
    ) -> None:
        await asyncio.sleep(60)
        if (
            runtime.done
            and runtime.followers == 0
            and self._active_provider_turns.get(turn_id) is runtime
        ):
            self._active_provider_turns.pop(turn_id, None)

    def _model_content(
        self, message: ChatRequestMessage, engagement_id: str | None
    ) -> str | list[dict[str, Any]]:
        return resolve_chat_model_content(
            self.store, self.artifact_store, message, engagement_id
        )

    def prepare(self, request: ChatCompletionRequest) -> PreparedChat:
        """Synchronous compatibility wrapper for non-ASGI callers and tests."""

        return asyncio.run(self.prepare_async(request))

    def harness_knowledge_context(
        self, engagement_id: str, query: str, *, token_budget: int = 4_096
    ) -> HarnessKnowledgeContext:
        """Reuse engagement retrieval without provider planning or history replay."""

        result = self.harness_knowledge_search(
            engagement_id,
            query,
            allow_local_only=True,
            token_budget=token_budget,
        )
        chunks = [
            _RetrievedChunk(
                citation=match.citation,
                text=match.text,
                local_only=match.local_only,
                score=0,
                ordinal=index,
            )
            for index, match in enumerate(result.matches)
        ]
        return HarnessKnowledgeContext(
            text=_reference_instructions(chunks, trusted_operator_help=False),
            citations=[chunk.citation for chunk in chunks],
            contains_local_only=any(chunk.local_only for chunk in chunks),
        )

    def harness_knowledge_search(
        self,
        engagement_id: str,
        query: str,
        *,
        allow_local_only: bool,
        token_budget: int = 4_096,
    ) -> HarnessKnowledgeSearchResult:
        """Return a bounded engagement-scoped search for a managed harness."""

        clean_query = query.strip()
        if not clean_query or not self._has_ready_knowledge(engagement_id):
            return HarnessKnowledgeSearchResult([])
        chunks = self._retrieve(
            engagement_id,
            [clean_query],
            redact=not allow_local_only,
            token_budget=max(1, min(token_budget, 8_192)),
            allow_local_only=allow_local_only,
        )
        return HarnessKnowledgeSearchResult(
            [
                HarnessKnowledgeMatch(
                    text=chunk.text,
                    citation=chunk.citation,
                    local_only=chunk.local_only,
                )
                for chunk in chunks
            ]
        )

    async def prepare_async(self, request: ChatCompletionRequest) -> PreparedChat:
        if request.backend != ChatBackend.PROVIDER or request.provider_id is None:
            raise ChatConfigurationError(
                "harness chat requests must be dispatched through HarnessRuntimeService"
            )
        profile = self.store.get(ProviderProfile, request.provider_id)
        if not profile.enabled:
            raise ChatConfigurationError(
                f"provider {request.provider_id!r} is disabled"
            )
        provider = self.provider_factory(profile)
        if (
            any(
                block.type == "image"
                for message in request.messages
                for block in message.content_blocks
            )
            and not profile.capabilities.vision
        ):
            raise ChatConfigurationError(
                "the selected provider/model is not verified for vision input"
            )

        session: ChatSession | None = None
        pending_session: ChatSession | None = None
        stored_messages: list[ChatMessage] = []
        engagement_id = request.engagement_id
        durable_incoming = list(request.messages)
        incoming = list(durable_incoming)
        if request.context_attachments:
            last = incoming[-1]
            incoming[-1] = ChatRequestMessage(
                role=last.role,
                content=_content_with_selected_context(
                    last.content, request.context_attachments
                ),
                content_blocks=last.content_blocks,
            )

        if request.session_id:
            session = self.store.get(ChatSession, request.session_id)
            if engagement_id and engagement_id != session.engagement_id:
                raise ChatHistoryConflict(
                    "chat session does not belong to the requested engagement"
                )
            engagement_id = session.engagement_id
            if self.pending_turn(session.id) is not None:
                raise ChatHistoryConflict(
                    "chat session already has an active response; resume or resolve it before sending another message"
                )
            stored_messages = self._session_messages(session)
            incoming, _ = self._merge_history(stored_messages, incoming)
            _, new_messages = self._merge_history(stored_messages, durable_incoming)
        else:
            new_messages = durable_incoming

        goal: ChatGoal | None = None
        if request.goal_id:
            if session is None:
                raise ChatConfigurationError(
                    "a goal turn requires an existing conversation"
                )
            goal = self.store.get(ChatGoal, request.goal_id)
            if (
                goal.session_id != session.id
                or goal.engagement_id != session.engagement_id
            ):
                raise ChatConfigurationError(
                    "goal does not belong to this conversation"
                )
            if goal.status != ChatGoalStatus.RUNNING:
                raise ChatConfigurationError("goal must be running before dispatch")
            now = utc_now()
            elapsed = goal.active_elapsed_seconds(now)
            exhausted_reason: str | None = None
            if goal.step_budget is not None and goal.current_step >= goal.step_budget:
                exhausted_reason = "Goal step budget is exhausted."
            elif (
                goal.token_budget is not None
                and goal.usage.total_tokens >= goal.token_budget
            ):
                exhausted_reason = "Goal token budget is exhausted."
            elif (
                goal.time_budget_seconds is not None
                and elapsed >= goal.time_budget_seconds
            ):
                exhausted_reason = "Goal active-time budget is exhausted."
            if exhausted_reason is not None:
                self.store.update(
                    ChatGoal,
                    goal.id,
                    {
                        "status": ChatGoalStatus.PAUSED,
                        "paused_at": now,
                        "active_since": None,
                        "elapsed_seconds": elapsed,
                        "blocked_reason": exhausted_reason,
                    },
                    expected_revision=goal.revision,
                )
                raise ChatConfigurationError(exhausted_reason.lower())

        from .native_hooks import (
            discover_native_hooks,
            snapshot_native_hook,
        )

        skill_snapshots = [
            SkillSnapshot.model_validate(item)
            for item in (goal.skill_snapshots if goal is not None else [])
        ]
        if request.skill is not None:
            if engagement_id is None:
                raise ChatConfigurationError(
                    "skill selection requires a project conversation"
                )
            try:
                workspace = self.workspace_resolver(engagement_id)
                selected_snapshot = snapshot_skill(
                    SkillSelection.model_validate(request.skill),
                    discover_skills(
                        native_skill_roots(workspace, self.managed_skill_root)
                    ),
                )
            except (NativeHookError, OSError, ValueError) as exc:
                raise ChatConfigurationError(str(exc)) from exc
            if goal is not None:
                existing_paths = {item.path for item in skill_snapshots}
                if selected_snapshot.path not in existing_paths:
                    skill_snapshots.append(selected_snapshot)
                    goal = self.store.update(
                        ChatGoal,
                        goal.id,
                        {
                            "skill_snapshots": [
                                item.model_dump(mode="json") for item in skill_snapshots
                            ]
                        },
                        expected_revision=goal.revision,
                    )
            else:
                skill_snapshots = [selected_snapshot]

        hook_snapshots = []
        if request.hook_ids:
            if engagement_id is None:
                raise ChatConfigurationError(
                    "hook selection requires a project conversation"
                )
            if len(set(request.hook_ids)) != len(request.hook_ids):
                raise ChatConfigurationError("hook selection contains duplicates")
            try:
                hook_catalog = discover_native_hooks(
                    self.workspace_resolver(engagement_id)
                )
                hook_snapshots = [
                    snapshot_native_hook(hook_id, hook_catalog)
                    for hook_id in request.hook_ids
                ]
            except (NativeHookError, OSError, ValueError) as exc:
                raise ChatConfigurationError(str(exc)) from exc

        selected_model = (
            request.model
            or (session.model if session else None)
            or profile.metadata.get("default_model")
            or next(iter(profile.model_allowlist), None)
        )
        if not isinstance(selected_model, str) or not selected_model:
            raise ChatConfigurationError(
                "chat requires an explicit model or a provider default model"
            )
        if profile.model_allowlist and selected_model not in profile.model_allowlist:
            raise ChatConfigurationError(
                f"model {selected_model!r} is not allowed by provider {profile.id!r}"
            )
        profile = await self._verify_openrouter_route_limits(
            profile, provider, selected_model
        )
        subagent_child = session is not None and is_subagent_session(session)
        subagents_enabled = bool(
            request.allow_subagents and not subagent_child and engagement_id
        )
        switch_tools_enabled = bool(
            request.tools_enabled
            or subagents_enabled
            or request.mcp_server_ids
            or request.ssh_environment_ids
            or any(item.resources for item in skill_snapshots)
            or any(
                item.source_kind
                in {"browser_page", "browser_companion", "application_model"}
                for item in request.context_attachments
            )
        )
        if session is not None and (
            session.provider_profile_id != profile.id or session.model != selected_model
        ):
            switch = self.runtime_switch_preflight(
                session.id,
                ChatRuntimeSwitchPreflightRequest(
                    provider_id=profile.id,
                    model=selected_model,
                    tools_enabled=switch_tools_enabled,
                    max_output_tokens=request.max_output_tokens,
                    expected_session_revision=session.revision,
                ),
            )
            if not switch.compatible:
                raise ChatConfigurationError(
                    switch.reason or "the selected provider/model is incompatible"
                )
            if switch.requires_compaction_confirmation and (
                request.runtime_switch_confirmation != switch.confirmation_token
            ):
                raise ChatConfigurationError(
                    "switching to this provider/model requires confirmed context compaction; review the switch again"
                )

        engagement: Engagement | None = None
        if engagement_id:
            engagement = self.store.get(Engagement, engagement_id)
            self._enforce_engagement_privacy(engagement, provider)
            if session is None:
                pending_session = ChatSession(
                    id=request.session_id or str(uuid4()),
                    engagement_id=engagement.id,
                    # Titles describe the operator's prompt, never the expanded
                    # provider payload containing selected-context envelopes.
                    title=self._title(request.messages),
                    provider_profile_id=profile.id,
                    model=selected_model,
                )

        citations: list[ChatCitation] = []
        from .chat_decisions import decision_snapshot, decision_instructions

        operator_decisions = decision_snapshot(
            self.store, session.id if session else None, engagement_id
        )
        instructions = _CHAT_INSTRUCTIONS + decision_instructions(operator_decisions)
        if subagent_child:
            instructions += SUBAGENT_CHILD_INSTRUCTIONS
        if goal is not None:
            instructions += "\n\nCore-owned goal context:\n" + json.dumps(
                {
                    "objective": goal.objective,
                    "completion_criteria": goal.completion_criteria,
                    "plan": goal.plan,
                    "current_step": goal.current_step,
                    "remaining_steps": (
                        goal.step_budget - goal.current_step
                        if goal.step_budget is not None
                        else None
                    ),
                    "remaining_tokens": (
                        goal.token_budget - goal.usage.total_tokens
                        if goal.token_budget is not None
                        else None
                    ),
                },
                ensure_ascii=False,
            )
        project_instructions = self._project_instructions(engagement_id)
        instructions += project_instructions_text(project_instructions)
        instructions += skill_instructions(skill_snapshots)
        knowledge_budget = max(
            1,
            resolve_context_limits(
                profile,
                model=selected_model,
                requested_output_tokens=request.max_output_tokens,
            ).target_input_tokens
            // 5,
        )
        operator_help_chunks = self._retrieve_operator_help(
            [incoming[-1].content], token_budget=knowledge_budget
        )
        operator_help_tokens = sum(
            estimate_tokens(chunk.text, message_count=1)
            for chunk in operator_help_chunks
        )
        engagement_chunks: list[_RetrievedChunk] = []
        if (
            request.include_knowledge
            and engagement_id
            and self._has_ready_knowledge(engagement_id)
        ):
            retrieval_queries = await self._plan_retrieval(
                provider=provider,
                model=selected_model,
                query=incoming[-1].content,
            )
            engagement_chunks = self._retrieve(
                engagement_id,
                retrieval_queries,
                redact=not provider.config.local,
                token_budget=max(1, knowledge_budget - operator_help_tokens),
            )
            if (
                engagement_chunks
                and not provider.config.local
                and any(chunk.local_only for chunk in engagement_chunks)
            ):
                raise ChatPrivacyError(
                    "selected knowledge is local-only and cannot be sent to a cloud provider"
                )
            if engagement_chunks and not provider.config.local:
                if not profile.privacy.permits_sensitive_data:
                    raise ChatPrivacyError(
                        "provider profile does not permit engagement data transfer"
                    )
                if not request.allow_cloud_knowledge:
                    raise ChatPrivacyError(
                        "cloud knowledge transfer requires explicit operator confirmation"
                    )
        citations = [
            chunk.citation for chunk in [*operator_help_chunks, *engagement_chunks]
        ]
        instructions += _reference_instructions(
            operator_help_chunks, trusted_operator_help=True
        )
        # JSON encoding keeps engagement document text inside an explicit data
        # value; embedded delimiter-like strings never become instruction lines.
        instructions += _reference_instructions(
            engagement_chunks, trusted_operator_help=False
        )
        base_instructions = instructions

        compaction_budget = ContextCallBudget(
            max_tokens=(
                max(0, goal.token_budget - goal.usage.total_tokens)
                if goal is not None and goal.token_budget is not None
                else None
            )
        )
        try:
            (
                model_messages,
                instructions,
                context_usage,
                context_snapshot,
                session,
            ) = await self._model_context(
                request=request,
                profile=profile,
                provider=provider,
                model=selected_model,
                messages=incoming,
                stored_messages=stored_messages,
                session=session,
                instructions=instructions,
                budget=compaction_budget,
                required_parameters={"tools"} if switch_tools_enabled else set(),
            )
        except ContextCapacityError as exc:
            if goal is not None and exc.usage.total_tokens > 0:
                goal = self._charge_goal(
                    goal.id,
                    exc.usage,
                    exhausted_reason="Token budget exhausted during context compaction.",
                )
            record_caught_exception(
                "chat",
                "chat.chat.caught_failure_001",
                "A handled chat operation raised an exception.",
                exc,
                stage="chat",
            )
            raise ChatConfigurationError(str(exc)) from exc
        except ContextCompactionError as exc:
            if goal is not None and exc.usage.total_tokens > 0:
                goal = self._charge_goal(
                    goal.id,
                    exc.usage,
                    exhausted_reason="Token budget exhausted during context compaction.",
                )
            record_caught_exception(
                "chat",
                "chat.chat.caught_failure_002",
                "A handled chat operation raised an exception.",
                exc,
                stage="chat",
            )
            raise ChatCompactionError(str(exc)) from exc
        if goal is not None and context_usage.total_tokens > 0:
            goal = self._charge_goal(
                goal.id,
                context_usage,
                exhausted_reason="Token budget exhausted during context compaction.",
            )
            if goal.status != ChatGoalStatus.RUNNING:
                raise ChatConfigurationError(
                    "goal token budget was exhausted during context compaction"
                )

        request_limits = resolve_context_limits(
            profile,
            model=selected_model,
            requested_output_tokens=request.max_output_tokens,
            required_parameters={"tools"} if switch_tools_enabled else None,
        )
        model_request = ModelRequest(
            model=selected_model,
            instructions=instructions,
            messages=[
                ModelMessage(
                    role=message.role.value,
                    content=self._model_content(message, engagement_id),
                )
                for message in model_messages
            ],
            max_output_tokens=request_limits.max_output_tokens,
            temperature=request.temperature,
            metadata={
                key: value
                for key, value in {
                    "engagement_id": engagement_id,
                    "chat_session_id": (
                        session.id
                        if session
                        else pending_session.id
                        if pending_session
                        else None
                    ),
                    "resolved_context_limits": json.dumps(
                        request_limits.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }.items()
                if value is not None
            },
        )
        if goal is not None:
            model_request = self._fit_goal_request_budget(goal.id, model_request)
        self._ensure_request_capacity(profile, model_request)
        tool_components: RuntimeToolComponents | AutomationToolComponents | None = None
        # Standing profile consent stands in for the per-turn confirmation the
        # operator would otherwise give before tool results leave the device.
        allow_cloud_tool_results = (
            request.allow_cloud_tool_results or profile.privacy.auto_share_tool_results
        )
        turn: ChatTurn | None = None
        mcp_profiles: tuple[McpServerProfile, ...] = ()
        if request.mcp_server_ids:
            try:
                mcp_profiles = resolve_mcp_profiles(self.store, request.mcp_server_ids)
            except (McpProbeError, ValueError) as exc:
                raise ChatConfigurationError(str(exc)) from exc
        ssh_environments: tuple[SshEnvironment, ...] = ()
        if request.tools_enabled or request.ssh_environment_ids:
            try:
                ssh_environments = resolve_ssh_environments(
                    self.store, request.ssh_environment_ids
                )
            except (NotFoundError, ValueError) as exc:
                raise ChatConfigurationError(str(exc)) from exc
        browser_session_ids = {
            item.source_id
            for item in request.context_attachments
            if item.source_kind in {"browser_page", "browser_companion"}
            and item.source_id
        }
        if len(browser_session_ids) > 1:
            raise ChatConfigurationError(
                "one chat turn cannot control more than one browser session"
            )
        browser_session_id = next(iter(browser_session_ids), None)
        companion_session_id = (
            attached_session(self.store, engagement_id, request.session_id)
            if engagement_id
            else None
        )
        browser_session_id = browser_session_id or companion_session_id
        if browser_session_id:
            selected_browser = self.store.get(BrowserSession, browser_session_id)
            if selected_browser.metadata.get("browser_companion_version") == 1 and (
                selected_browser.metadata.get("assistant_paused", True)
                or not profile.tools_verified_for(selected_model)
                or (
                    not provider.config.local
                    and (
                        not profile.privacy.permits_sensitive_data
                        or not allow_cloud_tool_results
                    )
                )
            ):
                browser_session_id = None
        model_context = any(
            item.source_kind == "application_model"
            for item in request.context_attachments
        )
        skill_resources_selected = any(item.resources for item in skill_snapshots)
        tools_enabled = (
            request.tools_enabled
            or bool(mcp_profiles)
            or bool(ssh_environments)
            or bool(browser_session_id)
            or model_context
            or skill_resources_selected
            or subagents_enabled
        )
        if tools_enabled:
            if engagement_id is None:
                raise ChatConfigurationError(
                    "command-runtime chat requires an engagement-scoped session"
                )
            if not profile.tools_verified_for(selected_model):
                raise ChatConfigurationError(
                    "command execution requires successful verification for the exact "
                    f"selected model {selected_model!r}"
                )
            if request.tools_enabled and self.automation_tool_platform is None:
                raise ChatConfigurationError(
                    "automation command runtime is unavailable"
                )
            if (
                not request.tools_enabled
                and (mcp_profiles or ssh_environments)
                and self.tool_platform is None
            ):
                raise ChatConfigurationError("MCP runtime is unavailable")
            if not provider.config.local:
                if not profile.privacy.permits_sensitive_data:
                    raise ChatPrivacyError(
                        "provider profile does not permit command-result transfer"
                    )
                if not allow_cloud_tool_results:
                    raise ChatPrivacyError(
                        "cloud command-result transfer requires explicit confirmation "
                        "for this turn"
                    )
            turn_id = str(uuid4())
            try:
                extra_components = (
                    self.tool_platform.chat_components(
                        engagement_id=engagement_id,
                        turn_id=turn_id,
                        provider=provider,
                        model=selected_model,
                        mcp_profiles=mcp_profiles,
                        ssh_environments=ssh_environments,
                        include_oci=False,
                        allow_empty=True,
                    )
                    if (mcp_profiles or ssh_environments)
                    and self.tool_platform is not None
                    else None
                )
                if request.tools_enabled:
                    assert self.automation_tool_platform is not None
                    tool_components = self.automation_tool_platform.chat_components(
                        engagement_id=engagement_id,
                        extra_components=extra_components,
                    )
                elif extra_components is not None:
                    tool_components = extra_components
                elif (
                    browser_session_id is None
                    and not model_context
                    and not skill_resources_selected
                    and not subagents_enabled
                ):
                    raise ChatConfigurationError(
                        "no runtime capabilities were selected"
                    )
                if browser_session_id is not None:
                    browser_session = self.store.get(BrowserSession, browser_session_id)
                    browser_components = (
                        companion_components(
                            self.store,
                            engagement_id,
                            browser_session_id,
                            artifact_store=self.artifact_store,
                            image_supported=profile.capabilities.vision,
                        )
                        if browser_session.metadata.get("browser_companion_version")
                        == 1
                        else self.browser_tool_platform.chat_components(
                            engagement_id=engagement_id,
                            browser_session_id=browser_session_id,
                        )
                    )
                    tool_components = combine_tool_components(
                        tool_components,
                        browser_components,
                    )
                if model_context and not browser_session_id:
                    tool_components = combine_tool_components(
                        tool_components,
                        standalone_components(self.store, engagement_id),
                    )
                if skill_resources_selected:
                    skill_components = skill_resource_components(
                        skill_snapshots,
                        engagement_id=engagement_id,
                        workspace=self.workspace_resolver(engagement_id),
                        scope=tool_components.scope if tool_components else None,
                    )
                    if skill_components is not None:
                        tool_components = combine_tool_components(
                            tool_components, skill_components
                        )
                if subagents_enabled:
                    tool_components = combine_tool_components(
                        tool_components,
                        subagent_components(
                            self.subagents,
                            engagement_id=engagement_id,
                            workspace=(
                                tool_components.workspace
                                if tool_components is not None
                                else Path(
                                    (engagement.workspace_path if engagement else None)
                                    or "."
                                ).resolve()
                            ),
                            scope=tool_components.scope if tool_components else None,
                        ),
                    )
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.chat.caught_failure_003",
                    "A handled chat operation raised an exception.",
                    exc,
                    stage="chat",
                )
                raise ChatConfigurationError(str(exc)) from exc
            if tool_components is None:
                raise ChatConfigurationError(
                    "no command, automation, MCP, or browser runtime capabilities were selected"
                )
            tool_suggestions: dict[str, Any] | None = None
            tool_catalog: dict[str, Any] | None = None
            deferred_specs = (
                deferrable_specs(
                    tool_components.specs,
                    always_loaded=tool_components.scope.always_loaded_tools,
                )
                if on_demand_enabled(tool_components.scope)
                else {}
            )
            if deferred_specs:
                operator_messages = [
                    *(
                        item.content
                        for item in stored_messages
                        if item.role == ChatRole.USER
                    ),
                    *(
                        item.content
                        for item in durable_incoming
                        if item.role == ChatRole.USER
                    ),
                ]
                tool_index = self._tool_index()
                catalog_receipt: CatalogReceipt | None = None
                if suggestions_enabled(tool_components.scope):
                    receipt = await suggest_tools(
                        self.tool_suggestion_client(),
                        deferred=deferred_specs,
                        operator_messages=operator_messages,
                        skills=skill_snapshots,
                    )
                    tool_suggestions = receipt.model_dump(mode="json")
                    if receipt.status != "unavailable":
                        catalog_receipt = CatalogReceipt(
                            deferred=receipt.deferred,
                            preloaded=receipt.preloaded,
                            suggested=receipt.suggested,
                            ranker="jev",
                        )
                if catalog_receipt is None:
                    # Local ranking; also the fallback when Jev is unavailable.
                    catalog_receipt = await asyncio.to_thread(
                        rank_for_request,
                        tool_index,
                        deferred_specs,
                        next(
                            (
                                text
                                for text in reversed(operator_messages)
                                if text.strip()
                            ),
                            "",
                        ),
                    )
                catalog = catalog_components(
                    tool_components,
                    deferred=catalog_receipt.deferred,
                    index=tool_index,
                )
                if catalog is not None:
                    tool_components = combine_tool_components(tool_components, catalog)
                tool_catalog = catalog_receipt.model_dump(mode="json")
            session_id = (
                session.id
                if session is not None
                else pending_session.id
                if pending_session is not None
                else ""
            )
            turn = ChatTurn(
                id=turn_id,
                engagement_id=engagement_id,
                session_id=session_id,
                goal_id=request.goal_id,
                provider_profile_id=profile.id,
                model=selected_model,
                tools_enabled=True,
                max_artifact_queries=request.max_artifact_queries,
                scope_policy_id=tool_components.scope.id,
                scope_revision=tool_components.scope.revision,
                request_snapshot={
                    "operator_decisions": operator_decisions,
                    "skill_snapshots": [
                        item.model_dump(mode="json") for item in skill_snapshots
                    ],
                    "project_instructions": (
                        project_instructions.receipt()
                        if project_instructions is not None
                        else None
                    ),
                    "hook_snapshots": [
                        item.model_dump(mode="json") for item in hook_snapshots
                    ],
                    "model_request": model_request.model_dump(mode="json"),
                    "citations": [item.model_dump(mode="json") for item in citations],
                    "context_usage": context_usage.model_dump(mode="json"),
                    "mcp_server_ids": [item.id for item in mcp_profiles],
                    "mcp_snapshot": [
                        item.model_dump(mode="json") for item in mcp_profiles
                    ],
                    "ssh_environment_snapshot": [
                        item.model_dump(mode="json") for item in ssh_environments
                    ],
                    "include_oci_tools": request.tools_enabled,
                    "browser_session_id": browser_session_id,
                    "application_model_context": model_context,
                    "allow_subagents": subagents_enabled,
                    "tool_suggestions": tool_suggestions,
                    "tool_catalog": tool_catalog,
                    "automation_runtime_digest": getattr(
                        tool_components, "runtime_digest", None
                    ),
                },
            )
        elif engagement_id is not None and (
            request.stream or goal is not None or hook_snapshots
        ):
            session_id = (
                session.id
                if session is not None
                else pending_session.id
                if pending_session is not None
                else ""
            )
            turn = ChatTurn(
                id=str(uuid4()),
                engagement_id=engagement_id,
                session_id=session_id,
                goal_id=request.goal_id,
                provider_profile_id=profile.id,
                model=selected_model,
                tools_enabled=False,
                request_snapshot={
                    "operator_decisions": operator_decisions,
                    "skill_snapshots": [
                        item.model_dump(mode="json") for item in skill_snapshots
                    ],
                    "project_instructions": (
                        project_instructions.receipt()
                        if project_instructions is not None
                        else None
                    ),
                    "hook_snapshots": [
                        item.model_dump(mode="json") for item in hook_snapshots
                    ],
                    "model_request": model_request.model_dump(mode="json"),
                    "citations": [item.model_dump(mode="json") for item in citations],
                    "context_usage": context_usage.model_dump(mode="json"),
                    "include_oci_tools": False,
                },
            )
        try:
            resolved_model = provider.require(model_request)
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.chat.caught_failure_004",
                "A handled chat operation raised an exception.",
                exc,
                stage="chat",
            )
            raise ChatConfigurationError(str(exc)) from exc
        prepared = PreparedChat(
            provider=provider,
            provider_profile=profile,
            model_request=model_request,
            resolved_model=resolved_model,
            citations=citations,
            engagement_id=engagement_id,
            session=session,
            pending_session=pending_session,
            stored_messages=stored_messages,
            new_messages=new_messages,
            context_attachments=list(request.context_attachments),
            context_usage=context_usage,
            context_snapshot=context_snapshot,
            tools_enabled=tools_enabled,
            tool_components=tool_components,
            turn=turn,
            queue_claim=request._queue_claim,
            operator_decisions=operator_decisions,
            source_request=request,
            base_instructions=base_instructions,
            required_parameters={"tools"} if switch_tools_enabled else set(),
            hook_snapshots=hook_snapshots,
        )
        if turn is not None:
            self._persist_turn_inputs(prepared)
        return prepared

    def _fail_closed_turn(
        self,
        prepared: PreparedChat,
        *,
        status: ChatTurnStatus,
        error: str,
    ) -> None:
        """Mark a durable turn terminal so a failed complete() cannot wedge the session."""

        turn = prepared.turn
        if turn is None:
            return
        try:
            latest = self.store.get(ChatTurn, turn.id)
        except (
            NotFoundError
        ):  # diagnostic-expected: turn already deleted; nothing to fail closed
            return
        if latest.status in {
            ChatTurnStatus.COMPLETE,
            ChatTurnStatus.CANCELLED,
            ChatTurnStatus.FAILED,
            ChatTurnStatus.WAITING_APPROVAL,
            ChatTurnStatus.WAITING_CALLBACK,
        }:
            return
        claim_id = prepared.execution_claim_id
        if claim_id is not None and latest.execution_claim_id not in {None, claim_id}:
            return
        prepared.turn = self.store.update(
            ChatTurn,
            latest.id,
            {"status": status, "error": error[:1_000]},
            expected_revision=latest.revision,
        )
        self._release_execution(prepared)

    async def complete(self, prepared: PreparedChat) -> ChatCompletionResponse:
        try:
            return await self._complete_claimed(prepared)
        except asyncio.CancelledError:
            await self._run_terminal_native_hooks(
                prepared, "chat.turn.cancelled", "response stopped"
            )
            self._fail_closed_turn(
                prepared,
                status=ChatTurnStatus.CANCELLED,
                error="response stopped",
            )
            raise
        except BaseException as exc:
            await self._run_terminal_native_hooks(
                prepared, "chat.turn.failed", str(exc)[:1_000]
            )
            self._fail_closed_turn(
                prepared,
                status=ChatTurnStatus.FAILED,
                error=str(exc)[:1_000],
            )
            raise

    async def _complete_claimed(self, prepared: PreparedChat) -> ChatCompletionResponse:
        self._claim_execution(prepared)
        await self._run_native_hooks(prepared, "chat.turn.started")
        if prepared.tools_enabled:
            completed: ChatCompletionResponse | None = None
            async for event, payload in self.stream(prepared):
                if event == "approval_required":
                    raise ChatError("command response is waiting for operator approval")
                if event == "done":
                    body = dict(payload)
                    body.pop("type", None)
                    completed = ChatCompletionResponse.model_validate(body)
            if completed is None:
                raise ChatError("command response ended before final synthesis")
            return completed
        request = self._fit_turn_goal_request(prepared, prepared.model_request)
        response = await self._complete_with_context_recovery(prepared, request)
        if prepared.turn is not None:
            self._assert_execution_owner(prepared)
        completion = self._completion(prepared, response)
        await self._run_native_hooks(
            prepared,
            "chat.turn.completed",
            {"finish_reason": completion.finish_reason},
        )
        self._persist(prepared, completion)
        self.start_optional_naming(
            self._name_initial_session(prepared, completion.message.content)
        )
        self._complete_turn(prepared, completion)
        self._release_execution(prepared)
        return completion

    async def _complete_with_context_recovery(
        self, prepared: PreparedChat, request: ModelRequest
    ) -> ModelResponse:
        try:
            return await prepared.provider.complete(request)
        except ProviderContextLengthError:
            retry = await self._recover_context_length_rejection(prepared, request)
            try:
                return await prepared.provider.complete(retry)
            except ProviderContextLengthError as exc:
                raise ChatConfigurationError(
                    "the provider rejected the compacted request context; reduce "
                    "mandatory instructions or configure a lower context cap"
                ) from exc

    async def _stream_with_context_recovery(
        self, prepared: PreparedChat, request: ModelRequest
    ) -> AsyncIterator[Any]:
        attempted_recovery = False
        while True:
            output_started = False
            try:
                async for event in prepared.provider.stream(request):
                    if (
                        event.type == StreamEventType.ERROR
                        and event.context_length_exceeded
                    ):
                        raise ProviderContextLengthError(
                            event.error or "provider rejected the request context"
                        )
                    if event.type in {
                        StreamEventType.TEXT_DELTA,
                        StreamEventType.REASONING_DELTA,
                        StreamEventType.TOOL_CALL,
                    }:
                        output_started = True
                    yield event
                return
            except ProviderContextLengthError:
                if attempted_recovery:
                    raise ChatConfigurationError(
                        "the provider rejected the compacted request context; reduce "
                        "mandatory instructions or configure a lower context cap"
                    )
                if output_started:
                    raise ChatConfigurationError(
                        "the provider rejected the request context after output began; "
                        "Nebula did not retry the partial response"
                    )
                request = await self._recover_context_length_rejection(
                    prepared, request
                )
                attempted_recovery = True

    async def _recover_context_length_rejection(
        self, prepared: PreparedChat, failed_request: ModelRequest
    ) -> ModelRequest:
        """Refresh exact limits and rebuild canonical context for one safe retry."""

        turn = prepared.turn
        if turn is not None and (turn.execution_tool_calls or turn.tool_history):
            raise ChatConfigurationError(
                "the provider rejected the request context after tool routing began; "
                "Nebula will not repeat tool work"
            )
        if prepared.session is None or prepared.source_request is None:
            raise ChatConfigurationError(
                "the provider rejected the request context and the durable conversation "
                "could not be reassembled safely"
            )
        refreshed = await self._refresh_context_metadata(
            prepared.provider_profile.id, prepared.provider, prepared.resolved_model
        )
        canonical = self._session_messages(prepared.session)
        messages = [
            ChatRequestMessage(
                role=item.role,
                content=item.content,
                content_blocks=item.content_blocks,
            )
            for item in canonical
        ]
        goal = self.store.get(ChatGoal, turn.goal_id) if turn and turn.goal_id else None
        budget = ContextCallBudget(
            max_tokens=(
                max(0, goal.token_budget - goal.usage.total_tokens)
                if goal is not None and goal.token_budget is not None
                else None
            )
        )
        try:
            (
                model_messages,
                instructions,
                usage,
                snapshot,
                session,
            ) = await self._model_context(
                request=prepared.source_request,
                profile=refreshed,
                provider=prepared.provider,
                model=prepared.resolved_model,
                messages=messages,
                stored_messages=canonical,
                session=prepared.session,
                instructions=prepared.base_instructions,
                budget=budget,
                required_parameters=prepared.required_parameters,
            )
        except (ContextCapacityError, ContextCompactionError) as exc:
            raise ChatConfigurationError(
                "the provider rejected the request context; refreshed limits show "
                f"that mandatory input still cannot fit: {exc}"
            ) from exc
        if goal is not None and usage.total_tokens:
            goal = self._charge_goal(
                goal.id,
                usage,
                exhausted_reason="Token budget exhausted during context recovery.",
            )
            if goal.status != ChatGoalStatus.RUNNING:
                raise ChatConfigurationError(
                    "goal token budget was exhausted during context recovery"
                )
        limits = resolve_context_limits(
            refreshed,
            model=prepared.resolved_model,
            requested_output_tokens=prepared.source_request.max_output_tokens,
            required_parameters=prepared.required_parameters,
        )
        metadata = {
            **failed_request.metadata,
            "resolved_context_limits": json.dumps(
                limits.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
            "context_length_recovery": "1",
        }
        recovered_base = prepared.model_request.model_copy(
            update={
                "instructions": instructions,
                "messages": [
                    ModelMessage(
                        role=item.role.value,
                        content=self._model_content(item, prepared.engagement_id),
                    )
                    for item in model_messages
                ],
                "max_output_tokens": limits.max_output_tokens,
                "metadata": metadata,
            }
        )
        retry = recovered_base.model_copy(
            update={
                "instructions": (
                    _CHAT_TOOL_INSTRUCTIONS + "\n\n" + instructions
                    if failed_request.tools
                    else instructions
                ),
                "tools": failed_request.tools,
                "tool_results": failed_request.tool_results,
                "tool_choice": failed_request.tool_choice,
                "parallel_tool_calls": failed_request.parallel_tool_calls,
            }
        )
        self._ensure_request_capacity(refreshed, retry)
        prepared.provider_profile = refreshed
        prepared.model_request = recovered_base
        prepared.context_snapshot = snapshot
        prepared.context_usage = ChatTokenUsage(
            input_tokens=prepared.context_usage.input_tokens + usage.input_tokens,
            output_tokens=prepared.context_usage.output_tokens + usage.output_tokens,
            total_tokens=prepared.context_usage.total_tokens + usage.total_tokens,
        )
        prepared.session = session or prepared.session
        if turn is not None:
            latest = self._refresh_turn(turn)
            prepared.turn = self.store.update(
                ChatTurn,
                latest.id,
                {
                    "request_snapshot": {
                        **latest.request_snapshot,
                        "model_request": recovered_base.model_dump(mode="json"),
                        "context_usage": prepared.context_usage.model_dump(mode="json"),
                        "context_length_recovery": {
                            "attempted": True,
                            "metadata_revision": limits.metadata_revision,
                        },
                    }
                },
                expected_revision=latest.revision,
            )
        return retry

    async def _verify_openrouter_route_limits(
        self, profile: ProviderProfile, provider: ModelProvider, model: str
    ) -> ProviderProfile:
        """Load unverified OpenRouter endpoint limits before sizing the context.

        Without them the model is held to the conservative 8K cap, which is far
        below the window most OpenRouter models actually serve.
        """

        if profile.provider_type != "openrouter":
            return profile
        descriptor = find_model_descriptor(
            profile.metadata.get("model_descriptors"), model
        )
        if route_limits_verified(descriptor, model):
            return profile
        if getattr(provider, "openrouter_route_limits", None) is None:
            return profile
        try:
            return await self._refresh_context_metadata(profile.id, provider, model)
        except (ChatConfigurationError, ConflictError) as exc:
            record_caught_exception(
                "chat",
                "chat.chat.openrouter_route_limits_unverified",
                "OpenRouter endpoint limits could not be verified; the conservative "
                "context cap stays in place.",
                exc,
                stage="chat",
            )
            return self.store.get(ProviderProfile, profile.id)

    async def _refresh_context_metadata(
        self, profile_id: str, provider: ModelProvider, model: str
    ) -> ProviderProfile:
        profile = self.store.get(ProviderProfile, profile_id)
        metadata = dict(profile.metadata)
        descriptors = [
            dict(item)
            for item in metadata.get("model_descriptors", [])
            if isinstance(item, dict)
        ]
        descriptor = next(
            (item for item in descriptors if item.get("id") == model),
            None,
        )
        if descriptor is None:
            descriptor = {"id": model, "name": model}
            descriptors.append(descriptor)
        checked_at = utc_now().isoformat()
        if profile.provider_type == "openrouter":
            loader = getattr(provider, "openrouter_route_limits", None)
            if loader is None:
                raise ChatConfigurationError(
                    "the provider rejected the request context and exact endpoint "
                    "limits cannot be refreshed"
                )
            # An alias exposes no endpoints; its target carries the real routes.
            discovery_model = route_discovery_model(descriptor, model)
            try:
                routes = await asyncio.wait_for(loader(discovery_model), 15)
            except Exception as exc:
                raise ChatConfigurationError(
                    "the provider rejected the request context and exact endpoint "
                    "limits could not be refreshed; refresh the provider and retry"
                ) from exc
            descriptor.update(
                {
                    "route_limits": [item.model_dump(mode="json") for item in routes],
                    "route_limits_verified": True,
                    "route_limits_checked_at": checked_at,
                    "route_limits_error": None,
                    "route_limits_source_model": discovery_model,
                }
            )
            revision_payload: Any = descriptor["route_limits"]
        else:
            try:
                health = await asyncio.wait_for(provider.health(), 15)
            except Exception as exc:
                raise ChatConfigurationError(
                    "the provider rejected the request context and model metadata "
                    "could not be refreshed; refresh the provider and retry"
                ) from exc
            exact = next(
                (item for item in health.model_descriptors if item.id == model),
                None,
            )
            if exact is None:
                raise ChatConfigurationError(
                    "the provider rejected the request context and returned no exact "
                    "model limits; configure a lower context cap and retry"
                )
            descriptor.update(exact.model_dump(mode="json", exclude_none=True))
            revision_payload = descriptor
        metadata["model_descriptors"] = descriptors
        metadata["route_catalog_revision"] = hashlib.sha256(
            json.dumps(
                {
                    "model": model,
                    "checked_at": checked_at,
                    "limits": revision_payload,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return self.store.update(
            ProviderProfile,
            profile.id,
            {"metadata": metadata},
            expected_revision=profile.revision,
        )

    async def stream(
        self, prepared: PreparedChat
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        self._claim_execution(prepared)
        await self._run_native_hooks(prepared, "chat.turn.started")
        yield (
            "started",
            {
                "type": "started",
                "turn_id": prepared.turn.id if prepared.turn is not None else None,
                "provider_id": prepared.provider_profile.id,
                "model": prepared.resolved_model,
                "session_id": self._session_id(prepared),
            },
        )
        if prepared.tools_enabled or (
            prepared.turn is not None
            and prepared.turn.status
            in {ChatTurnStatus.WAITING_APPROVAL, ChatTurnStatus.WAITING_CALLBACK}
        ):
            async for item in self._stream_tool_turn(prepared):
                yield item
            return
        completed = False
        request = self._fit_turn_goal_request(prepared, prepared.model_request)
        async for event in self._stream_with_context_recovery(prepared, request):
            if event.type == StreamEventType.STARTED:
                continue
            if event.type == StreamEventType.REASONING_DELTA:
                yield (
                    "reasoning_delta",
                    {
                        "type": "reasoning_delta",
                        "provider_id": prepared.provider_profile.id,
                        "model": prepared.resolved_model,
                        "delta": event.delta or "",
                    },
                )
                continue
            if event.type == StreamEventType.TEXT_DELTA:
                yield (
                    "delta",
                    {
                        "type": "delta",
                        "provider_id": prepared.provider_profile.id,
                        "model": prepared.resolved_model,
                        "delta": event.delta or "",
                    },
                )
                continue
            if event.type == StreamEventType.TOOL_CALL:
                raise ChatError(
                    "provider returned a tool call even though chat exposes no tools"
                )
            if event.type == StreamEventType.ERROR:
                raise ChatError(event.error or "provider stream failed")
            if event.type == StreamEventType.COMPLETED:
                if prepared.turn is not None:
                    self._assert_execution_owner(prepared)
                if event.response is None:
                    raise ChatError("provider stream completed without a response")
                completion = self._completion(prepared, event.response)
                await self._run_native_hooks(
                    prepared,
                    "chat.turn.completed",
                    {"finish_reason": completion.finish_reason},
                )
                self._persist(prepared, completion)
                self.start_optional_naming(
                    self._name_initial_session(prepared, completion.message.content)
                )
                self._complete_turn(prepared, completion)
                self._release_execution(prepared)
                payload = completion.model_dump(mode="json")
                payload["type"] = "done"
                yield "done", payload
                completed = True
        if not completed:
            raise ChatError("provider stream ended before completion")

    async def _stream_tool_turn(
        self, prepared: PreparedChat
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        turn = prepared.turn
        components = prepared.tool_components
        if turn is None or components is None or prepared.engagement_id is None:
            raise ChatError("command response is missing its durable runtime lock")
        try:
            turn = self._refresh_turn(turn)
            if turn.status == ChatTurnStatus.WAITING_APPROVAL:
                async for item in self._resume_pending_call(prepared, turn, components):
                    if item[0] == "_continued":
                        turn = self._refresh_turn(turn)
                        continue
                    yield item
                turn = self._refresh_turn(turn)
                if turn.status == ChatTurnStatus.WAITING_APPROVAL:
                    prepared.turn = turn
                    self._release_execution(prepared)
                    return
            if turn.status == ChatTurnStatus.WAITING_CALLBACK:
                async for item in self._resume_callback_result(prepared, turn):
                    yield item
                turn = self._refresh_turn(turn)
                if turn.status == ChatTurnStatus.WAITING_CALLBACK:
                    prepared.turn = turn
                    self._release_execution(prepared)
                    return

            catalog_receipt = catalog_snapshot(turn.request_snapshot)
            deferred_names = set(catalog_receipt.get("deferred", []))
            # Calls a routing response batched and Core has not run yet. The
            # queue is deliberately not durable: a pause abandons it, and the
            # model re-issues the calls it still wants, because an abandoned
            # call never executed and never enters the replayed history.
            batched_calls: list[tuple[ModelToolCall, dict[str, Any] | None]] = []
            while turn.status != ChatTurnStatus.FINALIZING:
                budgeted_specs = [
                    spec
                    for spec in components.specs.values()
                    if (
                        spec.budget_class == "artifact_query"
                        and (
                            turn.max_artifact_queries is None
                            or turn.artifact_queries < turn.max_artifact_queries
                        )
                    )
                    or (
                        spec.budget_class == "execution"
                        and (
                            turn.max_tool_calls is None
                            or turn.execution_tool_calls < turn.max_tool_calls
                        )
                    )
                ]
                # Deferred tools never enter the function list, so it stays
                # identical across steps and turns and provider prefix caches
                # hold. They run through tool_catalog.call, or by a direct call
                # to their name, within the same budgets.
                available_specs = [
                    spec for spec in budgeted_specs if spec.name not in deferred_names
                ]
                if not available_specs:
                    break
                budgeted_names = {spec.name for spec in budgeted_specs}
                if not batched_calls:
                    routing = prepared.model_request.model_copy(
                        update={
                            "instructions": _CHAT_TOOL_INSTRUCTIONS
                            + (
                                SUBAGENT_ROUTING_INSTRUCTIONS
                                if any(
                                    spec.name == "start_subagent"
                                    for spec in available_specs
                                )
                                else ""
                            )
                            + "\n\n"
                            + (prepared.model_request.instructions or "")
                            + catalog_instructions(catalog_receipt, components.specs),
                            "tools": [
                                ToolDefinition(
                                    name=spec.name,
                                    description=spec.description,
                                    input_schema=_routing_input_schema(spec),
                                    # The call envelope's arguments are
                                    # free-form; the real tool's schema is
                                    # enforced by Core.
                                    strict=spec.name != CATALOG_CALL,
                                )
                                for spec in sorted(
                                    available_specs, key=lambda item: item.name
                                )
                            ]
                            + [self._finish_tool()],
                            "tool_choice": ToolChoice.REQUIRED,
                            # A model may batch independent calls into one
                            # routing response. Core still executes them one
                            # at a time, in the requested order, so every
                            # call keeps its own step, budget, and approval.
                            "parallel_tool_calls": True,
                            "tool_results": self._provider_tool_history(turn),
                            "messages": self._browser_screenshot_messages(
                                prepared, turn
                            ),
                        }
                    )
                    routing = self._fit_turn_goal_request(prepared, routing)
                    self._ensure_request_capacity(prepared.provider_profile, routing)
                    response = await self._complete_with_context_recovery(
                        prepared, routing
                    )
                    self._assert_execution_owner(prepared)
                    turn = self._refresh_turn(turn)
                    turn = self._add_usage(turn, response)
                    if (
                        turn.goal_id is not None
                        and self.store.get(ChatGoal, turn.goal_id).status
                        != ChatGoalStatus.RUNNING
                    ):
                        raise ChatError(
                            "goal token budget was exhausted before tool execution"
                        )
                    if response.text.strip():
                        raise ChatError(
                            "provider returned routing prose instead of a tool call"
                        )
                    if not response.tool_calls:
                        # A required tool choice the provider ignored. The
                        # results already gathered are a better answer than a
                        # failed turn, so finish on what the turn has.
                        record_diagnostic(
                            "warning",
                            "chat",
                            "chat.routing.empty_tool_batch",
                            "The provider returned no tool call for a required "
                            "routing step; the turn answered from the results "
                            "it already had.",
                            outcome="fallback",
                            stage="chat",
                            retryable=True,
                            safe_failure_cause=(
                                "The provider returned neither a tool call nor "
                                "prose for a required tool choice."
                            ),
                        )
                        break
                    # Every call is validated before any of them executes, so
                    # a malformed batch never reaches the broker.
                    batched_calls = self._routing_batch(
                        response, turn, budgeted_names, deferred_names
                    )
                call, provider_call = batched_calls.pop(0)
                if call.name == "finish_response":
                    break
                if call.name not in budgeted_names:
                    # An earlier call in the batch consumed this budget class.
                    # Drop the queued remainder and route again with the tools
                    # the turn can still afford; the model re-issues what it
                    # still needs.
                    batched_calls = []
                    continue
                self._assert_execution_owner(prepared)
                spec = components.specs[call.name]
                if "cwd" in spec.path_arguments:
                    call = call.model_copy(
                        update={"arguments": {**call.arguments, "cwd": "."}}
                    )
                normalized_arguments = _normalize_routing_arguments(
                    components, spec, call.arguments
                )
                call = call.model_copy(update={"arguments": normalized_arguments})
                step = turn.next_step
                idempotency_key = f"chat:{turn.id}:step:{step}"
                durable_call_id = str(
                    uuid5(NAMESPACE_URL, f"nebula:{turn.id}:{idempotency_key}")
                )
                yield (
                    "tool_started",
                    {
                        "type": "tool_started",
                        "turn_id": turn.id,
                        "tool_call_id": durable_call_id,
                        "capability": call.name,
                        "arguments": call.arguments,
                        "step": step,
                    },
                )
                invocation = ToolInvocation(
                    engagement_id=prepared.engagement_id,
                    run_id=turn.id,
                    origin=ToolCallOrigin.CHAT,
                    chat_session_id=turn.session_id,
                    chat_turn_id=turn.id,
                    tool_name=call.name,
                    arguments=call.arguments,
                    workspace=components.workspace,
                    idempotency_key=idempotency_key,
                    requested_by="chat-assistant",
                    provider_call_id=call.id,
                    provider_step=step,
                )
                entry = {
                    "step": step,
                    "model_call_id": call.id,
                    "tool_call_id": durable_call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "budget_class": spec.budget_class,
                }
                if provider_call is not None:
                    entry["provider_call"] = provider_call
                try:
                    if (
                        call.name in CATALOG_DISCOVERY_NAMES
                        and discovery_calls(turn.tool_history)
                        >= MAX_CATALOG_CALLS_PER_TURN
                    ):
                        # Refused rather than removed from the function list,
                        # which would change the cached request prefix.
                        raise InvalidToolArguments(
                            "the catalog search limit for this turn is reached; "
                            "use a tool already loaded or finish_response"
                        )
                    result = await components.broker.execute(
                        invocation, components.scope
                    )
                except ApprovalRequired as paused:
                    record_caught_exception(
                        "chat",
                        "chat.chat.caught_failure_005",
                        "A handled chat operation raised an exception.",
                        paused,
                        stage="chat",
                    )
                    entry.update(
                        {
                            "status": "waiting_approval",
                            "approval_id": paused.approval.id,
                        }
                    )
                    turn = self._save_tool_step(
                        turn,
                        entry,
                        status=ChatTurnStatus.WAITING_APPROVAL,
                        approval_id=paused.approval.id,
                    )
                    yield (
                        "approval_required",
                        {
                            "type": "approval_required",
                            "turn_id": turn.id,
                            "tool_call_id": durable_call_id,
                            "approval": paused.approval.model_dump(mode="json"),
                        },
                    )
                    prepared.turn = turn
                    self._release_execution(prepared)
                    return
                except SubagentWaitPending as waiting:  # diagnostic-expected: subagent wait is control flow that pauses the turn durably
                    summary = (
                        f"Waiting for {len(waiting.subagent_ids)} subagent"
                        f"{'' if len(waiting.subagent_ids) == 1 else 's'} to report."
                    )
                    entry.update(
                        {
                            "status": "waiting_callback",
                            "subagent_wait": {
                                "ids": waiting.subagent_ids,
                                "mode": waiting.mode,
                            },
                            "result_summary": summary,
                        }
                    )
                    turn = self._save_tool_step(
                        turn, entry, status=ChatTurnStatus.WAITING_CALLBACK
                    )
                    yield (
                        "callback_required",
                        {
                            "type": "callback_required",
                            "turn_id": turn.id,
                            "tool_call_id": durable_call_id,
                            "subagent_ids": waiting.subagent_ids,
                            "summary": summary,
                        },
                    )
                    prepared.turn = turn
                    self._release_execution(prepared)
                    return
                except PolicyDenied as exc:
                    record_caught_exception(
                        "chat",
                        "chat.chat.caught_failure_006",
                        "A handled chat operation raised an exception.",
                        exc,
                        stage="chat",
                    )
                    provider_result = self._bounded_tool_error(
                        "denied", exc.decision.reason
                    )
                    entry.update(
                        {"status": "denied", "provider_result": provider_result}
                    )
                except Exception as exc:
                    record_caught_exception(
                        "chat",
                        "chat.chat.caught_failure_007",
                        "A handled chat operation raised an exception.",
                        exc,
                        stage="chat",
                    )
                    provider_result = self._bounded_tool_error(
                        "failed", f"{type(exc).__name__}: {str(exc)}"
                    )
                    entry.update(
                        {"status": "failed", "provider_result": provider_result}
                    )
                else:
                    provider_result = serialize_model_result(result.model_result())
                    result_failed = self._tool_result_failed(result)
                    waiting_callback = bool(
                        result.receipt
                        and result.receipt.results_url
                        and result.receipt.results_api_key
                    )
                    entry.update(
                        {
                            "status": (
                                "waiting_callback"
                                if waiting_callback
                                else "failed"
                                if result_failed
                                else "complete"
                            ),
                            "provider_result": provider_result,
                            "trusted_result": result.receipt is None,
                            "evidence_ids": result.evidence_ids,
                            "result_artifact_id": result.result_artifact_id,
                            "artifacts": result.model_result().get("artifacts", []),
                            "result_summary": self._result_summary(
                                result.model_result()
                            ),
                            "process_id": (
                                result.receipt.process_id if result.receipt else None
                            ),
                            "results_url": (
                                result.receipt.results_url if result.receipt else None
                            ),
                        }
                    )
                    receipt = result.receipt
                    if waiting_callback and receipt is not None:
                        turn = self._save_tool_step(
                            turn,
                            entry,
                            status=ChatTurnStatus.WAITING_CALLBACK,
                        )
                        yield (
                            "callback_required",
                            {
                                "type": "callback_required",
                                "turn_id": turn.id,
                                "tool_call_id": durable_call_id,
                                "process_id": receipt.process_id,
                                "results_url": receipt.results_url,
                                "summary": entry.get("result_summary")
                                or "Waiting for the command to POST results.",
                            },
                        )
                        prepared.turn = turn
                        self._release_execution(prepared)
                        return
                turn = self._save_tool_step(turn, entry)
                yield (
                    "tool_completed",
                    {
                        "type": "tool_completed",
                        "turn_id": turn.id,
                        "tool_call_id": durable_call_id,
                        "capability": call.name,
                        "status": entry["status"],
                        "summary": entry.get("result_summary")
                        or entry["provider_result"],
                        "evidence_ids": entry.get("evidence_ids", []),
                        "result_artifact_id": entry.get("result_artifact_id"),
                        "artifacts": entry.get("artifacts", []),
                        "receipt": _decoded_result(entry.get("provider_result")),
                        "step": step,
                    },
                )

            turn = self.store.update(
                ChatTurn,
                turn.id,
                {"status": ChatTurnStatus.FINALIZING, "approval_id": None},
                expected_revision=turn.revision,
            )
            operator_help_chunks = self._tool_operator_help(prepared, turn)
            known_citations = {
                (citation.source_id, citation.chunk_id)
                for citation in prepared.citations
            }
            prepared.citations.extend(
                chunk.citation
                for chunk in operator_help_chunks
                if (chunk.citation.source_id, chunk.citation.chunk_id)
                not in known_citations
            )
            # Unused on-demand tools stay out of the synthesis inventory too.
            loaded_names = loaded_tool_names(catalog_receipt, turn.tool_history)
            final_request = prepared.model_request.model_copy(
                update={
                    "instructions": (
                        _CHAT_TOOL_RESULT_INSTRUCTIONS
                        + "\n\n"
                        + (prepared.model_request.instructions or "")
                        + _tool_inventory_instructions(
                            {
                                name: spec
                                for name, spec in components.specs.items()
                                if name not in deferred_names or name in loaded_names
                            }
                        )
                        + _reference_instructions(
                            operator_help_chunks, trusted_operator_help=True
                        )
                    ),
                    "tools": [],
                    "tool_choice": ToolChoice.AUTO,
                    "parallel_tool_calls": False,
                    "tool_results": self._provider_tool_history(turn),
                    "messages": self._browser_screenshot_messages(prepared, turn),
                }
            )
            final_request = self._fit_turn_goal_request(prepared, final_request)
            self._ensure_request_capacity(prepared.provider_profile, final_request)
            completed = False
            async for event in prepared.provider.stream(final_request):
                if event.type == StreamEventType.STARTED:
                    continue
                if event.type == StreamEventType.REASONING_DELTA:
                    yield (
                        "reasoning_delta",
                        {
                            "type": "reasoning_delta",
                            "turn_id": turn.id,
                            "provider_id": prepared.provider_profile.id,
                            "model": prepared.resolved_model,
                            "delta": event.delta or "",
                        },
                    )
                    continue
                if event.type == StreamEventType.TEXT_DELTA:
                    yield (
                        "delta",
                        {
                            "type": "delta",
                            "turn_id": turn.id,
                            "provider_id": prepared.provider_profile.id,
                            "model": prepared.resolved_model,
                            "delta": event.delta or "",
                        },
                    )
                    continue
                if event.type == StreamEventType.TOOL_CALL:
                    raise ChatError(
                        "final synthesis attempted an unauthorized tool call"
                    )
                if event.type == StreamEventType.ERROR:
                    raise ChatError(event.error or "provider final synthesis failed")
                if event.type == StreamEventType.COMPLETED:
                    if event.response is None:
                        raise ChatError("provider final synthesis omitted its response")
                    self._assert_execution_owner(prepared)
                    turn = self._refresh_turn(turn)
                    turn = self._add_usage(turn, event.response)
                    prepared.turn = turn
                    completion = self._completion(prepared, event.response)
                    self._persist(prepared, completion)
                    turn = prepared.turn or turn
                    self.start_optional_naming(
                        self._name_initial_session(prepared, completion.message.content)
                    )
                    turn = self.store.update(
                        ChatTurn,
                        turn.id,
                        {
                            "status": ChatTurnStatus.COMPLETE,
                            "final_message_id": completion.message.id,
                            "usage": turn.usage,
                        },
                        expected_revision=turn.revision,
                    )
                    prepared.turn = turn
                    self._release_execution(prepared)
                    payload = completion.model_dump(mode="json")
                    payload["type"] = "done"
                    yield "done", payload
                    completed = True
            if not completed:
                raise ChatError("provider stream ended before final synthesis")
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "chat",
                "chat.chat.caught_failure_008",
                "A handled chat operation raised an exception.",
                caught_error,
                stage="chat",
            )
            latest = self._refresh_turn(turn)
            if (
                latest.status
                not in {
                    ChatTurnStatus.COMPLETE,
                    ChatTurnStatus.CANCELLED,
                }
                and latest.execution_claim_id == prepared.execution_claim_id
            ):
                self.store.update(
                    ChatTurn,
                    latest.id,
                    {"status": ChatTurnStatus.CANCELLED, "error": "response stopped"},
                    expected_revision=latest.revision,
                )
                self._release_execution(prepared)
            raise

        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.chat.caught_failure_009",
                "A handled chat operation raised an exception.",
                exc,
                stage="chat",
            )
            latest = self._refresh_turn(turn)
            if (
                latest.status
                not in {
                    ChatTurnStatus.COMPLETE,
                    ChatTurnStatus.CANCELLED,
                    ChatTurnStatus.WAITING_APPROVAL,
                }
                and latest.execution_claim_id == prepared.execution_claim_id
            ):
                self.store.update(
                    ChatTurn,
                    latest.id,
                    {
                        "status": ChatTurnStatus.FAILED,
                        "error": str(exc)[:1_000],
                    },
                    expected_revision=latest.revision,
                )
                self._release_execution(prepared)
            raise

    @staticmethod
    def _finish_tool() -> ToolDefinition:
        return ToolDefinition(
            name="finish_response",
            description=(
                "Finish tool routing and produce the final analyst response. Use "
                "this immediately for greetings, conversation, or questions about "
                "the supplied capability list that require no execution."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            strict=True,
        )

    @staticmethod
    def _ensure_request_capacity(
        profile: ProviderProfile, request: ModelRequest
    ) -> None:
        limits = resolve_context_limits(
            profile,
            model=request.model,
            requested_output_tokens=request.max_output_tokens,
            required_parameters={"tools"} if request.tools else set(),
        )
        estimated = estimate_model_request(request)
        if estimated > limits.input_capacity:
            raise ChatConfigurationError(
                "the complete provider request exceeds the selected model context "
                f"capacity ({estimated} estimated input tokens > "
                f"{limits.input_capacity})"
            )

    def _fit_turn_goal_request(
        self, prepared: PreparedChat, request: ModelRequest
    ) -> ModelRequest:
        goal_id = prepared.turn.goal_id if prepared.turn is not None else None
        return self._fit_goal_request_budget(goal_id, request) if goal_id else request

    def _fit_goal_request_budget(
        self, goal_id: str, request: ModelRequest
    ) -> ModelRequest:
        """Reserve estimated input and bounded output inside remaining goal tokens."""

        goal = self.store.get(ChatGoal, goal_id)
        if goal.status != ChatGoalStatus.RUNNING:
            raise ChatConfigurationError(
                "goal must be running before provider dispatch"
            )
        if goal.token_budget is None:
            return request
        remaining = goal.token_budget - goal.usage.total_tokens
        available_output = remaining - estimate_model_request(request)
        if available_output < 1:
            paused_at = utc_now()
            self.store.update(
                ChatGoal,
                goal.id,
                {
                    "status": ChatGoalStatus.PAUSED,
                    "paused_at": paused_at,
                    "active_since": None,
                    "elapsed_seconds": goal.active_elapsed_seconds(paused_at),
                    "blocked_reason": (
                        "Goal token budget cannot fit the next provider request."
                    ),
                },
                expected_revision=goal.revision,
            )
            raise ChatConfigurationError(
                "goal token budget cannot fit the next provider request"
            )
        requested_output = request.max_output_tokens or available_output
        return request.model_copy(
            update={"max_output_tokens": min(requested_output, available_output)}
        )

    def _tool_operator_help(
        self, prepared: PreparedChat, turn: ChatTurn
    ) -> list[_RetrievedChunk]:
        failed_entries = [
            entry
            for entry in turn.tool_history
            if entry.get("status") in {"failed", "denied"}
        ]
        if not failed_entries:
            return []
        queries = [
            str(entry.get("provider_result", ""))
            for entry in failed_entries
            if entry.get("provider_result")
        ]
        # Bounded artifact searches after a failure can contain the exact
        # observed error excerpt needed to choose the matching runbook.
        queries.extend(
            str(entry.get("provider_result", ""))
            for entry in turn.tool_history
            if entry.get("budget_class") == "artifact_query"
            and entry.get("provider_result")
        )
        if not queries:
            return []
        token_budget = max(
            1,
            resolve_context_limits(
                prepared.provider_profile,
                model=prepared.model_request.model,
                requested_output_tokens=prepared.model_request.max_output_tokens,
            ).target_input_tokens
            // 5,
        )
        return self._retrieve_operator_help(queries, token_budget=token_budget)

    def _browser_screenshot_messages(
        self, prepared: PreparedChat, turn: ChatTurn
    ) -> list[ModelMessage]:
        messages = list(prepared.model_request.messages)
        if (
            self.artifact_store is None
            or not prepared.provider_profile.capabilities.vision
        ):
            return messages
        for entry in reversed(turn.tool_history):
            if (
                entry.get("name") != "browser.companion"
                or entry.get("status") != "complete"
            ):
                continue
            for reference in entry.get("artifacts", []):
                artifact_id = reference.get("artifact_id")
                if not isinstance(artifact_id, str):
                    continue
                artifact = self.store.get(Artifact, artifact_id)
                if (
                    artifact.engagement_id != turn.engagement_id
                    or artifact.source != "browser.companion"
                    or artifact.metadata.get("chat_session_id") != turn.session_id
                    or artifact.metadata.get("tool_call_id")
                    != entry.get("tool_call_id")
                    or artifact.media_type != "image/png"
                    or artifact.size > 4 * 1024 * 1024
                ):
                    raise ChatConfigurationError(
                        "Browser screenshot ownership could not be verified."
                    )
                messages.append(
                    ModelMessage(
                        role="user",
                        content=[
                            {
                                "type": "text",
                                "text": "Historical page screenshot captured by browser.companion.",
                            },
                            {
                                "type": "image",
                                "media_type": "image/png",
                                "data": base64.b64encode(
                                    self.artifact_store.read(artifact)
                                ).decode(),
                            },
                        ],
                    )
                )
                return messages
        return messages

    def _tool_index(self) -> ToolIndex | None:
        index = self.knowledge_index
        if not all(hasattr(index, method) for method in ("index_tools", "rank_tools")):
            return None
        if (
            index is not None
            and index.status.state == "required"
            and hasattr(index, "prepare_model")
            and self._embedding_warmup is None
        ):
            # On-demand search is on by default, so fetch the local model once
            # in the background instead of waiting for a document upload.
            # Until it is ready, catalog search uses keyword ranking.
            self._embedding_warmup = threading.Thread(
                target=self._warm_embedding_model,
                args=(index,),
                name="nebula-embedding-warmup",
                daemon=True,
            )
            self._embedding_warmup.start()
        return index  # type: ignore[return-value]

    @staticmethod
    def _warm_embedding_model(index: Any) -> None:
        try:
            index.prepare_model()
        except KnowledgeIndexError as exc:
            record_diagnostic(
                "warning",
                "chat",
                "chat.tool_catalog.embedding_unavailable",
                "The local embedding model could not be prepared; on-demand tool "
                "search will use keyword ranking.",
                outcome="fallback",
                stage="tool-catalog",
                retryable=True,
                safe_failure_cause="The local embedding model download or load failed.",
                exception=exc,
            )

    @staticmethod
    def _routing_batch(
        response: ModelResponse,
        turn: ChatTurn,
        budgeted_names: set[str],
        deferred_names: set[str],
    ) -> list[tuple[ModelToolCall, dict[str, Any] | None]]:
        """Validate a whole routing response before any of it reaches a broker.

        A response may carry several independent calls. Rejecting a malformed
        batch as a unit keeps the old guarantee that a bad routing step never
        produces a partial effect.
        """

        seen = {
            str(entry["model_call_id"])
            for entry in turn.tool_history
            if entry.get("model_call_id")
        }
        batch: list[tuple[ModelToolCall, dict[str, Any] | None]] = []
        for call in response.tool_calls:
            if call.id in seen:
                raise ChatError(
                    "provider repeated a completed tool call id; "
                    "refusing duplicate execution"
                )
            seen.add(call.id)
            if call.name == "finish_response":
                if call.arguments:
                    raise ChatError("finish_response does not accept arguments")
                # Finishing ends the turn, so calls queued behind it never run.
                batch.append((call, None))
                break
            provider_call: dict[str, Any] | None = None
            if call.name == CATALOG_CALL:
                target = unwrap_call(call.arguments, deferred_names)
                # An unknown target stays a catalog call; its broker
                # answers with an error the model can correct.
                if target is not None:
                    provider_call = {"name": call.name, "arguments": call.arguments}
                    call = call.model_copy(
                        update={"name": target[0], "arguments": target[1]}
                    )
            if call.name not in budgeted_names:
                raise ChatError(f"provider requested unavailable tool {call.name!r}")
            batch.append((call, provider_call))
        return batch

    @staticmethod
    def _provider_tool_history(turn: ChatTurn) -> list[ModelToolResult]:
        history: list[ModelToolResult] = []
        for entry in turn.tool_history:
            persisted = entry.get("provider_result")
            if not isinstance(persisted, (dict, str)):
                continue
            output = sanitize_model_history_result(
                persisted,
                tool_call_id=str(entry.get("tool_call_id") or entry["model_call_id"]),
                tool_name=str(entry["name"]),
                trusted_result=entry.get("trusted_result") is True,
            )
            # An on-demand tool runs under its own name, but the provider must
            # see the tool_catalog.call it actually issued.
            provider_call = entry.get("provider_call")
            issued = provider_call if isinstance(provider_call, dict) else entry
            history.append(
                ModelToolResult(
                    call_id=str(entry["model_call_id"]),
                    name=str(issued["name"]),
                    arguments=dict(issued.get("arguments") or {}),
                    output=output,
                    is_error=entry.get("status") != "complete",
                )
            )
        return history

    def _refresh_turn(self, turn: ChatTurn) -> ChatTurn:
        return self.store.get(ChatTurn, turn.id)

    def _add_usage(self, turn: ChatTurn, response: ModelResponse) -> ChatTurn:
        usage = ChatTokenUsage(
            input_tokens=turn.usage.input_tokens + response.usage.input_tokens,
            output_tokens=turn.usage.output_tokens + response.usage.output_tokens,
            total_tokens=turn.usage.total_tokens + response.usage.total_tokens,
        )
        updated = self.store.update(
            ChatTurn,
            turn.id,
            {"usage": usage},
            expected_revision=turn.revision,
        )
        if turn.goal_id:
            self._charge_goal(
                turn.goal_id,
                ChatTokenUsage.model_validate(response.usage.model_dump()),
            )
        return updated

    def _charge_goal(
        self,
        goal_id: str,
        usage: ChatTokenUsage,
        *,
        exhausted_reason: str = "Token budget exhausted during provider response.",
    ) -> ChatGoal:
        goal = self.store.get(ChatGoal, goal_id)
        combined = ChatTokenUsage(
            input_tokens=goal.usage.input_tokens + usage.input_tokens,
            output_tokens=goal.usage.output_tokens + usage.output_tokens,
            total_tokens=goal.usage.total_tokens + usage.total_tokens,
        )
        if goal.token_budget is not None and combined.total_tokens >= goal.token_budget:
            paused_at = utc_now()
            return self.store.update(
                ChatGoal,
                goal.id,
                {
                    "status": ChatGoalStatus.PAUSED,
                    "paused_at": paused_at,
                    "active_since": None,
                    "elapsed_seconds": goal.active_elapsed_seconds(paused_at),
                    "blocked_reason": exhausted_reason,
                    "usage": combined,
                },
                expected_revision=goal.revision,
            )
        return self.store.update(
            ChatGoal,
            goal.id,
            {"usage": combined},
            expected_revision=goal.revision,
        )

    def _save_tool_step(
        self,
        turn: ChatTurn,
        entry: dict[str, Any],
        *,
        status: ChatTurnStatus = ChatTurnStatus.ROUTING,
        approval_id: str | None = None,
    ) -> ChatTurn:
        return self.store.update(
            ChatTurn,
            turn.id,
            {
                "status": status,
                "next_step": turn.next_step + 1,
                "execution_tool_calls": turn.execution_tool_calls
                + (1 if entry.get("budget_class") != "artifact_query" else 0),
                "artifact_queries": turn.artifact_queries
                + (1 if entry.get("budget_class") == "artifact_query" else 0),
                "tool_call_ids": [*turn.tool_call_ids, str(entry["tool_call_id"])],
                "tool_history": [*turn.tool_history, entry],
                "approval_id": approval_id,
            },
            expected_revision=turn.revision,
        )

    @staticmethod
    def _bounded_tool_result(output: dict[str, Any]) -> str:
        rendered = serialize_model_result(output)
        decoded = json.loads(rendered)
        if decoded.get("schema") != "nebula.bounded-result/v1":
            return rendered
        # Compatibility for trusted non-action capabilities. Executable action
        # results reach this method only as compact v2 receipts.
        normalized = json.loads(json.dumps(output, ensure_ascii=False, default=str))

        def redact_value(value: Any) -> Any:
            if isinstance(value, str):
                return redact_text(value)
            if isinstance(value, list):
                return [redact_value(item) for item in value]
            if isinstance(value, dict):
                return {key: redact_value(item) for key, item in value.items()}
            return value

        raw = json.dumps(redact_value(normalized), ensure_ascii=False, sort_keys=True)
        envelope: dict[str, Any] = {
            "status": "complete",
            "truncated": True,
            "original_characters": len(raw),
            "preview": "",
        }
        empty = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
        envelope["preview"] = raw[: max(0, 8_000 - len(empty))]
        bounded = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
        while len(bounded) > 8_000 and envelope["preview"]:
            envelope["preview"] = envelope["preview"][: -(len(bounded) - 8_000)]
            bounded = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
        return bounded

    @staticmethod
    def _bounded_tool_error(status: str, detail: str) -> str:
        safe = redact_text(re.sub(r"\s+", " ", detail)).strip()[:1_000]
        return json.dumps({"status": status, "detail": safe}, sort_keys=True)

    @staticmethod
    def _tool_result_failed(result: Any) -> bool:
        receipt = getattr(result, "receipt", None)
        if receipt is not None:
            return receipt.status in {
                ToolResultStatus.FAILED,
                ToolResultStatus.TIMED_OUT,
                ToolResultStatus.CANCELLED,
            }
        if result.exit_code not in {None, 0}:
            return True
        if result.execution.get("timed_out") is True:
            return True
        return result.output.get("timed_out") is True

    @staticmethod
    def _result_summary(output: dict[str, Any]) -> str:
        if output.get("schema") == "nebula.tool-result/v2":
            status = output.get("status")
            exit_code = output.get("exit_code")
            if status == "timed_out":
                return "Tool execution timed out; partial output is available"
            if status == "failed":
                return f"Tool execution failed with exit code {exit_code}"
            if output.get("incomplete"):
                return "Tool execution completed with incomplete captured output"
            warnings = output.get("warnings")
            if isinstance(warnings, list) and warnings:
                return "Tool execution completed with parser/capture warnings"
            return "Tool execution completed; inspect artifacts with tool_output.search"
        keys = ", ".join(sorted(str(key) for key in output)[:6])
        return f"Result fields: {keys}" if keys else "Capability completed"

    async def _resume_pending_call(
        self,
        prepared: PreparedChat,
        turn: ChatTurn,
        components: RuntimeToolComponents | AutomationToolComponents,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        if not turn.approval_id or not turn.tool_history:
            raise ChatError("pending command turn is missing its approval checkpoint")
        approval = self.store.get(Approval, turn.approval_id)
        entry = dict(turn.tool_history[-1])
        if entry.get("status") != "waiting_approval":
            raise ChatError("pending command turn has an invalid tool checkpoint")
        if approval.status == ApprovalStatus.PENDING:
            yield (
                "approval_required",
                {
                    "type": "approval_required",
                    "turn_id": turn.id,
                    "tool_call_id": entry["tool_call_id"],
                    "approval": approval.model_dump(mode="json"),
                },
            )
            return
        invocation = ToolInvocation(
            engagement_id=turn.engagement_id,
            run_id=turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=turn.session_id,
            chat_turn_id=turn.id,
            tool_name=str(entry["name"]),
            arguments=dict(entry.get("arguments") or {}),
            workspace=components.workspace,
            idempotency_key=f"chat:{turn.id}:step:{entry['step']}",
            requested_by="chat-assistant",
        )
        try:
            result = await components.broker.execute(
                invocation, components.scope, approval=approval
            )
        except PolicyDenied as exc:
            record_caught_exception(
                "chat",
                "chat.chat.caught_failure_011",
                "A handled chat operation raised an exception.",
                exc,
                stage="chat",
            )
            entry.update(
                {
                    "status": "denied",
                    "provider_result": self._bounded_tool_error(
                        "denied", exc.decision.reason
                    ),
                }
            )
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.chat.caught_failure_012",
                "A handled chat operation raised an exception.",
                exc,
                stage="chat",
            )
            entry.update(
                {
                    "status": "failed",
                    "provider_result": self._bounded_tool_error(
                        "failed", f"{type(exc).__name__}: {str(exc)}"
                    ),
                }
            )
        else:
            result_failed = self._tool_result_failed(result)
            entry.update(
                {
                    "status": "failed" if result_failed else "complete",
                    "provider_result": serialize_model_result(result.model_result()),
                    "trusted_result": result.receipt is None,
                    "evidence_ids": result.evidence_ids,
                    "result_artifact_id": result.result_artifact_id,
                    "artifacts": result.model_result().get("artifacts", []),
                    "result_summary": self._result_summary(result.model_result()),
                }
            )
        history = [*turn.tool_history[:-1], entry]
        turn = self.store.update(
            ChatTurn,
            turn.id,
            {
                "status": ChatTurnStatus.ROUTING,
                "approval_id": None,
                "tool_history": history,
            },
            expected_revision=turn.revision,
        )
        prepared.turn = turn
        yield (
            "tool_completed",
            {
                "type": "tool_completed",
                "turn_id": turn.id,
                "tool_call_id": entry["tool_call_id"],
                "capability": entry["name"],
                "status": entry["status"],
                "summary": entry.get("result_summary") or entry["provider_result"],
                "evidence_ids": entry.get("evidence_ids", []),
                "result_artifact_id": entry.get("result_artifact_id"),
                "artifacts": entry.get("artifacts", []),
                "receipt": _decoded_result(entry.get("provider_result")),
                "step": entry["step"],
            },
        )

    async def _resume_callback_result(
        self, prepared: PreparedChat, turn: ChatTurn
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        if not turn.tool_history:
            raise ChatError("pending callback turn is missing its tool checkpoint")
        entry = dict(turn.tool_history[-1])
        if entry.get("status") != "waiting_callback":
            raise ChatError("pending callback turn has an invalid tool checkpoint")
        wait = entry.get("subagent_wait")
        if isinstance(wait, dict):
            async for item in self._resume_subagent_wait(prepared, turn, entry, wait):
                yield item
            return
        process_id = entry.get("process_id")
        if not isinstance(process_id, str) or not process_id:
            raise ChatError("pending callback turn is missing its process id")
        from .automation_runtime import AutomationRuntimeManager

        execution_id = AutomationRuntimeManager._execution_id(process_id)
        execution = self.store.get(CommandExecution, execution_id)
        if not execution.metadata.get("results_received"):
            yield (
                "callback_required",
                {
                    "type": "callback_required",
                    "turn_id": turn.id,
                    "tool_call_id": entry["tool_call_id"],
                    "process_id": process_id,
                    "results_url": entry.get("results_url"),
                    "summary": "Waiting for the command to POST results.",
                },
            )
            return
        output = {
            "schema": "nebula.tool-result/v2",
            "tool_call_id": entry["tool_call_id"],
            "tool_name": entry["name"],
            "status": "completed"
            if execution.status.value == "completed"
            else "failed",
            "summary": execution.metadata.get("results_summary")
            or execution.error
            or f"Callback recorded {execution.status.value}",
            "exit_code": execution.exit_code,
            "output": execution.metadata.get("results_output") or {},
            "stdout": execution.metadata.get("results_stdout") or "",
            "incomplete": False,
        }
        failed = execution.status != CommandExecutionStatus.COMPLETED
        entry.update(
            {
                "status": "failed" if failed else "complete",
                "provider_result": serialize_model_result(output),
                "result_summary": output["summary"],
            }
        )
        from .domain import ToolCall

        tool_call_id = entry.get("tool_call_id")
        if isinstance(tool_call_id, str):
            try:
                call = self.store.get(ToolCall, tool_call_id)
                self.store.update(
                    ToolCall,
                    call.id,
                    {
                        "status": ToolCallStatus.FAILED
                        if failed
                        else ToolCallStatus.COMPLETE,
                        "completed_at": utc_now(),
                        "result": output,
                        "error": execution.error,
                    },
                    expected_revision=call.revision,
                )
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.callback.tool_finalize_failed",
                    "A callback result could not update the durable tool call.",
                    exc,
                    stage="callback",
                )
        history = [*turn.tool_history[:-1], entry]
        turn = self.store.update(
            ChatTurn,
            turn.id,
            {"status": ChatTurnStatus.ROUTING, "tool_history": history},
            expected_revision=turn.revision,
        )
        prepared.turn = turn
        yield (
            "tool_completed",
            {
                "type": "tool_completed",
                "turn_id": turn.id,
                "tool_call_id": entry["tool_call_id"],
                "capability": entry["name"],
                "status": entry["status"],
                "summary": entry.get("result_summary") or entry["provider_result"],
                "evidence_ids": entry.get("evidence_ids", []),
                "result_artifact_id": entry.get("result_artifact_id"),
                "artifacts": entry.get("artifacts", []),
                "receipt": output,
                "step": entry["step"],
            },
        )

    async def _resume_subagent_wait(
        self,
        prepared: PreparedChat,
        turn: ChatTurn,
        entry: dict[str, Any],
        wait: dict[str, Any],
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        ids = [str(item) for item in wait.get("ids") or []]
        mode = "any" if wait.get("mode") == "any" else "all"
        if not self.subagents.wait_satisfied(ids, mode):
            yield (
                "callback_required",
                {
                    "type": "callback_required",
                    "turn_id": turn.id,
                    "tool_call_id": entry["tool_call_id"],
                    "subagent_ids": ids,
                    "summary": entry.get("result_summary")
                    or "Waiting for subagents to report.",
                },
            )
            return
        output = self.subagents.wait_output(ids)
        received = len(ids) - len(output["still_running"])
        entry.update(
            {
                "status": "complete",
                "provider_result": serialize_model_result(output),
                "trusted_result": True,
                "result_summary": (
                    f"{received} subagent report{'' if received == 1 else 's'} received"
                ),
            }
        )
        turn = self.store.update(
            ChatTurn,
            turn.id,
            {
                "status": ChatTurnStatus.ROUTING,
                "tool_history": [*turn.tool_history[:-1], entry],
            },
            expected_revision=turn.revision,
        )
        prepared.turn = turn
        yield (
            "tool_completed",
            {
                "type": "tool_completed",
                "turn_id": turn.id,
                "tool_call_id": entry["tool_call_id"],
                "capability": entry["name"],
                "status": entry["status"],
                "summary": entry["result_summary"],
                "evidence_ids": [],
                "result_artifact_id": None,
                "artifacts": [],
                "receipt": output,
                "step": entry["step"],
            },
        )

    def continue_after_tool_callback(self, process_id: str) -> str | None:
        """Resume a provider turn after a LAN results webhook."""

        from .automation_runtime import AutomationRuntimeManager

        execution = self.store.get(
            CommandExecution, AutomationRuntimeManager._execution_id(process_id)
        )
        turn_id = execution.metadata.get("chat_turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return None
        turn = self.store.get(ChatTurn, turn_id)
        if turn.status != ChatTurnStatus.WAITING_CALLBACK:
            return None
        if turn.backend != ChatBackend.PROVIDER:
            return None
        if self.has_active_provider_turn(turn.id):
            return turn.id
        prepared = self.prepare_resume(turn.id)
        return self.start_provider_turn(prepared)

    def prepare_resume(self, turn_id: str) -> PreparedChat:
        turn = self.store.get(ChatTurn, turn_id)
        if turn.status not in {
            ChatTurnStatus.WAITING_APPROVAL,
            ChatTurnStatus.WAITING_CALLBACK,
            ChatTurnStatus.ROUTING,
            ChatTurnStatus.FINALIZING,
            ChatTurnStatus.INTERRUPTED,
        }:
            raise ChatHistoryConflict(
                f"chat turn cannot resume from {turn.status.value}"
            )
        recovery = turn.request_snapshot.get("recovery")
        recovering = turn.status == ChatTurnStatus.INTERRUPTED
        if recovering:
            unknown = (
                recovery.get("unknown_tool_call_ids", [])
                if isinstance(recovery, dict)
                else []
            )
            if unknown:
                raise ChatHistoryConflict(
                    "interrupted response has an unknown tool outcome; reconcile it before resume"
                )
            unknown_hooks = (
                recovery.get("unknown_hook_execution_ids", [])
                if isinstance(recovery, dict)
                else []
            )
            if unknown_hooks:
                raise ChatHistoryConflict(
                    "interrupted response has an unknown hook outcome; reconcile it before resume"
                )

        def activate_recovery(candidate: ChatTurn) -> ChatTurn:
            if not recovering:
                return candidate
            return self.store.update(
                ChatTurn,
                candidate.id,
                {
                    "status": ChatTurnStatus.ROUTING,
                    "error": None,
                    "request_snapshot": {
                        **candidate.request_snapshot,
                        "recovery": {
                            **(recovery if isinstance(recovery, dict) else {}),
                            "required": False,
                            "resumed_at": utc_now().isoformat(),
                        },
                    },
                },
                expected_revision=candidate.revision,
            )

        session = self.store.get(ChatSession, turn.session_id)
        if turn.provider_profile_id is None:
            raise ChatConfigurationError("chat turn no longer identifies a provider")
        profile = self.store.get(ProviderProfile, turn.provider_profile_id)
        if not profile.enabled:
            raise ChatConfigurationError("the chat provider is no longer enabled")
        provider = self.provider_factory(profile)
        model_request = ModelRequest.model_validate(
            turn.request_snapshot.get("model_request")
        )
        citations = [
            ChatCitation.model_validate(item)
            for item in turn.request_snapshot.get("citations", [])
        ]
        hook_snapshots = [
            NativeHookSnapshot.model_validate(item)
            for item in turn.request_snapshot.get("hook_snapshots", [])
        ]
        if not turn.tools_enabled:
            resolved_model = provider.require(model_request)
            turn = activate_recovery(turn)
            return PreparedChat(
                provider=provider,
                provider_profile=profile,
                model_request=model_request,
                resolved_model=resolved_model,
                citations=citations,
                engagement_id=turn.engagement_id,
                session=session,
                pending_session=None,
                stored_messages=self._session_messages(session),
                new_messages=[],
                context_usage=ChatTokenUsage.model_validate(
                    turn.request_snapshot.get("context_usage", {})
                ),
                tools_enabled=False,
                turn=turn,
                inputs_persisted=True,
                hook_snapshots=hook_snapshots,
            )
        if not profile.tools_verified_for(turn.model):
            raise ChatConfigurationError(
                "the exact chat model is no longer verified for command use"
            )
        if (
            bool(turn.request_snapshot.get("include_oci_tools", True))
            and self.automation_tool_platform is None
        ):
            raise ChatConfigurationError("automation command runtime is unavailable")
        try:
            mcp_profiles = tuple(
                McpServerProfile.model_validate(item)
                for item in turn.request_snapshot.get("mcp_snapshot", [])
            )
            ssh_environments = tuple(
                SshEnvironment.model_validate(item)
                for item in turn.request_snapshot.get("ssh_environment_snapshot", [])
            )
            include_commands = bool(
                turn.request_snapshot.get("include_oci_tools", True)
            )
            extra_components = (
                self.tool_platform.chat_components(
                    engagement_id=turn.engagement_id,
                    turn_id=turn.id,
                    provider=provider,
                    model=turn.model,
                    mcp_profiles=mcp_profiles,
                    ssh_environments=ssh_environments,
                    include_oci=False,
                    allow_empty=True,
                )
                if (mcp_profiles or ssh_environments) and self.tool_platform is not None
                else None
            )
            components: RuntimeToolComponents | AutomationToolComponents | None
            if include_commands:
                assert self.automation_tool_platform is not None
                components = self.automation_tool_platform.chat_components(
                    engagement_id=turn.engagement_id,
                    extra_components=extra_components,
                )
            else:
                components = extra_components
            browser_session_id = turn.request_snapshot.get("browser_session_id")
            if isinstance(browser_session_id, str):
                browser_session = self.store.get(BrowserSession, browser_session_id)
                browser_components = (
                    companion_components(
                        self.store,
                        turn.engagement_id,
                        browser_session_id,
                        artifact_store=self.artifact_store,
                        image_supported=profile.capabilities.vision,
                    )
                    if browser_session.metadata.get("browser_companion_version") == 1
                    else self.browser_tool_platform.chat_components(
                        engagement_id=turn.engagement_id,
                        browser_session_id=browser_session_id,
                    )
                )
                components = combine_tool_components(components, browser_components)
            if (
                turn.request_snapshot.get("application_model_context")
                and not browser_session_id
            ):
                components = combine_tool_components(
                    components, standalone_components(self.store, turn.engagement_id)
                )
            skill_snapshots = [
                SkillSnapshot.model_validate(item)
                for item in turn.request_snapshot.get("skill_snapshots", [])
            ]
            skill_components = (
                skill_resource_components(
                    skill_snapshots,
                    engagement_id=turn.engagement_id,
                    workspace=self.workspace_resolver(turn.engagement_id),
                    scope=components.scope if components else None,
                )
                if skill_snapshots
                else None
            )
            if skill_components is not None:
                components = combine_tool_components(components, skill_components)
            if turn.request_snapshot.get("allow_subagents"):
                workspace = (
                    components.workspace
                    if components is not None
                    else Path(
                        self.store.get(Engagement, turn.engagement_id).workspace_path
                        or "."
                    ).resolve()
                )
                components = combine_tool_components(
                    components,
                    subagent_components(
                        self.subagents,
                        engagement_id=turn.engagement_id,
                        workspace=workspace,
                        scope=components.scope if components else None,
                    ),
                )
            if components is None:
                raise ChatConfigurationError("no runtime capabilities were selected")
            deferred = catalog_snapshot(turn.request_snapshot).get("deferred")
            if deferred:
                # Rebuilt from the snapshot; nothing is re-ranked on resume.
                catalog = catalog_components(
                    components, deferred=deferred, index=self._tool_index()
                )
                if catalog is not None:
                    components = combine_tool_components(components, catalog)
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.chat.caught_failure_013",
                "A handled chat operation raised an exception.",
                exc,
                stage="chat",
            )
            raise ChatConfigurationError(str(exc)) from exc
        if (
            components.scope.id != turn.scope_policy_id
            or components.scope.revision != turn.scope_revision
            or turn.request_snapshot.get("automation_runtime_digest")
            != getattr(components, "runtime_digest", None)
        ):
            raise ChatHistoryConflict(
                "automation runtime or scope changed while the response was paused"
            )
        resolved_model = provider.require(model_request)
        turn = activate_recovery(turn)
        return PreparedChat(
            provider=provider,
            provider_profile=profile,
            model_request=model_request,
            resolved_model=resolved_model,
            citations=citations,
            engagement_id=turn.engagement_id,
            session=session,
            pending_session=None,
            stored_messages=self._session_messages(session),
            new_messages=[],
            context_usage=ChatTokenUsage.model_validate(
                turn.request_snapshot.get("context_usage", {})
            ),
            tools_enabled=True,
            tool_components=components,
            turn=turn,
            inputs_persisted=True,
            hook_snapshots=hook_snapshots,
        )

    async def _run_native_hooks(
        self,
        prepared: PreparedChat,
        event_name: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Run snapshotted native hooks once without granting approval authority."""

        turn = prepared.turn
        session = prepared.session or prepared.pending_session
        if not prepared.hook_snapshots:
            return
        if turn is None or session is None or prepared.engagement_id is None:
            raise ChatError("native hook execution requires a durable project turn")
        executions = [
            item
            for item in self.list_turn_hook_executions(turn.id)
            if item.event_name == event_name
        ]
        runner = NativeHookRunner(self.store)
        for snapshot in prepared.hook_snapshots:
            if event_name not in snapshot.manifest.events:
                continue
            prior = next(
                (item for item in executions if item.hook_id == snapshot.id), None
            )
            if prior is not None and not (
                prior.status == "interrupted" and prior.side_effects == "none"
            ):
                if (
                    prior.status in {"failed", "timed_out"}
                    and snapshot.manifest.failure_policy == "block"
                ):
                    raise ChatError(
                        f"required native hook {snapshot.id!r} did not complete: "
                        f"{prior.error or prior.status}"
                    )
                if prior.status == "running":
                    raise ChatHistoryConflict(
                        f"native hook {snapshot.id!r} already has an active execution"
                    )
                continue
            try:
                outcome = await runner.run(
                    snapshot,
                    engagement_id=prepared.engagement_id,
                    chat_session_id=session.id,
                    chat_turn_id=turn.id,
                    event_name=event_name,
                    payload={
                        "provider_id": prepared.provider_profile.id,
                        "model": prepared.resolved_model,
                        **(payload or {}),
                    },
                )
            except NativeHookError as exc:
                raise ChatConfigurationError(str(exc)) from exc
            if (
                outcome.status != "complete"
                and snapshot.manifest.failure_policy == "block"
            ):
                raise ChatError(
                    f"required native hook {snapshot.id!r} did not complete: "
                    f"{outcome.error or outcome.status}"
                )

    def list_turn_hook_executions(self, turn_id: str) -> list[NativeHookExecution]:
        """Return every durable hook attempt for a turn, paging past the store cap."""

        turn = self.store.get(ChatTurn, turn_id)
        executions: list[NativeHookExecution] = []
        offset = 0
        while page := self.store.list_entities(
            NativeHookExecution,
            engagement_id=turn.engagement_id,
            offset=offset,
            limit=1_000,
        ):
            executions.extend(item for item in page if item.chat_turn_id == turn.id)
            offset += len(page)
        return sorted(executions, key=lambda item: (item.started_at, item.id))

    def list_session_hook_executions(
        self, session_id: str
    ) -> list[NativeHookExecution]:
        """Return hook attempts for the pending or latest provider turn."""

        pending = self.pending_turn(session_id)
        if pending is not None:
            return self.list_turn_hook_executions(pending.id)
        latest: ChatTurn | None = None
        offset = 0
        while page := self.store.list_entities(ChatTurn, offset=offset, limit=1_000):
            for item in page:
                if item.session_id != session_id:
                    continue
                if latest is None or (item.created_at, item.id) > (
                    latest.created_at,
                    latest.id,
                ):
                    latest = item
            offset += len(page)
        return self.list_turn_hook_executions(latest.id) if latest is not None else []

    async def _run_terminal_native_hooks(
        self, prepared: PreparedChat, event_name: str, detail: str
    ) -> None:
        """Persist terminal hook outcomes without replacing the primary failure."""

        try:
            await self._run_native_hooks(prepared, event_name, {"detail": detail})
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.native_hook.terminal_failure",
                "A terminal native hook did not complete.",
                exc,
                stage=event_name,
            )

    def pending_turn(self, session_id: str) -> ChatTurn | None:
        self.store.get(ChatSession, session_id)
        active = [
            item
            for item in self.store.list_entities(ChatTurn, limit=1_000)
            if item.session_id == session_id
            and item.status
            in {
                ChatTurnStatus.ROUTING,
                ChatTurnStatus.WAITING_APPROVAL,
                ChatTurnStatus.WAITING_CALLBACK,
                ChatTurnStatus.FINALIZING,
                ChatTurnStatus.INTERRUPTED,
            }
            and (
                item.status != ChatTurnStatus.INTERRUPTED
                or bool(item.request_snapshot.get("recovery", {}).get("required"))
            )
        ]
        if len(active) > 1:
            raise ChatHistoryConflict("chat session has multiple active turns")
        return active[0] if active else None

    def reconcile_interrupted_tool(
        self,
        turn_id: str,
        tool_call_id: str,
        *,
        outcome: str,
        detail: str,
        expected_revision: int,
    ) -> ChatTurn:
        """Record an operator-confirmed outcome without replaying the tool effect."""

        turn = self.store.get(ChatTurn, turn_id)
        if turn.revision != expected_revision:
            raise ChatHistoryConflict(
                "interrupted response changed; reload before reconciling"
            )
        recovery = turn.request_snapshot.get("recovery")
        unknown = (
            list(recovery.get("unknown_tool_call_ids", []))
            if isinstance(recovery, dict)
            else []
        )
        if turn.status != ChatTurnStatus.INTERRUPTED or tool_call_id not in unknown:
            raise ChatHistoryConflict(
                "tool call is not an unresolved outcome for this interrupted response"
            )
        if outcome not in {"complete", "failed"}:
            raise ChatConfigurationError(
                "reconciled outcome must be complete or failed"
            )
        note = detail.strip()
        if not note:
            raise ChatConfigurationError("reconciliation requires an operator note")
        call = self.store.get(ToolCall, tool_call_id)
        if call.chat_turn_id != turn.id or call.status != ToolCallStatus.RUNNING:
            raise ChatHistoryConflict(
                "tool call state no longer matches restart recovery"
            )
        model_call_id = call.metadata.get("provider_call_id")
        step = call.metadata.get("provider_step")
        if not isinstance(model_call_id, str) or not isinstance(step, int):
            raise ChatHistoryConflict(
                "tool intent lacks provider continuation identity; cancel this response"
            )
        call_status = (
            ToolCallStatus.COMPLETE if outcome == "complete" else ToolCallStatus.FAILED
        )
        result = {
            "schema": "nebula.operator-reconciliation/v1",
            "status": outcome,
            "detail": note,
            "verified": False,
        }
        self.store.update(
            ToolCall,
            call.id,
            {
                "status": call_status,
                "completed_at": utc_now(),
                "result": result,
                "error": note if outcome == "failed" else None,
                "metadata": {
                    **call.metadata,
                    "reconciled_after_restart": True,
                    "reconciled_by": self.operator_id(),
                },
            },
            expected_revision=call.revision,
        )
        entry = {
            "step": step,
            "model_call_id": model_call_id,
            "tool_call_id": call.id,
            "name": call.tool_name,
            "arguments": call.arguments,
            "budget_class": call.metadata.get("budget_class", "execution"),
            "status": outcome,
            "provider_result": json.dumps(result, ensure_ascii=False, sort_keys=True),
            "trusted_result": False,
            "result_summary": note[:1_000],
        }
        history = [
            item for item in turn.tool_history if item.get("tool_call_id") != call.id
        ]
        history.append(entry)
        history.sort(key=lambda item: int(item.get("step", 0)))
        remaining = [item for item in unknown if item != call.id]
        return self.store.update(
            ChatTurn,
            turn.id,
            {
                "tool_call_ids": list(dict.fromkeys([*turn.tool_call_ids, call.id])),
                "tool_history": history,
                "next_step": max(turn.next_step, step + 1),
                "error": (
                    "Core restarted before this response completed. Tool outcomes "
                    "were reconciled; review and resume it."
                    if not remaining
                    else turn.error
                ),
                "request_snapshot": {
                    **turn.request_snapshot,
                    "recovery": {
                        **(recovery or {}),
                        "unknown_tool_call_ids": remaining,
                        "last_reconciled_at": utc_now().isoformat(),
                    },
                },
            },
            expected_revision=turn.revision,
        )

    def reconcile_interrupted_hook(
        self,
        turn_id: str,
        hook_execution_id: str,
        *,
        outcome: str,
        detail: str,
        expected_revision: int,
    ) -> ChatTurn:
        """Resolve an uncertain native-hook effect without replaying it."""

        turn = self.store.get(ChatTurn, turn_id)
        if turn.revision != expected_revision:
            raise ChatHistoryConflict(
                "interrupted response changed; reload before reconciling"
            )
        recovery = turn.request_snapshot.get("recovery")
        unknown = (
            list(recovery.get("unknown_hook_execution_ids", []))
            if isinstance(recovery, dict)
            else []
        )
        if (
            turn.status != ChatTurnStatus.INTERRUPTED
            or hook_execution_id not in unknown
        ):
            raise ChatHistoryConflict(
                "hook execution is not an unresolved outcome for this interrupted response"
            )
        if outcome not in {"complete", "failed"}:
            raise ChatConfigurationError(
                "reconciled outcome must be complete or failed"
            )
        note = detail.strip()
        if not note:
            raise ChatConfigurationError("reconciliation requires an operator note")
        execution = self.store.get(NativeHookExecution, hook_execution_id)
        if execution.chat_turn_id != turn.id or execution.status != "interrupted":
            raise ChatHistoryConflict(
                "hook execution state no longer matches restart recovery"
            )
        self.store.update(
            NativeHookExecution,
            execution.id,
            {
                "status": "reconciled",
                "reconciliation": {
                    "schema": "nebula.operator-reconciliation/v1",
                    "outcome": outcome,
                    "detail": note,
                    "verified": False,
                    "reconciled_by": self.operator_id(),
                    "reconciled_at": utc_now().isoformat(),
                },
            },
            expected_revision=execution.revision,
        )
        remaining = [item for item in unknown if item != execution.id]
        return self.store.update(
            ChatTurn,
            turn.id,
            {
                "error": (
                    "Core restarted before this response completed. Hook outcomes "
                    "were reconciled; review and resume it."
                    if not remaining
                    else turn.error
                ),
                "request_snapshot": {
                    **turn.request_snapshot,
                    "recovery": {
                        **(recovery or {}),
                        "unknown_hook_execution_ids": remaining,
                        "last_reconciled_at": utc_now().isoformat(),
                    },
                },
            },
            expected_revision=turn.revision,
        )

    def cancel_turn(self, turn_id: str) -> ChatTurn:
        turn = self.store.get(ChatTurn, turn_id)
        if turn.status in {ChatTurnStatus.COMPLETE, ChatTurnStatus.CANCELLED}:
            return turn
        if turn.approval_id:
            approval = self.store.get(Approval, turn.approval_id)
            if approval.status == ApprovalStatus.PENDING:
                self.store.update(
                    Approval,
                    approval.id,
                    {
                        "status": ApprovalStatus.CANCELLED,
                        "decided_by": self.operator_id(),
                        "decided_at": utc_now(),
                        "decision_note": "response stopped",
                    },
                    expected_revision=approval.revision,
                )
        if turn.tool_call_ids:
            try:
                call = self.store.get(ToolCall, turn.tool_call_ids[-1])
            except NotFoundError as caught_error:
                record_caught_exception(
                    "chat",
                    "chat.chat.caught_failure_014",
                    "A handled chat operation raised an exception.",
                    caught_error,
                    stage="chat",
                )
                call = None
            if call is not None and call.status not in {
                ToolCallStatus.COMPLETE,
                ToolCallStatus.FAILED,
                ToolCallStatus.DENIED,
                ToolCallStatus.CANCELLED,
            }:
                self.store.update_with_event(
                    ToolCall,
                    call.id,
                    {
                        "status": ToolCallStatus.CANCELLED,
                        "completed_at": utc_now(),
                        "error": "response stopped",
                    },
                    expected_revision=call.revision,
                    run_id=turn.id,
                    event_type="tool.cancelled",
                    event_payload={
                        "tool_call_id": call.id,
                        "status": ToolCallStatus.CANCELLED.value,
                    },
                    actor_id=self.operator_id(),
                    idempotency_key=f"tool:{call.id}:chat-stop",
                )
        goal = self.store.get(ChatGoal, turn.goal_id) if turn.goal_id else None
        with self.store.transaction() as transaction:
            cancelled = transaction.update(
                ChatTurn,
                turn.id,
                {
                    "status": ChatTurnStatus.CANCELLED,
                    "error": "response stopped",
                    "execution_owner_id": None,
                    "execution_claim_id": None,
                    "execution_claimed_at": None,
                },
                expected_revision=turn.revision,
            )
            if goal is not None and goal.execution_claim_id == turn.execution_claim_id:
                transaction.update(
                    ChatGoal,
                    goal.id,
                    {
                        "execution_owner_id": None,
                        "execution_claim_id": None,
                        "execution_claimed_at": None,
                    },
                    expected_revision=goal.revision,
                )
        return cancelled

    def session_messages(
        self, session_id: str, *, include_replaced: bool = False
    ) -> list[ChatMessage]:
        session = self.store.get(ChatSession, session_id)
        return self._session_messages(session, include_replaced=include_replaced)

    def attach_native_run_to_chat(self, run_id: str) -> ChatSession:
        """Create or return the durable provider chat that continues a mission result."""

        run = self.store.get(AgentRun, run_id)
        if run.backend != RunBackend.NATIVE:
            raise ChatConfigurationError(
                "only native runs can be attached to provider chat"
            )
        if not run.supervisor_provider_id or not run.supervisor_model:
            raise ChatConfigurationError(
                "mission has no provider runtime to continue in chat"
            )
        existing = next(
            (
                item
                for item in self.store.list_entities(
                    ChatSession, engagement_id=run.engagement_id, limit=1_000
                )
                if run.id in item.metadata.get("attached_run_ids", [])
            ),
            None,
        )
        if existing is not None:
            return existing
        summary = run.metadata.get("final_summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ChatHistoryConflict("mission has no final result to continue in chat")
        saved_name = run.metadata.get("name")
        title = (
            saved_name.strip()
            if isinstance(saved_name, str) and saved_name.strip()
            else run.objective.strip()
        )
        chat = ChatSession(
            id=str(uuid4()),
            engagement_id=run.engagement_id,
            title=(title or "Mission discussion")[:300],
            backend=ChatBackend.PROVIDER,
            provider_profile_id=run.supervisor_provider_id,
            model=run.supervisor_model,
            metadata={
                "attached_run_ids": [run.id],
                "context_management": "nebula",
            },
        )
        with self.store.transaction() as transaction:
            transaction.add(chat)
            transaction.add(
                ChatMessage(
                    id=str(uuid4()),
                    engagement_id=run.engagement_id,
                    session_id=chat.id,
                    sequence=1,
                    role=ChatRole.USER,
                    content=run.objective,
                    provider_profile_id=run.supervisor_provider_id,
                    model=run.supervisor_model,
                    metadata={"run_id": run.id, "handoff": "mission_to_chat"},
                )
            )
            transaction.add(
                ChatMessage(
                    id=str(uuid4()),
                    engagement_id=run.engagement_id,
                    session_id=chat.id,
                    sequence=2,
                    role=ChatRole.ASSISTANT,
                    content=summary,
                    provider_profile_id=run.supervisor_provider_id,
                    model=run.supervisor_model,
                    metadata={"run_id": run.id, "handoff": "mission_to_chat"},
                )
            )
        return chat

    def fork_session(
        self,
        session_id: str,
        *,
        through_message_id: str | None = None,
        before_message_id: str | None = None,
        title: str | None = None,
        harness_session_id: str | None = None,
    ) -> ChatSession:
        """Create an independent transcript branch with explicit provenance."""

        source = self.store.get(ChatSession, session_id)
        if self.pending_turn(session_id) is not None:
            raise ChatHistoryConflict(
                "conversation cannot be forked while a response is active"
            )
        messages = self._session_messages(source)
        boundary = next(
            (
                message
                for message in messages
                if message.id == (through_message_id or before_message_id)
            ),
            None,
        )
        if boundary is None:
            raise ChatHistoryConflict(
                "fork message does not belong to the selected conversation"
            )
        if source.backend == ChatBackend.HARNESS and not harness_session_id:
            raise ChatConfigurationError(
                "harness conversation forks require an independent harness session"
            )
        fork = self.store.create(
            ChatSession(
                id=str(uuid4()),
                engagement_id=source.engagement_id,
                title=(title or f"{source.title} (fork)")[:300],
                backend=source.backend,
                provider_profile_id=source.provider_profile_id,
                harness_profile_id=source.harness_profile_id,
                harness_session_id=(
                    harness_session_id
                    if source.backend == ChatBackend.HARNESS
                    else None
                ),
                model=source.model,
                parent_session_id=source.id,
                forked_from_message_id=boundary.id,
                metadata={
                    **{
                        key: value
                        for key, value in source.metadata.items()
                        if key != "archived_at"
                    },
                    "forked_from_session_id": source.id,
                    "forked_from_message_id": boundary.id,
                    "workspace_is_shared": True,
                    "branch_before_message": bool(before_message_id),
                    "harness_context_handoff_pending": (
                        source.backend == ChatBackend.HARNESS
                    ),
                },
            )
        )
        for message in messages:
            if message.sequence > boundary.sequence or (
                before_message_id and message.sequence == boundary.sequence
            ):
                break
            self.store.create(
                ChatMessage(
                    id=str(uuid4()),
                    engagement_id=fork.engagement_id,
                    session_id=fork.id,
                    sequence=message.sequence,
                    role=message.role,
                    content=message.content,
                    content_blocks=message.content_blocks,
                    source_message_id=message.id,
                    provider_profile_id=message.provider_profile_id,
                    model=message.model,
                    usage=message.usage,
                    finish_reason=message.finish_reason,
                    provider_request_id=message.provider_request_id,
                    citations=message.citations,
                    metadata={**message.metadata, "fork_source_message_id": message.id},
                )
            )
        from .chat_decisions import fork_decisions
        from .chat_goals import ChatGoalService
        from .storage import NotFoundError

        fork_decisions(
            self.store,
            source,
            fork,
            boundary.sequence - (1 if before_message_id else 0),
        )
        if source.backend == ChatBackend.PROVIDER:
            try:
                goal = ChatGoalService(self.store).get(source.id)
            except (
                NotFoundError
            ):  # diagnostic-expected: fork without a goal copies no goal
                goal = None
            if goal is not None:
                self.store.create(
                    ChatGoal(
                        engagement_id=fork.engagement_id,
                        session_id=fork.id,
                        objective=goal.objective,
                        completion_criteria=goal.completion_criteria,
                        plan=goal.plan,
                        token_budget=goal.token_budget,
                        time_budget_seconds=goal.time_budget_seconds,
                        step_budget=goal.step_budget,
                        child_budget=goal.child_budget,
                        skill_snapshots=goal.skill_snapshots,
                        metadata={
                            "forked_from_goal_id": goal.id,
                            "workspace_is_shared": True,
                        },
                    )
                )
        return fork

    def rewind_session(
        self, session_id: str, *, before_message_id: str
    ) -> tuple[ChatSession, list[ChatMessage], list[ChatMessage]]:
        """Retract an operator message and its replies so it can be resent here."""

        session = self.store.get(ChatSession, session_id)
        if self.pending_turn(session_id) is not None:
            raise ChatHistoryConflict(
                "conversation cannot be edited while a response is active"
            )
        messages = self._session_messages(session)
        boundary = next(
            (message for message in messages if message.id == before_message_id),
            None,
        )
        if boundary is None:
            raise ChatHistoryConflict(
                "edited message does not belong to the selected conversation"
            )
        if boundary.role != ChatRole.USER:
            raise ChatHistoryConflict("only operator messages can be edited in place")
        replaced = [
            message for message in messages if message.sequence >= boundary.sequence
        ]
        retraction_id = str(uuid4())
        retracted_at = utc_now().isoformat()
        history = [
            item
            for item in session.metadata.get("message_retractions", [])
            if isinstance(item, dict)
        ][-31:]
        history.append(
            {
                "id": retraction_id,
                "at": retracted_at,
                "reason": "operator_edit",
                "from_message_id": boundary.id,
                "from_sequence": boundary.sequence,
                "message_ids": [message.id for message in replaced],
            }
        )
        with self.store.transaction() as transaction:
            retracted = [
                transaction.update(
                    ChatMessage,
                    message.id,
                    {
                        "metadata": {
                            **message.metadata,
                            "retracted_at": retracted_at,
                            "retraction_id": retraction_id,
                            "retracted_reason": "operator_edit",
                        }
                    },
                    expected_revision=message.revision,
                )
                for message in replaced
            ]
            session = transaction.update(
                ChatSession,
                session.id,
                {
                    "metadata": {
                        **session.metadata,
                        "message_retractions": history,
                    }
                },
                expected_revision=session.revision,
            )
        retained = [
            message for message in messages if message.sequence < boundary.sequence
        ]
        return session, retained, retracted

    def context_status(self, session_id: str) -> ContextStatus:
        session = self.store.get(ChatSession, session_id)
        if session.provider_profile_id is None:
            raise ChatConfigurationError("chat session does not identify a provider")
        profile = self.store.get(ProviderProfile, session.provider_profile_id)
        messages = self._session_messages(session)
        limits = resolve_context_limits(profile, model=session.model)
        try:
            project_text = project_instructions_text(
                self._project_instructions(session.engagement_id)
            )
        except ChatConfigurationError:
            # diagnostic-expected: an unusable AGENTS.md is reported when a turn starts
            project_text = ""
        base_instructions = _CHAT_INSTRUCTIONS + project_text
        estimated = estimate_messages(
            [
                ModelMessage(role=message.role.value, content=message.content)
                for message in messages
            ],
            base_instructions,
        )
        active_estimated = estimated
        latest = ContextCompactor(self.store).latest(
            ContextOwnerType.CHAT_SESSION, session.id, session.engagement_id
        )
        if latest is None:
            status = (
                "not_needed" if estimated <= limits.target_input_tokens else "stale"
            )
            through = 0
        elif latest.status == ContextSnapshotStatus.FAILED:
            status = "failed"
            through = latest.compacted_through
        else:
            uncompacted = [
                message
                for message in messages
                if message.sequence > latest.compacted_through
            ]
            uncompacted_tokens = sum(
                estimate_tokens(message.content, message_count=1)
                for message in uncompacted
            )
            status = (
                "stale"
                if uncompacted_tokens > limits.target_input_tokens * 2 // 5
                else "ready"
            )
            through = latest.compacted_through
            if latest.memory is not None:
                active_estimated = (
                    estimate_tokens(
                        base_instructions + "\n\n" + memory_text(latest.memory)
                    )
                    + uncompacted_tokens
                )
        return ContextStatus(
            owner_type=ContextOwnerType.CHAT_SESSION,
            owner_id=session.id,
            status=status,
            context_window=limits.context_window,
            max_output_tokens=limits.max_output_tokens,
            target_input_tokens=limits.target_input_tokens,
            compacted_input_target=limits.compacted_input_target,
            capacity_source=limits.source,
            capacity_estimated=limits.estimated,
            metadata_revision=limits.metadata_revision,
            route_limits_verified=limits.route_limits_verified,
            eligible_route_count=limits.eligible_route_count,
            route_context_window=limits.route_context_window,
            route_input_limit=limits.route_input_limit,
            route_limits_required=limits.route_limits_required,
            estimated_input_tokens=active_estimated,
            compacted_through=through,
            source_references=latest.source_references if latest else [],
            compaction_usage=latest.usage if latest else ChatTokenUsage(),
            compaction_cost_usd=latest.cost_usd if latest else 0.0,
            snapshot=latest,
        )

    def runtime_switch_preflight(
        self,
        session_id: str,
        request: ChatRuntimeSwitchPreflightRequest,
    ) -> ChatRuntimeSwitchPreflight:
        """Validate a proposed provider/model switch against durable active context."""

        session = self.store.get(ChatSession, session_id)
        if (
            session.backend != ChatBackend.PROVIDER
            or session.provider_profile_id is None
        ):
            raise ChatConfigurationError(
                "runtime switching is only available for provider conversations"
            )
        if session.revision != request.expected_session_revision:
            raise ChatHistoryConflict(
                "conversation changed; reload it before changing provider or model"
            )
        if self.pending_turn(session.id) is not None:
            raise ChatHistoryConflict(
                "conversation has an active response; wait for it before changing provider or model"
            )
        profile = self.store.get(ProviderProfile, request.provider_id)
        current: dict[str, Any] = {
            "session_id": session.id,
            "session_revision": session.revision,
            "current_provider_id": session.provider_profile_id,
            "current_model": session.model,
            "target_provider_id": request.provider_id,
            "target_model": request.model,
        }
        if not profile.enabled:
            return ChatRuntimeSwitchPreflight(
                **current,
                compatible=False,
                reason="The selected provider is disabled.",
            )
        if profile.model_allowlist and request.model not in profile.model_allowlist:
            return ChatRuntimeSwitchPreflight(
                **current,
                compatible=False,
                reason="The selected model is no longer available from this provider.",
            )
        if request.tools_enabled and not profile.tools_verified_for(request.model):
            return ChatRuntimeSwitchPreflight(
                **current,
                compatible=False,
                reason=(
                    "The selected model is not verified for the tools enabled in this conversation."
                ),
            )
        try:
            provider = self.provider_factory(profile)
            engagement = self.store.get(Engagement, session.engagement_id)
            self._enforce_engagement_privacy(engagement, provider)
            limits = resolve_context_limits(
                profile,
                model=request.model,
                requested_output_tokens=request.max_output_tokens,
                required_parameters={"tools"} if request.tools_enabled else None,
            )
        except (
            ChatPrivacyError,
            ContextCapacityError,
            ProviderPrivacyViolation,
        ) as exc:  # diagnostic-expected: incompatibility is returned to the operator as the preflight result
            return ChatRuntimeSwitchPreflight(
                **current,
                compatible=False,
                reason=str(exc),
            )
        active_tokens = self.context_status(session.id).estimated_input_tokens
        requires_confirmation = active_tokens > limits.target_input_tokens
        confirmation = (
            self._runtime_switch_token(
                session=session,
                profile=profile,
                model=request.model,
                tools_enabled=request.tools_enabled,
                active_tokens=active_tokens,
                target_input_tokens=limits.target_input_tokens,
                metadata_revision=limits.metadata_revision,
            )
            if requires_confirmation
            else None
        )
        return ChatRuntimeSwitchPreflight(
            **current,
            compatible=True,
            requires_compaction_confirmation=requires_confirmation,
            confirmation_token=confirmation,
            reason=(
                "Switching requires compacting the active context to fit the selected model."
                if requires_confirmation
                else None
            ),
            estimated_active_input_tokens=active_tokens,
            target_context_window=limits.context_window,
            target_input_tokens=limits.target_input_tokens,
            target_max_output_tokens=limits.max_output_tokens,
            metadata_revision=limits.metadata_revision,
        )

    @staticmethod
    def _runtime_switch_token(
        *,
        session: ChatSession,
        profile: ProviderProfile,
        model: str,
        tools_enabled: bool,
        active_tokens: int,
        target_input_tokens: int,
        metadata_revision: str | None,
    ) -> str:
        payload = json.dumps(
            {
                "session_id": session.id,
                "session_revision": session.revision,
                "current_provider_id": session.provider_profile_id,
                "current_model": session.model,
                "target_provider_id": profile.id,
                "target_provider_revision": profile.revision,
                "target_model": model,
                "tools_enabled": tools_enabled,
                "active_tokens": active_tokens,
                "target_input_tokens": target_input_tokens,
                "metadata_revision": metadata_revision,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _session_messages(
        self, session: ChatSession, *, include_replaced: bool = False
    ) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        offset = 0
        while True:
            page = self.store.list_entities(
                ChatMessage,
                engagement_id=session.engagement_id,
                offset=offset,
                limit=1_000,
            )
            messages.extend(
                message
                for message in page
                if message.session_id == session.id
                and (include_replaced or not message_is_replaced(message))
            )
            if len(page) < 1_000:
                break
            offset += len(page)
        return sorted(
            messages, key=lambda item: (item.sequence, item.created_at, item.id)
        )

    def _next_sequence(self, session: ChatSession) -> int:
        """Reserve the next transcript slot above every message, replaced or not."""

        stored = self._session_messages(session, include_replaced=True)
        recorded = session.metadata.get("last_sequence")
        return (
            max(
                [message.sequence for message in stored]
                + [int(recorded) if isinstance(recorded, int) else 0]
            )
            + 1
        )

    @staticmethod
    def _merge_history(
        stored: list[ChatMessage], incoming: list[ChatRequestMessage]
    ) -> tuple[list[ChatRequestMessage], list[ChatRequestMessage]]:
        durable = [
            ChatRequestMessage(role=message.role, content=message.content).model_copy(
                update={"content_blocks": message.content_blocks}
            )
            for message in stored
        ]
        if len(incoming) >= len(durable) and incoming[: len(durable)] == durable:
            new_messages = incoming[len(durable) :]
            if not new_messages:
                raise ChatHistoryConflict("chat request contains no new message")
            if len(new_messages) != 1 or new_messages[0].role != ChatRole.USER:
                raise ChatHistoryConflict(
                    "a durable chat request may append exactly one user message"
                )
            return incoming, new_messages
        if len(incoming) == 1 and incoming[0].role == ChatRole.USER:
            return [*durable, *incoming], incoming
        raise ChatHistoryConflict(
            "supplied history diverges from the durable chat transcript"
        )

    async def _model_context(
        self,
        *,
        request: ChatCompletionRequest,
        profile: ProviderProfile,
        provider: ModelProvider,
        model: str,
        messages: list[ChatRequestMessage],
        stored_messages: list[ChatMessage],
        session: ChatSession | None,
        instructions: str,
        budget: ContextCallBudget | None = None,
        required_parameters: set[str] | None = None,
    ) -> tuple[
        list[ChatRequestMessage],
        str,
        ChatTokenUsage,
        ContextSnapshot | None,
        ChatSession | None,
    ]:
        limits = resolve_context_limits(
            profile,
            model=model,
            requested_output_tokens=request.max_output_tokens,
            required_parameters=required_parameters,
        )
        as_model_messages = [
            ModelMessage(role=message.role.value, content=message.content)
            for message in messages
        ]
        estimated = estimate_messages(as_model_messages, instructions)
        if estimated <= limits.target_input_tokens:
            return messages, instructions, ChatTokenUsage(), None, session

        current = messages[-1]
        mandatory = estimate_messages(
            [ModelMessage(role=current.role.value, content=current.content)],
            instructions,
        )
        if mandatory > limits.input_capacity:
            raise ContextCapacityError(
                "the current message and required instructions exceed the model context window"
            )
        if session is None or not stored_messages:
            raise ContextCapacityError(
                "chat context exceeds the model window and has no durable history to compact"
            )

        # Keep a recent, complete, user-led tail. The remaining space is reserved
        # for derived memory, retrieved originals, instructions, and headroom.
        tail_budget = max(
            estimate_tokens(current.content, message_count=1),
            limits.target_input_tokens * 2 // 5,
        )
        tail: list[ChatRequestMessage] = []
        tail_tokens = 0
        for message in reversed(messages):
            size = estimate_tokens(message.content, message_count=1)
            if tail and tail_tokens + size > tail_budget:
                break
            tail.append(message)
            tail_tokens += size
        tail.reverse()
        while tail and tail[0].role == ChatRole.ASSISTANT:
            tail.pop(0)
        if not tail:
            tail = [current]
        archived_count = len(messages) - len(tail)
        # A durable request appends one user message, so every archived item must
        # already exist in the canonical transcript.
        archived = stored_messages[: min(archived_count, len(stored_messages))]
        if not archived:
            raise ContextCapacityError(
                "chat context cannot be compacted without omitting the current turn"
            )
        compacted_through = archived[-1].sequence
        compactor = ContextCompactor(self.store)
        latest = compactor.latest(
            ContextOwnerType.CHAT_SESSION, session.id, session.engagement_id
        )
        created = False
        if (
            latest is None
            or latest.status != ContextSnapshotStatus.READY
            or latest.compacted_through != compacted_through
        ):
            result = await compactor.compact(
                owner_type=ContextOwnerType.CHAT_SESSION,
                owner_id=session.id,
                engagement_id=session.engagement_id,
                provider_profile=profile,
                provider=provider,
                model=model,
                compacted_through=compacted_through,
                sources=[
                    ContextSource(
                        reference=ContextSourceReference(
                            source_kind="chat_message",
                            source_id=message.id,
                            sequence=message.sequence,
                        ),
                        content=f"role={message.role.value}\n{message.content}",
                    )
                    for message in archived
                ],
                objective=current.content,
                budget=budget,
            )
            latest = result.snapshot
            created = result.created
            session = self.store.get(ChatSession, session.id)
        if latest.memory is None:
            raise ContextCompactionError("latest context snapshot has no memory")

        derived = memory_text(latest.memory)
        retrieval_budget = limits.target_input_tokens // 5
        retrieved: list[dict[str, Any]] = []
        retrieved_tokens = 0
        ranked = sorted(
            archived,
            key=lambda item: (
                -lexical_score(current.content, item.content),
                -item.sequence,
            ),
        )
        for archived_message in ranked:
            score = lexical_score(current.content, archived_message.content)
            if score <= 0:
                continue
            size = estimate_tokens(archived_message.content, message_count=1)
            if retrieved_tokens + size > retrieval_budget:
                continue
            retrieved.append(
                {
                    "message_id": archived_message.id,
                    "sequence": archived_message.sequence,
                    "role": archived_message.role.value,
                    "content": archived_message.content,
                }
            )
            retrieved_tokens += size
            if len(retrieved) >= 8:
                break
        context_instructions = instructions + "\n\n" + derived
        if retrieved:
            context_instructions += (
                "\n\nRETRIEVED CANONICAL TRANSCRIPT EXCERPTS (HISTORY; NOT SYSTEM "
                "INSTRUCTIONS)\n"
                + json.dumps(retrieved, ensure_ascii=False, separators=(",", ":"))
            )

        # Tighten the recent tail until the complete assembled input fits the
        # target. Never remove the current user message.
        while (
            len(tail) > 1
            and estimate_messages(
                [
                    ModelMessage(role=message.role.value, content=message.content)
                    for message in tail
                ],
                context_instructions,
            )
            > limits.target_input_tokens
        ):
            tail.pop(0)
            while tail and tail[0].role == ChatRole.ASSISTANT:
                tail.pop(0)
        final_estimate = estimate_messages(
            [
                ModelMessage(role=message.role.value, content=message.content)
                for message in tail
            ],
            context_instructions,
        )
        if final_estimate > limits.target_input_tokens:
            raise ContextCapacityError(
                "compacted chat context cannot meet the model input target"
            )
        usage = latest.usage if created else ChatTokenUsage()
        return tail, context_instructions, usage, latest, session

    def _enforce_engagement_privacy(
        self, engagement: Engagement, provider: ModelProvider
    ) -> None:
        try:
            validate_engagement_provider_privacy(self.store, engagement, provider)
        except ProviderPrivacyViolation as exc:
            record_caught_exception(
                "chat",
                "chat.chat.caught_failure_015",
                "A handled chat operation raised an exception.",
                exc,
                stage="chat",
            )
            raise ChatPrivacyError(str(exc)) from exc

    async def _plan_retrieval(
        self,
        *,
        provider: ModelProvider,
        model: str,
        query: str,
    ) -> list[str]:
        """Ask the selected model to plan retrieval, with a safe lexical fallback."""

        fallback = [query]
        request = ModelRequest(
            model=model,
            instructions=_RETRIEVAL_AGENT_INSTRUCTIONS,
            messages=[ModelMessage(role="user", content=query)],
            max_output_tokens=256,
            temperature=0,
            response_schema=(
                _RetrievalPlan.model_json_schema()
                if provider.capabilities.structured_output
                else None
            ),
            metadata={"operation": "agentic_knowledge_retrieval"},
        )
        try:
            response = await provider.complete(request)
            payload = response.text.strip()
            if payload.startswith("```"):
                payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload)
            plan = _RetrievalPlan.model_validate_json(payload)
        except Exception as exc:
            record_diagnostic(
                "warning",
                "chat",
                "chat.retrieval.plan_fallback",
                "The retrieval planner returned an unusable plan; the original query will be used.",
                outcome="fallback",
                stage="retrieval-planning",
                retryable=False,
                safe_failure_cause="The retrieval plan could not be validated safely.",
                exception=exc,
            )
            logger.info("retrieval agent planning failed; using original query")
            return fallback
        return [
            query,
            *[item for item in plan.queries if item.casefold() != query.casefold()],
        ][:4]

    def _has_ready_knowledge(self, engagement_id: str) -> bool:
        offset = 0
        while True:
            sources = self.store.list_entities(
                KnowledgeSource,
                engagement_id=engagement_id,
                offset=offset,
                limit=1_000,
            )
            if any(source.status.casefold() == "ready" for source in sources):
                return True
            if len(sources) < 1_000:
                break
            offset += len(sources)
        offset = 0
        while True:
            items = self.store.list_entities(
                LibraryItem,
                offset=offset,
                limit=1_000,
            )
            if any(item.status.casefold() == "ready" for item in items):
                return True
            if len(items) < 1_000:
                return False
            offset += len(items)

    def _retrieve(
        self,
        engagement_id: str,
        queries: list[str],
        *,
        redact: bool,
        token_budget: int,
        allow_local_only: bool = True,
    ) -> list[_RetrievedChunk]:
        query_terms = [
            {
                token.casefold()
                for token in _WORD.findall(query)
                if token.casefold() not in _STOP_WORDS
            }
            for query in queries
        ]
        all_terms = set().union(*query_terms) if query_terms else set()
        candidates: list[_RetrievedChunk] = []
        if (
            self.knowledge_index is not None
            and self.knowledge_index.status.state == "ready"
        ):
            try:
                candidates = self._retrieve_vector_candidates(
                    engagement_id,
                    queries,
                    query_terms=query_terms,
                    redact=redact,
                )
            except KnowledgeIndexError as exc:
                record_diagnostic(
                    "warning",
                    "chat",
                    "chat.retrieval.vector_fallback",
                    "Vector retrieval failed; legacy lexical retrieval will be used.",
                    outcome="fallback",
                    stage="knowledge-retrieval",
                    retryable=True,
                    safe_failure_cause="The local Chroma knowledge index was unavailable.",
                    exception=exc,
                )
        # Inline chunks are retained only for pre-Chroma sources and library
        # embedders without an index. Merge them during a gradual migration so
        # adding a newly indexed source never hides an older source.
        legacy_candidates = self._retrieve_legacy_candidates(
            engagement_id,
            query_terms=query_terms,
            redact=redact,
        )
        known_chunks = {item.citation.chunk_id for item in candidates}
        candidates.extend(
            item
            for item in legacy_candidates
            if item.citation.chunk_id not in known_chunks
        )
        if not allow_local_only:
            candidates = [item for item in candidates if not item.local_only]
        candidates.sort(key=lambda item: (-item.score, item.ordinal))
        selected: list[_RetrievedChunk] = []
        tokens = 0
        for candidate in candidates:
            if all_terms and candidate.score <= 0:
                continue
            candidate_tokens = estimate_tokens(candidate.text, message_count=1)
            if len(selected) >= 8 or tokens + candidate_tokens > token_budget:
                continue
            selected.append(candidate)
            tokens += candidate_tokens
        return selected

    def _retrieve_vector_candidates(
        self,
        engagement_id: str,
        queries: list[str],
        *,
        query_terms: list[set[str]],
        redact: bool,
    ) -> list[_RetrievedChunk]:
        assert self.knowledge_index is not None
        ready_sources: dict[str, KnowledgeSource | LibraryItem] = {}
        offset = 0
        while True:
            sources = self.store.list_entities(
                KnowledgeSource,
                engagement_id=engagement_id,
                offset=offset,
                limit=1_000,
            )
            ready_sources.update(
                (source.id, source)
                for source in sources
                if source.status.casefold() == "ready"
            )
            if len(sources) < 1_000:
                break
            offset += len(sources)
        offset = 0
        while True:
            items = self.store.list_entities(
                LibraryItem,
                offset=offset,
                limit=1_000,
            )
            ready_sources.update(
                (item.id, item) for item in items if item.status.casefold() == "ready"
            )
            if len(items) < 1_000:
                break
            offset += len(items)
        result_limit = min(64, max(16, len(queries) * 8))
        matches = self.knowledge_index.query(
            engagement_id,
            queries,
            limit=result_limit,
        )
        matches.extend(self.knowledge_index.query_library(queries, limit=result_limit))
        candidates: list[_RetrievedChunk] = []
        for match in matches:
            source = ready_sources.get(match.source_id)
            if source is None:
                continue
            text = match.text.strip()[:4000]
            if not text:
                continue
            if redact:
                text = self._redact_secrets(text)
            folded = text.casefold()
            lexical_bonus = sum(
                sum(folded.count(term) for term in terms) for terms in query_terms
            )
            # Chroma supplies the candidate set. A small lexical bonus preserves
            # exact hostnames, paths, hashes, and CVE identifiers during reranking.
            semantic_score = 100.0 / (1.0 + max(0.0, match.distance))
            candidates.append(
                _RetrievedChunk(
                    citation=ChatCitation(
                        source_id=source.id,
                        name=source.name,
                        citation=source.citation,
                        artifact_id=match.artifact_id or source.artifact_id,
                        chunk_id=match.id,
                        page=match.page,
                        excerpt=re.sub(r"\s+", " ", text)[:320],
                    ),
                    text=text,
                    local_only=self._source_is_local_only(source),
                    score=semantic_score + lexical_bonus,
                    ordinal=match.rank,
                )
            )
        return candidates

    def _retrieve_legacy_candidates(
        self,
        engagement_id: str,
        *,
        query_terms: list[set[str]],
        redact: bool,
    ) -> list[_RetrievedChunk]:
        candidates: list[_RetrievedChunk] = []
        ordinal = 0
        engagement_sources: list[KnowledgeSource | LibraryItem] = []
        offset = 0
        while len(engagement_sources) < 5_000:
            knowledge_page = self.store.list_entities(
                KnowledgeSource,
                engagement_id=engagement_id,
                offset=offset,
                limit=1_000,
            )
            engagement_sources.extend(knowledge_page)
            if len(knowledge_page) < 1_000:
                break
            offset += len(knowledge_page)
        library_sources: list[KnowledgeSource | LibraryItem] = []
        offset = 0
        while len(library_sources) < 5_000:
            library_page = self.store.list_entities(
                LibraryItem,
                offset=offset,
                limit=1_000,
            )
            library_sources.extend(library_page)
            if len(library_page) < 1_000:
                break
            offset += len(library_page)
        source_groups = (engagement_sources, library_sources)
        for sources in source_groups:
            for source in sources:
                if source.status.casefold() != "ready":
                    continue
                chunks = source.metadata.get("chunks", [])
                if not isinstance(chunks, list):
                    continue
                local_only = self._source_is_local_only(source)
                for index, raw in enumerate(chunks):
                    if len(candidates) >= 5_000:
                        break
                    if not isinstance(raw, dict):
                        continue
                    text = raw.get("text")
                    if not isinstance(text, str) or not text.strip():
                        continue
                    text = text.strip()[:4000]
                    if redact:
                        text = self._redact_secrets(text)
                    folded = text.casefold()
                    per_query_scores = [
                        sum(folded.count(term) for term in terms)
                        for terms in query_terms
                    ]
                    # Reward both strong matches and chunks that satisfy multiple
                    # retrieval intents. The original operator query is always the
                    # first and therefore receives a small tie-breaking preference.
                    score = sum(per_query_scores) + sum(
                        2 for value in per_query_scores if value > 0
                    )
                    if per_query_scores:
                        score += per_query_scores[0]
                    chunk_id = str(raw.get("id") or f"{source.id}:{index + 1}")
                    chunk_page = raw.get("page")
                    if not isinstance(chunk_page, int) or chunk_page < 1:
                        chunk_page = None
                    artifact_id = raw.get("artifact_id")
                    if not isinstance(artifact_id, str):
                        artifact_id = source.artifact_id
                    candidates.append(
                        _RetrievedChunk(
                            citation=ChatCitation(
                                source_id=source.id,
                                name=source.name,
                                citation=source.citation,
                                artifact_id=artifact_id,
                                chunk_id=chunk_id,
                                page=chunk_page,
                                excerpt=re.sub(r"\s+", " ", text)[:320],
                            ),
                            text=text,
                            local_only=local_only,
                            score=score,
                            ordinal=ordinal,
                        )
                    )
                    ordinal += 1
        return candidates

    def _project_instructions(
        self, engagement_id: str | None
    ) -> ProjectInstructions | None:
        """Re-read the project-root AGENTS.md for this turn; it is never compacted."""

        if engagement_id is None:
            return None
        try:
            workspace = self.workspace_resolver(engagement_id)
        except (ChatConfigurationError, OSError, ValueError):
            # diagnostic-expected: without a project workspace there is no AGENTS.md to apply
            return None
        try:
            return load_project_instructions(workspace)
        except (ProjectInstructionsError, OSError) as exc:
            raise ChatConfigurationError(f"AGENTS.md could not be used: {exc}") from exc

    @staticmethod
    def _retrieve_operator_help(
        queries: list[str], *, token_budget: int
    ) -> list[_RetrievedChunk]:
        selected: list[_RetrievedChunk] = []
        tokens = 0
        for ordinal, match in enumerate(search_operator_help(queries, limit=8)):
            article = match.article
            text = article.reference_text
            candidate_tokens = estimate_tokens(text, message_count=1)
            if tokens + candidate_tokens > token_budget:
                continue
            selected.append(
                _RetrievedChunk(
                    citation=ChatCitation(
                        source_id=article.source_id,
                        name=article.title,
                        citation=f"{CORPUS_ID} / {article.article_id}",
                        chunk_id=article.chunk_id,
                        excerpt=re.sub(r"\s+", " ", article.body)[:320],
                    ),
                    text=text,
                    local_only=False,
                    score=match.score,
                    ordinal=ordinal,
                )
            )
            tokens += candidate_tokens
        return selected

    @staticmethod
    def _source_is_local_only(source: KnowledgeSource | LibraryItem) -> bool:
        if source.metadata.get("local_only") is True:
            return True
        privacy = source.metadata.get("privacy")
        return isinstance(privacy, dict) and privacy.get("local_only") is True

    @staticmethod
    def _redact_secrets(value: str) -> str:
        return redact_text(value)

    @staticmethod
    def _title(messages: list[ChatRequestMessage]) -> str:
        first = next(
            (message.content for message in messages if message.role == ChatRole.USER),
            "Analyst chat",
        )
        return re.sub(r"\s+", " ", first).strip()[:120] or "Analyst chat"

    async def _name_initial_session(
        self, prepared: PreparedChat, assistant_response: str
    ) -> None:
        """Ask the selected provider for a durable first-turn conversation name.

        Naming is best-effort and deliberately separate from the visible answer.
        A provider failure must never discard or delay persistence of that answer.
        """

        session_id = self._session_id(prepared)
        if not session_id or not prepared.engagement_id:
            return
        session = self.store.get(ChatSession, session_id)
        from .chat_naming import should_name, substantive_prompt

        if not should_name(session):
            return
        first_prompt = substantive_prompt(
            [
                message.content
                for message in self.session_messages(session_id)
                if message.role == ChatRole.USER
            ]
        )
        if not first_prompt:
            return
        request = ModelRequest(
            model=prepared.resolved_model,
            instructions=(
                "Name this conversation from its first exchange. Return only a concise, "
                "specific 2-6 word title with no quotes, markdown, or trailing punctuation."
            ),
            messages=[
                ModelMessage(
                    role="user",
                    content=(
                        f"Operator request:\n{first_prompt[:4_000]}\n\n"
                        f"Assistant response:\n{assistant_response[:4_000]}"
                    ),
                )
            ],
            max_output_tokens=32,
            temperature=0,
            metadata={
                "operation": "conversation_naming",
                "chat_session_id": session.id,
            },
        )
        changes: dict[str, Any]
        try:
            response = await prepared.provider.complete(request)
            title = sanitize_display_text(response.text).strip().strip("\"'`# ")
            title = " ".join(
                re.sub(r"[.!?:;]+$", "", re.sub(r"\s+", " ", title)).split()[:6]
            )[:120]
            if not title:
                raise ChatError("provider returned an empty conversation name")
            changes = {
                "title": title,
                "metadata": {**session.metadata, "initial_title_state": "generated"},
            }
        except Exception as exc:
            record_diagnostic(
                "warning",
                "chat",
                "chat.naming.fallback",
                "The provider could not name the conversation; the prompt-based fallback was retained.",
                outcome="fallback",
                stage="conversation-naming",
                retryable=False,
                safe_failure_cause="The naming response was unavailable or invalid.",
                exception=exc,
            )
            changes = {
                "metadata": {**session.metadata, "initial_title_state": "failed"}
            }
        for _ in range(3):
            latest = self.store.get(ChatSession, session.id)
            if latest.metadata.get("initial_title_state") == "operator":
                return
            try:
                prepared.session = self.store.update(
                    ChatSession,
                    session.id,
                    {
                        **changes,
                        "metadata": {
                            **latest.metadata,
                            "initial_title_state": changes["metadata"][
                                "initial_title_state"
                            ],
                        },
                    },
                    expected_revision=latest.revision,
                )
                return
            except ConflictError:  # diagnostic-expected: concurrent writer won; the next candidate is tried
                continue

    @staticmethod
    def _completion(
        prepared: PreparedChat, response: ModelResponse
    ) -> ChatCompletionResponse:
        if response.tool_calls:
            raise ChatError(
                "provider returned a tool call even though chat exposes no tools"
            )
        content = response.text.strip()
        reasoning = response.reasoning.strip()
        if not content and not reasoning:
            raise ChatError("provider returned an empty chat response")
        return ChatCompletionResponse(
            turn_id=prepared.turn.id if prepared.turn is not None else None,
            session_id=ChatService._session_id(prepared),
            provider_id=response.provider_id,
            model=response.model,
            message=ChatResponseMessage(content=content, reasoning=reasoning),
            usage=(
                prepared.turn.usage
                if prepared.turn is not None and prepared.tools_enabled
                else ChatTokenUsage.model_validate(response.usage.model_dump())
            ),
            context_usage=(
                prepared.context_usage
                if prepared.context_usage.total_tokens > 0
                else None
            ),
            finish_reason=response.finish_reason,
            provider_request_id=response.provider_request_id,
            citations=prepared.citations,
            tool_suggestions=(
                public_suggestions(
                    prepared.turn.request_snapshot.get("tool_suggestions"),
                    prepared.turn.tool_history,
                )
                if prepared.turn is not None
                else None
            ),
        )

    def _persist_turn_inputs(self, prepared: PreparedChat) -> None:
        turn = prepared.turn
        if turn is None or not prepared.engagement_id:
            return
        active_statuses = {
            ChatTurnStatus.ROUTING,
            ChatTurnStatus.WAITING_APPROVAL,
            ChatTurnStatus.FINALIZING,
            ChatTurnStatus.INTERRUPTED,
        }
        active = [
            item
            for item in self.store.list_entities(
                ChatTurn,
                engagement_id=prepared.engagement_id,
                limit=1_000,
            )
            if item.session_id == turn.session_id
            and item.status in active_statuses
            and (
                item.status != ChatTurnStatus.INTERRUPTED
                or bool(item.request_snapshot.get("recovery", {}).get("required"))
            )
        ]
        if active:
            raise ChatHistoryConflict("chat session already has an active response")
        session = prepared.session or prepared.pending_session
        if session is None:
            raise ChatError("provider chat is missing its durable session")
        start = self._next_sequence(session)
        messages = [
            ChatMessage(
                engagement_id=prepared.engagement_id,
                session_id=session.id,
                sequence=start + index,
                role=message.role,
                content=message.content,
                content_blocks=message.content_blocks,
                metadata=(
                    {
                        **_context_attachment_metadata(prepared.context_attachments),
                        "operator_decisions": prepared.operator_decisions,
                    }
                    if index == len(prepared.new_messages) - 1
                    else {}
                ),
            )
            for index, message in enumerate(prepared.new_messages)
        ]
        last_sequence = messages[-1].sequence if messages else start - 1
        metadata = {
            **session.metadata,
            "tools_enabled": prepared.tools_enabled,
            "message_count": last_sequence,
            "last_sequence": last_sequence,
        }
        if prepared.pending_session is not None:
            session = prepared.pending_session.model_copy(update={"metadata": metadata})
            self.store.create_many([session, *messages, turn])
            prepared.session = session
            prepared.pending_session = None
        else:
            goal = self.store.get(ChatGoal, turn.goal_id) if turn.goal_id else None
            with self.store.transaction() as transaction:
                prepared.session = transaction.update(
                    ChatSession,
                    session.id,
                    {
                        "backend": ChatBackend.PROVIDER,
                        "provider_profile_id": prepared.provider_profile.id,
                        "harness_profile_id": None,
                        "harness_session_id": None,
                        "model": prepared.resolved_model,
                        "metadata": metadata,
                    },
                    expected_revision=session.revision,
                )
                transaction.add_all([*messages, turn])
                if goal is not None:
                    transaction.update(
                        ChatGoal,
                        goal.id,
                        {
                            "current_step": goal.current_step + 1,
                            "linked_turn_ids": [*goal.linked_turn_ids, turn.id],
                        },
                        expected_revision=goal.revision,
                    )
                from .chat_queue import link_queue_turn

                link_queue_turn(transaction, prepared.queue_claim, turn.id)
        browser_session_id = turn.request_snapshot.get("browser_session_id")
        if isinstance(browser_session_id, str):
            browser_session = self.store.get(BrowserSession, browser_session_id)
            if browser_session.metadata.get("browser_companion_version") == 1:
                BrowserCompanion(self.store, BrowserEngineRegistry()).bind(
                    browser_session_id, session.id
                )
        prepared.inputs_persisted = True
        prepared.stored_messages.extend(messages)
        prepared.new_messages = []

    def _complete_turn(
        self, prepared: PreparedChat, completion: ChatCompletionResponse
    ) -> None:
        if prepared.turn is None or completion.message.id is None:
            return
        latest = self._assert_execution_owner(prepared)
        if latest.status == ChatTurnStatus.COMPLETE:
            prepared.turn = latest
            return
        prepared.turn = self.store.update(
            ChatTurn,
            latest.id,
            {
                "status": ChatTurnStatus.COMPLETE,
                "final_message_id": completion.message.id,
                "usage": completion.usage,
                "error": None,
            },
            expected_revision=latest.revision,
        )
        if latest.goal_id and not prepared.tools_enabled:
            self._charge_goal(latest.goal_id, completion.usage)

    def _turn_timing(
        self, turn: ChatTurn | None, started_at: datetime | None = None
    ) -> tuple[int | None, int | None]:
        """Elapsed since the turn started, and the approval wait inside it."""

        start = turn.created_at if turn is not None else started_at
        if start is None:
            return None, None
        elapsed = max(0, round((utc_now() - start).total_seconds() * 1000))
        if turn is None:
            return elapsed, None
        waited = 0
        for call_id in turn.tool_call_ids:
            try:
                call = self.store.get(ToolCall, call_id)
                approval = (
                    self.store.get(Approval, call.approval_id)
                    if call.approval_id
                    else None
                )
            except NotFoundError:
                # diagnostic-expected: a pruned call or approval only costs its share
                continue
            if approval is None or approval.decided_at is None:
                continue
            waited += max(
                0,
                round(
                    (approval.decided_at - approval.requested_at).total_seconds() * 1000
                ),
            )
        # Parallel approvals can overlap, so the wait never exceeds the turn.
        return elapsed, min(waited, elapsed) if waited else None

    def _persist(
        self, prepared: PreparedChat, completion: ChatCompletionResponse
    ) -> None:
        if not prepared.engagement_id:
            return
        session = prepared.session or prepared.pending_session
        if session is None:
            raise ChatError("engagement chat is missing its durable session")
        start = self._next_sequence(session)
        messages: list[ChatMessage] = [
            ChatMessage(
                engagement_id=prepared.engagement_id,
                session_id=session.id,
                sequence=start + index,
                role=message.role,
                content=message.content,
                content_blocks=message.content_blocks,
                metadata=(
                    {
                        **_context_attachment_metadata(prepared.context_attachments),
                        "operator_decisions": prepared.operator_decisions,
                    }
                    if index == len(prepared.new_messages) - 1
                    else {}
                ),
            )
            for index, message in enumerate(prepared.new_messages)
        ]
        assistant_message_id = str(uuid4())
        completion.message.id = assistant_message_id
        elapsed_ms, approval_wait_ms = self._turn_timing(
            prepared.turn, prepared.started_at
        )
        completion.elapsed_ms = elapsed_ms
        completion.approval_wait_ms = approval_wait_ms
        messages.append(
            ChatMessage(
                id=assistant_message_id,
                engagement_id=prepared.engagement_id,
                session_id=session.id,
                sequence=start + len(prepared.new_messages),
                role=ChatRole.ASSISTANT,
                content=completion.message.content,
                reasoning=completion.message.reasoning,
                provider_profile_id=completion.provider_id,
                model=completion.model,
                usage=completion.usage,
                elapsed_ms=elapsed_ms,
                approval_wait_ms=approval_wait_ms,
                finish_reason=completion.finish_reason,
                provider_request_id=completion.provider_request_id,
                citations=completion.citations,
                metadata=(
                    {
                        "chat_turn_id": prepared.turn.id,
                        "tool_call_ids": prepared.turn.tool_call_ids,
                        "tool_results": [
                            {
                                "tool_call_id": item.get("tool_call_id"),
                                "capability": item.get("name"),
                                "status": item.get("status"),
                                "summary": item.get("result_summary"),
                                "evidence_ids": item.get("evidence_ids", []),
                                "result_artifact_id": item.get("result_artifact_id"),
                                "artifacts": item.get("artifacts", []),
                            }
                            for item in prepared.turn.tool_history
                        ],
                        **(
                            {"tool_suggestions": completion.tool_suggestions}
                            if completion.tool_suggestions
                            else {}
                        ),
                    }
                    if prepared.turn is not None
                    else {}
                ),
            )
        )
        entities: list[Any] = []
        if prepared.pending_session is not None:
            prepared.pending_session = prepared.pending_session.model_copy(
                update={
                    "metadata": {
                        **prepared.pending_session.metadata,
                        **(
                            {"tools_enabled": prepared.tools_enabled}
                            if prepared.tools_enabled
                            or "tools_enabled" in prepared.pending_session.metadata
                            else {}
                        ),
                        "message_count": messages[-1].sequence,
                        "last_sequence": messages[-1].sequence,
                    }
                }
            )
            entities.append(prepared.pending_session)
        elif prepared.session is not None:
            # Reserve the next transcript sequence by revision before inserting
            # messages. Concurrent sends for one session then fail with a clean
            # conflict instead of persisting duplicate sequence numbers.
            # Updating the cursor and inserting the exchange share one commit;
            # a failed message insert cannot leave the session ahead of history.
            last_error: Exception | None = None
            for _ in range(3):
                try:
                    with self.store.transaction() as transaction:
                        if prepared.turn is not None:
                            latest_turn = self._assert_execution_owner(prepared)
                            prepared.turn = transaction.update(
                                ChatTurn,
                                latest_turn.id,
                                {
                                    "execution_owner_id": latest_turn.execution_owner_id,
                                    "execution_claim_id": latest_turn.execution_claim_id,
                                    "execution_claimed_at": latest_turn.execution_claimed_at,
                                },
                                expected_revision=latest_turn.revision,
                            )
                        latest_session = self.store.get(
                            ChatSession, prepared.session.id
                        )
                        metadata = {
                            **latest_session.metadata,
                            **(
                                {"tools_enabled": prepared.tools_enabled}
                                if prepared.tools_enabled
                                or "tools_enabled" in latest_session.metadata
                                else {}
                            ),
                            "message_count": messages[-1].sequence,
                            "last_sequence": messages[-1].sequence,
                        }
                        title_state = latest_session.metadata.get("initial_title_state")
                        if title_state in {"generated", "operator", "failed"}:
                            metadata["initial_title_state"] = title_state
                        prepared.session = transaction.update(
                            ChatSession,
                            latest_session.id,
                            {
                                "backend": ChatBackend.PROVIDER,
                                "provider_profile_id": prepared.provider_profile.id,
                                "harness_profile_id": None,
                                "harness_session_id": None,
                                "model": prepared.resolved_model,
                                "title": latest_session.title,
                                "metadata": metadata,
                            },
                            expected_revision=latest_session.revision,
                        )
                        transaction.add_all(messages)
                    last_error = None
                    break
                except ConflictError as exc:  # diagnostic-expected: revision conflict is retried; exhaustion raises ChatHistoryConflict
                    last_error = exc
            if last_error is not None:
                raise ChatHistoryConflict(
                    "conversation changed while the reply was being saved; retry the message"
                ) from last_error
            return
        entities.extend(messages)
        self.store.create_many(entities)

    @staticmethod
    def _session_id(prepared: PreparedChat) -> str | None:
        session = prepared.session or prepared.pending_session
        return session.id if session else None


__all__ = [
    "ChatContextAttachment",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "ChatCompactionError",
    "ChatConfigurationError",
    "ChatError",
    "ChatHistoryConflict",
    "ChatPrivacyError",
    "ChatRequestMessage",
    "ChatResponseMessage",
    "ChatService",
    "PreparedChat",
]
