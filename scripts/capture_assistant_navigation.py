#!/usr/bin/env python3
"""Capture Assistant history, search and bookmark compatibility from Python.

Journey: select a saved conversation, read its canonical transcript, search its
project, save/remove a bookmark, then reload and retry a stale mutation. Core's
database owns messages, replacement markers and bookmark revisions; projections
must retain scope, order, literal search and stored-row pagination semantics.
This oracle exercises the real HTTP routes over an isolated database without
entering application lifespan, invoking providers, or starting tools. It is API
compatibility evidence; production UI, browser and reconnect gates remain separate.
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
from nebula.v3.chat_workspace import bookmark_id
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import ChatBookmark, ChatMessage, ChatSession, Engagement
from nebula.v3.storage import NebulaStore

ROOT = Path(__file__).resolve().parents[1]
BASE_TIME = datetime(2020, 1, 1, tzinfo=timezone.utc)


def envelope(record):
    return {"kind": record.entity_kind, "payload": record.model_dump(mode="json")}


def initial_records():
    """Stable canonical records, including deliberately unusual legacy metadata."""
    projects = [
        Engagement(id=identity, name=name, created_at=BASE_TIME, updated_at=BASE_TIME)
        for identity, name in [("project", "Research"), ("other-project", "Other")]
    ]
    sessions = [
        ChatSession(
            id=identity,
            engagement_id=project,
            title=title,
            provider_profile_id="fixture-provider",
            model="fixture-model",
            metadata=metadata,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
        for identity, project, title, metadata in [
            ("session", "project", "Saved conversation β", {}),
            ("peer", "project", "Another conversation", {}),
            ("empty", "project", "Empty conversation", {}),
            (
                "temporary",
                "project",
                "Temporary conversation",
                {"temporary_assistant": True},
            ),
            ("foreign", "other-project", "Other project's conversation", {}),
        ]
    ]
    specs = [
        (
            "a-replaced",
            "session",
            "project",
            1,
            "old searchable text",
            {"retracted_at": "2020-01-02T00:00:00Z"},
        ),
        (
            "b-literal",
            "session",
            "project",
            2,
            "literal 100% under_score slash / a/b plus +",
            {},
        ),
        (
            "c-hidden",
            "session",
            "project",
            3,
            "hidden metadata remains canonical",
            {"hidden": True},
        ),
        (
            "d-casefold",
            "session",
            "project",
            4,
            "ß" * 100 + "marker " + "tail 🦀 " * 80,
            {},
        ),
        (
            "e-unicode",
            "session",
            "project",
            5,
            "🦀" * 100 + "Straße STRASSE β BETA café CAFÉ " + "🌙" * 430,
            {},
        ),
        ("f-tie-a", "session", "project", 6, "tie alpha", {}),
        ("g-tie-b", "session", "project", 6, "tie beta", {}),
        ("h-late-sequence", "session", "project", 1, "late stored first sequence", {}),
        ("i-peer", "peer", "project", 1, "searchable peer", {}),
        ("j-temporary", "temporary", "project", 1, "searchable temporary", {}),
        ("k-foreign", "foreign", "other-project", 1, "searchable foreign", {}),
        ("l-orphan", "missing-session", "project", 1, "searchable orphan", {}),
        (
            "m-mixed-project",
            "session",
            "other-project",
            7,
            "legacy mixed project row",
            {},
        ),
    ]
    for number, value in enumerate([False, None, [], {}, 0, "", True, [0], {"x": 0}]):
        specs.append(
            (
                f"truth-{number}",
                "session",
                "project",
                10 + number,
                f"replacement truthiness {number}",
                {"retracted_at": value},
            )
        )
    messages = []
    for number, (identity, session, project, sequence, content, metadata) in enumerate(
        specs
    ):
        stamp = BASE_TIME + timedelta(
            minutes=6 if identity == "g-tie-b" else number + 1
        )
        messages.append(
            ChatMessage(
                id=identity,
                engagement_id=project,
                session_id=session,
                sequence=sequence,
                role="user" if number % 2 else "assistant",
                content=content,
                metadata=metadata,
                created_at=stamp,
                updated_at=stamp,
            )
        )
    marks = [
        ChatBookmark(
            id=bookmark_id("session", message),
            engagement_id="project",
            session_id="session",
            message_id=message,
            active=active,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
        for message, active in [("b-literal", True), ("a-replaced", False)]
    ]
    return projects, [*sessions, *messages, *marks]


def navigation_cases():
    cases = []

    def add(name, path, *, method="GET", body=None, params=None):
        cases.append(
            {
                "name": name,
                "method": method,
                "path": path + ("?" + urlencode(params) if params else ""),
                "body": body,
            }
        )

    search = "/api/v1/chat/projects/project/search"
    messages = "/api/v1/chat/sessions/session/messages"
    marks = "/api/v1/chat/sessions/session/bookmarks"
    for name, params in [
        ("search-all", []),
        ("search-empty-trim", [("q", " \t\n")]),
        ("search-python-whitespace", [("q", "\u001c")]),
        ("search-literal-percent", [("q", "%")]),
        ("search-literal-underscore", [("q", "_")]),
        ("search-literal-slash", [("q", "/")]),
        ("search-literal-path", [("q", "a/b")]),
        ("search-encoded-plus", [("q", "+")]),
        ("search-casefold-excerpt", [("q", "MARKER")]),
        ("search-astral-excerpt", [("q", "STRASSE")]),
        ("search-unicode-lower", [("q", "straße")]),
        ("search-unicode-uppercase-not-folded", [("q", "CAFÉ")]),
        ("search-replaced-first-page", [("limit", 1)]),
        ("search-page-two", [("limit", 1), ("offset", 1)]),
        ("search-two-stored-rows", [("limit", 2)]),
        ("search-end-page", [("offset", 20), ("limit", 2)]),
        ("search-offset-beyond-end", [("offset", 500)]),
        ("search-session", [("session_id", "session")]),
        ("search-peer", [("session_id", "peer")]),
        ("search-empty-session-filter", [("session_id", "")]),
        ("search-temporary-excluded", [("session_id", "temporary")]),
        ("search-missing-session", [("session_id", "missing")]),
        ("search-foreign-session", [("session_id", "foreign")]),
        ("search-bookmarked", [("bookmarked", "true")]),
        ("search-duplicate-q-last-wins", [("q", "absent"), ("q", "%")]),
        ("search-duplicate-limit-last-wins", [("limit", 1), ("limit", 2)]),
        (
            "search-duplicate-bool-last-wins",
            [("bookmarked", "true"), ("bookmarked", "false")],
        ),
    ]:
        add(name, search, params=params)
    add("search-other-project", "/api/v1/chat/projects/other-project/search")
    add("search-missing-project", "/api/v1/chat/projects/missing/search")
    add("search-invalid-utf8", search + "?q=%FF")
    add("search-plus-as-space", search + "?q=+")
    add("query-invalid-utf8-bool", search + "?bookmarked=%FF")
    for name, params in [
        ("messages-current", []),
        ("messages-history", [("include_replaced", "true")]),
        ("messages-boolean-one", [("include_replaced", "1")]),
        ("messages-boolean-on", [("include_replaced", "ON")]),
        ("messages-boolean-off", [("include_replaced", "off")]),
        (
            "messages-duplicate-last-wins",
            [("include_replaced", "true"), ("include_replaced", "false")],
        ),
        ("messages-invalid-bool", [("include_replaced", "invalid")]),
        ("messages-blank-bool", [("include_replaced", "")]),
    ]:
        add(name, messages, params=params)
    for identity in ["missing", "empty", "temporary", "peer", "foreign"]:
        add(f"messages-{identity}", f"/api/v1/chat/sessions/{identity}/messages")
    for name, params in [
        ("query-q-too-long", [("q", "🦀" * 513)]),
        ("query-limit-zero", [("limit", 0)]),
        ("query-limit-high", [("limit", 101)]),
        ("query-limit-negative", [("limit", -1)]),
        ("query-limit-invalid", [("limit", "abc")]),
        ("query-limit-fraction", [("limit", "1.5")]),
        ("query-limit-integer-float", [("limit", "1.0")]),
        ("query-limit-padded", [("limit", " +2 ")]),
        ("query-limit-underscore", [("limit", "1_0")]),
        ("query-offset-negative", [("offset", -1)]),
        ("query-offset-invalid", [("offset", "bad")]),
        ("query-offset-fraction", [("offset", "1.5")]),
        ("query-offset-integer-float", [("offset", "1.0")]),
        ("query-invalid-bool", [("bookmarked", "2")]),
        ("query-blank-bool", [("bookmarked", "")]),
        ("query-many-invalid", [("bookmarked", "bad"), ("offset", -1), ("limit", 0)]),
    ]:
        add(name, search, params=params)
    add("bookmarks-initial", marks)
    add("bookmarks-missing-session", "/api/v1/chat/sessions/missing/bookmarks")
    add("bookmarks-empty", "/api/v1/chat/sessions/empty/bookmarks")
    for name, message, body in [
        ("bookmark-create", "c-hidden", {"active": True, "expected_revision": 0}),
        (
            "bookmark-duplicate-create",
            "c-hidden",
            {"active": True, "expected_revision": 0},
        ),
        ("bookmark-stale", "c-hidden", {"active": False, "expected_revision": 8}),
        ("bookmark-disable", "c-hidden", {"active": False, "expected_revision": 1}),
    ]:
        add(name, f"{marks}/{message}", method="PUT", body=body)
    add("bookmarks-reload-inactive", marks)
    add("search-bookmarks-inactive", search, params=[("bookmarked", "true")])
    add(
        "bookmark-enable",
        f"{marks}/c-hidden",
        method="PUT",
        body={"active": True, "expected_revision": 2},
    )
    add("search-bookmarks-active", search, params=[("bookmarked", "yes")])
    for name, path, body in [
        (
            "bookmark-missing-session",
            "/api/v1/chat/sessions/missing/bookmarks/b-literal",
            {"active": True, "expected_revision": 0},
        ),
        (
            "bookmark-missing-message",
            f"{marks}/missing",
            {"active": True, "expected_revision": 0},
        ),
        (
            "bookmark-other-session",
            f"{marks}/i-peer",
            {"active": True, "expected_revision": 0},
        ),
        (
            "bookmark-other-project",
            f"{marks}/m-mixed-project",
            {"active": True, "expected_revision": 0},
        ),
        (
            "bookmark-update-absent",
            f"{marks}/d-casefold",
            {"active": True, "expected_revision": 1},
        ),
        (
            "bookmark-activate-replaced",
            f"{marks}/a-replaced",
            {"active": True, "expected_revision": 1},
        ),
    ]:
        add(name, path, method="PUT", body=body)
    for name, body in [
        ("body-null", None),
        ("body-array", []),
        ("body-string", "bookmark"),
        ("body-empty", {}),
        ("body-missing-active", {"expected_revision": 0}),
        ("body-missing-revision", {"active": True}),
        ("body-invalid-active", {"active": "maybe", "expected_revision": 0}),
        ("body-null-active", {"active": None, "expected_revision": 0}),
        ("body-list-active", {"active": [], "expected_revision": 0}),
        ("body-fraction-active", {"active": 0.5, "expected_revision": 0}),
        ("body-two-active", {"active": 2, "expected_revision": 0}),
        ("body-padded-active", {"active": "  true ", "expected_revision": 0}),
        ("body-negative-revision", {"active": True, "expected_revision": -1}),
        ("body-fraction-revision", {"active": True, "expected_revision": 1.5}),
        ("body-bad-revision", {"active": True, "expected_revision": "x"}),
        ("body-many-invalid", {"active": {}, "expected_revision": -1}),
    ]:
        add(name, f"{marks}/e-unicode", method="PUT", body=body)
    for revision, active in enumerate(
        [0, 1, "yes", "OFF", "True", "0", 0.0, 1.0], start=1
    ):
        add(
            f"bookmark-coerce-bool-{revision}",
            f"{marks}/b-literal",
            method="PUT",
            body={"active": active, "expected_revision": revision},
        )
    add(
        "bookmark-coerce-revision",
        f"{marks}/b-literal",
        method="PUT",
        body={"active": True, "expected_revision": " +9.0 ", "ignored": 1},
    )
    add(
        "bookmark-huge-stale-revision",
        f"{marks}/b-literal",
        method="PUT",
        body={"active": True, "expected_revision": 10**30},
    )
    add("bookmarks-final-reload", marks)
    add("search-final-bookmarks", search, params=[("bookmarked", "true")])
    overlong = "x" * 201
    overlong_unicode = quote("β" * 201, safe="")
    add(
        "messages-overlong-missing-session",
        f"/api/v1/chat/sessions/{overlong_unicode}/messages",
    )
    add(
        "bookmarks-overlong-missing-session",
        f"/api/v1/chat/sessions/{overlong}/bookmarks",
    )
    add(
        "search-overlong-missing-project",
        f"/api/v1/chat/projects/{overlong}/search",
    )
    add(
        "search-overlong-missing-session",
        search,
        params=[("session_id", overlong)],
    )
    add(
        "bookmark-overlong-missing-message",
        f"{marks}/{overlong}",
        method="PUT",
        body={"active": True, "expected_revision": 0},
    )
    add(
        "bookmark-overlong-missing-session",
        f"/api/v1/chat/sessions/{overlong}/bookmarks/b-literal",
        method="PUT",
        body={"active": True, "expected_revision": 0},
    )
    return cases


def normalize(value, fixed_timestamps):
    if isinstance(value, list):
        return [normalize(item, fixed_timestamps) for item in value]
    if isinstance(value, dict):
        return {
            key: "<generated>"
            if key in {"request_id", "error_id"}
            or (key in {"created_at", "updated_at"} and item not in fixed_timestamps)
            else normalize(item, fixed_timestamps)
            for key, item in value.items()
        }
    return value


def collect_navigation():
    projects, initial = initial_records()
    project_envelopes = [envelope(item) for item in projects]
    initial_envelopes = [envelope(item) for item in initial]
    fixed_timestamps = {
        item["payload"][key]
        for item in [*project_envelopes, *initial_envelopes]
        for key in ["created_at", "updated_at"]
    }
    # Pydantic entity responses use Z while FastAPI's plain-dict datetime
    # encoder uses +00:00. Both represent fixture timestamps; retain each exact
    # spelling so the oracle detects transport serialization differences.
    fixed_timestamps.update(
        datetime.fromisoformat(value).isoformat() for value in tuple(fixed_timestamps)
    )
    cases = navigation_cases()
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-navigation-oracle-"
    ) as directory:
        root = Path(directory)
        database = Database(root / "nebula.db")
        try:
            store = NebulaStore(database)
            for record in [*projects, *initial]:
                store.create(record)
            app = create_app(
                store,
                artifact_store=ArtifactStore(root / "artifacts"),
                # Supplying a backend prevents ambient keyring discovery. These
                # read/bookmark routes never resolve provider credentials.
                credential_store=CredentialStore(keyring_backend=NullKeyring()),
                auth_token="fixture-core",
                enable_executable_missions=False,
                bootstrap_workspace=False,
            )
            client = TestClient(app, raise_server_exceptions=False)
            try:
                for case in cases:
                    response = client.request(
                        case["method"],
                        case["path"],
                        json=case["body"],
                        headers={
                            "Authorization": "Bearer fixture-core",
                            "Content-Type": "application/json",
                            "X-Nebula-Operation-ID": "fixture-operation",
                        },
                    )
                    case["expected"] = {
                        "status": response.status_code,
                        "body": normalize(response.json(), fixed_timestamps),
                    }
            finally:
                client.close()
            final = [
                normalize(envelope(record), fixed_timestamps)
                for model in [ChatSession, ChatMessage, ChatBookmark]
                for record in store.list_entities(
                    model, limit=1000, include_temporary=True
                )
            ]
        finally:
            database.dispose()
    return {
        "format": "nebula.assistant-navigation-oracle/v1",
        "normalization": "Preserve initial timestamp values and their datetime.isoformat() spellings; replace generated created_at/updated_at and all request_id/error_id values with <generated>.",
        "headers": {
            "Authorization": "Bearer fixture-core",
            "X-Nebula-Operation-ID": "fixture-operation",
        },
        "projects": project_envelopes,
        "initial_records": initial_envelopes,
        "cases": cases,
        "final_records": final,
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in [
                "api",
                "chat",
                "chat_workspace",
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
    fixture = collect_navigation()
    args.output.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(fixture["cases"]),
                "initial_records": len(fixture["initial_records"]),
                "final_records": len(fixture["final_records"]),
            }
        )
    )


if __name__ == "__main__":
    main()
