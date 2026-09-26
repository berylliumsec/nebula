"""Local cross-encoder relevance for project knowledge.

Engagement knowledge is retrieved by nearest-neighbour search over local
embeddings, and a nearest neighbour exists for every question: without a
relevance judgement, a question about Nebula's own container runner was
answered with the project's harbor runbook attached. Embedding similarity
cannot draw that line on its own (a relevant paraphrase and an unrelated
question both score about 0.33), so a small cross-encoder reads each
candidate chunk together with the question and scores how well it answers it.
Chunks below a calibrated score are left out of the request.

The model is ``mixedbread-ai/mxbai-rerank-xsmall-v1`` (Apache-2.0), in its
quantised ONNX export, pinned to one repository commit and verified by SHA-256
before it is ever loaded. It runs on this host through onnxruntime: project
knowledge never leaves the machine to be scored. Until the model is present
and loaded, retrieval behaves as it did before; preparing it happens in the
background and never holds up a turn.

Calibration (a labelled set of 50 questions over six project documents:
questions sharing words with their answer, paraphrases sharing none,
questions about Nebula itself or general knowledge, and questions that share
vocabulary with a document it cannot answer) put the keep line at a score of
-2.5, with a second route for paraphrases the embedding model already found
(cosine similarity at least 0.40) that score at least -4.0. On that set every
unrelated question attached nothing, every lexical question kept its document,
and 12 of 15 paraphrases kept theirs; today's retrieval attaches documents to
all 21 questions nothing answers.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from .context_retrieval import terms
from .diagnostics import record_caught_exception, record_diagnostic
from .domain import NebulaModel

LOGGER = logging.getLogger(__name__)

RERANKER_MODEL = "mixedbread-ai/mxbai-rerank-xsmall-v1"
# The repository commit the files below were verified at. Resolving files by
# commit, never by branch, keeps the bytes fixed however the repository moves.
RERANKER_REVISION = "b5c6e9da73abc3711f593f705371cdbe9e0fe422"
RERANKER_BASE_URL = "https://huggingface.co"

# A cross-encoder logit at or above this reads the chunk as answering the
# question (see the module docstring for the calibration).
RELEVANCE_THRESHOLD = -2.5
# A chunk the embedding model already placed this close to the question, and
# that the cross-encoder does not firmly reject, is a paraphrase worth keeping.
RESCUE_SIMILARITY = 0.40
RESCUE_THRESHOLD = -4.0
# Scoring costs roughly 0.1 s per chunk on a laptop CPU, so only the best few
# candidates of the existing ranking are read.
MAX_RERANK_CANDIDATES = 8
MAX_SEQUENCE_TOKENS = 512
# The question is capped so the chunk always keeps most of the sequence.
MAX_QUERY_CHARACTERS = 1_000
RETRY_AFTER_SECONDS = 600.0


@dataclass(frozen=True)
class ModelFile:
    """One pinned file of the model repository."""

    path: str
    size: int
    sha256: str


RERANKER_FILES: tuple[ModelFile, ...] = (
    ModelFile(
        "onnx/model_quantized.onnx",
        87_245_802,
        "15ef19a6de90be7d52b627f2c784107bd806e64826450f41fb75fa4f0179ab30",
    ),
    ModelFile(
        "tokenizer.json",
        8_649_139,
        "305674b4d785287feecfb5f73f24aa75e9b57c87c579cfe24fbd207987d4b4c4",
    ),
)


class RerankerError(RuntimeError):
    """The local reranker could not be prepared or could not score."""


class RerankerStatus(NebulaModel):
    """Operator-safe state of the local relevance model."""

    state: Literal["disabled", "required", "downloading", "preparing", "ready", "error"]
    model: str = RERANKER_MODEL
    revision: str = RERANKER_REVISION
    downloaded_bytes: int = 0
    total_bytes: int = 0
    detail: str | None = None


class Scorer(Protocol):
    def __call__(self, pairs: Sequence[tuple[str, str]]) -> list[float]: ...


@dataclass(frozen=True)
class RerankCandidate:
    """A retrieved chunk as the relevance gate reads it.

    ``variant`` is the planned search that found the chunk, when that was not
    the operator's own words; ``similarity`` is the embedding cosine
    similarity, when the chunk came from vector search.
    """

    text: str
    variant: str | None = None
    similarity: float | None = None


def has_content(query: str) -> bool:
    """Whether a question says anything retrieval could match.

    "ok continue" or "thanks" carries no subject, so no document can be about
    it; attaching the nearest ones only spends the request.
    """

    return bool(terms(query))


def relevant_candidates(
    query: str, candidates: Sequence[RerankCandidate], scorer: Scorer
) -> list[tuple[int, float]]:
    """``(index, score)`` of the candidates that answer ``query``, best first.

    Each chunk is read with the operator's question; one that falls short is
    read again with the planned search that found it, and keeps the better
    score. A chunk is kept at ``RELEVANCE_THRESHOLD``, or at
    ``RESCUE_THRESHOLD`` when the embedding model already placed it within
    ``RESCUE_SIMILARITY`` of the question.
    """

    if not candidates:
        return []
    question = query[:MAX_QUERY_CHARACTERS]
    scores = list(scorer([(question, item.text) for item in candidates]))
    if len(scores) != len(candidates):
        raise RerankerError("the reranker returned an unexpected number of scores")
    retry = [
        index
        for index, item in enumerate(candidates)
        if scores[index] < RELEVANCE_THRESHOLD
        and item.variant
        and item.variant.casefold() != query.casefold()
    ]
    if retry:
        second = list(
            scorer(
                [
                    (
                        str(candidates[index].variant)[:MAX_QUERY_CHARACTERS],
                        candidates[index].text,
                    )
                    for index in retry
                ]
            )
        )
        if len(second) != len(retry):
            raise RerankerError("the reranker returned an unexpected number of scores")
        for index, score in zip(retry, second, strict=True):
            scores[index] = max(scores[index], score)
    kept = [
        (index, score)
        for index, score in enumerate(scores)
        if score >= RELEVANCE_THRESHOLD
        or (
            candidates[index].similarity is not None
            and float(candidates[index].similarity or 0.0) >= RESCUE_SIMILARITY
            and score >= RESCUE_THRESHOLD
        )
    ]
    return sorted(kept, key=lambda item: (-item[1], item[0]))


class _OnnxCrossEncoder:
    """The verified ONNX model and tokenizer, loaded once."""

    def __init__(self, model_path: Path, tokenizer_path: Path) -> None:
        onnxruntime = importlib.import_module("onnxruntime")
        tokenizers = importlib.import_module("tokenizers")
        self._numpy = importlib.import_module("numpy")
        tokenizer = tokenizers.Tokenizer.from_file(str(tokenizer_path))
        tokenizer.enable_truncation(
            max_length=MAX_SEQUENCE_TOKENS, strategy="longest_first"
        )
        # Pairs are scored one at a time, unpadded: padding a batch shifted
        # this model's scores by up to 0.2 and cost more than it saved.
        tokenizer.no_padding()
        self._tokenizer = tokenizer
        options = onnxruntime.SessionOptions()
        # A turn's scoring shares the host with Core and its runtimes.
        options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        options.inter_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self._inputs = {item.name for item in self._session.get_inputs()}
        self._lock = threading.Lock()

    def __call__(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        numpy = self._numpy
        scores: list[float] = []
        with self._lock:
            for question, passage in pairs:
                encoded = self._tokenizer.encode(question, passage)
                feeds = {
                    "input_ids": numpy.array([encoded.ids], dtype=numpy.int64),
                    "attention_mask": numpy.array(
                        [encoded.attention_mask], dtype=numpy.int64
                    ),
                    "token_type_ids": numpy.array(
                        [encoded.type_ids], dtype=numpy.int64
                    ),
                }
                output = self._session.run(
                    None,
                    {key: value for key, value in feeds.items() if key in self._inputs},
                )[0]
                scores.append(float(output.reshape(-1)[0]))
        return scores


def _onnx_cross_encoder(model_path: Path, tokenizer_path: Path) -> Scorer:
    return _OnnxCrossEncoder(model_path, tokenizer_path)


class CrossEncoderReranker:
    """Download, verify and run the pinned local relevance model.

    Files live under ``directory/<model>/<revision>/``. Each is fetched over
    HTTPS by commit, streamed to a private temporary file, and moved into
    place only when its size and SHA-256 match the pins; a file already
    present is verified the same way before it is loaded. Nothing is sent to
    the model host but the requests for these fixed files.
    """

    def __init__(
        self,
        directory: Path,
        *,
        enabled: bool = True,
        files: Sequence[ModelFile] = RERANKER_FILES,
        revision: str = RERANKER_REVISION,
        repository: str = RERANKER_MODEL,
        base_url: str = RERANKER_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        loader: Callable[[Path, Path], Scorer] = _onnx_cross_encoder,
    ) -> None:
        self.directory = (
            Path(directory).expanduser().resolve()
            / repository.replace("/", "--")
            / revision
        )
        self.files = tuple(files)
        self.revision = revision
        self.repository = repository
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._loader = loader
        self._lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        self._scorer: Scorer | None = None
        self._thread: threading.Thread | None = None
        self._failed_at: float | None = None
        total = sum(item.size for item in self.files)
        self._status = RerankerStatus(
            state="required" if enabled else "disabled",
            model=repository,
            revision=revision,
            total_bytes=total,
            downloaded_bytes=0,
        )

    @property
    def status(self) -> RerankerStatus:
        with self._lock:
            return self._status.model_copy(deep=True)

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._scorer is not None

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._status = self._status.model_copy(update=changes)

    def start_if_present(self) -> None:
        """Load already-downloaded files at start-up, before any turn needs them."""

        if all(
            self._path(item).is_file() and self._path(item).stat().st_size == item.size
            for item in self.files
        ):
            self.ensure_started()

    def ensure_started(self) -> None:
        """Prepare the model in the background unless it is ready or underway.

        A failed attempt is retried no sooner than ``RETRY_AFTER_SECONDS``
        later, so a host without network access does not retry every turn.
        """

        with self._lock:
            if self._status.state in {"disabled", "ready"}:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            if (
                self._failed_at is not None
                and time.monotonic() - self._failed_at < RETRY_AFTER_SECONDS
            ):
                return
            self._thread = threading.Thread(
                target=self._prepare_in_background,
                name="nebula-knowledge-reranker",
                daemon=True,
            )
            self._thread.start()

    def _prepare_in_background(self) -> None:
        try:
            self.prepare()
        except RerankerError as exc:
            record_diagnostic(
                "warning",
                "knowledge",
                "knowledge.reranker.unavailable",
                "The local knowledge relevance model could not be prepared; "
                "project knowledge is retrieved without it.",
                outcome="fallback",
                stage="knowledge-rerank",
                retryable=True,
                safe_failure_cause=str(exc),
                exception=exc,
            )

    def prepare(self) -> None:
        """Fetch and verify any missing file, then load the model."""

        with self._prepare_lock:
            if self.ready or self.status.state == "disabled":
                return
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.directory.chmod(0o700)
                present = 0
                for item in self.files:
                    if self._verified(self._path(item), item):
                        present += item.size
                self._update(state="downloading", downloaded_bytes=present, detail=None)
                for item in self.files:
                    target = self._path(item)
                    if not self._verified(target, item):
                        self._download(item, target)
                self._update(state="preparing", detail=None)
                model, tokenizer = (self._path(item) for item in self.files)
                scorer = self._loader(model, tokenizer)
            except RerankerError as exc:
                self._fail(str(exc))
                raise
            except Exception as exc:
                self._fail("the local relevance model could not be loaded")
                raise RerankerError(
                    "the local relevance model could not be loaded"
                ) from exc
            with self._lock:
                self._scorer = scorer
                self._failed_at = None
                self._status = self._status.model_copy(
                    update={
                        "state": "ready",
                        "downloaded_bytes": self._status.total_bytes,
                        "detail": None,
                    }
                )

    def _fail(self, detail: str) -> None:
        with self._lock:
            self._failed_at = time.monotonic()
            self._status = self._status.model_copy(
                update={"state": "error", "detail": detail}
            )

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        with self._lock:
            scorer = self._scorer
        if scorer is None:
            raise RerankerError("the local relevance model is not ready")
        return scorer(pairs)

    def _path(self, item: ModelFile) -> Path:
        target = (self.directory / item.path).resolve()
        if self.directory not in target.parents:
            raise RerankerError("a pinned model file resolves outside its directory")
        return target

    @staticmethod
    def _verified(path: Path, item: ModelFile) -> bool:
        try:
            if not path.is_file() or path.stat().st_size != item.size:
                return False
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
        except OSError as exc:
            record_caught_exception(
                "knowledge",
                "knowledge.reranker.verify_failed",
                "A local relevance model file could not be read for verification.",
                exc,
                stage="knowledge-rerank",
            )
            return False
        return digest.hexdigest() == item.sha256

    def _download(self, item: ModelFile, target: Path) -> None:
        url = f"{self.base_url}/{self.repository}/resolve/{self.revision}/{item.path}"
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".download-", delete=False
        )
        partial = Path(handle.name)
        try:
            os.chmod(partial, 0o600)
            digest = hashlib.sha256()
            received = 0
            with (
                handle,
                httpx.Client(
                    transport=self._transport,
                    follow_redirects=True,
                    timeout=httpx.Timeout(30.0, read=60.0),
                ) as client,
                client.stream("GET", url) as response,
            ):
                if response.url.scheme != "https":
                    raise RerankerError("the model download left HTTPS")
                if response.status_code != 200:
                    raise RerankerError(
                        f"the model host answered {response.status_code} for {item.path}"
                    )
                for block in response.iter_bytes(1 << 20):
                    received += len(block)
                    if received > item.size:
                        raise RerankerError(
                            f"{item.path} is larger than the pinned {item.size} bytes"
                        )
                    digest.update(block)
                    handle.write(block)
                    with self._lock:
                        self._status = self._status.model_copy(
                            update={
                                "downloaded_bytes": min(
                                    self._status.total_bytes,
                                    self._status.downloaded_bytes + len(block),
                                )
                            }
                        )
            if received != item.size or digest.hexdigest() != item.sha256:
                raise RerankerError(f"{item.path} does not match its pinned SHA-256")
            os.replace(partial, target)
        except httpx.HTTPError as exc:
            raise RerankerError(f"{item.path} could not be downloaded") from exc
        finally:
            partial.unlink(missing_ok=True)


__all__ = [
    "MAX_RERANK_CANDIDATES",
    "RELEVANCE_THRESHOLD",
    "RERANKER_FILES",
    "RERANKER_MODEL",
    "RERANKER_REVISION",
    "RESCUE_SIMILARITY",
    "RESCUE_THRESHOLD",
    "CrossEncoderReranker",
    "ModelFile",
    "RerankCandidate",
    "RerankerError",
    "RerankerStatus",
    "Scorer",
    "has_content",
    "relevant_candidates",
]
