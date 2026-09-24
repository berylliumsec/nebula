#!/usr/bin/env python3
"""Capture retained Assistant goal/schedule/catalog reads without execution.

All state is isolated. No Core lifespan, workers, providers, tools or keyring.
A trusted fixed clock affects presentation only; durable records stay unchanged.
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
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalUsageCharge,
    ChatSchedule,
    ChatSession,
    ChatSubagent,
    Engagement,
)
from nebula.v3.storage import NebulaStore

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
MODELS = [ChatSession, ChatGoal, ChatGoalUsageCharge, ChatSchedule, ChatSubagent]
RESOURCES = {
    "chat-goals": ChatGoal,
    "chat-goal-usage-charges": ChatGoalUsageCharge,
    "chat-schedules": ChatSchedule,
    "chat-subagents": ChatSubagent,
}
ORIGIN = "https://nebula.test:9443"


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


def initial_plans():
    records = []
    projects = [
        Engagement(
            id="project", name="Retained plans", created_at=BASE, updated_at=BASE
        )
    ]

    def session(identity, **fields):
        backend = fields.pop("backend", "provider")
        records.append(
            ChatSession(
                id=identity,
                engagement_id=fields.pop("engagement_id", "project"),
                title=identity,
                backend=backend,
                model="fixture",
                provider_profile_id="fixture-provider"
                if backend == "provider"
                else None,
                harness_profile_id="fixture-harness" if backend == "harness" else None,
                harness_session_id="fixture-harness-session"
                if backend == "harness"
                else None,
                created_at=BASE,
                updated_at=BASE,
                **fields,
            )
        )

    def goal(identity, session_id, **fields):
        records.append(
            ChatGoal(
                id=identity,
                engagement_id=fields.pop("engagement_id", "project"),
                session_id=session_id,
                objective="Review retained documentation",
                completion_criteria=["Recorded review exists"],
                created_at=fields.pop("created_at", BASE),
                updated_at=fields.pop("updated_at", BASE + timedelta(days=1)),
                **fields,
            )
        )

    for state in ["draft", "running", "paused", "blocked", "completed", "cancelled"]:
        session(state)
        fields = {
            "status": state,
            "elapsed_seconds": 4.25,
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 99},
            "metadata": {"opaque": [None, False, {"text": "  retained  "}]},
            "plan": ["Read", "Summarize"],
            "current_step": 7,
            "step_budget": 9,
            "child_budget": 100,
            "skill_snapshots": [{"id": "historical", "snapshot": {"unknown": True}}],
        }
        if state == "running":
            fields.update(
                active_since=NOW - timedelta(seconds=12, microseconds=345678),
                time_budget_seconds=1,
                execution_owner_id="recorded-owner",
                execution_claim_id="recorded-claim",
                execution_claimed_at=BASE,
            )
        if state == "paused":
            fields.update(active_since=BASE, paused_at=BASE + timedelta(hours=1))
        if state == "blocked":
            fields["blocked_reason"] = "Review recorded evidence"
        if state == "completed":
            fields.update(
                completion_summary="Recorded summary",
                completion_evidence=[{"source": "fixture", "verified": False}],
                completed_at=BASE + timedelta(hours=1),
            )
        goal("goal-" + state, state, **fields)
    for identity, active in [
        ("null", None),
        ("equal", NOW),
        ("future", NOW + timedelta(seconds=10)),
        (
            "offset",
            (NOW - timedelta(seconds=30)).astimezone(timezone(timedelta(hours=-5))),
        ),
        ("naive", datetime(2030, 1, 1, 11, 59, 30)),
    ]:
        session("running-" + identity)
        goal(
            "goal-running-" + identity,
            "running-" + identity,
            status="running",
            active_since=active,
            elapsed_seconds=7.5,
        )
    session("paused-naive")
    goal(
        "goal-paused-naive",
        "paused-naive",
        status="paused",
        active_since=datetime(2030, 1, 1),
        elapsed_seconds=9.125,
    )
    for identity, fields in [
        ("empty", {}),
        ("duplicate", {}),
        ("harness", {"backend": "harness"}),
        ("temporary", {"metadata": {"temporary_assistant": True}}),
        ("archived", {"metadata": {"archived_at": BASE.isoformat()}}),
        ("mismatch", {}),
        ("parent", {}),
        ("self-parent", {}),
        ("schedule", {}),
        ("schedule-duplicates", {}),
        ("schedule-harness", {"backend": "harness"}),
    ]:
        session(identity, **fields)
    for identity in ["harness", "temporary", "archived", "mismatch"]:
        goal(
            "goal-" + identity,
            identity,
            engagement_id="foreign-project" if identity == "mismatch" else "project",
        )
    for suffix in ["b", "a"]:
        goal("duplicate-" + suffix, "duplicate")
    goal("parent-goal", "parent", child_session_ids=["listed-but-unrelated"])
    goal("self-goal", "self-parent", parent_goal_id="self-goal")
    for index, (identity, parent, project, status) in enumerate(
        [
            ("child-z", "parent-goal", "foreign-project", "running"),
            ("child-a", "parent-goal", "project", "draft"),
            ("grandchild", "child-a", "project", "draft"),
            ("listed-but-unrelated", "missing-parent-goal", "project", "draft"),
            ("temporary-child", "parent-goal", "project", "draft"),
        ]
    ):
        if identity == "temporary-child":
            session("child-session-" + identity, metadata={"temporary_assistant": True})
        goal(
            identity,
            "child-session-" + identity,
            parent_goal_id=parent,
            engagement_id=project,
            status=status,
            active_since=BASE if status == "running" else None,
            elapsed_seconds=2.25,
            created_at=BASE + timedelta(seconds=index // 2),
        )
    for number, project in enumerate(["", "p" * 201, "foreign-project"]):
        identity = f"legacy-{number}"
        session(identity, engagement_id=project)
        goal("goal-" + identity, identity, engagement_id=project)
    schedules = [
        (
            "schedule-main",
            "schedule",
            "project",
            True,
            None,
            NOW - timedelta(days=1),
            NOW - timedelta(days=2),
        ),
        (
            "schedule-old-b",
            "schedule-duplicates",
            "project",
            False,
            "archive",
            NOW + timedelta(days=1),
            None,
        ),
        (
            "schedule-old-a",
            "schedule-duplicates",
            "other-project",
            True,
            None,
            NOW + timedelta(days=2),
            None,
        ),
        (
            "schedule-new",
            "schedule-duplicates",
            "project",
            True,
            None,
            NOW - timedelta(days=3),
            None,
        ),
        (
            "schedule-harness-record",
            "schedule-harness",
            "project",
            False,
            None,
            NOW,
            None,
        ),
        (
            "schedule-orphan",
            "missing-session",
            "foreign-project",
            True,
            None,
            NOW,
            None,
        ),
        ("schedule-empty-project", "legacy-0", "", True, None, NOW, None),
    ]
    for index, (
        identity,
        owner,
        project,
        enabled,
        paused,
        next_run,
        last_run,
    ) in enumerate(schedules):
        records.append(
            ChatSchedule(
                id=identity,
                engagement_id=project,
                session_id=owner,
                provider_profile_id="removed-provider",
                model="retained-model",
                interval_seconds=3600,
                next_run_at=next_run,
                enabled=enabled,
                paused_by=paused,
                last_run_at=last_run,
                last_turn_id="retained-turn" if last_run else None,
                last_status="skipped" if index == 0 else None,
                skip_reason="Retained reason" if index == 0 else None,
                created_at=BASE
                + timedelta(seconds=1 if identity == "schedule-new" else 0),
                updated_at=BASE + timedelta(days=1),
            )
        )
    for index, project in enumerate(["project", "foreign-project", "", "p" * 201]):
        records.append(
            ChatGoalUsageCharge(
                id=f"charge-{index}",
                engagement_id=project,
                goal_id="missing-goal",
                subagent_id="missing-subagent",
                child_turn_id="missing-turn",
                usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 99},
                created_at=BASE + timedelta(seconds=index // 2),
                updated_at=BASE + timedelta(days=1),
            )
        )
        records.append(
            ChatSubagent(
                id=f"subagent-{index}",
                engagement_id=project,
                parent_session_id="missing-parent",
                parent_turn_id="missing-turn",
                child_session_id="missing-child",
                name="Retained child",
                task="Review fixture notes",
                status="running" if index == 0 else "completed",
                started_at=BASE,
                finished_at=None if index == 0 else BASE + timedelta(minutes=1),
                result="" if index == 0 else "Retained result",
                pending_goal_charge_turn_id=None if index == 0 else "unsettled-receipt",
                parent_request={"opaque": {"do_not_run": True}},
                created_at=BASE + timedelta(seconds=index // 2),
                updated_at=BASE + timedelta(days=1),
            )
        )
    return projects, records


def plan_cases(records):
    cases = []

    def add(name, path, service=None, params=None, **fields):
        cases.append(
            {
                "name": name,
                "method": "GET",
                "path": path + ("?" + urlencode(params) if params else ""),
                "body": None,
                "service": service,
                **fields,
            }
        )

    identities = [r.id for r in records if isinstance(r, ChatSession)]
    for identity in identities + ["missing", "goal-draft", "β" * 201]:
        add(
            "goal-" + identity[:30],
            f"/api/v1/chat/sessions/{quote(identity, safe='')}/goal",
            {"kind": "goal", "session_id": identity},
        )
    for identity in [
        "parent",
        "self-parent",
        "empty",
        "duplicate",
        "missing",
        "goal-draft",
        "running-naive",
        "harness",
        "β" * 201,
    ]:
        add(
            "children-" + identity[:30],
            f"/api/v1/chat/sessions/{quote(identity, safe='')}/goal/children",
            {"kind": "children", "session_id": identity},
        )
    for identity in [
        "schedule",
        "schedule-duplicates",
        "schedule-harness",
        "legacy-0",
        "empty",
        "missing",
        "goal-draft",
        "β" * 201,
    ]:
        add(
            "schedule-" + identity[:30],
            f"/api/v1/chat/sessions/{quote(identity, safe='')}/schedule",
            {"kind": "schedule", "session_id": identity},
        )
    for resource, model in RESOURCES.items():
        examples = [r for r in records if isinstance(r, model)]
        for label, project, offset, limit in [
            ("all", None, 0, 100),
            ("project", "project", 0, 100),
            ("foreign", "foreign-project", 0, 100),
            ("empty-project", "", 0, 100),
            ("long-project", "p" * 201, 0, 100),
            ("missing", "missing", 0, 100),
            ("first", None, 0, 2),
            ("second", None, 2, 2),
            ("tail", None, 1000, 100),
            ("limit1000", None, 0, 1000),
        ]:
            params = [("offset", offset), ("limit", limit)]
            if project is not None:
                params.append(("engagement_id", project))
            add(
                resource + "-" + label,
                "/api/v1/" + resource,
                {
                    "kind": "catalog",
                    "resource": resource,
                    "engagement_id": project,
                    "offset": offset,
                    "limit": limit,
                },
                params=params,
            )
        for label, params in [
            ("zero", [("limit", 0)]),
            ("high", [("limit", 1001)]),
            ("negative", [("offset", -1)]),
            ("invalid", [("offset", "bad")]),
            ("multiple", [("offset", -1), ("limit", 0)]),
            (
                "duplicates",
                [("engagement_id", "missing"), ("engagement_id", "project")],
            ),
            ("coerced", [("offset", "+1.0"), ("limit", " 02 ")]),
            (
                "ignored",
                [
                    ("session_id", "missing"),
                    ("status", "invalid"),
                    ("include_temporary", "false"),
                ],
            ),
        ]:
            add(resource + "-" + label, "/api/v1/" + resource, params=params)
        for identity in [
            examples[0].id,
            examples[-1].id,
            "missing",
            "empty",
            "β" * 201,
        ]:
            add(
                resource + "-get-" + identity[:20],
                "/api/v1/" + resource + "/" + quote(identity, safe=""),
                {"kind": "catalog_record", "resource": resource, "id": identity},
            )
        add(resource + "-no-auth", "/api/v1/" + resource, auth={"headers": {}})
        add(
            resource + "-wrong-auth",
            "/api/v1/" + resource + "/" + examples[0].id,
            auth={"headers": {"Authorization": "Bearer wrong"}},
        )
    for suffix in ["goal", "goal/children", "schedule"]:
        add(
            "custom-no-auth-" + suffix,
            "/api/v1/chat/sessions/parent/" + suffix,
            auth={"headers": {}},
        )
    for kind, identity, suffix in [
        ("goal", "running", "goal"),
        ("children", "parent", "goal/children"),
        ("schedule", "schedule", "schedule"),
    ]:
        add(
            kind + "-reopen",
            f"/api/v1/chat/sessions/{identity}/{suffix}",
            {"kind": kind, "session_id": identity},
            action="reopen",
        )
    return cases


def collect_plans():
    projects, records = initial_plans()
    cases = plan_cases(records)
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-plans-oracle-"
    ) as directory:
        root = Path(directory)
        database = Database(root / "nebula.db")
        store = NebulaStore(database)
        for record in [*projects, *records]:
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
            with patch("nebula.v3.chat_goals.utc_now", return_value=NOW):
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
                for model in MODELS
                for record in store.list_entities(
                    model, limit=1000, include_temporary=True
                )
            ]
        finally:
            client.close()
            database.dispose()
    return {
        "format": "nebula.assistant-plans-oracle/v1",
        "clock": NOW.isoformat(),
        "origin": ORIGIN,
        "normalization": "Preserve retained values, counters and timestamps; normalize only request_id/error_id. Fixed observation clock never comes from requests.",
        "projects": [envelope(r) for r in projects],
        "initial_records": [envelope(r) for r in records],
        "dependency_records": [],
        "cases": cases,
        "final_records": final,
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in [
                "api",
                "chat_goals",
                "chat_schedules",
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
    fixture = collect_plans()
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
