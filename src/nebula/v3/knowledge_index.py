"""Persistent vector retrieval for engagement knowledge documents.

The Chroma collection is a rebuildable index.  The authoritative document
remains the immutable Nebula artifact referenced by ``KnowledgeSource``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import chromadb
from chromadb.config import Settings

from .domain import KnowledgeSource


COLLECTION_NAME = "nebula-knowledge-v1"
INDEX_BACKEND = "chromadb"
INDEX_VERSION = "nebula.chroma.v1"
UPSERT_BATCH_SIZE = 500


class KnowledgeIndexError(RuntimeError):
    """A persistent knowledge-index operation could not be completed."""


@dataclass(frozen=True)
class IndexedKnowledgeChunk:
    """A vector-search result with the provenance needed for a citation."""

    id: str
    text: str
    source_id: str
    artifact_id: str | None
    page: int | None
    distance: float
    rank: int


class KnowledgeIndex(Protocol):
    """Storage-neutral seam used by ingestion and chat retrieval."""

    @property
    def descriptor(self) -> dict[str, str]: ...

    def upsert_source(
        self, source: KnowledgeSource, chunks: Sequence[dict[str, Any]]
    ) -> None: ...

    def delete_source(self, source_id: str) -> None: ...

    def query(
        self,
        engagement_id: str,
        queries: Sequence[str],
        *,
        limit: int,
    ) -> list[IndexedKnowledgeChunk]: ...


class ChromaKnowledgeIndex:
    """A local persistent Chroma collection using Chroma's embedding function."""

    def __init__(
        self,
        path: str | Path,
        *,
        embedding_function: Any | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.chmod(0o700)
        try:
            self._client = chromadb.PersistentClient(
                path=self.path,
                settings=Settings(anonymized_telemetry=False),
            )
            options: dict[str, Any] = {
                "name": COLLECTION_NAME,
                "metadata": {"hnsw:space": "cosine", "index_version": INDEX_VERSION},
            }
            if embedding_function is not None:
                options["embedding_function"] = embedding_function
            self._collection = self._client.get_or_create_collection(**options)
        except Exception as exc:
            raise KnowledgeIndexError("could not initialize the Chroma index") from exc

    @property
    def descriptor(self) -> dict[str, str]:
        return {
            "index_backend": INDEX_BACKEND,
            "index_version": INDEX_VERSION,
            "collection": COLLECTION_NAME,
        }

    def upsert_source(
        self, source: KnowledgeSource, chunks: Sequence[dict[str, Any]]
    ) -> None:
        """Replace all indexed chunks for one authoritative source."""

        try:
            self._collection.delete(where={"source_id": source.id})
            for start in range(0, len(chunks), UPSERT_BATCH_SIZE):
                batch = chunks[start : start + UPSERT_BATCH_SIZE]
                ids: list[str] = []
                documents: list[str] = []
                metadatas: list[dict[str, Any]] = []
                for raw in batch:
                    chunk_id = str(raw["id"])
                    text = str(raw["text"])
                    metadata: dict[str, str | int | bool] = {
                        "engagement_id": source.engagement_id,
                        "source_id": source.id,
                        "name": source.name,
                    }
                    artifact_id = raw.get("artifact_id") or source.artifact_id
                    if isinstance(artifact_id, str):
                        metadata["artifact_id"] = artifact_id
                    page = raw.get("page")
                    if isinstance(page, int) and page > 0:
                        metadata["page"] = page
                    ids.append(chunk_id)
                    documents.append(text)
                    metadatas.append(metadata)
                self._collection.upsert(
                    ids=ids,
                    documents=documents,
                    metadatas=cast(Any, metadatas),
                )
        except Exception as exc:
            raise KnowledgeIndexError(
                f"could not index knowledge source {source.id}"
            ) from exc

    def delete_source(self, source_id: str) -> None:
        try:
            self._collection.delete(where={"source_id": source_id})
        except Exception as exc:
            raise KnowledgeIndexError(
                f"could not remove knowledge source {source_id} from the index"
            ) from exc

    def query(
        self,
        engagement_id: str,
        queries: Sequence[str],
        *,
        limit: int,
    ) -> list[IndexedKnowledgeChunk]:
        cleaned = [" ".join(query.split()).strip() for query in queries]
        cleaned = [query for query in cleaned if query]
        if not cleaned or limit <= 0:
            return []
        try:
            result = self._collection.query(
                query_texts=cleaned,
                n_results=limit,
                where={"engagement_id": engagement_id},
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            # Chroma reports an empty filtered collection differently across
            # releases. An entirely empty collection is an ordinary no-match.
            try:
                if self._collection.count() == 0:
                    return []
            except Exception:
                pass
            raise KnowledgeIndexError("Chroma knowledge retrieval failed") from exc

        candidates: dict[str, IndexedKnowledgeChunk] = {}
        id_rows = result.get("ids") or []
        document_rows = result.get("documents") or []
        metadata_rows = result.get("metadatas") or []
        distance_rows = result.get("distances") or []
        ordinal = 0
        for query_index, ids in enumerate(id_rows):
            documents = (
                document_rows[query_index] if query_index < len(document_rows) else []
            )
            metadatas = (
                metadata_rows[query_index] if query_index < len(metadata_rows) else []
            )
            distances = (
                distance_rows[query_index] if query_index < len(distance_rows) else []
            )
            for result_index, chunk_id in enumerate(ids):
                document = (
                    documents[result_index] if result_index < len(documents) else None
                )
                metadata = (
                    metadatas[result_index] if result_index < len(metadatas) else None
                )
                distance = (
                    distances[result_index] if result_index < len(distances) else None
                )
                if not isinstance(document, str) or not isinstance(metadata, dict):
                    continue
                source_id = metadata.get("source_id")
                if not isinstance(source_id, str):
                    continue
                artifact_id = metadata.get("artifact_id")
                page = metadata.get("page")
                candidate = IndexedKnowledgeChunk(
                    id=str(chunk_id),
                    text=document,
                    source_id=source_id,
                    artifact_id=artifact_id if isinstance(artifact_id, str) else None,
                    page=page if isinstance(page, int) and page > 0 else None,
                    distance=float(distance)
                    if isinstance(distance, (int, float))
                    else 1.0,
                    rank=ordinal,
                )
                previous = candidates.get(candidate.id)
                if previous is None or candidate.distance < previous.distance:
                    candidates[candidate.id] = candidate
                ordinal += 1
        return sorted(candidates.values(), key=lambda item: (item.distance, item.rank))


__all__ = [
    "ChromaKnowledgeIndex",
    "INDEX_BACKEND",
    "INDEX_VERSION",
    "IndexedKnowledgeChunk",
    "KnowledgeIndex",
    "KnowledgeIndexError",
]
