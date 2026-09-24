#!/usr/bin/env python3
"""Capture retained child views without starting or delivering agent work."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch
from urllib.parse import quote

from fastapi.testclient import TestClient
from keyring.backends.null import Keyring as NullKeyring
from nebula.v3.api import create_app
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import (
    Approval,
    ChatSession,
    ChatSubagent,
    ChatSubagentMessage,
    ChatTurn,
    Engagement,
)
from nebula.v3.storage import NebulaStore

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
ORIGIN = "https://nebula.test:9443"
MODELS = [ChatSession, ChatTurn, ChatSubagent, ChatSubagentMessage]


def envelope(record):
    return {"kind": record.entity_kind, "payload": record.model_dump(mode="json")}


def normalize(value):
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {
            k: "<generated>" if k in {"request_id", "error_id"} else normalize(v)
            for k, v in value.items()
        }
    return value


def initial_subagents():
    records, approvals, parents = [], [], []
    identities = set()

    def session(identity, parent=True, **fields):
        if identity in identities:
            return
        identities.add(identity)
        if parent:
            parents.append(identity)
        records.append(
            ChatSession(
                id=identity,
                engagement_id=fields.pop("engagement_id", "project"),
                title=identity,
                model=fields.pop("model", "fixture"),
                provider_profile_id=fields.pop(
                    "provider_profile_id", "fixture-provider"
                ),
                created_at=BASE,
                updated_at=BASE,
                **fields,
            )
        )

    def child(identity, parent, **fields):
        session(parent)
        status = fields.pop("status", "running")
        records.append(
            ChatSubagent(
                id=identity,
                engagement_id=fields.pop("engagement_id", "project"),
                parent_session_id=parent,
                parent_turn_id="parent-turn-retained",
                child_session_id=fields.pop(
                    "child_session_id", "missing-child-session"
                ),
                name=identity,
                task="Review retained local documentation",
                status=status,
                started_at=fields.pop("started_at", NOW - timedelta(seconds=10.25)),
                finished_at=fields.pop(
                    "finished_at",
                    None if status == "running" else NOW - timedelta(seconds=1),
                ),
                usage=fields.pop(
                    "usage", {"input_tokens": 2, "output_tokens": 3, "total_tokens": 99}
                ),
                created_at=BASE,
                updated_at=BASE,
                **fields,
            )
        )

    def turn(identity, **fields):
        records.append(
            ChatTurn(
                id=identity,
                engagement_id=fields.pop("engagement_id", "foreign-project"),
                session_id="unrelated-child-owner",
                model="retained-turn-model",
                provider_profile_id="retained-turn-provider",
                usage=fields.pop(
                    "usage", {"input_tokens": 5, "output_tokens": 7, "total_tokens": 8}
                ),
                next_step=fields.pop("next_step", 17),
                created_at=BASE,
                updated_at=BASE,
                **fields,
            )
        )

    def question(identity, owner, **fields):
        records.append(
            ChatSubagentMessage(
                id="message-" + identity,
                engagement_id="question-other-project",
                parent_session_id="other-parent",
                subagent_id=owner,
                direction=fields.pop("direction", "to_parent"),
                content="Question " + identity,
                expects_reply=fields.pop("expects_reply", True),
                awaiting_reply=fields.pop("awaiting_reply", True),
                created_at=fields.pop("created_at", BASE),
                updated_at=BASE + timedelta(days=1),
                **fields,
            )
        )

    session("empty")
    for state in ["running", "completed", "failed", "stopped", "interrupted"]:
        child(
            "child-" + state,
            "states",
            status=state,
            model="stored-model",
            provider_profile_id="stored-provider",
            reasoning_effort="high",
            rounds=3,
            result="Retained result",
            error="Retained error",
            reported_at=BASE,
            result_message_id="retained-result-message",
            pending_goal_charge_turn_id="unsettled-charge",
            parent_request={"opaque": [None, True]},
        )
    for state in [
        "routing",
        "waiting_callback",
        "finalizing",
        "complete",
        "failed",
        "cancelled",
        "interrupted",
    ]:
        parent = "turn-" + state
        turn(parent + "-record", status=state)
        child(parent + "-child", parent, child_turn_id=parent + "-record")
    child("child-missing-turn", "missing-turn", child_turn_id="no-turn")
    session("wrong-kind", parent=False)
    child("child-wrong-kind", "wrong-kind-turn", child_turn_id="wrong-kind")
    child("child-empty-turn", "empty-turn", child_turn_id="")
    child("child-model-fallback", "model-fallback", child_session_id="fallback-session")
    session(
        "fallback-session",
        parent=False,
        provider_profile_id="fallback-provider",
        model="fallback-model",
    )
    child(
        "child-empty-model",
        "empty-model",
        model="",
        child_session_id="fallback-session",
    )
    child(
        "child-known-model",
        "known-model",
        model="stored",
        provider_profile_id=None,
        child_session_id="fallback-session",
    )
    child(
        "child-cross-project",
        "cross-project",
        engagement_id="elsewhere",
        parent_backend="harness",
    )
    session(
        "archived",
        metadata={"archived_at": BASE.isoformat(), "temporary_assistant": True},
    )
    child("child-archived", "archived")
    child("child-future", "future", started_at=NOW + timedelta(seconds=5))
    child(
        "child-reversed-terminal",
        "reversed-terminal",
        status="stopped",
        started_at=NOW,
        finished_at=BASE,
    )
    for label, start, finish, status in [
        (
            "offset",
            NOW.astimezone(timezone(timedelta(hours=3))) - timedelta(seconds=1.123456),
            None,
            "running",
        ),
        ("naive-running", datetime(2030, 1, 1, 11), None, "running"),
        (
            "naive-terminal",
            datetime(2030, 1, 1, 11),
            datetime(2030, 1, 1, 11, 1, 2, 3),
            "completed",
        ),
        ("mixed-terminal", datetime(2030, 1, 1, 11), NOW, "completed"),
    ]:
        child(
            "child-" + label, label, status=status, started_at=start, finished_at=finish
        )
    child("order-b", "ordering", started_at=NOW - timedelta(seconds=2))
    child(
        "order-a",
        "ordering",
        started_at=(NOW - timedelta(seconds=2)).astimezone(
            timezone(timedelta(hours=-4))
        ),
    )
    child("order-z", "ordering", started_at=NOW - timedelta(seconds=3))
    child(
        "mixed-a",
        "mixed-ordering",
        status="completed",
        started_at=BASE,
        finished_at=NOW,
    )
    child(
        "mixed-b",
        "mixed-ordering",
        status="completed",
        started_at=BASE.replace(tzinfo=None),
        finished_at=NOW.replace(tzinfo=None),
    )

    recoveries = [
        ({"required": True, "cause": "core_shutdown"}, None),
        (
            {
                "required": True,
                "cause": "core_restart",
                "unknown_tool_call_ids": ["unknown"],
                "unknown_hook_execution_ids": ["unknown"],
                "auto_resume_attempted_at": "retained",
            },
            None,
        ),
        ({"required": True}, "Core stopped after a checkpoint"),
        ({"required": True, "cause": None}, "Core restarted after a checkpoint"),
        ({"required": True}, "Core stopped"),
        ({"required": 1, "cause": "core_restart"}, None),
        ({"required": "true", "cause": "core_restart"}, None),
        ({"required": False, "cause": "core_restart"}, None),
        ({"automatic_retry_pending": True, "cause": "core_restart"}, None),
        ({"required": True, "cause": "other"}, "Core stopped after a checkpoint"),
        ({}, None),
        (None, None),
        ([], None),
        ("invalid", None),
    ]
    for index, (recovery, error) in enumerate(recoveries):
        parent = f"recovery-{index}"
        turn(
            parent + "-turn",
            status="interrupted",
            request_snapshot={"recovery": recovery},
            error=error,
        )
        child(
            parent + "-child",
            parent,
            child_turn_id=parent + "-turn",
            result="Old result",
            error="Old error",
        )
    turn(
        "terminal-recovery-turn",
        status="interrupted",
        request_snapshot={"recovery": {"required": True, "cause": "core_restart"}},
        error="Recorded child recovery",
    )
    child(
        "terminal-recovery-child",
        "terminal-recovery",
        status="completed",
        child_turn_id="terminal-recovery-turn",
        result="Retained terminal result",
        error="Retained terminal error",
    )

    for status in ["pending", "approved", "edited", "rejected", "expired", "cancelled"]:
        identity = "approval-" + status
        approvals.append(
            Approval(
                id=identity,
                engagement_id="another-project",
                run_id="retained-owner",
                risk_class="local_read",
                exact_request={"fixture": True},
                policy_rationale="Retained decision rationale",
                requested_by="fixture",
                requested_at=BASE,
                expires_at=BASE + timedelta(days=1),
                status=status,
                decided_at=BASE if status != "pending" else None,
                created_at=BASE,
                updated_at=BASE,
            )
        )
    for identity in [
        "approval-pending",
        "approval-approved",
        "approval-edited",
        "approval-rejected",
        "approval-expired",
        "approval-cancelled",
        "missing-approval",
        "wrong-kind",
        "",
    ]:
        parent = "view-" + (identity or "empty-approval")
        turn(
            parent + "-turn",
            status="waiting_approval",
            approval_id=identity,
            tool_history=[
                {
                    "name": "fixture.read",
                    "status": "waiting_approval",
                    "arguments": {"path": " documentation.txt "},
                }
            ],
        )
        child(parent + "-child", parent, child_turn_id=parent + "-turn")
        question(parent + "-question", parent + "-child")
    turn("question-turn", status="routing")
    child("question-child", "questions", child_turn_id="question-turn")
    question("question-a", "question-child")
    question("question-b", "question-child", status="delivered", delivered_at=BASE)
    question("question-c", "question-child", status="undelivered")
    question(
        "question-z",
        "question-child",
        awaiting_reply=False,
        created_at=BASE + timedelta(seconds=1),
    )
    question(
        "to-child",
        "question-child",
        direction="to_child",
        expects_reply=False,
        awaiting_reply=False,
    )
    question("unrelated-question", "different-child")
    child("question-no-turn-child", "question-no-turn")
    question("question-no-turn", "question-no-turn-child")
    child(
        "question-terminal-child",
        "question-terminal",
        status="completed",
        child_turn_id="question-turn",
    )
    question("question-terminal", "question-terminal-child")

    opaque = [
        None,
        False,
        True,
        0,
        -7,
        10**30,
        1.0,
        -0.0,
        1e-5,
        1e16,
        "  retained  ",
        [],
        {},
        [True, None, "a'b"],
        {"z": "first", "a": [False, None, "\u00a0\u001c\n\t"]},
    ]
    for index in range(0, len(opaque), 3):
        parent = f"opaque-{index}"
        history = [
            {
                "name": "outside-last-four",
                "status": "skipped",
                "arguments": {"command": "invisible"},
            },
            {"name": "leading", "status": 0},
        ]
        history.extend(
            {"name": value, "status": value, "arguments": []}
            for value in opaque[index : index + 3]
        )
        turn(parent + "-turn", tool_history=history)
        child(parent + "-child", parent, child_turn_id=parent + "-turn")
    detail_arguments = [
        {"command": " \u001c echo\n safe\t documentation \u001f", "path": "ignored"},
        {"command": " \u001c", "path": " visible path ", "query": "ignored"},
        {
            "command": 1,
            "path": None,
            "query": "\u2003query\u2003text",
            "url": "ignored",
        },
        {"url": "https://example.invalid/local-fixture", "target": "ignored"},
        {"target": "local", "pattern": "ignored"},
        {"pattern": "pattern", "name": "ignored"},
        {"name": "name"},
        {"name": "🦀" * 120},
        {"name": "🦀" * 121},
        {"name": "x" * 118 + " " + "🦀" * 2},
        [],
        None,
        "ignored",
    ]
    for index in range(0, len(detail_arguments), 4):
        parent = f"details-{index}"
        turn(
            parent + "-turn",
            tool_history=[
                {"name": "fixture.read", "status": "complete", "arguments": args}
                for args in detail_arguments[index : index + 4]
            ],
        )
        child(parent + "-child", parent, child_turn_id=parent + "-turn")
    turn(
        "large-usage-turn",
        usage={
            "input_tokens": 10**30,
            "output_tokens": 10**30 + 1,
            "total_tokens": 10**30 + 7,
        },
    )
    child(
        "large-usage-child",
        "large-usage",
        child_turn_id="large-usage-turn",
        usage={
            "input_tokens": 10**30,
            "output_tokens": 10**30 + 9,
            "total_tokens": 10**30 + 4,
        },
    )
    projects = [
        Engagement(
            id="project", name="Retained child views", created_at=BASE, updated_at=BASE
        )
    ]
    return projects, records, approvals, parents


def subagent_cases(parents):
    cases = []

    def add(identity, name=None, **fields):
        cases.append(
            {
                "name": name or identity,
                "method": "GET",
                "path": "/api/v1/chat/sessions/"
                + quote(identity, safe="")
                + "/subagents",
                "body": None,
                "service": {"session_id": identity},
                **fields,
            }
        )

    for parent in [*parents, "missing", "child-running", "β" * 201]:
        add(parent)
    for identity in ["states", "questions", "ordering", "terminal-recovery"]:
        add(identity, identity + "-reopen", action="reopen")
    for identity in ["states", "missing"]:
        add(identity, identity + "-no-auth", service=None, auth={"headers": {}})
        add(
            identity,
            identity + "-wrong-auth",
            service=None,
            auth={"headers": {"Authorization": "Bearer incorrect"}},
        )
    return cases


def collect_subagents():
    projects, records, approvals, parents = initial_subagents()
    cases = subagent_cases(parents)
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-subagents-oracle-"
    ) as directory:
        path = Path(directory) / "nebula.db"
        database = Database(path)
        store = NebulaStore(database)
        for record in [*projects, *records, *approvals]:
            store.create(record)
        with sqlite3.connect(path) as raw:
            before = raw.execute("SELECT * FROM entities ORDER BY id").fetchall()
            raw_payloads = dict(
                raw.execute(
                    "SELECT id,payload FROM entities WHERE kind='chat_turns' ORDER BY id"
                )
            )

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
            with patch("nebula.v3.chat_subagents.utc_now", return_value=NOW):
                for case in cases:
                    if case.get("action") == "reopen":
                        client.close()
                        database.dispose()
                        database = Database(path, bootstrap=False)
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
                        case["method"], case["path"], headers=headers
                    )
                    case["expected"] = {
                        "status": response.status_code,
                        "body": normalize(response.json()),
                    }
                    case["expected_cache_control"] = response.headers.get(
                        "cache-control"
                    )
            final = [
                envelope(record)
                for model in MODELS
                for record in store.list_entities(
                    model, limit=1000, include_temporary=True
                )
            ]
            final_dependencies = [
                envelope(record) for record in store.list_entities(Approval, limit=1000)
            ]
            with sqlite3.connect(path) as raw:
                assert (
                    raw.execute("SELECT * FROM entities ORDER BY id").fetchall()
                    == before
                )
        finally:
            client.close()
            database.dispose()
    return {
        "format": "nebula.assistant-subagents-oracle/v1",
        "clock": NOW.isoformat(),
        "origin": ORIGIN,
        "normalization": "Only request_id/error_id; trusted observation clock is fixed. Preserve raw turn JSON for insertion-ordered opaque-history spelling.",
        "projects": [envelope(r) for r in projects],
        "initial_records": [envelope(r) for r in records],
        "dependency_records": [envelope(r) for r in approvals],
        "raw_payloads": raw_payloads,
        "cases": cases,
        "final_records": final,
        "final_dependencies": final_dependencies,
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in [
                "api",
                "chat_subagents",
                "domain",
                "storage",
                "relations",
                "diagnostic_guidance",
            ]
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = collect_subagents()
    args.output.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(fixture["cases"]),
                "records": len(fixture["initial_records"]),
                "sha256": sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
