#!/usr/bin/env python3
"""Capture atomic goal-conversation creation against inert isolated Python Core.

No lifespan, provider, helper, tool, credential lookup or workspace operation runs.
Only clocks, UUIDs and the trusted host home resolver are deterministic inputs.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
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
from nebula.v3.chat_goals import GoalConversationCreate
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import (
    ChatSession,
    Engagement,
    McpServerProfile,
    ModelCapabilities,
    PairedDeviceSession,
    ProviderCapabilityVerification,
    ProviderPrivacy,
    ProviderProfile,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.storage import NebulaStore

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.capture_assistant_model_validation import model_metadata
from scripts.capture_assistant_settings import digest, normalize

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
ORIGIN = "https://nebula.test:9443"
ENVIRONMENT = {
    "home": "/fixture/home/operator",
    "users": {"fixture": "/fixture/users/fixture"},
}
MODELS = {
    model.__name__: model
    for model in [
        Engagement,
        ProviderProfile,
        ModelCapabilities,
        ProviderPrivacy,
        ProviderCapabilityVerification,
        ChatSession,
    ]
}
BASE_FIELDS = ["id", "revision", "created_at", "updated_at"]


class Observations:
    def __init__(self):
        self.case = {}
        self.clock_calls = []
        self.home_calls = []
        self.lookup_calls = []
        self.trace = []
        self.uuid_calls = 0

    def clear(self, case=None):
        self.case = case or {}
        self.clock_calls.clear()
        self.home_calls.clear()
        self.lookup_calls.clear()
        self.trace.clear()
        self.uuid_calls = 0

    def now(self):
        values = self.case.get("model_clock_values")
        index = len(self.clock_calls)
        value = datetime.fromisoformat(values[index]) if values else NOW
        self.clock_calls.append("model")
        self.trace.append({"kind": "model", "value": value.isoformat()})
        return value

    def expand_user(self, first):
        if not first.startswith("~"):
            return first
        self.home_calls.append(first)
        self.trace.append({"kind": "home", "input": first})
        environment = self.case.get("environment", ENVIRONMENT)
        return (
            environment["home"]
            if first == "~"
            else environment["users"].get(first[1:], first)
        )

    def uuid(self):
        identities = self.case.get("generated_ids", [str(UUID(int=41))])
        index = self.uuid_calls
        self.uuid_calls += 1
        assert index < len(identities), "unexpected UUID allocation"
        value = identities[index]
        self.trace.append({"kind": "uuid", "value": value})
        return UUID(value)

    def fields(self):
        return {
            "expected_clock_calls": list(self.clock_calls),
            "expected_home_calls": list(self.home_calls),
            "expected_lookup_calls": list(self.lookup_calls),
            "expected_uuid_calls": self.uuid_calls,
            "expected_factory_trace": list(self.trace),
        }

    @contextmanager
    def frozen(self):
        observations = self

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = observations.now()
                return (
                    value.astimezone(tz)
                    if tz is not None
                    else value.replace(tzinfo=None)
                )

        with ExitStack() as stack:
            stack.enter_context(patch("nebula.v3.domain.datetime", FrozenDatetime))
            stack.enter_context(patch("nebula.v3.domain.uuid4", side_effect=self.uuid))
            # Preserve real pathlib lexical processing and its unknown-user
            # RuntimeError. The trusted first-component resolver is the seam.
            stack.enter_context(
                patch("pathlib.os.path.expanduser", side_effect=self.expand_user)
            )
            yield


def base_input(kind, **changes):
    value = {
        "id": "fixture-" + kind,
        "revision": 1,
        "created_at": BASE.isoformat(),
        "updated_at": BASE.isoformat(),
    }
    value.update(
        {"name": " Fixture "}
        if kind == "engagements"
        else {"name": " Fixture ", "provider_type": "opaque-provider"}
    )
    return {**value, **changes}


def validation_result(model, value, *, constructor=False):
    try:
        instance = (
            model(**deepcopy(value))
            if constructor
            else model.model_validate(deepcopy(value))
        )
        return {"accepted": True, "payload": instance.model_dump(mode="json")}
    except ValidationError as error:
        return {
            "accepted": False,
            "errors": jsonable_encoder(error.errors(include_url=False)),
            "exception_preview": str(error)[:300],
        }
    except (RuntimeError, TypeError, ValueError) as error:
        return {
            "accepted": False,
            "exception_type": type(error).__name__,
            "exception_message": str(error),
        }


def dependency_vectors():
    vectors, boundaries = [], []
    obs = Observations()

    def add(
        name,
        kind="providers",
        changes=None,
        *,
        raw_input=None,
        remove=(),
        boundary=False,
        context=None,
    ):
        value = base_input(kind, **(changes or {}))
        for key in remove:
            value.pop(key)
        if raw_input is not None:
            value = json.loads(raw_input)
        obs.clear(context)
        with obs.frozen():
            expected = validation_result(
                Engagement if kind == "engagements" else ProviderProfile, value
            )
        result = {
            "name": name,
            "kind": kind,
            "input": value,
            "raw_input": raw_input or json.dumps(value, ensure_ascii=False),
            **(context or {}),
            "expected": expected,
            **obs.fields(),
        }
        if boundary:
            result["strict_retained_boundary"] = "missing_canonical_fields"
        (boundaries if boundary else vectors).append(result)

    add("project-defaults", "engagements")
    for status in ["draft", "active", "paused", "complete", "archived"]:
        add("project-status-" + status, "engagements", {"status": status})
    add(
        "project-typed-trim",
        "engagements",
        {
            "description": " description ",
            "tags": [" a ", "a", " "],
            "client_name": " client ",
            "owner_id": " owner ",
            "scope_policy_id": " scope ",
            "metadata": {" a ": 1, "a": 2, "nested": {" b ": 3}},
        },
    )
    add(
        "project-multiple-errors",
        "engagements",
        {
            "name": " ",
            "status": "unknown",
            "tags": [1],
            "workspace_path": "relative",
            "extra": True,
        },
    )
    paths = [
        ("null", None),
        ("absolute", "/fixture/workspace"),
        ("root", "/"),
        ("double-root", "//"),
        ("triple-root", "///"),
        ("dotdot", "/a/.."),
        ("relative-dot", "./"),
        ("lexical", "//server//a/./b/../"),
        ("home", "~"),
        ("home-subpath", "~/a//./b"),
        ("user", "~fixture"),
        ("user-lexical", "~fixture//a/./b"),
        ("unknown-user", "~missing/a"),
        ("nul", "/a/\x00/b"),
        ("control", "/a/\x1c/b"),
        ("nbsp", " /a/\u00a0 "),
        ("at-limit", "/" + "x" * 4095),
        ("over-limit", "/" + "x" * 4096),
    ]
    for name, path in paths:
        add("project-path-" + name, "engagements", {"workspace_path": path})
    for name, home in [
        ("expanded-over-limit", "/" + "x" * 4200),
        ("expanded-root", "/"),
        ("expanded-relative", "relative/home"),
    ]:
        add(
            "project-path-" + name,
            "engagements",
            {"workspace_path": "~"},
            context={"environment": {**ENVIRONMENT, "home": home}},
        )
    add("provider-defaults")
    add(
        "provider-empty-strings",
        changes={"name": " ", "provider_type": " ", "endpoint": " "},
    )
    add(
        "provider-scalars",
        changes={"enabled": "yes", "is_local": 1, "endpoint": " not-a-url "},
    )
    add(
        "provider-extra-type-order",
        changes={"name": 1, "enabled": None, "z_extra": 1, "a_extra": 2},
    )
    for name, secret in [
        ("env", "env:FIXTURE"),
        ("systemd", "systemd:fixture.token-1"),
        ("vault", "vault:" + "a" * 32),
        ("session", "session:" + "b" * 32),
        ("bad", "literal-secret-fixture"),
        ("uppercase-hex", "vault:" + "A" * 32),
        ("padded", " env:FIXTURE "),
    ]:
        add("provider-secret-" + name, changes={"secret_ref": secret})
    for name, values in [
        ("trim-dedup", [" b ", "a", "b"]),
        ("empty", [" "]),
        ("types", ["good", 1, None]),
    ]:
        add("provider-allowlist-" + name, changes={"model_allowlist": values})
    for name, changes in [
        ("local-valid", {"privacy": {"local_only": True}, "is_local": True}),
        ("local-invalid", {"privacy": {"local_only": True}}),
        (
            "consent-valid",
            {
                "privacy": {
                    "auto_share_tool_results": True,
                    "permits_sensitive_data": True,
                }
            },
        ),
        ("consent-invalid", {"privacy": {"auto_share_tool_results": True}}),
        (
            "privacy-coercion",
            {
                "privacy": {
                    "permits_sensitive_data": "yes",
                    "local_only": 0,
                    "residency": [" EU "],
                }
            },
        ),
        (
            "privacy-before-parent",
            {"privacy": {"auto_share_tool_results": True, "local_only": True}},
        ),
        (
            "time-before-parent",
            {"updated_at": "2019-01-01T00:00:00Z", "privacy": {"local_only": True}},
        ),
        (
            "default-allowed",
            {"model_allowlist": ["fixture"], "metadata": {"default_model": "fixture"}},
        ),
        (
            "default-disallowed",
            {
                "model_allowlist": ["fixture"],
                "metadata": {"default_model": " fixture "},
            },
        ),
        (
            "default-opaque",
            {"model_allowlist": ["fixture"], "metadata": {"default_model": ["other"]}},
        ),
        ("default-unrestricted", {"metadata": {"default_model": "other"}}),
    ]:
        add("provider-policy-" + name, changes=changes)
    for name, options in [
        ("valid", {"context_window": 1, "max_output_tokens": 2}),
        ("huge", {"context_window": 10**100}),
        ("bool", {"context_window": True}),
        ("string", {"context_window": "1"}),
        ("float", {"context_window": 1.0}),
        ("negative", {"max_output_tokens": -1}),
        ("null", {"context_window": None}),
        ("scalar", 1),
        ("list", []),
        ("opaque-keys", {" context_window ": "bad"}),
    ]:
        add("provider-options-" + name, changes={"metadata": {"options": options}})

    def verification(**changes):
        return {
            "model": "fixture",
            "status": "verified",
            "checked_at": BASE.isoformat(),
            **changes,
        }

    for name, values in [
        ("verified", {"fixture": verification()}),
        (
            "failed",
            {"fixture": verification(status="failed", failure_detail="failed fixture")},
        ),
        ("failed-missing-detail", {"fixture": verification(status="failed")}),
        (
            "verified-with-detail",
            {"fixture": verification(failure_detail="unexpected")},
        ),
        ("old-contract", {"fixture": verification(contract_version="old")}),
        ("key-mismatch", {"other": verification()}),
        ("trim-key-model", {" fixture ": verification(model=" fixture ")}),
        ("naive", {"fixture": verification(checked_at="2020-01-01T00:00:00")}),
        (
            "offset",
            {"fixture": verification(checked_at="2020-01-01T01:00:00.123456789+01:00")},
        ),
        ("default-time", {"fixture": {"model": "fixture", "status": "verified"}}),
        (
            "default-time-nested-invalid",
            {"fixture": {"model": "fixture", "status": "bad"}},
        ),
        (
            "trim-collision",
            {
                " fixture ": {"model": "fixture", "status": "verified"},
                "fixture": {
                    "model": "fixture",
                    "status": "failed",
                    "failure_detail": "last",
                },
            },
        ),
        (
            "default-time-source-order",
            {
                "z": {"model": "z", "status": "verified"},
                "a": {"model": "a", "status": "verified"},
            },
        ),
    ]:
        clocks = [NOW.isoformat(), (NOW + timedelta(microseconds=1)).isoformat()]
        add(
            "provider-verification-" + name,
            changes={
                "capability_verifications": values,
                "capabilities": {
                    "tool_calling": True,
                    "parallel_tool_calls": True,
                    "streaming": "yes",
                },
            },
            context={"model_clock_values": clocks}
            if name in {"trim-collision", "default-time-source-order"}
            else None,
        )
    add(
        "provider-default-time-earlier-invalid",
        changes={
            "name": 1,
            "capability_verifications": {
                "fixture": {"model": "fixture", "status": "verified"}
            },
        },
    )
    add(
        "provider-default-time-before-disabled",
        changes={
            "enabled": False,
            "capability_verifications": {
                "fixture": {"model": "fixture", "status": "verified"}
            },
        },
    )
    duplicate = base_input("providers")
    raw = (
        json.dumps(duplicate)[:-1]
        + ', "capability_verifications": {"fixture": {"model": "fixture", "status": "bad"}, "fixture": {"model": "fixture", "status": "verified"}}}'
    )
    add("provider-raw-duplicate-verification", raw_input=raw)
    for kind in ["engagements", "providers"]:
        add(kind + "-huge-revision", kind, {"revision": 10**100})
        for field in BASE_FIELDS:
            add(kind + "-missing-" + field, kind, remove=[field], boundary=True)
    return vectors, boundaries


def constructor_vectors():
    values = []
    obs = Observations()
    base = {
        "id": "fixture-session",
        "engagement_id": "project",
        "title": "Title",
        "provider_profile_id": "provider",
        "model": "fixture",
        "metadata": {"reasoning_effort": None, "mcp_server_ids": [" selected "]},
    }
    for name, changes, clocks in [
        ("canonical", {}, None),
        ("trim", {"title": " Title ", "model": " fixture "}, None),
        ("empty-model", {"model": " "}, None),
        ("control-model", {"model": "\x1cfixture\x1f"}, None),
        ("blank-title", {"title": " "}, None),
        ("long-title", {"title": "😀" * 301}, None),
        ("missing-provider", {"provider_profile_id": None}, None),
        (
            "metadata-opaque",
            {
                "metadata": {
                    " b ": 1,
                    "b": 2,
                    "opaque": {" z ": 1, "a": 2},
                    "hook_ids": [" hook ", ""],
                }
            },
            None,
        ),
        ("field-errors", {"id": 1, "title": [], "model": None, "metadata": []}, None),
        ("extra", {"z_extra": 1}, None),
        (
            "distinct-clocks",
            {},
            [NOW.isoformat(), (NOW + timedelta(microseconds=1)).isoformat()],
        ),
        (
            "backwards-clock",
            {},
            [NOW.isoformat(), (NOW - timedelta(microseconds=1)).isoformat()],
        ),
    ]:
        value = {**base, **changes}
        case = {"model_clock_values": clocks} if clocks else {}
        obs.clear(case)
        with obs.frozen():
            expected = validation_result(ChatSession, value, constructor=True)
        values.append(
            {
                "name": "session-constructor-" + name,
                "model": "ChatSession",
                "input_origin": "constructor",
                "input": value,
                "raw_input": json.dumps(value, ensure_ascii=False),
                **case,
                "expected": expected,
                **obs.fields(),
            }
        )
    return values


def request_input(**changes):
    return {
        "objective": "Complete fixture",
        "completion_criteria": ["Receipt saved"],
        "engagement_id": "project",
        "provider_id": "provider",
        "model": "fixture",
        **changes,
    }


def request_vectors():
    cases = [
        ("defaults", {}),
        (
            "all-choices",
            {
                "plan": [" step "],
                "token_budget": "9" * 100,
                "time_budget_seconds": 1,
                "step_budget": 1,
                "child_budget": 0,
                "tools_enabled": True,
                "mcp_server_ids": ["mcp"],
                "hook_ids": [" hook ", ""],
                "reasoning_effort": "xhigh",
                "allow_subagents": True,
                "allow_agent_messaging": True,
                "max_active_subagents": 100,
            },
        ),
        (
            "ignored-extras",
            {"status": "running", "metadata": {"injected": True}, "backend": "harness"},
        ),
        (
            "inherited-plus-added-errors",
            {
                "objective": "",
                "completion_criteria": [],
                "provider_id": 1,
                "model": None,
                "tools_enabled": " true ",
            },
        ),
    ]
    for field, maximum in [
        ("engagement_id", 200),
        ("provider_id", 200),
        ("model", 500),
    ]:
        for name, value in [
            ("empty", ""),
            ("spaces", " "),
            ("overlong", "😀" * (maximum + 1)),
        ]:
            cases.append((field + "-" + name, {field: value}))
    for name, value in [
        ("none", None),
        ("minimal", "minimal"),
        ("unknown", "maximum"),
        ("padded", " high "),
    ]:
        cases.append(("reasoning-" + name, {"reasoning_effort": value}))
    for name, value in [
        ("coerced-true", "yes"),
        ("coerced-false", 0.0),
        ("null", None),
        ("spaced", " false "),
        ("integer-other", 2),
        ("huge-integer", 10**100),
        ("fraction", 1.5),
    ]:
        cases.append(
            (
                "booleans-" + name,
                {
                    "tools_enabled": value,
                    "allow_subagents": value,
                    "allow_agent_messaging": value,
                },
            )
        )
    for name, value in [
        ("null", None),
        ("minimum", 1),
        ("maximum", 100),
        ("zero", 0),
        ("above", 101),
        ("bool", True),
        ("float", 2.0),
        ("fraction", 1.5),
        ("string", " 0__10 "),
        ("huge", "9" * 100),
    ]:
        cases.append(("subagent-limit-" + name, {"max_active_subagents": value}))
    cases.extend(
        [
            ("boolean-i64-overflow", {"tools_enabled": 2**63}),
            ("boolean-large-integral-float", {"tools_enabled": 2e20}),
            ("boolean-i64-minimum", {"tools_enabled": -(2**63)}),
            ("boolean-i64-maximum", {"tools_enabled": 2**63 - 1}),
            ("boolean-float-i64-minimum", {"tools_enabled": float(-(2**63))}),
            (
                "boolean-float-i64-minimum-inward",
                {"tools_enabled": math.nextafter(float(-(2**63)), 0.0)},
            ),
            ("boolean-i64-negative-overflow", {"tools_enabled": -(2**63) - 1}),
        ]
    )
    for name, values in [
        ("duplicate", ["mcp", "mcp"]),
        ("whitespace-distinct", ["mcp", " mcp "]),
        ("empty", [""]),
        ("invalid-items", ["mcp", 1, None, {}]),
        ("null", None),
        ("over-max-invalid", [1] + ["x"] * 64),
    ]:
        cases.append(("mcp-" + name, {"mcp_server_ids": values}))
    for name, values in [
        ("duplicate", ["hook", "hook"]),
        ("whitespace-distinct", ["hook", " hook "]),
        ("empty", [""]),
        ("invalid-items", [1, None]),
        ("over-max-invalid", ["x"] * 32 + [1]),
    ]:
        cases.append(("hooks-" + name, {"hook_ids": values}))
    cases.extend(
        [
            (
                "both-duplicates",
                {"mcp_server_ids": ["mcp", "mcp"], "hook_ids": ["hook", "hook"]},
            ),
            ("field-before-duplicate", {"model": 1, "mcp_server_ids": ["mcp", "mcp"]}),
        ]
    )
    result = []
    for name, changes in cases:
        value = request_input(**changes)
        expected = validation_result(GoalConversationCreate, value)
        if expected["accepted"]:
            expected["fields_set"] = sorted(
                GoalConversationCreate.model_validate(value).model_fields_set
            )
        result.append(
            {
                "name": name,
                "input": value,
                "raw_input": json.dumps(value, ensure_ascii=False),
                "expected": expected,
            }
        )
    for name, value in [
        ("missing-all", {}),
        ("wrong-container", []),
        ("null-body", None),
    ]:
        result.append(
            {
                "name": name,
                "input": value,
                "raw_input": json.dumps(value),
                "expected": validation_result(GoalConversationCreate, value),
            }
        )
    return result


def model_contract():
    vectors, boundaries = dependency_vectors()
    return {
        "format": "nebula.assistant-goal-conversations-oracle/v1",
        "clock": NOW.isoformat(),
        "origin": ORIGIN,
        "environment": ENVIRONMENT,
        "pydantic_version": pydantic.__version__,
        "dependency_schemas": {
            m.entity_kind: m.model_json_schema()
            for m in [
                Engagement,
                ProviderProfile,
                McpServerProfile,
                PairedDeviceSession,
            ]
        },
        "models": {name: model_metadata(model) for name, model in MODELS.items()},
        "dependency_vectors": vectors,
        "strict_retained_boundary_vectors": boundaries,
        "constructor_vectors": constructor_vectors(),
        "request_schemas": {
            "goal_conversation_create": GoalConversationCreate.model_json_schema()
        },
        "request_vectors": request_vectors(),
    }


def initial_conversations():
    projects, dependencies, raw_overrides, cases = {}, {}, {}, []

    def project(identity, **changes):
        projects[identity] = Engagement.model_validate(
            base_input("engagements", id=identity, **changes)
        )
        return identity

    def provider(identity, **changes):
        dependencies[identity] = ProviderProfile.model_validate(
            base_input("providers", id=identity, **changes)
        )
        return identity

    project("project")
    for status in ["active", "paused", "complete", "archived"]:
        project("project-" + status, status=status)
    project("project-home")
    raw_overrides["project-home"] = {"workspace_path": "~fixture//a/./b"}
    project("project-unknown-user")
    raw_overrides["project-unknown-user"] = {"workspace_path": "~missing/a"}
    project("project-malformed")
    raw_overrides["project-malformed"] = {"name": " "}
    provider("provider")
    provider("provider-disabled", enabled=False)
    provider("provider-limited", model_allowlist=[" fixture ", "fixture"])
    provider(
        "provider-opaque",
        provider_type="unregistered fixture",
        endpoint="opaque endpoint",
        secret_ref="env:NEVER_RESOLVE_FIXTURE",
    )
    provider("provider-malformed")
    raw_overrides["provider-malformed"] = {"privacy": {"auto_share_tool_results": True}}
    for identity, enabled, values in [
        (
            "provider-clock",
            True,
            {
                "z": {"model": "z", "status": "verified"},
                "a": {"model": "a", "status": "verified"},
            },
        ),
        (
            "provider-clock-disabled",
            False,
            {"fixture": {"model": "fixture", "status": "verified"}},
        ),
        (
            "provider-clock-invalid",
            True,
            {"fixture": {"model": "fixture", "status": "bad"}},
        ),
    ]:
        provider(identity, enabled=enabled)
        raw_overrides[identity] = {"capability_verifications": values}
    provider("provider-raw-capabilities")
    raw_overrides["provider-raw-capabilities"] = {
        "capabilities": {"tool_calling": True, "parallel_tool_calls": True}
    }
    provider("provider'quoted", model_allowlist=["fixture"])
    for identity, enabled in [
        ("mcp", True),
        ("mcp-second", True),
        ("mcp-disabled", False),
        ("mcp-malformed", True),
    ]:
        dependencies[identity] = McpServerProfile(
            id=identity,
            name=identity,
            transport="streamable_http",
            url="https://mcp.invalid/rpc",
            enabled=enabled,
            created_at=BASE,
            updated_at=BASE,
        )
    raw_overrides["mcp-malformed"] = {"url": "http://nonloopback.invalid/rpc"}
    dependencies["mcp-stdio"] = McpServerProfile(
        id="mcp-stdio",
        name="mcp-stdio",
        transport="stdio",
        command="/fixture/never-run",
        enabled=True,
        trusted_stdio=True,
        created_at=BASE,
        updated_at=BASE,
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
    sentinel = ChatSession(
        id="sentinel",
        engagement_id="project",
        title="Retained fixture",
        provider_profile_id="provider",
        model="fixture",
        metadata={"z": 1, "a": 2},
        created_at=BASE,
        updated_at=BASE,
    )
    collision = str(UUID(int=39000))
    retained = ChatSession(
        id=collision,
        engagement_id="project",
        title="Collision fixture",
        provider_profile_id="provider",
        model="fixture",
        created_at=BASE,
        updated_at=BASE,
    )

    def add(name, changes=None, **extra):
        body = request_input(**(changes or {}))
        if "body" in extra:
            body = extra.pop("body")
        try:
            validated = GoalConversationCreate.model_validate(body)
            service = {
                "kind": "goal_conversation_create",
                "body": validated.model_dump(mode="json"),
            }
        except ValidationError:
            service = None
        row = {
            "name": name,
            "method": "POST",
            "path": "/api/v1/chat/goal-conversations",
            "body": body,
            "service": service,
            "generated_ids": [
                str(UUID(int=40000 + len(cases) * 2)),
                str(UUID(int=40001 + len(cases) * 2)),
            ],
            **extra,
        }
        if "auth" in row:
            row["service"] = None
        cases.append(row)
        return row

    first = add("default-create")
    add("repeat-same-body-new-ids")
    add("repeat-explicit-ids-conflict", generated_ids=first["generated_ids"])
    add(
        "all-composer-choices",
        {
            "tools_enabled": True,
            "mcp_server_ids": ["mcp", "mcp-stdio"],
            "hook_ids": [" hook ", ""],
            "reasoning_effort": "high",
            "allow_subagents": True,
            "allow_agent_messaging": True,
            "max_active_subagents": 7,
            "plan": [" first ", "second"],
            "token_budget": 10**100,
            "step_budget": 4,
            "time_budget_seconds": 90,
            "child_budget": 2,
        },
    )
    add("mcp-with-tools-disabled", {"tools_enabled": False, "mcp_server_ids": ["mcp"]})
    add(
        "opaque-provider-no-credential-resolution",
        {"provider_id": "provider-opaque", "tools_enabled": True},
    )
    add(
        "provider-derived-flags-not-persisted",
        {"provider_id": "provider-raw-capabilities", "tools_enabled": True},
    )
    for status in ["active", "paused", "complete", "archived"]:
        add("project-status-" + status, {"engagement_id": "project-" + status})
    add("project-home-expansion", {"engagement_id": "project-home"})
    for name, changes in [
        (
            "missing-project-before-provider",
            {"engagement_id": "missing", "provider_id": "missing"},
        ),
        ("wrong-project-kind", {"engagement_id": "provider"}),
        (
            "malformed-project-before-provider",
            {"engagement_id": "project-malformed", "provider_id": "missing"},
        ),
        (
            "unknown-user-before-provider",
            {"engagement_id": "project-unknown-user", "provider_id": "missing"},
        ),
        ("missing-provider", {"provider_id": "missing"}),
        ("wrong-provider-kind", {"provider_id": "project"}),
        (
            "malformed-provider-before-mcp",
            {"provider_id": "provider-malformed", "mcp_server_ids": ["missing"]},
        ),
        (
            "disabled-provider-before-model-mcp",
            {
                "provider_id": "provider-disabled",
                "model": "other",
                "mcp_server_ids": ["missing"],
            },
        ),
        (
            "model-before-mcp",
            {
                "provider_id": "provider-limited",
                "model": "other",
                "mcp_server_ids": ["missing"],
            },
        ),
        ("model-limited-exact", {"provider_id": "provider-limited"}),
        (
            "model-limited-untrimmed",
            {"provider_id": "provider-limited", "model": " fixture "},
        ),
        ("model-unrestricted-trim", {"model": " fixture "}),
        ("model-empty-after-trim", {"model": " "}),
        ("model-controls-search-strip", {"model": "\x1cfixture\x1f"}),
        (
            "model-python-repr",
            {"provider_id": "provider'quoted", "model": "model'\"\n\x1c"},
        ),
        ("padded-project-id", {"engagement_id": " project "}),
        ("padded-provider-id", {"provider_id": " provider "}),
        ("padded-mcp-id", {"mcp_server_ids": [" mcp "]}),
        ("missing-mcp", {"mcp_server_ids": ["missing"]}),
        ("wrong-mcp-kind", {"mcp_server_ids": ["provider"]}),
        (
            "disabled-mcp-before-missing",
            {"mcp_server_ids": ["mcp-disabled", "missing"]},
        ),
        (
            "missing-mcp-before-disabled",
            {"mcp_server_ids": ["missing", "mcp-disabled"]},
        ),
        (
            "malformed-mcp-before-disabled",
            {"mcp_server_ids": ["mcp-malformed", "mcp-disabled"]},
        ),
        ("ordered-mcp-success", {"mcp_server_ids": ["mcp-second", "mcp"]}),
        (
            "provider-default-clock-before-disabled",
            {"provider_id": "provider-clock-disabled"},
        ),
        (
            "provider-default-clock-before-invalid",
            {"provider_id": "provider-clock-invalid"},
        ),
    ]:
        add(name, changes)
    add(
        "provider-default-clocks-source-order",
        {"provider_id": "provider-clock"},
        model_clock_values=[
            (NOW + timedelta(microseconds=i)).isoformat() for i in range(6)
        ],
    )
    for name, objective in [
        ("ascii", "  First\n  second\tthird  "),
        ("unicode-space", "\u00a0First\u0085second\u3000third\u00a0"),
        ("control-space", "\x1cFirst\x1dsecond\x1ethird\x1f"),
        ("control-only", "\x1c\x1d\x1e\x1f"),
        ("zero-width", "\u200b"),
        ("combining", "e\u0301" * 151),
        ("astral-boundary", "a" * 299 + "😀suffix"),
        ("cut-space", "a" * 299 + " suffix"),
        ("blank", " \t\n"),
        ("nbsp-only", "\u00a0"),
    ]:
        add("title-" + name, {"objective": objective})
    add(
        "four-distinct-clocks",
        model_clock_values=[
            (NOW + timedelta(microseconds=i)).isoformat() for i in range(4)
        ],
    )
    add(
        "session-clock-reversed",
        model_clock_values=[
            NOW.isoformat(),
            (NOW - timedelta(microseconds=1)).isoformat(),
        ],
    )
    add(
        "goal-clock-reversed",
        model_clock_values=[
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
            (NOW - timedelta(microseconds=1)).isoformat(),
        ],
    )
    add(
        "goal-earlier-than-session",
        model_clock_values=[
            NOW.isoformat(),
            NOW.isoformat(),
            BASE.isoformat(),
            BASE.isoformat(),
        ],
    )
    add("first-id-collision", generated_ids=[collision, str(UUID(int=39100))])
    add("second-id-collision", generated_ids=[str(UUID(int=39101)), collision])
    add("same-ids-collision", generated_ids=[str(UUID(int=39102))] * 2)
    ghost = str(UUID(int=39103))
    add("second-id-collision-restores-stale-search", generated_ids=[ghost, collision])
    add("retry-after-rollback-reopen", action="reopen")
    for vector in request_vectors():
        add("request-" + vector["name"], body=vector["input"])
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
    return (
        list(projects.values()),
        [sentinel, retained],
        list(dependencies.values()),
        raw_overrides,
        cases,
        ghost,
    )


def envelope_observations(directory, obs):
    """Explicit source reads beyond the existing strict Rust envelope boundary."""
    path = Path(directory) / "envelopes.db"
    database = Database(path)
    store = NebulaStore(database)
    original = ProviderProfile.model_validate(base_input("providers"))
    store.create(original)
    with sqlite3.connect(path) as raw:
        raw.row_factory = sqlite3.Row
        initial = dict(
            raw.execute("SELECT * FROM entities WHERE id=?", (original.id,)).fetchone()
        )
    values = []
    try:
        for name, columns, payload_changes in [
            ("row-revision-mismatch", {"revision": 2}, {}),
            ("payload-identity-mismatch", {}, {"id": "different-payload-id"}),
            ("provider-engagement-column", {"engagement_id": "unexpected-project"}, {}),
            ("provider-session-column", {"chat_session_id": "unexpected-session"}, {}),
            (
                "row-created-time-mismatch",
                {"created_at": "2019-01-01 00:00:00.000000"},
                {},
            ),
            (
                "automation-envelope-unrelated",
                {"automation_run_id": "retained-opaque"},
                {},
            ),
        ]:
            row = {**initial, **columns}
            payload = json.loads(row["payload"])
            payload.update(payload_changes)
            row["payload"] = json.dumps(payload)
            with sqlite3.connect(path) as raw:
                raw.execute(
                    "UPDATE entities SET "
                    + ",".join(f"{key}=?" for key in row)
                    + " WHERE id=?",
                    [*row.values(), original.id],
                )
            obs.clear()
            try:
                hydrated = store.get(ProviderProfile, original.id)
                expected = {
                    "accepted": True,
                    "payload": hydrated.model_dump(mode="json"),
                }
            except Exception as error:
                expected = {
                    "accepted": False,
                    "exception_type": type(error).__name__,
                    "exception_message": str(error),
                }
            values.append(
                {
                    "name": name,
                    "kind": "providers",
                    "strict_retained_boundary": "envelope_consistency",
                    "raw_row": row,
                    "expected": expected,
                    **obs.fields(),
                }
            )
    finally:
        database.dispose()
    return values


def collect_goal_conversations():
    projects, records, dependencies, raw_overrides, cases, ghost = (
        initial_conversations()
    )
    obs = Observations()
    executions = []
    protected_names = [
        "operation_events",
        "run_events",
        "resource_relations",
        "session_projections",
    ]
    original_get = NebulaStore.get

    def observe_get(store, model, entity_id):
        if model in {Engagement, ProviderProfile, McpServerProfile}:
            obs.lookup_calls.append({"kind": model.entity_kind, "id": entity_id})
            obs.trace.append(
                {"kind": "lookup", "entity_kind": model.entity_kind, "id": entity_id}
            )
        return original_get(store, model, entity_id)

    def unexpected(*_args, **_kwargs):
        executions.append("attempted external execution")
        raise AssertionError(executions[-1])

    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-goal-conversations-oracle-"
    ) as directory:
        path = Path(directory) / "nebula.db"
        database = Database(path)
        store = NebulaStore(database)
        for record in [*projects, *records, *dependencies]:
            store.create(record)
        with sqlite3.connect(path) as raw:
            canonical_payloads = dict(
                raw.execute("SELECT id,payload FROM entities ORDER BY id")
            )
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
                ("sentinel", 7, "a" * 64),
            )
            raw.execute(
                "INSERT INTO operation_events(id,operation_id,operation_kind,engagement_id,sequence,event_type,payload,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "protected-operation",
                    "sentinel",
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
                    "sentinel",
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
                    "sentinel",
                    "fixture",
                    "chat_sessions",
                    records[1].id,
                    '{"z": 1, "a": 2}',
                    1,
                    stamp,
                    stamp,
                ),
            )
            raw.row_factory = sqlite3.Row
            search = dict(
                raw.execute(
                    "SELECT * FROM search_documents WHERE id='sentinel'"
                ).fetchone()
            )
            search.update(id=ghost, resource_id=ghost, label="Stale search projection")
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
                worker_id="inert-goal-conversation-oracle",
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
            # Setup emits an eager provider snapshot during app construction.
            # Model corruption after a valid bootstrap: temporarily expose the
            # original canonical fixture rows, then restore exact raw strings
            # before every request and its before-state snapshot. No Setup
            # behavior or route validator is replaced by this fixture setup.
            with sqlite3.connect(path) as raw:
                retained_payloads = dict(
                    raw.execute("SELECT id,payload FROM entities ORDER BY id")
                )
                for identity in raw_overrides:
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
                    for identity in raw_overrides:
                        raw.execute(
                            "UPDATE entities SET payload=? WHERE id=?",
                            (retained_payloads[identity], identity),
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
                patch("nebula.v3.chat_goals.utc_now", side_effect=unexpected)
            )
            guards.enter_context(
                patch("nebula.v3.storage.utc_now", side_effect=unexpected)
            )
            guards.enter_context(
                patch("nebula.v3.chat_goals.uuid4", side_effect=obs.uuid)
            )
            guards.enter_context(patch.object(NebulaStore, "get", new=observe_get))
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
                    patch.object(HarnessRuntimeService, method, side_effect=unexpected)
                )
            client = client_for(store)
            try:
                for case in cases:
                    obs.clear(case)
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
                    case.update(obs.fields())
                    after, after_rows, after_search, after_protected = snapshot()
                    changed = {
                        identity
                        for identity in before_rows.keys() | after_rows.keys()
                        if before_rows.get(identity) != after_rows.get(identity)
                    }
                    case["expected_changes"] = [
                        {"before": before.get(identity), "after": after.get(identity)}
                        for identity in sorted(changed)
                    ]
                    search_changed = {
                        identity
                        for identity in before_search.keys() | after_search.keys()
                        if before_search.get(identity) != after_search.get(identity)
                    }
                    case["expected_search_changes"] = [
                        {
                            "before": before_search.get(identity),
                            "after": after_search.get(identity),
                        }
                        for identity in sorted(search_changed)
                    ]
                    if response.status_code == 201:
                        assert changed == set(case["generated_ids"]), (
                            "creation did not atomically save exactly its pair"
                        )
                        assert all(identity not in before for identity in changed), (
                            "creation changed an existing entity"
                        )
                        assert search_changed == {case["generated_ids"][0]}, (
                            "creation search projection mismatch"
                        )
                        for identity in changed:
                            assert (
                                after_rows[identity]["engagement_id"]
                                == after[identity]["payload"]["engagement_id"]
                            )
                            expected_session = (
                                case["generated_ids"][0]
                                if after[identity]["kind"] == "chat_goals"
                                else None
                            )
                            assert (
                                after_rows[identity]["chat_session_id"]
                                == expected_session
                            )
                    else:
                        assert before_rows == after_rows, (
                            "failed creation changed durable entity state"
                        )
                        assert before_search == after_search, (
                            "failed creation changed search state"
                        )
                    assert before_protected == after_protected, (
                        "creation changed protected state"
                    )
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
                    assert not executions, (
                        "creation attempted execution or an update-only clock"
                    )
                final, final_rows, final_search, final_protected = snapshot()
                boundary_observations = envelope_observations(directory, obs)
            finally:
                client.close()
                database.dispose()
    result = model_contract()
    result.update(
        {
            "normalization": "Only diagnostic request_id/error_id. Model clocks and UUIDs are trusted per-case sequences. Home resolution patches only pathlib.os.path.expanduser's first-component resolver, preserving real Path lexical processing without filesystem operations. Lookup and trace observations cover Engagement/Provider/MCP reads only. Authentication and inert app-reopen factory calls are excluded. App construction temporarily sees canonical fixture dependency payloads; exact raw corruption is restored before every request snapshot, without replacing Setup behavior. This captures corruption after bootstrap, not malformed-profile startup. Hashes use sorted-key ASCII JSON, comma/colon separators, ID-sorted arrays and protected table maps. Failed creation must preserve exact raw entity and search rows.",
            "projects": [
                row for row in initial.values() if row["kind"] == "engagements"
            ],
            "initial_records": [
                row
                for row in initial.values()
                if row["kind"] in {"chat_sessions", "chat_goals"}
            ],
            "dependency_records": [
                row
                for row in initial.values()
                if row["kind"] not in {"engagements", "chat_sessions", "chat_goals"}
            ],
            "raw_payloads": raw_payloads,
            "initial_entity_rows": list(initial_rows.values()),
            "initial_search_documents": list(initial_search.values()),
            "initial_protected_tables": initial_protected,
            "schema_sql": schema_sql,
            "cases": cases,
            "known_unsupported_cases": [],
            "envelope_boundary_observations": boundary_observations,
            "final_records": [
                row
                for row in final.values()
                if row["kind"] in {"chat_sessions", "chat_goals"}
            ],
            "final_dependencies": [
                row
                for row in final.values()
                if row["kind"] not in {"engagements", "chat_sessions", "chat_goals"}
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
    result = collect_goal_conversations()
    args.output.write_text(
        json.dumps(
            result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                key: len(result[key])
                for key in [
                    "cases",
                    "dependency_vectors",
                    "constructor_vectors",
                    "request_vectors",
                ]
            }
            | {"sha256": sha256(args.output.read_bytes()).hexdigest()}
        )
    )


if __name__ == "__main__":
    main()
