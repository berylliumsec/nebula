"""Build Nebula's compact, release-bundled product knowledge vector index.

The source is maintained product documentation, split by headings. No manual
question/answer cards are needed. Run from the repository root before packaging.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sqlite3
import textwrap
import zlib
from pathlib import Path

import numpy as np
from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src/nebula/v3/product_knowledge.sqlite3.b85"
MODEL = ONNXMiniLM_L6_V2.MODEL_NAME
MAX_CHARS = 1_400


def source_paths() -> list[Path]:
    # Release notes, quality receipts and design experiments are deliberately
    # excluded: their historical claims can contradict the current product.
    excluded = {
        "README.md",
        "TEST_SELECTION.md",
        "assistant-research-continuity-contract.md",
        "chat-performance.md",
        "chat-switch-cache-quality.md",
        "chat-switch-performance-quality.md",
    }
    current_docs = [
        path
        for path in sorted((ROOT / "docs").glob("*.md"))
        if path.name not in excluded
    ]
    return [
        ROOT / "README.md",
        *current_docs,
        ROOT / "docs/design/nebula-settings-assistant.md",
        ROOT / "ui/src/settingsCatalog.ts",
    ]


def source_digest(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def passages(paths: list[Path]) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for path in paths:
        source = path.relative_to(ROOT).as_posix()
        if path.name == "settingsCatalog.ts":
            for block in re.findall(
                r'\{\s*id:\s*"settings\.[^"]+".*?\n\s*\}',
                path.read_text(encoding="utf-8"),
                re.S,
            ):
                fields = dict(
                    re.findall(
                        r'\b(id|label|description|category|scope|target):\s*"([^"]+)"',
                        block,
                    )
                )
                if set(fields) != {
                    "id",
                    "label",
                    "description",
                    "category",
                    "scope",
                    "target",
                }:
                    raise RuntimeError("a setting catalog entry could not be reduced")
                keywords = re.search(r"\bkeywords:\s*\[([^]]+)\]", block)
                aliases = (
                    ", ".join(re.findall(r'"([^"]+)"', keywords.group(1)))
                    if keywords
                    else ""
                )
                text = (
                    f"{fields['label']}\n\n{fields['description']}. "
                    f"Category: {fields['category']}. Scope: {fields['scope']}. "
                    f"Open in Settings: /settings#{fields['target']}. Related terms: {aliases}."
                )
                identifier = hashlib.sha256(
                    f"{source}\0{fields['id']}\0{text}".encode()
                ).hexdigest()[:24]
                rows.append((identifier, source, fields["label"], text))
            continue
        heading = path.stem.replace("-", " ")
        paragraphs: list[str] = []
        current = ""

        def flush() -> None:
            nonlocal current
            if current.strip():
                paragraphs.append(current.strip())
            current = ""

        def emit() -> None:
            nonlocal paragraphs
            if not paragraphs:
                return
            body = "\n\n".join(paragraphs).strip()
            paragraphs = []
            for offset in range(0, len(body), MAX_CHARS):
                text = f"{heading}\n\n{body[offset : offset + MAX_CHARS]}".strip()
                if len(text) < 80:
                    continue
                identifier = hashlib.sha256(
                    f"{source}\0{heading}\0{offset}\0{text}".encode()
                ).hexdigest()[:24]
                rows.append((identifier, source, heading, text))

        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^#{1,3}\s+(.+)$", line)
            if match:
                flush()
                emit()
                heading = match.group(1).strip()
            elif not line.strip():
                flush()
            else:
                current = f"{current}\n{line}" if current else line
        flush()
        emit()
    return rows


def build() -> None:
    paths = source_paths()
    rows = passages(paths)
    if not rows:
        raise RuntimeError("no product passages were extracted")
    model = ONNXMiniLM_L6_V2()
    vectors: list[np.ndarray] = []
    for start in range(0, len(rows), 32):
        batch = [row[3] for row in rows[start : start + 32]]
        vectors.extend(np.asarray(item, dtype="<f2") for item in model(batch))
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE passages (
                id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
                heading TEXT NOT NULL, text TEXT NOT NULL, vector BLOB NOT NULL
            );
            CREATE VIRTUAL TABLE passages_fts USING fts5(text, content='passages', content_rowid='rowid');
        """)
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                ("schema", "nebula.product-knowledge/v1"),
                ("source_digest", source_digest(paths)),
                ("embedding_model", MODEL),
                ("passage_count", str(len(rows))),
            ],
        )
        for (identifier, source, heading, text), vector in zip(
            rows, vectors, strict=True
        ):
            cursor = connection.execute(
                "INSERT INTO passages VALUES (?, ?, ?, ?, ?)",
                (identifier, source, heading, text, vector.tobytes()),
            )
            connection.execute(
                "INSERT INTO passages_fts(rowid, text) VALUES (?, ?)",
                (cursor.lastrowid, text),
            )
        connection.commit()
        connection.execute("VACUUM")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        encoded = base64.b85encode(zlib.compress(connection.serialize(), level=9))
    finally:
        connection.close()
    output = OUTPUT.with_name(f"{OUTPUT.name}.tmp")
    output.write_text(
        f"# Generated SQLite index; zlib plus base85; source sha256 {source_digest(paths)}\n"
        + "\n".join(textwrap.wrap(encoded.decode("ascii"), width=100))
        + "\n",
        encoding="ascii",
    )
    output.replace(OUTPUT)
    print(
        f"{OUTPUT}: {len(rows)} passages, {OUTPUT.stat().st_size:,} bytes, source {source_digest(paths)[:12]}"
    )


def database_bytes() -> bytes:
    lines = OUTPUT.read_text(encoding="ascii").splitlines()
    if not lines or not lines[0].startswith(
        "# Generated SQLite index; zlib plus base85;"
    ):
        raise RuntimeError("product knowledge index has no generation header")
    return zlib.decompress(base64.b85decode("".join(lines[1:])))


def verify() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(database_bytes())
        values = dict(connection.execute("SELECT key, value FROM metadata"))
        count = connection.execute("SELECT count(*) FROM passages").fetchone()[0]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
    assert values["schema"] == "nebula.product-knowledge/v1"
    assert values["source_digest"] == source_digest(source_paths())
    assert count == int(values["passage_count"])
    print(f"verified {count} passages, source {values['source_digest'][:12]}")


def ensure() -> None:
    """Create the package resource when absent or stale in a source checkout."""

    try:
        verify()
    except (OSError, ValueError, AssertionError, KeyError, sqlite3.DatabaseError):
        build()
        verify()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    arguments = parser.parse_args()
    verify() if arguments.verify else build()
