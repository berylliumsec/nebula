"""Transaction-local, allowlisted observation envelopes from existing records."""

from urllib.parse import urlsplit, urlunsplit
from sqlalchemy import select
from ..database import EntityRow

SOURCE_KINDS = frozenset(
    {
        "browser_traffic",
        "browser_websocket_frames",
        "browser_actions",
        "browser_commands",
        "browser_repeater_results",
        "observations",
        "evidence",
    }
)


def envelope(kind, payload, connection=None):
    payload = dict(payload)
    metadata = payload.get("metadata") or {}
    if kind == "observations" and payload.get("source") == "browser_companion":
        payload = {
            **payload,
            **{
                key: metadata[key]
                for key in (
                    "operation",
                    "status",
                    "url",
                    "page_revision",
                    "element_count",
                    "capture_kind",
                    "tab_id",
                    "identity_id",
                    "browser_session_id",
                    "chat_turn_id",
                    "artifact_id",
                )
                if key in metadata
            },
        }
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
    metadata = payload.get("metadata") or metadata
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
        "operation",
        "element_count",
        "capture_kind",
        "page_revision",
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
        "tool_call_id": payload.get("tool_call_id") or metadata.get("tool_call_id"),
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
