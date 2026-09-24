#!/usr/bin/env python3
"""Capture isolated provider text execution through the actual Assistant API."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from unittest.mock import patch
from uuid import UUID

import httpx
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from keyring.backends.null import Keyring as NullKeyring
import pydantic
from pydantic import ValidationError

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatContextAttachment,
    ChatRequestMessage,
    ChatService,
    PendingProviderSubagent,
)
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import ChatMessage, ChatSession, ChatTokenUsage, ChatTurn
from nebula.v3.domain import Engagement, ProviderProfile
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.providers import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
    ProviderOverloadedError,
    StreamEventType,
    provider_from_profile,
)
from nebula.v3.storage import NebulaStore

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.capture_assistant_forks import ForkObservations, fixed
from scripts.capture_assistant_goal_conversations import BASE, NOW, ORIGIN
from scripts.capture_assistant_model_validation import model_metadata
from scripts.capture_assistant_settings import digest, normalize

ROOT = Path(__file__).resolve().parents[1]
PROTECTED = [
    "operation_events",
    "run_events",
    "resource_relations",
    "session_projections",
]
PHASE = ContextVar("execution_oracle_phase", default="other")
REQUEST = {
    "backend": "provider",
    "provider_id": "provider",
    "model": "fixture",
    "engagement_id": "project",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": True,
    "tools_enabled": False,
    "include_knowledge": False,
    "allow_cloud_knowledge": False,
    "mcp_server_ids": [],
    "ssh_environment_ids": [],
    "hook_ids": [],
    "allow_subagents": False,
    "allow_agent_messaging": False,
    "context_attachments": [],
}


def changes(before, after):
    return [
        {"before": before.get(key), "after": after.get(key)}
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    ]


def parse_sse(text):
    frames = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        item = {}
        for line in block.splitlines():
            if line.startswith(":"):
                raise AssertionError("fixture unexpectedly waited for heartbeat")
            key, _, value = line.partition(":")
            item[key] = value.removeprefix(" ")
        item["data"] = normalize(json.loads(item["data"]))
        frames.append(item)
    return frames


def normalized_sse(text):
    return re.sub(
        r'("(?:request_id|error_id)"\s*:\s*)"[^"\\]*"', r'\1"<generated>"', text
    )


def request_vectors():
    base = {
        "provider_id": "provider",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    selected = "  λ🌌\n"
    attachment = {
        "source_kind": "note",
        "source_label": " Fixture ",
        "text": selected,
        "sha256": sha256(selected.encode()).hexdigest(),
    }
    inputs = [
        ("defaults", base),
        (
            "trimmed-scalars",
            {
                **base,
                "provider_id": " provider ",
                "model": " fixture ",
                "messages": [{"role": "user", "content": "\u00a0Hello\u00a0"}],
            },
        ),
        ("unicode-separators-preserved", {**base, "model": "\u001cfixture\u001f"}),
        (
            "coerced-controls",
            {
                **base,
                "stream": "yes",
                "tools_enabled": "OFF",
                "max_output_tokens": "+001",
                "temperature": " 1.25 ",
            },
        ),
        ("integer-integral-float", {**base, "max_output_tokens": 10.0}),
        ("integer-fractional-float", {**base, "max_output_tokens": 1.5}),
        ("integer-float-overflow", {**base, "max_output_tokens": float(2**63)}),
        ("integer-bool", {**base, "max_active_subagents": True}),
        ("integer-large", {**base, "max_artifact_queries": 10**100}),
        ("bool-int-overflow", {**base, "tools_enabled": 2**63}),
        ("bool-float-min", {**base, "tools_enabled": float(-(2**63))}),
        ("bool-whitespace", {**base, "stream": " true "}),
        (
            "nullable-fields",
            {
                **base,
                "model": None,
                "ssh_environment_ids": None,
                "max_output_tokens": None,
            },
        ),
        ("required-null", {**base, "messages": None}),
        ("missing-fields", {}),
        ("ordered-extras", {**base, "z_extra": "rate limit", "a_extra": "conflict"}),
        (
            "invalid-role-and-content",
            {**base, "messages": [{"role": "invalid", "content": 3, "z_extra": True}]},
        ),
        ("empty-content", {**base, "messages": [{"role": "user", "content": " \n "}]}),
        (
            "client-system",
            {
                **base,
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Hello"},
                ],
            },
        ),
        (
            "last-assistant",
            {**base, "messages": [{"role": "assistant", "content": "answer"}]},
        ),
        ("message-empty-list", {**base, "messages": []}),
        (
            "message-list-long-invalid-item",
            {**base, "messages": [False] + [{"role": "user", "content": "h"}] * 200},
        ),
        ("provider-absent", {**base, "provider_id": None}),
        ("provider-with-harness", {**base, "harness_profile_id": "harness"}),
        (
            "valid-harness-request",
            {
                **base,
                "backend": "harness",
                "provider_id": None,
                "harness_profile_id": "harness",
            },
        ),
        (
            "harness-missing-profile",
            {**base, "backend": "harness", "provider_id": None},
        ),
        ("hash-exact-unicode", {**base, "context_attachments": [attachment]}),
        (
            "hash-mismatch",
            {**base, "context_attachments": [{**attachment, "sha256": "a" * 64}]},
        ),
        (
            "hash-field-errors-before-after",
            {
                **base,
                "context_attachments": [
                    {
                        **attachment,
                        "source_kind": "NOT VALID",
                        "sha256": "bad",
                        "unexpected": 1,
                    }
                ],
            },
        ),
        (
            "pending-subagent-normalized",
            {
                **base,
                "pending_provider_subagent": {
                    "provider_profile_id": " provider ",
                    "model": " fixture ",
                    "max_active": "2",
                },
            },
        ),
        (
            "pending-subagent-multiple-errors",
            {
                **base,
                "pending_provider_subagent": {
                    "model": 3,
                    "max_active": 0,
                    "unknown": True,
                },
            },
        ),
        (
            "list-length-item-precedence",
            {**base, "mcp_server_ids": [1] + ["fixture"] * 64},
        ),
        (
            "nested-content-block",
            {
                **base,
                "messages": [
                    {
                        "role": "user",
                        "content": "Hello",
                        "content_blocks": [{"type": "image"}],
                    }
                ],
            },
        ),
        (
            "runtime-confirmation-format",
            {**base, "runtime_switch_confirmation": "A" * 64},
        ),
        (
            "field-bounds-and-after-precedence",
            {**base, "temperature": 3, "max_output_tokens": 0, "backend": "harness"},
        ),
    ]
    result = []
    for name, value in inputs:
        item = {
            "name": name,
            "input": value,
            "raw_input": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        }
        try:
            parsed = ChatCompletionRequest.model_validate(value)
            item["expected"] = {
                "accepted": True,
                "payload": parsed.model_dump(mode="json"),
                "fields_set": sorted(parsed.model_fields_set),
            }
        except ValidationError as error:
            errors = error.errors(include_url=False)
            request_error = RequestValidationError(
                [{**issue, "loc": ("body", *issue["loc"])} for issue in errors],
                body=value,
            )
            item["expected"] = {
                "accepted": False,
                "errors": jsonable_encoder(errors),
                "exception_preview": str(error)[:300],
                "request_exception_preview": str(request_error)[:300],
            }
        result.append(item)
    return result


def turn_vectors():
    base = fixed(
        ChatTurn,
        "turn",
        engagement_id="project",
        session_id="session",
        provider_profile_id="provider",
        model="fixture",
    ).model_dump(mode="json")
    variants = [
        ("canonical", {}),
        (
            "coerced-revision-counter",
            {"revision": "1", "next_step": "+002", "execution_tool_calls": True},
        ),
        ("coerced-bool", {"tools_enabled": "off"}),
        ("arbitrary-integer", {"artifact_queries": 10**100}),
        (
            "nested-usage",
            {
                "usage": {
                    "input_tokens": "10",
                    "output_tokens": True,
                    "total_tokens": 11.0,
                }
            },
        ),
        (
            "claim-all-present",
            {
                "execution_owner_id": "worker",
                "execution_claim_id": "claim",
                "execution_claimed_at": NOW.isoformat(),
            },
        ),
        ("claim-partial", {"execution_owner_id": "worker"}),
        ("provider-binding", {"provider_profile_id": None}),
        ("harness-binding", {"backend": "harness"}),
        (
            "field-before-binding",
            {"provider_profile_id": None, "next_step": -1, "status": "invalid"},
        ),
        (
            "nested-usage-multiple-errors",
            {"usage": {"input_tokens": -1, "output_tokens": "wrong", "unknown": True}},
        ),
        (
            "opaque-request-collisions",
            {"request_snapshot": {" a ": {" z ": 1}, "a": {" z ": 2}}},
        ),
        ("history-object-required", {"tool_history": [True]}),
        ("field-extras-order", {"z_extra": "conflict", "a_extra": "rate limit"}),
        (
            "naive-claim-time",
            {
                "execution_owner_id": "worker",
                "execution_claim_id": "claim",
                "execution_claimed_at": "2030-01-01T12:00:00",
            },
        ),
    ]
    result = []
    for name, change in variants:
        value = {**deepcopy(base), **change}
        item = {
            "name": name,
            "model": "ChatTurn",
            "input": value,
            "raw_input": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        }
        try:
            parsed = ChatTurn.model_validate(value)
            item["expected"] = {
                "accepted": True,
                "payload": parsed.model_dump(mode="json"),
            }
        except ValidationError as error:
            item["expected"] = {
                "accepted": False,
                "errors": jsonable_encoder(error.errors(include_url=False)),
                "exception_preview": str(error)[:300],
            }
        result.append(item)
    return result


class FixtureProvider(ModelProvider):
    """The only external seam: deterministic normalized provider responses."""

    def __init__(self, config, state):
        super().__init__(config)
        self.state = state

    def response(self, request, *, naming=False):
        return ModelResponse(
            provider_id=self.config.id,
            model=self.require(request),
            text="Fixture conversation" if naming else "Hello, world! 🌌",
            reasoning="Brief fixture reasoning.",
            usage=ModelUsage(input_tokens=12, output_tokens=7, total_tokens=19),
            finish_reason="stop",
            provider_request_id="fixture-provider-request",
        )

    async def health(self):
        raise AssertionError("provider health probing is outside the execution fixture")

    async def complete(self, request):
        self.state["provider_requests"].append(
            {"method": "complete", "request": request.model_dump(mode="json")}
        )
        assert request.metadata.get("operation") == "conversation_naming", (
            "unexpected complete() call outside optional source naming"
        )
        return self.response(request, naming=True)

    async def stream(self, request):
        self.require(request)
        assert not request.tools and not request.tool_results
        self.state["provider_requests"].append(
            {"method": "stream", "request": request.model_dump(mode="json")}
        )
        events = [
            ModelStreamEvent(type=StreamEventType.STARTED),
            ModelStreamEvent(
                type=StreamEventType.REASONING_DELTA, delta="Brief fixture reasoning."
            ),
            ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="Hello, "),
            ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="world! 🌌"),
        ]
        for event in events:
            self.state["provider_events"].append(event.model_dump(mode="json"))
            yield event
        self.state["entered"].set()
        if self.state["scenario"] == "stop":
            await self.state["release"].wait()
        if self.state["scenario"] == "overloaded":
            event = ModelStreamEvent.failure(
                ProviderOverloadedError("fixture provider is busy")
            )
        elif self.state["scenario"] == "eof":
            return
        else:
            event = ModelStreamEvent(
                type=StreamEventType.COMPLETED, response=self.response(request)
            )
        self.state["provider_events"].append(event.model_dump(mode="json"))
        yield event


def cases(vectors):
    result = []

    def add(
        name, *, body=None, path="/api/v1/chat/completions", method="POST", **fields
    ):
        ordinal = len(result) + 1
        item = {
            "name": name,
            "method": method,
            "path": path,
            "body": {**deepcopy(REQUEST), **(body or {})}
            if method == "POST" and path.endswith("completions")
            else None,
            "generated_ids": [
                str(UUID(int=ordinal * 100 + index)) for index in range(1, 33)
            ],
            **fields,
        }
        result.append(item)
        return item

    new = add("new-greeting-stream")
    add(
        "existing-stream",
        body={
            "session_id": "existing",
            "messages": [{"role": "user", "content": "Second exchange"}],
        },
    )
    add("unknown-provider", body={"provider_id": "missing"})
    add("disabled-provider", body={"provider_id": "disabled"})
    add("model-not-allowed", body={"model": "unavailable"})
    add("wrong-project", body={"session_id": "existing", "engagement_id": "other"})
    add("missing-session", body={"session_id": "missing"})
    add("missing-auth", headers={})
    add("provider-overload", body={"session_id": "failure"}, scenario="overloaded")
    add("provider-eof", body={"session_id": "eof"}, scenario="eof")
    stopped = add("stop-after-deltas", body={"session_id": "stop"}, scenario="stop")
    add(
        "terminal-reattach",
        path=f"/api/v1/chat/turns/{new['generated_ids'][1]}/events?after=999",
        method="GET",
    )
    add(
        "reopen-terminal-reattach",
        path=f"/api/v1/chat/turns/{new['generated_ids'][1]}/events",
        method="GET",
        action="reopen",
    )
    add("missing-reattach", path="/api/v1/chat/turns/missing/events", method="GET")
    add("cancel-again", path=f"/api/v1/chat/turns/{stopped['generated_ids'][0]}/cancel")
    add("cancel-complete", path=f"/api/v1/chat/turns/{new['generated_ids'][1]}/cancel")
    add("cancel-missing", path="/api/v1/chat/turns/missing/cancel")
    for label, index in [("session", 0), ("message", 2), ("turn", 1)]:
        collision = add("admission-rollback-" + label)
        if label == "message":
            collision["name"] = "admission-message-collision-retry"
        collision["generated_ids"][index] = str(UUID(int=999999))
    for vector in vectors:
        if not vector["expected"]["accepted"]:
            item = add("request-" + vector["name"], request_vector=vector["name"])
            item["body"] = vector["input"]
    return result


async def _collect_execution():
    import nebula.v3.api
    import nebula.v3.chat
    import nebula.v3.domain

    for module in [nebula.v3.api, nebula.v3.chat, nebula.v3.domain]:
        assert Path(module.__file__).resolve().is_relative_to(ROOT / "src"), (
            module.__file__
        )
    obs = ForkObservations()
    state = {
        "active": False,
        "phases": [],
        "provider_requests": [],
        "provider_events": [],
    }
    unintended = []

    def unexpected(*_args, **_kwargs):
        unintended.append("unsupported external execution")
        raise AssertionError(unintended[-1])

    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-execution-oracle-"
    ) as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        path = root / "nebula.db"
        database = Database(path)
        store = NebulaStore(database)
        projects = [fixed(Engagement, "project", name="Fixture project")]
        profiles = [
            fixed(
                ProviderProfile,
                name,
                name=name,
                provider_type="custom",
                endpoint="http://127.0.0.1:9/v1",
                is_local=True,
                enabled=name != "disabled",
                model_allowlist=["fixture"],
                metadata={
                    "default_model": "fixture",
                    "options": {"context_window": 32768, "max_output_tokens": 4096},
                },
            )
            for name in ["provider", "disabled"]
        ]
        sessions = [
            fixed(
                ChatSession,
                name,
                engagement_id="project",
                title="Operator title",
                provider_profile_id="provider",
                model="fixture",
                metadata={
                    "initial_title_state": "operator",
                    "opaque": {"z": 1, "a": 2},
                },
            )
            for name in [
                "existing",
                "failure",
                "eof",
                "stop",
                "gap-admission",
                "gap-claim",
                "gap-answer",
                "gap-complete",
                "stale-worker",
                "settings-during-answer",
            ]
        ]
        messages = [
            fixed(
                ChatMessage,
                "existing-user",
                engagement_id="project",
                session_id="existing",
                sequence=1,
                role="user",
                content="Earlier exchange",
            ),
            fixed(
                ChatMessage,
                "existing-answer",
                engagement_id="project",
                session_id="existing",
                sequence=2,
                role="assistant",
                content="Earlier reply",
            ),
        ]
        for record in [*projects, *profiles, *sessions, *messages]:
            store.create(record)
        store.create(
            fixed(
                ChatMessage,
                str(UUID(int=999999)),
                engagement_id="project",
                session_id="retained-collision",
                sequence=1,
                role="user",
                content="Retained collision sentinel",
            )
        )
        with sqlite3.connect(path) as connection:
            stamp = BASE.strftime("%Y-%m-%d %H:%M:%S.%f")
            connection.execute(
                "INSERT INTO session_projections(session_id,revision,digest) VALUES(?,?,?)",
                ("existing", 7, "a" * 64),
            )
            connection.execute(
                "INSERT INTO operation_events(id,operation_id,operation_kind,engagement_id,sequence,event_type,payload,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "protected-operation",
                    "existing",
                    "chat",
                    "project",
                    1,
                    "fixture.saved",
                    '{"z": 1, "a": "λ retained"}',
                    stamp,
                ),
            )
            connection.execute(
                "INSERT INTO run_events(id,run_id,sequence,event_type,payload,occurred_at) VALUES(?,?,?,?,?,?)",
                (
                    "protected-event",
                    "existing",
                    1,
                    "fixture.saved",
                    '{"opaque": true}',
                    stamp,
                ),
            )
            connection.execute(
                "INSERT INTO resource_relations(id,project_id,source_kind,source_id,predicate,target_kind,target_id,provenance,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "protected-relation",
                    "project",
                    "chat_sessions",
                    "existing",
                    "fixture",
                    "chat_sessions",
                    "failure",
                    '{"z": 1, "a": 2}',
                    1,
                    stamp,
                    stamp,
                ),
            )

        def snapshot():
            with sqlite3.connect(path) as connection:
                connection.row_factory = sqlite3.Row
                rows = {
                    row["id"]: dict(row)
                    for row in connection.execute("SELECT * FROM entities ORDER BY id")
                }
                records = {
                    identity: {
                        "kind": row["kind"],
                        "payload": json.loads(row["payload"]),
                    }
                    for identity, row in rows.items()
                }
                search = {
                    row["id"]: dict(row)
                    for row in connection.execute(
                        "SELECT * FROM search_documents ORDER BY id"
                    )
                }
                protected = {
                    table: [
                        dict(row)
                        for row in connection.execute(
                            f"SELECT * FROM {table} ORDER BY 1"
                        )
                    ]
                    for table in PROTECTED
                }
                return records, rows, search, protected

        original_transaction = NebulaStore.transaction

        @contextmanager
        def observed_transaction(current):
            before = snapshot() if state["active"] else None
            try:
                with original_transaction(current) as transaction:
                    yield transaction
            except Exception as error:
                if before is not None:
                    after = snapshot()
                    assert before == after, (
                        "failed transaction leaked a durable mutation"
                    )
                    state["transaction_attempts"].append(
                        {
                            "phase": PHASE.get(),
                            "committed": False,
                            "error_class": type(error).__name__,
                            "error": str(error),
                            "before_snapshot_sha256": digest(before[1]),
                            "after_snapshot_sha256": digest(after[1]),
                        }
                    )
                raise
            if before is not None:
                after = snapshot()
                state["transaction_attempts"].append(
                    {
                        "phase": PHASE.get(),
                        "committed": True,
                        "before_snapshot_sha256": digest(before[1]),
                        "after_snapshot_sha256": digest(after[1]),
                    }
                )
                delta = changes(before[0], after[0])
                if delta or before[2:] != after[2:]:
                    state["phases"].append(
                        {
                            "phase": PHASE.get(),
                            "changes": delta,
                            "search_changes": changes(before[2], after[2]),
                            "protected_sha256": digest(after[3]),
                            "snapshot_sha256": digest(after[1]),
                        }
                    )

        def observed_method(method, label):
            def invoke(current, *args, **kwargs):
                token = PHASE.set(label)
                try:
                    return method(current, *args, **kwargs)
                finally:
                    PHASE.reset(token)

            return invoke

        services = []

        def inert_chat(current, **kwargs):
            kwargs.update(
                provider_factory=lambda profile: FixtureProvider(
                    provider_from_profile(profile).config, state
                ),
                workspace_resolver=lambda _identity: workspace,
                tool_suggestion_client=unexpected,
                worker_id="fixture-worker",
            )
            service = ChatService(current, **kwargs)
            services.append(service)
            return service

        def app_for(current):
            credentials = CredentialStore(keyring_backend=NullKeyring())
            artifacts = ArtifactStore(root / "artifacts")
            runtime = HarnessRuntimeService(
                current,
                credential_store=credentials,
                workspace_resolver=unexpected,
                adapter_factory=unexpected,
                artifact_store=artifacts,
            )
            with patch("nebula.v3.api.ChatService", side_effect=inert_chat):
                return create_app(
                    current,
                    credential_store=credentials,
                    harness_runtime_service=runtime,
                    artifact_store=artifacts,
                    auth_token="fixture-core",
                    enable_executable_missions=False,
                    bootstrap_workspace=False,
                )

        app = app_for(store)
        initial = snapshot()
        with sqlite3.connect(path) as connection:
            schema_sql = [
                row[0]
                for row in connection.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name IN (?,?,?,?) AND sql IS NOT NULL ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name",
                    PROTECTED,
                )
            ]
        captured = []
        phase_cases = []
        vectors = request_vectors()
        with ExitStack() as guards:
            guards.enter_context(obs.frozen())
            guards.enter_context(patch("nebula.v3.chat.uuid4", side_effect=obs.uuid))
            guards.enter_context(patch("nebula.v3.chat.utc_now", side_effect=obs.now))
            guards.enter_context(
                patch("nebula.v3.storage.utc_now", side_effect=obs.writer)
            )
            guards.enter_context(patch("nebula.v3.api.utc_now", return_value=NOW))
            guards.enter_context(
                patch.object(NebulaStore, "transaction", new=observed_transaction)
            )
            for name, label in [
                ("_persist_turn_inputs", "admission"),
                ("_claim_execution", "claim"),
                ("_persist", "answer"),
                ("_complete_turn", "complete"),
                ("_release_execution", "release"),
                ("cancel_turn", "cancel"),
                ("record_turn_outcome", "outcome"),
                ("_fail_closed_turn", "failure"),
                ("_interrupt_orphaned_turn", "restart-interrupt"),
            ]:
                guards.enter_context(
                    patch.object(
                        ChatService,
                        name,
                        new=observed_method(getattr(ChatService, name), label),
                    )
                )
            for case in cases(vectors):
                state["active"] = False
                if case.get("action") == "reopen":
                    database.dispose()
                    database = Database(path, bootstrap=False)
                    store = NebulaStore(database)
                    app = app_for(store)
                obs.clear(case)
                state.update(
                    active=True,
                    phases=[],
                    transaction_attempts=[],
                    provider_requests=[],
                    provider_events=[],
                    scenario=case.get("scenario", "success"),
                    entered=asyncio.Event(),
                    release=asyncio.Event(),
                )
                before = snapshot()
                headers = case.get("headers", {"Authorization": "Bearer fixture-core"})
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                    base_url=ORIGIN,
                ) as client:
                    pending = asyncio.create_task(
                        client.request(
                            case["method"],
                            case["path"],
                            json=case["body"],
                            headers=headers,
                        )
                    )
                    if state["scenario"] == "stop":
                        await asyncio.wait_for(state["entered"].wait(), timeout=10)
                        turn = store.list_session_entities(ChatTurn, "stop")[-1]
                        duplicate = await client.post(
                            "/api/v1/chat/completions",
                            json=case["body"],
                            headers=headers,
                        )
                        case["expected_duplicate"] = {
                            "status": duplicate.status_code,
                            "body": normalize(duplicate.json()),
                        }
                        attached = asyncio.create_task(
                            client.get(
                                f"/api/v1/chat/turns/{turn.id}/events?after=2",
                                headers=headers,
                            )
                        )

                        async def attached_viewer_ready():
                            while (
                                services[-1]._active_provider_turns[turn.id].followers
                                < 2
                            ):
                                await asyncio.sleep(0)

                        await asyncio.wait_for(attached_viewer_ready(), timeout=10)
                        stopped = await client.post(
                            f"/api/v1/chat/turns/{turn.id}/cancel", headers=headers
                        )
                        case["expected_stop"] = {
                            "status": stopped.status_code,
                            "body": normalize(stopped.json()),
                        }
                        reattached = await asyncio.wait_for(attached, timeout=10)
                        case["expected_active_reattach"] = {
                            "status": reattached.status_code,
                            "events": parse_sse(reattached.text),
                        }
                    response = await asyncio.wait_for(pending, timeout=10)
                    for service in services:
                        if service._naming_tasks:
                            await asyncio.gather(*list(service._naming_tasks))
                after = snapshot()
                state["active"] = False
                sse = response.headers.get("content-type", "").startswith(
                    "text/event-stream"
                )
                body = parse_sse(response.text) if sse else normalize(response.json())
                if case["name"] in {
                    "new-greeting-stream",
                    "existing-stream",
                    "admission-message-collision-retry",
                }:
                    assert response.status_code == 200 and body[-1]["event"] == "done"
                    assert len(state["provider_requests"]) == 1
                if case["name"] in {
                    "admission-rollback-session",
                    "admission-rollback-turn",
                }:
                    assert response.status_code == 409 and before == after
                if state["scenario"] in {"overloaded", "eof", "stop"}:
                    assert response.status_code == 200
                    assert body[-1]["event"] == (
                        "cancelled" if state["scenario"] == "stop" else "error"
                    )
                assert all(not service._active_provider_turns for service in services)
                captured.append(
                    {
                        **case,
                        "raw_body": response.request.content.decode(),
                        "expected": {
                            "status": response.status_code,
                            "events" if sse else "body": body,
                        },
                        "expected_cache_control": response.headers.get("cache-control"),
                        "expected_raw_sse": normalized_sse(response.text)
                        if sse
                        else None,
                        "expected_provider_requests": deepcopy(
                            state["provider_requests"]
                        ),
                        "expected_provider_events": deepcopy(state["provider_events"]),
                        "expected_commit_phases": deepcopy(state["phases"]),
                        "expected_transaction_attempts": deepcopy(
                            state["transaction_attempts"]
                        ),
                        "expected_changes": changes(before[0], after[0]),
                        "expected_search_changes": changes(before[2], after[2]),
                        "before_snapshot_sha256": digest(before[1]),
                        "after_snapshot_sha256": digest(after[1]),
                        "before_protected_sha256": digest(before[3]),
                        "after_protected_sha256": digest(after[3]),
                        **obs.fields(),
                    }
                )
                if "request_vector" in case:
                    vector = next(
                        item
                        for item in vectors
                        if item["name"] == case["request_vector"]
                    )
                    vector["expected_http"] = captured[-1]["expected"]
                    assert response.status_code == 422
                assert before[3] == after[3], "plain text mutated a protected ledger"
                touched = {
                    item["after"]["payload"]["id"]
                    if item["after"]
                    else item["before"]["payload"]["id"]
                    for item in captured[-1]["expected_changes"]
                }
                for identity in before[1].keys() & after[1].keys() - touched:
                    assert before[1][identity] == after[1][identity], (
                        "unrelated entity raw envelope changed"
                    )
            # Step actual writers without a producer whose exception cleanup
            # could overwrite the abrupt process gap being observed.
            for ordinal, scenario in enumerate(
                [
                    "gap-admission",
                    "gap-claim",
                    "gap-answer",
                    "gap-complete",
                    "stale-worker",
                    "settings-during-answer",
                ],
                start=100,
            ):
                case = {
                    "name": scenario,
                    "body": {**deepcopy(REQUEST), "session_id": scenario},
                    "generated_ids": [
                        str(UUID(int=ordinal * 100 + index)) for index in range(1, 33)
                    ],
                }
                obs.clear(case)
                state.update(
                    active=True,
                    phases=[],
                    transaction_attempts=[],
                    provider_requests=[],
                    provider_events=[],
                )
                before = snapshot()
                service = services[-1]
                steps = []

                def step(name, action):
                    start = snapshot()
                    trace_start = len(obs.trace)
                    phase_start = len(state["phases"])
                    try:
                        value = action()
                        expected = {"ok": True}
                        if hasattr(value, "model_dump"):
                            expected["value"] = value.model_dump(mode="json")
                    except Exception as error:
                        expected = {
                            "ok": False,
                            "error_class": type(error).__name__,
                            "error": str(error),
                        }
                    end = snapshot()
                    steps.append(
                        {
                            "step": name,
                            "expected": expected,
                            "expected_changes": changes(start[0], end[0]),
                            "expected_search_changes": changes(start[2], end[2]),
                            "expected_commit_phases": deepcopy(
                                state["phases"][phase_start:]
                            ),
                            "expected_factory_trace": deepcopy(obs.trace[trace_start:]),
                            "before_snapshot_sha256": digest(start[1]),
                            "after_snapshot_sha256": digest(end[1]),
                        }
                    )

                prepared = await service.prepare_async(
                    ChatCompletionRequest.model_validate(case["body"])
                )
                admitted = snapshot()
                case["prepared"] = {
                    "session_id": prepared.session.id,
                    "turn_id": prepared.turn.id,
                    "model_request": prepared.model_request.model_dump(mode="json"),
                    "resolved_model": prepared.resolved_model,
                    "tools_enabled": prepared.tools_enabled,
                    "inputs_persisted": prepared.inputs_persisted,
                }
                case["admission_changes"] = changes(before[0], admitted[0])
                if scenario != "gap-admission":
                    step("claim", lambda: service._claim_execution(prepared))
                    step(
                        "claim-same-owner-again",
                        lambda: service._claim_execution(prepared),
                    )
                if scenario in {
                    "gap-answer",
                    "gap-complete",
                    "stale-worker",
                    "settings-during-answer",
                }:
                    response = prepared.provider.response(prepared.model_request)
                    completion = service._completion(prepared, response)
                    case["model_response"] = response.model_dump(mode="json")
                    if scenario == "stale-worker":
                        step(
                            "operator-cancel",
                            lambda: service.cancel_turn(prepared.turn.id),
                        )
                    if scenario == "settings-during-answer":
                        latest = store.get(ChatSession, prepared.session.id)
                        step(
                            "operator-settings",
                            lambda: store.update(
                                ChatSession,
                                latest.id,
                                {
                                    "title": "Renamed while generating",
                                    "metadata": {
                                        **latest.metadata,
                                        "reasoning_effort": "high",
                                        "allow_subagents": True,
                                        "opaque_during_turn": {"z": 9, "a": "retained"},
                                    },
                                },
                                expected_revision=latest.revision,
                            ),
                        )
                    step(
                        "append-answer", lambda: service._persist(prepared, completion)
                    )
                    case["completion"] = completion.model_dump(mode="json")
                    if scenario in {"gap-complete", "settings-during-answer"}:
                        step(
                            "complete",
                            lambda: service._complete_turn(prepared, completion),
                        )
                    if scenario in {"stale-worker", "settings-during-answer"}:
                        step("release", lambda: service._release_execution(prepared))
                        step(
                            "release-again",
                            lambda: service._release_execution(prepared),
                        )
                    if scenario == "stale-worker":
                        step(
                            "cancel-again",
                            lambda: service.cancel_turn(prepared.turn.id),
                        )
                gap = snapshot()
                case["before_reopen_records"] = [
                    gap[0][identity]
                    for identity in sorted(gap[0])
                    if gap[0][identity]["payload"].get("session_id") == scenario
                    or identity == scenario
                ]
                database.dispose()
                database = Database(path, bootstrap=False)
                store = NebulaStore(database)
                app = app_for(store)
                trace_start = len(obs.trace)
                phase_start = len(state["phases"])
                await services[-1].startup()
                restarted = snapshot()
                case["restart"] = {
                    "expected_changes": changes(gap[0], restarted[0]),
                    "expected_search_changes": changes(gap[2], restarted[2]),
                    "expected_commit_phases": deepcopy(state["phases"][phase_start:]),
                    "expected_factory_trace": deepcopy(obs.trace[trace_start:]),
                }
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                    base_url=ORIGIN,
                ) as client:
                    replay = await client.get(
                        f"/api/v1/chat/turns/{prepared.turn.id}/events",
                        headers={"Authorization": "Bearer fixture-core"},
                    )
                sse = replay.headers.get("content-type", "").startswith(
                    "text/event-stream"
                )
                case["replay"] = {
                    "status": replay.status_code,
                    "events" if sse else "body": parse_sse(replay.text)
                    if sse
                    else normalize(replay.json()),
                }
                after = snapshot()
                case.update(
                    steps=steps,
                    expected_commit_phases=deepcopy(state["phases"]),
                    expected_transaction_attempts=deepcopy(
                        state["transaction_attempts"]
                    ),
                    expected_changes=changes(before[0], after[0]),
                    expected_search_changes=changes(before[2], after[2]),
                    before_snapshot_sha256=digest(before[1]),
                    after_snapshot_sha256=digest(after[1]),
                    **obs.fields(),
                )
                for item in steps:
                    should_fail = (
                        scenario == "stale-worker" and item["step"] == "append-answer"
                    )
                    assert item["expected"]["ok"] is not should_fail, item
                assert not state["provider_requests"], (
                    "phase-only recovery dispatched provider work"
                )
                assert before[3] == after[3]
                phase_cases.append(case)
                state["active"] = False
        final = snapshot()
        database.dispose()
        assert not unintended
    models = [
        ChatCompletionRequest,
        ChatRequestMessage,
        ChatContextAttachment,
        PendingProviderSubagent,
        ChatCompletionResponse,
        ChatTurn,
        ChatMessage,
        ChatSession,
        ChatTokenUsage,
        ModelRequest,
        ModelResponse,
        ModelStreamEvent,
    ]
    return {
        "format": "nebula-assistant-execution-oracle.v1",
        "clock": NOW.isoformat(),
        "origin": ORIGIN,
        "pydantic_version": pydantic.__version__,
        "models": {model.__name__: model_metadata(model) for model in models},
        "model_schemas": {
            model.__name__: model.model_json_schema() for model in models
        },
        "projects": [row.model_dump(mode="json") for row in projects],
        "initial_records": list(initial[0].values()),
        "initial_entity_rows": list(initial[1].values()),
        "initial_search_documents": list(initial[2].values()),
        "initial_protected_tables": initial[3],
        "schema_sql": schema_sql,
        "cases": captured,
        "request_vectors": vectors,
        "model_vectors": turn_vectors(),
        "phase_cases": phase_cases,
        "final_records": list(final[0].values()),
        "final_entity_rows": list(final[1].values()),
        "final_search_documents": list(final[2].values()),
        "final_protected_tables": final[3],
        "known_source_gaps": [
            "Provider deltas are volatile; terminal replay reconstructs only the final answer.",
            "Answer, completion, and claim release are separate durable transactions.",
            "A saved answer before Turn completion is not adopted by source startup; its follow request returns 409.",
            "Source startup leaves execution ownership on an already-complete Turn unchanged.",
        ],
        "unsupported_paths": [
            "Tools, native hooks, knowledge, MCP, SSH, browser resources, images, goals, subagents and peer messaging remain outside this fixture.",
            "Substantive initial conversation naming is not exercised; greetings use the real naming guard.",
            "Historical noncanonical ChatTurn coercions are model observations, not proof of Rust retained hydration parity.",
        ],
        "scope": {
            "provider": "inert ModelProvider with source require/config validation",
            "workspace": "empty isolated temporary directory",
            "optional_capabilities": "explicitly disabled by request",
            "naming": "greeting new session and operator-titled existing sessions use the real naming guards",
            "lifespan": "not entered",
        },
        "source_sha256": {
            name: sha256((ROOT / name).read_bytes()).hexdigest()
            for name in [
                "src/nebula/v3/api.py",
                "src/nebula/v3/chat.py",
                "src/nebula/v3/domain.py",
                "src/nebula/v3/providers.py",
                "src/nebula/v3/storage.py",
                "src/nebula/v3/chat_naming.py",
            ]
        },
    }


def collect_execution():
    return asyncio.run(_collect_execution())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = collect_execution()
    args.output.write_text(
        json.dumps(
            payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
