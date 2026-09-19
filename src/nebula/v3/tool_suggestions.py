"""Optional next-turn tool suggestions from Jev, TypeSafe's System One model.

When an engagement opts in, tools from non-standard sources (any spec with a
``source_id``, such as MCP servers) are deferred: the provider sees only their
names through two catalog tools and loads a full schema on demand. Before the
turn starts, Jev ranks the deferred catalog against the operator's recent
messages. Its answer is a hint in the per-turn instructions and can preload a
few schemas; it never hides a tool, grants a permission, or blocks the turn.

Jev receives only redacted operator messages and tool names/descriptions, never
tool output, which may be controlled by an assessed target.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import Field

from .domain import (
    NebulaModel,
    RiskClass,
    ScopePolicy,
    ToolSuggestionSettings,
    ToolSuggestionTest,
    utc_now,
)
from .redaction import redact_text
from .runtime_platform import RuntimeToolComponents
from .tools import InvalidToolArguments, ToolExecutionResult, ToolInvocation, ToolSpec

CATALOG_SEARCH = "tool_catalog.search"
CATALOG_LOAD = "tool_catalog.load"
CATALOG_TOOL_NAMES = frozenset({CATALOG_SEARCH, CATALOG_LOAD})
MAX_CATALOG_CALLS_PER_TURN = 8
MAX_LOAD_NAMES = 5

SETTINGS_ID = "typesafe"
ENV_KEY = "TYPESAFE_API_KEY"
JEV_MODEL = "jev-latest"
DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_TIMEOUT_SECONDS = 4.0
# Choice accepts 255 options; one is reserved for "none".
MAX_CHOICE_TOOLS = 254
# Keeps state plus the longest question well inside Jev's 32k-token window.
MAX_CHOICE_CRITERIA_CHARS = 60_000
MAX_DESCRIPTION_CHARS = 240
MAX_OPERATOR_MESSAGE_CHARS = 4_000
MAX_PRIOR_OPERATOR_MESSAGES = 3

# A mean gate below this means the request needs no extra tool at all.
GATE_THRESHOLD = 0.30
# Schemas are preloaded only for confident picks on a request that needs a tool.
PRELOAD_THRESHOLD = 0.50
MAX_PRELOADED = 2
SUGGEST_THRESHOLD = 0.15
MAX_SUGGESTED = 5

NONE_OPTION = "none_of_these"

_GATE_QUESTIONS: dict[str, dict[str, Any]] = {
    "gate_action": {
        "type": "noul",
        "instructions": (
            "Does fulfilling operator_request require taking an action in, or "
            "fetching data from, an external system or service?"
        ),
        "criteria": {
            "true": "An external action or lookup is required.",
            "false": "It can be answered from conversation or general knowledge alone.",
        },
    },
    "gate_prose": {
        "type": "noul",
        "instructions": (
            "Can operator_request be fully satisfied with a written answer and no "
            "tool use at all?"
        ),
        "criteria": {
            "true": "A written answer alone is enough.",
            "false": "Some tool or system interaction is needed.",
        },
    },
}


class ToolSuggestionError(RuntimeError):
    pass


class ToolSuggestionReceipt(NebulaModel):
    """What Jev decided for one turn; stored in the turn's request snapshot."""

    status: Literal["suggested", "no_tool_needed", "unavailable"]
    deferred: list[str] = Field(default_factory=list)
    preloaded: list[str] = Field(default_factory=list)
    suggested: list[str] = Field(default_factory=list)
    probabilities: dict[str, float] = Field(default_factory=dict)
    gate: float | None = None
    model: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    error: str | None = Field(default=None, max_length=500)


def is_deferrable(spec: ToolSpec) -> bool:
    """Built-in Nebula tools have no source; every sourced tool is non-standard."""

    return spec.source_id is not None and spec.name not in CATALOG_TOOL_NAMES


def deferrable_specs(specs: Mapping[str, ToolSpec]) -> dict[str, ToolSpec]:
    return {name: spec for name, spec in specs.items() if is_deferrable(spec)}


def suggestions_enabled(scope: ScopePolicy) -> bool:
    # local_only forbids any remote model, including Jev.
    return scope.tool_suggestions and not scope.local_only


def _summary(spec: ToolSpec) -> str:
    text = " ".join(spec.description.split())
    return text[:MAX_DESCRIPTION_CHARS]


def _chunks(specs: Sequence[ToolSpec]) -> list[list[ToolSpec]]:
    chunks: list[list[ToolSpec]] = []
    current: list[ToolSpec] = []
    size = 0
    for spec in specs:
        cost = len(spec.name) + len(_summary(spec))
        if current and (
            len(current) >= MAX_CHOICE_TOOLS or size + cost > MAX_CHOICE_CRITERIA_CHARS
        ):
            chunks.append(current)
            current, size = [], 0
        current.append(spec)
        size += cost
    if current:
        chunks.append(current)
    return chunks


def build_state(operator_messages: Sequence[str]) -> dict[str, Any]:
    """Only operator-authored text reaches Jev, redacted and bounded."""

    cleaned = [
        redact_text(item)[:MAX_OPERATOR_MESSAGE_CHARS]
        for item in operator_messages
        if item.strip()
    ]
    if not cleaned:
        raise ToolSuggestionError("no operator message to evaluate")
    return {
        "operator_request": cleaned[-1],
        "earlier_operator_messages": cleaned[-1 - MAX_PRIOR_OPERATOR_MESSAGES : -1],
    }


def build_questions(specs: Sequence[ToolSpec]) -> dict[str, dict[str, Any]]:
    questions = dict(_GATE_QUESTIONS)
    for index, chunk in enumerate(_chunks(sorted(specs, key=lambda item: item.name))):
        criteria = {spec.name: _summary(spec) or spec.name for spec in chunk}
        criteria[NONE_OPTION] = (
            "None of the listed tools is needed for operator_request."
        )
        questions[f"tools_{index}"] = {
            "type": "choice",
            "instructions": (
                "Which listed tool is most likely needed to act on operator_request "
                "next? Choose none_of_these unless a listed tool clearly applies."
            ),
            "criteria": criteria,
        }
    return questions


def interpret_answers(
    answers: Mapping[str, Any], deferred: Sequence[str]
) -> tuple[float, dict[str, float]]:
    """Return the mean gate probability and each deferred tool's probability."""

    action = answers.get("gate_action", {}).get("noul")
    prose = answers.get("gate_prose", {}).get("noul")
    if not isinstance(action, (int, float)) or not isinstance(prose, (int, float)):
        raise ToolSuggestionError("Jev response is missing gate answers")
    gate = (float(action) + (1.0 - float(prose))) / 2
    known = set(deferred)
    probabilities: dict[str, float] = {}
    for key, answer in answers.items():
        if not key.startswith("tools_") or not isinstance(answer, Mapping):
            continue
        for name, value in dict(answer.get("probabilities") or {}).items():
            if name in known and isinstance(value, (int, float)):
                probabilities[name] = round(float(value), 4)
    return round(gate, 4), probabilities


def rank(
    gate: float, probabilities: Mapping[str, float]
) -> tuple[list[str], list[str]]:
    """Split confident picks (schemas preloaded) from softer hints."""

    if gate < GATE_THRESHOLD:
        return [], []
    ordered = sorted(probabilities.items(), key=lambda item: (-item[1], item[0]))
    preloaded = [name for name, value in ordered if value >= PRELOAD_THRESHOLD][
        :MAX_PRELOADED
    ]
    suggested = [
        name
        for name, value in ordered
        if value >= SUGGEST_THRESHOLD and name not in preloaded
    ][: MAX_SUGGESTED - len(preloaded)]
    return preloaded, suggested


@dataclass(frozen=True)
class JevClient:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    transport: httpx.AsyncBaseTransport | None = None

    @classmethod
    def from_key(cls, api_key: str) -> JevClient:
        return cls(
            api_key=api_key,
            base_url=os.environ.get("TYPESAFE_BASE_URL", "").strip()
            or DEFAULT_BASE_URL,
        )

    @classmethod
    def from_environment(cls) -> JevClient | None:
        api_key = os.environ.get(ENV_KEY, "").strip()
        return cls.from_key(api_key) if api_key else None

    async def system_one(
        self, state: Mapping[str, Any], questions: Mapping[str, Any]
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/v1/systemone",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": JEV_MODEL, "state": state, "questions": questions},
            )
        if response.status_code != 200:
            raise ToolSuggestionError(f"Jev returned HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
            raise ToolSuggestionError("Jev response has no answers")
        return body


async def suggest_tools(
    client: JevClient | None,
    *,
    deferred: Mapping[str, ToolSpec],
    operator_messages: Sequence[str],
) -> ToolSuggestionReceipt:
    """Ask Jev once; any failure yields an `unavailable` receipt, never an error."""

    names = sorted(deferred)
    if client is None:
        return ToolSuggestionReceipt(
            status="unavailable",
            deferred=names,
            error="No TypeSafe key is configured",
        )
    started = time.monotonic()
    try:
        body = await client.system_one(
            build_state(operator_messages),
            build_questions(list(deferred.values())),
        )
        gate, probabilities = interpret_answers(body["answers"], names)
    except (ToolSuggestionError, httpx.HTTPError, ValueError, TypeError) as exc:
        # diagnostic-expected: suggestions are optional; the turn proceeds without them
        return ToolSuggestionReceipt(
            status="unavailable",
            deferred=names,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
    preloaded, suggested = rank(gate, probabilities)
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return ToolSuggestionReceipt(
        status="suggested" if preloaded or suggested else "no_tool_needed",
        deferred=names,
        preloaded=preloaded,
        suggested=suggested,
        probabilities=probabilities,
        gate=gate,
        model=str(body.get("model") or JEV_MODEL),
        latency_ms=int((time.monotonic() - started) * 1000),
        input_tokens=usage.get("input_tokens"),
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


def catalog_calls(tool_history: Iterable[Mapping[str, Any]]) -> int:
    return sum(1 for entry in tool_history if entry.get("name") in CATALOG_TOOL_NAMES)


def suggestion_instructions(receipt: Mapping[str, Any]) -> str:
    deferred = receipt.get("deferred", [])
    if not deferred:
        return ""
    text = (
        f"\n\nOn-demand tools: {len(deferred)} tools from connected sources are not "
        f"loaded. Use {CATALOG_SEARCH} to find one and {CATALOG_LOAD} to receive its "
        "full schema; a loaded tool is callable from the next step."
    )
    preloaded = receipt.get("preloaded", [])
    suggested = receipt.get("suggested", [])
    if preloaded or suggested:
        # JSON keeps tool names as data; MCP servers choose these names.
        text += (
            "\nLikely relevant to the current request: "
            + json.dumps({"loaded": preloaded, "not_loaded": suggested})
            + ". Ignore these if they do not fit what the operator actually asked."
        )
    return text


def _terms(value: str) -> set[str]:
    return {term for term in re.split(r"[^a-z0-9]+", value.lower()) if len(term) > 1}


class ToolCatalogBroker:
    def __init__(self, deferred: Mapping[str, ToolSpec]):
        self.deferred = dict(deferred)

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Any | None = None,
    ) -> ToolExecutionResult:
        del scope, approval
        if invocation.tool_name == CATALOG_SEARCH:
            return ToolExecutionResult(output=self._search(invocation.arguments))
        if invocation.tool_name == CATALOG_LOAD:
            return ToolExecutionResult(output=self._load(invocation.arguments))
        raise InvalidToolArguments(f"unknown catalog tool {invocation.tool_name!r}")

    def _search(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        query = _terms(str(arguments.get("query", "")))
        if not query:
            raise InvalidToolArguments("query must contain a searchable term")
        limit = int(arguments.get("limit") or 10)
        scored = []
        for name, spec in self.deferred.items():
            name_terms = _terms(name)
            score = 2 * len(query & name_terms) + len(query & _terms(spec.description))
            if score:
                scored.append((score, name, spec))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return {
            "matches": [
                {"name": name, "summary": _summary(spec)}
                for _, name, spec in scored[:limit]
            ],
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
                {
                    "name": name,
                    "description": self.deferred[name].description,
                    "input_schema": self.deferred[name].input_schema,
                }
                for name in names
            ],
            "note": "These tools are callable from the next step.",
        }


def catalog_components(
    components: RuntimeToolComponents | Any,
    *,
    deferred: Iterable[str],
) -> RuntimeToolComponents | None:
    names = sorted(set(deferred) & set(components.specs))
    if not names:
        return None
    specs = {name: components.specs[name] for name in names}
    search = ToolSpec(
        name=CATALOG_SEARCH,
        description=(
            "Search on-demand tools from connected sources by keyword. Returns "
            "names and short summaries only."
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
    )
    load = ToolSpec(
        name=CATALOG_LOAD,
        description=(
            "Load full descriptions and input schemas for on-demand tools so they "
            "can be called from the next step. Loading grants no permission."
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
    )
    return RuntimeToolComponents(
        broker=ToolCatalogBroker(specs),
        scope=components.scope,
        workspace=components.workspace,
        specs={search.name: search, load.name: load},
        # Deterministic, so a resumed turn rebuilds the same runtime digest.
        runtime_digest=str(
            uuid5(NAMESPACE_URL, "nebula:tool-catalog:" + ",".join(names))
        ),
    )


def load_settings(store: Any) -> ToolSuggestionSettings | None:
    from .storage import NotFoundError

    try:
        return store.get(ToolSuggestionSettings, SETTINGS_ID)
    except NotFoundError:  # diagnostic-expected: no key has been saved yet
        return None


def key_source(store: Any, credentials: Any) -> tuple[str | None, bool]:
    """Where the key comes from ("vault", "session", "environment") and if usable."""

    settings = load_settings(store)
    if settings is not None and settings.secret_ref:
        status = credentials.status(settings.secret_ref)
        return status.persistence, status.available
    if os.environ.get(ENV_KEY, "").strip():
        return "environment", True
    return None, False


def resolve_jev_client(store: Any, credentials: Any) -> JevClient | None:
    """A saved key wins over the environment; an unusable saved key yields None."""

    from .credentials import CredentialError

    settings = load_settings(store)
    if settings is not None and settings.secret_ref:
        try:
            secret = credentials.resolve(settings.secret_ref)
        except CredentialError:
            # diagnostic-expected: a lost session/vault key surfaces as "not working"
            return None
        return JevClient.from_key(secret.get_secret_value())
    return JevClient.from_environment()


async def check_connection(client: JevClient | None) -> ToolSuggestionTest:
    """Ask one fixed question; the sample never includes project data."""

    tested_at = utc_now()
    if client is None:
        return ToolSuggestionTest(
            tested_at=tested_at, ok=False, error="No TypeSafe key is configured"
        )
    started = time.monotonic()
    try:
        body = await client.system_one(
            {"operator_request": "Nebula connection test: list open issues."},
            {
                "connection_test": {
                    "type": "noul",
                    "instructions": "Does operator_request ask to list issues?",
                }
            },
        )
        answer = body["answers"].get("connection_test", {})
        if not isinstance(answer.get("noul"), (int, float)):
            raise ToolSuggestionError("Jev returned no answer to the test question")
    except (ToolSuggestionError, httpx.HTTPError, ValueError, TypeError) as exc:
        # diagnostic-expected: reported to the operator as a failed test
        return ToolSuggestionTest(
            tested_at=tested_at,
            ok=False,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
    return ToolSuggestionTest(
        tested_at=tested_at,
        ok=True,
        latency_ms=int((time.monotonic() - started) * 1000),
        model=str(body.get("model") or JEV_MODEL)[:120],
    )


def public_suggestions(
    receipt: Mapping[str, Any] | None, tool_history: Iterable[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Operator-facing summary for the chat chip; probabilities stay in the turn."""

    if not receipt or not receipt.get("deferred"):
        return None
    called = {
        str(entry.get("name"))
        for entry in tool_history
        if entry.get("name") not in CATALOG_TOOL_NAMES
    }
    preloaded = list(receipt.get("preloaded", []))
    suggested = list(receipt.get("suggested", []))
    mentioned = set(preloaded) | set(suggested)
    deferred = set(receipt.get("deferred", []))
    return {
        "status": receipt.get("status"),
        "preloaded": preloaded,
        "suggested": suggested,
        "used": sorted(name for name in mentioned if name in called),
        "loaded_by_model": sorted(loaded_tool_names(receipt, tool_history) - mentioned),
        "unloaded_count": len(
            deferred - mentioned - loaded_tool_names(receipt, tool_history)
        ),
        "on_demand_count": len(deferred),
        "model": receipt.get("model"),
        "latency_ms": receipt.get("latency_ms"),
        "error": receipt.get("error"),
    }
