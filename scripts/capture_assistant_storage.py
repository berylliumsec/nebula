#!/usr/bin/env python3
"""Capture current SQLite storage contracts using an isolated Python database.

No Core lifespan, provider, helper, or tool runs. The fixture is deterministic and
contains only explicitly constructed records, never operator state.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
from pydantic import ValidationError

from nebula.v3.database import Database
from nebula.v3.domain import ENTITY_MODEL_BY_KIND
from nebula.v3.storage import NebulaStore

from scripts.capture_assistant_records import collect_records

ROOT = Path(__file__).resolve().parents[1]


def collect_storage():
    corpus = collect_records()
    records = [case for case in corpus["cases"] if case["name"].endswith(":canonical")]
    tables = (
        "entities",
        "search_documents",
        "schema_versions",
        "alembic_version",
        "session_projections",
    )
    with tempfile.TemporaryDirectory(prefix="nebula-rust-storage-oracle-") as directory:
        path = Path(directory) / "nebula.db"
        database = Database(path)
        try:
            store = NebulaStore(database)
            for case in records:
                model = ENTITY_MODEL_BY_KIND[case["kind"]]
                store.create(model.model_validate(case["payload"]))
            with sqlite3.connect(path) as raw:
                raw.row_factory = sqlite3.Row
                ddl = [
                    row["sql"]
                    for row in raw.execute(
                        "SELECT sql FROM sqlite_master WHERE tbl_name IN (?,?,?,?,?) AND sql IS NOT NULL ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name",
                        tables,
                    )
                ]
                entities = [
                    dict(row)
                    for row in raw.execute("SELECT * FROM entities ORDER BY kind,id")
                ]
                for row in entities:
                    row["payload"] = json.loads(row["payload"])
                search = [
                    dict(row)
                    for row in raw.execute("SELECT * FROM search_documents ORDER BY id")
                ]
                version = raw.execute(
                    "SELECT max(version) FROM schema_versions"
                ).fetchone()[0]
                alembic = raw.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0]
        finally:
            database.dispose()
    legacy = []
    for case in records:
        model = ENTITY_MODEL_BY_KIND[case["kind"]]
        schema = model.model_json_schema()
        retained = set(schema.get("required", [])) | {
            "id",
            "created_at",
            "updated_at",
            "revision",
        }
        # Clock factories have no deterministic default. Preserve their existing
        # values, instead of calling utc_now while replaying old storage.
        retained |= {
            name
            for name, prop in schema["properties"].items()
            if prop.get("format") == "date-time" and "default" not in prop
        }
        payload = deepcopy(case["payload"])
        # Some optional fields become required by cross-field invariants (for
        # example a user message needs content). Retain those values explicitly.
        for key in sorted(set(payload) - retained):
            value = payload.pop(key)
            try:
                model.model_validate(payload)
            except ValidationError:
                payload[key] = value
        legacy.append(
            {
                "kind": case["kind"],
                "payload": payload,
                "normalized": model.model_validate(payload).model_dump(mode="json"),
            }
        )
    return {
        "format": "nebula.assistant-storage-oracle/v1",
        "baseline_commit": corpus["baseline_commit"],
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in ("database", "storage", "search")
        },
        "schema_version": version,
        "alembic_revision": alembic,
        "schema_sql": ddl,
        "entities": entities,
        "search_documents": search,
        "legacy_records": legacy,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect_storage()
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "entities": len(result["entities"]),
                "legacy_records": len(result["legacy_records"]),
                "schema_version": result["schema_version"],
                "alembic_revision": result["alembic_revision"],
            }
        )
    )


if __name__ == "__main__":
    main()
