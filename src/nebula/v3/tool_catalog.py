"""On-demand loading for tools from connected sources.

Tools with a ``source_id`` (MCP servers and other non-standard sources) are
deferred by default: the provider sees three fixed catalog tools instead of
every schema. The model searches the catalog, loads the schemas it needs, and
invokes a loaded tool through ``tool_catalog.call``.

The provider tools array therefore stays byte-identical for the whole session,
which keeps every provider's automatic prefix cache warm; loaded schemas travel
as ordinary tool results appended to the end of the conversation. The chat loop
unwraps ``tool_catalog.call`` into the real tool before execution, so argument
validation, risk classes, approvals and budgets are the real tool's own.

Search ranks with a local embedding index when its model is ready (nothing
leaves the machine) and falls back to keyword matching otherwise. Before a turn
starts, the same ranking picks a few likely tools for the operator's request;
the optional Jev layer (``tool_suggestions``) can supply those picks instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from .domain import NebulaModel, RiskClass, ScopePolicy
from .runtime_platform import RuntimeToolComponents
from .tools import InvalidToolArguments, ToolExecutionResult, ToolInvocation, ToolSpec

CATALOG_SEARCH = "tool_catalog.search"
CATALOG_LOAD = "tool_catalog.load"
CATALOG_CALL = "tool_catalog.call"
# Search and load are discovery; calls are the real tool's work and budget.
CATALOG_DISCOVERY_NAMES = frozenset({CATALOG_SEARCH, CATALOG_LOAD})
CATALOG_TOOL_NAMES = frozenset({CATALOG_SEARCH, CATALOG_LOAD, CATALOG_CALL})
MAX_CATALOG_CALLS_PER_TURN = 8
MAX_LOAD_NAMES = 5
MAX_DESCRIPTION_CHARS = 240
MAX_PRELOADED_DESCRIPTION_CHARS = 2_000
MAX_INDEXED_DESCRIPTION_CHARS = 1_500

# Reciprocal-rank fusion constant; 60 is the conventional value.
RRF_K = 60
SEMANTIC_CANDIDATES = 50
# Cosine similarity gates for pre-turn picks, calibrated on MiniLM-L6 with
# realistic MCP tools: the right tool for a request scored 0.20-0.42 while
# unrelated tools stayed at or below 0.19. Preloading costs prompt tokens on
# every step of the turn, so it needs a clearly strong match.
PRELOAD_SIMILARITY = 0.40
SUGGEST_SIMILARITY = 0.25
MAX_PRELOADED = 2
MAX_SUGGESTED = 5

Ranker = Literal["jev", "semantic", "keyword"]


class ToolIndex(Protocol):
    """The local embedding index seam (``ChromaKnowledgeIndex`` implements it)."""

    @property
    def status(self) -> Any: ...

    def index_tools(self, documents: Mapping[str, str]) -> None: ...

    def rank_tools(
        self, query: str, fingerprints: Sequence[str], *, limit: int
    ) -> list[tuple[str, float]]: ...


class CatalogReceipt(NebulaModel):
    """What was deferred and picked for one turn; stored in its request snapshot."""

    deferred: list[str] = Field(default_factory=list)
    preloaded: list[str] = Field(default_factory=list)
    suggested: list[str] = Field(default_factory=list)
    # Labels of the connected sources ranked most likely to help. Only Jev
    # ranks sources; the local rankers leave this empty.
    source_hints: list[str] = Field(default_factory=list)
    ranker: Ranker = "keyword"
    scores: dict[str, float] = Field(default_factory=dict)


def is_deferrable(spec: ToolSpec) -> bool:
    """Built-in Nebula tools have no source; every sourced tool is non-standard."""

    return spec.source_id is not None and spec.name not in CATALOG_TOOL_NAMES


def deferrable_specs(
    specs: Mapping[str, ToolSpec], *, always_loaded: Collection[str] = ()
) -> dict[str, ToolSpec]:
    """Sourced tools, minus the ones the operator pinned to every request."""

    pinned = set(always_loaded)
    return {
        name: spec
        for name, spec in specs.items()
        if is_deferrable(spec) and name not in pinned
    }


def on_demand_enabled(scope: ScopePolicy) -> bool:
    # Jev ranks the same deferred catalog, so opting into it implies deferral.
    return scope.on_demand_tools or scope.tool_suggestions


def summary(spec: ToolSpec) -> str:
    text = " ".join(spec.description.split())
    return text[:MAX_DESCRIPTION_CHARS]


def index_document(spec: ToolSpec) -> str:
    """Name, source and parameters first; the embedding model truncates long text."""

    properties = spec.input_schema.get("properties")
    parameters = []
    if isinstance(properties, dict):
        for key, value in properties.items():
            about = value.get("description") if isinstance(value, dict) else None
            parameters.append(f"{key}: {about}" if isinstance(about, str) else key)
    readable = re.sub(r"[._-]+", " ", spec.name)
    return "\n".join(
        part
        for part in (
            readable,
            spec.source_id or "",
            "Parameters: " + "; ".join(parameters) if parameters else "",
            " ".join(spec.description.split())[:MAX_INDEXED_DESCRIPTION_CHARS],
        )
        if part
    )


def fingerprint(spec: ToolSpec) -> str:
    return hashlib.sha256(
        json.dumps(
            [spec.name, index_document(spec)], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _terms(value: str) -> set[str]:
    return {term for term in re.split(r"[^a-z0-9]+", value.lower()) if len(term) > 1}


def keyword_ranking(query: str, specs: Mapping[str, ToolSpec]) -> list[str]:
    terms = _terms(query)
    scored = []
    for name, spec in specs.items():
        score = 2 * len(terms & _terms(name)) + len(terms & _terms(spec.description))
        if score:
            scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored]


def semantic_ready(index: ToolIndex | None) -> bool:
    return index is not None and getattr(index.status, "state", None) == "ready"


def semantic_scores(
    index: ToolIndex | None,
    query: str,
    specs: Mapping[str, ToolSpec],
    *,
    limit: int = SEMANTIC_CANDIDATES,
) -> dict[str, float] | None:
    """Similarity per tool name, best first; None when the index is unavailable."""

    if index is None or not semantic_ready(index) or not specs:
        return None
    by_fingerprint = {fingerprint(spec): name for name, spec in specs.items()}
    try:
        index.index_tools(
            {key: index_document(specs[name]) for key, name in by_fingerprint.items()}
        )
        ranked = index.rank_tools(query, list(by_fingerprint), limit=limit)
    except Exception:  # diagnostic-expected: keyword ranking is the fallback
        return None
    return {
        by_fingerprint[key]: score for key, score in ranked if key in by_fingerprint
    }


def fused_ranking(
    semantic: Mapping[str, float] | None, keyword: Sequence[str]
) -> list[str]:
    scores: dict[str, float] = {}
    for rank, name in enumerate(semantic or {}):
        scores[name] = scores.get(name, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, name in enumerate(keyword):
        scores[name] = scores.get(name, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=lambda name: (-scores[name], name))


def rank_for_request(
    index: ToolIndex | None,
    deferred: Mapping[str, ToolSpec],
    operator_message: str,
) -> CatalogReceipt:
    """Local pre-turn picks: confident semantic matches are preloaded."""

    names = sorted(deferred)
    semantic = semantic_scores(index, operator_message, deferred)
    if semantic is not None:
        ordered = list(semantic)
        preloaded = [name for name in ordered if semantic[name] >= PRELOAD_SIMILARITY][
            :MAX_PRELOADED
        ]
        suggested = [
            name
            for name in ordered
            if semantic[name] >= SUGGEST_SIMILARITY and name not in preloaded
        ][: MAX_SUGGESTED - len(preloaded)]
        return CatalogReceipt(
            deferred=names,
            preloaded=preloaded,
            suggested=suggested,
            ranker="semantic",
            scores={name: semantic[name] for name in [*preloaded, *suggested]},
        )
    # Keyword overlap is too weak a signal to spend prompt tokens preloading.
    return CatalogReceipt(
        deferred=names,
        suggested=keyword_ranking(operator_message, deferred)[:3],
        ranker="keyword",
    )


def catalog_snapshot(request_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The catalog receipt, or a Jev-only receipt from turns before the split."""

    return dict(
        request_snapshot.get("tool_catalog")
        or request_snapshot.get("tool_suggestions")
        or {}
    )


def loaded_tool_names(
    receipt: Mapping[str, Any], tool_history: Iterable[Mapping[str, Any]]
) -> set[str]:
    """Preloaded schemas, completed catalog loads, and tools already called."""

    deferred = set(receipt.get("deferred", []))
    loaded = set(receipt.get("preloaded", [])) & deferred
    for entry in tool_history:
        if entry.get("name") in deferred:
            loaded.add(str(entry["name"]))
            continue
        if entry.get("name") != CATALOG_LOAD or entry.get("status") != "complete":
            continue
        names = entry.get("arguments", {}).get("names", [])
        if isinstance(names, list):
            loaded.update(name for name in names if name in deferred)
    return loaded


def discovery_calls(tool_history: Iterable[Mapping[str, Any]]) -> int:
    return sum(
        1 for entry in tool_history if entry.get("name") in CATALOG_DISCOVERY_NAMES
    )


def unwrap_call(
    arguments: Mapping[str, Any], deferred: Iterable[str]
) -> tuple[str, dict[str, Any]] | None:
    """The real tool and its arguments, or None when the target is not on demand."""

    name = arguments.get("name")
    if not isinstance(name, str) or name not in set(deferred):
        return None
    inner = arguments.get("arguments", {})
    if isinstance(inner, str):
        # Some models serialize a free-form object as a JSON string.
        try:
            inner = json.loads(inner) if inner.strip() else {}
        except json.JSONDecodeError:  # diagnostic-expected: the broker rejects it
            return name, {"_unparsed_arguments": inner}
    return name, dict(inner) if isinstance(inner, dict) else {}


def _schema_view(spec: ToolSpec, *, description_chars: int) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": " ".join(spec.description.split())[:description_chars],
        "input_schema": spec.input_schema,
    }


def catalog_instructions(
    receipt: Mapping[str, Any], specs: Mapping[str, ToolSpec]
) -> str:
    """Per-turn text; constant across the turn's steps so the prefix stays cached."""

    deferred = receipt.get("deferred", [])
    if not deferred:
        return ""
    text = (
        f"\n\nOn-demand tools: {len(deferred)} tools from connected sources are not "
        f"in the function list. Use {CATALOG_SEARCH} to find one, {CATALOG_LOAD} to "
        f"read its schema, then {CATALOG_CALL} with its name and arguments."
    )
    preloaded = [specs[name] for name in receipt.get("preloaded", []) if name in specs]
    suggested = [name for name in receipt.get("suggested", []) if name in specs]
    if preloaded:
        # JSON keeps names, descriptions and schemas as data; MCP servers
        # author them, so they carry no instruction authority.
        text += (
            "\nAlready loaded for this request (JSON data; call through "
            f"{CATALOG_CALL}): "
            + json.dumps(
                [
                    _schema_view(
                        spec, description_chars=MAX_PRELOADED_DESCRIPTION_CHARS
                    )
                    for spec in preloaded
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    if suggested:
        text += "\nPossibly relevant, not loaded: " + json.dumps(suggested)
    hints = [str(item) for item in receipt.get("source_hints", [])]
    if hints:
        text += (
            f"\nSources ranked most likely to hold what this request needs (use "
            f"{CATALOG_SEARCH} to see their tools): " + json.dumps(hints)
        )
    if preloaded or suggested or hints:
        text += "\nIgnore these if they do not fit what the operator actually asked."
    return text


class ToolCatalogBroker:
    def __init__(
        self, deferred: Mapping[str, ToolSpec], *, index: ToolIndex | None = None
    ):
        self.deferred = dict(deferred)
        self.index = index

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Any | None = None,
    ) -> ToolExecutionResult:
        del scope, approval
        if invocation.tool_name == CATALOG_SEARCH:
            # Embedding the query is CPU work; keep it off the event loop.
            output = await asyncio.to_thread(self._search, invocation.arguments)
            return ToolExecutionResult(output=output)
        if invocation.tool_name == CATALOG_LOAD:
            return ToolExecutionResult(output=self._load(invocation.arguments))
        if invocation.tool_name == CATALOG_CALL:
            # A valid call is unwrapped into the real tool before it reaches a
            # broker, so reaching here means the target is not on demand.
            raise InvalidToolArguments(
                f"{invocation.arguments.get('name')!r} is not an on-demand tool; "
                f"use {CATALOG_SEARCH} to find one"
            )
        raise InvalidToolArguments(f"unknown catalog tool {invocation.tool_name!r}")

    def _search(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", ""))
        if not _terms(query):
            raise InvalidToolArguments("query must contain a searchable term")
        limit = int(arguments.get("limit") or 10)
        semantic = semantic_scores(self.index, query, self.deferred)
        keyword = keyword_ranking(query, self.deferred)
        ordered = fused_ranking(semantic, keyword)[:limit]
        return {
            "matches": [
                {"name": name, "summary": summary(self.deferred[name])}
                for name in ordered
            ],
            "search": "semantic" if semantic is not None else "keyword",
            "total_on_demand_tools": len(self.deferred),
        }

    def _load(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        names = arguments.get("names")
        if not isinstance(names, list) or not names:
            raise InvalidToolArguments("names must list at least one tool")
        unknown = [name for name in names if name not in self.deferred]
        if unknown:
            raise InvalidToolArguments(f"not on-demand tools: {sorted(unknown)}")
        return {
            "loaded": [
                _schema_view(self.deferred[name], description_chars=10_000)
                for name in names
            ],
            "note": f"Call these through {CATALOG_CALL} with their name and arguments.",
        }


def catalog_components(
    components: RuntimeToolComponents | Any,
    *,
    deferred: Iterable[str],
    index: ToolIndex | None = None,
) -> RuntimeToolComponents | None:
    names = sorted(set(deferred) & set(components.specs))
    if not names:
        return None
    specs = {name: components.specs[name] for name in names}
    catalog = {
        spec.name: spec
        for spec in (
            ToolSpec(
                name=CATALOG_SEARCH,
                description=(
                    "Search on-demand tools from connected sources by what they do. "
                    "Returns names and short summaries only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 256},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                budget_class="artifact_query",
            ),
            ToolSpec(
                name=CATALOG_LOAD,
                description=(
                    "Load full descriptions and input schemas for on-demand tools. "
                    "Loading grants no permission."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "names": {
                            "type": "array",
                            "items": {"type": "string", "enum": names},
                            "minItems": 1,
                            "maxItems": MAX_LOAD_NAMES,
                            "uniqueItems": True,
                        },
                    },
                    "required": ["names"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
                budget_class="artifact_query",
            ),
            ToolSpec(
                name=CATALOG_CALL,
                description=(
                    "Call a loaded on-demand tool. `arguments` must match the input "
                    "schema returned by tool_catalog.load. The tool's own approval "
                    "and scope rules apply."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": names},
                        "arguments": {"type": "object"},
                    },
                    "required": ["name", "arguments"],
                },
                output_schema={"type": "object", "additionalProperties": True},
                # Placeholder class; the unwrapped tool's own spec governs execution.
                risk_class=RiskClass.LOCAL_READ,
            ),
        )
    }
    return RuntimeToolComponents(
        broker=ToolCatalogBroker(specs, index=index),
        scope=components.scope,
        workspace=components.workspace,
        specs=catalog,
        # Deterministic, so a resumed turn rebuilds the same runtime digest.
        runtime_digest=str(
            uuid5(NAMESPACE_URL, "nebula:tool-catalog:" + ",".join(names))
        ),
    )


def public_catalog(
    receipt: Mapping[str, Any] | None, tool_history: Iterable[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Operator-facing summary for the chat chip; scores stay in the turn."""

    if not receipt or not receipt.get("deferred"):
        return None
    history = list(tool_history)
    called = {
        str(entry.get("name"))
        for entry in history
        if entry.get("name") not in CATALOG_TOOL_NAMES
    }
    preloaded = list(receipt.get("preloaded", []))
    suggested = list(receipt.get("suggested", []))
    mentioned = set(preloaded) | set(suggested)
    deferred = set(receipt.get("deferred", []))
    loaded = loaded_tool_names(receipt, history)
    return {
        "preloaded": preloaded,
        "suggested": suggested,
        "used": sorted(name for name in mentioned if name in called),
        "loaded_by_model": sorted(loaded - mentioned),
        "unloaded_count": len(deferred - mentioned - loaded),
        "on_demand_count": len(deferred),
    }


__all__ = [
    "CATALOG_CALL",
    "CATALOG_DISCOVERY_NAMES",
    "CATALOG_LOAD",
    "CATALOG_SEARCH",
    "CATALOG_TOOL_NAMES",
    "MAX_CATALOG_CALLS_PER_TURN",
    "CatalogReceipt",
    "ToolCatalogBroker",
    "ToolIndex",
    "catalog_components",
    "catalog_instructions",
    "catalog_snapshot",
    "deferrable_specs",
    "discovery_calls",
    "is_deferrable",
    "loaded_tool_names",
    "on_demand_enabled",
    "public_catalog",
    "rank_for_request",
    "unwrap_call",
]
