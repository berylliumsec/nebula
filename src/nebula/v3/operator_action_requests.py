"""Operator action requests: typed "Needs you" records for work only a human can do.

Some blockers need a person: restart a host, power-cycle or plug in a
device, attach a credential, look at hardware, approve an account change.
Butler records each one as a typed request the operator sees at the top of the
Butler board, and closes it on its own when a machine-checkable condition (the
request's auto-check) passes.

Storage. A request is a project Work item titled ``Needs you: ...`` (the
stopgap the Butler mandate already uses, so older untyped ``Needs you:`` items
keep showing). The typed record lives in the item's description as one fenced
``nebula-operator-action`` JSON block under a readable summary, so the Work
page, Work check-ins and Butler's existing Work tools all see the same item,
and no new entity kind or Work schema change is needed. The block is
rewritten (never edited in place) whenever the record changes.

Lifecycle. ``open`` while the item is not done. Butler closes it (``closed``)
when its auto-check passes or on request; the operator closes it by marking
the Work item done (the board's Done and Cancel buttons post a Work check-in;
Cancel's summary starts with ``CANCEL_SUMMARY_PREFIX``). The sweep finishes a
record whose item was marked done outside this module, notifies, and resumes
a goal that was paused or blocked waiting on the request: the goal's blocked
reason carries ``operator-action:<request id>`` (``goal_wait_reason``) or its
metadata names the request under ``GOAL_METADATA_KEY``.

Checks are declarative (``CheckSpec``) and evaluated through
``CheckEvaluator``. ``path_exists``, ``work_item_stage`` and ``time_elapsed``
are native; host probe kinds are delegated to ``butler_host_probes`` when that
module is present, or to an injected ``probe_runner``.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, field_validator, model_validator
from sqlalchemy import and_, func, select
from sqlalchemy.orm import aliased

from .database import EntityRow
from .diagnostics import record_caught_exception
from .domain import (
    ButlerIncident,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    Engagement,
    EngagementStatus,
    NebulaModel,
    RiskClass,
    WorkItem,
    WorkUpdate,
    utc_now,
)
from .stored_entities import decode_stored
from .storage import ConflictError, NebulaStore, NotFoundError
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec
from .work import WorkCheckIn, WorkCreate, WorkPatch, WorkService

CREATE_TOOL = "nebula.supervisor.request_operator_action"
LIST_TOOL = "nebula.supervisor.list_operator_actions"
CLOSE_TOOL = "nebula.supervisor.close_operator_action"

TITLE_PREFIX = "Needs you:"
PAYLOAD_FENCE = "nebula-operator-action"
SCHEMA_VERSION = "nebula.operator-action/v1"
# A board Cancel posts a Work check-in whose summary starts with this.
CANCEL_SUMMARY_PREFIX = "Operator cancelled this request"
DONE_SUMMARY_PREFIX = "Operator marked this request done"
GOAL_WAIT_MARKER = "operator-action:"
GOAL_METADATA_KEY = "operator_action_request_id"
INCIDENT_SOURCE = "operator_action"
WORK_ACTOR = "butler"
DESCRIPTION_LIMIT = 20_000
BOARD_LIMIT = 100

OperatorAction = Literal[
    "restart_host",
    "power_cycle_device",
    "plug_in_device",
    "attach_credential",
    "physical_check",
    "approve_account_change",
    "review_model_refusal",
    "other",
]
ACTIONS: tuple[str, ...] = (
    "restart_host",
    "power_cycle_device",
    "plug_in_device",
    "attach_credential",
    "physical_check",
    "approve_account_change",
    "review_model_refusal",
    "other",
)
ACTION_LABEL: dict[str, str] = {
    "restart_host": "Restart host",
    "power_cycle_device": "Power-cycle device",
    "plug_in_device": "Plug in device",
    "attach_credential": "Attach credential",
    "physical_check": "Physical check",
    "approve_account_change": "Approve account change",
    # Raised by Core (model_refusal); resuming or ending the goal closes it,
    # and closing it never resumes the goal.
    "review_model_refusal": "Review model refusal",
    "other": "Operator action",
}
# Actions about a machine: they need an auto-check, which runs once before
# the request is created.
HOST_ACTIONS = frozenset(
    {"restart_host", "power_cycle_device", "plug_in_device", "physical_check"}
)
Urgency = Literal["low", "normal", "high", "blocking"]
URGENCY_RANK = {"blocking": 0, "high": 1, "normal": 2, "low": 3}
_WORK_PRIORITY = {
    "low": "low",
    "normal": "normal",
    "high": "high",
    "blocking": "urgent",
}
_URGENCY_OF_PRIORITY = {
    "low": "low",
    "normal": "normal",
    "high": "high",
    "urgent": "blocking",
}
RequestStatus = Literal["open", "closed", "cancelled"]

NATIVE_CHECKS = ("path_exists", "work_item_stage", "time_elapsed")
PROBE_CHECKS = (
    "disk_free",
    "mem_available",
    "tcp_reachable",
    "http_status",
    "tls_chain",
    "vpn_configs",
    "lsof_port",
    "lease_state",
    "host_health",
)
CHECK_KINDS = NATIVE_CHECKS + PROBE_CHECKS
PROBE_MODULE = "nebula.v3.butler_host_probes"

_Scalar = str | int | float | bool | None
_Line = Field(min_length=1, max_length=300)
_PAYLOAD_BLOCK = re.compile(
    r"```" + re.escape(PAYLOAD_FENCE) + r"[ \t]*\n(?P<body>.*?)\n```", re.DOTALL
)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _lines(values: list[str]) -> list[str]:
    cleaned = [" ".join(value.split()) for value in values]
    if any(not value or len(value) > 300 for value in cleaned):
        raise ValueError("each entry needs 1 to 300 characters")
    return cleaned


class CheckSpec(NebulaModel):
    """A machine-checkable condition that, once true, closes the request."""

    kind: str = Field(min_length=1, max_length=40)
    params: dict[str, _Scalar | list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_and_complete(self) -> "CheckSpec":
        if self.kind not in CHECK_KINDS:
            raise ValueError(
                f"unknown check kind {self.kind!r}; use one of {', '.join(CHECK_KINDS)}"
            )
        if len(self.params) > 16 or any(
            len(key) > 80
            or (isinstance(value, str) and len(value) > 500)
            or (isinstance(value, list) and len(value) > 16)
            for key, value in self.params.items()
        ):
            raise ValueError("check params exceed the compact limit")
        params = self.params
        if self.kind == "path_exists":
            if not isinstance(params.get("path"), str) or not params["path"]:
                raise ValueError("path_exists needs a path")
            if "exists" in params and not isinstance(params["exists"], bool):
                raise ValueError("path_exists.exists must be true or false")
        elif self.kind == "work_item_stage":
            for name in ("project_id", "item_id"):
                if not isinstance(params.get(name), str) or not params[name]:
                    raise ValueError(f"work_item_stage needs {name}")
            if not any(
                isinstance(params.get(name), str) for name in ("stage", "status")
            ):
                raise ValueError("work_item_stage needs a stage or a status to reach")
        elif self.kind == "disk_free" and not _whole(params.get("min_free_bytes")):
            raise ValueError("disk_free needs min_free_bytes")
        elif self.kind == "mem_available" and not _whole(
            params.get("min_available_bytes")
        ):
            raise ValueError("mem_available needs min_available_bytes")
        elif self.kind == "lease_state" and not isinstance(
            params.get("state_key"), str
        ):
            raise ValueError("lease_state needs state_key and state_value")
        elif self.kind == "time_elapsed":
            seconds = params.get("seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
                raise ValueError("time_elapsed needs a positive whole seconds value")
        return self

    def describe(self) -> str:
        shown = ", ".join(
            f"{key}={value}" for key, value in sorted(self.params.items())
        )
        return f"{self.kind} ({shown})" if shown else self.kind


class CheckResult(NebulaModel):
    """One evaluation: ``passed`` is None when the check could not be decided."""

    passed: bool | None
    detail: str = Field(default="", max_length=500)
    checked_at: datetime = Field(default_factory=utc_now)


class OperatorActionRecord(NebulaModel):
    """The typed record stored in a ``Needs you:`` Work item."""

    schema_version: Literal["nebula.operator-action/v1"] = "nebula.operator-action/v1"
    action: OperatorAction
    device_or_host: str = Field(min_length=1, max_length=253)
    reason: str = Field(min_length=1, max_length=1000)
    project_id: str = Field(min_length=1, max_length=200)
    goal_session_id: str | None = Field(default=None, max_length=200)
    evidence: list[str] = Field(default_factory=list, max_length=10)
    verification: list[str] = Field(min_length=1, max_length=6)
    urgency: Urgency = "normal"
    status: RequestStatus = "open"
    check: CheckSpec | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str = Field(default=WORK_ACTOR, min_length=1, max_length=200)
    last_check: CheckResult | None = None
    closed_at: datetime | None = None
    closed_by: str | None = Field(default=None, max_length=200)
    closing_evidence: list[str] = Field(default_factory=list, max_length=6)
    resumed_at: datetime | None = None
    resume_outcome: str | None = Field(default=None, max_length=300)

    @field_validator("evidence", "verification", "closing_evidence")
    @classmethod
    def bounded_lines(cls, values: list[str]) -> list[str]:
        return _lines(values)

    @model_validator(mode="after")
    def coherent(self) -> "OperatorActionRecord":
        if self.action in HOST_ACTIONS and self.check is None:
            raise ValueError(
                f"{self.action} needs an auto-check that shows when it is done"
            )
        if self.status != "open" and self.closed_at is None:
            raise ValueError("a closed or cancelled request records closed_at")
        return self


class OperatorActionView(NebulaModel):
    """A request as read back from its Work item, typed or not."""

    id: str
    project_id: str
    project_name: str = ""
    title: str
    typed: bool
    work_status: str
    work_revision: int
    created_at: datetime
    record: OperatorActionRecord | None = None
    # Untyped ``Needs you:`` items only.
    legacy_reason: str = ""
    legacy_urgency: Urgency = "normal"
    legacy_goal_session_id: str | None = None

    @property
    def status(self) -> RequestStatus:
        if self.record is not None and self.record.status != "open":
            return self.record.status
        return "closed" if self.work_status == "done" else "open"

    @property
    def goal_session_id(self) -> str | None:
        if self.record is not None:
            return self.record.goal_session_id
        return self.legacy_goal_session_id

    def summary(self) -> dict[str, Any]:
        record = self.record
        return {
            "id": self.id,
            "project": {"id": self.project_id, "name": self.project_name},
            "title": self.title,
            "typed": self.typed,
            "action": record.action if record else "other",
            "action_label": ACTION_LABEL[record.action if record else "other"],
            "device_or_host": record.device_or_host if record else "",
            "reason": record.reason if record else self.legacy_reason,
            "urgency": record.urgency if record else self.legacy_urgency,
            "status": self.status,
            "goal_session_id": self.goal_session_id,
            "evidence": list(record.evidence) if record else [],
            "verification": list(record.verification) if record else [],
            "check": record.check.model_dump(mode="json")
            if record and record.check
            else None,
            "last_check": record.last_check.model_dump(mode="json")
            if record and record.last_check
            else None,
            "created_at": (
                record.created_at if record else self.created_at
            ).isoformat(),
            "closed_at": record.closed_at.isoformat()
            if record and record.closed_at
            else None,
            "closed_by": record.closed_by if record else None,
            "closing_evidence": list(record.closing_evidence) if record else [],
            "work_revision": self.work_revision,
        }


# ---------------------------------------------------------------------------
# Work item encoding
# ---------------------------------------------------------------------------


def goal_wait_reason(request_id: str, record: OperatorActionRecord) -> str:
    """Blocked-reason text that ties a goal to this request (it mentions the operator, so hygiene never resumes it)."""

    return (
        f"Waiting on operator action {GOAL_WAIT_MARKER}{request_id}: "
        f"{ACTION_LABEL[record.action]} on {record.device_or_host}."
    )


def request_title(record: OperatorActionRecord) -> str:
    return f"{TITLE_PREFIX} {ACTION_LABEL[record.action]} on {record.device_or_host}"[
        :300
    ]


def render_description(record: OperatorActionRecord) -> str:
    """The readable summary plus the authoritative JSON block."""

    payload = json.dumps(record.model_dump(mode="json"), indent=1, sort_keys=True)
    block = f"```{PAYLOAD_FENCE}\n{payload}\n```"
    head = [
        record.reason,
        "",
        f"Action: {ACTION_LABEL[record.action]}",
        f"Device or host: {record.device_or_host}",
        f"Urgency: {record.urgency}",
        f"Status: {record.status}",
    ]
    if record.goal_session_id:
        head.append(f"Goal chat: {record.goal_session_id}")
    if record.check:
        head.append(f"Auto-check: {record.check.describe()}")
    if record.last_check:
        state = {True: "passes", False: "not yet", None: "unknown"}[
            record.last_check.passed
        ]
        head.append(f"Last check: {state}. {record.last_check.detail}".rstrip())
    sections: list[list[str]] = [head]
    if record.evidence:
        sections.append(["Evidence:", *(f"- {line}" for line in record.evidence)])
    sections.append(
        ["Verified before asking:", *(f"- {line}" for line in record.verification)]
    )
    if record.closing_evidence:
        sections.append(
            ["Closed with:", *(f"- {line}" for line in record.closing_evidence)]
        )
    sections.append(
        [
            "Mark this item done when it is finished. Butler also closes it when "
            "the auto-check passes."
        ]
    )
    text = "\n\n".join("\n".join(lines) for lines in sections)
    full = f"{text}\n\n{block}"
    if len(full) <= DESCRIPTION_LIMIT:
        return full
    # The JSON block is authoritative; the readable part gives way.
    return f"{record.reason}\n\n{block}"[:DESCRIPTION_LIMIT]


def parse_record(description: str) -> OperatorActionRecord | None:
    """The typed record in a Work item description, or None when it has none."""

    found = list(_PAYLOAD_BLOCK.finditer(description or ""))
    if not found:
        return None
    try:
        return OperatorActionRecord.model_validate(json.loads(found[-1]["body"]))
    except ValueError as exc:
        # An edited or foreign block makes the item an untyped "Needs you" item.
        record_caught_exception(
            "butler",
            "butler.operator_action.payload_unreadable",
            "An operator action request's typed block could not be read.",
            exc,
            stage="operator-action-parse",
        )
        return None


def is_request_title(title: str) -> bool:
    return title.strip().casefold().startswith(TITLE_PREFIX.casefold())


def view_of(item: WorkItem, project_name: str = "") -> OperatorActionView:
    record = parse_record(item.description)
    legacy_reason = ""
    if record is None:
        first = next(
            (line.strip() for line in item.description.splitlines() if line.strip()),
            "",
        )
        legacy_reason = (first or item.title.strip()[len(TITLE_PREFIX) :].strip())[
            :1000
        ]
    return OperatorActionView(
        id=item.id,
        project_id=item.engagement_id,
        project_name=project_name,
        title=item.title,
        typed=record is not None,
        work_status=item.status,
        work_revision=item.revision,
        created_at=item.created_at,
        record=record,
        legacy_reason=legacy_reason or item.title,
        legacy_urgency=_URGENCY_OF_PRIORITY.get(item.priority, "normal"),  # type: ignore[arg-type]
        legacy_goal_session_id=item.assignee_session_id
        or (item.source_id if item.source_kind == "chat" else None),
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _request_rows(
    store: NebulaStore,
    *,
    done: bool | None,
    project_ids: Iterable[str] | None = None,
    description_contains: Iterable[str] = (),
    limit: int | None = None,
) -> list[tuple[WorkItem, str]]:
    project = aliased(EntityRow, name="operator_action_project")
    title = EntityRow.payload["title"].as_string()
    status = EntityRow.payload["status"].as_string()
    description = EntityRow.payload["description"].as_string()
    statement = (
        select(EntityRow.payload, project.payload["name"].as_string())
        .join(
            project,
            and_(
                project.id == EntityRow.engagement_id,
                project.kind == Engagement.entity_kind,
            ),
        )
        .where(
            EntityRow.kind == WorkItem.entity_kind,
            func.lower(title).startswith(TITLE_PREFIX.lower(), autoescape=True),
            func.coalesce(project.payload["status"].as_string(), "")
            != EngagementStatus.ARCHIVED.value,
        )
        .order_by(EntityRow.created_at, EntityRow.id)
    )
    if done is True:
        statement = statement.where(status == "done")
    elif done is False:
        statement = statement.where(status != "done")
    if project_ids is not None:
        statement = statement.where(EntityRow.engagement_id.in_(list(project_ids)))
    for text in description_contains:
        statement = statement.where(description.contains(text, autoescape=True))
    if limit is not None:
        statement = statement.limit(limit)
    async with store.database.session() as database:
        rows = list(await database.execute(statement))
    return [
        (decode_stored(WorkItem, payload), str(name or "")) for payload, name in rows
    ]


async def list_requests(
    store: NebulaStore,
    *,
    include_closed: bool = False,
    project_ids: Iterable[str] | None = None,
    limit: int = BOARD_LIMIT,
) -> list[OperatorActionView]:
    """Requests, open ones first by urgency then age; closed ones only on request."""

    rows = await _request_rows(
        store,
        done=None if include_closed else False,
        project_ids=project_ids,
    )
    views = [view_of(item, name) for item, name in rows]
    if not include_closed:
        views = [view for view in views if view.status == "open"]

    def order(view: OperatorActionView) -> tuple[int, int, str]:
        urgency = view.record.urgency if view.record else view.legacy_urgency
        return (
            0 if view.status == "open" else 1,
            URGENCY_RANK[urgency],
            view.created_at.isoformat(),
        )

    return sorted(views, key=order)[:limit]


async def board_requests(
    store: NebulaStore, *, limit: int = BOARD_LIMIT
) -> list[dict[str, Any]]:
    """Open requests for the Butler board, with their goal chat's title and status."""

    views = await list_requests(store, limit=limit)
    sessions = {view.goal_session_id for view in views if view.goal_session_id}
    titles: dict[str, str] = {}
    goal_status: dict[str, str] = {}
    if sessions:
        async with store.database.session() as database:
            for session_id, chat_title in await database.execute(
                select(EntityRow.id, EntityRow.payload["title"].as_string()).where(
                    EntityRow.kind == ChatSession.entity_kind,
                    EntityRow.id.in_(sorted(sessions)),
                )
            ):
                titles[str(session_id)] = str(chat_title or "")
            for session_id, status in await database.execute(
                select(
                    EntityRow.payload["session_id"].as_string(),
                    EntityRow.payload["status"].as_string(),
                ).where(
                    EntityRow.kind == ChatGoal.entity_kind,
                    EntityRow.payload["session_id"].as_string().in_(sorted(sessions)),
                )
            ):
                goal_status[str(session_id)] = str(status or "")
    items = []
    for view in views:
        item = view.summary()
        session_id = view.goal_session_id
        item["goal"] = (
            {
                "session_id": session_id,
                "title": titles.get(session_id, ""),
                "status": goal_status.get(session_id),
            }
            if session_id
            else None
        )
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


class CheckEvaluator(Protocol):
    async def evaluate(
        self, check: CheckSpec, record: OperatorActionRecord, *, now: datetime
    ) -> CheckResult: ...


ProbeRunner = Callable[[str, Mapping[str, Any]], Awaitable[Any] | Any]
"""``(kind, probe arguments) -> probe result``; the result is read by ``probe_outcome``."""

# Check params that state the condition; the rest are the probe's own arguments.
CONDITION_KEYS = frozenset(
    {
        "expect",
        "expect_status",
        "min_free_bytes",
        "min_available_bytes",
        "connected",
        "state_key",
        "state_value",
    }
)


def _module_probe_runner(project_id: str) -> ProbeRunner | None:
    """``ButlerHostProbes.probe`` for the request's project, when installed."""

    try:
        module = importlib.import_module(PROBE_MODULE)
    except ImportError as exc:
        record_caught_exception(
            "butler",
            "butler.operator_action.probes_unavailable",
            "Host probes are not installed; probe checks stay undecided.",
            exc,
            stage="operator-action-check",
        )
        return None
    probes = getattr(module, "ButlerHostProbes", None)
    if probes is None:
        return None
    instance = probes(project_id=project_id, actor="operator-action-check")

    async def run(kind: str, arguments: Mapping[str, Any]) -> Any:
        return await instance.probe(
            {
                **arguments,
                "kind": kind,
                "reason": "operator action request auto-check",
            }
        )

    return run


def _int_param(params: Mapping[str, Any], key: str) -> int | None:
    value = params.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def probe_outcome(
    kind: str, params: Mapping[str, Any], result: Any
) -> tuple[bool | None, str]:
    """Whether a ``butler_host_probes`` result meets the check's condition.

    ``None`` means the probe could not tell (it timed out, the command failed,
    or the answer is not readable).
    """

    if isinstance(result, bool):
        return result, ""
    if not isinstance(result, Mapping):
        return None, f"unreadable probe answer: {type(result).__name__}"
    expect = params.get("expect", True) is not False
    if kind == "tcp_reachable":
        reachable = result.get("reachable")
        if not isinstance(reachable, bool):
            return None, "reachability unknown"
        return reachable == expect, "reachable" if reachable else "not reachable"
    if kind == "http_status":
        code = result.get("status_code")
        if not isinstance(code, int) or code == 0:
            return None, "no HTTP answer"
        wanted = _int_param(params, "expect_status")
        passed = code == wanted if wanted is not None else 200 <= code < 400
        return passed, f"HTTP {code}"
    if kind == "lsof_port":
        listening = result.get("listening")
        if not isinstance(listening, bool):
            return None, "listener state unknown"
        return listening == expect, "listening" if listening else "not listening"
    if kind == "disk_free":
        disk = result.get("disk")
        free = disk.get("free_bytes") if isinstance(disk, Mapping) else None
        floor = _int_param(params, "min_free_bytes")
        if not isinstance(free, int) or floor is None:
            return None, "free space unknown"
        return free >= floor, f"{free} bytes free (need {floor})"
    if kind == "mem_available":
        memory = result.get("memory")
        available = (
            memory.get("available_bytes") if isinstance(memory, Mapping) else None
        )
        floor = _int_param(params, "min_available_bytes")
        if not isinstance(available, int) or floor is None:
            return None, "available memory unknown"
        return available >= floor, f"{available} bytes available (need {floor})"
    if kind == "host_health":
        disks = result.get("disks")
        memory = result.get("memory")
        if not isinstance(disks, list) or not isinstance(memory, Mapping):
            return None, "host health unknown"
        if any(
            not isinstance(entry, Mapping) or entry.get("exit_code") != 0
            for entry in disks
        ):
            return False, "a disk read failed"
        floor = _int_param(params, "min_free_bytes")
        if floor is not None:
            for entry in disks:
                disk = entry.get("disk")
                free = disk.get("free_bytes") if isinstance(disk, Mapping) else None
                if not isinstance(free, int) or free < floor:
                    return False, f"{entry.get('path')} below {floor} bytes free"
        floor = _int_param(params, "min_available_bytes")
        available = memory.get("available_bytes")
        if floor is not None and (not isinstance(available, int) or available < floor):
            return False, f"memory below {floor} bytes available"
        return available is not None, "host answered"
    if kind == "tls_chain":
        if not result.get("handshake"):
            return False, "no TLS handshake"
        issuer = params.get("expected_issuer_substring")
        if isinstance(issuer, str):
            chain = result.get("chain") or []
            found = any(
                isinstance(entry, Mapping) and issuer in str(entry.get("issuer") or "")
                for entry in chain
            )
            return found, "issuer matches" if found else "issuer differs"
        code = result.get("verify_code")
        return code == 0, f"verify code {code}"
    if kind == "vpn_configs":
        if result.get("ok") is not True:
            return None, "VPN list unavailable"
        configs = [
            entry for entry in result.get("configs") or [] if isinstance(entry, Mapping)
        ]
        name = params.get("name_substring")
        if isinstance(name, str):
            configs = [entry for entry in configs if name in str(entry.get("name"))]
        if params.get("connected") is True:
            configs = [
                entry
                for entry in configs
                if str(entry.get("status", "")).casefold() == "connected"
            ]
        return bool(configs) == expect, f"{len(configs)} matching VPN configurations"
    if kind == "lease_state":
        if result.get("ok") is not True:
            return None, "lease state unavailable"
        state = result.get("state")
        key = params.get("state_key")
        if not isinstance(key, str) or not isinstance(state, Mapping):
            return None, "lease state unreadable"
        value = state.get(key)
        return value == params.get("state_value"), f"{key} is {value}"
    for key in ("passed", "ok"):
        if isinstance(result.get(key), bool):
            return result[key], ""
    return None, "no rule reads this probe"


class DefaultCheckEvaluator:
    """Native checks here; host probe kinds through ``butler_host_probes``."""

    def __init__(
        self, store: NebulaStore, probe_runner: ProbeRunner | None = None
    ) -> None:
        self.store = store
        self._probe_runner = probe_runner

    async def evaluate(
        self, check: CheckSpec, record: OperatorActionRecord, *, now: datetime
    ) -> CheckResult:
        params = check.params
        if check.kind == "path_exists":
            path = Path(str(params["path"])).expanduser()
            want = params.get("exists", True) is not False
            present = await asyncio.to_thread(path.exists)
            return CheckResult(
                passed=present == want,
                detail=f"{path} {'exists' if present else 'does not exist'}",
                checked_at=now,
            )
        if check.kind == "time_elapsed":
            seconds = int(params["seconds"])  # type: ignore[arg-type]
            elapsed = (now - record.created_at).total_seconds()
            return CheckResult(
                passed=elapsed >= seconds,
                detail=f"{int(elapsed)} of {seconds} seconds elapsed",
                checked_at=now,
            )
        if check.kind == "work_item_stage":
            try:
                item = await self.store.get(WorkItem, str(params["item_id"]))
            except NotFoundError:
                # diagnostic-expected: a removed Work item leaves the check undecided.
                return CheckResult(
                    passed=None, detail="the Work item was not found", checked_at=now
                )
            if item.engagement_id != params["project_id"]:
                return CheckResult(
                    passed=None,
                    detail="the Work item is in another project",
                    checked_at=now,
                )
            wanted_stage = params.get("stage")
            wanted_status = params.get("status")
            stage_ok = (
                wanted_stage is None
                or (item.stage or "") == str(wanted_stage).strip().casefold()
            )
            status_ok = wanted_status is None or item.status == wanted_status
            return CheckResult(
                passed=stage_ok and status_ok,
                detail=f"Work item stage {item.stage or 'none'}, status {item.status}",
                checked_at=now,
            )
        runner = self._probe_runner or _module_probe_runner(record.project_id)
        if runner is None:
            return CheckResult(
                passed=None,
                detail=f"host probe {check.kind} is not available",
                checked_at=now,
            )
        try:
            arguments = {
                key: value for key, value in params.items() if key not in CONDITION_KEYS
            }
            answer = runner(check.kind, arguments)
            if inspect.isawaitable(answer):
                answer = await answer
        except Exception as exc:
            record_caught_exception(
                "butler",
                "butler.operator_action.probe_failed",
                "A host probe for an operator action request failed.",
                exc,
                stage="operator-action-check",
                metadata={"kind": check.kind},
            )
            return CheckResult(
                passed=None,
                detail=f"host probe {check.kind} failed: {type(exc).__name__}",
                checked_at=now,
            )
        passed, detail = probe_outcome(check.kind, params, answer)
        return CheckResult(
            passed=passed, detail=(detail or check.kind)[:500], checked_at=now
        )


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class OperatorActionNotifier(Protocol):
    async def opened(self, request_id: str, record: OperatorActionRecord) -> None: ...

    async def closed(self, request_id: str, record: OperatorActionRecord) -> None: ...


class IncidentNotifier:
    """In-app notification: a Butler incident per request in its project.

    Nebula has no push channel (no APNs, web push or iOS notification code), so
    the operator is told through Butler's incident feed, which the Oversight
    view and Butler's own reads already surface and acknowledge.
    """

    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    async def opened(self, request_id: str, record: OperatorActionRecord) -> None:
        await self._write(request_id, record, opened=True)

    async def closed(self, request_id: str, record: OperatorActionRecord) -> None:
        await self._write(request_id, record, opened=False)

    async def _write(
        self, request_id: str, record: OperatorActionRecord, *, opened: bool
    ) -> None:
        from .butler_oversight import (
            ButlerEventPayload,
            ButlerEvidenceRef,
            _append_project_event,
            _entity_in_transaction,
            incident_id,
            project_stream_id,
        )

        project_id = record.project_id
        key = f"{INCIDENT_SOURCE}:{request_id}"
        record_id = incident_id(project_id, key)
        heading = f"Needs you: {ACTION_LABEL[record.action]} on {record.device_or_host} ({record.urgency})."[
            :1000
        ]
        now = utc_now()
        goal_ids: list[str] = []
        if record.goal_session_id:
            goal = await _goal_of_session(self.store, record.goal_session_id)
            if goal is not None:
                goal_ids = [goal.id]
        async with self.store.transaction(immediate=True) as transaction:
            await transaction.lock_operation_stream(project_stream_id(project_id))
            incident = await _entity_in_transaction(
                transaction, ButlerIncident, record_id
            )
            if opened:
                if incident is not None and incident.status != "resolved":
                    return
                if incident is None:
                    incident = ButlerIncident(
                        id=record_id,
                        engagement_id=project_id,
                        incident_key=key,
                        source=INCIDENT_SOURCE,
                        verification_state="agent_reported",
                        summary=heading,
                        impact=record.reason,
                        next_action=f"{ACTION_LABEL[record.action]} on {record.device_or_host}, then mark the request done on the Butler board.",
                        evidence_summary=[line[:300] for line in record.evidence[:8]],
                        goal_ids=goal_ids,
                        session_ids=[record.goal_session_id]
                        if record.goal_session_id
                        else [],
                        observed_at=now,
                    )
                    await transaction.add(incident)
                    event_type = "incident.opened"
                else:
                    incident = await transaction.update(
                        ButlerIncident,
                        record_id,
                        {
                            "status": "open",
                            "resolved_at": None,
                            "acknowledged_at": None,
                            "summary": heading,
                            "impact": record.reason,
                            "observed_at": now,
                        },
                        expected_revision=incident.revision,
                    )
                    event_type = "incident.reopened"
            else:
                if incident is None or incident.status == "resolved":
                    return
                incident = await transaction.update(
                    ButlerIncident,
                    record_id,
                    {"status": "resolved", "resolved_at": now},
                    expected_revision=incident.revision,
                )
                event_type = "incident.resolved"
            await _append_project_event(
                transaction,
                engagement_id=project_id,
                event_type=event_type,
                payload=ButlerEventPayload(
                    project_id=project_id,
                    session_id=record.goal_session_id,
                    goal_id=goal_ids[0] if goal_ids else None,
                    source="core",
                    actor_role="core",
                    summary=heading
                    if opened
                    else f"Operator action request {record.status}: {ACTION_LABEL[record.action]} on {record.device_or_host}."[
                        :500
                    ],
                    metrics={
                        "incident_status": incident.status,
                        "operator_action": record.action,
                        "urgency": record.urgency,
                    },
                    verification_state="agent_reported" if opened else "core_observed",
                    evidence_refs=[
                        ButlerEvidenceRef(
                            kind="incident", id=incident.id, project_id=project_id
                        )
                    ],
                    incident_key=key,
                ),
                idempotency_key=f"incident:{incident.id}:{event_type}:{incident.revision}",
                now=now,
            )


async def _notify(
    notifier: OperatorActionNotifier | None,
    event: Literal["opened", "closed"],
    request_id: str,
    record: OperatorActionRecord,
) -> bool:
    """Send one notification; a failure is recorded and never undoes the request."""

    if notifier is None:
        return False
    try:
        await getattr(notifier, event)(request_id, record)
    except Exception as exc:
        record_caught_exception(
            "butler",
            "butler.operator_action.notify_failed",
            "An operator action notification could not be sent.",
            exc,
            stage="operator-action-notify",
            metadata={"event": event},
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def _goal_of_session(store: NebulaStore, session_id: str) -> ChatGoal | None:
    goals = await store.list_session_entities(ChatGoal, session_id)
    return goals[0] if len(goals) == 1 else None


def _same_device(left: str, right: str) -> bool:
    return " ".join(left.split()).casefold() == " ".join(right.split()).casefold()


async def find_duplicate(
    store: NebulaStore, record: OperatorActionRecord
) -> OperatorActionView | None:
    """An open request for the same action and device (and goal, when named)."""

    rows = await _request_rows(
        store, done=False, description_contains=[f"```{PAYLOAD_FENCE}"]
    )
    for item, name in rows:
        view = view_of(item, name)
        existing = view.record
        if existing is None or view.status != "open":
            continue
        if existing.action != record.action or not _same_device(
            existing.device_or_host, record.device_or_host
        ):
            continue
        if (
            record.goal_session_id
            and existing.goal_session_id != record.goal_session_id
        ):
            continue
        return view
    return None


_CREATE_LOCK = asyncio.Lock()


async def _save_record(
    service: WorkService,
    project_id: str,
    item_id: str,
    update: Callable[[OperatorActionRecord], OperatorActionRecord | None],
) -> tuple[WorkItem, OperatorActionRecord | None]:
    """Rewrite one item's typed block on its newest revision."""

    for _attempt in range(3):
        item = await service.get(project_id, item_id)
        record = parse_record(item.description)
        if record is None:
            return item, None
        changed = update(record)
        if changed is None:
            return item, record
        try:
            saved = await service.patch(
                project_id,
                item_id,
                WorkPatch(
                    description=render_description(changed),
                    expected_revision=item.revision,
                ),
            )
        except ConflictError:
            # diagnostic-expected: a concurrent item edit retries on its newest revision.
            continue
        return saved, changed
    raise ConflictError("operator action request changed concurrently; retry")


async def resume_waiting_goal(
    store: NebulaStore,
    work: Any,
    request_id: str,
    record: OperatorActionRecord,
) -> str:
    """Resume the request's goal when it is paused or blocked for this request."""

    if not record.goal_session_id:
        return "no_goal"
    goal = await _goal_of_session(store, record.goal_session_id)
    if goal is None:
        return "goal_not_found"
    if goal.status not in {ChatGoalStatus.PAUSED, ChatGoalStatus.BLOCKED}:
        return f"goal_{goal.status.value}"
    waiting = (
        f"{GOAL_WAIT_MARKER}{request_id}" in (goal.blocked_reason or "")
        or goal.metadata.get(GOAL_METADATA_KEY) == request_id
    )
    if not waiting:
        return "goal_not_waiting_on_request"
    if work is None or not hasattr(work, "goal_action"):
        return "resume_unavailable"
    await work.goal_action(
        goal.engagement_id,
        {
            "session_id": goal.session_id,
            "expected_revision": goal.revision,
            "action": "resume",
            "reason": (
                f"Operator action done: {ACTION_LABEL[record.action]} on "
                f"{record.device_or_host}."
            )[:2000],
        },
    )
    return "resumed"


async def _finish_resume(
    store: NebulaStore,
    service: WorkService,
    work: Any,
    project_id: str,
    request_id: str,
    record: OperatorActionRecord,
) -> str | None:
    """Resume the waiting goal and record the outcome once."""

    if record.status != "closed" or record.resumed_at is not None:
        return None
    if not record.goal_session_id:
        return None
    try:
        outcome = await resume_waiting_goal(store, work, request_id, record)
    except Exception as exc:
        record_caught_exception(
            "butler",
            "butler.operator_action.resume_failed",
            "Resuming a goal after its operator action closed failed.",
            exc,
            stage="operator-action-resume",
        )
        outcome = f"resume_failed: {type(exc).__name__}"
    if outcome == "resume_unavailable":
        # Left for a sweep that has Butler's Work gateway.
        return outcome
    now = utc_now()

    def mark(current: OperatorActionRecord) -> OperatorActionRecord | None:
        if current.resumed_at is not None:
            return None
        return current.model_copy(
            update={"resumed_at": now, "resume_outcome": outcome[:300]}
        )

    await _save_record(service, project_id, request_id, mark)
    return outcome


async def close_request(
    store: NebulaStore,
    project_id: str,
    request_id: str,
    *,
    outcome: Literal["closed", "cancelled"],
    evidence: list[str],
    closed_by: str,
    work: Any = None,
    notifier: OperatorActionNotifier | None = None,
) -> dict[str, Any]:
    """Close or cancel one request, notify, and resume a goal waiting on it."""

    service = WorkService(store)
    item = await service.get(project_id, request_id)
    if not is_request_title(item.title):
        raise ValueError("that Work item is not an operator action request")
    lines = _lines(evidence[:6]) if evidence else []
    now = utc_now()
    before = view_of(item)
    if before.record is not None and before.record.status != "open":
        return {"closed": False, "already": before.record.status, **before.summary()}

    def close(current: OperatorActionRecord) -> OperatorActionRecord | None:
        if current.status != "open":
            return None
        return current.model_copy(
            update={
                "status": outcome,
                "closed_at": now,
                "closed_by": closed_by,
                "closing_evidence": lines,
            }
        )

    item, record = await _save_record(service, project_id, request_id, close)
    if item.status != "done":
        summary = (
            CANCEL_SUMMARY_PREFIX if outcome == "cancelled" else "Operator action done"
        )
        detail = f"{summary}: {'; '.join(lines)}" if lines else f"{summary}."
        await service.check_in(
            project_id,
            request_id,
            WorkCheckIn(
                summary=detail[:4000],
                status="done",
                request_id=f"operator-action-close:{request_id}",
            ),
            actor_kind="operator" if closed_by == "operator" else "agent",
            actor_id=closed_by[:200],
        )
    notified = False
    resumed = None
    if record is not None:
        notified = await _notify(notifier, "closed", request_id, record)
        resumed = await _finish_resume(
            store, service, work, project_id, request_id, record
        )
    view = view_of(await service.get(project_id, request_id))
    return {"closed": True, "notified": notified, "resume": resumed, **view.summary()}


async def create_request(
    store: NebulaStore,
    arguments: Mapping[str, Any],
    *,
    work: Any = None,
    evaluator: CheckEvaluator | None = None,
    notifier: OperatorActionNotifier | None = None,
) -> dict[str, Any]:
    """Verify, de-duplicate, then save one request as a ``Needs you:`` Work item."""

    verification = arguments.get("verification")
    if not isinstance(verification, list) or not verification:
        raise ValueError(
            "verify first: give the checks you ran that show only the operator can do this"
        )
    goal_session_id = arguments.get("goal_session_id")
    project_id = arguments.get("project_id")
    if goal_session_id:
        session = await store.get(ChatSession, str(goal_session_id))
        if project_id and project_id != session.engagement_id:
            raise ValueError("the goal chat belongs to another project")
        project_id = session.engagement_id
    if not project_id:
        raise ValueError("name the project (project_id) or the goal chat")
    await store.get(Engagement, str(project_id))
    record = OperatorActionRecord.model_validate(
        {
            "action": arguments.get("action"),
            "device_or_host": arguments.get("device_or_host"),
            "reason": arguments.get("reason"),
            "project_id": project_id,
            "goal_session_id": goal_session_id or None,
            "evidence": list(arguments.get("evidence") or []),
            "verification": list(verification),
            "urgency": arguments.get("urgency", "normal"),
            "check": arguments.get("check"),
            "created_by": str(arguments.get("created_by") or WORK_ACTOR),
        }
    )
    async with _CREATE_LOCK:
        existing = await find_duplicate(store, record)
        if existing is not None:
            return {
                "created": False,
                "refusal": "duplicate",
                "message": "An open request already covers this action and device.",
                "request": existing.summary(),
            }
        if record.check is not None:
            evaluator = evaluator or DefaultCheckEvaluator(store)
            result = await evaluator.evaluate(record.check, record, now=utc_now())
            if result.passed is True:
                return {
                    "created": False,
                    "refusal": "check_already_passes",
                    "message": "The auto-check already passes; there is nothing for the operator to do.",
                    "check": result.model_dump(mode="json"),
                }
            record = record.model_copy(update={"last_check": result})
        service = WorkService(store)
        item = await service.create(
            str(project_id),
            WorkCreate(
                title=request_title(record),
                description=render_description(record),
                status="ready",
                priority=_WORK_PRIORITY[record.urgency],  # type: ignore[arg-type]
                source_kind="chat" if record.goal_session_id else "manual",
                source_id=record.goal_session_id,
            ),
            actor_id=WORK_ACTOR,
        )
    block = None
    if arguments.get("block_goal") and record.goal_session_id:
        block = await _block_goal(store, work, item.id, record)
    notified = await _notify(notifier, "opened", item.id, record)
    return {
        "created": True,
        "notified": notified,
        "goal_block": block,
        "goal_wait_reason": goal_wait_reason(item.id, record),
        "request": view_of(item).summary(),
    }


async def _block_goal(
    store: NebulaStore, work: Any, request_id: str, record: OperatorActionRecord
) -> str:
    """Block the running goal on this request so the sweep resumes it later."""

    assert record.goal_session_id
    goal = await _goal_of_session(store, record.goal_session_id)
    if goal is None:
        return "goal_not_found"
    if goal.status != ChatGoalStatus.RUNNING:
        return f"goal_{goal.status.value}"
    if work is None or not hasattr(work, "goal_action"):
        return "block_unavailable"
    try:
        await work.goal_action(
            goal.engagement_id,
            {
                "session_id": goal.session_id,
                "expected_revision": goal.revision,
                "action": "block",
                "reason": goal_wait_reason(request_id, record),
            },
        )
    except Exception as exc:
        record_caught_exception(
            "butler",
            "butler.operator_action.block_failed",
            "Blocking a goal on its operator action request failed.",
            exc,
            stage="operator-action-block",
        )
        return f"block_failed: {type(exc).__name__}"
    return "blocked"


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


async def _closing_outcome(
    store: NebulaStore, item: WorkItem
) -> Literal["closed", "cancelled"]:
    """Whether the item's newest check-in cancelled it (the board's Cancel)."""

    updates = await store.find_entities(
        WorkUpdate,
        {"item_id": item.id},
        engagement_id=item.engagement_id,
        newest_first=True,
        limit=1,
    )
    if updates and updates[0].summary.startswith(CANCEL_SUMMARY_PREFIX):
        return "cancelled"
    return "closed"


async def sweep_operator_actions(
    store: NebulaStore,
    work: Any = None,
    *,
    evaluator: CheckEvaluator | None = None,
    notifier: OperatorActionNotifier | None = None,
    project_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Re-check open requests, close those that pass, and resume their goals.

    Deterministic and idempotent: it also finishes requests whose Work item
    was marked done elsewhere (the board, the Work page) and retries a goal
    resume a sweep without ``work`` could not do.
    """

    evaluator = evaluator or DefaultCheckEvaluator(store)
    service = WorkService(store)
    selected = list(project_ids) if project_ids is not None else None
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    now = utc_now()

    def note(item: WorkItem, action: str, outcome: str, **detail: Any) -> None:
        actions.append(
            {
                "request_id": item.id,
                "project_id": item.engagement_id,
                "action": action,
                "outcome": outcome,
                **detail,
            }
        )

    fence = f"```{PAYLOAD_FENCE}"
    # 1. Open requests: evaluate their checks.
    for item, _name in await _request_rows(
        store, done=False, project_ids=selected, description_contains=[fence]
    ):
        record = parse_record(item.description)
        if record is None or record.status != "open" or record.check is None:
            continue
        try:
            result = await evaluator.evaluate(record.check, record, now=now)
            if result.passed is True:
                closed = await close_request(
                    store,
                    item.engagement_id,
                    item.id,
                    outcome="closed",
                    evidence=[
                        f"Auto-check passed: {result.detail or record.check.kind}"
                    ],
                    closed_by="auto-check",
                    work=work,
                    notifier=notifier,
                )
                note(item, "auto_close", "applied", resume=closed.get("resume"))
                continue
            previous = record.last_check
            if previous is None or (previous.passed, previous.detail) != (
                result.passed,
                result.detail,
            ):

                def remember(
                    current: OperatorActionRecord, result: CheckResult = result
                ) -> OperatorActionRecord | None:
                    if current.status != "open":
                        return None
                    return current.model_copy(update={"last_check": result})

                await _save_record(service, item.engagement_id, item.id, remember)
            note(item, "check", "waiting", passed=result.passed, detail=result.detail)
        except Exception as exc:
            record_caught_exception(
                "butler",
                "butler.operator_action.sweep_failed",
                "One operator action request could not be swept; the sweep continues.",
                exc,
                stage="operator-action-sweep",
            )
            errors.append({"request_id": item.id, "error": type(exc).__name__})

    # 2. Items marked done outside this module whose record is still open.
    for item, _name in await _request_rows(
        store,
        done=True,
        project_ids=selected,
        description_contains=[fence, '"status": "open"'],
    ):
        record = parse_record(item.description)
        if record is None or record.status != "open":
            continue
        try:
            outcome = await _closing_outcome(store, item)
            closed = await close_request(
                store,
                item.engagement_id,
                item.id,
                outcome=outcome,
                evidence=["The operator marked the Work item done."],
                closed_by="operator",
                work=work,
                notifier=notifier,
            )
            note(item, "finish", outcome, resume=closed.get("resume"))
        except Exception as exc:
            record_caught_exception(
                "butler",
                "butler.operator_action.sweep_failed",
                "One operator action request could not be swept; the sweep continues.",
                exc,
                stage="operator-action-sweep",
            )
            errors.append({"request_id": item.id, "error": type(exc).__name__})

    # 3. Closed requests whose goal resume is still pending.
    if work is not None:
        for item, _name in await _request_rows(
            store,
            done=True,
            project_ids=selected,
            description_contains=[fence, '"status": "closed"', '"resumed_at": null'],
        ):
            record = parse_record(item.description)
            if record is None or record.status != "closed" or record.resumed_at:
                continue
            if not record.goal_session_id:
                continue
            try:
                resumed = await _finish_resume(
                    store, service, work, item.engagement_id, item.id, record
                )
                note(item, "resume", resumed or "skipped")
            except Exception as exc:
                record_caught_exception(
                    "butler",
                    "butler.operator_action.sweep_failed",
                    "One operator action request could not be swept; the sweep continues.",
                    exc,
                    stage="operator-action-sweep",
                )
                errors.append({"request_id": item.id, "error": type(exc).__name__})

    counts: dict[str, int] = {}
    for entry in actions:
        key = f"{entry['action']}:{entry['outcome']}"
        counts[key] = counts.get(key, 0) + 1
    return {
        "at": now.isoformat(),
        "counts": counts,
        "actions": actions,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Butler operations
# ---------------------------------------------------------------------------

_EVIDENCE_LIST = {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "maxLength": 300},
}
_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(CHECK_KINDS)},
        "params": {"type": "object", "maxProperties": 16},
    },
    "required": ["kind"],
    "additionalProperties": False,
    "description": (
        "Closes the request when it passes. Native: path_exists {path, exists?}, "
        "work_item_stage {project_id, item_id, stage?|status?}, time_elapsed "
        "{seconds}. Host probes (nebula.supervisor host probe arguments plus a "
        "condition): tcp_reachable {host?, target, port, expect?}, http_status "
        "{url, expect_status?}, lsof_port {port, expect?}, disk_free {path, "
        "min_free_bytes}, mem_available {min_available_bytes}, host_health "
        "{host?, min_free_bytes?, min_available_bytes?}, tls_chain {target, "
        "expected_issuer_substring?}, vpn_configs {name_substring?, connected?, "
        "expect?}, lease_state {state_key, state_value}."
    ),
}
_CREATE_INPUT = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "device_or_host": {"type": "string", "minLength": 1, "maxLength": 253},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
        "project_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "goal_session_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "The waiting goal's chat; its project is used when project_id is omitted.",
        },
        "evidence": {**_EVIDENCE_LIST, "maxItems": 10},
        "verification": {
            **_EVIDENCE_LIST,
            "minItems": 1,
            "maxItems": 6,
            "description": "Required: what you checked that shows only the operator can do this.",
        },
        "urgency": {
            "type": "string",
            "enum": ["low", "normal", "high", "blocking"],
            "default": "normal",
        },
        "check": _CHECK_SCHEMA,
        "block_goal": {
            "type": "boolean",
            "default": False,
            "description": "Block the running goal on this request; it resumes when the request closes.",
        },
    },
    "required": ["action", "device_or_host", "reason", "verification"],
    "additionalProperties": False,
}
_LIST_INPUT = {
    "type": "object",
    "properties": {
        "project_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 64,
        },
        "include_closed": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
    },
    "additionalProperties": False,
}
_CLOSE_INPUT = {
    "type": "object",
    "properties": {
        "project_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "outcome": {"type": "string", "enum": ["closed", "cancelled"]},
        "evidence": {**_EVIDENCE_LIST, "minItems": 1, "maxItems": 6},
    },
    "required": ["project_id", "request_id", "outcome", "evidence"],
    "additionalProperties": False,
}


class ButlerOperatorActions:
    """Butler's operator action request tools, in ``ButlerGoalHygiene``'s shape."""

    def __init__(
        self,
        store: NebulaStore,
        work: Any = None,
        *,
        evaluator: CheckEvaluator | None = None,
        notifier: OperatorActionNotifier | None = None,
    ) -> None:
        self.store = store
        self.work = work
        self.evaluator = evaluator or DefaultCheckEvaluator(store)
        self.notifier: OperatorActionNotifier | None = (
            notifier if notifier is not None else IncidentNotifier(store)
        )

    async def create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await create_request(
            self.store,
            arguments,
            work=self.work,
            evaluator=self.evaluator,
            notifier=self.notifier,
        )

    async def list_requests(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_ids = arguments.get("project_ids")
        views = await list_requests(
            self.store,
            include_closed=bool(arguments.get("include_closed", False)),
            project_ids=list(project_ids) if project_ids is not None else None,
            limit=int(arguments.get("limit", 50)),
        )
        return {"requests": [view.summary() for view in views]}

    async def close(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await close_request(
            self.store,
            str(arguments["project_id"]),
            str(arguments["request_id"]),
            outcome=arguments["outcome"],
            evidence=list(arguments.get("evidence") or []),
            closed_by=WORK_ACTOR,
            work=self.work,
            notifier=self.notifier,
        )

    async def sweep(self) -> dict[str, Any]:
        return await sweep_operator_actions(
            self.store, self.work, evaluator=self.evaluator, notifier=self.notifier
        )

    def actions(self) -> list[AnalysisTool]:
        output = {"type": "object", "additionalProperties": True}
        return [
            AnalysisTool(
                ToolSpec(
                    name=CREATE_TOOL,
                    description=(
                        "Ask the operator for something only a person can do: restart a "
                        "host, power-cycle or plug in a device, attach a credential, a "
                        "physical check, or approve an account change. Verify first and "
                        "pass what you checked as verification. Give an auto-check that "
                        "shows when it is done (required for host actions); if it already "
                        "passes the request is refused. An open request for the same "
                        "action and device (and goal) is returned instead of a new one. "
                        "The request shows at the top of the Butler board and closes when "
                        "its check passes; block_goal parks the goal until then."
                    ),
                    input_schema=_CREATE_INPUT,
                    output_schema=output,
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                self.create,
            ),
            AnalysisTool(
                ToolSpec(
                    name=LIST_TOOL,
                    description=(
                        "List operator action requests (Needs you items) across "
                        "projects, open ones first by urgency, with their auto-check "
                        "state. include_closed adds closed and cancelled ones."
                    ),
                    input_schema=_LIST_INPUT,
                    output_schema=output,
                    risk_class=RiskClass.LOCAL_READ,
                ),
                self.list_requests,
            ),
            AnalysisTool(
                ToolSpec(
                    name=CLOSE_TOOL,
                    description=(
                        "Close (done) or cancel an operator action request with the "
                        "evidence that it is finished or no longer needed. Closing "
                        "resumes a goal blocked on the request."
                    ),
                    input_schema=_CLOSE_INPUT,
                    output_schema=output,
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                self.close,
            ),
        ]
