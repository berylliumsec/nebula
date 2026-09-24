#!/usr/bin/env python3
"""Capture pure Assistant activity, saved queue and hook summaries in isolation.

No Core lifespan, queue runner, hook execution, provider, tool or ambient keyring.
Canonical retained records are authoritative; missing queues are response-only.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from urllib.parse import quote, urlencode

from fastapi.testclient import TestClient
from keyring.backends.null import Keyring as NullKeyring
from nebula.v3.api import create_app
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import (
    ChatQueue,
    ChatSession,
    ChatTurn,
    Engagement,
    NativeHookExecution,
)
from nebula.v3.storage import NebulaStore

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
ORIGIN = "https://nebula.test:9443"


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


@contextmanager
def queue_clock():
    # Freeze only response factories on this one model, restoring them even if
    # capture fails. Retained rows supply explicit timestamps and remain exact.
    fields = [ChatQueue.model_fields[name] for name in ["created_at", "updated_at"]]
    factories = [field.default_factory for field in fields]
    try:
        for field in fields:
            field.default_factory = lambda: NOW
        ChatQueue.model_rebuild(force=True)
        yield
    finally:
        for field, factory in zip(fields, factories, strict=True):
            field.default_factory = factory
        ChatQueue.model_rebuild(force=True)


def initial_status():
    records, hooks = [], []
    projects = [
        Engagement(
            id="project", name="Status fixture", created_at=BASE, updated_at=BASE
        )
    ]

    def session(identity, project="project", metadata=None, **fields):
        records.append(
            ChatSession(
                id=identity,
                engagement_id=project,
                title=identity,
                model="fixture",
                provider_profile_id="fixture-provider",
                metadata=metadata or {},
                created_at=BASE + timedelta(seconds=len(records) // 2),
                updated_at=BASE + timedelta(days=1),
                **fields,
            )
        )

    def turn(identity, session_id, status="routing", project="project", **fields):
        records.append(
            ChatTurn(
                id=identity,
                engagement_id=project,
                session_id=session_id,
                model="fixture",
                provider_profile_id="fixture-provider",
                status=status,
                created_at=BASE,
                updated_at=BASE,
                **fields,
            )
        )

    session("idle")
    session("archived", metadata={"archived_at": BASE.isoformat()})
    for status in [
        "routing",
        "waiting_approval",
        "waiting_callback",
        "finalizing",
        "complete",
        "failed",
        "cancelled",
    ]:
        session(status)
        turn("turn-" + status, status, status)
    for index, recovery in enumerate(
        [
            {},
            {"required": False},
            {"required": True},
            {"automatic_retry_pending": True},
            {"required": 0, "automatic_retry_pending": []},
            {"required": "false"},
            {"required": {}, "automatic_retry_pending": {"retained": True}},
        ]
    ):
        identity = f"interrupted-{index}"
        session(identity)
        turn(
            "turn-" + identity,
            identity,
            "interrupted",
            request_snapshot={"recovery": recovery},
        )
    for index, marker in enumerate(["child", "", None, False, 1, {}, []]):
        identity = f"child-{index}"
        session(identity, metadata={"subagent_id": marker})
        turn("turn-" + identity, identity, "waiting_approval")
    for index, marker in enumerate([True, False, None, 0, 1, "false", "", {}, []]):
        identity = f"temporary-{index}"
        session(identity, metadata={"temporary_assistant": marker})
        turn("turn-" + identity, identity)
    turn("orphan", "no-session")
    turn("foreign", "idle", project="foreign-project")
    for index, project in enumerate(["", "p" * 201]):
        session(f"historical-{index}", project)
        turn(
            f"historical-turn-{index}",
            f"historical-{index}",
            "interrupted",
            project,
            request_snapshot={"recovery": {"automatic_retry_pending": True}},
        )
    for label in ["visible", "hidden", "orphan"]:
        project = "conflict-" + label
        if label != "orphan":
            session(project, project, {"temporary_assistant": label == "hidden"})
        for suffix in ["a", "b"]:
            turn(project + "-" + suffix, project, project=project)
    for index, invalid in enumerate([None, 1, "required", []]):
        project = f"malformed-{index}"
        session(project, project)
        turn(
            "turn-" + project,
            project,
            "interrupted",
            project,
            request_snapshot={"recovery": invalid},
        )
    session("queue")
    session("scope-mismatch")
    session("wrong-queue-kind")
    session("chat-queue-wrong-queue-kind")
    session("s" * 189)
    session("s" * 190)
    records.append(
        ChatQueue(
            id="chat-queue-queue",
            engagement_id="project",
            session_id="queue",
            paused=True,
            resume_after_turn_id="retained-turn",
            revision=7,
            created_at=BASE,
            updated_at=BASE,
            items=[
                {"id": "one", "status": "complete", "retained": {"proof": True}},
                {"id": "two", "status": "uncertain", "opaque": [None, False]},
                {"id": "three", "status": "queued", "content": "Review retained notes"},
            ],
        )
    )
    records.append(
        ChatQueue(
            id="chat-queue-scope-mismatch",
            engagement_id="foreign",
            session_id="retained-foreign",
            created_at=BASE,
            updated_at=BASE,
        )
    )
    records.append(
        ChatQueue(
            id="noncanonical-queue",
            engagement_id="project",
            session_id="idle",
            created_at=BASE,
            updated_at=BASE,
        )
    )
    for identity in ["hooks", "naive-hooks", "mixed-hooks", "empty-hooks"]:
        turn(identity, "session-for-" + identity, "complete")
    for index, status in enumerate(
        ["running", "complete", "failed", "timed_out", "interrupted", "reconciled"]
    ):
        stamp = BASE + timedelta(seconds=10 - index)
        hooks.append(
            NativeHookExecution(
                id=f"hook-{index}",
                engagement_id="foreign" if index == 2 else "project",
                chat_session_id="session-for-hooks",
                chat_turn_id="hooks",
                owner_kind="api" if index == 3 else "chat",
                hook_id=f"fixture-{index}",
                hook_snapshot={"never": "execute this fixture"},
                event_name="Stop",
                status=status,
                side_effects=["none", "workspace", "external"][index % 3],
                started_at=stamp,
                completed_at=stamp + timedelta(seconds=1)
                if status != "running"
                else None,
                error="Retained failure" if status in {"failed", "timed_out"} else None,
                stdout="Fixture output must not appear in summary",
                stderr="Fixture stderr must not appear",
                late_outcome={
                    "status": "complete",
                    "exit_code": 0,
                    "observed_at": BASE + timedelta(days=1),
                    "stdout": "Private late output",
                }
                if status == "interrupted"
                else None,
                reconciliation={
                    "outcome": "complete",
                    "detail": "Recorded operator decision",
                    "opaque": None,
                }
                if status == "reconciled"
                else None,
                created_at=BASE,
                updated_at=BASE,
            )
        )
    for index, (identity, owner, stamp) in enumerate(
        [
            (
                "hook-offset-a",
                "hooks",
                datetime(2020, 1, 1, 3, tzinfo=timezone(timedelta(hours=3))),
            ),
            ("hook-offset-b", "hooks", BASE),
            ("hook-naive-a", "naive-hooks", datetime(2020, 1, 1, 0, 0, 1)),
            ("hook-naive-b", "naive-hooks", datetime(2020, 1, 1)),
            ("hook-mixed-a", "mixed-hooks", BASE),
            ("hook-mixed-b", "mixed-hooks", datetime(2020, 1, 1)),
            ("hook-other-turn", "other-turn", BASE),
        ]
    ):
        hooks.append(
            NativeHookExecution(
                id=identity,
                engagement_id="project",
                chat_session_id="session-for-"
                + ("hooks" if owner == "other-turn" else owner),
                chat_turn_id=owner,
                hook_id="fixture",
                hook_snapshot={},
                event_name="Stop",
                started_at=stamp,
                created_at=BASE,
                updated_at=BASE,
            )
        )
    hooks.append(
        NativeHookExecution(
            id="hook-wrong-session",
            engagement_id="project",
            chat_session_id="different",
            chat_turn_id="hooks",
            hook_id="fixture",
            hook_snapshot={},
            event_name="Stop",
            started_at=BASE,
            created_at=BASE,
            updated_at=BASE,
        )
    )
    return projects, records, hooks


def status_cases():
    cases = []

    def add(name, path, service=None, **fields):
        cases.append(
            {
                "name": name,
                "method": "GET",
                "path": path,
                "body": None,
                "service": service,
                **fields,
            }
        )

    for project in [
        "project",
        "foreign-project",
        "missing",
        "",
        "p" * 201,
        "conflict-visible",
        "conflict-hidden",
        "conflict-orphan",
        *[f"malformed-{i}" for i in range(4)],
    ]:
        add(
            "activity-" + str(len(cases)),
            "/api/v1/chat/session-activity?" + urlencode({"engagement_id": project}),
            {"kind": "activity", "project_id": project},
        )
    add("activity-required-query", "/api/v1/chat/session-activity")
    add(
        "activity-last-query-value",
        "/api/v1/chat/session-activity?engagement_id=missing&engagement_id=project&limit=bad",
        {"kind": "activity", "project_id": "project"},
    )
    for identity in [
        "idle",
        "queue",
        "scope-mismatch",
        "wrong-queue-kind",
        "missing",
        "hooks",
        "s" * 189,
        "s" * 190,
        "z" * 201,
    ]:
        add(
            "queue-" + str(len(cases)),
            "/api/v1/chat/sessions/" + quote(identity, safe="") + "/queue",
            {"kind": "queue", "session_id": identity},
        )
    add(
        "queue-repeat",
        "/api/v1/chat/sessions/idle/queue",
        {"kind": "queue", "session_id": "idle"},
    )
    for identity in [
        "hooks",
        "naive-hooks",
        "mixed-hooks",
        "empty-hooks",
        "missing",
        "idle",
        "z" * 201,
    ]:
        add(
            "hooks-" + str(len(cases)),
            "/api/v1/chat/turns/" + quote(identity, safe="") + "/hooks",
            {"kind": "hooks", "turn_id": identity},
        )
    for path in [
        "/api/v1/chat/session-activity?engagement_id=project",
        "/api/v1/chat/sessions/queue/queue",
        "/api/v1/chat/turns/hooks/hooks",
    ]:
        add("unauthorized-" + str(len(cases)), path, auth={"headers": {}})
        add(
            "wrong-token-" + str(len(cases)),
            path,
            auth={"headers": {"Authorization": "Bearer incorrect"}},
        )
    for name, path, service in [
        (
            "activity",
            "/api/v1/chat/session-activity?engagement_id=project",
            {"kind": "activity", "project_id": "project"},
        ),
        (
            "queue",
            "/api/v1/chat/sessions/queue/queue",
            {"kind": "queue", "session_id": "queue"},
        ),
        (
            "hooks",
            "/api/v1/chat/turns/hooks/hooks",
            {"kind": "hooks", "turn_id": "hooks"},
        ),
    ]:
        add(name + "-reopen", path, service, action="reopen")
    return cases


def collect_status():
    projects, records, hooks = initial_status()
    cases = status_cases()
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-status-oracle-"
    ) as directory:
        root = Path(directory)
        database = Database(root / "nebula.db")
        store = NebulaStore(database)
        for record in [*projects, *records, *hooks]:
            store.create(record)

        def client_for(current):
            return TestClient(
                create_app(
                    current,
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
            with queue_clock():
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
                        "body": normalize(response.json()),
                    }
            final = [
                envelope(record)
                for model in [ChatSession, ChatTurn, ChatQueue]
                for record in store.list_entities(
                    model, limit=1000, include_temporary=True
                )
            ]
            final_hooks = [
                envelope(record)
                for record in store.list_entities(NativeHookExecution, limit=1000)
            ]
        finally:
            client.close()
            database.dispose()
    return {
        "format": "nebula.assistant-status-oracle/v1",
        "clock": NOW.isoformat(),
        "fixed_response_clock": True,
        "origin": ORIGIN,
        "normalization": "Freeze only ephemeral queue timestamps; preserve all retained bytes. Normalize only request_id/error_id.",
        "projects": [envelope(r) for r in projects],
        "initial_records": [envelope(r) for r in records],
        "dependency_records": [envelope(r) for r in hooks],
        "dependency_schemas": {
            "native_hook_executions": NativeHookExecution.model_json_schema()
        },
        "cases": cases,
        "final_records": final,
        "final_dependencies": final_hooks,
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in [
                "api",
                "chat",
                "chat_queue",
                "chat_subagents",
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
    fixture = collect_status()
    args.output.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(fixture["cases"]),
                "records": len(fixture["initial_records"]),
                "dependencies": len(fixture["dependency_records"]),
                "sha256": sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
