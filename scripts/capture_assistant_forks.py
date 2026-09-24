#!/usr/bin/env python3
"""Capture complete execution-free Assistant forks in isolated SQLite."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
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
from nebula.v3.api import ChatSessionForkRequest, create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatService
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import (
    ChatCitation,
    ChatContentBlock,
    ChatDecision,
    ChatGoal,
    ChatMessage,
    ChatSession,
    ChatTokenUsage,
    ChatTurn,
    Engagement,
    HarnessSession,
    NativeHookExecution,
    PairedDeviceSession,
    ToolCall,
    WorkspaceProvenanceObservation,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.storage import NebulaStore

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.capture_assistant_goal_conversations import BASE, NOW, ORIGIN, Observations
from scripts.capture_assistant_model_validation import model_metadata
from scripts.capture_assistant_recovery import receipt
from scripts.capture_assistant_settings import digest, normalize

ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    model.__name__: model
    for model in [
        HarnessSession,
        ChatMessage,
        ChatContentBlock,
        ChatCitation,
        ChatDecision,
        ChatSession,
        ChatGoal,
        ChatTokenUsage,
    ]
}
NEW_MODELS = [HarnessSession, ChatMessage, ChatContentBlock, ChatCitation, ChatDecision]


def fixed(model_type, identity, **fields):
    return model_type(id=identity, created_at=BASE, updated_at=BASE, **fields)


def model_input(name):
    base = dict(
        id="fixture",
        revision=1,
        created_at=BASE.isoformat(),
        updated_at=BASE.isoformat(),
    )
    fields = {
        "HarnessSession": dict(
            engagement_id="project",
            harness_profile_id="retained-profile",
            model="fixture",
            last_activity_at=BASE.isoformat(),
        ),
        "ChatMessage": dict(
            engagement_id="project",
            session_id="source",
            sequence=1,
            role="user",
            content="Fixture",
        ),
        "ChatDecision": dict(
            engagement_id="project", session_id="source", text="Retain context"
        ),
        "ChatSession": dict(
            engagement_id="project",
            title="Fork",
            backend="provider",
            provider_profile_id="provider",
            harness_profile_id=None,
            harness_session_id=None,
            model="fixture",
            parent_session_id="source",
            forked_from_message_id="source-message",
            metadata={"workspace_is_shared": True},
        ),
        "ChatGoal": dict(
            engagement_id="project",
            session_id="fork",
            objective="Goal",
            completion_criteria=["Done"],
            plan=[],
            token_budget=None,
            time_budget_seconds=None,
            step_budget=None,
            child_budget=None,
            skill_snapshots=[],
            metadata={
                "forked_from_goal_id": "source-goal",
                "workspace_is_shared": True,
            },
        ),
        "ChatContentBlock": dict(type="text", text="Fixture"),
        "ChatCitation": dict(
            source_id="source", name="Citation", chunk_id="chunk", excerpt="Text"
        ),
    }
    return {
        **(base if name not in {"ChatContentBlock", "ChatCitation"} else {}),
        **fields[name],
    }


class ForkObservations(Observations):
    def clear(self, case=None):
        super().clear(case)
        self.writer_calls = 0
        self.storage_trace = []
        self.commit_phases = []

    def writer(self):
        values = self.case.get("writer_clock_values")
        value = datetime.fromisoformat(values[self.writer_calls]) if values else NOW
        self.writer_calls += 1
        self.clock_calls.append("writer")
        self.trace.append({"kind": "writer", "value": value.isoformat()})
        return value

    def now(self):
        values = self.case.get("model_clock_values")
        index = sum(label == "model" for label in self.clock_calls)
        value = datetime.fromisoformat(values[index]) if values else NOW
        self.clock_calls.append("model")
        self.trace.append({"kind": "model", "value": value.isoformat()})
        return value


def validation(model, value):
    try:
        result = model.model_validate(deepcopy(value))
        return {"accepted": True, "payload": result.model_dump(mode="json")}
    except ValidationError as error:
        return {
            "accepted": False,
            "errors": jsonable_encoder(error.errors(include_url=False)),
            "exception_preview": str(error)[:300],
        }


def model_vectors():
    vectors, boundaries = [], []
    obs = ForkObservations()

    def add(
        name,
        model,
        changes=None,
        *,
        remove=(),
        boundary=False,
        constructor=False,
        typed_paths=None,
        clocks=None,
    ):
        value = model_input(model) if not constructor else deepcopy(changes)
        if not constructor:
            value.update(changes or {})
        for key in remove:
            value.pop(key, None)
        case = {
            "generated_ids": [str(UUID(int=41))],
            **({"model_clock_values": clocks} if clocks else {}),
        }
        supplied = deepcopy(value)
        for typed in typed_paths or []:
            holder = supplied
            for key in typed["path"][:-1]:
                holder = holder[key]
            key = typed["path"][-1]
            holder[key] = MODELS[typed["model"]].model_validate(holder[key])
        encoded = jsonable_encoder(supplied)
        obs.clear(case)
        with obs.frozen():
            expected = validation(MODELS[model], supplied)
        result = {
            "name": name,
            "model": model,
            "input": encoded,
            "raw_input": json.dumps(encoded, ensure_ascii=False),
            "input_origin": "constructor" if constructor else "retained_json",
            "expected": expected,
            **case,
            **obs.fields(),
        }
        if typed_paths:
            result["typed_paths"] = typed_paths
        (boundaries if boundary else vectors).append(result)

    for model in NEW_MODELS:
        name = model.__name__
        add(name + "-defaults", name)
        add(name + "-extra-order", name, {"z_extra": 1, "a_extra": 2})
        for field, info in model.model_fields.items():
            if info.is_required():
                add(name + "-missing-" + field, name, remove=[field])
        if name not in {"ChatContentBlock", "ChatCitation"}:
            for field in ["id", "revision", "created_at", "updated_at"]:
                add(name + "-factory-" + field, name, remove=[field], boundary=True)
            add(
                name + "-reversed-time",
                name,
                {"updated_at": (BASE - timedelta(seconds=1)).isoformat()},
            )
            add(name + "-naive-created", name, {"created_at": "2020-01-01T00:00:00"})
            add(
                name + "-multi-field",
                name,
                {"id": " ", "revision": 0, "created_at": "bad"},
            )
    for name, changes in [
        ("status", {"status": "unknown"}),
        ("model-empty", {"model": " "}),
        ("mcp-over", {"mcp_server_ids": ["x"] * 65}),
        ("snapshot-item", {"mcp_snapshot": [1]}),
        ("display-max", {"display_name": "x" * 161}),
        (
            "optional-types",
            {"external_session_id": 1, "adapter_version": False, "last_turn_id": {}},
        ),
        ("last-naive", {"last_activity_at": "2020-01-01T00:00:00"}),
        ("last-numeric", {"last_activity_at": 1}),
        ("metadata-ordered", {"metadata": {" a ": 1, "a": 2, "opaque": {" z ": 1}}}),
    ]:
        add("harness-" + name, "HarnessSession", changes)
    add("harness-lazy-last-activity", "HarnessSession", remove=["last_activity_at"])
    add(
        "harness-invalid-still-lazy",
        "HarnessSession",
        {"model": ""},
        remove=["last_activity_at"],
    )
    for name, changes in [
        ("user-blank", {"content": " "}),
        ("assistant-blank", {"role": "assistant", "content": ""}),
        ("role-invalid", {"role": "tool"}),
        ("sequence-coercion", {"sequence": "2.0"}),
        ("sequence-bound", {"sequence": 0}),
        ("content-max", {"content": "x" * 200001}),
        ("timing", {"elapsed_ms": "2", "approval_wait_ms": -1}),
        ("usage-nested", {"usage": {"input_tokens": -1, "unexpected": 1}}),
        ("blocks-nested", {"content_blocks": [{"type": "image"}, {"type": "text"}]}),
        (
            "citations-nested",
            {
                "citations": [
                    {
                        "source_id": "s",
                        "name": "n",
                        "chunk_id": "c",
                        "excerpt": "x",
                        "page": 0,
                    }
                ]
            },
        ),
        ("blocks-limit", {"content_blocks": [{"type": "text", "text": "x"}] * 65}),
        (
            "blocks-limit-invalid",
            {"content_blocks": [1] + [{"type": "text", "text": "x"}] * 64},
        ),
        ("missing-nested", {"content_blocks": [{}], "citations": [{}]}),
        ("user-only-controls", {"content": "\x1c\x1f"}),
        ("trim-controls", {"content": "\x1c hello \x1f"}),
    ]:
        add("message-" + name, "ChatMessage", changes)
    for typ in ["text", "code", "image", "artifact", "citation", "activity", "unknown"]:
        add("block-" + typ, "ChatContentBlock", {"type": typ, "text": None})
    add(
        "block-opaque",
        "ChatContentBlock",
        {"type": "image", "artifact_id": " fixture ", "metadata": {" x ": {" y ": 1}}},
    )
    add("citation-page-coercion", "ChatCitation", {"page": "1.0"})
    add("citation-excerpt-bound", "ChatCitation", {"excerpt": "x" * 321})
    for name, changes in [
        ("scope", {"scope": "outside"}),
        ("kind", {"kind": "other"}),
        ("status", {"status": "other"}),
        ("text", {"text": " "}),
        ("effective", {"effective_sequence": -1}),
        ("history", {"history": [1]}),
        ("copy-revision", {"copied_from_revision": -10}),
        ("copy-revision-huge", {"copied_from_revision": -(10**100)}),
        ("trim-collision", {"history": [{" a ": 1, "a": 2}]}),
    ]:
        add("decision-" + name, "ChatDecision", changes)
    constructors = []
    for model in [
        "HarnessSession",
        "ChatMessage",
        "ChatDecision",
        "ChatSession",
        "ChatGoal",
    ]:
        value = model_input(model)
        for key in ["created_at", "updated_at", "revision", "last_activity_at"]:
            value.pop(key, None)
        if model in {"ChatDecision", "ChatGoal"}:
            value.pop("id")
        for suffix, clocks in [
            ("normal", None),
            (
                "reversed",
                [
                    NOW.isoformat(),
                    (NOW - timedelta(microseconds=1)).isoformat(),
                    NOW.isoformat(),
                ],
            ),
        ]:
            add(
                model + "-constructor-" + suffix,
                model,
                value,
                constructor=True,
                clocks=clocks,
            )
            constructors.append(vectors.pop())
    rich = {
        k: v
        for k, v in model_input("ChatMessage").items()
        if k not in {"created_at", "updated_at", "revision"}
    }
    rich.update(
        content_blocks=[{"type": "text", "text": "Nested model"}],
        citations=[model_input("ChatCitation")],
        usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
    )
    paths = [
        {"path": ["content_blocks", 0], "model": "ChatContentBlock"},
        {"path": ["citations", 0], "model": "ChatCitation"},
        {"path": ["usage"], "model": "ChatTokenUsage"},
    ]
    for suffix, clocks in [
        ("typed", None),
        (
            "typed-reversed",
            [NOW.isoformat(), (NOW - timedelta(microseconds=1)).isoformat()],
        ),
    ]:
        add(
            "message-constructor-" + suffix,
            "ChatMessage",
            rich,
            constructor=True,
            typed_paths=paths,
            clocks=clocks,
        )
        constructors.append(vectors.pop())
    return vectors, boundaries, constructors


def request_vectors():
    inputs = [
        ("through", {"through_message_id": "p-2"}),
        ("before", {"before_message_id": "p-2"}),
        ("neither", {}),
        ("both", {"through_message_id": "p-1", "before_message_id": "p-2"}),
        ("nulls", {"through_message_id": None, "before_message_id": None}),
        ("null-other", {"through_message_id": "p-2", "before_message_id": None}),
        ("custom-title", {"through_message_id": " p-2 ", "title": " New branch "}),
        ("null-title", {"through_message_id": "p-2", "title": None}),
    ]
    for field, maximum in [
        ("through_message_id", 200),
        ("before_message_id", 200),
        ("title", 300),
    ]:
        for label, value in [
            ("blank", " \u00a0 "),
            ("control", "\x1c"),
            ("number", 1),
            ("max", "😀" * maximum),
            ("over", "😀" * (maximum + 1)),
        ]:
            body = {"through_message_id": "p-2"} if field == "title" else {}
            inputs.append((field + "-" + label, {**body, field: value}))
    inputs.extend(
        [
            ("extra-order", {"through_message_id": "p-2", "z": 1, "a": 2}),
            (
                "multi-field",
                {
                    "through_message_id": False,
                    "before_message_id": [],
                    "title": {},
                    "extra": 1,
                },
            ),
            ("root-null", None),
            ("root-list", []),
        ]
    )
    result = []
    for name, value in inputs:
        expected = validation(ChatSessionForkRequest, value)
        result.append(
            {
                "name": name,
                "input": value,
                "raw_input": json.dumps(value, ensure_ascii=False),
                "expected": expected,
            }
        )
    return result


def model_contract():
    models, boundaries, constructors = model_vectors()
    return {
        "format": "nebula.assistant-forks-oracle/v1",
        "clock": NOW.isoformat(),
        "origin": ORIGIN,
        "pydantic_version": pydantic.__version__,
        "models": {m.__name__: model_metadata(m) for m in NEW_MODELS},
        "model_schemas": {m.__name__: m.model_json_schema() for m in NEW_MODELS},
        "dependency_schemas": {"harness_sessions": HarnessSession.model_json_schema()},
        "model_vectors": models,
        "strict_retained_boundary_vectors": boundaries,
        "constructor_vectors": constructors,
        "request_schema": ChatSessionForkRequest.model_json_schema(),
        "request_vectors": request_vectors(),
    }


def initial_forks():
    projects = [
        fixed(Engagement, "project", name="Fork fixtures"),
        fixed(Engagement, "corrupt-decisions", name="Isolated malformed context"),
    ]
    rows = []
    overrides = {}
    cases = []
    collision = str(UUID(int=99000))

    def source(
        identity,
        *,
        harness=False,
        project="project",
        rich=False,
        messages=3,
        goal=False,
        **changes,
    ):
        if harness:
            rows.append(
                fixed(
                    HarnessSession,
                    identity + "-vendor",
                    engagement_id=project,
                    harness_profile_id="unconfigured-harness",
                    model="fixture",
                    status="closed",
                    last_activity_at=BASE,
                    external_session_id="do-not-reuse",
                    display_name="Vendor display",
                    adapter_version="old-adapter",
                    last_turn_id="old-turn",
                    mcp_server_ids=["missing-mcp"],
                    mcp_snapshot=[{"id": "missing-mcp", "frozen": {"z": 1, "a": 2}}],
                    metadata={
                        "opaque": {"z": 1, "a": 2},
                        "workspace_binding_version": "retained",
                        "forked_from_session_id": "old-source",
                    },
                )
            )
        session = fixed(
            ChatSession,
            identity,
            engagement_id=project,
            title=identity,
            backend="harness" if harness else "provider",
            provider_profile_id=None if harness else "missing-provider",
            harness_profile_id="unconfigured-harness" if harness else None,
            harness_session_id=identity + "-vendor" if harness else None,
            model="\x1cfixture\x1f",
            metadata={
                "archived_at": "old",
                "subagent_id": "child",
                "subagent_parent_session_id": "parent",
                "subagent_parent_turn_id": "turn",
                "temporary_assistant": True,
                "message_count": 99,
                "last_sequence": 77,
                "opaque": {"z": 1, "a": 2},
                "forked_from_session_id": "overwrite",
                "harness_context_handoff_pending": "overwrite",
            },
            **changes,
        )
        rows.append(session)
        for index in range(1, messages + 1):
            fields = {}
            if rich and index == 2:
                fields = dict(
                    reasoning="Do not copy reasoning",
                    elapsed_ms=123,
                    approval_wait_ms=45,
                    content_blocks=[
                        {"type": "text", "text": "visible"},
                        {
                            "type": "artifact",
                            "artifact_id": "missing-artifact",
                            "metadata": {"z": 1, "a": 2},
                        },
                    ],
                    usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                    citations=[
                        {
                            "source_id": "unknown",
                            "name": "Saved citation",
                            "chunk_id": "chunk",
                            "excerpt": "retained",
                        }
                    ],
                    finish_reason="stop",
                    provider_request_id="old-request",
                    source_message_id="older-source",
                )
            rows.append(
                fixed(
                    ChatMessage,
                    f"{identity}-{index}",
                    engagement_id=project,
                    session_id=identity,
                    sequence=index,
                    role="user" if index % 2 else "assistant",
                    content=f"Message {index}",
                    provider_profile_id="retained-provider",
                    model="retained-model",
                    metadata={"z": 1, "fork_source_message_id": "overwrite", "a": 2},
                    **fields,
                )
            )
        if goal:
            rows.append(
                fixed(
                    ChatGoal,
                    identity + "-goal",
                    engagement_id=project,
                    session_id=identity,
                    objective="Retain the objective",
                    completion_criteria=["Done"],
                    plan=["Step"],
                    status="running",
                    current_step=7,
                    token_budget=10**100,
                    time_budget_seconds=12,
                    step_budget=20,
                    child_budget=3,
                    usage={"input_tokens": 5, "output_tokens": 6, "total_tokens": 11},
                    elapsed_seconds=7.5,
                    children_started=2,
                    linked_turn_ids=["old-turn"],
                    active_since=BASE.replace(tzinfo=None),
                    started_at=BASE,
                    child_session_ids=["old-child"],
                    execution_owner_id="old-worker",
                    execution_claim_id="claim",
                    execution_claimed_at=BASE,
                    skill_snapshots=[{"path": "skill", "opaque": {"z": 1, "a": 2}}],
                    metadata={"do_not_copy": True},
                )
            )
        return session

    rows.append(
        fixed(
            WorkspaceProvenanceObservation,
            "retained-provenance",
            engagement_id="project",
            workspace_root="/fixture/unopened",
            scope_kind="turn",
            scope_id="retained-turn",
            actor_id="operator",
            owner_id="p",
            chat_session_id="p",
            chat_turn_id="retained-turn",
            started_at=BASE,
            baseline={"opaque": {"sha256": "a" * 64}},
            current={"opaque": {"sha256": "b" * 64}},
            mutations=[{"path": "opaque", "kind": "modified"}],
        )
    )
    source("p", rich=True, goal=True)
    source("h", harness=True, rich=True, goal=True)
    source("plain", messages=1)
    source("legacy-revision-string", messages=1)
    overrides["legacy-revision-string"] = {"revision": "1"}
    source(collision, messages=0)
    for identity in ["p", "h"]:
        for suffix, fields in [
            ("early", {}),
            ("edge", {"effective_sequence": 2}),
            ("late", {"effective_sequence": 4}),
            ("removed", {"status": "removed"}),
            ("project", {"scope": "project"}),
        ]:
            rows.append(
                fixed(
                    ChatDecision,
                    identity + "-decision-" + suffix,
                    engagement_id="project",
                    session_id=identity,
                    text="Context " + suffix,
                    source_message_id=identity + "-1",
                    source_session_id=identity,
                    source_selection="Message",
                    history=[{"old": "history"}],
                    **fields,
                )
            )
    source("ties", messages=0)
    for identity, sequence, created in [
        ("ties-a", 1, 0),
        ("ties-b", 2, 0),
        ("ties-c", 2, 1),
        ("ties-d", 4, 0),
    ]:
        rows.append(
            ChatMessage(
                id=identity,
                created_at=BASE + timedelta(seconds=created),
                updated_at=BASE + timedelta(seconds=created),
                engagement_id="project",
                session_id="ties",
                sequence=sequence,
                role="assistant",
                content=identity,
            )
        )
    source("retracted", messages=3)
    overrides["retracted-2"] = {"metadata": {"retracted_at": {"opaque": True}}}
    overrides["retracted-1"] = {"metadata": {"retracted_at": 0}}
    source("bad-message", messages=2)
    overrides["bad-message-2"] = {
        "role": "invalid",
        "metadata": {"retracted_at": "hidden"},
    }
    source("bad-decision", project="corrupt-decisions", messages=1)
    rows.append(
        fixed(
            ChatDecision,
            "bad-project-decision",
            engagement_id="corrupt-decisions",
            scope="project",
            text="Initially valid",
            status="removed",
        )
    )
    overrides["bad-project-decision"] = {"text": ""}
    source("duplicate-goal", messages=1, goal=True)
    rows.append(
        fixed(
            ChatGoal,
            "duplicate-goal-second",
            engagement_id="project",
            session_id="duplicate-goal",
            objective="Second",
            completion_criteria=["Done"],
        )
    )
    source("bad-goal", messages=1, goal=True)
    overrides["bad-goal-goal"] = {"objective": ""}
    source("bad-harness-goal", harness=True, messages=1, goal=True)
    overrides["bad-harness-goal-goal"] = {"objective": ""}
    source("bad-source", messages=1)
    overrides["bad-source"] = {"provider_profile_id": None}
    for name, status in [
        ("h-running", "running"),
        ("h-approval", "waiting_approval"),
        ("h-starting", "starting"),
        ("h-missing", "closed"),
        ("h-bad", "closed"),
        ("h-lazy", "idle"),
    ]:
        source(name, harness=True, messages=1)
        overrides[name + "-vendor"] = {"status": status}
    overrides["h-missing"] = {"harness_session_id": "absent-vendor"}
    overrides["h-bad-vendor"] = {"model": ""}
    overrides["h-lazy-vendor"] = {"last_activity_at": "__REMOVE__"}
    for owner, kind, recovery in [
        ("pending", "routing", {}),
        ("interrupted-idle", "interrupted", {}),
        ("retry", "interrupted", {"automatic_retry_pending": True}),
        ("bad-recovery", "interrupted", "opaque"),
        ("duplicate-pending", "routing", {}),
        ("failed-answer", "failed", {"final_answer_recovery": True}),
    ]:
        source(owner, messages=1)
        rows.append(
            fixed(
                ChatTurn,
                owner + "-turn",
                engagement_id="project",
                session_id=owner,
                provider_profile_id="missing-provider",
                model="fixture",
                status=kind,
                request_snapshot={"recovery": recovery},
            )
        )
    rows.append(
        fixed(
            ChatTurn,
            "duplicate-pending-second",
            engagement_id="project",
            session_id="duplicate-pending",
            provider_profile_id="missing-provider",
            model="fixture",
            status="waiting_callback",
        )
    )
    for owner, broken, harness in [
        ("repair", False, False),
        ("repair-hook-failure", True, False),
        ("h-repair", False, True),
    ]:
        source(owner, harness=harness, messages=1)
        recovery = {
            "required": True,
            "automatic_retry_pending": True,
            "unknown_tool_call_ids": [owner + "-call"],
            "unknown_hook_execution_ids": [owner + "-hook"],
        }
        if broken:
            recovery["recorded_hook_outcome_ids"] = [{}]
        rows.append(
            fixed(
                ChatTurn,
                owner + "-turn",
                engagement_id="project",
                session_id=owner,
                provider_profile_id="missing-provider",
                model="fixture",
                status="interrupted",
                request_snapshot={"recovery": recovery},
            )
        )
        rows.append(
            fixed(
                ToolCall,
                owner + "-call",
                engagement_id="project",
                run_id=owner + "-turn",
                origin="chat",
                chat_session_id=owner,
                chat_turn_id=owner + "-turn",
                tool_name="read_fixture",
                risk_class="local_read",
                status="complete",
                arguments={"path": "retained.txt"},
                result=receipt(owner + "-call"),
                metadata={"provider_step": 1, "provider_call_id": "model-call"},
            )
        )
        rows.append(
            fixed(
                NativeHookExecution,
                owner + "-hook",
                engagement_id="project",
                chat_session_id=owner,
                chat_turn_id=owner + "-turn",
                hook_id="retained-hook",
                hook_snapshot={"never": "execute"},
                event_name="Stop",
                status="complete",
                started_at=BASE,
                completed_at=BASE,
                exit_code=0,
            )
        )
    rows.append(
        fixed(
            PairedDeviceSession,
            "paired",
            name="Fixture device",
            token_sha256=sha256(b"fixture-device").hexdigest(),
            csrf_sha256=sha256(b"fixture-csrf").hexdigest(),
            last_used_at=NOW + timedelta(days=1),
            idle_expires_at=NOW + timedelta(days=2),
            absolute_expires_at=NOW + timedelta(days=60),
        )
    )

    def add(name, owner="p", body=None, **extras):
        value = body if body is not None else {"through_message_id": owner + "-2"}
        if extras.pop("null_body", False):
            value = None
        service = None
        try:
            validated = ChatSessionForkRequest.model_validate(value)
            if bool(validated.through_message_id) != bool(validated.before_message_id):
                service = {
                    "kind": "fork",
                    "session_id": owner,
                    "body": validated.model_dump(mode="json"),
                }
        except ValidationError:
            pass
        item = {
            "name": name,
            "method": "POST",
            "path": f"/api/v1/chat/sessions/{owner}/fork",
            "body": value,
            "service": service,
            "generated_ids": [
                str(UUID(int=100000 + len(cases) * 20 + i)) for i in range(12)
            ],
            **extras,
        }
        if "auth" in item and name != "auth-paired":
            item["service"] = None
        cases.append(item)
        return item

    add("provider-rich-through")
    add("provider-before", body={"before_message_id": "p-2"})
    add("provider-empty-prefix", body={"before_message_id": "p-1"})
    add(
        "provider-title-unicode",
        body={"through_message_id": "p-1", "title": "😀" * 300},
    )
    first = add("provider-repeat")
    add(
        "fork-of-fork",
        owner=first["generated_ids"][0],
        body={"through_message_id": first["generated_ids"][2]},
    )
    add("provider-reopen", action="reopen")
    add("harness-rich-through", "h")
    add("harness-before", "h", body={"before_message_id": "h-1"})
    add(
        "harness-ignores-invalid-goal",
        "bad-harness-goal",
        body={"through_message_id": "bad-harness-goal-1"},
    )
    add("ties-through", "ties", body={"through_message_id": "ties-b"})
    add("ties-before", "ties", body={"before_message_id": "ties-c"})
    add("replaced-boundary", "retracted", body={"through_message_id": "retracted-2"})
    add("replaced-hidden", "retracted", body={"through_message_id": "retracted-3"})
    for owner in [
        "plain",
        "bad-message",
        "bad-decision",
        "duplicate-goal",
        "bad-goal",
        "bad-source",
        "h-running",
        "h-approval",
        "h-starting",
        "h-missing",
        "h-bad",
        "h-lazy",
        "pending",
        "interrupted-idle",
        "retry",
        "bad-recovery",
        "duplicate-pending",
        "failed-answer",
        "repair",
        "repair-hook-failure",
        "h-repair",
    ]:
        add(owner, owner, body={"through_message_id": owner + "-1"})
    add("repair-repeat", "repair", body={"through_message_id": "repair-1"})
    add("harness-invalid-boundary-cleanup", "h", body={"through_message_id": "absent"})
    add("pending-before-boundary", "pending", body={"through_message_id": "absent"})
    add("absent-source", "absent", body={"through_message_id": "absent"})
    add("wrongkind-source", "p-1", body={"through_message_id": "p-1"})
    # Rich provider: Session, two Messages, two Decisions, Goal.
    for name, index, owner in [
        ("provider-session-collision", 0, "p"),
        ("provider-first-message-collision", 1, "p"),
        ("provider-second-message-collision", 2, "p"),
        ("provider-first-decision-collision", 3, "p"),
        ("provider-second-decision-collision", 4, "p"),
        ("provider-goal-collision", 5, "p"),
        ("harness-clone-collision", 0, "h"),
        ("harness-chat-collision", 1, "h"),
        ("harness-second-message-collision", 3, "h"),
        ("harness-decision-collision", 4, "h"),
    ]:
        case = add(name, owner)
        case["generated_ids"][index] = collision
    for name, index, owner in [
        ("provider-session-reversed", 1, "p"),
        ("provider-message-reversed", 3, "p"),
        ("provider-decision-reversed", 7, "p"),
        ("provider-goal-reversed", 11, "p"),
        ("harness-vendor-reversed", 1, "h"),
    ]:
        clocks = [NOW.isoformat()] * 20
        clocks[index] = (NOW - timedelta(microseconds=1)).isoformat()
        add(name, owner, model_clock_values=clocks)
    for vector in request_vectors():
        add(
            "request-" + vector["name"],
            body=vector["input"],
            null_body=vector["input"] is None,
        )
    paired = {
        "Cookie": "nebula_device=fixture-device; nebula_csrf=fixture-csrf",
        "Origin": ORIGIN,
        "X-Nebula-CSRF": "fixture-csrf",
    }
    for name, headers in [
        ("missing", {}),
        ("wrong", {"Authorization": "Bearer wrong"}),
        ("paired", paired),
        ("csrf", {k: v for k, v in paired.items() if k != "X-Nebula-CSRF"}),
        ("origin", {**paired, "Origin": "https://other.invalid"}),
    ]:
        add("auth-" + name, auth={"headers": headers})
    cleanup = add("harness-cleanup-database-failure", "h")
    cleanup["generated_ids"][3] = collision
    cleanup["fault"] = {
        "kind": "fork_cleanup_database_error",
        "harness_session_id": cleanup["generated_ids"][0],
    }
    add(
        "legacy-session-revision-coerces-in-pending-read",
        "legacy-revision-string",
        body={"through_message_id": "legacy-revision-string-1"},
    )
    return projects, rows, overrides, cases


def collect_forks():
    projects, records, overrides, cases = initial_forks()
    obs = ForkObservations()
    obs.clear()
    active = False
    executions = []
    protected_names = [
        "operation_events",
        "run_events",
        "resource_relations",
        "session_projections",
    ]

    def unexpected(*_args, **_kwargs):
        executions.append("unexpected execution")
        raise AssertionError(executions[-1])

    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-forks-oracle-"
    ) as directory:
        path = Path(directory) / "nebula.db"
        database = Database(path)
        store = NebulaStore(database)
        for row in [*projects, *records]:
            store.create(row)
        with sqlite3.connect(path) as raw:
            canonical_payloads = dict(
                raw.execute("SELECT id,payload FROM entities ORDER BY id")
            )
            for identity, changes in overrides.items():
                payload = json.loads(canonical_payloads[identity])
                for key, value in changes.items():
                    if value == "__REMOVE__":
                        payload.pop(key, None)
                    else:
                        payload[key] = value
                raw.execute(
                    "UPDATE entities SET payload=? WHERE id=?",
                    (json.dumps(payload), identity),
                )
            stamp = "2020-01-01 00:00:00.000000"
            raw.execute(
                "INSERT INTO session_projections(session_id,revision,digest) VALUES(?,?,?)",
                ("p", 7, "a" * 64),
            )
            raw.execute(
                "INSERT INTO operation_events(id,operation_id,operation_kind,engagement_id,sequence,event_type,payload,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "protected-operation",
                    "p",
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
                ("protected-event", "p", 1, "fixture.saved", '{"opaque": true}', stamp),
            )
            raw.execute(
                "INSERT INTO resource_relations(id,project_id,source_kind,source_id,predicate,target_kind,target_id,provenance,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "protected-relation",
                    "project",
                    "chat_sessions",
                    "p",
                    "fixture",
                    "chat_sessions",
                    "h",
                    '{"z": 1, "a": 2}',
                    1,
                    stamp,
                    stamp,
                ),
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
                    r["id"]: dict(r)
                    for r in raw.execute("SELECT * FROM entities ORDER BY id")
                }
                envelopes = {
                    identity: {
                        "kind": row["kind"],
                        "payload": json.loads(row["payload"]),
                    }
                    for identity, row in rows.items()
                }
                search = {
                    r["id"]: dict(r)
                    for r in raw.execute("SELECT * FROM search_documents ORDER BY id")
                }
                protected = {
                    name: [
                        dict(r) for r in raw.execute(f"SELECT * FROM {name} ORDER BY 1")
                    ]
                    for name in protected_names
                }
                return envelopes, rows, search, protected

        def changes(before, after):
            return [
                {"before": before.get(key), "after": after.get(key)}
                for key in sorted(before.keys() | after.keys())
                if before.get(key) != after.get(key)
            ]

        def phase(before, action):
            after = snapshot()
            delta = changes(before[0], after[0])
            search = changes(before[2], after[2])
            assert before[3] == after[3], "fork changed protected tables"
            if delta or search:
                obs.commit_phases.append(
                    {
                        "action": action,
                        "changes": delta,
                        "search_changes": search,
                        "protected_sha256": digest(after[3]),
                    }
                )

        original_transaction = NebulaStore.transaction
        original_delete = NebulaStore.delete
        original_get = NebulaStore.get
        original_list = NebulaStore.list_session_entities

        @contextmanager
        def observed_transaction(current):
            before = snapshot() if active else None
            with original_transaction(current) as transaction:
                yield transaction
            if before is not None:
                phase(before, "transaction")

        def observed_delete(current, model, identity, **kwargs):
            before = snapshot() if active else None
            if active:
                obs.storage_trace.append(
                    {"action": "delete", "kind": model.entity_kind, "id": identity}
                )
            result = original_delete(current, model, identity, **kwargs)
            if before is not None:
                phase(before, "delete")
            return result

        def observed_get(current, model, identity):
            if active:
                obs.storage_trace.append(
                    {"action": "get", "kind": model.entity_kind, "id": identity}
                )
            return original_get(current, model, identity)

        def observed_list(current, model, identity, **kwargs):
            if active:
                obs.storage_trace.append(
                    {
                        "action": "list_session",
                        "kind": model.entity_kind,
                        "session_id": identity,
                        **kwargs,
                    }
                )
            return original_list(current, model, identity, **kwargs)

        def inert_chat(current, **kwargs):
            kwargs.update(
                provider_factory=unexpected,
                workspace_resolver=unexpected,
                tool_suggestion_client=unexpected,
                worker_id="inert-fork-oracle",
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
            # Bootstrap only sees canonical rows, then all exact corruption is
            # restored before source requests. No bootstrap/route method mocked.
            with sqlite3.connect(path) as raw:
                saved = dict(raw.execute("SELECT id,payload FROM entities ORDER BY id"))
                for identity in overrides:
                    raw.execute(
                        "UPDATE entities SET payload=? WHERE id=?",
                        (canonical_payloads[identity], identity),
                    )
            try:
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
            finally:
                with sqlite3.connect(path) as raw:
                    for identity in overrides:
                        raw.execute(
                            "UPDATE entities SET payload=? WHERE id=?",
                            (saved[identity], identity),
                        )
            return TestClient(app, raise_server_exceptions=False, base_url=ORIGIN)

        initial, initial_rows, initial_search, initial_protected = snapshot()
        with ExitStack() as guards:
            guards.enter_context(obs.frozen())
            for module in ["api", "chat", "chat_schedules"]:
                guards.enter_context(
                    patch(f"nebula.v3.{module}.utc_now", return_value=NOW)
                )
            guards.enter_context(
                patch("nebula.v3.storage.utc_now", side_effect=obs.writer)
            )
            for module in ["chat", "harnesses"]:
                guards.enter_context(
                    patch(f"nebula.v3.{module}.uuid4", side_effect=obs.uuid)
                )
            for name, method in [
                ("transaction", observed_transaction),
                ("delete", observed_delete),
                ("get", observed_get),
                ("list_session_entities", observed_list),
            ]:
                guards.enter_context(patch.object(NebulaStore, name, new=method))
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
            for method in [
                "create_session",
                "prepare_chat",
                "stream_turn",
                "close_session",
            ]:
                guards.enter_context(
                    patch.object(HarnessRuntimeService, method, side_effect=unexpected)
                )
            client = client_for(store)
            try:
                for case in cases:
                    active = False
                    if case.get("action") == "reopen":
                        client.close()
                        database.dispose()
                        database = Database(path, bootstrap=False)
                        store = NebulaStore(database)
                        client = client_for(store)
                    obs.clear(case)
                    before, before_rows, before_search, before_protected = snapshot()
                    headers = {
                        "Authorization": "Bearer fixture-core",
                        "X-Nebula-Operation-ID": "fixture-operation",
                    }
                    if "auth" in case:
                        headers.pop("Authorization")
                        headers.update(case["auth"]["headers"])
                    fault = case.get("fault")
                    if fault:
                        assert fault["kind"] == "fork_cleanup_database_error"
                        target = str(UUID(fault["harness_session_id"]))
                        with sqlite3.connect(path) as raw:
                            raw.execute(
                                "CREATE TRIGGER fixture_fork_cleanup_failure BEFORE DELETE ON entities "
                                f"WHEN OLD.id='{target}' BEGIN SELECT RAISE(ABORT, "
                                "'fixture fork cleanup failure'); END"
                            )
                    try:
                        active = True
                        response = client.request(
                            case["method"],
                            case["path"],
                            headers=headers,
                            json=case["body"],
                        )
                    finally:
                        active = False
                        if fault:
                            with sqlite3.connect(path) as raw:
                                raw.execute("DROP TRIGGER fixture_fork_cleanup_failure")
                    case["raw_body"] = response.request.content.decode("utf-8")
                    case["expected"] = {
                        "status": response.status_code,
                        "body": normalize(response.json()),
                    }
                    case["expected_cache_control"] = response.headers.get(
                        "cache-control"
                    )
                    case.update(obs.fields())
                    case["expected_storage_trace"] = list(obs.storage_trace)
                    case["expected_commit_phases"] = list(obs.commit_phases)
                    after, after_rows, after_search, after_protected = snapshot()
                    case["expected_changes"] = changes(before, after)
                    case["expected_search_changes"] = changes(
                        before_search, after_search
                    )
                    # Source Session/transcript/provenance rows are immutable.
                    allowed_updates = {r.id for r in records if isinstance(r, ChatTurn)}
                    for identity, row in before_rows.items():
                        if identity not in allowed_updates:
                            assert after_rows.get(identity) == row, (
                                "fork changed retained entity",
                                identity,
                                case["name"],
                            )
                    assert before_protected == after_protected
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
                    assert not executions, "fork attempted external execution"
                final, final_rows, final_search, final_protected = snapshot()
            finally:
                active = False
                client.close()
                database.dispose()
    result = model_contract()
    owned = {
        "chat_sessions",
        "chat_messages",
        "chat_decisions",
        "chat_goals",
        "chat_turns",
        "harness_sessions",
    }
    result.update(
        {
            "normalization": "Only diagnostic request_id/error_id. Model/UUID/writer factories use explicit per-case sequences. Store read/transaction/delete wrappers call real implementations; post-return snapshots observe committed prefixes and cleanup. Source read trace is evidence, not a requirement for a public Rust SQL observer. Canonical rows are temporarily exposed only for inert app bootstrap/reopen and exact raw corruption restored before requests; no malformed-startup claim. Hashes use sorted-key ASCII JSON with comma/colon separators and ID-sorted arrays. No lifespan/provider/harness dispatch/credentials/workspace effects. Source Session/transcript/provenance raw rows and protected ledgers are immutable; existing pending-turn repairs may update Turn rows before refusal.",
            "projects": [r for r in initial.values() if r["kind"] == "engagements"],
            "initial_records": [r for r in initial.values() if r["kind"] in owned],
            "dependency_records": [
                r for r in initial.values() if r["kind"] not in owned | {"engagements"}
            ],
            "raw_payloads": raw_payloads,
            "initial_entity_rows": list(initial_rows.values()),
            "initial_search_documents": list(initial_search.values()),
            "initial_protected_tables": initial_protected,
            "schema_sql": schema_sql,
            "cases": cases,
            "known_unsupported_cases": [],
            "final_records": [r for r in final.values() if r["kind"] in owned],
            "final_dependencies": [
                r for r in final.values() if r["kind"] not in owned | {"engagements"}
            ],
            "final_entity_rows": list(final_rows.values()),
            "final_search_documents": list(final_search.values()),
            "final_protected_tables": final_protected,
            "source_sha256": {
                f"src/nebula/v3/{name}.py": sha256(
                    (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
                ).hexdigest()
                for name in [
                    "api",
                    "chat",
                    "chat_decisions",
                    "chat_goals",
                    "domain",
                    "harnesses",
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
    parser.add_argument("--models-only", action="store_true")
    args = parser.parse_args()
    result = model_contract() if args.models_only else collect_forks()
    args.output.write_text(
        json.dumps(
            result, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
