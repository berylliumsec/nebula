#!/usr/bin/env python3
"""Capture Assistant request coercion and errors through an isolated Python API.

Never enters Core lifespan or calls a provider/tool. The fixture includes full
normalized responses and final Assistant records, not just status assertions.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.database import Database
from nebula.v3.domain import ENTITY_MODEL_BY_KIND, ChatDecision, ChatReadCursor
from nebula.v3.storage import NebulaStore
from nebula.v3.diagnostic_guidance import load_catalog

ROOT = Path(__file__).resolve().parents[1]


def normalize(value):
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {
            k: "<generated>"
            if k in {"created_at", "updated_at", "request_id", "error_id"}
            else normalize(v)
            for k, v in value.items()
        }
    return value


def compact(value):
    """Keep boundary-sized repeated strings reviewable in the committed corpus."""
    if isinstance(value, str) and len(value) >= 1000 and len(set(value)) == 1:
        return {"$repeat_string": [value[0], len(value)]}
    if isinstance(value, list):
        return [compact(item) for item in value]
    if isinstance(value, dict):
        return {key: compact(item) for key, item in value.items()}
    return value


def collect_http():
    context = json.loads(
        (ROOT / "assistant-rs/compatibility/python-context.json").read_text()
    )
    cases = []

    def add(name, body=None, *, route="decision", path=None, method="PUT"):
        number = len(cases)
        cases.append(
            {
                "name": name,
                "method": method,
                "path": path
                or (
                    "/api/v1/chat/sessions/session/read-cursor"
                    if route == "cursor"
                    else f"/api/v1/chat/sessions/session/decisions/input-{number}"
                ),
                "body": body,
            }
        )

    decision = {"expected_revision": 0, "text": "Keep β exact"}
    cursor = {
        "expected_revision": 0,
        "device_id": "phone",
        "through_at": "2020-01-01T00:00:00Z",
    }
    for body in [None, [], "string", 1, True, {}]:
        add(f"body-{len(cases)}", body)
    for field in [
        "expected_revision",
        "action",
        "kind",
        "text",
        "source_message_id",
        "source_selection",
    ]:
        for value in [None, False, 1, 1.25, [], {}]:
            add(f"{field}-type-{len(cases)}", {**decision, field: value})
    for revision in [
        -1,
        "-1",
        "  +00  ",
        "0.000",
        "000",
        "0__0",
        "0_0",
        "1e0",
        "abc",
        "1.25",
        0.5,
        10**30,
        str(10**30),
    ]:
        add(f"revision-{len(cases)}", {**decision, "expected_revision": revision})
    for field, value in [
        ("action", "unknown"),
        ("action", " save"),
        ("kind", "unknown"),
        ("text", " "),
        ("text", "🦀" * 4001),
        ("source_selection", "β" * 200001),
    ]:
        add(f"{field}-bounds-{len(cases)}", {**decision, field: value})
    add(
        "multiple-fields",
        {
            "expected_revision": -1,
            "action": "unknown",
            "kind": 3,
            "text": None,
            "source_message_id": [],
            "source_selection": False,
        },
    )
    add(
        "unknown-fields-ignored",
        {**decision, "metadata": {"retained": False}, "unknown": True},
    )
    for field in cursor:
        body = dict(cursor)
        body.pop(field)
        add(f"cursor-missing-{field}", body, route="cursor")
    for field in ["device_id", "through_at"]:
        for value in [None, False, [], {}, "", 1, 1.25]:
            add(
                f"cursor-{field}-{len(cases)}", {**cursor, field: value}, route="cursor"
            )
    for through in [
        "2020-01-01",
        "2020-01-01T00:00:00",
        "2020-01-01_00:00:00Z",
        "2020-01-01t00:00:00z",
        "2020-01-01T03:04:05.123456789+02:30",
        "1577836800",
        "1577836800000",
        1577836800,
        1577836800000,
        1577836800.1234567,
        "x",
        "2020-13-01",
        "2020-01-32",
        "2020-01-01T25:00:00Z",
        "2020-01-01T00:00:00+25:00",
        10**30,
        -(10**30),
    ]:
        add(
            f"cursor-time-{len(cases)}",
            {**cursor, "device_id": f"device-{len(cases)}", "through_at": through},
            route="cursor",
        )
    add("cursor-device-bound", {**cursor, "device_id": "🦀" * 201}, route="cursor")
    for method, path, body in [
        ("GET", "/api/v1/chat/sessions/missing/decisions", None),
        ("PUT", "/api/v1/chat/sessions/missing/decisions/x", decision),
        (
            "PUT",
            "/api/v1/chat/sessions/session/decisions/missing",
            {"expected_revision": 1, "text": "update"},
        ),
        (
            "PUT",
            "/api/v1/chat/sessions/session/decisions/source-missing",
            {**decision, "source_message_id": "missing"},
        ),
        (
            "PUT",
            "/api/v1/chat/sessions/session/decisions/source-foreign",
            {**decision, "source_message_id": "foreign-message"},
        ),
        (
            "PUT",
            "/api/v1/chat/sessions/session/decisions/source-selection",
            {
                **decision,
                "source_message_id": "source",
                "source_selection": "not present",
            },
        ),
        ("PUT", "/api/v1/chat/sessions/session/decisions/retry", decision),
        ("PUT", "/api/v1/chat/sessions/session/decisions/retry", decision),
        (
            "PUT",
            "/api/v1/chat/sessions/session/decisions/retry",
            {"expected_revision": 3, "text": "stale"},
        ),
        (
            "PUT",
            "/api/v1/chat/sessions/session/decisions/retry",
            {"expected_revision": 1, "text": "corrected"},
        ),
        (
            "PUT",
            "/api/v1/chat/sessions/peer/decisions/retry",
            {"expected_revision": 2, "text": "foreign"},
        ),
        (
            "PUT",
            "/api/v1/chat/sessions/session/read-cursor",
            {**cursor, "device_id": "stale-cursor"},
        ),
        (
            "PUT",
            "/api/v1/chat/sessions/session/read-cursor",
            {**cursor, "device_id": "stale-cursor", "expected_revision": 7},
        ),
        ("GET", "/api/v1/chat/sessions/session/decisions", None),
    ]:
        add(f"lifecycle-{len(cases)}", body, path=path, method=method)
    add("negative-zero-float", {**decision, "expected_revision": -0.0})
    add("negative-zero-integer")
    cases[-1]["raw_body"] = '{"expected_revision":-0,"text":"Keep negative zero"}'
    for stamp in ["0000-01-01T00:00:00Z", "0000-01-01", -62167219200000, -62167219200]:
        add(
            f"cursor-year-boundary-{len(cases)}",
            {**cursor, "device_id": f"year-{len(cases)}", "through_at": stamp},
            route="cursor",
        )
    add(
        "large-stale-decision",
        {"expected_revision": 10**30, "text": "must not replace"},
        path="/api/v1/chat/sessions/session/decisions/retry",
    )
    add(
        "large-stale-cursor",
        {**cursor, "device_id": "stale-cursor", "expected_revision": 10**30},
        route="cursor",
    )
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-http-oracle-"
    ) as directory:
        root = Path(directory)
        database = Database(root / "nebula.db")
        try:
            store = NebulaStore(database)
            for item in context["initial"]:
                store.create(
                    ENTITY_MODEL_BY_KIND[item["kind"]].model_validate(item["payload"])
                )
            app = create_app(
                store,
                artifact_store=ArtifactStore(root / "artifacts"),
                auth_token="fixture-core",
                enable_executable_missions=False,
                bootstrap_workspace=False,
            )
            client = TestClient(app, raise_server_exceptions=False)
            try:
                for case in cases:
                    response = client.request(
                        case["method"],
                        case["path"],
                        **(
                            {"content": case["raw_body"]}
                            if "raw_body" in case
                            else {"json": case["body"]}
                        ),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": "Bearer fixture-core",
                            "X-Nebula-Operation-ID": "fixture-operation",
                        },
                    )
                    case["expected"] = {
                        "status": response.status_code,
                        "body": normalize(response.json()),
                    }
            finally:
                client.close()
            final = [
                {
                    "kind": model.entity_kind,
                    "payload": normalize(item.model_dump(mode="json")),
                }
                for model in [ChatDecision, ChatReadCursor]
                for item in store.list_entities(model, limit=1000)
            ]
        finally:
            database.dispose()
    catalog = load_catalog()
    return compact(
        {
            "format": "nebula.assistant-http-oracle/v1",
            "encoding": "Repeated strings use {$repeat_string: [character, count]}",
            "initial": context["initial"],
            "cases": cases,
            "final": final,
            "guidance": {
                "feature": catalog["features"]["chat"],
                "reasons": catalog["reason_families"],
            },
            "source_sha256": {
                f"src/nebula/v3/{name}.py": sha256(
                    (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
                ).hexdigest()
                for name in [
                    "api",
                    "chat_catchup",
                    "chat_decisions",
                    "diagnostic_guidance",
                    "storage",
                ]
            },
        }
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = collect_http()
    args.output.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {"cases": len(fixture["cases"]), "final_records": len(fixture["final"])}
        )
    )


if __name__ == "__main__":
    main()
