#!/usr/bin/env python3
"""Capture retained receipt adoption without executing any recovered effect.

Pending response and session-hook reads run over an isolated fixed-clock Core.
Only explicit saved receipt/outcome repair is permitted; providers, workspaces,
adapters and native-hook execution are guarded against invocation.
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
import tempfile
from unittest.mock import patch
from urllib.parse import quote

from fastapi.testclient import TestClient
from keyring.backends.null import Keyring as NullKeyring
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatService
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import (
    ChatSession,
    ChatTurn,
    Engagement,
    NativeHookExecution,
    ToolCall,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_results import ToolResultReceipt, serialize_model_result

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


def receipt(call_id="fixture-call", **fields):
    return {
        "schema": "nebula.tool-result/v2",
        "tool_call_id": call_id,
        "tool_name": "read_fixture",
        "tool_version": "fixture-v1",
        "status": "completed",
        **fields,
    }


def receipt_vectors():
    vectors = []

    def add(name, value):
        try:
            model = ToolResultReceipt.model_validate(value)
            payload = model.as_model_result()
            nonfinite = []

            def finite(item, path):
                if isinstance(item, float) and not math.isfinite(item):
                    nonfinite.append(
                        {
                            "path": path,
                            "value": "NaN"
                            if math.isnan(item)
                            else "Infinity"
                            if item > 0
                            else "-Infinity",
                        }
                    )
                    return None
                if isinstance(item, dict):
                    return {k: finite(v, [*path, k]) for k, v in item.items()}
                if isinstance(item, list):
                    return [finite(v, [*path, n]) for n, v in enumerate(item)]
                return item

            expected = {
                "accepted": True,
                "payload": finite(payload, []),
                "nonfinite_paths": nonfinite,
                "serialized": serialize_model_result(payload),
                "result_summary": ChatService._result_summary(payload),
            }
        except ValueError:
            expected = {"accepted": False}
        vectors.append({"name": name, "input": value, "expected": expected})

    add("minimal-defaults", {k: v for k, v in receipt().items() if k != "schema"})
    add("alias-only", receipt())
    add("unexpected-extra", receipt(unknown=True))
    add("wrong-schema", receipt(schema="nebula.tool-result/v1"))
    alias = receipt()
    alias["schema_"] = alias.pop("schema")
    add("internal-alias-name", alias)
    for field in ["tool_call_id", "tool_name", "tool_version", "status"]:
        value = receipt()
        value.pop(field)
        add("missing-" + field, value)
    for status in ["completed", "failed", "timed_out", "cancelled", "running"]:
        add(
            "status-" + status,
            receipt(
                status=status,
                exit_code=1,
                incomplete=True,
                warnings=["retained warning"],
            ),
        )
    for n, value in enumerate(
        [
            "1_000",
            "1.0",
            "1e3",
            "+01",
            "1__2",
            "١٢",
            "１２",
            " 1.0 ",
            "1_000.0",
            "9" * 40,
            True,
            False,
            1.0,
            1.5,
            1e20,
            None,
            -1,
        ]
    ):
        add(f"integer-coercion-{n}", receipt(exit_code=value))
        add(f"float-coercion-{n}", receipt(timing={"duration_seconds": value}))
    for n, value in enumerate(["nan", "inf", "Infinity", "-inf", "-0.0", "1e400"]):
        add(f"duration-special-{n}", receipt(timing={"duration_seconds": value}))
    add("integer-400-digits-positive", receipt(exit_code="9" * 400))
    add("integer-400-digits-negative", receipt(exit_code="-" + "9" * 400))
    for n, value in enumerate(
        [
            float(-(2**63)),
            math.nextafter(float(-(2**63)), -math.inf),
            math.nextafter(float(2**63), -math.inf),
            float(2**63),
            math.nextafter(float(2**64), -math.inf),
            float(2**64),
            "\u00a01\u00a0",
            "\u20031\u2003",
            "\t1\n",
        ]
    ):
        add(f"integer-boundary-{n}", receipt(exit_code=value))
        add(f"duration-boundary-{n}", receipt(timing={"duration_seconds": value}))
    for n, value in enumerate(
        [
            "y",
            "n",
            "yes",
            "no",
            "true",
            "false",
            "on",
            "off",
            "1",
            "0",
            "YeS",
            " true ",
            "",
            0,
            1,
            1.0,
            0.0,
            2,
            None,
        ]
    ):
        add(f"boolean-coercion-{n}", receipt(incomplete=value, truncated=value))
    for name, values in [
        ("null-summary", {"summary": None}),
        ("summary-max", {"summary": "β" * 1000}),
        ("summary-too-long", {"summary": "β" * 1001}),
        ("numeric-name", {"tool_name": 1}),
        ("null-list", {"warnings": None}),
        ("invalid-warning", {"warnings": [1]}),
        ("warning-count", {"warnings": ["x"] * 21}),
        ("long-warning-bounded", {"warnings": ["β" * 5000]}),
        ("null-next-actions", {"next_actions": None}),
        ("empty-next-actions", {"next_actions": []}),
        ("timing-extra", {"timing": {"unknown": True}}),
        (
            "timing-opaque-strings",
            {
                "timing": {
                    "started_at": "not a timestamp",
                    "completed_at": "also opaque",
                }
            },
        ),
        ("parser-extra", {"parser": {"unknown": True}}),
        (
            "parser-opaque",
            {
                "parser": {
                    "state": "completed",
                    "contract": {"float": 1e-7, "integer": 10**30, "β": [True, None]},
                }
            },
        ),
        ("results-url-max", {"results_url": "x" * 1000}),
        ("results-url-too-long", {"results_url": "x" * 1001}),
    ]:
        add(name, receipt(**values))
    port = {"kind": "network_port", "protocol": "tcp", "port": "443.0", "state": "open"}
    web = {
        "kind": "web_result",
        "rank": True,
        "title": "Retained title",
        "url": "https://example.test/",
    }
    add("network-and-web-coercion", receipt(observations=[port, web]))
    for name, item in [
        ("missing-discriminator", {"protocol": "tcp", "port": 80, "state": "open"}),
        ("unknown-discriminator", {"kind": "unknown"}),
        ("port-zero", {**port, "port": 0}),
        ("port-high", {**port, "port": 65536}),
        ("port-float", {**port, "port": 1.5}),
        ("network-extra", {**port, "extra": 1}),
        ("network-empty-state", {**port, "state": ""}),
        ("web-rank-high", {**web, "rank": 11}),
        ("web-extra", {**web, "extra": 1}),
        ("web-title-long", {**web, "title": "x" * 201}),
    ]:
        add(name, receipt(observations=[item]))
    add("observations-max", receipt(observations=[port] * 100))
    add("observations-too-many", receipt(observations=[port] * 101))
    artifact = {
        "artifact_id": "retained",
        "kind": "stdout",
        "byte_count": True,
        "observed_byte_count": "2.0",
        "sha256": "a" * 64,
    }
    add("artifact-defaults-coercion", receipt(artifacts=[artifact]))
    add(
        "artifact-400-digit-counts",
        receipt(
            artifacts=[
                {
                    **artifact,
                    "byte_count": "9" * 400,
                    "observed_byte_count": "9" * 400,
                }
            ]
        ),
    )
    for name, item in [
        ("artifact-extra", {**artifact, "extra": True}),
        ("artifact-negative", {**artifact, "byte_count": -1}),
        ("artifact-hash-uppercase", {**artifact, "sha256": "A" * 64}),
        ("artifact-hash-short", {**artifact, "sha256": "a" * 63}),
        ("artifact-bad-kind", {**artifact, "kind": "file"}),
        ("artifact-long-name", {**artifact, "filename": "x" * 301}),
    ]:
        add(name, receipt(artifacts=[item]))
    add("artifacts-max", receipt(artifacts=[artifact] * 12))
    add("artifacts-too-many", receipt(artifacts=[artifact] * 13))
    return vectors


def serialization_vectors():
    values = [
        ("empty", {}),
        ("ordinary", {"β": "🦀\n\u0001", "z": [True, None, 1.0], "a": {"b": 2}}),
        ("numbers", {"values": [0.0, -0.0, 1e-7, 1e-6, 1e15, 1e16, 10**30]}),
        ("exact-byte-bound", {"x": "a" * 8183}),
        ("one-byte-over", {"x": "a" * 8184}),
        ("utf8-over", {"x": "β" * 4092}),
        ("summary-generic", {"z": 1, "a": 2, "h": 3, "d": 4, "b": 5, "c": 6, "f": 7}),
    ]
    values += [
        ("receipt-" + state, receipt(status=state, exit_code=None))
        for state in ["completed", "failed", "timed_out", "cancelled"]
    ]
    values += [
        ("summary-incomplete", receipt(incomplete=True, warnings=["warning"])),
        ("summary-warnings", receipt(warnings=["warning"])),
    ]
    return [
        {
            "name": name,
            "input": value,
            "expected": serialize_model_result(value),
            "result_summary": ChatService._result_summary(value),
        }
        for name, value in values
    ]


def initial_recovery():
    records, dependencies, identities = [], [], []

    def session(identity):
        records.append(
            ChatSession(
                id=identity,
                engagement_id="project",
                title=identity,
                model="fixture",
                provider_profile_id="fixture-provider",
                created_at=BASE,
                updated_at=BASE,
            )
        )
        identities.append(identity)

    def turn(identity, owner, **fields):
        stamp = BASE + timedelta(seconds=fields.pop("seconds", 1))
        record = ChatTurn(
            id=identity,
            engagement_id=fields.pop("engagement_id", "project"),
            session_id=owner,
            provider_profile_id="fixture-provider",
            model="fixture",
            created_at=stamp,
            updated_at=stamp,
            **fields,
        )
        records.append(record)
        return record

    def interrupted(owner, tools=None, hooks=None, **fields):
        session(owner)
        recovery = {
            "required": True,
            "automatic_retry_pending": True,
            "unknown_tool_call_ids": tools or [],
            "unknown_hook_execution_ids": hooks or [],
            **fields.pop("recovery", {}),
        }
        return turn(
            owner + "-turn",
            owner,
            status="interrupted",
            error="Saved interrupted outcome",
            request_snapshot={"recovery": recovery},
            **fields,
        )

    def call(identity, owner, **fields):
        record = ToolCall(
            id=identity,
            engagement_id=fields.pop("engagement_id", "project"),
            run_id=owner + "-turn",
            origin="chat",
            chat_session_id=owner,
            chat_turn_id=fields.pop("chat_turn_id", owner + "-turn"),
            tool_name="read_fixture",
            status=fields.pop("status", "complete"),
            risk_class="local_read",
            arguments=fields.pop("arguments", {"path": "retained.txt"}),
            result=fields.pop("result", receipt(identity)),
            metadata=fields.pop(
                "metadata",
                {"provider_step": 1, "provider_call_id": "provider-" + identity},
            ),
            created_at=BASE,
            updated_at=BASE,
            **fields,
        )
        dependencies.append(record)
        return record

    def hook(identity, owner, **fields):
        status = fields.pop("status", "complete")
        record = NativeHookExecution(
            id=identity,
            engagement_id=fields.pop("engagement_id", "project"),
            chat_session_id=fields.pop("chat_session_id", owner),
            chat_turn_id=fields.pop("chat_turn_id", owner + "-turn"),
            hook_id="retained-hook",
            hook_snapshot={"fixture": "never execute"},
            event_name="Stop",
            status=status,
            started_at=fields.pop("started_at", BASE),
            completed_at=None if status == "running" else BASE + timedelta(seconds=1),
            exit_code=fields.pop("exit_code", 0),
            stdout="Private saved output",
            stderr="Private saved stderr",
            created_at=BASE,
            updated_at=BASE,
            **fields,
        )
        dependencies.append(record)
        return record

    session("idle")
    for status in [
        "routing",
        "waiting_approval",
        "waiting_callback",
        "finalizing",
        "complete",
        "failed",
        "cancelled",
    ]:
        session("state-" + status)
        turn("state-" + status + "-turn", "state-" + status, status=status)
    for index, recovery in enumerate(
        [
            {},
            {"required": False},
            {"required": "false"},
            {"automatic_retry_pending": [0]},
            {"required": 0, "automatic_retry_pending": {}},
            None,
            [],
            1,
            "required",
        ]
    ):
        owner = f"recovery-shape-{index}"
        session(owner)
        turn(
            owner + "-turn",
            owner,
            status="interrupted",
            request_snapshot={"recovery": recovery},
        )
    for index, attempts in enumerate([1, 2, True, "2", None, 10**30]):
        owner = f"final-answer-{index}"
        session(owner)
        turn(
            owner + "-turn",
            owner,
            status="failed",
            request_snapshot={"final_answer_recovery": {"attempts": attempts}},
        )
    for final in ["", "saved-message"]:
        owner = "final-answer-present-" + ("empty" if not final else "saved")
        session(owner)
        turn(
            owner + "-turn",
            owner,
            status="failed",
            final_message_id=final,
            request_snapshot={"final_answer_recovery": {"attempts": 2}},
        )
    session("latest-wins")
    turn(
        "latest-wins-old",
        "latest-wins",
        status="failed",
        request_snapshot={"final_answer_recovery": {"attempts": 2}},
    )
    turn("latest-wins-new", "latest-wins", status="complete", seconds=2)
    session("duplicate-pending")
    turn("duplicate-pending-a", "duplicate-pending")
    turn("duplicate-pending-b", "duplicate-pending", engagement_id="foreign")
    session("callback-summary")
    turn(
        "callback-summary-turn",
        "callback-summary",
        status="waiting_callback",
        tool_history=[
            {
                "status": "waiting_callback",
                "results_url": "retained://results",
                "process_id": "old",
            },
            {
                "status": "waiting_callback",
                "results_url": "",
                "process_id": {"opaque": True},
            },
        ],
    )
    interrupted("nonstring-unknown", tools=[0, False, {}, ["missing"]], hooks=[None, 1])
    interrupted(
        "missing-effects",
        tools=["missing-call", "idle"],
        hooks=["missing-hook", "idle"],
    )
    for label, status in [
        ("complete", "completed"),
        ("failed", "failed"),
        ("timed-out", "timed_out"),
        ("cancelled", "cancelled"),
    ]:
        owner = "adopt-" + label
        identity = owner + "-call"
        interrupted(owner, tools=[identity])
        call(
            identity,
            owner,
            status="complete" if status == "completed" else "failed",
            result=receipt(
                identity, status=status, exit_code=0 if status == "completed" else 7
            ),
        )
    rejected = [
        ("wrong-owner", {"chat_turn_id": "other-turn"}),
        ("running", {"status": "running"}),
        ("result-list", {"result": []}),
        ("result-string", {"result": "retained"}),
        ("result-null", {"result": None}),
        ("missing-schema", {"result": {"status": "completed"}}),
        ("wrong-schema", {"result": receipt(schema="old")}),
        ("wrong-id", {"result": receipt("different-call")}),
        ("wrong-name", {"result_fields": {"tool_name": "different"}}),
        ("background", {"result_fields": {"results_url": "retained://pending"}}),
        ("extra-receipt", {"result_fields": {"unexpected": True}}),
        ("complete-failed", {"result_fields": {"status": "failed"}}),
        ("failed-completed", {"status": "failed"}),
        ("step-bool", {"metadata_fields": {"provider_step": True}}),
        ("step-negative", {"metadata_fields": {"provider_step": -1}}),
        ("step-float", {"metadata_fields": {"provider_step": 1.0}}),
        ("step-string", {"metadata_fields": {"provider_step": "1"}}),
        ("step-null", {"metadata_fields": {"provider_step": None}}),
        ("model-call-empty", {"metadata_fields": {"provider_call_id": ""}}),
        ("model-call-number", {"metadata_fields": {"provider_call_id": 1}}),
    ]
    for label, supplied in rejected:
        owner = "reject-" + label
        identity = owner + "-call"
        fields = deepcopy(supplied)
        interrupted(owner, tools=[identity])
        if "result_fields" in fields:
            fields["result"] = receipt(identity, **fields.pop("result_fields"))
        if "metadata_fields" in fields:
            fields["metadata"] = {
                "provider_step": 1,
                "provider_call_id": "provider-" + identity,
                **fields.pop("metadata_fields"),
            }
        call(identity, owner, **fields)
    for label, budget in [
        ("artifact", "artifact_query"),
        ("unknown", "other"),
        ("null", None),
    ]:
        owner = "budget-" + label
        identity = owner + "-call"
        interrupted(owner, tools=[identity, identity])
        call(
            identity,
            owner,
            metadata={
                "provider_step": 0,
                "provider_call_id": "provider",
                "budget_class": budget,
            },
        )
    owner = "opaque-intent"
    identity = owner + "-call"
    interrupted(
        owner,
        tools=[identity],
        recovery={"recorded_tool_result_ids": [True, 1, 1.0, "prior"]},
    )
    call(
        identity,
        owner,
        arguments={"flag": 1},
        metadata={
            "provider_step": 1,
            "provider_call_id": "provider",
            "provider_history_intent": {
                "step": True,
                "model_call_id": "provider",
                "tool_call_id": identity,
                "name": "read_fixture",
                "arguments": {"flag": True},
                "opaque": {"β": [None]},
                "budget_class": "artifact_query",
            },
        },
    )
    owner = "existing-history"
    identity = owner + "-call"
    interrupted(
        owner,
        tools=[identity],
        execution_tool_calls=7,
        artifact_queries=8,
        next_step=50,
        tool_call_ids=[identity, identity],
        tool_history=[
            {"tool_call_id": "unrelated", "step": "2_0", "opaque": True},
            {
                "tool_call_id": identity,
                "step": " +02 ",
                "name": "retained-existing",
                "budget_class": "execution",
                "opaque": "first",
            },
            {"tool_call_id": identity, "step": -1, "opaque": "duplicate removed"},
        ],
    )
    call(
        identity,
        owner,
        result=receipt(identity, results_url="", warnings=["β" * 5000]),
        result_artifact_id="retained-artifact",
    )
    for label, step in [
        ("float", 1.9),
        ("bool", True),
        ("unicode", "١٢"),
        ("invalid", "1.5"),
        ("null", None),
        ("list", []),
    ]:
        owner = "history-step-" + label
        identity = owner + "-call"
        interrupted(
            owner,
            tools=[identity],
            tool_history=[{"tool_call_id": "retained-other", "step": step}],
        )
        call(identity, owner)
    for label, prior in [("dict", [{}]), ("list", [[]]), ("scalar", "prior")]:
        owner = "recorded-ids-" + label
        identity = owner + "-call"
        interrupted(
            owner, tools=[identity], recovery={"recorded_tool_result_ids": prior}
        )
        call(identity, owner)
    owner = "partial-tools"
    identity = owner + "-call"
    interrupted(owner, tools=[identity, "missing", 0])
    call(identity, owner)
    owner = "foreign-tool"
    identity = owner + "-call"
    interrupted(owner, tools=[identity])
    call(identity, owner, engagement_id="foreign")
    for label, status, exit_code, late in [
        ("complete", "complete", 0, None),
        ("nonzero", "complete", 1, None),
        ("failed", "failed", 0, None),
        ("running", "running", 0, None),
        ("late-success", "interrupted", None, {"status": "complete", "exit_code": 0}),
        ("late-nonzero", "interrupted", None, {"status": "complete", "exit_code": 2}),
        ("late-failed", "interrupted", None, {"status": "failed", "exit_code": 0}),
        ("late-timeout", "interrupted", None, {"status": "timed_out", "exit_code": 0}),
    ]:
        owner = "hook-" + label
        identity = owner + "-record"
        interrupted(owner, hooks=[identity])
        hook(
            identity,
            owner,
            status=status,
            exit_code=exit_code,
            late_outcome={
                **late,
                "observed_at": BASE + timedelta(seconds=3),
                "stdout": "Retained late β output",
                "stderr": "Retained late stderr",
            }
            if late
            else None,
        )
    for owner, tools, prior in [
        ("duplicate-late-hook", False, []),
        ("tool-commit-hook-conflict", True, []),
        ("tool-commit-hook-error", True, [{}]),
    ]:
        hook_id = owner + "-hook"
        tool_id = owner + "-call"
        interrupted(
            owner,
            tools=[tool_id] if tools else [],
            hooks=[hook_id, hook_id] if not prior else [hook_id],
            recovery={"recorded_hook_outcome_ids": prior},
        )
        if tools:
            call(tool_id, owner)
        hook(
            hook_id,
            owner,
            status="interrupted",
            exit_code=None,
            late_outcome={
                "status": "complete",
                "exit_code": 0,
                "observed_at": BASE + timedelta(seconds=3),
            },
        )
    owner = "both-phases"
    tool_id = owner + "-call"
    hook_id = owner + "-hook"
    interrupted(owner, tools=[tool_id], hooks=[hook_id])
    call(tool_id, owner)
    hook(
        hook_id,
        owner,
        status="interrupted",
        exit_code=None,
        late_outcome={
            "status": "complete",
            "exit_code": 0,
            "observed_at": BASE + timedelta(seconds=3),
        },
    )
    owner = "wrong-hook-owner"
    hook_id = owner + "-hook"
    interrupted(owner, hooks=[hook_id])
    hook(hook_id, owner, chat_turn_id="other-turn")
    owner = "foreign-hook"
    hook_id = owner + "-hook"
    interrupted(owner, hooks=[hook_id])
    hook(hook_id, owner, engagement_id="foreign", chat_session_id="other-session")
    session("pending-beats-latest")
    turn("pending-beats-latest-turn", "pending-beats-latest", status="routing")
    turn(
        "pending-beats-latest-new", "pending-beats-latest", status="complete", seconds=2
    )
    hook("pending-hook", "pending-beats-latest")
    hook("newer-hook", "pending-beats-latest", chat_turn_id="pending-beats-latest-new")
    session("latest-hooks")
    turn("latest-hooks-old", "latest-hooks", status="complete")
    turn("latest-hooks-turn", "latest-hooks", status="failed", seconds=2)
    hook("latest-hook", "latest-hooks")
    hook("older-hook", "latest-hooks", chat_turn_id="latest-hooks-old")
    session("mixed-hooks")
    turn("mixed-hooks-turn", "mixed-hooks", status="complete")
    hook("mixed-hook-aware", "mixed-hooks")
    hook("mixed-hook-naive", "mixed-hooks", started_at=BASE.replace(tzinfo=None))
    projects = [
        Engagement(
            id="project",
            name="Retained receipt recovery",
            created_at=BASE,
            updated_at=BASE,
        )
    ]
    return projects, records, dependencies, identities


def recovery_cases(identities):
    cases = []
    for identity in [*identities, "missing", "state-routing-turn", "β" * 201]:
        for endpoint in (
            ["hooks", "pending-turn"]
            if identity in {"both-phases", "hook-late-success"}
            else ["pending-turn", "hooks"]
        ):
            cases.append(
                {
                    "name": identity + "-" + endpoint,
                    "method": "GET",
                    "body": None,
                    "path": "/api/v1/chat/sessions/"
                    + quote(identity, safe="")
                    + "/"
                    + endpoint,
                    "service": {"session_id": identity, "endpoint": endpoint},
                }
            )
    for identity in [
        "adopt-complete",
        "both-phases",
        "hook-late-success",
        "tool-commit-hook-conflict",
        "partial-tools",
    ]:
        for endpoint in ["pending-turn", "hooks"]:
            cases.append(
                {
                    "name": identity + "-reopen-" + endpoint,
                    "method": "GET",
                    "body": None,
                    "path": "/api/v1/chat/sessions/" + identity + "/" + endpoint,
                    "service": {"session_id": identity, "endpoint": endpoint},
                    "action": "reopen",
                }
            )
    for endpoint in ["pending-turn", "hooks"]:
        for identity in ["idle", "missing"]:
            cases.append(
                {
                    "name": identity + "-no-auth-" + endpoint,
                    "method": "GET",
                    "body": None,
                    "path": "/api/v1/chat/sessions/" + identity + "/" + endpoint,
                    "service": None,
                    "auth": {"headers": {}},
                }
            )
    return cases


def collect_recovery():
    projects, records, dependencies, identities = initial_recovery()
    cases = recovery_cases(identities)
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-recovery-oracle-"
    ) as directory:
        path = Path(directory) / "nebula.db"
        database = Database(path)
        store = NebulaStore(database)
        for record in [*projects, *records, *dependencies]:
            store.create(record)
        with sqlite3.connect(path) as raw:
            raw_payloads = dict(
                raw.execute(
                    "SELECT id,payload FROM entities WHERE kind='chat_turns' ORDER BY id"
                )
            )
        phase_vectors = {"tool": [], "hook": []}
        seen_phases = set()
        current_case = [""]

        def unexpected(*_args, **_kwargs):
            raise AssertionError("retained receipt observation attempted execution")

        def inert_chat(current, **kwargs):
            kwargs.update(
                provider_factory=unexpected,
                workspace_resolver=unexpected,
                tool_suggestion_client=unexpected,
                worker_id="inert-oracle",
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

        def snapshot():
            with sqlite3.connect(path) as raw:
                envelopes = {
                    identity: {"kind": kind, "payload": json.loads(payload)}
                    for identity, kind, payload in raw.execute(
                        "SELECT id,kind,payload FROM entities ORDER BY id"
                    )
                }
                rows = {
                    row[0]: row
                    for row in raw.execute("SELECT * FROM entities ORDER BY id")
                }
                others = {
                    table: raw.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    for table in [
                        "operation_events",
                        "run_events",
                        "resource_relations",
                        "session_projections",
                        "search_documents",
                    ]
                }
                return envelopes, rows, others

        def digest(envelopes):
            return sha256(
                json.dumps(
                    list(envelopes.values()),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()

        def phase_records(turn_id, phase):
            with sqlite3.connect(path) as raw:
                kind, payload = raw.execute(
                    "SELECT kind,payload FROM entities WHERE id=?", (turn_id,)
                ).fetchone()
                turn_row = {"kind": kind, "payload": json.loads(payload)}
                recovery = turn_row["payload"]["request_snapshot"].get("recovery")
                unknown = (
                    recovery.get(
                        "unknown_tool_call_ids"
                        if phase == "tool"
                        else "unknown_hook_execution_ids"
                    )
                    if isinstance(recovery, dict)
                    else None
                )
                ids = (
                    sorted({item for item in unknown if isinstance(item, str)})
                    if isinstance(unknown, list)
                    else []
                )
                dependencies = [
                    {"kind": kind, "payload": json.loads(payload)}
                    for kind, payload in raw.execute(
                        "SELECT kind,payload FROM entities WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id",
                        (json.dumps(ids),),
                    )
                ]
                return turn_row, dependencies

        def wrap_phase(phase, original):
            def observe(service, turn_id):
                key = phase, turn_id
                if key in seen_phases:
                    return original(service, turn_id)
                seen_phases.add(key)
                turn_row, dependencies = phase_records(turn_id, phase)
                before = {
                    row["payload"]["id"]: row for row in [turn_row, *dependencies]
                }
                vector = {
                    "name": current_case[0],
                    "turn": turn_row,
                    "dependencies": dependencies,
                }
                try:
                    result = original(service, turn_id)
                    vector["expected"] = {"turn": envelope(result)}
                    return result
                except Exception as error:
                    vector["expected"] = {
                        "error": {"type": type(error).__name__, "detail": str(error)}
                    }
                    raise
                finally:
                    with sqlite3.connect(path) as raw:
                        after = {
                            identity: {"kind": kind, "payload": json.loads(payload)}
                            for identity, kind, payload in raw.execute(
                                "SELECT id,kind,payload FROM entities WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id",
                                (json.dumps(list(before)),),
                            )
                        }
                    vector["expected"]["changes"] = [
                        {"before": before[identity], "after": after[identity]}
                        for identity in sorted(before)
                        if before[identity] != after[identity]
                    ]
                    phase_vectors[phase].append(vector)

            return observe

        client = client_for(store)
        try:
            with ExitStack() as guards:
                guards.enter_context(
                    patch("nebula.v3.storage.utc_now", return_value=NOW)
                )
                for phase, method in [
                    ("tool", "_reconcile_recorded_tool_results"),
                    ("hook", "_reconcile_recorded_hook_outcomes"),
                ]:
                    guards.enter_context(
                        patch.object(
                            ChatService,
                            method,
                            new=wrap_phase(phase, getattr(ChatService, method)),
                        )
                    )
                for method in [
                    "prepare",
                    "prepare_async",
                    "prepare_resume",
                    "stream",
                    "_run_native_hooks",
                    "resume_turns_stopped_by_core",
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
                    current_case[0] = case["name"]
                    if case.get("action") == "reopen":
                        client.close()
                        database.dispose()
                        database = Database(path, bootstrap=False)
                        store = NebulaStore(database)
                        client = client_for(store)
                    before, before_rows, before_other = snapshot()
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
                    after, after_rows, after_other = snapshot()
                    assert before.keys() == after.keys(), (
                        "recovery must not create or delete entities"
                    )
                    changes = [
                        {"before": before[identity], "after": after[identity]}
                        for identity in before
                        if before[identity] != after[identity]
                    ]
                    changed_ids = {entry["after"]["payload"]["id"] for entry in changes}
                    assert changed_ids == {
                        identity
                        for identity in before_rows
                        if before_rows[identity] != after_rows[identity]
                    }, "unexpected envelope-only mutation"
                    assert all(
                        entry["after"]["kind"]
                        in {"chat_turns", "native_hook_executions"}
                        for entry in changes
                    )
                    assert before_other == after_other, (
                        "recovery changed a ledger, projection, relation or search row"
                    )
                    case["expected_changes"] = changes
                    case["before_snapshot_sha256"] = digest(before)
                    case["after_snapshot_sha256"] = digest(after)
            final, _, _ = snapshot()
        finally:
            client.close()
            database.dispose()
    return {
        "format": "nebula.assistant-recovery-oracle/v1",
        "clock": NOW.isoformat(),
        "origin": ORIGIN,
        "normalization": "Only request_id/error_id. Entity update timestamps use the fixed trusted storage clock. Snapshot SHA256 covers ID-sorted envelope arrays using Python sorted-key ASCII JSON with separators comma/colon; nonfinite expected receipt values use null plus explicit nonfinite_paths.",
        "projects": [envelope(record) for record in projects],
        "initial_records": [envelope(record) for record in records],
        "dependency_records": [envelope(record) for record in dependencies],
        "raw_payloads": raw_payloads,
        "dependency_schemas": {
            model.entity_kind: model.model_json_schema()
            for model in [ToolCall, NativeHookExecution]
        },
        "receipt_schema": ToolResultReceipt.model_json_schema(),
        "receipt_vectors": receipt_vectors(),
        "serialization_vectors": serialization_vectors(),
        "tool_repair_vectors": phase_vectors["tool"],
        "hook_repair_vectors": phase_vectors["hook"],
        "cases": cases,
        "final_records": [
            row
            for row in final.values()
            if row["kind"] in {"chat_sessions", "chat_turns"}
        ],
        "final_dependencies": [
            row
            for row in final.values()
            if row["kind"] in {"tool_calls", "native_hook_executions"}
        ],
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in ["api", "chat", "domain", "storage", "database", "tool_results"]
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = collect_recovery()
    args.output.write_text(
        json.dumps(
            fixture, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(fixture["cases"]),
                "records": len(fixture["initial_records"]),
                "dependencies": len(fixture["dependency_records"]),
                "receipt_vectors": len(fixture["receipt_vectors"]),
                "sha256": sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
