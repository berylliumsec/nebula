#!/usr/bin/env python3
"""Capture Assistant settings/schedule mutations over inert isolated SQLite.

No lifespan, provider, MCP process, hook, workspace or scheduler is executed.
Real HTTP requests retain source validation, commit order and search projections.
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
from uuid import UUID

from fastapi.testclient import TestClient
from keyring.backends.null import Keyring as NullKeyring
from pydantic import ValidationError

from nebula.v3.api import ChatSessionUpdateRequest, create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatService
from nebula.v3.chat_schedules import ScheduleCreate, ScheduleWrite
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import (
    ChatSchedule,
    ChatSession,
    ChatTurn,
    Engagement,
    McpServerProfile,
    NativeHookExecution,
    PairedDeviceSession,
    ToolCall,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.storage import ConflictError, NebulaStore, StoreTransaction

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
ORIGIN = "https://nebula.test:9443"
REQUEST_MODELS = {
    "session_patch": ChatSessionUpdateRequest,
    "schedule_create": ScheduleCreate,
    "schedule_write": ScheduleWrite,
}


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


def validation_vector(name, model, value):
    try:
        result = model.model_validate(value)
        expected = {
            "accepted": True,
            "payload": result.model_dump(mode="json"),
            "fields_set": sorted(result.model_fields_set),
        }
    except ValidationError as error:
        # Context contains ValueError objects; the HTTP encoder renders them {}.
        from fastapi.encoders import jsonable_encoder

        expected = {
            "accepted": False,
            "errors": jsonable_encoder(error.errors(include_url=False)),
        }
    return {"name": name, "input": deepcopy(value), "expected": expected}


def profile_input(**changes):
    return {
        "id": "mcp-vector",
        "created_at": BASE.isoformat(),
        "updated_at": BASE.isoformat(),
        "revision": 1,
        "name": "fixture",
        "transport": "streamable_http",
        "url": "https://mcp.invalid/rpc",
        **changes,
    }


def mcp_profile_vectors():
    vectors = []

    def add(vector_name, **changes):
        vectors.append(
            validation_vector(vector_name, McpServerProfile, profile_input(**changes))
        )

    add("http-defaults")
    add("http-enabled", enabled=True)
    add(
        "trimmed-values",
        id=" mcp-vector ",
        name=" fixture ",
        url=" https://mcp.invalid/rpc ",
        enabled_tools=[" read "],
        header_secret_refs={" X-Test ": " env:FIXTURE "},
    )
    add(
        "numeric-and-boolean-coercion",
        startup_timeout_seconds=" 1_000.0 ",
        tool_timeout_seconds=" 8.5 ",
        enabled="true",
    )
    add(
        "numeric-coercion-valid",
        startup_timeout_seconds=" 10.5 ",
        tool_timeout_seconds=1,
        enabled="yes",
        required=0,
    )
    add("timeout-bool", startup_timeout_seconds=True, tool_timeout_seconds=True)
    for name, fields in [
        (
            "stdio-disabled",
            {"transport": "stdio", "command": "/usr/bin/fixture", "url": None},
        ),
        (
            "stdio-enabled-trusted",
            {
                "transport": "stdio",
                "command": "/usr/bin/fixture",
                "url": None,
                "enabled": True,
                "trusted_stdio": True,
            },
        ),
        (
            "stdio-untrusted",
            {
                "transport": "stdio",
                "command": "/usr/bin/fixture",
                "url": None,
                "enabled": True,
            },
        ),
        ("stdio-no-command", {"transport": "stdio", "url": None}),
        (
            "stdio-relative-command",
            {"transport": "stdio", "command": "fixture", "url": None},
        ),
        ("stdio-with-url", {"transport": "stdio", "command": "/fixture"}),
        (
            "stdio-auth",
            {
                "transport": "stdio",
                "command": "/fixture",
                "url": None,
                "auth_mode": "bearer",
                "bearer_secret_ref": "env:FIXTURE",
            },
        ),
        (
            "stdio-fixed-cwd",
            {
                "transport": "stdio",
                "command": "/fixture",
                "url": None,
                "cwd_policy": "fixed",
                "cwd": "/fixture/../kept",
            },
        ),
        (
            "stdio-fixed-missing-cwd",
            {
                "transport": "stdio",
                "command": "/fixture",
                "url": None,
                "cwd_policy": "fixed",
            },
        ),
        (
            "stdio-relative-cwd",
            {
                "transport": "stdio",
                "command": "/fixture",
                "url": None,
                "cwd_policy": "fixed",
                "cwd": "relative",
            },
        ),
        (
            "stdio-workspace-cwd",
            {
                "transport": "stdio",
                "command": "/fixture",
                "url": None,
                "cwd": "/fixture",
            },
        ),
        (
            "stdio-environment",
            {
                "transport": "stdio",
                "command": "/fixture",
                "url": None,
                "environment": {" PATH ": " /fixture "},
                "environment_secret_refs": {" TOKEN ": " env:FIXTURE "},
            },
        ),
        ("http-process-command", {"command": "/fixture"}),
        ("http-process-arguments", {"arguments": ["--fixture"]}),
        ("http-process-environment", {"environment": {"PATH": "/fixture"}}),
        (
            "http-process-secret-environment",
            {"environment_secret_refs": {"TOKEN": "env:FIXTURE"}},
        ),
        ("http-process-cwd", {"cwd": "/fixture"}),
        (
            "bearer-reference",
            {"auth_mode": "bearer", "bearer_secret_ref": "env:FIXTURE"},
        ),
        ("bearer-missing", {"auth_mode": "bearer"}),
        (
            "headers-reference",
            {
                "auth_mode": "headers",
                "header_secret_refs": {"X-Fixture": "vault:" + "a" * 32},
            },
        ),
        ("headers-missing", {"auth_mode": "headers"}),
        ("session-reference", {"bearer_secret_ref": "session:" + "b" * 32}),
        ("invalid-secret-reference", {"bearer_secret_ref": "literal-secret"}),
        ("invalid-secret-uppercase-hex", {"bearer_secret_ref": "vault:" + "A" * 32}),
        ("invalid-header-name", {"header_secret_refs": {"X Bad": "env:FIXTURE"}}),
        ("invalid-header-secret", {"header_secret_refs": {"X-Good": "not-a-ref"}}),
        ("invalid-env-name", {"environment_secret_refs": {"1BAD": "env:FIXTURE"}}),
        ("invalid-literal-env-name", {"environment": {"BAD-NAME": "fixture"}}),
        ("literal-env-secret", {"environment": {"MY_API_KEY": "fixture"}}),
        ("literal-env-limit", {"environment": {"PATH": "x" * 8193}}),
        ("argument-limit", {"arguments": ["x" * 8193]}),
        ("tools-overlap", {"enabled_tools": ["read"], "disabled_tools": [" read "]}),
        ("timeout-zero", {"startup_timeout_seconds": 0}),
        ("timeout-upper", {"startup_timeout_seconds": 121}),
        ("tool-timeout-upper", {"tool_timeout_seconds": 901}),
        ("timeout-nan", {"startup_timeout_seconds": "nan"}),
        ("timeout-infinity", {"tool_timeout_seconds": "inf"}),
        (
            "nested-capability",
            {
                "capabilities": {
                    "checked_at": "2030-01-01T13:00:00+01:00",
                    "tools": [
                        {
                            "name": " read ",
                            "read_only": "yes",
                            "input_schema": {"opaque": [1, True]},
                        }
                    ],
                }
            },
        ),
        (
            "nested-naive-capability-time",
            {"capabilities": {"checked_at": "2030-01-01T12:00:00"}},
        ),
        (
            "nested-zero-offset-capability-time",
            {"capabilities": {"checked_at": "2030-01-01T12:00:00+00:00"}},
        ),
        (
            "nested-fraction-offset-capability-time",
            {"capabilities": {"checked_at": "2030-01-01T12:00:00.123456789+01:00"}},
        ),
        (
            "nested-fraction-naive-capability-time",
            {"capabilities": {"checked_at": "2030-01-01T12:00:00.123456789"}},
        ),
        ("nested-extra", {"capabilities": {"extra": True}}),
        ("extra-field", {"extra": True}),
        ("name-pattern", {"name": "bad name"}),
        ("bad-transport", {"transport": "sse"}),
    ]:
        add(name, **fields)
    for name, url in [
        ("loopback", "http://localhost/rpc"),
        ("loopback-uppercase", "HTTP://LOCALHOST/rpc"),
        ("loopback-dot", "http://localhost./rpc"),
        ("loopback-ipv4", "http://127.0.0.1/rpc"),
        ("loopback-ipv6", "http://[::1]/rpc"),
        ("nonloopback-http", "http://example.invalid/rpc"),
        ("scheme-reject", "ws://localhost/rpc"),
        ("missing-host", "https:///rpc"),
        ("empty-userinfo", "https://@example.invalid/rpc"),
        ("empty-user-and-password", "https://:@example.invalid/rpc"),
        ("username", "https://user@example.invalid/rpc"),
        ("password", "https://:password@example.invalid/rpc"),
        ("query", "https://example.invalid/rpc?x=1"),
        ("fragment", "https://example.invalid/rpc#fragment"),
        ("empty-query-fragment", "https://example.invalid/rpc?#"),
        ("invalid-port", "https://example.invalid:bad/rpc"),
        ("out-of-range-port", "https://example.invalid:65536/rpc"),
        ("unclosed-bracket", "https://[::1/rpc"),
        ("invalid-bracket-host", "https://[example.invalid]/rpc"),
        ("internal-control", "https://local\thost/rpc"),
        ("unix-authority", "http+unix://%2Ftmp%2Fsock/rpc"),
    ]:
        add("url-" + name, url=url)
    add(
        "trimmed-key-collision",
        header_secret_refs={" X-Test ": "env:FIRST", "X-Test": "env:LAST"},
    )
    vectors[-1]["raw_input"] = json.dumps(vectors[-1]["input"], ensure_ascii=False)
    return vectors


def request_vectors():
    inputs = {
        "session_patch": [
            ("empty", {}),
            ("null-title", {"title": None}),
            ("only-revision", {"expected_revision": 1}),
            ("trim-title", {"title": "  renamed β  "}),
            ("blank-title", {"title": " \t "}),
            ("title-limit", {"title": "x" * 301}),
            ("title-number", {"title": 123}),
            ("explicit-null-effort", {"reasoning_effort": None}),
            ("unknown-effort", {"reasoning_effort": "unknown"}),
            ("trim-effort", {"reasoning_effort": " high "}),
            ("explicit-null-limit", {"max_active_subagents": None}),
            ("coerced-limit", {"max_active_subagents": " 1.0 "}),
            ("bool-limit", {"max_active_subagents": True}),
            ("limit-low", {"max_active_subagents": 0}),
            ("limit-high", {"max_active_subagents": 101}),
            ("coerced-booleans", {"archived": "yes", "allow_agent_messaging": 0}),
            ("spaced-boolean", {"archived": " true "}),
            ("clear-lists", {"mcp_server_ids": [], "hook_ids": []}),
            ("trimmed-list", {"mcp_server_ids": [" mcp-http "]}),
            ("duplicate-mcp", {"mcp_server_ids": ["a", " a "]}),
            ("duplicate-hooks", {"hook_ids": ["a", " a "]}),
            ("mcp-list-limit", {"mcp_server_ids": [str(i) for i in range(65)]}),
            ("hook-list-limit", {"hook_ids": [str(i) for i in range(33)]}),
            ("list-number", {"hook_ids": [1]}),
            (
                "mcp-length-and-first-item",
                {"mcp_server_ids": [1, *[str(i) for i in range(64)]]},
            ),
            (
                "hook-length-and-last-item",
                {"hook_ids": [*[str(i) for i in range(32)], 1]},
            ),
            (
                "blank-provider-model",
                {"subagent_provider_id": "  ", "subagent_model": ""},
            ),
            ("bad-revision", {"title": "new", "expected_revision": 0}),
            ("huge-revision", {"title": "new", "expected_revision": "9" * 100}),
            ("extra-field", {"title": "new", "unknown": True}),
            ("not-object", []),
        ],
        "schedule_create": [
            ("missing", {}),
            ("minimum", {"interval_seconds": 3600}),
            ("maximum", {"interval_seconds": 2592000}),
            ("below", {"interval_seconds": 3599}),
            ("above", {"interval_seconds": 2592001}),
            ("coerced", {"interval_seconds": " 3_600.0 "}),
            ("fraction", {"interval_seconds": 3600.5}),
            ("null", {"interval_seconds": None}),
            ("extra-ignored", {"interval_seconds": 3600, "unknown": True}),
            ("not-object", []),
        ],
        "schedule_write": [
            ("missing", {}),
            ("revision-only", {"expected_revision": 1}),
            ("null-enabled", {"expected_revision": 1, "enabled": None}),
            ("coerced", {"expected_revision": "+01", "enabled": "YES"}),
            ("spaced-bool", {"expected_revision": 1, "enabled": " false "}),
            ("bad-revision", {"expected_revision": 0, "enabled": False}),
            (
                "extra-ignored",
                {"expected_revision": 1, "enabled": False, "interval_seconds": 7200},
            ),
            ("not-object", []),
        ],
    }
    for key in ["session_patch", "schedule_write"]:
        for name, value in [
            ("float-revision-large", 1e20),
            ("float-revision-i64-max", float(2**63)),
            ("float-revision-i64-min", float(-(2**63))),
            ("float-revision-below-i64-max", math.nextafter(float(2**63), 0)),
        ]:
            inputs[key].append(
                (
                    name,
                    {
                        "expected_revision": value,
                        **({"title": "new"} if key == "session_patch" else {}),
                    },
                )
            )
    return [
        {
            "model": key,
            **validation_vector(key + "-" + name, REQUEST_MODELS[key], value),
        }
        for key, rows in inputs.items()
        for name, value in rows
    ]


def initial_settings():
    records, dependencies, cases, raw_overrides = {}, {}, [], {}
    project = Engagement(
        id="project", name="Settings fixture", created_at=BASE, updated_at=BASE
    )

    def session(identity, *, metadata=None, harness=False, **fields):
        if identity not in records:
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
                **fields,
            )
        return identity

    def schedule(owner, *, identity=None, **fields):
        identity = identity or owner + "-schedule"
        records[identity] = ChatSchedule(
            id=identity,
            engagement_id=fields.pop("engagement_id", "project"),
            session_id=owner,
            provider_profile_id="missing-provider",
            model="saved-model",
            interval_seconds=3600,
            next_run_at=BASE + timedelta(hours=1),
            created_at=fields.pop("created_at", BASE),
            updated_at=BASE,
            **fields,
        )
        return identity

    def add(name, kind, body, owner=None, *, auth=None, **extra):
        owner = owner or session("case-" + name)
        method, suffix = {
            "session_patch": ("PATCH", f"/chat-sessions/{owner}"),
            "schedule_create": ("POST", f"/chat/sessions/{owner}/schedule"),
            "schedule_write": ("POST", f"/chat/sessions/{owner}/schedule/actions"),
        }[kind]
        try:
            validated = REQUEST_MODELS[kind].model_validate(body)
            service = {
                "kind": kind,
                "session_id": owner,
                "body": validated.model_dump(mode="json", exclude_unset=True),
            }
        except ValidationError:
            service = None
        row = {
            "name": name,
            "method": method,
            "path": "/api/v1" + suffix,
            "body": body,
            "service": service,
            **extra,
        }
        if auth is not None:
            row["auth"] = auth
            row["service"] = None
        if kind == "schedule_create":
            row["generated_ids"] = [str(UUID(int=1000 + len(cases)))]
        cases.append(row)
        return row

    for identity, changes in [
        ("mcp-http", {"enabled": True}),
        ("mcp-disabled", {}),
        (
            "mcp-stdio",
            {
                "transport": "stdio",
                "url": None,
                "command": "/fixture/never-run",
                "enabled": True,
                "trusted_stdio": True,
            },
        ),
        (
            "mcp-bearer",
            {
                "enabled": True,
                "auth_mode": "bearer",
                "bearer_secret_ref": "env:NEVER_LOOK_UP",
            },
        ),
        (
            "mcp-headers",
            {
                "enabled": True,
                "auth_mode": "headers",
                "header_secret_refs": {"X-Fixture": "session:" + "a" * 32},
            },
        ),
        ("mcp-malformed", {"enabled": True}),
    ]:
        dependencies[identity] = McpServerProfile(
            **profile_input(id=identity, **changes)
        )
    raw_overrides["mcp-malformed"] = {"url": "http://not-loopback.invalid"}

    # Validated request vectors are also real HTTP requests, on independent rows.
    for vector in request_vectors():
        owner = session("request-" + vector["name"])
        if vector["model"] == "schedule_write":
            schedule(owner)
        add(
            "request-" + vector["name"],
            vector["model"],
            vector["input"],
            owner,
            request_vector=vector["name"],
        )

    owner = session(
        "sequence", metadata={"opaque": {"z": 1, "a": "β"}, "hook_ids": ["old"]}
    )
    schedule(owner)
    add(
        "sequence-rename",
        "session_patch",
        {"title": " Renamed β ", "expected_revision": 1},
        owner,
    )
    add(
        "sequence-stale-title",
        "session_patch",
        {"title": "stale", "expected_revision": 1},
        owner,
    )
    add("sequence-reasoning", "session_patch", {"reasoning_effort": "high"}, owner)
    add("sequence-clear-reasoning", "session_patch", {"reasoning_effort": None}, owner)
    add("sequence-archive", "session_patch", {"archived": True}, owner)
    add(
        "sequence-repeat-archive",
        "session_patch",
        {"archived": True},
        owner,
        clock=(NOW + timedelta(minutes=5)).isoformat(),
    )
    add("sequence-unarchive", "session_patch", {"archived": False}, owner)
    add(
        "sequence-reopen-rename",
        "session_patch",
        {"title": "After reopen"},
        owner,
        action="reopen",
    )
    add(
        "sequence-mcp",
        "session_patch",
        {"mcp_server_ids": ["mcp-http", "mcp-stdio", "mcp-bearer", "mcp-headers"]},
        owner,
    )
    add(
        "sequence-hook-no-discovery",
        "session_patch",
        {"hook_ids": ["missing-hook", "../opaque-hook"]},
        owner,
    )
    add(
        "sequence-clear-mcp-hooks",
        "session_patch",
        {"mcp_server_ids": [], "hook_ids": []},
        owner,
    )
    for name, selection in [
        ("disabled", "mcp-disabled"),
        ("missing", "absent"),
        ("wrong-kind", "project"),
        ("malformed", "mcp-malformed"),
    ]:
        add("mcp-" + name, "session_patch", {"mcp_server_ids": [selection]})
    add(
        "mcp-source-order",
        "session_patch",
        {"mcp_server_ids": ["mcp-disabled", "absent"]},
    )

    for name, metadata, body in [
        ("provider-enable", {}, {"allow_subagents": True, "max_active_subagents": 8}),
        (
            "provider-clear-limit",
            {"allow_subagents": True, "max_active_subagents": 8},
            {"max_active_subagents": None},
        ),
        (
            "provider-disable-retain-limit",
            {"allow_subagents": True, "max_active_subagents": 8},
            {"allow_subagents": False},
        ),
        (
            "provider-only-names",
            {},
            {"subagent_provider_id": "not-validated", "subagent_model": "model"},
        ),
    ]:
        add(name, "session_patch", body, session(name, metadata=metadata))
    harness_vectors = [
        ("harness-enable", {}, {"allow_subagents": True}),
        (
            "harness-disable",
            {
                "provider_subagent": {
                    "provider_profile_id": "p",
                    "model": "m",
                    "max_active": 4,
                }
            },
            {"allow_subagents": False},
        ),
        (
            "harness-retain",
            {
                "provider_subagent": {
                    "provider_profile_id": "p",
                    "model": "m",
                    "max_active": 4,
                    "opaque": True,
                }
            },
            {"subagent_model": "new"},
        ),
        (
            "harness-clear-limit",
            {
                "provider_subagent": {
                    "provider_profile_id": "p",
                    "model": "m",
                    "max_active": 4,
                }
            },
            {"max_active_subagents": None},
        ),
        (
            "harness-explicit-empty",
            {"provider_subagent": {"provider_profile_id": "p", "model": "m"}},
            {"subagent_provider_id": "", "subagent_model": ""},
        ),
        ("harness-null-saved", {"provider_subagent": None}, {"allow_subagents": True}),
        ("harness-empty-list", {"provider_subagent": []}, {"allow_subagents": True}),
        ("harness-truthy-list", {"provider_subagent": [1]}, {"allow_subagents": True}),
        (
            "harness-truthy-scalar",
            {"provider_subagent": "saved"},
            {"allow_subagents": True},
        ),
        (
            "harness-scalar-bypass",
            {"provider_subagent": "saved"},
            {
                "allow_subagents": True,
                "subagent_provider_id": "p",
                "subagent_model": "m",
                "max_active_subagents": None,
            },
        ),
        (
            "harness-scalar-limit-fallback",
            {"provider_subagent": "saved"},
            {
                "allow_subagents": True,
                "subagent_provider_id": "p",
                "subagent_model": "m",
            },
        ),
        (
            "harness-opaque-limit",
            {
                "provider_subagent": {
                    "provider_profile_id": 7,
                    "model": True,
                    "max_active": {"opaque": 1},
                }
            },
            {"allow_subagents": True},
        ),
    ]
    for name, metadata, body in harness_vectors:
        add(name, "session_patch", body, session(name, harness=True, metadata=metadata))
    owner = session(
        "harness-order",
        harness=True,
        metadata={
            "provider_subagent": {
                "provider_profile_id": {"z": 1, "a": "β"},
                "model": [False, {"z": 1, "a": 2}],
            }
        },
    )
    add(
        "harness-order-title",
        "session_patch",
        {"title": "Preserve nested order"},
        owner,
    )
    add(
        "harness-order-fallback",
        "session_patch",
        {"allow_subagents": True},
        owner,
        action="reopen",
    )

    for name, metadata, body in [
        ("messaging-main", {}, {"allow_agent_messaging": True}),
        ("messaging-child-empty", {"subagent_id": ""}, {"allow_agent_messaging": True}),
        (
            "messaging-child-numeric",
            {"subagent_id": 1},
            {"allow_agent_messaging": True},
        ),
        (
            "messaging-temporary",
            {"temporary_assistant": True},
            {"allow_agent_messaging": True},
        ),
        (
            "messaging-temporary-int",
            {"temporary_assistant": 1},
            {"allow_agent_messaging": True},
        ),
        (
            "messaging-archived-empty",
            {"archived_at": ""},
            {"allow_agent_messaging": True},
        ),
        ("messaging-archive-int", {"archived_at": 1}, {"allow_agent_messaging": True}),
        (
            "messaging-disable-hidden",
            {"temporary_assistant": True},
            {"allow_agent_messaging": False},
        ),
        (
            "messaging-unarchive-original",
            {"archived_at": BASE.isoformat()},
            {"archived": False, "allow_agent_messaging": True},
        ),
        (
            "messaging-archive-original",
            {},
            {"archived": True, "allow_agent_messaging": True},
        ),
        ("archive-preserve-null", {"archived_at": None}, {"archived": True}),
        (
            "archive-hidden-search",
            {"temporary_assistant": True},
            {"title": "Never indexed", "archived": True},
        ),
    ]:
        add(name, "session_patch", body, session(name, metadata=metadata))

    for status in [
        "routing",
        "waiting_approval",
        "waiting_callback",
        "finalizing",
        "complete",
        "failed",
        "cancelled",
        "interrupted",
    ]:
        owner = session("turn-" + status)
        records[owner + "-turn"] = ChatTurn(
            id=owner + "-turn",
            engagement_id="project",
            session_id=owner,
            provider_profile_id="missing-provider",
            model="fixture",
            status=status,
            created_at=BASE,
            updated_at=BASE,
        )
        add(
            "turn-" + status + "-title",
            "session_patch",
            {"title": "Allowed if idle"},
            owner,
        )
        if status == "routing":
            for name, body in [
                ("reasoning", {"reasoning_effort": None}),
                ("subagents", {"allow_subagents": True}),
                ("messaging", {"allow_agent_messaging": True}),
                ("mcp", {"mcp_server_ids": []}),
                ("hooks", {"hook_ids": []}),
                ("archive", {"archived": False}),
            ]:
                add("routing-" + name, "session_patch", body, owner)
    owner = session("duplicate-pending")
    for suffix in ["a", "b"]:
        records[owner + suffix] = ChatTurn(
            id=owner + suffix,
            engagement_id="project",
            session_id=owner,
            provider_profile_id="p",
            model="fixture",
            status="routing",
            created_at=BASE,
            updated_at=BASE,
        )
    add(owner, "session_patch", {"reasoning_effort": None}, owner)
    owner = session("failed-answer-fallback")
    records[owner + "-turn"] = ChatTurn(
        id=owner + "-turn",
        engagement_id="project",
        session_id=owner,
        provider_profile_id="p",
        model="fixture",
        status="failed",
        error="failed final answer",
        request_snapshot={"final_answer_recovery": {"required": True}},
        created_at=BASE,
        updated_at=BASE,
    )
    add(owner, "session_patch", {"title": "No pending fallback"}, owner)

    for suffix, body in [
        ("refusal", {"title": "Refused after repair"}),
        ("safe", {"reasoning_effort": "low"}),
    ]:
        owner = session("receipt-" + suffix)
        call_id = owner + "-call"
        records[owner + "-turn"] = ChatTurn(
            id=owner + "-turn",
            engagement_id="project",
            session_id=owner,
            provider_profile_id="p",
            model="fixture",
            status="interrupted",
            request_snapshot={
                "recovery": {
                    "required": True,
                    "unknown_tool_call_ids": [call_id],
                    "unknown_hook_execution_ids": [],
                }
            },
            created_at=BASE,
            updated_at=BASE,
        )
        dependencies[call_id] = ToolCall(
            id=call_id,
            engagement_id="project",
            run_id=owner + "-turn",
            origin="chat",
            chat_session_id=owner,
            chat_turn_id=owner + "-turn",
            tool_name="read_fixture",
            status="complete",
            risk_class="local_read",
            result={
                "schema": "nebula.tool-result/v2",
                "tool_call_id": call_id,
                "tool_name": "read_fixture",
                "tool_version": "fixture",
                "status": "completed",
            },
            metadata={"provider_step": 1, "provider_call_id": call_id},
            created_at=BASE,
            updated_at=BASE,
        )
        add(owner, "session_patch", body, owner)
        add(owner + "-repeat", "session_patch", body, owner, action="reopen")

    for name, options in [
        ("schedule-new", {}),
        ("schedule-harness", {"harness": True}),
        ("schedule-archived", {"metadata": {"archived_at": BASE.isoformat()}}),
    ]:
        owner = session(name, **options)
        add(name + "-create", "schedule_create", {"interval_seconds": 7200}, owner)
        if name == "schedule-new":
            add(
                name + "-duplicate",
                "schedule_create",
                {"interval_seconds": 7200},
                owner,
            )
            add(
                name + "-disable",
                "schedule_write",
                {"expected_revision": 1, "enabled": False},
                owner,
            )
            add(
                name + "-stale",
                "schedule_write",
                {"expected_revision": 1, "enabled": True},
                owner,
            )
            add(name + "-empty", "schedule_write", {"expected_revision": 2}, owner)
            add(
                name + "-null",
                "schedule_write",
                {"expected_revision": 3, "enabled": None},
                owner,
            )
            add(
                name + "-enable",
                "schedule_write",
                {"expected_revision": 4, "enabled": True},
                owner,
                action="reopen",
            )
    owner = session("schedule-no-provider")
    raw_overrides[owner] = {"provider_profile_id": None}
    add(owner, "schedule_create", {"interval_seconds": 3600}, owner)
    add("schedule-absent", "schedule_write", {"expected_revision": 1, "enabled": False})

    for name, session_meta, schedule_fields, body in [
        ("archive-operator-pause", {}, {"enabled": False}, {"archived": True}),
        (
            "unarchive-operator-pause",
            {"archived_at": BASE.isoformat()},
            {"enabled": False},
            {"archived": False},
        ),
        (
            "unarchive-enabled",
            {"archived_at": BASE.isoformat()},
            {"enabled": True, "paused_by": "archive"},
            {"archived": False},
        ),
        (
            "unarchive-archive-pause",
            {"archived_at": BASE.isoformat()},
            {"enabled": False, "paused_by": "archive", "skip_reason": "archived"},
            {"archived": False},
        ),
    ]:
        owner = session(name, metadata=session_meta)
        schedule(owner, **schedule_fields)
        add(name, "session_patch", body, owner)
    owner = session("schedule-enable-unarchives", metadata={"archived_at": None})
    schedule(
        owner, enabled=False, paused_by="archive", skip_reason="saved archive reason"
    )
    add(owner, "schedule_write", {"expected_revision": 1, "enabled": True}, owner)
    owner = session(
        "schedule-disable-retains-reason", metadata={"archived_at": BASE.isoformat()}
    )
    schedule(owner, paused_by="archive", skip_reason="saved reason")
    add(owner, "schedule_write", {"expected_revision": 1, "enabled": False}, owner)
    owner = session("schedule-duplicates")
    schedule(owner, identity=owner + "-a", engagement_id="other-project")
    schedule(owner, identity=owner + "-b")
    add(owner, "schedule_write", {"expected_revision": 1, "enabled": False}, owner)

    for name, archived in [
        ("archive-schedule-conflict", False),
        ("unarchive-schedule-conflict", True),
    ]:
        owner = session(
            name, metadata={"archived_at": BASE.isoformat()} if archived else {}
        )
        sid = schedule(
            owner, enabled=not archived, paused_by="archive" if archived else None
        )
        add(
            name,
            "session_patch",
            {"archived": not archived},
            owner,
            fault={
                "kind": "schedule_conflict",
                "count": 1,
                "session_id": owner,
                "schedule_id": sid,
            },
        )
    owner = session(
        "unarchive-retry-exhausted", metadata={"archived_at": BASE.isoformat()}
    )
    schedule(owner, enabled=False)
    add(
        owner,
        "schedule_write",
        {"expected_revision": 1, "enabled": True},
        owner,
        fault={"kind": "unarchive_conflict", "count": 3, "session_id": owner},
    )

    # Natural retained-model failures are captured separately until their exact
    # validation envelopes are supported by the Rust retained-record adapter.
    for name, kind, body in [
        ("malformed-schedule-after-archive", "session_patch", {"archived": True}),
        ("malformed-schedule-create", "schedule_create", {"interval_seconds": 3600}),
        (
            "malformed-schedule-action",
            "schedule_write",
            {"expected_revision": 1, "enabled": True},
        ),
    ]:
        owner = session(name)
        sid = schedule(owner)
        raw_overrides[sid] = {"next_run_at": "2030-01-01T12:00:00"}
        add(name, kind, body, owner, known_unsupported=True)

    for kind, body in [
        ("session_patch", {"title": "missing"}),
        ("schedule_create", {"interval_seconds": 3600}),
        ("schedule_write", {"expected_revision": 1, "enabled": True}),
    ]:
        add(kind + "-missing", kind, body, "not-found")
        add(kind + "-wrong-kind", kind, body, "project")
        add(
            kind + "-no-auth", kind, body, session("auth-" + kind), auth={"headers": {}}
        )
        add(
            kind + "-bad-auth",
            kind,
            body,
            session("auth-" + kind),
            auth={"headers": {"Authorization": "Bearer wrong"}},
        )
    dependencies["paired"] = PairedDeviceSession(
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
    for name, headers in [
        ("paired-valid", paired),
        ("paired-no-csrf", {k: v for k, v in paired.items() if k != "X-Nebula-CSRF"}),
        ("paired-wrong-origin", {**paired, "Origin": "https://other.invalid"}),
    ]:
        add(
            name,
            "session_patch",
            {"title": "Paired mutation"},
            auth={"headers": headers},
        )
    return (
        [project],
        list(records.values()),
        list(dependencies.values()),
        raw_overrides,
        cases,
    )


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def collect_settings():
    projects, records, dependencies, raw_overrides, cases = initial_settings()
    cases.sort(key=lambda case: bool(case.get("known_unsupported")))
    clock = [NOW]
    current_case = [{}]
    executions = []
    fault_attempts = [0]
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
            return (
                clock[0].astimezone(tz)
                if tz is not None
                else clock[0].replace(tzinfo=None)
            )

    def unexpected(*_args, **_kwargs):
        executions.append("attempted external execution")
        raise AssertionError(executions[-1])

    def fixed_uuid():
        ids = current_case[0].get("generated_ids", [])
        assert len(ids) == 1, "unexpected generated schedule identity"
        uuid_calls[0] += 1
        return UUID(ids[0])

    original_update = StoreTransaction.update

    def update_with_fault(transaction, model, entity_id, changes, **kwargs):
        fault = current_case[0].get("fault")
        if fault and fault_attempts[0] < fault["count"]:
            selected = (
                fault["kind"] == "schedule_conflict"
                and model is ChatSchedule
                and entity_id == fault["schedule_id"]
            ) or (
                fault["kind"] == "unarchive_conflict"
                and model is ChatSession
                and entity_id == fault["session_id"]
                and "metadata" in changes
                and "archived_at" not in changes["metadata"]
            )
            if selected:
                fault_attempts[0] += 1
                # This injected storage fault is deliberately inside the real
                # Database.session rollback/diagnostic boundary.
                raise ConflictError(
                    "assistant record revision or identity conflicts; reload the current record"
                )
        return original_update(transaction, model, entity_id, changes, **kwargs)

    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-settings-oracle-"
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
                    "harness-order",
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
                entity_rows = {
                    row["id"]: dict(row)
                    for row in raw.execute("SELECT * FROM entities ORDER BY id")
                }
                envelopes = {
                    identity: {
                        "kind": row["kind"],
                        "payload": json.loads(row["payload"]),
                    }
                    for identity, row in entity_rows.items()
                }
                search_rows = {
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
                return envelopes, entity_rows, search_rows, protected

        def inert_chat(current, **kwargs):
            kwargs.update(
                provider_factory=unexpected,
                workspace_resolver=unexpected,
                tool_suggestion_client=unexpected,
                worker_id="inert-settings-oracle",
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

        initial, initial_rows, initial_search, initial_protected = snapshot()
        client = client_for(store)
        try:
            with ExitStack() as guards:
                # Domain's default_factory captured utc_now at class creation;
                # freeze its datetime lookup as well as imported clock aliases.
                guards.enter_context(patch("nebula.v3.domain.datetime", FrozenDatetime))
                for module in ["api", "storage", "chat", "chat_schedules"]:
                    guards.enter_context(
                        patch(
                            f"nebula.v3.{module}.utc_now", side_effect=lambda: clock[0]
                        )
                    )
                guards.enter_context(
                    patch("nebula.v3.chat_schedules.uuid4", side_effect=fixed_uuid)
                )
                guards.enter_context(
                    patch.object(StoreTransaction, "update", new=update_with_fault)
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
                supported_final = None
                for case in cases:
                    if case.get("known_unsupported") and supported_final is None:
                        supported_final = snapshot()
                    current_case[0] = case
                    fault_attempts[0] = 0
                    uuid_calls[0] = 0
                    clock[0] = datetime.fromisoformat(
                        case.get("clock", NOW.isoformat())
                    )
                    if case.get("action") == "reopen":
                        client.close()
                        database.dispose()
                        database = Database(path, bootstrap=False)
                        store = NebulaStore(database)
                        client = client_for(store)
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
                    if "generated_ids" in case:
                        case["expected_uuid_calls"] = uuid_calls[0]
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
                        in {
                            "chat_sessions",
                            "chat_schedules",
                            "chat_turns",
                            "native_hook_executions",
                        }
                        for identity in changed_ids
                    ), "unexpected authoritative mutation"
                    assert all(
                        canonical(before.get(identity))
                        != canonical(after.get(identity))
                        for identity in changed_ids
                    ), "unexpected raw-only authoritative mutation"
                    assert before.keys() <= after.keys(), (
                        "settings must not delete entities"
                    )
                    assert before_protected == after_protected, (
                        "settings mutated a protected table"
                    )
                    search_ids = {
                        identity
                        for identity in before_search.keys() | after_search.keys()
                        if before_search.get(identity) != after_search.get(identity)
                    }
                    assert search_ids <= changed_ids, (
                        "search changed without its owning entity"
                    )
                    case["expected_search_changes"] = [
                        {
                            "before": before_search.get(identity),
                            "after": after_search.get(identity),
                        }
                        for identity in sorted(search_ids)
                    ]
                    case["before_snapshot_sha256"] = digest(list(before.values()))
                    case["after_snapshot_sha256"] = digest(list(after.values()))
                    case["before_search_sha256"] = digest(list(before_search.values()))
                    case["after_search_sha256"] = digest(list(after_search.values()))
                    case["before_protected_sha256"] = digest(before_protected)
                    case["after_protected_sha256"] = digest(after_protected)
                    if "fault" in case:
                        case["expected_fault_attempts"] = fault_attempts[0]
                        assert fault_attempts[0] == case["fault"]["count"]
                    assert not executions, (
                        "a settings request attempted external execution"
                    )
            final, final_rows, final_search, final_protected = (
                supported_final or snapshot()
            )
        finally:
            client.close()
            database.dispose()
    assert initial_protected == final_protected
    unsupported = [case for case in cases if case.pop("known_unsupported", False)]
    supported = [case for case in cases if case not in unsupported]
    assert all(case["expected"]["status"] == 422 for case in unsupported), (
        "re-review malformed-model gap classification against capture truth"
    )
    assistant_kinds = {"chat_sessions", "chat_schedules", "chat_turns"}
    return {
        "format": "nebula.assistant-settings-oracle/v1",
        "clock": NOW.isoformat(),
        "origin": ORIGIN,
        "normalization": "Only diagnostic request_id/error_id. Trusted clocks and per-case generated_ids are injected. SHA256 uses ID-sorted arrays (protected table maps) with Python sorted-key ASCII JSON and comma/colon separators. Raw SQLite row objects retain timestamps, payload strings and every envelope column.",
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
            model.entity_kind: model.model_json_schema()
            for model in [
                McpServerProfile,
                ToolCall,
                NativeHookExecution,
                PairedDeviceSession,
            ]
        },
        "mcp_profile_vectors": mcp_profile_vectors(),
        "request_schemas": {
            key: model.model_json_schema() for key, model in REQUEST_MODELS.items()
        },
        "request_vectors": request_vectors(),
        "cases": supported,
        "known_unsupported_cases": unsupported,
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
                "api",
                "chat",
                "chat_schedules",
                "chat_subagents",
                "domain",
                "storage",
                "database",
                "search",
                "tool_results",
            ]
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect_settings()
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(result["cases"]),
                "known_unsupported_cases": len(result["known_unsupported_cases"]),
                "mcp_profile_vectors": len(result["mcp_profile_vectors"]),
                "request_vectors": len(result["request_vectors"]),
                "sha256": sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
