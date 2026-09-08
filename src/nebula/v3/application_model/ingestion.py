"""Transaction-local, allowlisted observation envelopes from existing records."""

from urllib.parse import urlsplit, urlunsplit
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from ..database import ApplicationModelOutboxRow, EntityRow
from ..domain import utc_now
from .domain import ModelSession, semantic_hash

SOURCE_KINDS = frozenset(
    {
        "browser_traffic",
        "browser_websocket_frames",
        "browser_actions",
        "browser_commands",
        "browser_repeater_results",
        "evidence",
    }
)


def envelope(kind, payload, connection=None):
    payload = dict(payload)
    if connection is not None:
        # Resolve durable cross-record references instead of guessing causality
        # from timestamps. A Repeater tab is not a browser tab.
        reference = payload.get("exchange_id")
        parent = (
            connection.execute(
                select(EntityRow.payload).where(
                    EntityRow.id == reference,
                    EntityRow.kind == "browser_traffic",
                    EntityRow.engagement_id == payload.get("engagement_id"),
                )
            ).scalar()
            if reference
            else None
        )
        if parent:
            for name in ("session_id", "identity_id", "tab_id"):
                payload[name] = parent.get(name)
        elif kind == "browser_repeater_results":
            parent = connection.execute(
                select(EntityRow.payload).where(
                    EntityRow.id == payload.get("tab_id"),
                    EntityRow.kind == "browser_repeater_tabs",
                    EntityRow.engagement_id == payload.get("engagement_id"),
                )
            ).scalar()
            if parent:
                payload["session_id"] = parent.get("session_id")
                payload["identity_id"] = parent.get("identity_id")
                payload["tab_id"] = "repeater:" + payload["tab_id"]
    metadata = payload.get("metadata") or {}
    browser_session_id = payload.get("session_id") or metadata.get("browser_session_id")
    if not browser_session_id:
        return None
    facts = {}
    for key in (
        "method",
        "protocol",
        "status_code",
        "status",
        "state",
        "request_bytes",
        "response_bytes",
        "duration_ms",
        "blocked",
        "truncated",
        "opcode",
        "direction",
        "payload_bytes",
    ):
        value = payload.get(key)
        if type(value) in (str, bool, int):
            facts[key] = {
                "kind": "concrete",
                "type": {str: "string", bool: "boolean", int: "integer"}[type(value)],
                "value": value,
            }
    route = (
        payload.get("url")
        or payload.get("page_url")
        or payload.get("expected_page_url")
    )
    if route:
        try:
            url = urlsplit(route)
            port = url.port
        except ValueError:
            url, port = urlsplit(""), None
        host = url.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if port:
            host += ":" + str(port)
        facts["route"] = {
            "kind": "concrete",
            "type": "string",
            "value": urlunsplit((url.scheme, host, url.path, "", "")),
        }
    for key in ("identity_id", "tab_id"):
        if payload.get(key):
            facts[key] = {"kind": "concrete", "type": "identity", "value": payload[key]}
    # A receipt does not establish remote success. Its normalized status is
    # recorded as receipt metadata only, never as a resource lifecycle value.
    artifacts = [
        payload[k]
        for k in (
            "request_body_artifact_id",
            "response_body_artifact_id",
            "artifact_id",
            "payload_artifact_id",
        )
        if payload.get(k)
    ]
    return {
        "browser_session_id": browser_session_id,
        "tab_id": payload.get("tab_id") or metadata.get("tab_id"),
        "identity_id": payload.get("identity_id") or metadata.get("identity_id"),
        "command_id": payload.get("command_id") or metadata.get("browser_command_id"),
        "action_id": payload.get("action_id") or metadata.get("browser_action_id"),
        "tool_call_id": payload.get("tool_call_id"),
        "exchange_id": payload.get("exchange_id"),
        "facts": facts,
        "response_body_artifact_id": payload.get("response_body_artifact_id"),
        "evidence_ids": payload.get("evidence_ids", [])
        + ([payload["id"]] if kind == "evidence" else []),
        "artifact_ids": artifacts,
        "occurred_at": payload.get("observed_at")
        or payload.get("captured_at")
        or payload.get("completed_at")
        or payload.get("updated_at")
        or payload.get("created_at"),
        "source_kind": kind,
        "source_id": payload["id"],
        "source_revision": payload["revision"],
    }


def enqueue_source(connection, entity):
    if entity.entity_kind not in SOURCE_KINDS:
        return
    payload = entity.model_dump(mode="json")
    data = envelope(entity.entity_kind, payload, connection)
    if data is None:
        return
    sessions = connection.execute(
        select(EntityRow.payload).where(
            EntityRow.kind == ModelSession.entity_kind,
            EntityRow.engagement_id == payload.get("engagement_id"),
            EntityRow.payload["browser_session_id"].as_string()
            == data["browser_session_id"],
            EntityRow.payload["status"].as_string() == "active",
        )
    ).scalars()
    for session in sessions:
        enqueue_envelope(connection, session, data)


def enqueue_envelope(connection, session, data):
    key = semantic_hash(
        [
            session["id"],
            data["source_kind"],
            data["source_id"],
            data["source_revision"],
            "1",
        ]
    )
    if connection.execute(
        select(ApplicationModelOutboxRow.id).where(ApplicationModelOutboxRow.id == key)
    ).first():
        return
    statement = insert(ApplicationModelOutboxRow).values(
        id=key,
        engagement_id=session["engagement_id"],
        model_session_id=session["id"],
        source_kind=data["source_kind"],
        source_id=data["source_id"],
        source_revision=data["source_revision"],
        adapter_version="1",
        payload=data,
        status="pending",
        attempts=0,
        created_at=utc_now(),
    )
    # Concurrent live/history insertion must not abort the source transaction.
    # The unique source key is authoritative; use a savepoint for its race.
    try:
        with connection.begin_nested():
            connection.execute(statement)
    except IntegrityError:
        if not connection.execute(
            select(ApplicationModelOutboxRow.id).where(
                ApplicationModelOutboxRow.id == key
            )
        ).first():
            raise
