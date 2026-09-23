#!/usr/bin/env python3
"""Capture deterministic catch-up, acknowledgment and response-summary reads.

Journey: return to saved work, distinguish unseen results from pending action,
acknowledge the server watermark, and reopen without losing pending requests.
Messages, turns, cursors and shared approval/input records remain authoritative.
The real Python API runs over disposable data, without lifespan or execution.
Both projection clocks and authentication are frozen; record creation/update
clocks remain real and only their generated timestamps are normalized.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import quote, urlencode

from fastapi.testclient import TestClient
from keyring.backends.null import Keyring as NullKeyring
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat_catchup import cursor_id
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database, EntityRow
from nebula.v3.domain import (
    Approval,
    ChatMessage,
    ChatReadCursor,
    ChatSession,
    ChatTurn,
    Engagement,
    HarnessInteraction,
    HarnessTurn,
    PairedDeviceSession,
)
from nebula.v3.storage import NebulaStore

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
ORIGIN = "https://nebula.test:9443"


def envelope(record):
    return {"kind": record.entity_kind, "payload": record.model_dump(mode="json")}


def normalize(value, fixed):
    if isinstance(value, list):
        return [normalize(item, fixed) for item in value]
    if isinstance(value, dict):
        return {
            key: "<generated>"
            if key in {"request_id", "error_id"}
            or (key in {"created_at", "updated_at"} and item not in fixed)
            else normalize(item, fixed)
            for key, item in value.items()
        }
    return value


def initial_catchup():
    records = []
    dependencies = []
    scenarios = []
    project = Engagement(
        id="project", name="Catch-up", created_at=BASE, updated_at=BASE
    )

    def session(identity, *, cursor=True, cursor_through=BASE):
        records.append(
            ChatSession(
                id=identity,
                engagement_id="project",
                title=f"Catch-up {identity}",
                provider_profile_id="fixture-provider",
                model="fixture-model",
                created_at=BASE,
                updated_at=BASE,
            )
        )
        if cursor:
            records.append(
                ChatReadCursor(
                    id=cursor_id(identity, "reader"),
                    engagement_id="project",
                    session_id=identity,
                    device_id="reader",
                    through_at=cursor_through,
                    created_at=BASE,
                    updated_at=BASE,
                )
            )
        scenarios.append(identity)

    def message(identity, session_id, sequence, role, content, seconds=1, **fields):
        stamp = fields.pop("created_at", BASE + timedelta(seconds=seconds))
        record = ChatMessage(
            id=identity,
            engagement_id=fields.pop("engagement_id", "project"),
            session_id=session_id,
            sequence=sequence,
            role=role,
            content=content,
            created_at=stamp,
            updated_at=stamp,
            **fields,
        )
        records.append(record)
        return record

    def turn(identity, session_id, *, status="routing", seconds=1, updated=5, **fields):
        backend = fields.pop("backend", "provider")
        record = ChatTurn(
            id=identity,
            engagement_id=fields.pop("engagement_id", "project"),
            session_id=session_id,
            backend=backend,
            provider_profile_id="fixture-provider" if backend == "provider" else None,
            model="fixture-model",
            status=status,
            created_at=BASE + timedelta(seconds=seconds),
            updated_at=fields.pop("updated_at", BASE + timedelta(seconds=updated)),
            **fields,
        )
        records.append(record)
        return record

    def approval(
        identity,
        *,
        session_id="pending",
        owner=None,
        status="pending",
        seconds=30,
        **fields,
    ):
        dependencies.append(
            Approval(
                id=identity,
                engagement_id=fields.pop("engagement_id", "project"),
                run_id="fixture-run",
                origin="chat",
                chat_session_id=session_id,
                chat_turn_id=owner,
                status=status,
                risk_class="local_read",
                exact_request={"fixture": "read a retained note"},
                policy_rationale="Harmless fixture",
                requested_by="fixture",
                requested_at=BASE,
                created_at=BASE,
                updated_at=BASE + timedelta(seconds=seconds),
                **fields,
            )
        )

    def harness(identity, chat, *, status="running"):
        dependencies.append(
            HarnessTurn(
                id=identity,
                engagement_id="project",
                harness_session_id="fixture-harness-session",
                origin="chat",
                chat_session_id="pending",
                chat_turn_id=chat,
                status=status,
                prompt="Read retained fixture",
                created_at=BASE,
                updated_at=BASE,
            )
        )

    def question(identity, harness_id, *, secret=False, status="pending", **fields):
        dependencies.append(
            HarnessInteraction(
                id=identity,
                engagement_id=fields.pop("engagement_id", "project"),
                harness_turn_id=harness_id,
                harness_session_id="fixture-harness-session",
                origin="chat",
                kind="user_input",
                chat_session_id="pending",
                vendor_request_id=identity,
                status=status,
                prompt="PRIVATE-PROMPT-NOT-FOR-DISPLAY"
                if secret
                else "Choose the retained note",
                contains_secret=secret,
                resolved_at=None
                if status == "pending"
                else BASE + timedelta(seconds=31),
                created_at=BASE,
                updated_at=BASE + timedelta(seconds=30),
                **fields,
            )
        )

    session("fresh", cursor=False)
    message("fresh-user", "fresh", 1, "user", "Summarize the retained note", 1)
    message(
        "fresh-answer",
        "fresh",
        2,
        "assistant",
        "A retained answer",
        20,
        metadata={"chat_turn_id": "fresh-failed"},
    )
    turn(
        "fresh-failed",
        "fresh",
        status="failed",
        updated=25,
        final_message_id="fresh-answer",
        error="Fixture response stopped",
    )
    turn(
        "fresh-wait",
        "fresh",
        status="waiting_approval",
        updated=30,
        approval_id="fresh-approval",
    )
    approval("fresh-approval", session_id="fresh", owner="fresh-wait")
    session("empty")
    session("failures")
    for number, status in enumerate(
        [
            "failed",
            "interrupted",
            "cancelled",
            "complete",
            "routing",
            "waiting_callback",
            "finalizing",
        ]
    ):
        turn(
            f"failure-{status}",
            "failures",
            status=status,
            seconds=number + 1,
            updated=20 + number,
            error=None if number % 2 else f"Synthetic {status} β",
        )
    turn(
        "failure-future",
        "failures",
        status="failed",
        updated_at=NOW + timedelta(seconds=1),
    )
    session("summary")
    message("fallback-z", "summary", 1, "user", "First fallback", 10)
    message("fallback-a", "summary", 2, "user", "Equal-time fallback", 10)
    message(
        "summary-final",
        "summary",
        3,
        "assistant",
        "Retained replaced final",
        11,
        metadata={"retracted_at": BASE.isoformat()},
    )
    message("foreign-final", "empty", 1, "assistant", "Other session final", 10)
    turn(
        "summary-final-turn",
        "summary",
        status="complete",
        seconds=1,
        final_message_id="summary-final",
    )
    turn(
        "summary-missing-final",
        "summary",
        status="failed",
        seconds=10,
        updated=12,
        final_message_id="missing",
    )
    turn(
        "summary-foreign-final",
        "summary",
        status="interrupted",
        seconds=9,
        updated=12,
        final_message_id="foreign-final",
    )
    turn("summary-fallback", "summary", status="cancelled", seconds=1)
    turn("summary-no-source", "summary", status="complete", seconds=100, updated=100)
    # CPython re.I has additional Unicode matches beyond ASCII case-insensitivity.
    greetings = [
        "hi",
        "HELLO!",
        "hey...",
        "greetings",
        "Good MORNING!",
        "good afternoon",
        "good evening",
        "thanks",
        "thank you!",
        "hİ",
        "hı",
        "thankſ",
        "\u001chi\u001f",
        "hi?",
        "hello there",
        "thank you for the note",
        "🦀",
        "...",
        "\u001c",
    ]
    for number, prompt in enumerate(greetings):
        identity = f"greeting-{number}"
        session(identity)
        # Stored user messages cannot be all whitespace. A system prompt models
        # the no-user case for the whitespace-only input scenario.
        role = "user" if prompt.strip() else "system"
        message(identity + "-user", identity, 1, role, prompt, 1)
        message(identity + "-answer", identity, 2, "assistant", "β" * 245, 2)
    for kind, extra, content in [
        ("fence", {}, "```text\nretained\n```"),
        (
            "tool",
            {"metadata": {"tool_results": [{"fixture": True}]}},
            "Retained tool receipt",
        ),
        (
            "citation",
            {
                "citations": [
                    {
                        "source_id": "source",
                        "name": "Fixture",
                        "chunk_id": "chunk",
                        "excerpt": "Retained citation",
                    }
                ]
            },
            "Retained citation",
        ),
        (
            "artifact",
            {
                "content_blocks": [
                    {"type": "artifact", "artifact_id": "fixture-artifact"}
                ]
            },
            "Retained artifact",
        ),
        (
            "image",
            {"content_blocks": [{"type": "image", "artifact_id": "fixture-image"}]},
            "Retained image",
        ),
        (
            "code",
            {"content_blocks": [{"type": "code", "text": "fixture"}]},
            "Retained code",
        ),
        (
            "text",
            {"content_blocks": [{"type": "text", "text": "fixture"}]},
            "Greeting prose",
        ),
    ]:
        identity = "output-" + kind
        session(identity)
        message(identity + "-user", identity, 1, "user", "Hi", 1)
        message(identity + "-answer", identity, 2, "assistant", content, 2, **extra)
    session("replaced-prompt")
    message(
        "replaced-prompt-user",
        "replaced-prompt",
        1,
        "user",
        "Summarize the retained note",
        1,
        metadata={"retracted_at": BASE.isoformat()},
    )
    message(
        "replaced-prompt-answer", "replaced-prompt", 2, "assistant", "Saved reply", 2
    )
    session("time-boundaries")
    message("time-user", "time-boundaries", 1, "user", "Read retained report", 0)
    message(
        "time-equal-cursor", "time-boundaries", 2, "assistant", "Equal lower bound", 0
    )
    message(
        "time-after-cursor", "time-boundaries", 3, "assistant", "After lower bound", 1
    )
    message(
        "time-equal-now",
        "time-boundaries",
        4,
        "assistant",
        "Equal server clock",
        created_at=NOW,
    )
    message(
        "time-after-now",
        "time-boundaries",
        5,
        "assistant",
        "Future result excluded",
        created_at=NOW + timedelta(microseconds=1),
    )
    # SQLite binds aware datetimes using their wall-clock fields, while Python
    # compares failure/pending times as instants. Preserve both behaviors.
    for label, hours in [("positive", 2), ("negative", -2)]:
        identity = "offset-" + label
        offset = timezone(timedelta(hours=hours))
        session(identity, cursor_through=BASE.astimezone(offset))
        message(identity + "-user", identity, 1, "user", "Read retained report", -86400)
        for number, (name, seconds) in enumerate(
            [
                ("before-instant", -1),
                ("equal-instant", 0),
                ("after-instant", 1),
                ("before-wall", hours * 3600 - 1),
                ("equal-wall", hours * 3600),
                ("after-wall", hours * 3600 + 1),
            ],
            start=2,
        ):
            message(identity + "-" + name, identity, number, "assistant", name, seconds)
        turn(
            identity + "-failed",
            identity,
            status="failed",
            seconds=-1,
            updated=1,
            final_message_id=identity + "-after-instant",
        )
        turn(
            identity + "-wait",
            identity,
            status="waiting_approval",
            seconds=-1,
            updated=1,
            approval_id=identity + "-approval",
        )
        approval(
            identity + "-approval",
            session_id=identity,
            owner=identity + "-wait",
            seconds=1,
            expires_at=(NOW + timedelta(seconds=1)).astimezone(offset),
        )
    for count in [50, 51, 101]:
        identity = f"messages-{count}"
        session(identity)
        message(identity + "-user", identity, 1, "user", "Read retained report", 1)
        for number in range(count):
            message(
                f"{identity}-{number:03}",
                identity,
                number + 2,
                "assistant",
                f"Saved result {number}",
                number + 2,
            )
    session("retracted-window")
    message("window-user", "retracted-window", 1, "user", "Read retained report", 0)
    message(
        "window-old-visible",
        "retracted-window",
        2,
        "assistant",
        "Outside the101 stored row window",
        1,
    )
    for number in range(101):
        message(
            f"window-retracted-{number:03}",
            "retracted-window",
            number + 3,
            "assistant",
            "Retracted fixture",
            number + 2,
            metadata={"retracted_at": BASE.isoformat()},
        )
    session("turns-101")
    for number in range(101):
        turn(
            f"window-turn-{number:03}",
            "turns-101",
            status="failed",
            seconds=number,
            updated=number + 1,
        )
    session("pending")
    turn(
        "pending-active",
        "pending",
        status="waiting_approval",
        approval_id="approval-fallback",
    )
    turn(
        "pending-harness",
        "pending",
        backend="harness",
        harness_turn_id="harness-active",
        status="waiting_approval",
        seconds=2,
    )
    turn(
        "pending-terminal-harness",
        "pending",
        backend="harness",
        harness_turn_id="harness-terminal",
        status="waiting_approval",
        seconds=3,
    )
    turn(
        "pending-missing-harness",
        "pending",
        backend="harness",
        harness_turn_id="harness-missing",
        status="waiting_approval",
        seconds=4,
    )
    turn("pending-terminal", "pending", status="complete", seconds=5)
    turn(
        "pending-explicit-priority",
        "pending",
        status="waiting_approval",
        approval_id="approval-explicit-terminal",
        seconds=6,
        updated=8,
    )
    turn(
        "pending-fallback-active",
        "pending",
        status="waiting_approval",
        approval_id="approval-fallback-shared",
        seconds=6,
        updated=8,
    )
    turn(
        "pending-fallback-terminal",
        "pending",
        status="complete",
        approval_id="approval-fallback-shared",
        seconds=7,
        updated=8,
    )
    turn(
        "pending-shared-active",
        "pending",
        backend="harness",
        harness_turn_id="harness-shared",
        seconds=6,
        updated=8,
    )
    turn(
        "pending-shared-terminal",
        "pending",
        backend="harness",
        harness_turn_id="harness-shared",
        status="complete",
        seconds=7,
        updated=8,
    )
    harness("harness-active", "pending-harness")
    harness("harness-terminal", "pending-terminal-harness", status="complete")
    harness("harness-shared", "pending-shared-active")
    approval("approval-active", owner="pending-active")
    approval(
        "approval-expired",
        owner="pending-active",
        expires_at=NOW - timedelta(seconds=1),
    )
    approval("approval-equal", owner="pending-active", expires_at=NOW)
    approval(
        "approval-future", owner="pending-active", expires_at=NOW + timedelta(seconds=1)
    )
    approval("approval-rejected", owner="pending-active", status="rejected")
    approval("approval-terminal", owner="pending-terminal")
    approval("approval-harness-terminal", owner="pending-terminal-harness")
    approval("approval-fallback", session_id=None)
    approval(
        "approval-via-turn", session_id="different-session", owner="pending-active"
    )
    approval("approval-explicit-terminal", owner="pending-terminal")
    approval("approval-fallback-shared", session_id=None)
    approval(
        "approval-wrong-project", owner="pending-active", engagement_id="other-project"
    )
    question("question-plain", "harness-active")
    question("question-secret", "harness-active", secret=True)
    question("question-terminal-harness", "harness-terminal")
    question("question-missing-harness", "harness-missing")
    question("question-shared", "harness-shared")
    question("question-answered", "harness-active", status="answered")
    question("question-wrong-project", "harness-active", engagement_id="other-project")
    dependencies.append(
        PairedDeviceSession(
            id="paired",
            name="Fixture phone",
            token_sha256=sha256(b"fixture-device").hexdigest(),
            csrf_sha256=sha256(b"fixture-csrf").hexdigest(),
            created_at=BASE,
            updated_at=BASE,
            last_used_at=NOW,
            idle_expires_at=NOW + timedelta(days=1),
            absolute_expires_at=NOW + timedelta(days=60),
        )
    )
    return [project], records, dependencies, scenarios


def catchup_cases(scenarios):
    cases = []

    def read(name, session="fresh", device="reader", *, auth=None, action=None):
        item = {
            "name": name,
            "method": "GET",
            "path": f"/api/v1/chat/sessions/{quote(session, safe='')}/catch-up?"
            + urlencode({"device_id": device}),
            "body": None,
            "service": {"kind": "catch_up", "session_id": session, "device_id": device},
        }
        if auth:
            item["auth"] = auth
            item["service"]["authenticated_device"] = "paired"
        if action:
            item["action"] = action
        cases.append(item)

    def cursor(name, through, revision, *, device="reader", session="fresh", auth=None):
        item = {
            "name": name,
            "method": "PUT",
            "path": f"/api/v1/chat/sessions/{quote(session, safe='')}/read-cursor",
            "body": {
                "device_id": device,
                "through_at": through,
                "expected_revision": revision,
            },
            "service": {
                "kind": "advance_cursor",
                "session_id": session,
                "device_id": device,
            },
        }
        if auth:
            item["auth"] = auth
            item["service"]["authenticated_device"] = "paired"
        cases.append(item)

    read("fresh-uninitialized")
    cursor("initialize-past-watermark", BASE.isoformat(), 0)
    read("fresh-unseen")
    cursor("advance-equal-watermark", BASE.isoformat(), 1)
    cursor("reject-past-watermark", (BASE - timedelta(seconds=1)).isoformat(), 2)
    cursor("reject-future-watermark", (NOW + timedelta(microseconds=1)).isoformat(), 2)
    cursor("reject-naive-watermark", "2020-01-01T00:00:00", 2)
    cursor("reject-stale-revision", (BASE + timedelta(seconds=10)).isoformat(), 99)
    cursor("acknowledge-returned-server-time", NOW.isoformat(), 2)
    read("acknowledged-pending-remains")
    read("reopen-acknowledged-pending-remains", action="reopen")
    read("different-device-uninitialized", device="different-device")
    paired = {
        "headers": {
            "Cookie": "nebula_device=fixture-device; nebula_csrf=fixture-csrf",
            "Origin": ORIGIN,
            "X-Nebula-CSRF": "fixture-csrf",
        }
    }
    read("paired-ignores-supplied-reader", auth=paired)
    cursor(
        "paired-initialize-owner", BASE.isoformat(), 0, device="spoofed", auth=paired
    )
    read("paired-retains-own-cursor", device="other-supplied", auth=paired)
    read("bearer-spoofed-owner-still-uninitialized", device="spoofed")
    for identity in scenarios:
        if identity != "fresh":
            read("projection-" + identity, identity)
    read("turn-window-uninitialized-no-truncation", "turns-101", device="new")
    cursor("pending-acknowledge", NOW.isoformat(), 1, session="pending")
    read("pending-visible-after-ack", "pending")
    read("pending-reopen", "pending", action="reopen")
    for identity in [
        "summary-final-turn",
        "summary-missing-final",
        "summary-foreign-final",
        "summary-fallback",
        "summary-no-source",
        "missing",
        "summary-final",
        "x" * 201,
    ]:
        cases.append(
            {
                "name": "summary-" + identity[:32],
                "method": "GET",
                "path": f"/api/v1/chat/sessions/summary/turns/{quote(identity, safe='')}/summary",
                "body": None,
                "service": {
                    "kind": "turn_summary",
                    "session_id": "summary",
                    "turn_id": identity,
                },
            }
        )
    for name, session, identity in [
        ("summary-wrong-session", "empty", "summary-final-turn"),
        ("summary-missing-session", "missing", "summary-final-turn"),
    ]:
        cases.append(
            {
                "name": name,
                "method": "GET",
                "path": f"/api/v1/chat/sessions/{session}/turns/{identity}/summary",
                "body": None,
                "service": {
                    "kind": "turn_summary",
                    "session_id": session,
                    "turn_id": identity,
                },
            }
        )
    for name, path in [
        ("device-query-required", "/api/v1/chat/sessions/fresh/catch-up"),
        ("device-query-empty", "/api/v1/chat/sessions/fresh/catch-up?device_id="),
        (
            "device-query-long",
            "/api/v1/chat/sessions/fresh/catch-up?"
            + urlencode({"device_id": "β" * 201}),
        ),
        (
            "device-query-duplicates",
            "/api/v1/chat/sessions/fresh/catch-up?device_id=missing&device_id=reader",
        ),
        (
            "device-query-invalid-utf8",
            "/api/v1/chat/sessions/fresh/catch-up?device_id=%FF",
        ),
        ("missing-session", "/api/v1/chat/sessions/missing/catch-up?device_id=reader"),
        (
            "long-session",
            "/api/v1/chat/sessions/" + "x" * 201 + "/catch-up?device_id=reader",
        ),
    ]:
        cases.append({"name": name, "method": "GET", "path": path, "body": None})
    return cases


def collect_catchup():
    projects, records, dependencies, scenarios = initial_catchup()
    all_records = [*projects, *records, *dependencies]
    initial = [envelope(record) for record in records]
    dependency_payloads = [envelope(record) for record in dependencies]
    # Model construction normalizes base timestamps. These explicit persisted
    # legacy payloads exercise normalization on later database hydration instead.
    raw_offsets = {"fresh-answer": 2, "summary-final-turn": 2, "approval-active": -2}
    for row in [*initial, *dependency_payloads]:
        if row["payload"]["id"] in raw_offsets:
            offset = timezone(timedelta(hours=raw_offsets[row["payload"]["id"]]))
            for field in ["created_at", "updated_at"]:
                row["payload"][field] = (
                    datetime.fromisoformat(row["payload"][field])
                    .astimezone(offset)
                    .isoformat()
                )
    fixed = {
        envelope(record)["payload"][field]
        for record in all_records
        for field in ["created_at", "updated_at"]
    }
    fixed.update(
        row["payload"][field]
        for row in [*initial, *dependency_payloads]
        for field in ["created_at", "updated_at"]
    )
    fixed.update(datetime.fromisoformat(value).isoformat() for value in tuple(fixed))
    fixed.update(
        datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
        for value in tuple(fixed)
    )
    cases = catchup_cases(scenarios)
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-catchup-oracle-"
    ) as directory:
        root = Path(directory)
        database = Database(root / "nebula.db")
        store = NebulaStore(database)
        for record in all_records:
            store.create(record)
        with database.session() as connection:
            for row in [*initial, *dependency_payloads]:
                if row["payload"]["id"] in raw_offsets:
                    persisted = connection.get(EntityRow, row["payload"]["id"])
                    persisted.payload = row["payload"]
            connection.commit()

        def client_for(current_store):
            return TestClient(
                create_app(
                    current_store,
                    artifact_store=ArtifactStore(root / "artifacts"),
                    credential_store=CredentialStore(keyring_backend=NullKeyring()),
                    auth_token="fixture-core",
                    enable_executable_missions=False,
                    bootstrap_workspace=False,
                ),
                raise_server_exceptions=False,
                base_url=ORIGIN,
            )

        client = client_for(store)
        try:
            with (
                patch("nebula.v3.chat_catchup.utc_now", return_value=NOW),
                patch("nebula.v3.session_state.utc_now", return_value=NOW),
                patch("nebula.v3.api.utc_now", return_value=NOW),
            ):
                for case in cases:
                    if case.get("action") == "reopen":
                        client.close()
                        database.dispose()
                        database = Database(root / "nebula.db", bootstrap=False)
                        store = NebulaStore(database)
                        client = client_for(store)
                    headers = {
                        "Authorization": "Bearer fixture-core",
                        "X-Nebula-Operation-ID": "fixture-operation",
                    }
                    if "auth" in case:
                        headers.pop("Authorization")
                        headers.update(case["auth"]["headers"])
                    response = client.request(
                        case["method"], case["path"], json=case["body"], headers=headers
                    )
                    case["expected"] = {
                        "status": response.status_code,
                        "body": normalize(response.json(), fixed),
                    }
            final = [
                normalize(envelope(record), fixed)
                for model in [ChatSession, ChatMessage, ChatTurn, ChatReadCursor]
                for record in store.list_entities(
                    model, limit=1000, include_temporary=True
                )
            ]
        finally:
            client.close()
            database.dispose()
    return {
        "format": "nebula.assistant-catchup-oracle/v1",
        "clock": NOW.isoformat(),
        "origin": ORIGIN,
        "normalization": "Preserve initial timestamps and their datetime.isoformat spellings; normalize generated created_at/updated_at and diagnostic IDs only.",
        "projects": [envelope(p) for p in projects],
        "initial_records": initial,
        "dependency_records": dependency_payloads,
        "raw_base_timestamp_offsets": raw_offsets,
        "dependency_schemas": {
            model.entity_kind: model.model_json_schema()
            for model in [Approval, HarnessInteraction, HarnessTurn]
        },
        "cases": cases,
        "final_records": final,
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in [
                "api",
                "chat_catchup",
                "session_state",
                "chat_naming",
                "domain",
                "storage",
                "diagnostic_guidance",
            ]
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = collect_catchup()
    args.output.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(fixture["cases"]),
                "initial_records": len(fixture["initial_records"]),
                "dependency_records": len(fixture["dependency_records"]),
            }
        )
    )


if __name__ == "__main__":
    main()
