"""Optional next-turn tool suggestions from Jev, TypeSafe's System One model.

Deferral and the catalog tools live in ``tool_catalog``. When an engagement
opts in, Jev ranks the deferred catalog against the operator's recent messages
before the turn starts, in place of the local ranking. Its answer is a hint in
the per-turn instructions and can preload a few schemas; it never hides a tool,
grants a permission, or blocks the turn.

Jev receives only redacted operator messages and tool names/descriptions, never
tool output, which may be controlled by an assessed target.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import Field

from .domain import (
    NebulaModel,
    ScopePolicy,
    ToolSuggestionSettings,
    ToolSuggestionTest,
    utc_now,
)
from .redaction import redact_text
from .tool_catalog import CATALOG_TOOL_NAMES, loaded_tool_names, summary
from .tools import ToolSpec

SETTINGS_ID = "typesafe"
ENV_KEY = "TYPESAFE_API_KEY"
JEV_MODEL = "jev-latest"
DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_TIMEOUT_SECONDS = 4.0
# Choice accepts 255 options; one is reserved for "none".
MAX_CHOICE_TOOLS = 254
# Keeps state plus the longest question well inside Jev's 32k-token window.
MAX_CHOICE_CRITERIA_CHARS = 60_000
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


def suggestions_enabled(scope: ScopePolicy) -> bool:
    # local_only forbids any remote model, including Jev.
    return scope.tool_suggestions and not scope.local_only


def _chunks(specs: Sequence[ToolSpec]) -> list[list[ToolSpec]]:
    chunks: list[list[ToolSpec]] = []
    current: list[ToolSpec] = []
    size = 0
    for spec in specs:
        cost = len(spec.name) + len(summary(spec))
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
        criteria = {spec.name: summary(spec) or spec.name for spec in chunk}
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
    raw_usage = body.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
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


def load_settings(store: Any) -> ToolSuggestionSettings | None:
    from .storage import NotFoundError

    try:
        return store.get(ToolSuggestionSettings, SETTINGS_ID)
    except NotFoundError:  # diagnostic-expected: no key has been saved yet
        return None


KeySource = Literal["vault", "session", "environment"]


def key_source(store: Any, credentials: Any) -> tuple[KeySource | None, bool]:
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
