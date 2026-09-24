#!/usr/bin/env python3
"""Capture passive decision snapshots using the real isolated source SQLite store."""

from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import event

from nebula.v3 import chat_decisions, domain
from nebula.v3.database import Database
from nebula.v3.storage import NebulaStore

ROOT = Path(os.environ.get("NEBULA_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
STAMP = "2020-01-01T00:00:00Z"
SQL_STAMP = "2020-01-01 00:00:00.000000"


def record(
    identifier,
    *,
    session="session",
    project="project",
    scope="conversation",
    status="active",
    text="Saved choice",
    **changes,
):
    payload = {
        "id": identifier,
        "created_at": STAMP,
        "updated_at": STAMP,
        "revision": 1,
        "engagement_id": project,
        "session_id": session,
        "scope": scope,
        "kind": "decision",
        "text": text,
        "status": status,
        "source_message_id": None,
        "source_session_id": None,
        **changes,
    }
    return {
        "id": identifier,
        "kind": "chat_decisions",
        "engagement_id": project,
        "chat_session_id": session,
        "revision": 1,
        "created_at": SQL_STAMP,
        "updated_at": SQL_STAMP,
        "payload": json.dumps(payload, ensure_ascii=False),
    }


def change_payload(row, changes, remove=()):
    row = deepcopy(row)
    payload = json.loads(row["payload"])
    payload.update(changes)
    for key in remove:
        payload.pop(key)
    row["payload"] = json.dumps(payload, ensure_ascii=False)
    return row


def digest(rows):
    return sha256(
        json.dumps(
            rows, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def collect_decision_preparation():
    assert Path(chat_decisions.__file__).resolve().is_relative_to(ROOT / "src")
    assert Path(domain.__file__).resolve().is_relative_to(ROOT / "src")
    datasets = {}
    base = [
        record(
            "a-project-null",
            session=None,
            scope="project",
            text=' Keep Unicode café 🌌 and "quoted" text ',
            source_message_id="source-message",
            source_session_id="source-session",
        ),
        record(
            "b-project-other-session",
            session="other",
            scope="project",
            kind="constraint",
            text="Use the selected project",
        ),
        record(
            "c-conversation-null",
            session=None,
            kind="question",
            text="Does the operator approve?",
        ),
        record(
            "d-conversation-session",
            text="Saved for this conversation",
            kind="assumption",
        ),
        record("e-conversation-other", session="other", text="Other conversation"),
        record("f-removed-null", session=None, status="removed"),
        record("g-superseded-session", status="superseded"),
        record("h-foreign", project="foreign", scope="project"),
        record(
            "i-empty-session",
            session="",
            text="Empty session selector is distinct from NULL",
        ),
    ]
    wrong_kind = record("j-wrong-kind", session=None, scope="project")
    wrong_kind["kind"] = "opaque-unrelated-kind"
    base.append(wrong_kind)
    # Missing status defaults to active only after hydration; padded strings are
    # model-normalized, not prefiltered by a raw JSON status predicate.
    base.extend(
        [
            change_payload(
                record("k-default-active", session=None),
                {},
                ["status", "scope", "kind", "source_message_id", "source_session_id"],
            ),
            change_payload(
                record("l-trimmed-active", session=None),
                {
                    "status": " active ",
                    "text": "  keep me  ",
                    "engagement_id": " project ",
                },
            ),
            change_payload(
                record("m-unmatched-raw-project", session="other", scope="project"),
                {"scope": " project "},
            ),
            change_payload(
                record("n-null-raw-project", session=None, scope="project"),
                {"scope": " project "},
            ),
            record(
                "o-unicode-controls",
                session=None,
                text="\u001ckeep\nline\u001f",
                source_message_id="",
                source_session_id="",
            ),
        ]
    )
    datasets["mixed"] = base
    datasets["invalid-inactive-null"] = [
        *base,
        record("z-invalid-null", session=None, status="removed", text=""),
    ]
    datasets["invalid-inactive-session"] = [
        *base,
        record("z-invalid-session", status="superseded", text=""),
    ]
    datasets["invalid-inactive-project"] = [
        *base,
        record(
            "z-invalid-project",
            session="other",
            scope="project",
            status="removed",
            kind="invalid-kind",
        ),
    ]
    datasets["invalid-excluded"] = [
        *base,
        record("z-invalid-excluded", session="not-selected", status="removed", text=""),
    ]
    datasets["ordered-invalid"] = [
        record("a-invalid-removed", session=None, status="removed", text=""),
        record("b-invalid-active", session=None, kind="invalid-second"),
    ]
    datasets["count100"] = [
        record(f"count-{i:03}", session=None, text="x") for i in range(100)
    ]
    datasets["count101"] = [
        record(f"count-{i:03}", session=None, text="x") for i in range(101)
    ]
    datasets["count100-inactive"] = [
        *datasets["count100"],
        record("z-removed", session=None, status="removed"),
    ]
    datasets["text40000"] = [
        record(f"unicode-{i:02}", session=None, text="🌌" * 4000) for i in range(10)
    ]
    datasets["text40001"] = [
        *datasets["text40000"],
        record("z-extra", session=None, text="x"),
    ]
    datasets["over-budget-invalid-last"] = [
        *datasets["count101"],
        record("z-invalid-last", session=None, status="removed", text=""),
    ]
    datasets["row-order"] = [
        record("z-older", session=None),
        record("a-newer", session=None),
    ]
    datasets["row-order"][0]["created_at"] = "2019-01-01 00:00:00.000000"
    datasets["row-order"][0] = change_payload(
        datasets["row-order"][0], {"created_at": "2019-01-01T00:00:00Z"}
    )
    mismatch = record("envelope-mismatch", session=None, project="project")
    mismatch = change_payload(
        mismatch, {"session_id": "payload-session", "engagement_id": "payload-project"}
    )
    datasets["envelope-mismatch"] = [mismatch]
    requests = []
    for session in [None, "session", "other", "", "missing"]:
        requests.append((f"mixed-{session}", "mixed", session, "project", False))
    for dataset in [
        "invalid-inactive-null",
        "invalid-inactive-session",
        "invalid-inactive-project",
        "invalid-excluded",
    ]:
        for session in [None, "session"]:
            requests.append(
                (dataset + "-" + str(session), dataset, session, "project", False)
            )
    for project in [None, "", "missing", "foreign"]:
        requests.append(
            ("project-" + str(project), "invalid-inactive-null", None, project, False)
        )
    for dataset in [
        "ordered-invalid",
        "count100",
        "count101",
        "count100-inactive",
        "text40000",
        "text40001",
        "over-budget-invalid-last",
        "row-order",
    ]:
        requests.append((dataset, dataset, None, "project", False))
    requests.append(
        ("denormalized-envelope-boundary", "envelope-mismatch", None, "project", True)
    )
    cases, boundaries = [], []
    with TemporaryDirectory(prefix="nebula-decision-preparation-") as temporary:
        path = Path(temporary) / "fixture.db"
        database = Database(path)
        store = NebulaStore(database)
        for name, dataset, session, project, boundary in requests:
            rows = datasets[dataset]
            with sqlite3.connect(path) as raw:
                raw.execute("DELETE FROM entities")
                for row in rows:
                    columns = list(row)
                    raw.execute(
                        f"INSERT INTO entities ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        tuple(row.values()),
                    )
            validation_order, statements = [], []
            original = domain.ChatDecision.model_validate

            def observed_validate(value, *args, **kwargs):
                validation_order.append(
                    value.get("id") if isinstance(value, dict) else None
                )
                return original(value, *args, **kwargs)

            def observed_statement(
                connection, cursor, statement, parameters, context, executemany
            ):
                if "entities" in statement:
                    statements.append(
                        {"sql": statement, "parameters": list(parameters)}
                    )

            def snapshot():
                with sqlite3.connect(path) as raw:
                    raw.row_factory = sqlite3.Row
                    return [
                        dict(row)
                        for row in raw.execute("SELECT * FROM entities ORDER BY id")
                    ]

            before = snapshot()
            event.listen(database.engine, "before_cursor_execute", observed_statement)
            try:
                with patch.object(
                    domain.ChatDecision, "model_validate", side_effect=observed_validate
                ):
                    value = chat_decisions.decision_snapshot(store, session, project)
                    expected = {
                        "accepted": True,
                        "snapshot": value,
                        "instructions": chat_decisions.decision_instructions(value),
                    }
            except ValidationError as error:
                expected = {
                    "accepted": False,
                    "error": {
                        "kind": "ValidationError",
                        "errors": jsonable_encoder(error.errors(include_url=False)),
                        "exception_preview": str(error)[:300],
                    },
                }
            except Exception as error:
                expected = {
                    "accepted": False,
                    "error": {"kind": type(error).__name__, "detail": str(error)},
                }
            finally:
                event.remove(
                    database.engine, "before_cursor_execute", observed_statement
                )
            after = snapshot()
            assert before == after, "decision preparation mutated isolated state"
            item = {
                "name": name,
                "dataset": dataset,
                "session_id": session,
                "project_id": project,
                "expected": expected,
                "expected_validation_order": validation_order,
                "source_statements": statements,
                "before_after_sha256": digest(before),
            }
            (boundaries if boundary else cases).append(item)
        database.dispose()
    # Formatter receives the already-validated seven-field snapshot. These pure
    # vectors preserve large revision integers without pretending they fit SQL.
    format_vectors = []
    for revision in [1, 10**100]:
        snapshot = [
            {
                "id": "entry",
                "revision": revision,
                "kind": "question",
                "text": 'café 🌌\n"quoted"\t\u001c',
                "scope": "project",
                "source_message_id": None,
                "source_session_id": "origin",
            }
        ]
        format_vectors.append(
            {
                "snapshot": snapshot,
                "expected": chat_decisions.decision_instructions(snapshot),
            }
        )
    format_vectors.append(
        {"snapshot": [], "expected": chat_decisions.decision_instructions([])}
    )
    return {
        "source_sha256": {
            name: sha256((ROOT / name).read_bytes()).hexdigest()
            for name in [
                "src/nebula/v3/chat_decisions.py",
                "src/nebula/v3/domain.py",
                "src/nebula/v3/storage.py",
                "src/nebula/v3/database.py",
            ]
        },
        "datasets": datasets,
        "cases": cases,
        "strict_envelope_boundary_observations": boundaries,
        "format_vectors": format_vectors,
        "normalization": "Real isolated SQLAlchemy decisions_for/decision_snapshot/decision_instructions; no app/lifespan/provider/workspace/tool. An observation wrapper records ChatDecision.model_validate input identity and invokes the unchanged bound source method. Every selected row is hydrated before active filtering/budget checks. SQL and parameters are source evidence; optional session None is SQL IS NULL. Existing Rust strict envelope checks remain a separately captured boundary, not Python parity. All raw rows remain unchanged.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(
            collect_decision_preparation(), sort_keys=True, ensure_ascii=False, indent=2
        )
        + "\n"
    )
