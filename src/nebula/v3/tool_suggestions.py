"""Optional next-turn tool suggestions from Jev, TypeSafe's System One model.

Deferral and the catalog tools live in ``tool_catalog``. When an engagement
opts in, Jev ranks the deferred catalog against the operator's recent messages
before the turn starts, in place of the local ranking. One call ranks two
things: the connected sources (today, the selected MCP servers) and the tools
themselves. A source's rank weights the tools that come from it, and only the
top few tools survive. The answer is a hint in the per-turn instructions and
can preload a few schemas; it never hides a tool, grants a permission, or
blocks the turn.

Nothing asks Jev whether the turn needs a tool at all. Every choice question
carries a "none of these" option, so a listed tool has to look more useful than
"nothing here applies" before it is picked.

Jev receives only redacted operator messages, the expanded instructions of the
skills the operator selected for the turn, source labels and descriptions, and
tool names and descriptions. It never receives tool output, which may be
controlled by an assessed target. Source descriptions come from the MCP
handshake, so like tool descriptions they are third-party text: they steer a
ranking, never an action.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
from pydantic import Field

from .domain import (
    McpServerProfile,
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
MAX_CHOICE_OPTIONS = 254
# Keeps state and the longest question inside Jev's 32k-token window: at most
# ~28k chars of state (messages plus skills) against ~60k of criteria.
MAX_CHOICE_CRITERIA_CHARS = 60_000
MAX_OPERATOR_MESSAGE_CHARS = 4_000
MAX_PRIOR_OPERATOR_MESSAGES = 3
# A selected skill names the steps the turn will follow, so its expanded text
# ranks tools better than the operator's message alone. These caps keep the
# newest selections inside the window once a goal has collected several skills.
MAX_STATE_SKILLS = 4
MAX_SKILL_INSTRUCTION_CHARS = 6_000
MAX_STATE_SKILL_CHARS = 12_000

# Schemas are preloaded only for confident picks on a request that needs a tool.
PRELOAD_THRESHOLD = 0.50
MAX_PRELOADED = 2
SUGGEST_THRESHOLD = 0.15
# However large the catalog, a turn carries at most this many picks: two can be
# preloaded, and the rest are named as hints.
MAX_PICKS = 3
# A source's rank weights its tools without ever silencing one: the top-ranked
# server's tools keep their full probability and the bottom-ranked keep half.
# Tools whose source Jev did not rank are left at full weight.
SOURCE_PRIOR_FLOOR = 0.5
# A server has no description of its own, so its criterion is its name plus the
# instructions it returned at the MCP handshake, or its tool names instead.
MAX_SOURCE_CRITERION_CHARS = 600
MAX_SOURCE_TOOL_NAMES = 12

NONE_OPTION = "none_of_these"

# One ranking per distinct request. A conversation asks a new question every
# turn, so hits come from work that repeats a request: a retry after a failed
# turn, a regenerate, a queued message re-prepared, or a mission step that asks
# the same thing again while the catalog stands still.
MAX_CACHED_RANKINGS = 64

# Appended to every question only when skills are in state, so Jev reads them as
# part of the request rather than as background prose.
_SKILL_CLAUSE = (
    " The operator also selected the skills in operator_selected_skills; the steps"
    " they lay out are part of fulfilling operator_request."
)


class SelectedSkill(Protocol):
    """The part of a ``skill_catalog.SkillSnapshot`` that Jev may see."""

    @property
    def name(self) -> str: ...

    @property
    def instructions(self) -> str: ...


@dataclass(frozen=True)
class ToolSource:
    """A connected source of on-demand tools, as Jev is shown it."""

    id: str
    label: str
    description: str = ""


def mcp_sources(profiles: Sequence[McpServerProfile]) -> dict[str, ToolSource]:
    """Describe the selected MCP servers, keyed like ``ToolSpec.source_id``.

    An ``McpServerProfile`` carries no description field, so the instructions a
    server returned at the handshake are the only prose describing it. Servers
    that returned none fall back to their tool names in ``source_criteria``.
    """

    return {
        f"mcp:{profile.id}": ToolSource(
            id=f"mcp:{profile.id}",
            label=profile.name,
            description=" ".join((profile.capabilities.instructions or "").split()),
        )
        for profile in profiles
    }


class ToolSuggestionError(RuntimeError):
    pass


class ToolSuggestionReceipt(NebulaModel):
    """What Jev decided for one turn; stored in the turn's request snapshot."""

    status: Literal["suggested", "no_tool_needed", "unavailable"]
    deferred: list[str] = Field(default_factory=list)
    # Which selected skills had their instructions sent, so the snapshot records
    # the egress as well as the ranking.
    skills: list[str] = Field(default_factory=list, max_length=MAX_STATE_SKILLS)
    preloaded: list[str] = Field(default_factory=list)
    suggested: list[str] = Field(default_factory=list)
    # Labels of the connected sources Jev ranked highest, best first.
    sources: list[str] = Field(default_factory=list, max_length=MAX_PICKS)
    probabilities: dict[str, float] = Field(default_factory=dict)
    source_probabilities: dict[str, float] = Field(default_factory=dict)
    model: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    # True when this ranking was served from SuggestionCache rather than asked.
    cached: bool = False
    error: str | None = Field(default=None, max_length=500)


def suggestions_enabled(scope: ScopePolicy) -> bool:
    # local_only forbids any remote model, including Jev.
    return scope.tool_suggestions and not scope.local_only


def request_key(state: Mapping[str, Any], questions: Mapping[str, Any]) -> str:
    """Identify a ranking request by the exact payload that would be sent."""

    return hashlib.sha256(
        json.dumps(
            [JEV_MODEL, state, questions],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class SuggestionCache:
    """Rankings already asked for, keyed by the request that produced them.

    The catalog alone cannot be the key: a ranking answers one operator
    request, so a cached answer is only reusable when the request, the selected
    skills, the tools and the source descriptions are all unchanged. The key
    covers the whole payload, so adding an MCP server, re-probing one into a
    new description, or simply moving on to the next message all miss.

    Entries live in this process, hold no operator text (the key is a digest),
    and are bounded. Two identical requests in flight at once both miss; the
    second overwrites the first's entry with the same answer.
    """

    def __init__(self, max_entries: int = MAX_CACHED_RANKINGS) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[str, ToolSuggestionReceipt] = OrderedDict()

    def get(self, key: str) -> ToolSuggestionReceipt | None:
        receipt = self._entries.get(key)
        if receipt is None:
            return None
        self._entries.move_to_end(key)
        return receipt

    def put(self, key: str, receipt: ToolSuggestionReceipt) -> None:
        # An unavailable receipt records a transient failure, never an answer.
        if receipt.status == "unavailable":
            return
        self._entries[key] = receipt
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


Option = tuple[str, str]


def _chunks(options: Sequence[Option]) -> list[list[Option]]:
    """Split (name, criterion) pairs into questions Jev's window can hold."""

    chunks: list[list[Option]] = []
    current: list[Option] = []
    size = 0
    for option in options:
        cost = len(option[0]) + len(option[1])
        if current and (
            len(current) >= MAX_CHOICE_OPTIONS
            or size + cost > MAX_CHOICE_CRITERIA_CHARS
        ):
            chunks.append(current)
            current, size = [], 0
        current.append(option)
        size += cost
    if current:
        chunks.append(current)
    return chunks


def source_criteria(
    specs: Sequence[ToolSpec], sources: Mapping[str, ToolSource]
) -> list[Option]:
    """One criterion per source that owns a deferred tool, ordered by id."""

    grouped: dict[str, list[str]] = {}
    for spec in specs:
        if spec.source_id:
            grouped.setdefault(spec.source_id, []).append(spec.name)
    options: list[Option] = []
    for source_id in sorted(grouped):
        source = sources.get(source_id)
        label = source.label if source else source_id
        description = source.description if source else ""
        if not description:
            # Nothing describes this source, so its tool names stand in for it.
            names = ", ".join(sorted(grouped[source_id])[:MAX_SOURCE_TOOL_NAMES])
            description = f"Tools: {names}" if names else "No description."
        criterion = f"{label}. {description}"[:MAX_SOURCE_CRITERION_CHARS]
        options.append((source_id, criterion))
    return options


def skill_state(skills: Sequence[SelectedSkill]) -> list[dict[str, str]]:
    """The newest selected skills, redacted and bounded like operator messages."""

    newest: list[dict[str, str]] = []
    remaining = MAX_STATE_SKILL_CHARS
    for skill in reversed(list(skills)):
        if len(newest) >= MAX_STATE_SKILLS or remaining <= 0:
            break
        instructions = redact_text(skill.instructions).strip()
        if not instructions:
            continue
        instructions = instructions[: min(MAX_SKILL_INSTRUCTION_CHARS, remaining)]
        remaining -= len(instructions)
        newest.append({"name": skill.name, "instructions": instructions})
    newest.reverse()
    return newest


def build_state(
    operator_messages: Sequence[str],
    skills: Sequence[SelectedSkill] = (),
) -> dict[str, Any]:
    """Only operator-authored text and selected skills reach Jev, bounded."""

    cleaned = [
        redact_text(item)[:MAX_OPERATOR_MESSAGE_CHARS]
        for item in operator_messages
        if item.strip()
    ]
    if not cleaned:
        raise ToolSuggestionError("no operator message to evaluate")
    state: dict[str, Any] = {
        "operator_request": cleaned[-1],
        "earlier_operator_messages": cleaned[-1 - MAX_PRIOR_OPERATOR_MESSAGES : -1],
    }
    selected = skill_state(skills)
    if selected:
        state["operator_selected_skills"] = selected
    return state


def build_questions(
    specs: Sequence[ToolSpec],
    *,
    sources: Mapping[str, ToolSource] | None = None,
    with_skills: bool = False,
) -> dict[str, dict[str, Any]]:
    """Ask which sources, then which tools, are most likely to help."""

    # Skill-free turns keep their exact previous wording, so the thresholds
    # below stay calibrated against the questions they were tuned on.
    suffix = _SKILL_CLAUSE if with_skills else ""
    ordered = sorted(specs, key=lambda item: item.name)
    questions: dict[str, dict[str, Any]] = {}
    for index, chunk in enumerate(_chunks(source_criteria(ordered, sources or {}))):
        criteria = dict(chunk)
        criteria[NONE_OPTION] = (
            "None of the listed sources can help with operator_request."
        )
        questions[f"sources_{index}"] = {
            "type": "choice",
            "instructions": (
                "Which listed source is most likely to hold a tool that helps "
                "fulfil operator_request? Choose none_of_these unless a listed "
                "source clearly applies."
            )
            + suffix,
            "criteria": criteria,
        }
    tools = [(spec.name, summary(spec) or spec.name) for spec in ordered]
    for index, chunk in enumerate(_chunks(tools)):
        criteria = dict(chunk)
        criteria[NONE_OPTION] = (
            "None of the listed tools is needed for operator_request."
        )
        questions[f"tools_{index}"] = {
            "type": "choice",
            "instructions": (
                "Which listed tool is most likely needed to act on operator_request "
                "next? Choose none_of_these unless a listed tool clearly applies."
            )
            + suffix,
            "criteria": criteria,
        }
    return questions


@dataclass(frozen=True)
class JevRanking:
    """One turn's answers: how each tool and each source was rated."""

    tools: dict[str, float]
    sources: dict[str, float]
    # The "none of these" probability of the question each tool was listed in.
    floors: dict[str, float]


def interpret_answers(
    answers: Mapping[str, Any],
    deferred: Iterable[str],
    sources: Iterable[str] = (),
) -> JevRanking:
    """Read the choice answers into per-tool and per-source probabilities."""

    known = {"tools_": set(deferred), "sources_": set(sources)}
    scores: dict[str, dict[str, float]] = {"tools_": {}, "sources_": {}}
    floors: dict[str, float] = {}
    answered_tools = False
    for key, answer in answers.items():
        prefix = next((item for item in known if key.startswith(item)), None)
        if prefix is None or not isinstance(answer, Mapping):
            continue
        raw = dict(answer.get("probabilities") or {})
        picked = {
            name: round(float(value), 4)
            for name, value in raw.items()
            if name in known[prefix] and isinstance(value, (int, float))
        }
        scores[prefix].update(picked)
        if prefix != "tools_":
            continue
        answered_tools = True
        none_value = raw.get(NONE_OPTION)
        floor = (
            round(float(none_value), 4) if isinstance(none_value, (int, float)) else 0.0
        )
        floors.update(dict.fromkeys(picked, floor))
    if not answered_tools:
        raise ToolSuggestionError("Jev response is missing tool answers")
    return JevRanking(tools=scores["tools_"], sources=scores["sources_"], floors=floors)


def source_prior(sources: Mapping[str, float], source_id: str | None) -> float:
    """A weight in [floor, 1]; neutral unless Jev ranked this tool's source."""

    if source_id is None or source_id not in sources:
        return 1.0
    return SOURCE_PRIOR_FLOOR + (1.0 - SOURCE_PRIOR_FLOOR) * sources[source_id]


def rank(
    ranking: JevRanking, deferred: Mapping[str, ToolSpec]
) -> tuple[list[str], list[str]]:
    """The top picks, weighted by source; confident ones have schemas preloaded."""

    scored: list[tuple[float, str, bool]] = []
    for name, probability in ranking.tools.items():
        spec = deferred.get(name)
        if spec is None or probability < SUGGEST_THRESHOLD:
            continue
        prior = source_prior(ranking.sources, spec.source_id)
        # Naming a tool costs one line, so a hint only needs the threshold. A
        # preloaded schema costs tokens on every step of the turn, so it also
        # has to beat its question's "none of these" option — which is what
        # replaces the old "does this turn need a tool at all" gate.
        beats_none = probability > ranking.floors.get(name, 0.0)
        scored.append((round(probability * prior, 4), name, beats_none))
    scored.sort(key=lambda item: (-item[0], item[1]))
    picks = scored[:MAX_PICKS]
    preloaded = [
        name
        for score, name, beats_none in picks
        if beats_none and score >= PRELOAD_THRESHOLD
    ][:MAX_PRELOADED]
    suggested = [name for _, name, _ in picks if name not in preloaded]
    return preloaded, suggested


def ranked_sources(ranking: JevRanking, sources: Mapping[str, ToolSource]) -> list[str]:
    """Labels of the sources worth naming in the turn's instructions."""

    ordered = sorted(
        (
            (-probability, source_id)
            for source_id, probability in ranking.sources.items()
            if probability >= SUGGEST_THRESHOLD
        )
    )
    return [
        sources[source_id].label if source_id in sources else source_id
        for _, source_id in ordered[:MAX_PICKS]
    ]


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
    skills: Sequence[SelectedSkill] = (),
    sources: Mapping[str, ToolSource] | None = None,
    cache: SuggestionCache | None = None,
) -> ToolSuggestionReceipt:
    """Ask Jev once; any failure yields an `unavailable` receipt, never an error.

    An identical request answered earlier in this process is served from
    ``cache`` without calling out at all.
    """

    names = sorted(deferred)
    if client is None:
        return ToolSuggestionReceipt(
            status="unavailable",
            deferred=names,
            error="No TypeSafe key is configured",
        )
    started = time.monotonic()
    # Names are recorded once the request exists, so a failed turn still shows
    # which skills left the host.
    sent_skills: list[str] = []
    try:
        state = build_state(operator_messages, skills)
        sent_skills = [
            str(item["name"]) for item in state.get("operator_selected_skills", [])
        ]
        questions = build_questions(
            list(deferred.values()),
            sources=sources,
            with_skills=bool(sent_skills),
        )
        if cache is not None:
            key = request_key(state, questions)
            hit = cache.get(key)
            if hit is not None:
                # This turn spent no tokens upstream, so it reports none.
                return hit.model_copy(
                    update={
                        "cached": True,
                        "latency_ms": int((time.monotonic() - started) * 1000),
                        "input_tokens": None,
                    }
                )
        body = await client.system_one(state, questions)
        ranking = interpret_answers(
            body["answers"],
            names,
            {spec.source_id for spec in deferred.values() if spec.source_id},
        )
    except (ToolSuggestionError, httpx.HTTPError, ValueError, TypeError) as exc:
        # diagnostic-expected: suggestions are optional; the turn proceeds without them
        return ToolSuggestionReceipt(
            status="unavailable",
            deferred=names,
            skills=sent_skills,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
    preloaded, suggested = rank(ranking, deferred)
    raw_usage = body.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    receipt = ToolSuggestionReceipt(
        status="suggested" if preloaded or suggested else "no_tool_needed",
        deferred=names,
        skills=sent_skills,
        preloaded=preloaded,
        suggested=suggested,
        sources=ranked_sources(ranking, sources or {}),
        probabilities=ranking.tools,
        source_probabilities=ranking.sources,
        model=str(body.get("model") or JEV_MODEL),
        latency_ms=int((time.monotonic() - started) * 1000),
        input_tokens=usage.get("input_tokens"),
    )
    if cache is not None:
        cache.put(key, receipt)
    return receipt


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
        "sources": [str(item) for item in receipt.get("sources", [])],
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
