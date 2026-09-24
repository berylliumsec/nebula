#!/usr/bin/env python3
"""Capture existing-conversation goal configuration over inert isolated SQLite.

No application lifespan, provider, harness, tool, workspace or goal execution.
Requests, retained-model reports and raw transactional effects are source truth.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from keyring.backends.null import Keyring as NullKeyring
import pydantic
from pydantic import ValidationError

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatService
from nebula.v3.chat_goals import GoalCreate, GoalUpdate
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import (
    ChatGoal,
    ChatSession,
    ChatTokenUsage,
    ChatTurn,
    Engagement,
    PairedDeviceSession,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.storage import NebulaStore

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.capture_assistant_model_validation import model_metadata
from scripts.capture_assistant_settings import canonical, digest, envelope, normalize

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
ORIGIN = "https://nebula.test:9443"
MODELS = {"ChatGoal": ChatGoal, "ChatTokenUsage": ChatTokenUsage}
REQUEST_MODELS = {"goal_create": GoalCreate, "goal_update": GoalUpdate}


def goal_input(**changes):
    return {
        "id": "fixture-goal",
        "created_at": BASE.isoformat(),
        "updated_at": BASE.isoformat(),
        "revision": 1,
        "engagement_id": "project",
        "session_id": "session",
        "objective": "Complete the fixture",
        "completion_criteria": ["Receipt retained"],
        **changes,
    }


def strict_json(value):
    """Represent nonfinite observations explicitly, never emit invalid JSON."""
    paths = []

    def walk(item, path):
        if isinstance(item, float) and not math.isfinite(item):
            paths.append({"path": path, "value": repr(item)})
            return None
        if isinstance(item, dict):
            return {key: walk(member, [*path, key]) for key, member in item.items()}
        if isinstance(item, list):
            return [walk(member, [*path, index]) for index, member in enumerate(item)]
        return item

    return walk(value, []), paths


def model_vector(name, model, value, *, origin="retained_json"):
    encoded = jsonable_encoder(value)
    result = {
        "name": name,
        "model": model,
        "input": encoded,
        "raw_input": json.dumps(encoded, ensure_ascii=False),
        "input_origin": origin,
    }
    dates = (
        [key for key, item in value.items() if isinstance(item, datetime)]
        if isinstance(value, dict)
        else []
    )
    if dates:
        result["datetime_fields"] = dates
    try:
        validated = MODELS[model].model_validate(deepcopy(value))
        payload, nonfinite = strict_json(validated.model_dump(mode="json"))
        result["expected"] = {"accepted": True, "payload": payload}
        if nonfinite:
            result["expected"]["nonfinite_paths"] = nonfinite
            result["expected"]["serialized_payload"] = json.loads(
                validated.model_dump_json()
            )
    except ValidationError as error:
        result["expected"] = {
            "accepted": False,
            "errors": jsonable_encoder(error.errors(include_url=False)),
            "exception_preview": str(error)[:300],
        }
    return result


def model_vectors():
    result = []

    def add(name, changes=None, *, remove=(), model="ChatGoal", raw=None):
        value = {} if model == "ChatTokenUsage" else goal_input()
        value.update(changes or {})
        for key in remove:
            value.pop(key)
        result.append(model_vector(name, model, value if raw is None else raw))

    add("goal-defaults")
    add(
        "goal-trimmed",
        {
            "objective": " \tGoal β\n",
            "completion_criteria": [" first ", "", "  "],
            "plan": [" step ", " step "],
        },
    )
    for name, value in [
        ("blank", " \t\n"),
        ("nbsp", "\u00a0"),
        ("control", "\x1c"),
        ("zero-width", "\u200b"),
        ("number", 1),
        ("too-long", "😀" * 20001),
    ]:
        add("objective-" + name, {"objective": value})
    for field in ["engagement_id", "session_id", "objective", "completion_criteria"]:
        add("required-" + field, remove=[field])
    add(
        "required-multiple",
        remove=["engagement_id", "objective", "completion_criteria"],
    )
    add("extras-order", {"z_extra": {"z": 1, "a": 2}, "a_extra": "conflict"})
    add("unbounded-empty-identities", {"engagement_id": " ", "session_id": " "})
    for name, value in [
        ("defaults", {}),
        (
            "independent-total",
            {"input_tokens": 20, "output_tokens": 30, "total_tokens": 1},
        ),
        (
            "coercion",
            {"input_tokens": True, "output_tokens": "1_000.0", "total_tokens": 2.0},
        ),
        ("negative", {"input_tokens": -1, "output_tokens": -2, "total_tokens": -3}),
        (
            "ordered-errors",
            {
                "z": 1,
                "input_tokens": "no",
                "a": 2,
                "output_tokens": [],
                "total_tokens": None,
            },
        ),
        ("huge", {"total_tokens": "9" * 100}),
        ("fraction", {"total_tokens": 1.5}),
        ("float-overflow", {"total_tokens": 1e20}),
    ]:
        add("usage-direct-" + name, value, model="ChatTokenUsage")
        add("usage-nested-" + name, {"usage": value})
    for name, value in [
        ("null", None),
        ("list", []),
        ("string", "invalid"),
        ("number", 1),
    ]:
        result.append(
            model_vector("usage-direct-type-" + name, "ChatTokenUsage", value)
        )
        add("usage-nested-type-" + name, {"usage": value})
    for field, limit in [
        ("completion_criteria", 50),
        ("plan", 200),
        ("linked_turn_ids", 10000),
        ("child_session_ids", 32),
    ]:
        add(field + "-max", {field: ["x"] * limit})
        add(field + "-over-max-invalid-first", {field: [1] + ["x"] * limit})
        add(field + "-items-invalid", {field: [" x ", None, 1, {}]})
        add(field + "-null", {field: None})
    add("criteria-empty", {"completion_criteria": []})
    for field, limit in [("completion_evidence", 200), ("skill_snapshots", 20)]:
        add(field + "-opaque", {field: [{"z": [1, True, None], "a": {"q": " β "}}]})
        add(field + "-items-invalid", {field: [[], "x", 1, None]})
        add(field + "-over-max-invalid-first", {field: [1] + [{}] * limit})
    add(
        "metadata-opaque", {"metadata": {"z": {"b": 1, "a": 2}, "a": [True, 1.0, None]}}
    )
    add("metadata-invalid", {"metadata": []})
    for name, value in [
        ("trim-collision", {" a ": 1, "a": 2}),
        ("trim-collision-reversed", {"a": 2, " a ": 1}),
    ]:
        add("metadata-" + name, {"metadata": value})
        add("evidence-" + name, {"completion_evidence": [value]})
    for field in [
        "token_budget",
        "time_budget_seconds",
        "step_budget",
        "child_budget",
        "current_step",
        "children_started",
        "consecutive_stalls",
    ]:
        for name, value in [
            ("bool", True),
            ("string", " 0__3600 "),
            ("negative", -1),
            ("null", None),
            ("huge", "9" * 80),
        ]:
            add(field + "-" + name, {field: value})
    for name, value in [
        ("fraction", 0.0000001),
        ("numeric-string", " 1_000.25 "),
        ("bool", True),
        ("negative", -0.1),
        ("nan", "nan"),
        ("infinity", "inf"),
        ("negative-infinity", "-inf"),
        ("null", None),
        ("huge", 1e300),
        ("dict", {}),
    ]:
        add("elapsed-" + name, {"elapsed_seconds": value})
    for name, value in [
        ("decimal", "10.5"),
        ("padded", " 10.5 "),
        ("underscore", "1_000.25"),
        ("plus", "+1.0"),
        ("Infinity", "Infinity"),
    ]:
        add("elapsed-string-" + name, {"elapsed_seconds": value})
    for name, value in [
        ("running", "running"),
        ("spaced", " running "),
        ("unknown", "conflict"),
        ("null", None),
        ("number", 1),
    ]:
        add("status-" + name, {"status": value})
    for field in [
        "started_at",
        "active_since",
        "paused_at",
        "completed_at",
        "execution_claimed_at",
    ]:
        base = (
            {"execution_owner_id": "owner", "execution_claim_id": "claim"}
            if field == "execution_claimed_at"
            else {}
        )
        for name, value in [
            ("naive", "2030-01-01T12:00:00"),
            ("offset", "2030-01-01T13:00:00+01:00"),
            ("fraction", "2030-01-01T12:00:00.123456789Z"),
            ("invalid", "not-a-date"),
        ]:
            add(field + "-" + name, {**base, field: value})
    for field, limit in [
        ("blocked_reason", 2000),
        ("completion_summary", 20000),
        ("parent_goal_id", 200),
        ("execution_owner_id", 200),
        ("execution_claim_id", 200),
    ]:
        add(field + "-too-long", {field: "x" * (limit + 1)})
    for name, changes in [
        ("blocked-missing", {"status": "blocked"}),
        ("blocked-blank", {"status": "blocked", "blocked_reason": " "}),
        ("blocked-valid", {"status": "blocked", "blocked_reason": " wait "}),
        ("completed-missing", {"status": "completed"}),
        (
            "completed-summary-only",
            {"status": "completed", "completion_summary": "done"},
        ),
        (
            "completed-valid",
            {
                "status": "completed",
                "completion_summary": " done ",
                "completion_evidence": [{}],
            },
        ),
        ("claim-one", {"execution_owner_id": "owner"}),
        ("claim-two", {"execution_owner_id": "owner", "execution_claim_id": "claim"}),
        (
            "claim-all",
            {
                "execution_owner_id": "",
                "execution_claim_id": "",
                "execution_claimed_at": NOW.isoformat(),
            },
        ),
        ("blocked-before-claim", {"status": "blocked", "execution_owner_id": "owner"}),
        (
            "time-before-coherence",
            {"updated_at": "2019-01-01T00:00:00Z", "status": "blocked"},
        ),
        (
            "field-before-coherence",
            {"usage": {"total_tokens": -1}, "status": "blocked"},
        ),
    ]:
        add("coherence-" + name, changes)
    writer = ChatGoal.model_validate(
        goal_input(active_since="2020-01-01T01:00:00+01:00", usage={"total_tokens": 7})
    ).model_dump(mode="python")
    for name, changes in [
        ("valid", {"updated_at": NOW}),
        ("model-after", {"updated_at": BASE - timedelta(seconds=1)}),
        ("nested-error", {"updated_at": NOW, "usage": {"total_tokens": -1}}),
        ("goal-coherence", {"updated_at": NOW, "status": "blocked"}),
    ]:
        result.append(
            model_vector(
                "writer-" + name,
                "ChatGoal",
                {**writer, **changes, "revision": 2},
                origin="writer_model_dump",
            )
        )
    return result


def request_vectors():
    vectors = []
    for kind, model in REQUEST_MODELS.items():
        base = {"objective": " Goal ", "completion_criteria": [" criterion "]}
        if kind == "goal_update":
            base["expected_revision"] = 1
        cases = [
            ("defaults", {}),
            ("ignored-extras", {"status": "running", "z_extra": 1}),
            ("empty-objective", {"objective": ""}),
            ("blank-objective", {"objective": " \t "}),
            ("control-objective", {"objective": "\x1c"}),
            ("nbsp-objective", {"objective": "\u00a0"}),
            ("criteria-empty", {"completion_criteria": []}),
            ("criteria-items", {"completion_criteria": ["", " ", " same ", "same"]}),
            ("criteria-invalid-items", {"completion_criteria": ["x", 1, None, {}]}),
            (
                "criteria-over-max-invalid-first",
                {"completion_criteria": [1] + ["x"] * 50},
            ),
            ("plan-over-max-invalid-last", {"plan": ["x"] * 200 + [1]}),
            ("plan-invalid-items", {"plan": [1, None]}),
            ("null-plan", {"plan": None}),
            (
                "null-budgets",
                {
                    "token_budget": None,
                    "time_budget_seconds": None,
                    "step_budget": None,
                    "child_budget": None,
                },
            ),
            (
                "bool-budgets",
                {
                    "token_budget": True,
                    "time_budget_seconds": True,
                    "step_budget": True,
                    "child_budget": False,
                },
            ),
            (
                "integral-float-budgets",
                {
                    "token_budget": 2.0,
                    "time_budget_seconds": 3.0,
                    "step_budget": 4.0,
                    "child_budget": 0.0,
                },
            ),
            ("fraction-budget", {"token_budget": 1.5}),
            ("huge-budget", {"token_budget": "9" * 100}),
            ("float-i64-boundary", {"token_budget": float(2**63)}),
            ("child-limit", {"child_budget": 32}),
            ("child-over-limit", {"child_budget": 33}),
            (
                "budget-zero",
                {
                    "token_budget": 0,
                    "time_budget_seconds": 0,
                    "step_budget": 0,
                    "child_budget": -1,
                },
            ),
        ]
        for name, changes in cases:
            value = {**base, **changes}
            try:
                validated = model.model_validate(value)
                expected = {
                    "accepted": True,
                    "payload": validated.model_dump(mode="json"),
                    "fields_set": sorted(validated.model_fields_set),
                }
            except ValidationError as error:
                expected = {
                    "accepted": False,
                    "errors": jsonable_encoder(error.errors(include_url=False)),
                }
            vectors.append(
                {
                    "name": kind + "-" + name,
                    "kind": kind,
                    "input": value,
                    "raw_input": json.dumps(value, ensure_ascii=False),
                    "expected": expected,
                }
            )
        for name, value in [
            ("missing-required", {}),
            ("wrong-container", []),
            ("null-body", None),
        ]:
            try:
                validated = model.model_validate(value)
                raise AssertionError(validated)
            except ValidationError as error:
                vectors.append(
                    {
                        "name": kind + "-" + name,
                        "kind": kind,
                        "input": value,
                        "raw_input": json.dumps(value),
                        "expected": {
                            "accepted": False,
                            "errors": jsonable_encoder(error.errors(include_url=False)),
                        },
                    }
                )
    return vectors


def model_contract():
    return {
        "format": "nebula.assistant-goal-drafts-oracle/v1",
        "pydantic_version": pydantic.__version__,
        "models": {name: model_metadata(model) for name, model in MODELS.items()},
        "vectors": model_vectors(),
        "request_schemas": {
            name: model.model_json_schema() for name, model in REQUEST_MODELS.items()
        },
        "request_vectors": request_vectors(),
    }


def initial_goals():
    records, cases, raw_overrides = {}, [], {}
    project = Engagement(
        id="project", name="Goal fixture", created_at=BASE, updated_at=BASE
    )

    def session(identity, *, harness=False, metadata=None):
        records[identity] = ChatSession(
            id=identity,
            engagement_id="project",
            title=identity,
            model="fixture",
            backend="harness" if harness else "provider",
            provider_profile_id=None if harness else "missing-provider",
            harness_profile_id="missing-harness" if harness else None,
            harness_session_id=identity + "-vendor" if harness else None,
            metadata=metadata or {},
            created_at=BASE,
            updated_at=BASE,
        )
        return identity

    def goal(owner, *, identity=None, **changes):
        identity = identity or owner + "-goal"
        records[identity] = ChatGoal.model_validate(
            goal_input(id=identity, session_id=owner, **changes)
        )
        return identity

    def add(name, kind, body=None, owner=None, **extra):
        owner = owner or session(name)
        body = (
            body
            if body is not None
            else {
                "objective": " Saved objective β ",
                "completion_criteria": [" first ", ""],
                **({"expected_revision": 1} if kind == "goal_update" else {}),
            }
        )
        try:
            value = REQUEST_MODELS[kind].model_validate(body)
            service = {
                "kind": kind,
                "session_id": owner,
                "body": value.model_dump(mode="json"),
            }
        except ValidationError:
            service = None
        row = {
            "name": name,
            "method": "POST" if kind == "goal_create" else "PATCH",
            "path": f"/api/v1/chat/sessions/{owner}/goal",
            "body": body,
            "service": service,
            **extra,
        }
        if kind == "goal_create":
            row.setdefault("generated_ids", [str(UUID(int=20000 + len(cases)))])
        if "auth" in row:
            row["service"] = None
        cases.append(row)
        return row

    owner = session("sequence")
    created = add("create-sequence", "goal_create", owner=owner)
    add("create-repeat", "goal_create", owner=owner)
    add(
        "update-sequence",
        "goal_update",
        {
            "expected_revision": 1,
            "objective": "Changed",
            "completion_criteria": ["done"],
            "plan": ["first"],
            "token_budget": 100,
            "time_budget_seconds": 60,
            "step_budget": 2,
            "child_budget": 1,
        },
        owner,
    )
    add(
        "update-replace-defaults",
        "goal_update",
        {
            "expected_revision": 2,
            "objective": "Changed",
            "completion_criteria": ["done"],
        },
        owner,
    )
    add(
        "update-repeat-stale",
        "goal_update",
        {
            "expected_revision": 2,
            "objective": "Changed",
            "completion_criteria": ["done"],
        },
        owner,
    )
    add(
        "update-no-effective-change",
        "goal_update",
        {
            "expected_revision": 3,
            "objective": "Changed",
            "completion_criteria": ["done"],
        },
        owner,
        action="reopen",
    )
    add("create-harness", "goal_create", owner=session("create-harness", harness=True))
    add("create-missing-session", "goal_create", owner="missing-session")
    add("create-wrong-kind-session", "goal_create", owner="project")
    owner = session("create-corrupt-session")
    raw_overrides[owner] = {"provider_profile_id": None}
    add(owner, "goal_create", owner=owner)
    owner = session(
        "create-archived-pending",
        metadata={
            "archived_at": BASE.isoformat(),
            "temporary": True,
            "z": {"b": 1, "a": 2},
        },
    )
    records[owner + "-turn"] = ChatTurn(
        id=owner + "-turn",
        engagement_id="project",
        session_id=owner,
        provider_profile_id="missing-provider",
        model="fixture",
        status="routing",
        created_at=BASE,
        updated_at=BASE,
    )
    add(owner, "goal_create", owner=owner)
    for name, objective in [
        ("blank", " \t\n"),
        ("nbsp", "\u00a0"),
        ("control", "\x1c"),
        ("zero-width", "\u200b"),
        ("unicode", " e\u0301 😀 \n"),
    ]:
        add(
            "create-objective-" + name,
            "goal_create",
            {"objective": objective, "completion_criteria": [" criterion "]},
        )
    add(
        "create-two-clock-samples",
        "goal_create",
        model_clock_values=[
            NOW.isoformat(),
            (NOW + timedelta(microseconds=1)).isoformat(),
        ],
    )
    add(
        "create-backward-clock",
        "goal_create",
        model_clock_values=[
            NOW.isoformat(),
            (NOW - timedelta(microseconds=1)).isoformat(),
        ],
    )
    collision = str(UUID(int=19999))
    session(collision)
    add("create-id-collision", "goal_create", generated_ids=[collision])
    for name, malformed in [
        ("multiple-valid", False),
        ("multiple-malformed", True),
        ("single-malformed", True),
    ]:
        owner = session(name)
        goal(owner, identity=owner + "-a")
        if name != "single-malformed":
            second = goal(owner, identity=owner + "-b")
        else:
            second = owner + "-a"
        if malformed:
            raw_overrides[second] = {"usage": {"total_tokens": -1}}
        add("create-" + name, "goal_create", owner=owner)
        add("update-" + name, "goal_update", owner=owner)
    # Corrupt nonmatching rows must not enter the indexed candidate collection.
    owner = session("unrelated-malformed")
    raw_overrides[goal(owner)] = {"objective": []}
    add("create-excludes-unrelated-malformed", "goal_create")
    for status in ["draft", "running", "paused", "blocked", "completed", "cancelled"]:
        owner = session("status-" + status, harness=status == "paused")
        goal(
            owner,
            status=status,
            blocked_reason="retained block",
            completion_summary="retained summary",
            completion_evidence=[{"z": 1, "a": 2}],
            usage={"input_tokens": 99, "output_tokens": 101, "total_tokens": 7},
            elapsed_seconds=2.5,
            current_step=2,
            children_started=1,
            linked_turn_ids=["turn"],
            started_at=BASE,
            active_since=NOW - timedelta(seconds=1),
            paused_at=BASE,
            completed_at=BASE,
            execution_owner_id="owner",
            execution_claim_id="claim",
            execution_claimed_at=BASE,
            skill_snapshots=[{"z": 1, "a": 2}],
            parent_goal_id="retained-parent",
            child_session_ids=["child"],
            consecutive_stalls=3,
            metadata={"z": {"y": True, "b": 1}, "a": 2},
        )
        add("update-status-" + status, "goal_update", owner=owner)
    add("update-missing-session", "goal_update", owner="absent")
    add("update-wrong-kind-session", "goal_update", owner="project")
    add("update-no-goal", "goal_update", owner=session("update-no-goal"))
    add("update-corrupt-session", "goal_update", owner="create-corrupt-session")
    # Each boundary owns one goal so a successful earlier mutation cannot alter
    # the budget/counter condition intended for the next request.
    boundaries = [
        (
            "revision-first",
            {
                "status": "completed",
                "completion_summary": "done",
                "completion_evidence": [{}],
                "current_step": 5,
            },
            {"expected_revision": 2, "step_budget": 1},
        ),
        (
            "terminal-before-step",
            {"status": "cancelled", "current_step": 5},
            {"step_budget": 1},
        ),
        (
            "step-before-child",
            {"current_step": 5, "children_started": 3},
            {
                "step_budget": 4,
                "child_budget": 2,
                "time_budget_seconds": 1,
                "token_budget": 1,
            },
        ),
        (
            "child-before-time",
            {"children_started": 3, "elapsed_seconds": 10},
            {"child_budget": 2, "time_budget_seconds": 1},
        ),
        (
            "time-before-token",
            {"elapsed_seconds": 10, "usage": {"total_tokens": 10}},
            {"time_budget_seconds": 9, "token_budget": 9},
        ),
        ("step-equal", {"current_step": 5}, {"step_budget": 5}),
        ("child-equal", {"children_started": 3}, {"child_budget": 3}),
        ("time-equal", {"elapsed_seconds": 10}, {"time_budget_seconds": 10}),
        ("token-equal", {"usage": {"total_tokens": 10}}, {"token_budget": 10}),
        ("token-below", {"usage": {"total_tokens": 10}}, {"token_budget": 9}),
        (
            "token-total-independent",
            {"usage": {"input_tokens": 1000, "output_tokens": 1000, "total_tokens": 1}},
            {"token_budget": 1},
        ),
        (
            "token-big-equal",
            {"usage": {"total_tokens": 10**100}},
            {"token_budget": 10**100},
        ),
        (
            "token-big-below",
            {"usage": {"total_tokens": 10**100}},
            {"token_budget": 10**100 - 1},
        ),
        (
            "running-live-equal",
            {
                "status": "running",
                "elapsed_seconds": 2,
                "active_since": NOW - timedelta(seconds=3),
            },
            {"time_budget_seconds": 5},
        ),
        (
            "running-live-below",
            {
                "status": "running",
                "elapsed_seconds": 2,
                "active_since": NOW - timedelta(seconds=3),
            },
            {"time_budget_seconds": 4},
        ),
        ("fraction-below", {"elapsed_seconds": 1.0000001}, {"time_budget_seconds": 1}),
        ("fraction-above", {"elapsed_seconds": 0.9999999}, {"time_budget_seconds": 1}),
        (
            "running-submicro-below",
            {
                "status": "running",
                "elapsed_seconds": 0.9999991,
                "active_since": NOW - timedelta(microseconds=1),
            },
            {"time_budget_seconds": 1},
        ),
        (
            "running-submicro-above",
            {
                "status": "running",
                "elapsed_seconds": 0.9999989,
                "active_since": NOW - timedelta(microseconds=1),
            },
            {"time_budget_seconds": 1},
        ),
        (
            "running-future",
            {
                "status": "running",
                "elapsed_seconds": 1,
                "active_since": NOW + timedelta(seconds=1),
            },
            {"time_budget_seconds": 1},
        ),
        (
            "running-no-active",
            {"status": "running", "elapsed_seconds": 1},
            {"time_budget_seconds": 1},
        ),
        (
            "naive-active-error",
            {"status": "running", "active_since": NOW.replace(tzinfo=None)},
            {"time_budget_seconds": 1},
        ),
        (
            "naive-active-null-bypass",
            {"status": "running", "active_since": NOW.replace(tzinfo=None)},
            {"time_budget_seconds": None},
        ),
        (
            "naive-active-paused-bypass",
            {"status": "paused", "active_since": NOW.replace(tzinfo=None)},
            {"time_budget_seconds": 1},
        ),
        (
            "naive-active-revision-bypass",
            {"status": "running", "active_since": NOW.replace(tzinfo=None)},
            {"time_budget_seconds": 1, "expected_revision": 2},
        ),
        (
            "naive-active-step-bypass",
            {
                "status": "running",
                "active_since": NOW.replace(tzinfo=None),
                "current_step": 2,
            },
            {"time_budget_seconds": 1, "step_budget": 1},
        ),
        (
            "null-clear-over-request-child-limit",
            {"child_budget": 40, "children_started": 40},
            {"child_budget": None},
        ),
        (
            "large-elapsed-below",
            {"elapsed_seconds": 1e100},
            {"time_budget_seconds": 10**99},
        ),
        (
            "large-elapsed-above",
            {"elapsed_seconds": 1e100},
            {"time_budget_seconds": 10**101},
        ),
        (
            "time-int-float-rounding",
            {"elapsed_seconds": float(2**53)},
            {"time_budget_seconds": 2**53 - 1},
        ),
    ]
    for name, fields, changes in boundaries:
        owner = session("boundary-" + name)
        goal(owner, **fields)
        add(
            "update-" + name,
            "goal_update",
            {
                "expected_revision": 1,
                "objective": "Updated",
                "completion_criteria": ["Done"],
                **changes,
            },
            owner,
        )
    owner = session("update-invalid-objective")
    goal(owner, plan=["retained"], token_budget=9)
    add(
        owner,
        "goal_update",
        {"expected_revision": 1, "objective": " \t ", "completion_criteria": ["Done"]},
        owner,
    )
    owner = session("writer-clock")
    goal(owner)
    row = add(
        "update-writer-clock-rollback",
        "goal_update",
        owner=owner,
        writer_clock="2019-01-01T00:00:00+00:00",
    )
    row["service"] = None
    add("update-writer-clock-reopen", "goal_update", owner=owner, action="reopen")
    owner = session("elapsed-microsecond-rounding")
    goal(
        owner,
        status="running",
        active_since=datetime.fromisoformat("1744-07-29T12:12:25.259007+00:00"),
    )
    cases.append(
        {
            "name": "read-elapsed-microsecond-rounding",
            "method": "GET",
            "path": f"/api/v1/chat/sessions/{owner}/goal",
            "body": None,
            "service": None,
        }
    )
    add(
        "update-elapsed-microsecond-rounding",
        "goal_update",
        {
            "expected_revision": 1,
            "objective": "Updated",
            "completion_criteria": ["Done"],
            "time_budget_seconds": 9007199255,
        },
        owner,
    )
    owner = session("stale-goal-search")
    goal(owner)
    add("update-clears-stale-search", "goal_update", owner=owner)
    owner = session("retained-dictionary-keys")
    identity = goal(owner)
    raw_overrides[identity] = {
        "metadata": {
            "b": 0,
            " a ": {"z": 1, "a": 2},
            "a": {"a": 2, "z": 1},
            " c ": "retained",
        },
        "completion_evidence": [
            {"z": 0, " a ": "first", "a": "last", " c ": {"y": 1, "a": 2}}
        ],
        "skill_snapshots": [
            {"z": 0, "a": "first", " a ": "last", " c ": {"z": 1, "a": 2}}
        ],
    }
    add("update-retained-dictionary-keys", "goal_update", owner=owner)
    for value in request_vectors():
        owner = session("request-" + value["name"])
        if value["kind"] == "goal_update":
            goal(owner)
        row = add(
            "request-" + value["name"],
            value["kind"],
            value["input"] if value["input"] is not None else {},
            owner,
        )
        row["body"] = value["input"]
        if not value["expected"]["accepted"]:
            row["service"] = None
    dependency = PairedDeviceSession(
        id="paired",
        name="Fixture device",
        token_sha256=sha256(b"fixture-device").hexdigest(),
        csrf_sha256=sha256(b"fixture-csrf").hexdigest(),
        created_at=BASE,
        updated_at=BASE,
        last_used_at=NOW + timedelta(days=1),
        idle_expires_at=NOW + timedelta(days=2),
        absolute_expires_at=NOW + timedelta(days=60),
    )
    paired = {
        "Cookie": "nebula_device=fixture-device; nebula_csrf=fixture-csrf",
        "Origin": ORIGIN,
        "X-Nebula-CSRF": "fixture-csrf",
    }
    for kind in REQUEST_MODELS:
        for name, headers in [
            ("missing", {}),
            ("wrong", {"Authorization": "Bearer wrong"}),
            ("paired", paired),
            ("csrf", {k: v for k, v in paired.items() if k != "X-Nebula-CSRF"}),
            ("origin", {**paired, "Origin": "https://other.invalid"}),
        ]:
            owner = session("auth-" + kind + "-" + name)
            if kind == "goal_update":
                goal(owner)
            add(owner, kind, owner=owner, auth={"headers": headers})
    assert created["generated_ids"]
    return [project], list(records.values()), [dependency], raw_overrides, cases


def collect_goal_drafts():
    projects, records, dependencies, raw_overrides, cases = initial_goals()
    clock, writer_clock, current_case = [NOW], [NOW], [{}]
    executions, clock_calls = [], []
    uuid_calls = [0]
    protected_names = [
        "operation_events",
        "run_events",
        "resource_relations",
        "session_projections",
    ]

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            index = clock_calls.count("model")
            values = current_case[0].get("model_clock_values")
            instant = datetime.fromisoformat(values[index]) if values else clock[0]
            clock_calls.append("model")
            return (
                instant.astimezone(tz)
                if tz is not None
                else instant.replace(tzinfo=None)
            )

    def unexpected(*_args, **_kwargs):
        executions.append("attempted external execution")
        raise AssertionError(executions[-1])

    def fixed_uuid():
        ids = current_case[0].get("generated_ids", [])
        index = uuid_calls[0]
        uuid_calls[0] += 1
        assert index < len(ids), "unexpected goal UUID allocation"
        return UUID(ids[index])

    def elapsed_clock():
        clock_calls.append("elapsed")
        return clock[0]

    def commit_clock():
        clock_calls.append("writer")
        return writer_clock[0]

    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-goal-drafts-oracle-"
    ) as directory:
        path = Path(directory) / "nebula.db"
        database = Database(path)
        store = NebulaStore(database)
        for record in [*projects, *records, *dependencies]:
            store.create(record)
        with sqlite3.connect(path) as raw:
            for identity, replacement in raw_overrides.items():
                payload = json.loads(
                    raw.execute(
                        "SELECT payload FROM entities WHERE id=?", (identity,)
                    ).fetchone()[0]
                )
                payload.update(replacement)
                raw.execute(
                    "UPDATE entities SET payload=? WHERE id=?",
                    (json.dumps(payload), identity),
                )
            stamp = "2020-01-01 00:00:00.000000"
            raw.execute(
                "INSERT INTO session_projections(session_id,revision,digest) VALUES(?,?,?)",
                ("sequence", 7, "a" * 64),
            )
            raw.execute(
                "INSERT INTO operation_events(id,operation_id,operation_kind,engagement_id,sequence,event_type,payload,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "protected-operation",
                    "sequence",
                    "chat",
                    "project",
                    1,
                    "fixture.saved",
                    '{"z": 1, "a": "retained"}',
                    stamp,
                ),
            )
            raw.execute(
                "INSERT INTO run_events(id,run_id,sequence,event_type,payload,occurred_at) VALUES(?,?,?,?,?,?)",
                (
                    "protected-run-event",
                    "sequence",
                    1,
                    "fixture.saved",
                    '{"opaque": true}',
                    stamp,
                ),
            )
            raw.execute(
                "INSERT INTO resource_relations(id,project_id,source_kind,source_id,predicate,target_kind,target_id,provenance,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "protected-relation",
                    "project",
                    "chat_sessions",
                    "sequence",
                    "fixture",
                    "chat_sessions",
                    "create-harness",
                    '{"z": 1, "a": 2}',
                    1,
                    stamp,
                    stamp,
                ),
            )
            # A stale historical search row for a nonindexed Goal is removed by
            # the same generic writer projection boundary as any other update.
            search = dict(
                zip(
                    [
                        row[1]
                        for row in raw.execute("PRAGMA table_info(search_documents)")
                    ],
                    raw.execute(
                        "SELECT * FROM search_documents WHERE id='stale-goal-search'"
                    ).fetchone(),
                )
            )
            search["id"] = "stale-goal-search-goal"
            search["resource_id"] = "stale-goal-search-goal"
            raw.execute(
                f"INSERT INTO search_documents({','.join(search)}) VALUES({','.join('?' for _ in search)})",
                tuple(search.values()),
            )
            schema_sql = [
                row[0]
                for row in raw.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name IN (?,?,?,?) AND sql IS NOT NULL ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name",
                    protected_names,
                )
            ]
            raw_payloads = dict(
                raw.execute("SELECT id,payload FROM entities ORDER BY id")
            )

        def snapshot():
            with sqlite3.connect(path) as raw:
                raw.row_factory = sqlite3.Row
                rows = {
                    row["id"]: dict(row)
                    for row in raw.execute("SELECT * FROM entities ORDER BY id")
                }
                envelopes = {
                    identity: {
                        "kind": row["kind"],
                        "payload": json.loads(row["payload"]),
                    }
                    for identity, row in rows.items()
                }
                search = {
                    row["id"]: dict(row)
                    for row in raw.execute("SELECT * FROM search_documents ORDER BY id")
                }
                protected = {
                    name: [
                        dict(row)
                        for row in raw.execute(f"SELECT * FROM {name} ORDER BY 1")
                    ]
                    for name in protected_names
                }
                return envelopes, rows, search, protected

        def inert_chat(current, **kwargs):
            kwargs.update(
                provider_factory=unexpected,
                workspace_resolver=unexpected,
                tool_suggestion_client=unexpected,
                worker_id="inert-goal-oracle",
            )
            return ChatService(current, **kwargs)

        def client_for(current):
            credentials = CredentialStore(keyring_backend=NullKeyring())
            runtime = HarnessRuntimeService(
                current,
                credential_store=credentials,
                workspace_resolver=unexpected,
                adapter_factory=unexpected,
                artifact_store=ArtifactStore(Path(directory) / "artifacts"),
            )
            with patch("nebula.v3.api.ChatService", side_effect=inert_chat):
                app = create_app(
                    current,
                    credential_store=credentials,
                    harness_runtime_service=runtime,
                    artifact_store=ArtifactStore(Path(directory) / "artifacts"),
                    auth_token="fixture-core",
                    enable_executable_missions=False,
                    bootstrap_workspace=False,
                )
            return TestClient(app, raise_server_exceptions=False, base_url=ORIGIN)

        def capture_nonfinite():
            """Observe source-only nonfinite state without a fake finite envelope."""
            other_path = Path(directory) / "nonfinite.db"
            other_database = Database(other_path)
            other_store = NebulaStore(other_database)
            owner = ChatSession(
                id="nonfinite-session",
                engagement_id="project",
                title="Nonfinite fixture",
                provider_profile_id="missing-provider",
                model="fixture",
                created_at=BASE,
                updated_at=BASE,
            )
            target = ChatGoal.model_validate(
                goal_input(id="nonfinite-goal", session_id=owner.id)
            )
            for record in [projects[0], owner, target]:
                other_store.create(record)
            with sqlite3.connect(other_path) as raw:
                payload = json.loads(
                    raw.execute(
                        "SELECT payload FROM entities WHERE id=?", (target.id,)
                    ).fetchone()[0]
                )
                payload["elapsed_seconds"] = "inf"
                raw.execute(
                    "UPDATE entities SET payload=? WHERE id=?",
                    (json.dumps(payload), target.id),
                )

            def raw_state():
                with sqlite3.connect(other_path) as raw:
                    raw.row_factory = sqlite3.Row
                    return {
                        name: [
                            dict(row)
                            for row in raw.execute(f"SELECT * FROM {name} ORDER BY 1")
                        ]
                        for name in ["entities", "search_documents", *protected_names]
                    }

            observed = []
            other_client = client_for(other_store)
            try:
                for name, method, body in [
                    (
                        "finite-budget-refused",
                        "PATCH",
                        {
                            "expected_revision": 1,
                            "objective": "Updated",
                            "completion_criteria": ["done"],
                            "time_budget_seconds": 10,
                        },
                    ),
                    (
                        "null-budget-write",
                        "PATCH",
                        {
                            "expected_revision": 1,
                            "objective": "Updated",
                            "completion_criteria": ["done"],
                        },
                    ),
                    ("read-after-write", "GET", None),
                ]:
                    current_case[0] = {}
                    clock[0] = writer_clock[0] = NOW
                    clock_calls.clear()
                    uuid_calls[0] = 0
                    before = raw_state()
                    response = other_client.request(
                        method,
                        f"/api/v1/chat/sessions/{owner.id}/goal",
                        headers={"Authorization": "Bearer fixture-core"},
                        json=body,
                    )
                    after = raw_state()
                    observed.append(
                        {
                            "name": name,
                            "method": method,
                            "body": body,
                            "expected": {
                                "status": response.status_code,
                                "body": normalize(response.json()),
                            },
                            "expected_cache_control": response.headers.get(
                                "cache-control"
                            ),
                            "expected_clock_calls": list(clock_calls),
                            "before_raw_tables": before,
                            "after_raw_tables": after,
                        }
                    )
                    assert not executions
            finally:
                other_client.close()
                other_database.dispose()
            return {
                "boundary": "Source accepts nonfinite elapsed state outside the finite Rust retained JSON contract. Raw payload strings preserve actual persisted Infinity; no null is substituted for durable state.",
                "cases": observed,
            }

        initial, initial_rows, initial_search, initial_protected = snapshot()
        client = client_for(store)
        try:
            with ExitStack() as guards:
                guards.enter_context(patch("nebula.v3.domain.datetime", FrozenDatetime))
                for module in ["api", "chat", "chat_schedules"]:
                    guards.enter_context(
                        patch(
                            f"nebula.v3.{module}.utc_now", side_effect=lambda: clock[0]
                        )
                    )
                guards.enter_context(
                    patch("nebula.v3.chat_goals.utc_now", side_effect=elapsed_clock)
                )
                guards.enter_context(
                    patch("nebula.v3.storage.utc_now", side_effect=commit_clock)
                )
                guards.enter_context(
                    patch("nebula.v3.chat_goals.uuid4", side_effect=fixed_uuid)
                )
                for method in [
                    "prepare",
                    "prepare_async",
                    "prepare_resume",
                    "stream",
                    "_run_native_hooks",
                    "resume_turns_stopped_by_core",
                    "fire_due_schedules",
                ]:
                    guards.enter_context(
                        patch.object(ChatService, method, side_effect=unexpected)
                    )
                for method in ["create_session", "prepare_chat", "stream_turn"]:
                    guards.enter_context(
                        patch.object(
                            HarnessRuntimeService, method, side_effect=unexpected
                        )
                    )
                for case in cases:
                    current_case[0] = case
                    uuid_calls[0] = 0
                    clock_calls.clear()
                    clock[0] = datetime.fromisoformat(
                        case.get("clock", NOW.isoformat())
                    )
                    writer_clock[0] = datetime.fromisoformat(
                        case.get("writer_clock", case.get("clock", NOW.isoformat()))
                    )
                    if case.get("action") == "reopen":
                        client.close()
                        database.dispose()
                        database = Database(path, bootstrap=False)
                        store = NebulaStore(database)
                        client = client_for(store)
                    # Reopening constructs inert application state before the
                    # request. Its default factories are not route clock calls.
                    clock_calls.clear()
                    uuid_calls[0] = 0
                    before, before_rows, before_search, before_protected = snapshot()
                    headers = {
                        "Authorization": "Bearer fixture-core",
                        "X-Nebula-Operation-ID": "fixture-operation",
                    }
                    if "auth" in case:
                        headers.pop("Authorization")
                        headers.update(case["auth"]["headers"])
                    response = client.request(
                        case["method"], case["path"], headers=headers, json=case["body"]
                    )
                    case["expected"] = {
                        "status": response.status_code,
                        "body": normalize(response.json()),
                    }
                    case["expected_cache_control"] = response.headers.get(
                        "cache-control"
                    )
                    case["expected_uuid_calls"] = uuid_calls[0]
                    case["expected_clock_calls"] = list(clock_calls)
                    after, after_rows, after_search, after_protected = snapshot()
                    changed_ids = {
                        identity
                        for identity in before_rows.keys() | after_rows.keys()
                        if before_rows.get(identity) != after_rows.get(identity)
                    }
                    case["expected_changes"] = [
                        {"before": before.get(identity), "after": after.get(identity)}
                        for identity in sorted(changed_ids)
                    ]
                    assert all(
                        after.get(identity, before.get(identity))["kind"]
                        == "chat_goals"
                        for identity in changed_ids
                    ), "unexpected authoritative mutation"
                    assert all(
                        canonical(before.get(identity))
                        != canonical(after.get(identity))
                        for identity in changed_ids
                    ), "unexpected raw-only authoritative mutation"
                    assert before.keys() <= after.keys(), (
                        "goal configuration must not delete entities"
                    )
                    assert before_protected == after_protected, (
                        "goal configuration mutated a protected table"
                    )
                    search_ids = {
                        identity
                        for identity in before_search.keys() | after_search.keys()
                        if before_search.get(identity) != after_search.get(identity)
                    }
                    assert search_ids <= changed_ids, (
                        "search changed without owning entity"
                    )
                    case["expected_search_changes"] = [
                        {
                            "before": before_search.get(identity),
                            "after": after_search.get(identity),
                        }
                        for identity in sorted(search_ids)
                    ]
                    for label, left, right in [
                        ("snapshot", list(before.values()), list(after.values())),
                        (
                            "search",
                            list(before_search.values()),
                            list(after_search.values()),
                        ),
                        ("protected", before_protected, after_protected),
                    ]:
                        case[f"before_{label}_sha256"] = digest(left)
                        case[f"after_{label}_sha256"] = digest(right)
                    assert not executions, "goal request attempted external execution"
                nonfinite_http_observations = capture_nonfinite()
            final, final_rows, final_search, final_protected = snapshot()
        finally:
            client.close()
            database.dispose()
    result = model_contract()
    nonfinite = [
        value for value in result["vectors"] if value["expected"].get("nonfinite_paths")
    ]
    result["vectors"] = [value for value in result["vectors"] if value not in nonfinite]
    result["nonfinite_model_observations"] = nonfinite
    result["nonfinite_http_observations"] = nonfinite_http_observations
    assistant_kinds = {"chat_sessions", "chat_goals", "chat_turns"}
    result.update(
        {
            "clock": NOW.isoformat(),
            "origin": ORIGIN,
            "normalization": "Only diagnostic request_id/error_id. Per-case generated_ids and trusted clocks are injected. model_clock_values supplies successive created_at and updated_at factories; expected_clock_calls excludes authentication. SHA256 uses ID-sorted arrays/protected maps with Python sorted-key ASCII JSON and comma/colon separators. Raw rows retain timestamps, payload strings and all envelope columns. Source goal absence lookup is outside insertion: this oracle does not establish singleton admission.",
            "projects": [envelope(record) for record in projects],
            "initial_records": [
                row for row in initial.values() if row["kind"] in assistant_kinds
            ],
            "dependency_records": [
                row
                for row in initial.values()
                if row["kind"] not in assistant_kinds and row["kind"] != "engagements"
            ],
            "raw_payloads": raw_payloads,
            "initial_entity_rows": list(initial_rows.values()),
            "initial_search_documents": list(initial_search.values()),
            "initial_protected_tables": initial_protected,
            "schema_sql": schema_sql,
            "dependency_schemas": {
                "paired_device_sessions": PairedDeviceSession.model_json_schema()
            },
            "cases": cases,
            "known_unsupported_cases": [],
            "final_records": [
                row for row in final.values() if row["kind"] in assistant_kinds
            ],
            "final_dependencies": [
                row
                for row in final.values()
                if row["kind"] not in assistant_kinds and row["kind"] != "engagements"
            ],
            "final_entity_rows": list(final_rows.values()),
            "final_search_documents": list(final_search.values()),
            "final_protected_tables": final_protected,
            "source_sha256": {
                f"src/nebula/v3/{name}.py": sha256(
                    (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
                ).hexdigest()
                for name in [
                    "domain",
                    "chat_goals",
                    "api",
                    "storage",
                    "database",
                    "search",
                ]
            },
        }
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect_goal_drafts()
    args.output.write_text(
        json.dumps(
            result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(result["cases"]),
                "vectors": len(result["vectors"]),
                "request_vectors": len(result["request_vectors"]),
                "sha256": sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
