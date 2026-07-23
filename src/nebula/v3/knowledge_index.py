"""Persistent vector retrieval for engagement knowledge documents.

The Chroma collection is a rebuildable index.  The authoritative document
remains the immutable Nebula artifact referenced by ``KnowledgeSource``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol, cast

import chromadb
from chromadb.api.types import DefaultEmbeddingFunction, Documents, Embeddings
from chromadb.config import Settings
from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

from .diagnostics import record_caught_exception
from .domain import KnowledgeSource, NebulaModel


COLLECTION_NAME = "nebula-knowledge-v1"
INDEX_BACKEND = "chromadb"
INDEX_VERSION = "nebula.chroma.v1"
UPSERT_BATCH_SIZE = 500
DEFAULT_MODEL_BYTES = 83_178_821
_MODEL_FILES = (
    "config.json",
    "model.onnx",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "vocab.txt",
)


class KnowledgeIndexError(RuntimeError):
    """A persistent knowledge-index operation could not be completed."""


class KnowledgeIndexStatus(NebulaModel):
    """Operator-safe state for local embedding-model preparation."""

    backend: str = INDEX_BACKEND
    state: Literal["disabled", "required", "downloading", "preparing", "ready", "error"]
    model: str = ONNXMiniLM_L6_V2.MODEL_NAME
    downloaded_bytes: int = 0
    total_bytes: int = DEFAULT_MODEL_BYTES
    detail: str | None = None


class _ModelStatusTracker:
    def __init__(self, *, ready: bool, model: str = ONNXMiniLM_L6_V2.MODEL_NAME):
        self._lock = Lock()
        self._status = KnowledgeIndexStatus(
            state="ready" if ready else "required",
            model=model,
            downloaded_bytes=DEFAULT_MODEL_BYTES if ready else 0,
        )

    def snapshot(self) -> KnowledgeIndexStatus:
        with self._lock:
            return self._status.model_copy(deep=True)

    def downloading(self, *, downloaded: int, total: int) -> None:
        with self._lock:
            bounded_total = max(1, total or DEFAULT_MODEL_BYTES)
            self._status = self._status.model_copy(
                update={
                    "state": "downloading",
                    "downloaded_bytes": min(max(0, downloaded), bounded_total),
                    "total_bytes": bounded_total,
                    "detail": None,
                }
            )

    def preparing(self) -> None:
        with self._lock:
            self._status = self._status.model_copy(
                update={
                    "state": "preparing",
                    "downloaded_bytes": self._status.total_bytes,
                    "detail": None,
                }
            )

    def ready(self) -> None:
        with self._lock:
            self._status = self._status.model_copy(
                update={
                    "state": "ready",
                    "downloaded_bytes": self._status.total_bytes,
                    "detail": None,
                }
            )

    def failed(self) -> None:
        with self._lock:
            self._status = self._status.model_copy(
                update={
                    "state": "error",
                    "detail": "The local embedding model could not be prepared.",
                }
            )


class _DownloadProgress:
    def __init__(self, tracker: _ModelStatusTracker, *, total: int):
        self.tracker = tracker
        self.total = total or DEFAULT_MODEL_BYTES
        self.downloaded = 0

    def __enter__(self) -> "_DownloadProgress":
        self.tracker.downloading(downloaded=0, total=self.total)
        return self

    def __exit__(self, *_args: object) -> None:
        self.tracker.preparing()

    def update(self, size: int) -> None:
        self.downloaded += size
        self.tracker.downloading(downloaded=self.downloaded, total=self.total)


class _TrackedDefaultEmbeddingFunction(DefaultEmbeddingFunction):
    """Chroma's default local model with observable first-use progress."""

    def __init__(self, tracker: _ModelStatusTracker) -> None:
        self._tracker = tracker
        self._model = ONNXMiniLM_L6_V2()
        self._model.tqdm = self._progress

    def _progress(self, **options: Any) -> _DownloadProgress:
        total = options.get("total")
        return _DownloadProgress(
            self._tracker,
            total=total if isinstance(total, int) else DEFAULT_MODEL_BYTES,
        )

    def __call__(self, input: Documents) -> Embeddings:
        if not _default_model_ready() and _default_model_archive().is_file():
            self._tracker.preparing()
        try:
            embeddings = self._model(input)
        except Exception:
            self._tracker.failed()
            raise
        self._tracker.ready()
        return embeddings


def _default_model_directory() -> Path:
    return Path(ONNXMiniLM_L6_V2.DOWNLOAD_PATH) / ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME


def _default_model_archive() -> Path:
    return Path(ONNXMiniLM_L6_V2.DOWNLOAD_PATH) / ONNXMiniLM_L6_V2.ARCHIVE_FILENAME


def _default_model_ready() -> bool:
    directory = _default_model_directory()
    return all((directory / filename).is_file() for filename in _MODEL_FILES)


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

    @property
    def status(self) -> KnowledgeIndexStatus: ...

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
        if embedding_function is None:
            self._model_status = _ModelStatusTracker(ready=_default_model_ready())
            embedding_function = _TrackedDefaultEmbeddingFunction(self._model_status)
        else:
            name = getattr(embedding_function, "name", lambda: "custom")()
            self._model_status = _ModelStatusTracker(ready=True, model=str(name))
        try:
            self._client = chromadb.PersistentClient(
                path=self.path,
                settings=Settings(anonymized_telemetry=False),
            )
            options: dict[str, Any] = {
                "name": COLLECTION_NAME,
                "metadata": {"hnsw:space": "cosine", "index_version": INDEX_VERSION},
            }
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

    @property
    def status(self) -> KnowledgeIndexStatus:
        return self._model_status.snapshot()

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
            except Exception as count_error:
                record_caught_exception(
                    "knowledge",
                    "knowledge.knowledge_index.caught_failure_001",
                    "The Chroma collection size could not be checked after retrieval failed.",
                    count_error,
                    stage="knowledge",
                )
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
    "KnowledgeIndexStatus",
]
