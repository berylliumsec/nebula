#!/usr/bin/env python3
"""Capture session display revisions against disposable, inert Python Core state.

Select, refresh, reconnect, expire an approval and observe retained progress.
Only the display watermark may change during a GET. Fixture actions explicitly
change retained records or passive observations; no execution is dispatched.
"""

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
from sqlalchemy import text
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database, EntityRow, OperationEventRow
from nebula.v3.domain import (
    Approval,
    ChatSession,
    ChatTurn,
    Engagement,
    HarnessInteraction,
    HarnessProfile,
    HarnessTurn,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.storage import NebulaStore

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
ORIGIN = "https://nebula.test:9443"
MODELS = [
    ChatSession,
    ChatTurn,
    Approval,
    HarnessInteraction,
    HarnessTurn,
    HarnessProfile,
]
MODEL_BY_KIND = {model.entity_kind: model for model in MODELS}


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


class PassiveRuntime(HarnessRuntimeService):
    """A runtime-shaped passive observer; no adapter is created or called."""

    enabled = True

    def __bool__(self):
        return self.enabled

    def connection_state(self, session_id):
        return self.observations.get(session_id, "disconnected")


def initial_state():
    records, dependencies, identities = [], [], []

    def session(identity, **fields):
        harness = fields.pop("harness", False)
        record = ChatSession(
            id=identity,
            engagement_id=fields.pop("engagement_id", "project"),
            title=identity,
            model="fixture",
            backend="harness" if harness else "provider",
            provider_profile_id=None if harness else "fixture-provider",
            harness_profile_id=fields.pop(
                "harness_profile_id", "profile-stop" if harness else None
            ),
            harness_session_id=fields.pop(
                "harness_session_id", identity + "-transport" if harness else None
            ),
            created_at=BASE,
            updated_at=BASE,
            **fields,
        )
        records.append(record)
        identities.append(identity)
        return record

    def turn(identity, owner, **fields):
        stamp = BASE + timedelta(seconds=fields.pop("seconds", 1))
        record = ChatTurn(
            id=identity,
            engagement_id=fields.pop("engagement_id", "project"),
            session_id=owner,
            model="fixture",
            provider_profile_id="fixture-provider",
            created_at=stamp,
            updated_at=stamp,
            **fields,
        )
        records.append(record)
        return record

    def harness(identity, owner, **fields):
        record = HarnessTurn(
            id=identity,
            engagement_id=fields.pop("engagement_id", "project"),
            harness_session_id=owner + "-transport",
            origin="chat",
            chat_session_id=owner,
            chat_turn_id=owner + "-turn",
            prompt="Read a retained fixture",
            created_at=BASE,
            updated_at=BASE,
            **fields,
        )
        dependencies.append(record)
        return record

    def approval(identity, owner, **fields):
        record = Approval(
            id=identity,
            engagement_id=fields.pop("engagement_id", "project"),
            run_id="fixture-run",
            origin="chat",
            chat_session_id=fields.pop("chat_session_id", owner),
            chat_turn_id=fields.pop("chat_turn_id", owner + "-turn"),
            risk_class="local_read",
            exact_request={"fixture": "retained note"},
            policy_rationale="Harmless retained fixture",
            requested_by="fixture",
            requested_at=BASE,
            created_at=BASE,
            updated_at=fields.pop("updated_at", BASE),
            **fields,
        )
        dependencies.append(record)
        return record

    def question(identity, owner, **fields):
        status = fields.pop("status", "pending")
        record = HarnessInteraction(
            id=identity,
            engagement_id=fields.pop("engagement_id", "project"),
            harness_turn_id=fields.pop("harness_turn_id", owner + "-harness"),
            harness_session_id=owner + "-transport",
            origin="chat",
            kind="user_input",
            chat_session_id=fields.pop("chat_session_id", owner),
            vendor_request_id=identity,
            status=status,
            resolved_at=None if status == "pending" else BASE,
            prompt=fields.pop("prompt", "Choose a retained note"),
            created_at=BASE,
            updated_at=fields.pop("updated_at", BASE),
            **fields,
        )
        dependencies.append(record)
        return record

    for identity, enabled in [("profile-stop", True), ("profile-no-stop", False)]:
        dependencies.append(
            HarnessProfile(
                id=identity,
                name=identity,
                kind="claude_agent_sdk",
                capabilities={"interruption": enabled},
                created_at=BASE,
                updated_at=BASE,
            )
        )
    session("idle")
    session("idle-connected", harness=True)
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
        owner = "chat-" + status
        session(owner)
        turn(owner + "-turn", owner, status=status)
    for status in [
        "queued",
        "running",
        "waiting_approval",
        "complete",
        "failed",
        "cancelled",
        "interrupted",
    ]:
        owner = "harness-" + status
        session(owner, harness=True)
        turn(owner + "-turn", owner, harness_turn_id=owner + "-harness")
        harness(owner + "-harness", owner, status=status)
    for status in ["complete", "failed", "cancelled", "interrupted"]:
        owner = "terminal-chat-" + status
        session(owner, harness=True)
        turn(owner + "-turn", owner, status=status, harness_turn_id=owner + "-harness")
        harness(owner + "-harness", owner, status="running")
        approval(owner + "-approval", owner)
        question(owner + "-question", owner)
    for identity, profile in [
        ("no-stop", "profile-no-stop"),
        ("missing-profile", "missing-profile-record"),
        ("wrong-profile-kind", "idle"),
    ]:
        session(identity, harness=True, harness_profile_id=profile)
        turn(identity + "-turn", identity)
    session("oldest-active")
    turn("oldest-active-old-terminal", "oldest-active", seconds=0, status="complete")
    turn("oldest-active-a", "oldest-active", seconds=2)
    turn("oldest-active-z", "oldest-active", seconds=2)
    turn("oldest-active-new", "oldest-active", seconds=3)
    turn("oldest-active-foreign", "oldest-active", seconds=0, engagement_id="foreign")
    session("newest-terminal")
    turn("newest-terminal-a", "newest-terminal", status="failed", seconds=3)
    turn("newest-terminal-z", "newest-terminal", status="complete", seconds=3)
    session("missing-harness", harness=True)
    turn("missing-harness-turn", "missing-harness", harness_turn_id="absent")
    session("foreign-harness", harness=True)
    turn(
        "foreign-harness-turn",
        "foreign-harness",
        harness_turn_id="foreign-harness-record",
    )
    harness(
        "foreign-harness-record",
        "foreign-harness",
        status="complete",
        engagement_id="foreign",
    )
    session("pending", harness=True)
    turn(
        "pending-turn",
        "pending",
        harness_turn_id="pending-harness",
        approval_id="fallback-approval",
    )
    turn(
        "pending-newer-turn",
        "pending",
        seconds=2,
        harness_turn_id="pending-harness",
        approval_id="fallback-approval",
    )
    harness("pending-harness", "pending", status="running")
    approval("fallback-approval", "pending", chat_turn_id=None, chat_session_id="other")
    approval("explicit-cross-session", "pending", chat_session_id="other")
    approval("explicit-wrong-owner", "pending", chat_turn_id="missing-turn")
    approval("foreign-approval", "pending", engagement_id="foreign")
    approval("expired-approval", "pending", expires_at=NOW)
    approval("future-approval", "pending", expires_at=NOW + timedelta(microseconds=1))
    question(
        "pending-question",
        "pending",
        prompt='Retained β 🦀 / "quote" \\ control\n\t\u0001\u2028',
    )
    question(
        "secret-question",
        "pending",
        contains_secret=True,
        prompt="DO-NOT-DISPLAY-SECRET",
    )
    question("foreign-question", "pending", engagement_id="foreign")
    question("other-session-question", "pending", chat_session_id="other")
    question("orphan-question", "pending", harness_turn_id="missing")
    for status in ["answered", "declined", "cancelled", "expired"]:
        question("question-" + status, "pending", status=status)
    for status in ["approved", "rejected", "cancelled", "expired"]:
        owner = "decision-" + status
        session(owner)
        turn(owner + "-turn", owner, status="waiting_approval")
        approval(owner + "-approval", owner, status=status)
    for state, adapter in [
        ("pending", "not_required"),
        ("delivered", "not_required"),
        ("delivered", "pending"),
        ("delivered", "sent"),
        ("delivered", "failed"),
        ("delivered", "unknown"),
        ("failed", "not_required"),
    ]:
        owner = "continuation-" + state + "-" + adapter
        session(owner)
        turn(owner + "-turn", owner)
        approval(
            owner + "-approval",
            owner,
            status="approved",
            continuation={
                "harness_turn_id": owner + "-ledger",
                "status": state,
                "adapter_status": adapter,
                "progress_after_sequence": 10,
                "updated_at": NOW,
                "detail": "Retained β 🦀\n",
                "adapter_detail": "Observed only",
            },
        )
    session("decision-reference")
    turn(
        "decision-reference-turn",
        "decision-reference",
        approval_id="reference-decision",
    )
    approval(
        "reference-decision",
        "decision-reference",
        status="approved",
        chat_turn_id="different-owner",
        chat_session_id="other",
    )
    session("failed-decision-pending")
    turn("failed-decision-pending-turn", "failed-decision-pending")
    approval("failed-decision-pending-action", "failed-decision-pending")
    approval(
        "failed-decision-pending-failed",
        "failed-decision-pending",
        status="approved",
        continuation={
            "harness_turn_id": "no-ledger",
            "status": "failed",
            "updated_at": NOW,
        },
    )
    session("terminal-decision")
    turn("terminal-decision-turn", "terminal-decision", status="complete")
    approval(
        "terminal-decision-approval",
        "terminal-decision",
        status="approved",
        continuation={
            "harness_turn_id": "no-ledger",
            "status": "failed",
            "updated_at": NOW,
        },
    )
    for suffix, turn_status, approval_status in [
        ("active", "routing", "pending"),
        ("decided", "routing", "approved"),
        ("inactive", "complete", "pending"),
    ]:
        owner = "naive-expiry-" + suffix
        session(owner)
        turn(owner + "-turn", owner, status=turn_status)
        approval(
            owner + "-approval",
            owner,
            status=approval_status,
            expires_at=NOW.replace(tzinfo=None),
        )
    for suffix, continuation_status, adapter_status in [
        ("query", "delivered", "sent"),
        ("pending-adapter", "delivered", "pending"),
        ("pending-status", "pending", "sent"),
    ]:
        owner = "oversized-sequence-" + suffix
        session(owner)
        turn(owner + "-turn", owner)
        approval(
            owner + "-approval",
            owner,
            status="approved",
            continuation={
                "harness_turn_id": "no-ledger",
                "status": continuation_status,
                "adapter_status": adapter_status,
                "updated_at": NOW,
                "progress_after_sequence": 2**63,
            },
        )
    session("sequence", harness=True)
    turn("sequence-turn", "sequence", harness_turn_id="sequence-harness")
    harness("sequence-harness", "sequence", status="running")
    approval("sequence-approval", "sequence", expires_at=NOW + timedelta(seconds=1))
    session("delete-session")
    session("python-existing-β-🦀")
    return (
        [
            Engagement(
                id="project", name="Session state", created_at=BASE, updated_at=BASE
            )
        ],
        records,
        dependencies,
        identities,
    )


def event(identity, sequence, **fields):
    return {
        "id": identity,
        "operation_id": fields.pop("operation_id", "sequence-harness"),
        "operation_kind": fields.pop("operation_kind", "harness_turn"),
        "engagement_id": fields.pop("engagement_id", "project"),
        "sequence": sequence,
        "event_type": fields.pop("event_type", "harness.message_delta"),
        "payload": {"fixture": "immutable retained observation"},
        "actor_id": None,
        "occurred_at": BASE.isoformat(),
        "idempotency_key": identity,
        **fields,
    }


def profile_vectors():
    """Record model acceptance/hydration, including source URL-parser semantics."""
    vectors = []
    base = {
        "id": "profile-vector",
        "name": "Retained profile",
        "revision": 1,
        "created_at": BASE.isoformat(),
        "updated_at": BASE.isoformat(),
        "kind": "claude_agent_sdk",
    }

    def add(name, **fields):
        value = {**base, **fields}
        try:
            model = HarnessProfile.model_validate(value)
            expected = {"accepted": True, "payload": model.model_dump(mode="json")}
        except ValueError:
            expected = {"accepted": False}
        vectors.append({"name": name, "input": value, "expected": expected})

    add("claude-defaults")
    add("codex-spawn", kind="codex_app_server", executable="/fixture/bin/codex")
    add("grok-spawn", kind="grok_acp", executable="/fixture/bin/grok")
    add("codex-missing-executable", kind="codex_app_server")
    add("grok-missing-executable", kind="grok_acp")
    add("relative-executable", executable="relative")
    add("empty-executable", executable="")
    add("spawn-with-endpoint", endpoint="unix:///fixture/socket")
    add("spawn-wrong-transport", transport="unix")
    add("spawn-endpoint-auth", auth_mode="endpoint_bearer", secret_ref="env:FIXTURE")
    add("claude-secret", auth_mode="secret_ref", secret_ref="env:FIXTURE")
    add("missing-secret", auth_mode="secret_ref")
    add("existing-session-secret", secret_ref="env:FIXTURE")
    add("opaque-vault", auth_mode="secret_ref", secret_ref="vault:" + "a" * 32)
    add("opaque-session", auth_mode="secret_ref", secret_ref="session:" + "0" * 32)
    for identity, secret in [
        ("bad-env", "env:9NAME"),
        ("bad-vault", "vault:" + "A" * 32),
        ("raw-secret", "literal-secret"),
        ("systemd-secret", "systemd:fixture"),
    ]:
        add(identity, auth_mode="secret_ref", secret_ref=secret)
    add("claude-native-shell-existing", native_capabilities={"shell": True})
    add(
        "claude-native-shell-secret",
        native_capabilities={"shell": True},
        auth_mode="secret_ref",
        secret_ref="env:FIXTURE",
    )
    for feature in ["browser", "computer_use", "image_generation"]:
        add("claude-unsupported-" + feature, native_capabilities={feature: True})
    add("claude-web-fetch", native_capabilities={"web_fetch": True})
    add(
        "codex-web-fetch",
        kind="codex_app_server",
        executable="/fixture/codex",
        native_capabilities={"web_fetch": True},
    )
    add(
        "codex-native",
        kind="codex_app_server",
        executable="/fixture/codex",
        native_capabilities={
            "workspace_access": "write",
            "shell": True,
            "browser": True,
            "computer_use": True,
            "image_generation": True,
            "skills": True,
            "subagents": True,
        },
    )
    add(
        "grok-secret",
        kind="grok_acp",
        executable="/fixture/grok",
        auth_mode="secret_ref",
        secret_ref="env:FIXTURE",
    )
    for name, home in [
        ("home-normalized", "/fixture/home///"),
        ("home-root", "/"),
        ("home-all-slashes", "///"),
        ("home-relative", "relative"),
        ("home-traversal", "/fixture/../home"),
        ("home-dots", "/fixture/.../home"),
        ("home-null", "/fixture/\0home"),
        ("home-newline", "/fixture/\nhome"),
        ("home-carriage", "/fixture/\rhome"),
    ]:
        add(
            name,
            kind="codex_app_server",
            executable="/fixture/codex",
            home_directory=home,
        )
    add("claude-home", home_directory="/fixture")
    add(
        "grok-home",
        kind="grok_acp",
        executable="/fixture/grok",
        home_directory="/fixture///",
    )
    add("privacy-requires-consent", privacy={"auto_share_tool_results": True})
    add(
        "privacy-consented",
        privacy={
            "auto_share_tool_results": True,
            "permits_sensitive_data": True,
            "local_only": True,
            "residency": ["fixture"],
            "retention": "none",
        },
    )
    endpoint_base = {
        "kind": "codex_app_server",
        "connection_mode": "endpoint",
        "transport": "websocket",
    }
    for name, endpoint in [
        ("ws-loopback", "ws://127.0.0.1:1234/rpc"),
        ("ws-localhost-case", "WS://LOCALHOST:1234/rpc"),
        ("ws-ipv6", "ws://[::1]:1234/rpc"),
        ("ws-no-port", "ws://localhost"),
        ("ws-invalid-port-retained", "ws://localhost:not-a-port/rpc"),
        ("ws-out-of-range-port-retained", "ws://localhost:999999/rpc"),
        ("ws-empty-userinfo", "ws://@localhost/rpc"),
        ("ws-empty-user-password", "ws://:@localhost/rpc"),
        ("ws-userinfo", "ws://user@localhost/rpc"),
        ("ws-password", "ws://:secret@localhost/rpc"),
        ("ws-empty-query", "ws://localhost/rpc?"),
        ("ws-query", "ws://localhost/rpc?x=1"),
        ("ws-empty-fragment", "ws://localhost/rpc#"),
        ("ws-fragment", "ws://localhost/rpc#x"),
        ("ws-nonloopback", "ws://example.test/rpc"),
        ("ws-unbracketed-ipv6", "ws://::1/rpc"),
        ("ws-broken-bracket", "ws://[::1/rpc"),
        ("ws-invalid-bracket-host", "ws://[localhost]/rpc"),
        ("wss-loopback", "wss://localhost/rpc"),
        ("ws-leading-controls", " \t\nws://localhost/rpc"),
        ("ws-embedded-newline", "ws://local\nhost/rpc"),
    ]:
        add(name, **endpoint_base, endpoint=endpoint)
    for name, endpoint in [
        ("unix-absolute", "unix:///fixture/socket"),
        ("unix-authority", "unix://remote/fixture/socket"),
        ("unix-credentials-authority", "unix://user:pass@remote/fixture/socket"),
        ("unix-relative", "unix://socket"),
        ("unix-uppercase", "UNIX:///fixture/socket"),
        ("unix-empty-query", "unix:///fixture/socket?"),
        ("unix-query", "unix:///fixture/socket?x"),
        ("unix-empty-fragment", "unix:///fixture/socket#"),
        ("unix-fragment", "unix:///fixture/socket#x"),
    ]:
        add(name, **{**endpoint_base, "transport": "unix"}, endpoint=endpoint)
    add("endpoint-no-url", **endpoint_base)
    add(
        "endpoint-executable",
        **endpoint_base,
        endpoint="ws://localhost",
        executable="/fixture/codex",
    )
    add(
        "endpoint-home",
        **endpoint_base,
        endpoint="ws://localhost",
        home_directory="/fixture",
    )
    add(
        "endpoint-stdio",
        **{**endpoint_base, "transport": "stdio"},
        endpoint="ws://localhost",
    )
    add(
        "endpoint-bearer",
        **endpoint_base,
        endpoint="ws://localhost",
        auth_mode="endpoint_bearer",
        secret_ref="env:FIXTURE",
    )
    add(
        "endpoint-secret-ref",
        **endpoint_base,
        endpoint="ws://localhost",
        auth_mode="secret_ref",
        secret_ref="env:FIXTURE",
    )
    add(
        "claude-endpoint",
        **{**endpoint_base, "kind": "claude_agent_sdk"},
        endpoint="ws://localhost",
    )
    add(
        "grok-endpoint",
        **{**endpoint_base, "kind": "grok_acp"},
        endpoint="ws://localhost",
        executable="/fixture/grok",
    )
    add(
        "capability-offset-times",
        capabilities={
            "interruption": False,
            "checked_at": "2030-01-01T13:30:00+01:30",
            "last_successful_turn_at": "2030-01-01T12:00:00.123456Z",
            "authentication_state": "verified",
            "session_state": "failed",
            "turn_state": "verified",
            "models": ["fixture"],
            "modes": ["retained"],
            "supported_native_capabilities": ["shell"],
            "exercised_capabilities": ["interruption"],
        },
    )
    add("capability-naive-times", capabilities={"checked_at": "2030-01-01T12:00:00"})
    add("capability-invalid-time", capabilities={"checked_at": "yesterday"})
    add("capability-invalid-state", capabilities={"authentication_state": "ready"})
    add("capability-model-limit", capabilities={"models": ["fixture"] * 257})
    add("capability-mode-limit", capabilities={"modes": ["fixture"] * 65})
    return vectors


def state_cases(identities, dependencies):
    cases = []

    def add(identity, name=None, **fields):
        cases.append(
            {
                "name": name or identity,
                "method": "GET",
                "path": "/api/v1/chat/sessions/" + quote(identity, safe="") + "/state",
                "body": None,
                "service": {"session_id": identity},
                **fields,
            }
        )

    for identity in [*identities, "missing", "chat-routing-turn", "β" * 201]:
        add(identity)
    for identity in ["idle", "missing"]:
        add(identity, identity + "-no-auth", service=None, auth={"headers": {}})
        add(
            identity,
            identity + "-wrong-auth",
            service=None,
            auth={"headers": {"Authorization": "Bearer incorrect"}},
        )
    add(
        "idle-connected",
        "connected-idle",
        pre_actions=[
            {"action": "set_runtime", "enabled": True},
            {
                "action": "set_connection",
                "harness_session_id": "idle-connected-transport",
                "state": "connected",
            },
        ],
    )
    add(
        "idle-connected",
        "disconnected-idle",
        pre_actions=[
            {
                "action": "set_connection",
                "harness_session_id": "idle-connected-transport",
                "state": "disconnected",
            }
        ],
    )
    add(
        "idle-connected",
        "unknown-transport",
        pre_actions=[
            {
                "action": "set_connection",
                "harness_session_id": "idle-connected-transport",
                "state": "unknown",
            }
        ],
    )
    add("sequence", "runtime-present-missing-connection")
    add(
        "sequence",
        "expiry-at-boundary",
        pre_actions=[
            {"action": "set_clock", "now": (NOW + timedelta(seconds=1)).isoformat()}
        ],
    )
    add(
        "sequence",
        "clock-rollback-restores-pending",
        pre_actions=[{"action": "set_clock", "now": NOW.isoformat()}],
    )
    approval = next(
        record for record in dependencies if record.id == "sequence-approval"
    )
    decided = envelope(approval)
    decided["payload"].update(revision=2, status="approved")
    decided["payload"]["continuation"] = {
        "harness_turn_id": "sequence-harness",
        "status": "delivered",
        "detail": None,
        "adapter_handoff": "transport_write",
        "adapter_status": "sent",
        "adapter_detail": None,
        "progress_after_sequence": 10,
        "updated_at": NOW.isoformat(),
    }
    # Revalidate to preserve exact model JSON timestamp spelling.
    decided = envelope(Approval.model_validate(decided["payload"]))
    add(
        "sequence",
        "decision-is-not-progress",
        pre_actions=[{"action": "upsert_record", "record": decided}],
    )
    for name, row in [
        ("boundary-is-not-progress", event("boundary", 10)),
        (
            "wrong-kind-is-not-progress",
            event("wrong-kind", 11, operation_kind="chat_turn"),
        ),
        (
            "wrong-type-is-not-progress",
            event("wrong-type", 12, event_type="harness.approval_decided"),
        ),
        (
            "wrong-operation-is-not-progress",
            event("wrong-operation", 13, operation_id="different-harness"),
        ),
        (
            "first-progress-cross-project",
            event("first-progress", 15, engagement_id="foreign"),
        ),
        (
            "later-progress-keeps-revision",
            event("later-progress", 20, event_type="harness.completed"),
        ),
        (
            "earlier-observed-sequence-revises",
            event("earlier-progress", 14, event_type="harness.tool_started"),
        ),
        (
            "tool-completed-keeps-first",
            event("tool-completed", 16, event_type="harness.tool_completed"),
        ),
    ]:
        add(
            "sequence",
            name,
            pre_actions=[{"action": "append_operation_event", "record": row}],
        )
    add("sequence", "reopen-watermark", pre_actions=[{"action": "reopen"}])
    add(
        "sequence",
        "runtime-absent-is-unknown",
        pre_actions=[{"action": "set_runtime", "enabled": False}],
    )
    add(
        "sequence",
        "decision-deletion-revises",
        pre_actions=[{"action": "delete_record", "id": "sequence-approval"}],
    )
    add("sequence", "unchanged-fast-path")
    add(
        "pending",
        "pending-deletion-revises",
        pre_actions=[{"action": "delete_record", "id": "secret-question"}],
    )
    add(
        "delete-session",
        "session-deletion-cascades-watermark",
        pre_actions=[{"action": "delete_record", "id": "delete-session"}],
    )
    add(
        "python-existing-β-🦀",
        "python-watermark-reopen",
        pre_actions=[{"action": "reopen"}],
    )
    return cases


def collect_state():
    projects, records, dependencies, identities = initial_state()
    cases = state_cases(identities, dependencies)
    clock = [NOW]
    observations = {}
    runtime_enabled = False
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-state-oracle-"
    ) as directory:
        path = Path(directory) / "nebula.db"
        database = Database(path)
        store = NebulaStore(database)
        for record in [*projects, *records, *dependencies]:
            store.create(record)

        def snapshots():
            with sqlite3.connect(path) as raw:
                return {
                    table: raw.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    for table in [
                        "entities",
                        "operation_events",
                        "run_events",
                        "resource_relations",
                    ]
                }

        def watermarks():
            with sqlite3.connect(path) as raw:
                return [
                    {"session_id": row[0], "revision": row[1], "digest": row[2]}
                    for row in raw.execute(
                        "SELECT session_id,revision,digest FROM session_projections ORDER BY session_id"
                    )
                ]

        def client_for(current):
            def unexpected_execution(*_args, **_kwargs):
                raise AssertionError(
                    "session-state observation must not execute or resolve a workspace"
                )

            runtime = PassiveRuntime(
                current,
                credential_store=CredentialStore(keyring_backend=NullKeyring()),
                workspace_resolver=unexpected_execution,
                adapter_factory=unexpected_execution,
                artifact_store=ArtifactStore(Path(directory) / "artifacts"),
            )
            runtime.observations = observations
            runtime.enabled = (
                True  # create_app must preserve this explicitly supplied object.
            )
            client = TestClient(
                create_app(
                    current,
                    credential_store=CredentialStore(keyring_backend=NullKeyring()),
                    harness_runtime_service=runtime,
                    auth_token="fixture-core",
                    enable_executable_missions=False,
                    bootstrap_workspace=False,
                ),
                raise_server_exceptions=False,
                base_url=ORIGIN,
            )
            runtime.enabled = runtime_enabled
            return client, runtime

        def apply_record(item):
            record = MODEL_BY_KIND[item["kind"]].model_validate(item["payload"])
            with database.session() as connection:
                row = connection.get(EntityRow, record.id)
                assert row is not None, (
                    "upsert fixture currently replaces existing records only"
                )
                row.kind = record.entity_kind
                row.payload = record.model_dump(mode="json")
                row.revision = record.revision
                row.engagement_id = getattr(record, "engagement_id", None)
                row.chat_session_id = getattr(
                    record, "session_id", getattr(record, "chat_session_id", None)
                )
                row.created_at = record.created_at
                row.updated_at = record.updated_at
                connection.commit()

        from nebula.v3.session_state import session_state

        with patch("nebula.v3.session_state.utc_now", side_effect=lambda: clock[0]):
            session_state(store, store.get(ChatSession, "python-existing-β-🦀"))
        initial_watermarks = watermarks()
        client, runtime = client_for(store)
        try:
            # The initial reader succeeds. Inject a valid-but-naive expiry only
            # after the writer lock, so the writer's re-projection fails and the
            # mutation rolls back. Its error attribution differs from errors
            # raised inside Database.session() on the initial read.
            before_write_error = snapshots()
            watermarks_before_write_error = watermarks()
            begin_write = store._begin_run_write
            write_keys = []

            def invalidate_expiry_after_lock(connection, key):
                begin_write(connection, key)
                write_keys.append(key)
                connection.execute(
                    text(
                        "UPDATE entities SET payload=json_set(payload,'$.expires_at',:expiry) WHERE id=:id"
                    ),
                    {
                        "expiry": NOW.replace(tzinfo=None).isoformat(),
                        "id": "sequence-approval",
                    },
                )

            with (
                patch("nebula.v3.session_state.utc_now", return_value=NOW),
                patch.object(
                    store,
                    "_begin_run_write",
                    side_effect=invalidate_expiry_after_lock,
                ),
            ):
                response = client.get(
                    "/api/v1/chat/sessions/sequence/state",
                    headers={
                        "Authorization": "Bearer fixture-core",
                        "X-Nebula-Operation-ID": "fixture-operation",
                    },
                )
            write_phase_error = {
                "expected": {
                    "status": response.status_code,
                    "body": normalize(response.json()),
                },
                "expected_cache_control": response.headers.get("cache-control"),
            }
            assert write_keys == ["session-state:sequence"]
            assert response.status_code == 500
            assert snapshots() == before_write_error
            assert watermarks() == watermarks_before_write_error
            with patch("nebula.v3.session_state.utc_now", side_effect=lambda: clock[0]):
                for case in cases:
                    for action in case.get("pre_actions", []):
                        kind = action["action"]
                        if kind == "set_clock":
                            clock[0] = datetime.fromisoformat(action["now"])
                        elif kind == "set_runtime":
                            runtime_enabled = action["enabled"]
                            runtime.enabled = runtime_enabled
                        elif kind == "set_connection":
                            observations[action["harness_session_id"]] = action["state"]
                        elif kind == "upsert_record":
                            apply_record(action["record"])
                        elif kind == "delete_record":
                            with database.session() as connection:
                                connection.delete(
                                    connection.get(EntityRow, action["id"])
                                )
                                connection.commit()
                        elif kind == "append_operation_event":
                            row = dict(action["record"])
                            row["occurred_at"] = datetime.fromisoformat(
                                row["occurred_at"]
                            )
                            with database.session() as connection:
                                connection.add(OperationEventRow(**row))
                                connection.commit()
                        elif kind == "reopen":
                            client.close()
                            database.dispose()
                            database = Database(path, bootstrap=False)
                            store = NebulaStore(database)
                            client, runtime = client_for(store)
                        else:
                            raise AssertionError(kind)
                    before = snapshots()
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
                    case["expected_watermarks"] = watermarks()
                    assert snapshots() == before, (
                        f"GET changed authoritative state: {case['name']}"
                    )
            with sqlite3.connect(path) as raw:
                final_rows = [
                    {"kind": kind, "payload": json.loads(payload)}
                    for kind, payload in raw.execute(
                        "SELECT kind,payload FROM entities WHERE kind!='engagements' ORDER BY id"
                    )
                ]
                raw.row_factory = sqlite3.Row
                final_events = []
                for row in raw.execute("SELECT * FROM operation_events ORDER BY id"):
                    item = dict(row)
                    item["payload"] = json.loads(item["payload"])
                    item["occurred_at"] = (
                        datetime.fromisoformat(item["occurred_at"])
                        .replace(tzinfo=timezone.utc)
                        .isoformat()
                    )
                    final_events.append(item)
                schema_sql = [
                    row[0]
                    for row in raw.execute(
                        "SELECT sql FROM sqlite_master WHERE tbl_name='operation_events' AND sql IS NOT NULL ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name"
                    )
                ]
        finally:
            client.close()
            database.dispose()
    return {
        "format": "nebula.assistant-state-oracle/v1",
        "clock": NOW.isoformat(),
        "origin": ORIGIN,
        "runtime_enabled": False,
        "connections": {},
        "normalization": "Only request_id/error_id. Trusted clock/passive connection actions are explicit. Raw entities, ledger and relations stay unchanged during each GET; only session_projections may change.",
        "projects": [envelope(record) for record in projects],
        "initial_records": [envelope(record) for record in records],
        "dependency_records": [envelope(record) for record in dependencies],
        "dependency_schemas": {
            model.entity_kind: model.model_json_schema() for model in MODELS[2:]
        },
        "profile_vectors": profile_vectors(),
        "write_phase_error": write_phase_error,
        "schema_sql": schema_sql,
        "initial_operation_events": [],
        "initial_watermarks": initial_watermarks,
        "cases": cases,
        "final_records": [
            row for row in final_rows if row["kind"] in {"chat_sessions", "chat_turns"}
        ],
        "final_dependencies": [
            row
            for row in final_rows
            if row["kind"] not in {"chat_sessions", "chat_turns"}
        ],
        "final_operation_events": final_events,
        "final_watermarks": cases[-1]["expected_watermarks"],
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in [
                "api",
                "session_state",
                "domain",
                "storage",
                "database",
                "harnesses",
            ]
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = collect_state()
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
