"""Provider-neutral, provenance-backed working-context compaction."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field

from .domain import (
    AgentAttempt,
    AgentRun,
    ChatMessage,
    ChatSession,
    ChatTokenUsage,
    ContextMemory,
    ContextMemoryItem,
    ContextOwnerType,
    ContextSnapshot,
    ContextSnapshotStatus,
    ContextSourceReference,
    ProviderProfile,
    RunEvent,
    Task,
    utc_now,
)
from .known_model_limits import KNOWN_MODEL_LIMITS, KNOWN_MODEL_LIMITS_REVISION
from .model_catalog import route_limits_verified as descriptor_routes_verified
from .providers import (
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ProviderError,
    json_schema_instruction,
)
from .storage import ConflictError, NebulaStore, NotFoundError

DEFAULT_CONTEXT_WINDOW = 8_192
# Output allowance when the model's own output limit is unknown.
DEFAULT_MAX_OUTPUT_TOKENS = 2_048
# When the model's output limit is known, the default allowance is that limit,
# held to opencode's 32,000-token OUTPUT_TOKEN_MAX and to a quarter of the
# window, so input keeps at least the three quarters the 2,048-of-8,192
# fallback has always left it. An explicit request may still ask for more.
KNOWN_MODEL_OUTPUT_CEILING = 32_000
DEFAULT_OUTPUT_WINDOW_DIVISOR = 4
CONTEXT_TARGET_FRACTION = 0.75
COMPACTOR_INPUT_FRACTION = 0.60
COMPACTOR_OUTPUT_FRACTION = 0.05
COMPACTOR_MIN_OUTPUT_TOKENS = 32
# Instructions, message framing, and the JSON envelope around each compactor
# request; reserved together with the objective before segments are budgeted.
COMPACTOR_PROMPT_OVERHEAD_TOKENS = 64
CONTEXT_PROMPT_VERSION = "nebula-context-v1"

_HOSTED_MODEL_PREFIX = re.compile(
    r"^(?:(?:us|eu|apac|au|jp|global)\.)?"
    r"(?:anthropic|amazon|meta|mistral|deepseek|qwen|openai|cohere|moonshot|"
    r"moonshotai|minimax|zai|writer)\."
)
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{1,}")
# How providers name a refused structured-output request: DeepSeek's "This
# response_format type is unavailable now", OpenAI-style "'response_format'
# of type 'json_schema' is not supported".
_RESPONSE_FORMAT_REJECTION = re.compile(r"response[_ ]format|json[_ ]schema", re.I)
_SECURITY_IDENTIFIER = re.compile(
    r"(?i)(?:CVE-\d{4}-\d{4,}|[a-f0-9]{32,}|"
    r"(?:artifact|task|attempt|evidence)[-_:#][A-Za-z0-9][A-Za-z0-9_.:-]*|"
    r"(?:\d{1,3}\.){3}\d{1,3}|(?:/[^\s]+)+|\b\d{2,5}\b)"
)


class ContextCompactionError(RuntimeError):
    """A required compaction could not produce validated working memory."""

    def __init__(self, message: str, *, usage: ChatTokenUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage or ChatTokenUsage()


class ContextCapacityError(ContextCompactionError):
    """Mandatory request material cannot fit the declared model window."""


class ContextLimits(BaseModel):
    model: str | None = None
    context_window: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    input_capacity: int = Field(ge=1)
    target_input_tokens: int = Field(ge=1)
    compacted_input_target: int = Field(ge=1)
    source: str = Field(pattern=r"^(model_catalog|known_model|configured|fallback)$")
    estimated: bool = False
    metadata_revision: str | None = None
    route_limits_verified: bool = False
    eligible_route_count: int | None = Field(default=None, ge=0)
    route_context_window: int | None = Field(default=None, ge=1)
    route_input_limit: int | None = Field(default=None, ge=1)
    route_limits_required: bool = False


class ContextStatus(BaseModel):
    owner_type: ContextOwnerType
    owner_id: str
    status: str = Field(pattern=r"^(not_needed|ready|stale|failed|runtime_managed)$")
    context_window: int
    max_output_tokens: int
    target_input_tokens: int
    compacted_input_target: int | None = Field(default=None, ge=1)
    capacity_source: str | None = Field(
        default=None,
        pattern=r"^(model_catalog|known_model|configured|fallback|runtime)$",
    )
    capacity_estimated: bool = False
    metadata_revision: str | None = None
    route_limits_verified: bool = False
    eligible_route_count: int | None = Field(default=None, ge=0)
    route_context_window: int | None = Field(default=None, ge=1)
    route_input_limit: int | None = Field(default=None, ge=1)
    route_limits_required: bool = False
    estimated_input_tokens: int = Field(default=0, ge=0)
    compacted_through: int = Field(default=0, ge=0)
    source_references: list[ContextSourceReference] = Field(default_factory=list)
    compaction_usage: ChatTokenUsage = Field(default_factory=ChatTokenUsage)
    compaction_cost_usd: float = Field(default=0.0, ge=0)
    snapshot: ContextSnapshot | None = None


@dataclass(frozen=True)
class ContextSource:
    reference: ContextSourceReference
    content: str
    provenance: tuple[ContextSourceReference, ...] = ()


@dataclass(frozen=True)
class CompactionResult:
    snapshot: ContextSnapshot
    created: bool


@dataclass(frozen=True)
class ContextCallBudget:
    """Remaining mission budget available to the complete compaction attempt."""

    max_tokens: int | None = None
    max_cost_usd: float | None = None


def _positive_option(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return fallback


def known_model_limits(model: str | None) -> tuple[int, int | None] | None:
    """Published (context_window, max_output_tokens) for a well-known hosted model.

    Provider spellings of one model are folded together: vendor paths
    ("deepseek/deepseek-v3.2", "models/gemini-2.5-pro"), Bedrock prefixes and
    version suffixes ("us.anthropic.claude-opus-5-v1:0"), Vertex snapshots
    ("claude-haiku-4-5@20251001") and dotted versions. The longest known id that
    the model equals or extends with a "-" suffix (dated snapshot) wins.
    """

    if not model:
        return None
    normalized = model.strip().lower().rsplit("/", 1)[-1]
    normalized = normalized.split(":", 1)[0].split("@", 1)[0]
    normalized = _HOSTED_MODEL_PREFIX.sub("", normalized)
    normalized = normalized.replace(".", "-").replace("_", "-")
    candidate = normalized
    while candidate:
        if candidate in KNOWN_MODEL_LIMITS:
            return KNOWN_MODEL_LIMITS[candidate]
        candidate = candidate.rpartition("-")[0]
    return None


def resolve_context_limits(
    profile: ProviderProfile,
    *,
    model: str | None = None,
    requested_output_tokens: int | None = None,
    required_parameters: set[str] | None = None,
) -> ContextLimits:
    options = profile.metadata.get("options", {})
    if not isinstance(options, dict):
        options = {}
    configured_window = _positive_option(options.get("context_window"), 0)
    configured_output = _positive_option(options.get("max_output_tokens"), 0)
    descriptors = profile.metadata.get("model_descriptors", [])
    descriptor = next(
        (
            item
            for item in descriptors
            if isinstance(item, dict) and model is not None and item.get("id") == model
        ),
        None,
    )
    model_window = (
        _positive_option(descriptor.get("context_window"), 0)
        if isinstance(descriptor, dict)
        else 0
    )
    model_output = (
        _positive_option(descriptor.get("max_output_tokens"), 0)
        if isinstance(descriptor, dict)
        else 0
    )
    input_limit = (
        _positive_option(descriptor.get("max_input_tokens"), 0)
        if isinstance(descriptor, dict)
        else 0
    )
    known_model = False
    if not model_window and not profile.is_local:
        # Local runtimes serve their own configured window (e.g. Ollama num_ctx),
        # not the model's published maximum.
        known = known_model_limits(model)
        if known is not None:
            model_window, model_output = known[0], model_output or known[1] or 0
            known_model = True
    route_limits_verified = model is not None and descriptor_routes_verified(
        descriptor, model
    )
    route_context_window = 0
    route_input_limit = 0
    route_output_limit = 0
    eligible_route_count: int | None = None
    if route_limits_verified:
        required = required_parameters or set()
        raw_routes = (
            descriptor.get("route_limits", []) if isinstance(descriptor, dict) else []
        )
        eligible_routes = []
        for route in raw_routes:
            if not isinstance(route, dict) or route.get("status", 0) != 0:
                continue
            parameters = route.get("supported_parameters", [])
            supported = (
                {item for item in parameters if isinstance(item, str)}
                if isinstance(parameters, list)
                else set()
            )
            if required <= supported:
                eligible_routes.append(route)
        eligible_route_count = len(eligible_routes)
        if not eligible_routes:
            requirement = ", ".join(sorted(required)) or "text generation"
            raise ContextCapacityError(
                f"no verified OpenRouter endpoint supports {requirement} for {model}"
            )
        route_windows = [
            _positive_option(item.get("context_window"), 0) for item in eligible_routes
        ]
        route_inputs = [
            _positive_option(item.get("max_input_tokens"), 0)
            for item in eligible_routes
        ]
        route_outputs = [
            _positive_option(item.get("max_output_tokens"), 0)
            for item in eligible_routes
        ]
        if not all(route_windows) or not all(route_inputs) or not all(route_outputs):
            raise ContextCapacityError(
                f"verified OpenRouter endpoint limits are incomplete for {model}"
            )
        route_context_window = min(route_windows)
        route_input_limit = min(route_inputs)
        route_output_limit = min(route_outputs)
        model_window = min(
            value for value in (model_window, route_context_window) if value
        )
        model_output = min(
            value for value in (model_output, route_output_limit) if value
        )
        input_limit = min(value for value in (input_limit, route_input_limit) if value)
    elif profile.provider_type == "openrouter":
        # Aggregate model metadata is not proof that every automatic route can
        # accept that window: `context_window` is the maximum across endpoints
        # and automatic routing may land on a smaller one. Hold the request to
        # the primary route OpenRouter publishes instead. Without that figure
        # nothing bounds the routing set, so only the flat floor is safe.
        primary_window = (
            _positive_option(descriptor.get("primary_route_context_window"), 0)
            if isinstance(descriptor, dict)
            else 0
        )
        window_cap = primary_window or DEFAULT_CONTEXT_WINDOW
        model_window = model_window or primary_window
        if model_window:
            model_window = min(model_window, window_cap)
        if configured_window:
            configured_window = min(configured_window, window_cap)
        if not primary_window:
            # `max_output_tokens` is that same route's completion limit, so it
            # needs no floor of its own once the route's window is known.
            if model_output:
                model_output = min(model_output, DEFAULT_MAX_OUTPUT_TOKENS)
            if configured_output:
                configured_output = min(configured_output, DEFAULT_MAX_OUTPUT_TOKENS)
    if model_window:
        context_window = min(
            value for value in (model_window, configured_window) if value
        )
        source = "known_model" if known_model else "model_catalog"
        estimated = profile.provider_type == "openrouter" and not route_limits_verified
    elif configured_window:
        context_window = configured_window
        source = "configured"
        estimated = True
    else:
        context_window = DEFAULT_CONTEXT_WINDOW
        source = "fallback"
        estimated = True
    output_caps = [max(1, context_window - 1)]
    if model_output:
        output_caps.append(model_output)
    if configured_output:
        output_caps.append(configured_output)
    default_output = min(DEFAULT_MAX_OUTPUT_TOKENS, *output_caps)
    if model_output or configured_output:
        # Reasoning models spend their thinking from this same allowance, so
        # a flat 2,048 cuts them off mid-thought. Size it from the known
        # limit instead, never below what the fallback already allowed.
        default_output = max(
            default_output,
            min(
                KNOWN_MODEL_OUTPUT_CEILING,
                context_window // DEFAULT_OUTPUT_WINDOW_DIVISOR,
                *output_caps,
            ),
        )
    output = min(requested_output_tokens or default_output, *output_caps)
    input_capacity = context_window - output
    if input_limit:
        input_capacity = min(input_capacity, input_limit)
    metadata_revision = (
        profile.metadata.get("route_catalog_revision")
        or profile.metadata.get("model_catalog_revision")
        or (f"known-models:{KNOWN_MODEL_LIMITS_REVISION}" if known_model else None)
    )
    return ContextLimits(
        model=model,
        context_window=context_window,
        max_output_tokens=output,
        input_capacity=input_capacity,
        target_input_tokens=max(
            1, math.floor(input_capacity * CONTEXT_TARGET_FRACTION)
        ),
        compacted_input_target=max(
            1, math.floor(input_capacity * COMPACTOR_INPUT_FRACTION)
        ),
        source=source,
        estimated=estimated,
        metadata_revision=(
            metadata_revision if isinstance(metadata_revision, str) else None
        ),
        route_limits_verified=route_limits_verified,
        eligible_route_count=eligible_route_count,
        route_context_window=route_context_window or None,
        route_input_limit=route_input_limit or None,
        route_limits_required=profile.provider_type == "openrouter",
    )


def default_output_tokens(
    store: NebulaStore,
    provider_id: str | None,
    model: str | None,
    *,
    token_budget: int | None = None,
) -> int:
    """Output allowance for a mission call that names no maximum of its own.

    Sized the way chat sizes a turn, from the stored profile's limits for the
    model; without them it is the 2,048-token fallback. A smaller mission
    token budget still bounds it.
    """

    output = DEFAULT_MAX_OUTPUT_TOKENS
    if provider_id:
        try:
            profile = store.get(ProviderProfile, provider_id)
            output = resolve_context_limits(profile, model=model).max_output_tokens
        except (
            NotFoundError,
            ContextCapacityError,
        ):  # diagnostic-expected: the fallback allowance applies; the provider call reports any routing failure itself
            output = DEFAULT_MAX_OUTPUT_TOKENS
    return min(output, token_budget) if token_budget else output


def estimate_tokens(value: str, *, message_count: int = 0) -> int:
    """Conservatively estimate provider-neutral tokens without model dependencies."""

    byte_estimate = math.ceil(len(value.encode("utf-8")) / 3)
    return max(1, byte_estimate) + message_count * 8


def estimate_messages(messages: Iterable[ModelMessage], instructions: str = "") -> int:
    values = list(messages)
    total = estimate_tokens(instructions)
    for message in values:
        if isinstance(message.content, str):
            total += estimate_tokens(message.content, message_count=1)
            continue
        total += 8
        for block in message.content:
            if not isinstance(block, dict):
                total += estimate_tokens(str(block))
                continue
            if block.get("type") == "image":
                # Raw/base64 bytes do not map to text tokens. Reserve a conservative
                # image budget and count only textual metadata here.
                total += 2_048
                text_metadata = {
                    key: value
                    for key, value in block.items()
                    if key not in {"data", "image_url", "url"}
                }
                total += estimate_tokens(
                    json.dumps(text_metadata, ensure_ascii=False, separators=(",", ":"))
                )
            else:
                total += estimate_tokens(
                    json.dumps(block, ensure_ascii=False, separators=(",", ":"))
                )
    return total


def estimate_model_request(request: ModelRequest) -> int:
    """Estimate every text/schema component sent on one provider request."""

    total = estimate_messages(request.messages, request.instructions or "")
    for value in (
        request.tools,
        request.tool_results,
        request.response_schema,
        request.metadata.get("continuation") if request.metadata else None,
    ):
        if not value:
            continue
        payload = (
            [item.model_dump(mode="json") for item in value]
            if isinstance(value, list) and value and hasattr(value[0], "model_dump")
            else value
        )
        total += estimate_tokens(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        )
    # What a tool showed the model (a screenshot) is left out of the dump
    # above; it is counted as the message content it is sent as.
    attachments = [
        ModelMessage(role="user", content=result.attachments)
        for result in request.tool_results
        if result.attachments
    ]
    if attachments:
        total += estimate_messages(attachments)
    return total


def lexical_score(query: str, content: str) -> int:
    query_terms = {term.casefold() for term in _WORD.findall(query)}
    folded = content.casefold()
    score = sum(folded.count(term) for term in query_terms)
    identifiers = {item.casefold() for item in _SECURITY_IDENTIFIER.findall(query)}
    score += 20 * sum(identifier in folded for identifier in identifiers)
    return score


def memory_text(memory: ContextMemory) -> str:
    def section(title: str, items: list[ContextMemoryItem]) -> list[str]:
        return [title, *(f"- {item.text}" for item in items)] if items else []

    lines = ["DERIVED WORKING MEMORY (not authoritative evidence)"]
    if memory.objective:
        lines.append(f"Objective: {memory.objective}")
    lines.extend(["Summary:", memory.summary])
    for title, items in (
        ("Confirmed facts:", memory.confirmed_facts),
        ("Decisions:", memory.decisions),
        ("Constraints:", memory.constraints),
        ("Corrections:", memory.corrections),
        ("Open questions:", memory.open_questions),
    ):
        lines.extend(section(title, items))
    if memory.evidence_ids:
        lines.append("Evidence IDs: " + ", ".join(memory.evidence_ids))
    if memory.artifact_ids:
        lines.append("Artifact IDs: " + ", ".join(memory.artifact_ids))
    return "\n".join(lines)


class ContextCompactor:
    """Create immutable context snapshots using an explicitly selected model."""

    def __init__(self, store: NebulaStore) -> None:
        self.store = store
        locks = getattr(store, "_context_compaction_locks", None)
        if locks is None:
            locks = {}
            setattr(store, "_context_compaction_locks", locks)
        self._locks: dict[tuple[str, str], asyncio.Lock] = locks

    def snapshots(
        self, owner_type: ContextOwnerType, owner_id: str, engagement_id: str
    ) -> list[ContextSnapshot]:
        found: list[ContextSnapshot] = []
        offset = 0
        while True:
            page = self.store.list_entities(
                ContextSnapshot,
                engagement_id=engagement_id,
                offset=offset,
                limit=1_000,
            )
            found.extend(
                item
                for item in page
                if item.owner_type == owner_type and item.owner_id == owner_id
            )
            if len(page) < 1_000:
                break
            offset += len(page)
        return sorted(found, key=lambda item: (item.version, item.created_at, item.id))

    def latest(
        self, owner_type: ContextOwnerType, owner_id: str, engagement_id: str
    ) -> ContextSnapshot | None:
        snapshots = self.snapshots(owner_type, owner_id, engagement_id)
        return snapshots[-1] if snapshots else None

    async def compact(
        self,
        *,
        owner_type: ContextOwnerType,
        owner_id: str,
        engagement_id: str,
        provider_profile: ProviderProfile,
        provider: ModelProvider,
        model: str,
        sources: list[ContextSource],
        compacted_through: int,
        objective: str | None = None,
        budget: ContextCallBudget | None = None,
    ) -> CompactionResult:
        lock = self._locks.setdefault((owner_type.value, owner_id), asyncio.Lock())
        async with lock:
            return await self._compact_serialized(
                owner_type=owner_type,
                owner_id=owner_id,
                engagement_id=engagement_id,
                provider_profile=provider_profile,
                provider=provider,
                model=model,
                sources=sources,
                compacted_through=compacted_through,
                objective=objective,
                budget=budget,
            )

    async def _compact_serialized(
        self,
        *,
        owner_type: ContextOwnerType,
        owner_id: str,
        engagement_id: str,
        provider_profile: ProviderProfile,
        provider: ModelProvider,
        model: str,
        sources: list[ContextSource],
        compacted_through: int,
        objective: str | None,
        budget: ContextCallBudget | None,
    ) -> CompactionResult:
        if not sources:
            raise ContextCompactionError(
                "context compaction requires canonical sources"
            )
        self._validate_canonical_sources(owner_type, owner_id, sources)
        canonical = json.dumps(
            [
                {
                    "reference": source.reference.model_dump(mode="json"),
                    "content": source.content,
                }
                for source in sources
            ],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        source_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        previous = self.snapshots(owner_type, owner_id, engagement_id)
        for snapshot in reversed(previous):
            if (
                snapshot.status == ContextSnapshotStatus.READY
                and snapshot.source_sha256 == source_sha256
                and snapshot.provider_profile_id == provider_profile.id
                and snapshot.model == model
                and snapshot.prompt_version == CONTEXT_PROMPT_VERSION
            ):
                return CompactionResult(snapshot=snapshot, created=False)
        version = previous[-1].version + 1 if previous else 1
        snapshot_id = str(
            uuid5(
                NAMESPACE_URL,
                f"nebula-context:{owner_type.value}:{owner_id}:{source_sha256}:"
                f"{model}:{CONTEXT_PROMPT_VERSION}:{version}",
            )
        )
        all_references: list[ContextSourceReference] = []
        seen_references: set[tuple[str, str, int | None]] = set()
        for source in sources:
            key = self._reference_key(source.reference)
            if key not in seen_references:
                all_references.append(source.reference)
                seen_references.add(key)
        usage = ChatTokenUsage()
        try:
            memory, usage = await self._hierarchical_memory(
                provider=provider,
                profile=provider_profile,
                model=model,
                sources=sources,
                objective=objective,
                budget=budget,
            )
            self._validate_memory_sources(
                memory, {self._reference_key(item) for item in all_references}
            )
            snapshot = ContextSnapshot(
                id=snapshot_id,
                engagement_id=engagement_id,
                owner_type=owner_type,
                owner_id=owner_id,
                version=version,
                status=ContextSnapshotStatus.READY,
                compacted_through=compacted_through,
                memory=memory,
                source_references=all_references,
                provider_profile_id=provider_profile.id,
                model=model,
                prompt_version=CONTEXT_PROMPT_VERSION,
                source_sha256=source_sha256,
                usage=usage,
                cost_usd=self._cost(provider, usage),
            )
        except Exception as exc:
            record_caught_exception(
                "knowledge",
                "knowledge.context.caught_failure_001",
                "A handled knowledge operation raised an exception.",
                exc,
                stage="context",
            )
            if isinstance(exc, ContextCompactionError):
                usage = exc.usage
            safe_error = self._safe_error(exc)
            snapshot = ContextSnapshot(
                id=snapshot_id,
                engagement_id=engagement_id,
                owner_type=owner_type,
                owner_id=owner_id,
                version=version,
                status=ContextSnapshotStatus.FAILED,
                compacted_through=compacted_through,
                source_references=all_references,
                provider_profile_id=provider_profile.id,
                model=model,
                prompt_version=CONTEXT_PROMPT_VERSION,
                source_sha256=source_sha256,
                usage=usage,
                cost_usd=self._cost(provider, usage),
                error=safe_error,
            )
            self._persist(snapshot)
            if isinstance(exc, ContextCapacityError):
                # Keep the capacity subclass: chat maps it to a configuration
                # error the operator must act on, not a retryable failure.
                raise ContextCapacityError(safe_error, usage=usage) from exc
            raise ContextCompactionError(safe_error, usage=usage) from exc
        self._persist(snapshot)
        return CompactionResult(snapshot=snapshot, created=True)

    async def _hierarchical_memory(
        self,
        *,
        provider: ModelProvider,
        profile: ProviderProfile,
        model: str,
        sources: list[ContextSource],
        objective: str | None,
        budget: ContextCallBudget | None,
    ) -> tuple[ContextMemory, ChatTokenUsage]:
        limits = resolve_context_limits(profile, model=model)
        summary_output_tokens = min(
            limits.max_output_tokens,
            math.floor(limits.compacted_input_target * COMPACTOR_OUTPUT_FRACTION),
        )
        if summary_output_tokens < COMPACTOR_MIN_OUTPUT_TOKENS:
            raise ContextCapacityError(
                "model context leaves too little room for a faithful compaction summary"
            )
        complete_prompt_capacity = limits.input_capacity - summary_output_tokens
        # Every compactor request (each segment and every roll-up) embeds the
        # objective beside the sources, and the memory schema when it cannot go
        # on the wire, so the segment budget is carved from what remains after
        # those reservations rather than the whole capacity.
        segment_capacity = (
            complete_prompt_capacity
            - self._objective_reserve(objective)
            - self._schema_reserve(provider)
        )
        if segment_capacity < COMPACTOR_MIN_OUTPUT_TOKENS:
            raise ContextCapacityError(
                "compaction objective leaves too little room for context segments"
            )
        segment_budget = max(1, math.floor(segment_capacity * COMPACTOR_INPUT_FRACTION))
        pending = self._split_sources(sources, segment_budget)
        total = ChatTokenUsage()
        while len(pending) > 1:
            memories: list[tuple[ContextMemory, list[ContextSource]]] = []
            groups = self._group_sources(pending, segment_budget)
            for group in groups:
                try:
                    memory, usage = await self._request_memory(
                        provider,
                        model,
                        group,
                        objective,
                        summary_output_tokens,
                        input_capacity=complete_prompt_capacity,
                        prior_usage=total,
                        budget=budget,
                    )
                except ContextCompactionError as exc:
                    record_caught_exception(
                        "knowledge",
                        "knowledge.context.caught_failure_002",
                        "A handled knowledge operation raised an exception.",
                        exc,
                        stage="context",
                    )
                    exc.usage = self._add_usage(total, exc.usage)
                    raise
                total = self._add_usage(total, usage)
                memories.append((memory, group))
            if len(memories) == 1:
                return memories[0][0], total
            pending = [
                ContextSource(
                    reference=ContextSourceReference(
                        source_kind="context_segment",
                        source_id=f"level:{index}",
                        sequence=index + 1,
                    ),
                    content=json.dumps(
                        memory.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    provenance=tuple(self._memory_references(memory)),
                )
                for index, (memory, group) in enumerate(memories)
            ]
            # Roll-up prompts contain original references inside each memory item.
            if len(pending) > 1 and len(
                self._group_sources(pending, segment_budget)
            ) == len(pending):
                raise ContextCapacityError(
                    "derived context cannot fit compactor window"
                )
        try:
            memory, call_usage = await self._request_memory(
                provider,
                model,
                pending,
                objective,
                summary_output_tokens,
                input_capacity=complete_prompt_capacity,
                prior_usage=total,
                budget=budget,
            )
        except ContextCompactionError as exc:
            record_caught_exception(
                "knowledge",
                "knowledge.context.caught_failure_003",
                "A handled knowledge operation raised an exception.",
                exc,
                stage="context",
            )
            exc.usage = self._add_usage(total, exc.usage)
            raise
        return memory, self._add_usage(total, call_usage)

    @staticmethod
    def _objective_reserve(objective: str | None) -> int:
        return (
            estimate_tokens(
                json.dumps(
                    {"objective": objective},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            + COMPACTOR_PROMPT_OVERHEAD_TOKENS
        )

    @staticmethod
    def _schema_reserve(provider: ModelProvider) -> int:
        """Instruction room the memory schema takes without structured output."""

        if provider.capabilities.structured_output:
            return 0
        return estimate_tokens(
            json_schema_instruction(ContextMemory.model_json_schema())
        )

    @classmethod
    def _split_sources(
        cls, sources: list[ContextSource], token_budget: int
    ) -> list[ContextSource]:
        result: list[ContextSource] = []
        for source in sources:
            if cls._source_tokens(source) <= token_budget:
                result.append(source)
                continue
            empty_source = ContextSource(
                reference=source.reference,
                content="",
                provenance=source.provenance,
            )
            source_budget = max(1, token_budget - cls._source_tokens(empty_source) - 8)
            max_bytes = max(1, (source_budget - 16) * 3)
            parts: list[str] = []
            start = 0
            byte_count = 0
            for index, character in enumerate(source.content):
                size = len(character.encode("utf-8"))
                if byte_count and byte_count + size > max_bytes:
                    parts.append(source.content[start:index])
                    start = index
                    byte_count = 0
                byte_count += size
            if start < len(source.content):
                parts.append(source.content[start:])
            for index, part in enumerate(parts, start=1):
                result.append(
                    ContextSource(
                        reference=source.reference,
                        content=f"[part {index} of {len(parts)}]\n{part}",
                        provenance=source.provenance,
                    )
                )
        return result

    @classmethod
    def _group_sources(
        cls, sources: list[ContextSource], token_budget: int
    ) -> list[list[ContextSource]]:
        groups: list[list[ContextSource]] = []
        current: list[ContextSource] = []
        current_tokens = 0
        for source in sources:
            size = cls._source_tokens(source)
            if current and current_tokens + size > token_budget:
                groups.append(current)
                current = []
                current_tokens = 0
            current.append(source)
            current_tokens += size
        if current:
            groups.append(current)
        return groups

    @classmethod
    def _source_payload(cls, source: ContextSource) -> dict[str, Any]:
        return {
            "reference": source.reference.model_dump(mode="json"),
            "canonical_references": [
                reference.model_dump(mode="json")
                for reference in cls._canonical_references(source)
            ],
            "text": source.content,
        }

    @classmethod
    def _source_tokens(cls, source: ContextSource) -> int:
        return estimate_tokens(
            json.dumps(
                cls._source_payload(source),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    async def _request_memory(
        self,
        provider: ModelProvider,
        model: str,
        sources: list[ContextSource],
        objective: str | None,
        max_output_tokens: int,
        *,
        input_capacity: int,
        prior_usage: ChatTokenUsage,
        budget: ContextCallBudget | None,
    ) -> tuple[ContextMemory, ChatTokenUsage]:
        allowed = {
            self._reference_key(reference)
            for source in sources
            for reference in self._canonical_references(source)
        }
        payload = [self._source_payload(source) for source in sources]
        instructions = "Return structured working memory matching the supplied schema."
        prompt = json.dumps(
            {"objective": objective, "sources": payload},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        usage = ChatTokenUsage()
        last_error = "invalid structured memory"
        previous_output = ""
        schema = ContextMemory.model_json_schema()
        # Whether the schema goes on the wire (response_format) or in the
        # instructions: for a provider without structured output, and once a
        # provider has refused that parameter.
        wire_schema = provider.capabilities.structured_output
        schema_in_prompt = not wire_schema
        attempt = 0
        while attempt < 2:
            messages = [ModelMessage(role="user", content=prompt)]
            if attempt:
                # The rejected output goes back as the model's own turn, so
                # the repair request alternates roles as strict chat templates
                # and Bedrock require, and the model sees what it answered.
                messages.extend(
                    [
                        ModelMessage(
                            role="assistant",
                            content=previous_output[:8_000].strip()
                            or "(The previous response contained no text.)",
                        ),
                        ModelMessage(
                            role="user",
                            content=(
                                "The previous response failed validation. Repair it "
                                "and return only valid JSON. Validation error: "
                                f"{last_error}."
                            ),
                        ),
                    ]
                )
            request = ModelRequest(
                model=model,
                instructions=(
                    f"{instructions}\n\n{json_schema_instruction(schema)}"
                    if schema_in_prompt
                    else instructions
                ),
                messages=messages,
                max_output_tokens=max_output_tokens,
                temperature=0,
                # The memory JSON is the whole answer; thinking would spend
                # the allowance it needs.
                reasoning_effort="none",
                response_schema=schema if wire_schema else None,
                metadata={"operation": "context_compaction"},
            )
            if (
                estimate_messages(request.messages, request.instructions or "")
                > input_capacity
            ):
                raise ContextCapacityError(
                    "context segment cannot fit the compactor input allowance",
                    usage=usage,
                )
            self._enforce_call_budget(
                provider=provider,
                request=request,
                prior_usage=prior_usage,
                attempt_usage=usage,
                budget=budget,
            )
            try:
                response = await provider.complete(request)
            except Exception as exc:
                record_caught_exception(
                    "knowledge",
                    "knowledge.context.caught_failure_004",
                    "A handled knowledge operation raised an exception.",
                    exc,
                    stage="context",
                )
                if wire_schema and self._response_format_rejected(exc):
                    # DeepSeek and Z.ai refuse a response_format they do not
                    # serve, and a gateway in front of them may too. Ask
                    # once more with the schema in the instructions, rather
                    # than fail this and every later turn of the owner.
                    wire_schema = False
                    schema_in_prompt = True
                    continue
                raise ContextCompactionError(
                    "context compactor provider request failed", usage=usage
                ) from exc
            attempt += 1
            call_usage = ChatTokenUsage.model_validate(response.usage.model_dump())
            usage = self._add_usage(usage, call_usage)
            if response.tool_calls:
                last_error = "compactor returned an unauthorized tool call"
                previous_output = response.text
                continue
            previous_output = response.text.strip()
            try:
                memory = ContextMemory.model_validate_json(
                    self._json_object(previous_output)
                )
                self._validate_memory_sources(memory, allowed)
                return memory, usage
            except Exception as exc:
                record_caught_exception(
                    "knowledge",
                    "knowledge.context.caught_failure_005",
                    "A handled knowledge operation raised an exception.",
                    exc,
                    stage="context",
                )
                last_error = str(exc)[:500]
        raise ContextCompactionError(
            "compactor did not return valid sourced memory", usage=usage
        )

    @staticmethod
    def _response_format_rejected(exc: Exception) -> bool:
        """Whether a provider refused the request's response_format itself."""

        return isinstance(exc, ProviderError) and bool(
            _RESPONSE_FORMAT_REJECTION.search(str(exc))
        )

    @staticmethod
    def _json_object(value: str) -> str:
        stripped = value.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
            stripped = re.sub(r"\s*```$", "", stripped)
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise ValueError("response did not contain a JSON object")
        return stripped[start : end + 1]

    @staticmethod
    def _validate_memory_sources(
        memory: ContextMemory, allowed: set[tuple[str, str, int | None]]
    ) -> None:
        for collection in (
            memory.confirmed_facts,
            memory.decisions,
            memory.constraints,
            memory.corrections,
            memory.open_questions,
        ):
            for item in collection:
                if not item.sources or any(
                    ContextCompactor._reference_key(source) not in allowed
                    for source in item.sources
                ):
                    raise ValueError("memory item cites an unknown canonical source")

    def _validate_canonical_sources(
        self,
        owner_type: ContextOwnerType,
        owner_id: str,
        sources: list[ContextSource],
    ) -> None:
        if owner_type == ContextOwnerType.CHAT_SESSION:
            session = self.store.get(ChatSession, owner_id)
            messages: dict[str, ChatMessage] = {}
            offset = 0
            while True:
                page = self.store.list_entities(
                    ChatMessage,
                    engagement_id=session.engagement_id,
                    offset=offset,
                    limit=1_000,
                )
                messages.update(
                    (message.id, message)
                    for message in page
                    if message.session_id == owner_id
                )
                if len(page) < 1_000:
                    break
                offset += len(page)
            for source in sources:
                if source.reference.source_kind != "chat_message":
                    raise ValueError("chat memory sources must reference chat messages")
                message = messages.get(source.reference.source_id)
                if message is None or source.reference.sequence != message.sequence:
                    raise ValueError(
                        "chat memory source does not exist in this session"
                    )
            return

        event_sources: dict[str, RunEvent] = {}
        cursor = 0
        while True:
            events = self.store.replay_events(
                owner_id, after_sequence=cursor, limit=10_000
            )
            event_sources.update((event.id, event) for event in events)
            if len(events) < 10_000:
                break
            cursor = events[-1].sequence
        for source in sources:
            reference = source.reference
            if reference.source_kind in {"task", "task_result"}:
                task = self.store.get(Task, reference.source_id)
                if task.run_id != owner_id:
                    raise ValueError(
                        "mission memory task source belongs to another run"
                    )
            elif reference.source_kind == "agent_attempt":
                attempt = self.store.get(AgentAttempt, reference.source_id)
                if attempt.run_id != owner_id:
                    raise ValueError("mission memory attempt belongs to another run")
            elif reference.source_kind == "run_event":
                event = event_sources.get(reference.source_id)
                if event is None or (
                    reference.sequence is not None
                    and reference.sequence != event.sequence
                ):
                    raise ValueError("mission memory event source does not exist")
            else:
                raise ValueError("unsupported mission memory source kind")

    @staticmethod
    def _reference_key(
        reference: ContextSourceReference,
    ) -> tuple[str, str, int | None]:
        return (reference.source_kind, reference.source_id, reference.sequence)

    @classmethod
    def _memory_references(cls, memory: ContextMemory) -> list[ContextSourceReference]:
        found: list[ContextSourceReference] = []
        seen: set[tuple[str, str, int | None]] = set()
        for collection in (
            memory.confirmed_facts,
            memory.decisions,
            memory.constraints,
            memory.corrections,
            memory.open_questions,
        ):
            for item in collection:
                for reference in item.sources:
                    key = cls._reference_key(reference)
                    if key not in seen:
                        found.append(reference)
                        seen.add(key)
        return found

    @staticmethod
    def _canonical_references(
        source: ContextSource,
    ) -> tuple[ContextSourceReference, ...]:
        if source.reference.source_kind == "context_segment":
            return source.provenance
        return source.provenance or (source.reference,)

    def _persist(self, snapshot: ContextSnapshot) -> None:
        owner_model: type[ChatSession] | type[AgentRun]
        owner_model = (
            ChatSession
            if snapshot.owner_type == ContextOwnerType.CHAT_SESSION
            else AgentRun
        )
        for _ in range(3):
            owner = self.store.get(owner_model, snapshot.owner_id)
            metadata = dict(owner.metadata)
            metadata["context_compaction"] = {
                "snapshot_id": snapshot.id,
                "status": snapshot.status.value,
                "version": snapshot.version,
                "compacted_through": snapshot.compacted_through,
                "updated_at": utc_now().isoformat(),
            }
            try:
                with self.store.transaction() as transaction:
                    transaction.add(snapshot)
                    transaction.update(
                        owner_model,
                        owner.id,
                        {"metadata": metadata},
                        expected_revision=owner.revision,
                    )
                return
            except ConflictError as caught_error:
                record_caught_exception(
                    "knowledge",
                    "knowledge.context.caught_failure_006",
                    "A handled knowledge operation raised an exception.",
                    caught_error,
                    stage="context",
                )
                try:
                    existing = self.store.get(ContextSnapshot, snapshot.id)
                except NotFoundError as caught_error:
                    record_caught_exception(
                        "knowledge",
                        "knowledge.context.caught_failure_007",
                        "A handled knowledge operation raised an exception.",
                        caught_error,
                        stage="context",
                    )
                    continue
                if existing.source_sha256 != snapshot.source_sha256:
                    raise
                return
        raise ConflictError(
            f"owner {snapshot.owner_id} changed during context snapshot persistence"
        )

    @staticmethod
    def _add_usage(left: ChatTokenUsage, right: ChatTokenUsage) -> ChatTokenUsage:
        return ChatTokenUsage(
            input_tokens=left.input_tokens + right.input_tokens,
            output_tokens=left.output_tokens + right.output_tokens,
            total_tokens=left.total_tokens + right.total_tokens,
        )

    @staticmethod
    def _cost(provider: ModelProvider, usage: ChatTokenUsage) -> float:
        input_rate = float(provider.config.options.get("input_cost_per_million", 0))
        output_rate = float(provider.config.options.get("output_cost_per_million", 0))
        return (
            usage.input_tokens * input_rate + usage.output_tokens * output_rate
        ) / 1_000_000

    @classmethod
    def _enforce_call_budget(
        cls,
        *,
        provider: ModelProvider,
        request: ModelRequest,
        prior_usage: ChatTokenUsage,
        attempt_usage: ChatTokenUsage,
        budget: ContextCallBudget | None,
    ) -> None:
        if budget is None:
            return
        consumed = cls._add_usage(prior_usage, attempt_usage)
        estimated_input = estimate_messages(
            request.messages, request.instructions or ""
        )
        estimated_output = request.max_output_tokens or 0
        if budget.max_tokens is not None and (
            consumed.input_tokens
            + consumed.output_tokens
            + estimated_input
            + estimated_output
            > budget.max_tokens
        ):
            raise ContextCompactionError(
                "insufficient mission token budget for context compaction",
                usage=attempt_usage,
            )
        projected = ChatTokenUsage(
            input_tokens=consumed.input_tokens + estimated_input,
            output_tokens=consumed.output_tokens + estimated_output,
            total_tokens=(
                consumed.input_tokens
                + consumed.output_tokens
                + estimated_input
                + estimated_output
            ),
        )
        if (
            budget.max_cost_usd is not None
            and cls._cost(provider, projected) > budget.max_cost_usd
        ):
            raise ContextCompactionError(
                "insufficient mission cost budget for context compaction",
                usage=attempt_usage,
            )

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, ContextCompactionError):
            return str(exc)[:1_000]
        return "context compaction failed; retry with the configured provider"


__all__ = [
    "COMPACTOR_INPUT_FRACTION",
    "CONTEXT_TARGET_FRACTION",
    "ContextCapacityError",
    "ContextCallBudget",
    "ContextCompactionError",
    "ContextCompactor",
    "ContextLimits",
    "ContextSource",
    "ContextStatus",
    "CompactionResult",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "default_output_tokens",
    "estimate_messages",
    "estimate_model_request",
    "estimate_tokens",
    "known_model_limits",
    "lexical_score",
    "memory_text",
    "resolve_context_limits",
]
