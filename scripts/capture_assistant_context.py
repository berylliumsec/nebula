#!/usr/bin/env python3
"""Deterministic saved-context/cursor oracle using isolated Python services.

Mounts only existing routers in a test client. No Core lifespan, runtime,
provider or tool is started. Clock-dependent creation/update fields are labeled
explicitly; their history retention is checked separately by the Rust tests.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import tempfile

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from nebula.v3.chat_catchup import catchup_router
from nebula.v3.chat_decisions import decision_snapshot, decisions_router
from nebula.v3.database import Database
from nebula.v3.domain import ChatMessage, ChatSession
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError

ROOT = Path(__file__).resolve().parents[1]


def normalize(value):
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "<server-time>"
            if key in {"created_at", "updated_at"}
            else normalize(item)
            for key, item in value.items()
        }
    return value


def collect_context():
    at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    sessions = [
        ChatSession(
            id=identity,
            engagement_id=project,
            title=identity,
            provider_profile_id="fixture-provider",
            model="fixture",
            created_at=at,
            updated_at=at,
        )
        for identity, project in [
            ("session", "project"),
            ("peer", "project"),
            ("foreign", "other"),
        ]
    ]
    messages = [
        ChatMessage(
            id=identity,
            engagement_id=project,
            session_id=session,
            role="user",
            sequence=sequence,
            content=content,
            created_at=at,
            updated_at=at,
        )
        for identity, project, session, sequence, content in [
            ("source", "project", "session", 1, "First line\nKeep β exact\nLast line"),
            ("latest", "project", "session", 5, "Review the docs"),
            ("foreign-message", "other", "foreign", 1, "private"),
        ]
    ]
    initial = [
        {"kind": record.entity_kind, "payload": record.model_dump(mode="json")}
        for record in [*sessions, *messages]
    ]
    actions = [
        {
            "method": "write",
            "session": "session",
            "id": "one",
            "body": {
                "expected_revision": 0,
                "text": "  Keep β exact  ",
                "kind": "question",
                "source_message_id": "source",
                "source_selection": "Keep β exact",
            },
        },
        {
            "method": "write",
            "session": "session",
            "id": "one",
            "body": {"expected_revision": 0, "text": "duplicate"},
        },
        {
            "method": "write",
            "session": "session",
            "id": "bad-selection",
            "body": {
                "expected_revision": 0,
                "text": "bad",
                "source_message_id": "source",
                "source_selection": "not present",
            },
        },
        {
            "method": "write",
            "session": "session",
            "id": "bad-source",
            "body": {
                "expected_revision": 0,
                "text": "bad",
                "source_message_id": "foreign-message",
            },
        },
        {
            "method": "write",
            "session": "session",
            "id": "latest-decision",
            "body": {"expected_revision": 0, "text": "Document defaults"},
        },
        {
            "method": "write",
            "session": "session",
            "id": "one",
            "body": {
                "expected_revision": 1,
                "text": "Changed question",
                "kind": "constraint",
            },
        },
        {
            "method": "write",
            "session": "session",
            "id": "one",
            "body": {"expected_revision": 1, "text": "stale"},
        },
        {
            "method": "write",
            "session": "foreign",
            "id": "one",
            "body": {"expected_revision": 2, "text": "cross-project"},
        },
        {
            "method": "write",
            "session": "peer",
            "id": "one",
            "body": {"expected_revision": 2, "text": "cross-session"},
        },
        {
            "method": "write",
            "session": "session",
            "id": "one",
            "body": {"expected_revision": 2, "action": "promote"},
        },
        {"method": "read", "session": "session"},
        {"method": "read", "session": "peer"},
        {"method": "read", "session": "foreign"},
        {"method": "snapshot", "session": "session", "project": "project"},
        {
            "method": "write",
            "session": "session",
            "id": "one",
            "body": {"expected_revision": 3, "text": "cannot restore"},
        },
        {
            "method": "write",
            "session": "peer",
            "id": "project-one",
            "body": {"expected_revision": 1, "action": "remove"},
        },
        {"method": "snapshot", "session": "session", "project": "project"},
        {
            "method": "write",
            "session": "session",
            "id": "latest-decision",
            "body": {"expected_revision": 1, "action": "supersede"},
        },
        {"method": "snapshot", "session": "session", "project": "project"},
        {
            "method": "cursor",
            "session": "session",
            "body": {
                "expected_revision": 0,
                "device_id": "phone",
                "through_at": "2020-01-01T00:00:00Z",
            },
        },
        {
            "method": "cursor",
            "session": "session",
            "body": {
                "expected_revision": 0,
                "device_id": "phone",
                "through_at": "2020-01-01T00:00:00Z",
            },
        },
        {
            "method": "cursor",
            "session": "session",
            "body": {
                "expected_revision": 1,
                "device_id": "phone",
                "through_at": "2020-01-02T03:00:00+02:00",
            },
        },
        {
            "method": "cursor",
            "session": "session",
            "body": {
                "expected_revision": 2,
                "device_id": "phone",
                "through_at": "2020-01-01T00:00:00Z",
            },
        },
        {
            "method": "cursor",
            "session": "session",
            "body": {
                "expected_revision": 2,
                "device_id": "phone",
                "through_at": "2099-01-01T00:00:00Z",
            },
        },
        {
            "method": "cursor",
            "session": "session",
            "body": {
                "expected_revision": 2,
                "device_id": "phone",
                "through_at": "2020-01-02T04:00:00",
            },
        },
        {
            "method": "cursor",
            "session": "session",
            "authenticated_device": "paired-device",
            "body": {
                "expected_revision": 0,
                "device_id": "untrusted-device",
                "through_at": "2020-01-01T00:00:00Z",
            },
        },
        {
            "method": "cursor",
            "session": "session",
            "authenticated_device": "paired-device",
            "body": {
                "expected_revision": 1,
                "device_id": "different-untrusted-device",
                "through_at": "2020-01-03T00:00:00Z",
            },
        },
    ]
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-context-oracle-"
    ) as directory:
        database = Database(Path(directory) / "nebula.db")
        try:
            store = NebulaStore(database)
            store.create_many([*sessions, *messages])
            app = FastAPI()

            @app.exception_handler(ConflictError)
            async def conflict(_request, exc):
                return JSONResponse(status_code=409, content={"detail": str(exc)})

            @app.exception_handler(NotFoundError)
            async def missing(_request, exc):
                return JSONResponse(status_code=404, content={"detail": str(exc)})

            @app.middleware("http")
            async def fixture_device(request: Request, call_next):
                request.state.auth_device_id = request.headers.get("x-fixture-device")
                return await call_next(request)

            app.include_router(decisions_router(store))
            app.include_router(catchup_router(store, None))
            with TestClient(app) as client:
                for action in actions:
                    base = f"/chat/sessions/{action['session']}"
                    if action["method"] == "snapshot":
                        result = decision_snapshot(
                            store, action["session"], action["project"]
                        )
                        action["expected"] = {"status": 200, "value": normalize(result)}
                        continue
                    if action["method"] == "read":
                        response = client.get(base + "/decisions")
                    elif action["method"] == "write":
                        response = client.put(
                            base + "/decisions/" + action["id"], json=action["body"]
                        )
                    else:
                        response = client.put(
                            base + "/read-cursor",
                            json=action["body"],
                            headers={
                                "x-fixture-device": action.get(
                                    "authenticated_device", ""
                                )
                            },
                        )
                    # Preserve all success fields. Error status is compared here;
                    # exact transport error envelopes remain an HTTP-layer gate.
                    action["expected"] = {"status": response.status_code}
                    if response.status_code == 200:
                        action["expected"]["value"] = normalize(response.json())
        finally:
            database.dispose()
    return {
        "format": "nebula.assistant-context-oracle/v1",
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in ("chat_catchup", "chat_decisions", "storage")
        },
        "initial": initial,
        "actions": actions,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = collect_context()
    args.output.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    print(
        json.dumps(
            {"actions": len(value["actions"]), "initial_records": len(value["initial"])}
        )
    )


if __name__ == "__main__":
    main()
