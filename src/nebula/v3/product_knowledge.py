"""Read-only search over the generated product documentation vector database."""

from __future__ import annotations

import re
import sqlite3
import threading
import base64
import zlib
from contextlib import closing
from functools import lru_cache
from importlib import resources
from pathlib import Path

import numpy as np


_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = {
    "about",
    "after",
    "assistant",
    "can",
    "does",
    "have",
    "help",
    "how",
    "nebula",
    "not",
    "the",
    "this",
    "what",
    "when",
    "where",
    "which",
    "why",
    "with",
    "would",
}
_DENSE_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _database_bytes() -> bytes:
    resource = resources.files("nebula.v3").joinpath("product_knowledge.sqlite3.b85")
    lines = resource.read_text(encoding="ascii").splitlines()
    if not lines or not lines[0].startswith(
        "# Generated SQLite index; zlib plus base85;"
    ):
        raise RuntimeError("bundled product knowledge index has no generation header")
    return zlib.decompress(base64.b85decode("".join(lines[1:])))


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.deserialize(_database_bytes())
    connection.execute("PRAGMA query_only=ON")
    return connection


@lru_cache(maxsize=1)
def _rows() -> tuple[dict[str, object], ...]:
    with closing(_connection()) as connection:
        connection.row_factory = sqlite3.Row
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if metadata.get("schema") != "nebula.product-knowledge/v1":
            raise RuntimeError("bundled product knowledge index is incompatible")
        return tuple(
            dict(row)
            for row in connection.execute(
                "SELECT rowid, id, source_path, heading, text, vector FROM passages"
            )
        )


@lru_cache(maxsize=1)
def _model():
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    return ONNXMiniLM_L6_V2()


def _dense_query(query: str) -> np.ndarray | None:
    # The packaged vectors are always available. The local embedding model is
    # prepared separately; a first-use download must never block help search.
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    directory = (
        Path(ONNXMiniLM_L6_V2.DOWNLOAD_PATH) / ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME
    )
    if not all(
        (directory / name).is_file()
        for name in (
            "config.json",
            "model.onnx",
            "special_tokens_map.json",
            "tokenizer_config.json",
            "tokenizer.json",
            "vocab.txt",
        )
    ):
        return None
    try:
        with _DENSE_LOCK:
            vector = np.asarray(_model()([query])[0], dtype=np.float32)
        return vector / max(float(np.linalg.norm(vector)), 1e-12)
    except Exception:
        # Text search remains usable when the optional local model fails.
        return None


def search_product_knowledge(query: str, *, limit: int = 4) -> list[dict[str, object]]:
    """Return bounded, source-linked passages; no project files are indexed."""

    if not query.strip() or limit < 1:
        return []
    query = query[:1_000]
    rows = _rows()
    terms = list(
        dict.fromkeys(
            word for word in _WORD.findall(query.casefold()) if word not in _STOP
        )
    )[:12]
    lexical: dict[str, int] = {}
    if terms:
        with closing(_connection()) as connection:
            expression = " OR ".join(f'"{word}"' for word in terms)
            for rank, (identifier,) in enumerate(
                connection.execute(
                    "SELECT p.id FROM passages_fts f JOIN passages p ON p.rowid=f.rowid "
                    "WHERE passages_fts MATCH ? ORDER BY bm25(passages_fts) LIMIT 24",
                    (expression,),
                )
            ):
                lexical[identifier] = rank
    dense = _dense_query(query)
    candidates: list[tuple[float, dict[str, object]]] = []
    for row in rows:
        identifier = str(row["id"])
        rank = lexical.get(identifier)
        # A named setting or exact product term should outrank a vaguely
        # similar historical paragraph. Dense similarity breaks close ties and
        # still finds useful passages when the wording differs.
        score = 1 / (2 + rank) if rank is not None else 0.0
        similarity = 0.0
        if dense is not None:
            vector = np.frombuffer(row["vector"], dtype="<f2").astype(np.float32)
            vector /= max(float(np.linalg.norm(vector)), 1e-12)
            similarity = float(np.dot(dense, vector))
            if similarity >= 0.38:
                score += 0.35 * similarity
        if score <= 0:
            continue
        candidates.append((score, row))
    candidates.sort(
        key=lambda item: (
            -item[0],
            str(item[1]["source_path"]),
            str(item[1]["heading"]),
        )
    )
    return [
        {
            "id": row["id"],
            "source": row["source_path"],
            "heading": row["heading"],
            "text": row["text"],
        }
        for _, row in candidates[: min(limit, 6)]
    ]
