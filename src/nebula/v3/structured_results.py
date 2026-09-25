"""Schema-agnostic structured results: publish any JSON output, explore it later.

A producer — an in-product agent, an external agent over the API, or an
operator — publishes a JSON-compatible value and an optional, *separate*
presentation-hints document. Core keeps the published value exactly as it
arrived: it is the authority, and every dashboard view is derived from it.

Nothing here knows what a result means. The only inspection performed is
structural (size, depth, node count, root type) so the explorer can describe a
payload before it materialises one, and so a runaway producer cannot write an
unbounded document into the project database.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .domain import (
    ChatGoal,
    ChatGoalStatus,
    Engagement,
    RiskClass,
    StructuredResult,
    StructuredResultStats,
    utc_now,
)
from .conditional_reads import conditional, content_etag
from .storage import NebulaStore
from .tools import (
    IdempotencyBehavior,
    ToolExecutionResult,
    ToolInvocation,
    ToolPlugin,
    ToolSpec,
)

DASHBOARD_PUBLISH_TOOL_NAME = "dashboard.publish"

# A published payload is read back into one browser tab, so it is bounded well
# below the artifact store's limits. Producers with more than this should write
# an artifact and publish a result that points at it.
MAX_RESULT_BYTES = 4_000_000
MAX_RESULT_NODES = 250_000
MAX_RESULT_DEPTH = 200
MAX_HINT_BYTES = 200_000
# Retention is explicit rather than silent: publishing past this many results in
# one project removes the oldest, and every caller is told which ones went.
MAX_RESULTS_PER_PROJECT = 500
PREVIEW_FIELDS = 6
PREVIEW_VALUE_CHARS = 120
# How long a running goal may go without showing the operator where it is.
SNAPSHOT_INTERVAL_SECONDS = 600
STREAM_LABEL_CHARS = 200


class StructuredResultRejected(ValueError):
    """The payload is not something the explorer can hold, and why."""


def _root_type(
    value: Any,
) -> Literal["object", "array", "string", "number", "boolean", "null"]:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    raise StructuredResultRejected(
        f"a result cannot contain a {type(value).__name__} value; publish JSON-compatible data"
    )


def inspect_payload(value: Any) -> StructuredResultStats:
    """Describe a JSON-compatible payload, refusing what the explorer cannot hold.

    The walk is iterative (a deeply nested document must not exhaust the
    interpreter stack) and tracks the ancestors of the node being visited so a
    structure that refers back into itself is refused by name rather than
    hanging. Values are only inspected, never rewritten.
    """

    root = _root_type(value)
    nodes = 0
    deepest = 0
    # Each frame is (value, depth, ancestor ids on the path to this value).
    stack: list[tuple[Any, int, tuple[int, ...]]] = [(value, 0, ())]
    while stack:
        current, depth, ancestors = stack.pop()
        nodes += 1
        deepest = max(deepest, depth)
        if nodes > MAX_RESULT_NODES:
            raise StructuredResultRejected(
                f"the result holds more than {MAX_RESULT_NODES} values; publish a bounded excerpt"
            )
        if depth > MAX_RESULT_DEPTH:
            raise StructuredResultRejected(
                f"the result nests deeper than {MAX_RESULT_DEPTH} levels; publish a bounded excerpt"
            )
        kind = _root_type(current)
        if kind not in {"object", "array"}:
            continue
        if id(current) in ancestors:
            raise StructuredResultRejected(
                "the result refers back into itself; publish an acyclic document"
            )
        path = (*ancestors, id(current))
        if kind == "object":
            for key, item in current.items():
                if not isinstance(key, str):
                    raise StructuredResultRejected(
                        "object keys must be strings in a JSON-compatible result"
                    )
                stack.append((item, depth + 1, path))
        else:
            for item in current:
                stack.append((item, depth + 1, path))
    # Encoding comes after the walk so a self-referential document is refused
    # by name rather than by ``json``'s generic circular-reference error.
    encoded = _encode(value)
    if len(encoded) > MAX_RESULT_BYTES:
        raise StructuredResultRejected(
            f"the result is {len(encoded)} bytes; the limit is {MAX_RESULT_BYTES}. "
            "Publish a bounded excerpt, or store the full document as an artifact."
        )
    top_level = len(value) if isinstance(value, (dict, list)) else 0
    return StructuredResultStats(
        byte_size=len(encoded),
        node_count=nodes,
        max_depth=deepest,
        root_type=root,
        top_level_count=top_level,
    )


def _encode(value: Any) -> bytes:
    try:
        return json.dumps(
            value, separators=(",", ":"), allow_nan=False, ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        # diagnostic-expected: reported to the publisher as a refusal, not a fault.
        raise StructuredResultRejected(
            f"the result is not JSON-compatible: {exc}"
        ) from exc


def validate_hints(hints: dict[str, Any] | None) -> dict[str, Any] | None:
    """Bound an optional hints document without interpreting it.

    Hints are advice for the explorer's presentation layer, which validates
    each field itself and ignores what it cannot use. Core only refuses a
    document that is not a bounded JSON object, so that a hint vocabulary can
    grow in the interface without a Core release.
    """

    if hints is None:
        return None
    if not isinstance(hints, dict):
        raise StructuredResultRejected("presentation hints must be a JSON object")
    encoded = _encode(hints)
    if len(encoded) > MAX_HINT_BYTES:
        raise StructuredResultRejected(
            f"presentation hints are {len(encoded)} bytes; the limit is {MAX_HINT_BYTES}"
        )
    return hints


def payload_preview(value: Any) -> list[dict[str, Any]]:
    """A few top-level scalars so a list row is recognisable.

    This is presentation only. It claims no meaning for the fields it picks,
    it is bounded, and the dashboard never treats it as the result.
    """

    if not isinstance(value, dict):
        return []
    preview: list[dict[str, Any]] = []
    for key, item in value.items():
        if len(preview) >= PREVIEW_FIELDS:
            break
        if isinstance(item, (dict, list)):
            continue
        if isinstance(item, str) and len(item) > PREVIEW_VALUE_CHARS:
            preview.append(
                {"key": key, "value": item[:PREVIEW_VALUE_CHARS], "truncated": True}
            )
            continue
        preview.append({"key": key, "value": item, "truncated": False})
    return preview


class StructuredResultSummary(BaseModel):
    """List rows stay light: the payload itself is fetched when one is opened."""

    model_config = ConfigDict(extra="forbid")

    id: str
    engagement_id: str
    title: str
    summary: str
    producer: str
    origin: str
    labels: list[str]
    created_at: str
    updated_at: str
    revision: int
    stats: StructuredResultStats
    has_hints: bool
    preview: list[dict[str, Any]]
    stream: str | None = None
    stream_label: str = ""
    sequence: int = 1
    chat_session_id: str | None = None
    tool_call_id: str | None = None


class StructuredResultPublish(BaseModel):
    """What a producer sends. ``result`` accepts any JSON-compatible value."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    result: Any = None
    summary: str = Field(default="", max_length=2_000)
    producer: str = Field(default="", max_length=200)
    origin: Literal["agent", "tool", "operator", "api"] = "api"
    labels: list[str] = Field(default_factory=list, max_length=12)
    stream: str | None = Field(default=None, max_length=200)
    stream_label: str = Field(default="", max_length=STREAM_LABEL_CHARS)
    hints: dict[str, Any] | None = None
    chat_session_id: str | None = Field(default=None, max_length=200)
    chat_turn_id: str | None = Field(default=None, max_length=200)
    tool_call_id: str | None = Field(default=None, max_length=200)


class PublishOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: StructuredResult
    retention_removed: list[str] = Field(default_factory=list)


def summarize(result: StructuredResult) -> StructuredResultSummary:
    return StructuredResultSummary(
        id=result.id,
        engagement_id=result.engagement_id,
        title=result.title,
        summary=result.summary,
        producer=result.producer,
        origin=result.origin,
        labels=list(result.labels),
        created_at=result.created_at.isoformat(),
        updated_at=result.updated_at.isoformat(),
        revision=result.revision,
        stats=result.stats,
        has_hints=result.hints is not None,
        preview=payload_preview(result.result),
        stream=result.stream,
        stream_label=result.stream_label,
        sequence=result.sequence,
        chat_session_id=result.chat_session_id,
        tool_call_id=result.tool_call_id,
    )


def publish_result(
    store: NebulaStore,
    engagement_id: str,
    request: StructuredResultPublish,
) -> PublishOutcome:
    """Persist one published result, then enforce per-project retention."""

    store.get(Engagement, engagement_id)
    stats = inspect_payload(request.result)
    hints = validate_hints(request.hints)
    created = store.create(
        StructuredResult(
            engagement_id=engagement_id,
            stream=request.stream,
            stream_label=request.stream_label,
            sequence=_next_sequence(store, engagement_id, request.stream),
            title=request.title,
            summary=request.summary,
            producer=request.producer,
            origin=request.origin,
            labels=list(request.labels),
            result=request.result,
            hints=hints,
            stats=stats,
            chat_session_id=request.chat_session_id,
            chat_turn_id=request.chat_turn_id,
            tool_call_id=request.tool_call_id,
        )
    )
    return PublishOutcome(
        result=created, retention_removed=_prune(store, engagement_id)
    )


def _next_sequence(store: NebulaStore, engagement_id: str, stream: str | None) -> int:
    """Number the snapshots of one stream so a series reads in order."""

    if stream is None:
        return 1
    existing = [
        item for item in _all_results(store, engagement_id) if item.stream == stream
    ]
    return max((item.sequence for item in existing), default=0) + 1


def _all_results(store: NebulaStore, engagement_id: str) -> list[StructuredResult]:
    collected: list[StructuredResult] = []
    offset = 0
    while True:
        page = store.list_entities(
            StructuredResult, engagement_id=engagement_id, offset=offset, limit=1_000
        )
        collected.extend(page)
        if len(page) < 1_000:
            return collected
        offset += 1_000


def _prune(store: NebulaStore, engagement_id: str) -> list[str]:
    """Drop the oldest results past the cap, and report exactly which ones."""

    total = store.count(StructuredResult, engagement_id=engagement_id)
    if total <= MAX_RESULTS_PER_PROJECT:
        return []
    excess = total - MAX_RESULTS_PER_PROJECT
    oldest = store.list_entities(
        StructuredResult,
        engagement_id=engagement_id,
        limit=min(excess, 1_000),
    )
    removed: list[str] = []
    for stale in oldest:
        store.delete(StructuredResult, stale.id)
        removed.append(stale.id)
    return removed


def structured_results_router(store: NebulaStore) -> APIRouter:
    router = APIRouter(tags=["structured-results"])

    @router.get(
        "/projects/{project_id}/structured-results",
        response_model=list[StructuredResultSummary],
    )
    def list_results(
        project_id: str,
        request: Request,
        response: Response,
        stream: str | None = Query(default=None, max_length=200),
        chat_session_id: str | None = Query(default=None, max_length=200),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Any:
        store.get(Engagement, project_id)
        # The conversation filter runs in SQL, so a chat's poll parses only
        # its own page of results instead of every result the project keeps.
        filters = {
            key: value
            for key, value in (
                ("stream", stream),
                ("chat_session_id", chat_session_id),
            )
            if value is not None
        }
        page = store.find_entities(
            StructuredResult,
            filters,
            engagement_id=project_id,
            offset=offset,
            limit=limit,
            newest_first=True,
        )
        summaries = [summarize(item) for item in page]
        # Open conversations poll this; an unchanged page answers 304.
        unchanged = conditional(request, response, content_etag("results", summaries))
        return unchanged or summaries

    @router.post(
        "/projects/{project_id}/structured-results",
        response_model=PublishOutcome,
        status_code=201,
    )
    def publish(project_id: str, body: StructuredResultPublish) -> PublishOutcome:
        try:
            return publish_result(store, project_id, body)
        except StructuredResultRejected as exc:
            # diagnostic-expected: a producer's payload problem, reported to it.
            raise HTTPException(422, str(exc)) from exc

    @router.get(
        "/projects/{project_id}/structured-results/{result_id}",
        response_model=StructuredResult,
    )
    def read_result(project_id: str, result_id: str) -> StructuredResult:
        return _owned(store, project_id, result_id)

    @router.delete(
        "/projects/{project_id}/structured-results/{result_id}", status_code=204
    )
    def delete_result(
        project_id: str, result_id: str, expected_revision: int | None = None
    ) -> Response:
        owned = _owned(store, project_id, result_id)
        store.delete(StructuredResult, owned.id, expected_revision=expected_revision)
        return Response(status_code=204)

    return router


def _owned(store: NebulaStore, project_id: str, result_id: str) -> StructuredResult:
    result = store.get(StructuredResult, result_id)
    if result.engagement_id != project_id:
        raise HTTPException(404, "That result belongs to another project")
    return result


# -- goal snapshots ---------------------------------------------------------


def latest_snapshot(
    store: NebulaStore, engagement_id: str, stream: str
) -> StructuredResult | None:
    """The most recent result published under one stream, or None."""

    published = [
        item for item in _all_results(store, engagement_id) if item.stream == stream
    ]
    if not published:
        return None
    return max(published, key=lambda item: (item.sequence, item.created_at))


def snapshot_overdue(
    store: NebulaStore,
    engagement_id: str,
    stream: str,
    *,
    now: datetime | None = None,
    interval_seconds: int = SNAPSHOT_INTERVAL_SECONDS,
) -> tuple[bool, float | None]:
    """Whether a stream has gone longer than the interval without a snapshot.

    Returns the decision and the seconds since the last one, so a caller can
    tell an operator (or a model) how long it has actually been. A stream with
    nothing published is always overdue: the first snapshot is what gives the
    operator something to watch.
    """

    latest = latest_snapshot(store, engagement_id, stream)
    if latest is None:
        return True, None
    elapsed = ((now or utc_now()) - latest.created_at).total_seconds()
    return elapsed >= interval_seconds, elapsed


def goal_snapshot_instruction(
    store: NebulaStore,
    goal: ChatGoal,
    *,
    now: datetime | None = None,
    interval_seconds: int = SNAPSHOT_INTERVAL_SECONDS,
) -> str:
    """Ask a running goal's model to show the operator where the work stands.

    The ask is added only when the goal's series has gone quiet for longer than
    the interval, so a goal that is already publishing is left alone. It never
    asks for anything to be repeated or summarised away: the snapshot is data
    the model already has in front of it.
    """

    if goal.status != ChatGoalStatus.RUNNING:
        return ""
    overdue, elapsed = snapshot_overdue(
        store,
        goal.engagement_id,
        goal.id,
        now=now,
        interval_seconds=interval_seconds,
    )
    if not overdue:
        return ""
    minutes = max(1, interval_seconds // 60)
    since = (
        "Nothing has been published for this goal yet"
        if elapsed is None
        else f"The last snapshot was {int(elapsed // 60)} minutes ago"
    )
    return (
        "\n\nShow the operator where this work stands. "
        f"{since}, and they see your progress only through the result "
        f"dashboard, which is asked for every {minutes} minutes.\n"
        f"Call {DASHBOARD_PUBLISH_TOOL_NAME} once, now, with a short title and "
        "a result that depicts the current state of the work: what you have "
        "examined, decided or changed, the values, code or relationships "
        "behind it, and what is next. Send whatever shape that data already "
        "has — an object, a list of rows, a code snippet with its language, "
        "nodes and edges. Report only what you actually have; an empty or "
        "uncertain state is worth publishing as exactly that. Then continue "
        "the goal in the same turn."
    )


# -- tool ------------------------------------------------------------------

PUBLISH_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "Short operator-readable name for this result.",
        },
        "result": {
            "description": (
                "Any JSON value: object, array, string, number, boolean or null. "
                "It is stored exactly as sent and stays the authority."
            )
        },
        "summary": {
            "type": "string",
            "maxLength": 2_000,
            "description": "Optional one-line description of what was published.",
        },
        "producer": {
            "type": "string",
            "maxLength": 200,
            "description": "Optional label for what produced the result.",
        },
        "labels": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "maxLength": 40},
        },
        "stream": {
            "type": "string",
            "maxLength": 200,
            "description": (
                "Optional. Group successive snapshots of the same work under "
                "one key. Inside a goal this is ignored: the goal's own series "
                "collects its snapshots in order."
            ),
        },
        "hints": {
            "type": "object",
            "description": (
                "Optional presentation hints (preferred title/summary fields, "
                "field order, hidden fields, table columns, graph mappings, "
                "redaction). Kept beside the result, never merged into it. "
                "Hints are advisory: the result renders without them."
            ),
        },
    },
    "required": ["title", "result"],
    "additionalProperties": False,
}


def dashboard_publish_spec() -> ToolSpec:
    return ToolSpec(
        name=DASHBOARD_PUBLISH_TOOL_NAME,
        version="1",
        description=(
            "Show the operator where this goal's work stands. Publishing sends "
            "a structured result to the project's result dashboard, which they "
            "read beside the conversation and in Project > Results as a "
            "summary, table, relationship graph, tree and raw JSON.\n\n"
            "Send whatever shape the data already has: no schema is registered "
            "and none is required, so objects, arrays, code snippets, "
            "explanations and node/edge relationships are all acceptable. The "
            "value is stored unchanged and stays the authority. Multi-line "
            "text renders as a readable block, and a sibling 'language' field "
            "labels a code snippet.\n\n"
            "Publish at each significant turn — what was just examined, "
            "decided or changed, and what is next — and whenever a result is "
            "large enough that pasting it into the conversation would bury it. "
            "Snapshots of this goal are collected into one ordered series "
            "automatically, so the operator can follow the work as it happens."
        ),
        input_schema=PUBLISH_INPUT,
        output_schema={"type": "object", "additionalProperties": True},
        # Durable project state, no filesystem and no network: the write is
        # confined to this project's own records.
        risk_class=RiskClass.WORKSPACE_WRITE,
        network_access=False,
        filesystem_access="none",
        idempotency=IdempotencyBehavior.SAFE,
        timeout_seconds=30,
        budget_class="execution",
    )


class PublishResultTool(ToolPlugin):
    """Let a goal's agent hand a structured result to the operator's dashboard.

    The tool is bound to the goal it was built for, so the series a snapshot
    joins is Core's decision rather than an argument a model has to get right:
    the stream key is the goal's identity and the label is its objective.
    """

    def __init__(self, store: NebulaStore, goal: ChatGoal | None = None) -> None:
        self.spec = dashboard_publish_spec()
        self.store = store
        self.goal = goal

    async def execute(
        self, invocation: ToolInvocation, runner: Any
    ) -> ToolExecutionResult:
        del runner
        started = utc_now()
        arguments = dict(invocation.arguments)
        try:
            request = StructuredResultPublish(
                title=str(arguments.get("title") or "").strip() or "Untitled result",
                result=arguments.get("result"),
                summary=str(arguments.get("summary") or ""),
                producer=str(arguments.get("producer") or "") or invocation.tool_name,
                origin="agent",
                labels=[str(item) for item in arguments.get("labels") or []][:12],
                stream=self._stream(arguments),
                stream_label=(
                    self.goal.objective[:STREAM_LABEL_CHARS] if self.goal else ""
                ),
                hints=arguments.get("hints"),
                chat_session_id=invocation.chat_session_id,
                chat_turn_id=invocation.chat_turn_id,
            )
            outcome = publish_result(self.store, invocation.engagement_id, request)
        except (StructuredResultRejected, ValueError) as exc:
            # diagnostic-expected: returned to the model as a readable refusal.
            return self._failure(started, str(exc))
        published = outcome.result
        return ToolExecutionResult(
            output={
                "tool": DASHBOARD_PUBLISH_TOOL_NAME,
                "result_id": published.id,
                "title": published.title,
                "stream": published.stream,
                "sequence": published.sequence,
                "stats": published.stats.model_dump(mode="json"),
                "hints_stored": published.hints is not None,
                "retention_removed": outcome.retention_removed,
                # Where the operator reads it; the payload is deliberately not
                # echoed back into the conversation.
                "open_path": (
                    f"/projects/{published.engagement_id}/results/{published.id}"
                ),
                "detail": (
                    f"Published {published.title!r} to the result dashboard"
                    + (
                        f" as step {published.sequence} of {published.stream!r}"
                        if published.stream
                        else ""
                    )
                    + ". The operator reads it in Project > Results and beside "
                    "this conversation."
                ),
            },
            exit_code=0,
            execution=_timing(started),
        )

    def _stream(self, arguments: dict[str, Any]) -> str | None:
        # A goal owns its own series; only an unbound tool takes the model's key.
        if self.goal is not None:
            return self.goal.id
        raw = arguments.get("stream")
        return str(raw)[:200] if raw else None

    def _failure(self, started: Any, detail: str) -> ToolExecutionResult:
        return ToolExecutionResult(
            output={
                "tool": DASHBOARD_PUBLISH_TOOL_NAME,
                "result_id": None,
                "detail": detail,
            },
            stderr=detail[:1_000],
            exit_code=1,
            execution=_timing(started),
        )


def _timing(started: Any) -> dict[str, Any]:
    completed = utc_now()
    return {
        "runtime": "structured_results",
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_seconds": max(0.0, (completed - started).total_seconds()),
    }


__all__ = [
    "DASHBOARD_PUBLISH_TOOL_NAME",
    "MAX_RESULTS_PER_PROJECT",
    "MAX_RESULT_BYTES",
    "MAX_RESULT_DEPTH",
    "MAX_RESULT_NODES",
    "PublishResultTool",
    "StructuredResultPublish",
    "StructuredResultRejected",
    "StructuredResultSummary",
    "SNAPSHOT_INTERVAL_SECONDS",
    "dashboard_publish_spec",
    "goal_snapshot_instruction",
    "inspect_payload",
    "latest_snapshot",
    "payload_preview",
    "publish_result",
    "structured_results_router",
    "snapshot_overdue",
    "summarize",
    "validate_hints",
]
