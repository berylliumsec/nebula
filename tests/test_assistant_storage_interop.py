"""Actual Python → Rust → Python → Rust storage handoff on an isolated database.

Build timeout: 600 s. Each Rust process: 120 s. No Core lifespan, tools, model
providers, user database or automatic schema repair after the first bootstrap.
"""

import json
import os
from pathlib import Path
import sqlite3
import subprocess

from nebula.v3.database import Database
from nebula.v3.domain import (
    ENTITY_MODEL_BY_KIND,
    ChatBookmark,
    ChatMessage,
    ChatSession,
)
from nebula.v3.storage import NebulaStore

ROOT = Path(__file__).resolve().parents[1]


def test_python_rust_python_storage_roundtrip(tmp_path):
    fixture = json.loads(
        (ROOT / "assistant-rs/compatibility/python-storage.json").read_text()
    )
    path = tmp_path / "nebula.db"
    database = Database(path)
    try:
        store = NebulaStore(database)
        for row in fixture["entities"]:
            store.create(
                ENTITY_MODEL_BY_KIND[row["kind"]].model_validate(row["payload"])
            )
        with sqlite3.connect(path) as raw:
            raw.execute(
                "CREATE TABLE assistant_interop_fixture (identity TEXT NOT NULL)"
            )
            raw.execute(
                "INSERT INTO assistant_interop_fixture VALUES ('python-rust-python-test/v1')"
            )
            # Check an unrelated row byte-for-byte, without invoking its service.
            raw.execute(
                "INSERT INTO entities (id,kind,revision,payload,created_at,updated_at) VALUES ('unrelated','fixture_other_area',1,'{\"untouched\":true}','2026-09-23 12:00:00.000000','2026-09-23 12:00:00.000000')"
            )
            unrelated = raw.execute(
                "SELECT * FROM entities WHERE id='unrelated'"
            ).fetchone()
    finally:
        database.dispose()
    target = (
        tmp_path / "target" if os.environ.get("CI") else ROOT / "assistant-rs/target"
    )
    subprocess.run(
        [
            "cargo",
            "+1.94.0",
            "build",
            "--locked",
            "--manifest-path",
            "assistant-rs/Cargo.toml",
            "-p",
            "nebula-assistant-storage",
            "--example",
            "compatibility_roundtrip",
            "--target-dir",
            str(target),
        ],
        cwd=ROOT,
        check=True,
        timeout=600,
    )
    executable = target / "debug/examples/compatibility_roundtrip"
    subprocess.run([str(executable), str(path), "mutate"], check=True, timeout=120)
    database = Database(path, bootstrap=False)
    try:
        store = NebulaStore(database)
        session = store.get(ChatSession, "fixture-chat_sessions")
        assert session.title == "From Rust 🦀"
        assert session.revision == 3
        assert session.metadata == {
            "opaque": "  retain  ",
            "large": 18446744073709551616,
        }
        assert (
            store.get(ChatBookmark, "rust-bookmark").message_id
            == "fixture-chat_messages"
        )
        assert [m.id for m in store.list_session_entities(ChatMessage, session.id)] == [
            "fixture-chat_messages"
        ]
        message = store.get(ChatMessage, "fixture-chat_messages")
        assert message.metadata["retracted_at"] == "2026-09-23T12:02:00Z"
        with sqlite3.connect(path) as raw:
            assert (
                raw.execute("SELECT * FROM entities WHERE id='unrelated'").fetchone()
                == unrelated
            )
            assert raw.execute(
                "SELECT label,revision FROM search_documents WHERE id=?", (session.id,)
            ).fetchone() == ("From Rust 🦀", 3)
            assert raw.execute(
                "SELECT max(version) FROM schema_versions"
            ).fetchone() == (5,)
            assert raw.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == ("0016_chat_session_lookup",)
        store.update(
            ChatSession, session.id, {"title": "Back in Python"}, expected_revision=3
        )
        store.delete(ChatBookmark, "rust-bookmark", expected_revision=2)
    finally:
        database.dispose()
    subprocess.run([str(executable), str(path), "verify"], check=True, timeout=120)
