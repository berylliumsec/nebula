#!/usr/bin/env python3
"""Capture generated Assistant catalog reads against an isolated Python API.

Journey: discover all saved conversations, select archived and child records,
refresh a stable page, and fetch canonical messages without transcript filtering.
The database owns identity, revisions and ordering. No application lifespan,
providers, tools or ambient keyring are started. This is HTTP compatibility
coverage; it does not establish production UI/browser journey acceptance.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from urllib.parse import quote, urlencode

from fastapi.testclient import TestClient
from keyring.backends.null import Keyring as NullKeyring
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import ChatMessage, ChatSession, Engagement
from nebula.v3.storage import NebulaStore

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2020, 1, 1, tzinfo=timezone.utc)


def envelope(record):
    return {"kind": record.entity_kind, "payload": record.model_dump(mode="json")}


def normalize(value):
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "<generated>" if key in {"request_id", "error_id"} else normalize(item)
            for key, item in value.items()
        }
    return value


def initial_catalog():
    project = Engagement(
        id="project", name="Catalog", created_at=STAMP, updated_at=STAMP
    )
    records = []
    sessions = [
        ("session", "project", {}, None),
        ("archived", "project", {"archived_at": STAMP.isoformat()}, None),
        ("child", "project", {"subagent_id": "child-link"}, "session"),
        ("orphan-child", "project", {"subagent_id": "orphan-link"}, "missing"),
        ("empty-marker-child", "project", {"subagent_id": ""}, "session"),
        ("foreign", "other-project", {}, None),
        ("empty-project", "", {}, None),
    ]
    sessions += [
        (f"temporary-{i}", "project", {"temporary_assistant": value}, None)
        for i, value in enumerate([True, False, None, 0, 1, "false", "", {}, []])
    ]
    for number, (identity, project_id, metadata, parent) in enumerate(sessions):
        # Deliberately reverse updated order; list order belongs to created_at/id.
        records.append(
            ChatSession(
                id=identity,
                engagement_id=project_id,
                title=f"Catalog {identity} β",
                provider_profile_id="fixture-provider",
                model="fixture-model",
                parent_session_id=parent,
                metadata=metadata,
                created_at=STAMP + timedelta(seconds=number // 2),
                updated_at=STAMP + timedelta(days=20, seconds=-number),
            )
        )
    for number, (identity, session, project_id, metadata) in enumerate(
        [
            ("message", "session", "project", {}),
            ("replaced", "session", "project", {"retracted_at": STAMP.isoformat()}),
            ("orphan", "missing", "project", {}),
            ("temporary-message", "temporary-0", "project", {}),
            ("foreign-message", "foreign", "other-project", {}),
            ("empty-project-message", "session", "", {}),
        ]
    ):
        records.append(
            ChatMessage(
                id=identity,
                engagement_id=project_id,
                session_id=session,
                sequence=20 - number,
                role="assistant",
                content=f"Canonical {identity} β",
                metadata=metadata,
                created_at=STAMP + timedelta(seconds=number // 2),
                updated_at=STAMP + timedelta(days=1, seconds=number),
            )
        )
    return [project], records


def catalog_cases():
    cases = []

    def add(name, path, params=None):
        cases.append(
            {
                "name": name,
                "method": "GET",
                "path": path + ("?" + urlencode(params) if params else ""),
                "body": None,
            }
        )

    for resource in ["chat-sessions", "chat-messages"]:
        path = f"/api/v1/{resource}"
        for name, params in [
            ("all", None),
            ("project", [("engagement_id", "project")]),
            ("foreign-project", [("engagement_id", "other-project")]),
            ("missing-project", [("engagement_id", "missing")]),
            ("empty-project", [("engagement_id", "")]),
            ("first-page", [("limit", 2)]),
            ("second-page", [("offset", 2), ("limit", 2)]),
            ("beyond-end", [("offset", 1000)]),
            (
                "ignored-filters",
                [
                    ("session_id", "missing"),
                    ("include_temporary", "true"),
                    ("archived", "false"),
                    ("newest_first", "true"),
                    ("status", "missing"),
                ],
            ),
            (
                "duplicate-project",
                [("engagement_id", "missing"), ("engagement_id", "project")],
            ),
            ("duplicate-limit", [("limit", "bad"), ("limit", 2)]),
            ("coerced-integer", [("offset", "+1.0"), ("limit", " 02 ")]),
            ("underscore-integer", [("limit", "1_0")]),
            ("limit-zero", [("limit", 0)]),
            ("limit-high", [("limit", 1001)]),
            ("limit-negative", [("limit", -1)]),
            ("limit-fraction", [("limit", "1.5")]),
            ("limit-bool", [("limit", "true")]),
            ("limit-empty", [("limit", "")]),
            ("offset-negative", [("offset", -1)]),
            ("offset-invalid", [("offset", "bad")]),
            ("multiple-errors", [("offset", -1), ("limit", 0)]),
        ]:
            add(f"{resource}-{name}", path, params)
    for resource, ids in [
        (
            "chat-sessions",
            [
                "session",
                "archived",
                "child",
                "orphan-child",
                "temporary-0",
                "temporary-5",
                "missing",
                "message",
                "β" * 201,
            ],
        ),
        (
            "chat-messages",
            [
                "message",
                "replaced",
                "orphan",
                "temporary-message",
                "foreign-message",
                "missing",
                "session",
                "β" * 201,
            ],
        ),
    ]:
        for number, identity in enumerate(ids):
            add(
                f"{resource}-get-{number}",
                f"/api/v1/{resource}/{quote(identity, safe='')}",
            )
        add(
            f"{resource}-get-ignored-query",
            f"/api/v1/{resource}/{ids[0]}",
            [("engagement_id", "missing"), ("limit", "bad")],
        )
    add(
        "chat-sessions-overrange-offset-invalid-limit",
        "/api/v1/chat-sessions",
        [("offset", 9223372036854775808), ("limit", 0)],
    )
    return cases


def collect_catalog():
    projects, records = initial_catalog()
    cases = catalog_cases()
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-catalog-oracle-"
    ) as directory:
        root = Path(directory)
        database = Database(root / "nebula.db")
        try:
            store = NebulaStore(database)
            for record in [*projects, *records]:
                store.create(record)
            app = create_app(
                store,
                artifact_store=ArtifactStore(root / "artifacts"),
                credential_store=CredentialStore(keyring_backend=NullKeyring()),
                auth_token="fixture-core",
                enable_executable_missions=False,
                bootstrap_workspace=False,
            )
            client = TestClient(app, raise_server_exceptions=False)
            try:
                for case in cases:
                    response = client.get(
                        case["path"],
                        headers={
                            "Authorization": "Bearer fixture-core",
                            "X-Nebula-Operation-ID": "fixture-operation",
                        },
                    )
                    case["expected"] = {
                        "status": response.status_code,
                        "body": normalize(response.json()),
                    }
            finally:
                client.close()
            final = [
                envelope(record)
                for model in [ChatSession, ChatMessage]
                for record in store.list_entities(
                    model, limit=1000, include_temporary=True
                )
            ]
        finally:
            database.dispose()
    return {
        "format": "nebula.assistant-catalog-oracle/v1",
        "projects": [envelope(p) for p in projects],
        "initial_records": [envelope(r) for r in records],
        "cases": cases,
        "final_records": final,
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in ["api", "domain", "storage", "relations", "diagnostic_guidance"]
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = collect_catalog()
    args.output.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(fixture["cases"]),
                "initial_records": len(fixture["initial_records"]),
            }
        )
    )


if __name__ == "__main__":
    main()
