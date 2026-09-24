#!/usr/bin/env python3
"""Capture retained Results and context-source reads from an isolated Python API.

Journey: inspect saved outputs, page across replaced and non-assistant messages,
read bounded recorded diffs, and recover the same view after database reopen.
The message, turn, tool-call and artifact records are authoritative. All files
are harmless fixture bytes under a temporary artifact root. No application
lifespan, provider, tool, document SDK or ambient credential discovery runs.
This oracle establishes HTTP compatibility, not production UI acceptance.
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
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import (
    _CHAT_INSTRUCTIONS,
    _CHAT_TOOL_INSTRUCTIONS,
    _CHAT_TOOL_RESULT_INSTRUCTIONS,
)
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import (
    Artifact,
    ChatMessage,
    ChatSession,
    ChatTurn,
    Engagement,
    PairedDeviceSession,
    ToolCall,
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


def policies():
    return {
        "text_only": _CHAT_INSTRUCTIONS,
        "tools_enabled": _CHAT_TOOL_INSTRUCTIONS
        + "\n\nFinal synthesis policy:\n"
        + _CHAT_TOOL_RESULT_INSTRUCTIONS,
        "instruction_note_provider": "Core assistant policy; retrieval and operator context are additional inputs.",
        "instruction_note_harness": "Harness internal instructions are not fully exposed to Nebula.",
    }


def initial_results():
    records = []
    dependencies = []
    blobs = []
    projects = [
        Engagement(id=identity, name=identity, created_at=BASE, updated_at=BASE)
        for identity in ["project", "other-project"]
    ]

    def session(identity, *, backend="provider", project="project", metadata=None):
        records.append(
            ChatSession(
                id=identity,
                engagement_id=project,
                title=f"Results {identity}",
                backend=backend,
                provider_profile_id="fixture-provider"
                if backend == "provider"
                else None,
                harness_profile_id="fixture-harness" if backend == "harness" else None,
                harness_session_id="fixture-harness-session"
                if backend == "harness"
                else None,
                model="fixture-model",
                metadata=metadata or {},
                created_at=BASE,
                updated_at=BASE,
            )
        )

    def message(
        identity,
        session_id,
        sequence,
        *,
        role="assistant",
        content="Saved text",
        **fields,
    ):
        records.append(
            ChatMessage(
                id=identity,
                engagement_id=fields.pop("engagement_id", "project"),
                session_id=session_id,
                sequence=sequence,
                role=role,
                content=content,
                created_at=BASE,
                updated_at=BASE,
                **fields,
            )
        )

    def turn(identity, final_message, *, session_id="results", project="project"):
        records.append(
            ChatTurn(
                id=identity,
                engagement_id=project,
                session_id=session_id,
                backend="provider",
                provider_profile_id="fixture-provider",
                model="fixture-model",
                final_message_id=final_message,
                status="complete",
                created_at=BASE,
                updated_at=BASE,
            )
        )

    def call(identity, turn_id, *, project="project", session_id="results", **fields):
        dependencies.append(
            ToolCall(
                id=identity,
                engagement_id=project,
                run_id="fixture-retained-run",
                origin="chat",
                chat_session_id=session_id,
                chat_turn_id=turn_id,
                tool_name=f"fixture.{identity}",
                risk_class="local_read",
                created_at=BASE,
                updated_at=BASE,
                **fields,
            )
        )

    def artifact(
        identity, data=b"Harmless recorded fixture diff\n", *, write=True, **fields
    ):
        digest = fields.pop("digest", sha256(identity.encode() + data).hexdigest())
        path = fields.pop("storage_path", f"sha256/{digest[:2]}/{digest[2:4]}/{digest}")
        dependencies.append(
            Artifact(
                id=identity,
                engagement_id=fields.pop("engagement_id", "project"),
                sha256=digest,
                size=len(data),
                filename=f"{identity}.diff",
                media_type="text/plain",
                storage_path=path,
                source=fields.pop("source", "harness-file-diff"),
                metadata=fields.pop(
                    "metadata", {"harness_turn_id": "harness-recorded"}
                ),
                created_at=BASE,
                updated_at=BASE,
                **fields,
            )
        )
        if write:
            blobs.append({"storage_path": path, "hex": data.hex()})

    for identity in [
        "results",
        "fences",
        "paging",
        "ties",
        "large-sequences",
        "context-opaque",
        "empty",
    ]:
        session(identity)
    session("foreign", project="other-project")
    for identity, project_id in [("empty-project", ""), ("long-project", "β" * 201)]:
        session(identity, project=project_id)
        message(
            f"{identity}-output",
            identity,
            1,
            engagement_id=project_id,
            content="```text\nHistorical project scope\n```",
            metadata={"context_attachments": ["historical scope"]},
        )
        message(
            f"{identity}-wrong-project",
            identity,
            2,
            engagement_id="other-project",
            content="```text\nMust remain outside this scope\n```",
            metadata={"context_attachments": ["wrong project"]},
        )
    session("temporary", metadata={"temporary_assistant": True})
    session("archived", metadata={"archived_at": BASE.isoformat()})
    session("harness", backend="harness", metadata={"tools_enabled": True})
    policy_values = [
        True,
        False,
        None,
        0,
        1,
        "",
        "false",
        [],
        [False],
        {},
        {"enabled": False},
    ]
    for number, value in enumerate(policy_values):
        session(f"policy-{number}", metadata={"tools_enabled": value})

    message(
        "user-output",
        "results",
        1,
        role="user",
        content="```text\nUser code is not a result\n```",
        metadata={"context_attachments": ["user-context"]},
    )
    message(
        "replaced-output",
        "results",
        2,
        content="```text\nReplaced\n```",
        metadata={"retracted_at": BASE.isoformat(), "context_attachments": ["hidden"]},
    )
    message(
        "rich-output",
        "results",
        3,
        content="Saved answer\n```python\nprint('fixture β')\n```\n```\nplain\n```",
        content_blocks=[
            {"type": "text", "text": "Not a retained result"},
            {
                "type": "code",
                "text": "block code",
                "language": "rust",
                "alt": "Preferred label",
            },
            {"type": "image", "artifact_id": "image-only", "alt": "Fixture image"},
            {"type": "artifact", "artifact_id": "download-only"},
            {"type": "code", "text": "", "language": "", "alt": ""},
            {"type": "code", "text": "language label", "language": "json"},
            {"type": "activity", "activity_id": "retained-activity"},
        ],
        citations=[
            {
                "source_id": "source-z",
                "name": "Second alphabetically",
                "chunk_id": "same",
                "excerpt": "β first",
            },
            {
                "source_id": "source-a",
                "name": "First alphabetically",
                "chunk_id": "same",
                "excerpt": "second",
            },
        ],
        metadata={
            "harness_turn_id": "harness-recorded",
            "context_attachments": None,
            "operator_decisions": {"accepted": False},
        },
    )
    message(
        "system-output",
        "results",
        4,
        role="system",
        content="```text\nSystem code\n```",
        metadata={"operator_decisions": "system context"},
    )
    message(
        "repeat-diff",
        "results",
        5,
        content="No fences",
        metadata={
            "harness_turn_id": "harness-recorded",
            "context_attachments": False,
            "operator_decisions": ["read only"],
        },
    )
    message(
        "wrong-project-message",
        "results",
        6,
        engagement_id="other-project",
        content="```text\nMust not appear\n```",
        metadata={"operator_decisions": ["foreign"]},
    )
    for identity, owner, project in [
        ("foreign-output", "foreign", "other-project"),
        ("temporary-output", "temporary", "project"),
        ("archived-output", "archived", "project"),
        ("harness-output", "harness", "project"),
    ]:
        message(
            identity,
            owner,
            1,
            engagement_id=project,
            content="```text\nScoped retained output\n```",
            metadata={"context_attachments": [identity]},
        )
    for number, value in enumerate(
        [False, None, [], 0, "", {}, True, [False], {"at": "fixture"}]
    ):
        message(
            f"retraction-{number}",
            "results",
            10 + number,
            content=f"```text\nRetraction {number}\n```",
            metadata={"retracted_at": value},
        )
    for number, value in enumerate([42, True, 0]):
        message(
            f"numeric-harness-{number}",
            "results",
            30 + number,
            metadata={"harness_turn_id": value},
        )
        artifact(f"numeric-diff-{number}", metadata={"harness_turn_id": value})

    turn("result-turn", "rich-output")
    turn("repeat-turn", "repeat-diff")
    turn("empty-final-turn", None)
    turn(
        "foreign-final-turn",
        "rich-output",
        session_id="foreign",
        project="other-project",
    )
    for number, status in enumerate(
        [
            "proposed",
            "waiting_approval",
            "approved",
            "running",
            "denied",
            "cancelled",
            "failed",
            "complete",
        ]
    ):
        call(
            f"tool-{7 - number}",
            "result-turn",
            status=status,
            error="Recorded failure" if status == "failed" else None,
            result_artifact_id="retained-tool-output" if status == "complete" else None,
            result={"opaque": [None, False, "β"]},
            arguments={"fixture": True},
            usage={"tokens": 0},
            metadata={"retained": True},
        )
    call("tool-repeat", "repeat-turn", status="complete", result="retained only")
    call(
        "tool-foreign-turn",
        "foreign-final-turn",
        status="complete",
        mcp_server_id="fixture-server",
        mcp_tool_name="read",
        vendor_tool_name="vendor-read",
    )
    call("tool-missing-turn", "missing", status="failed", error="Missing retained turn")
    call("tool-no-turn", None)
    call("tool-empty-final", "empty-final-turn")
    call("tool-wrong-project", "result-turn", project="other-project")
    call("tool-wrong-session", "result-turn", session_id="foreign")

    valid = b"--- recorded/fixture.txt\n+++ recorded/fixture.txt\n@@ -1 +1 @@\n-before\n+after\n"
    artifact("diff-valid", valid, digest=sha256(valid).hexdigest())
    artifact("diff-missing", write=False)
    artifact("diff-mismatched-path", write=False, storage_path="sha256/mismatch.diff")
    artifact("diff-outside-path", write=False, storage_path="../outside-fixture.diff")
    artifact("diff-bounded-utf8", b"x" * 8191 + "€tail".encode())
    artifact("diff-invalid-utf8", b"\xff\xc0\xaf\xed\xa0\x80\xf0\x9f\x92fixture\n")
    # Preview reads enforce digest-derived path identity, not full blob verification.
    artifact(
        "diff-unverified-content",
        b"Recorded bytes with an identity-only digest\n",
        digest="a" * 64,
    )
    artifact("diff-wrong-project", engagement_id="other-project")
    artifact("diff-wrong-source", source="other-source")
    artifact("diff-wrong-harness", metadata={"harness_turn_id": "different"})

    fence_texts = [
        "```\nempty language\n```",
        "```python\r\nCRLF\r\n```",
        "``` λ language \n😀 Unicode\n```",
        "```one\na``` between ```two\nb```",
        "```never closed\ntext",
        "```header`with`ticks\nbody```",
        "````four\nfour ending````",
        "```empty\n```",
        "```header\u2028still header\nbody```",
        "```a```b\nbody``` and tail",
        "Inline ```not-a-fence``` only",
    ]
    for number, content in enumerate(fence_texts):
        message(f"fence-{number:02}", "fences", number + 1, content=content)
    for number in range(45):
        message(
            f"page-{number:02}",
            "paging",
            number + 1,
            role="user" if number == 0 else "assistant",
            content=f"```text\nPage {number}\n```",
            metadata={
                "context_attachments": [number],
                **({"retracted_at": "recorded"} if number in {1, 4, 5, 40, 44} else {}),
            },
        )
    # Insertion order differs from ID order; neither projection declares a tie breaker.
    for identity in ["tie-z", "tie-a", "tie-m"]:
        message(
            identity,
            "ties",
            7,
            content=f"```text\n{identity}\n```",
            metadata={"context_attachments": [identity]},
        )
    for number, sequence in enumerate(
        [2**63 + 1, 2**63, 2**63 - 1, 2**64, 2**64 - 1, 2**100]
    ):
        message(
            f"large-{number}",
            "large-sequences",
            sequence,
            content=f"```text\n{sequence}\n```",
            metadata={"context_attachments": [sequence]},
        )
    opaque = [
        {},
        {"context_attachments": None},
        {"context_attachments": False, "operator_decisions": []},
        {"context_attachments": 0, "operator_decisions": {}},
        {"context_attachments": "", "operator_decisions": None},
        {"context_attachments": [False]},
        {"context_attachments": "opaque string"},
        {"context_attachments": {"nested": None}},
        {"context_attachments": 1.5},
        {"context_attachments": False, "operator_decisions": [False]},
        {"context_attachments": None, "operator_decisions": True},
        {"operator_decisions": "opaque decision"},
        {"context_attachments": [], "operator_decisions": {"nested": []}},
        {"context_attachments": "context", "operator_decisions": None},
    ]
    for number, metadata in enumerate(opaque):
        message(
            f"opaque-{number:02}",
            "context-opaque",
            number + 1,
            role=["user", "assistant", "system"][number % 3],
            metadata=metadata,
        )

    for identity, token, revoked in [
        ("paired", "fixture-device", False),
        ("revoked", "fixture-revoked", True),
    ]:
        dependencies.append(
            PairedDeviceSession(
                id=identity,
                name="Fixture phone",
                token_sha256=sha256(token.encode()).hexdigest(),
                csrf_sha256=sha256(b"fixture-csrf").hexdigest(),
                created_at=BASE,
                updated_at=BASE,
                last_used_at=NOW,
                idle_expires_at=NOW + timedelta(days=1),
                absolute_expires_at=NOW + timedelta(days=60),
                revoked_at=BASE + timedelta(days=1) if revoked else None,
            )
        )
    return projects, records, dependencies, blobs, len(policy_values)


def results_cases(policy_count):
    cases = []

    def add(
        name, session="results", *, kind="results", params=None, service=True, **fields
    ):
        route = "context-sources" if kind == "context_sources" else "results"
        case = {
            "name": name,
            "method": "GET",
            "path": f"/api/v1/chat/sessions/{quote(session, safe='')}/{route}"
            + ("?" + urlencode(params) if params else ""),
            "body": None,
            **fields,
        }
        if service:
            values = dict(params or [])
            case["service"] = {
                "kind": kind,
                "session_id": session,
                "offset": int(float(values.get("offset", 0))),
            }
            if kind == "results":
                case["service"]["limit"] = int(values.get("limit", 40))
        cases.append(case)

    for kind in ["results", "context_sources"]:
        for session in [
            "results",
            "empty",
            "foreign",
            "empty-project",
            "long-project",
            "temporary",
            "archived",
            "harness",
            "fences",
            "ties",
            "large-sequences",
            "context-opaque",
        ]:
            add(f"{kind}-{session}", session, kind=kind)
        for offset in [0, 1, 4, 5, 39, 40, 41, 44, 45, 100]:
            add(
                f"{kind}-page-{offset}",
                "paging",
                kind=kind,
                params=[("offset", offset)],
            )
        for name, params in [
            ("offset-negative", [("offset", -1)]),
            ("offset-invalid", [("offset", "bad")]),
            ("offset-fraction", [("offset", "1.5")]),
            ("offset-empty", [("offset", "")]),
            ("offset-bool", [("offset", "true")]),
            ("offset-invalid-utf8", [("offset", "�")]),
            ("duplicate-offset-valid", [("offset", "bad"), ("offset", 1)]),
            ("duplicate-offset-invalid", [("offset", 1), ("offset", "bad")]),
            ("coerced-offset", [("offset", "+1.0")]),
            (
                "ignored-query",
                [
                    ("session_id", "missing"),
                    ("engagement_id", "missing"),
                    ("newest_first", "true"),
                ],
            ),
        ]:
            add(f"{kind}-{name}", kind=kind, params=params, service=False)
        for identity in ["missing", "rich-output", "β" * 201]:
            add(
                f"{kind}-missing-{len(identity)}-{identity[:8]}",
                identity,
                kind=kind,
                service=False,
            )
        add(f"{kind}-no-auth", kind=kind, auth={"headers": {}}, service=False)
        add(
            f"{kind}-bad-bearer",
            kind=kind,
            auth={"headers": {"Authorization": "Bearer incorrect"}},
            service=False,
        )
        add(
            f"{kind}-paired",
            kind=kind,
            auth={
                "headers": {
                    "Cookie": "nebula_device=fixture-device; nebula_csrf=fixture-csrf",
                    "Origin": ORIGIN,
                    "X-Nebula-CSRF": "fixture-csrf",
                }
            },
        )
        add(
            f"{kind}-revoked",
            kind=kind,
            auth={
                "headers": {
                    "Cookie": "nebula_device=fixture-revoked; nebula_csrf=fixture-csrf",
                    "Origin": ORIGIN,
                }
            },
            service=False,
        )
    for limit in [1, 2, 3, 100]:
        add(f"results-limit-{limit}", params=[("limit", limit)])
    for name, params in [
        ("zero", [("limit", 0)]),
        ("high", [("limit", 101)]),
        ("negative", [("limit", -1)]),
        ("fraction", [("limit", "1.5")]),
        ("empty", [("limit", "")]),
        ("bool", [("limit", "true")]),
        ("duplicates-valid", [("limit", "bad"), ("limit", 2)]),
        ("duplicates-invalid", [("limit", 2), ("limit", "bad")]),
        ("coerced", [("limit", " 02 ")]),
        ("multiple-errors", [("offset", -1), ("limit", 0)]),
        (
            "overrange-offset-invalid-limit",
            [("offset", 9223372036854775808), ("limit", 0)],
        ),
    ]:
        add(f"results-limit-{name}", params=params, service=False)
    add("results-replaced-page", params=[("offset", 1), ("limit", 1)])
    add("results-system-page", params=[("offset", 3), ("limit", 1)])
    add("results-no-artifact-store", artifacts_enabled=False)
    add("context-ignores-limit", kind="context_sources", params=[("limit", "bad")])
    for number in range(policy_count):
        add(f"context-policy-{number}", f"policy-{number}", kind="context_sources")
    add("results-reopen", action="reopen")
    add("context-reopen", "context-opaque", kind="context_sources")
    return cases


def collect_results():
    projects, records, dependencies, blobs, policy_count = initial_results()
    cases = results_cases(policy_count)
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-results-oracle-"
    ) as directory:
        root = Path(directory)
        database = Database(root / "nebula.db")
        store = NebulaStore(database)
        for record in [*projects, *records, *dependencies]:
            store.create(record)
        artifacts = ArtifactStore(root / "artifacts")
        for blob in blobs:
            target = artifacts.root / blob["storage_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes.fromhex(blob["hex"]))

        def client_for(current_store, enabled):
            return TestClient(
                create_app(
                    current_store,
                    artifact_store=artifacts if enabled else None,
                    credential_store=CredentialStore(keyring_backend=NullKeyring()),
                    auth_token="fixture-core",
                    enable_executable_missions=False,
                    bootstrap_workspace=False,
                ),
                raise_server_exceptions=False,
                base_url=ORIGIN,
            )

        clients = {enabled: client_for(store, enabled) for enabled in [True, False]}
        try:
            with patch("nebula.v3.api.utc_now", return_value=NOW):
                for case in cases:
                    if case.get("action") == "reopen":
                        for client in clients.values():
                            client.close()
                        database.dispose()
                        database = Database(root / "nebula.db", bootstrap=False)
                        store = NebulaStore(database)
                        clients = {
                            enabled: client_for(store, enabled)
                            for enabled in [True, False]
                        }
                    headers = {
                        "Authorization": "Bearer fixture-core",
                        "X-Nebula-Operation-ID": "fixture-operation",
                    }
                    if "auth" in case:
                        headers.pop("Authorization")
                        headers.update(case["auth"]["headers"])
                    response = clients[case.get("artifacts_enabled", True)].request(
                        case["method"], case["path"], json=case["body"], headers=headers
                    )
                    case["expected"] = {
                        "status": response.status_code,
                        "body": normalize(response.json()),
                    }
            final = [
                envelope(record)
                for model in [ChatSession, ChatMessage, ChatTurn]
                for record in store.list_entities(
                    model, limit=1000, include_temporary=True
                )
            ]
        finally:
            for client in clients.values():
                client.close()
            database.dispose()
    return {
        "format": "nebula.assistant-results-oracle/v1",
        "clock": NOW.isoformat(),
        "origin": ORIGIN,
        "normalization": "Preserve all retained values and timestamps; normalize only diagnostic request_id/error_id.",
        "projects": [envelope(record) for record in projects],
        "initial_records": [envelope(record) for record in records],
        "dependency_records": [envelope(record) for record in dependencies],
        "dependency_schemas": {
            model.entity_kind: model.model_json_schema()
            for model in [Artifact, ToolCall]
        },
        "artifact_blobs": blobs,
        "policies": policies(),
        "cases": cases,
        "final_records": final,
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in [
                "api",
                "chat_results",
                "chat",
                "application_model/workflow",
                "domain",
                "storage",
                "artifacts",
                "diagnostic_guidance",
            ]
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = collect_results()
    args.output.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(fixture["cases"]),
                "initial_records": len(fixture["initial_records"]),
                "dependency_records": len(fixture["dependency_records"]),
                "artifact_blobs": len(fixture["artifact_blobs"]),
                "sha256": sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
