"""The local relevance gate for project knowledge (``knowledge_rerank``)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import nebula.v3.chat as chat_module
import nebula.v3.knowledge_rerank as rerank_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.domain import Engagement
from nebula.v3.knowledge_index import ChromaKnowledgeIndex
from nebula.v3.knowledge_rerank import (
    RELEVANCE_THRESHOLD,
    RESCUE_SIMILARITY,
    RESCUE_THRESHOLD,
    CrossEncoderReranker,
    ModelFile,
    RerankCandidate,
    RerankerError,
    has_content,
    relevant_candidates,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider, _profile
from tests.v3.test_knowledge_index import SecurityEmbeddingFunction

ACCESS = b"Operators authenticate with a hardware credential before access."
HARBOR = (
    b"# Harbor service runbook\n\nThe harbor service listens on TCP port 8443 "
    b"behind the internal load balancer.\nThe maintenance window is every Tuesday "
    b"from 02:00 to 04:00 UTC.\n"
)
RUNNER_QUESTION = (
    "Nebula says no rootless container runner is available. What should I check "
    "first? Two sentences at most."
)


class TableScorer:
    """Scores from a table keyed by (question, passage); records each call."""

    def __init__(self, table: dict[tuple[str, str], float], default: float = -9.0):
        self.table = table
        self.default = default
        self.calls: list[list[tuple[str, str]]] = []

    def __call__(self, pairs):
        self.calls.append(list(pairs))
        return [self.table.get(pair, self.default) for pair in pairs]


def test_the_gate_keeps_answers_at_the_calibrated_boundaries():
    candidates = [
        RerankCandidate("at the line"),
        RerankCandidate("just below"),
        RerankCandidate("strong"),
        RerankCandidate("close paraphrase", similarity=RESCUE_SIMILARITY),
        RerankCandidate("not close enough", similarity=RESCUE_SIMILARITY - 0.01),
        RerankCandidate("close but rejected", similarity=0.9),
    ]
    scorer = TableScorer(
        {
            ("q", "at the line"): RELEVANCE_THRESHOLD,
            ("q", "just below"): RELEVANCE_THRESHOLD - 0.01,
            ("q", "strong"): 4.0,
            ("q", "close paraphrase"): RESCUE_THRESHOLD,
            ("q", "not close enough"): RESCUE_THRESHOLD + 1.0,
            ("q", "close but rejected"): RESCUE_THRESHOLD - 0.01,
        }
    )

    kept = relevant_candidates("q", candidates, scorer)

    assert kept == [(2, 4.0), (0, RELEVANCE_THRESHOLD), (3, RESCUE_THRESHOLD)]


def test_a_chunk_below_the_line_is_read_again_with_the_search_that_found_it():
    candidates = [
        RerankCandidate("credential passage", variant="user authentication steps"),
        RerankCandidate("already relevant", variant="another search"),
        RerankCandidate("same words", variant="HOW DO I LOG IN"),
        RerankCandidate("no variant"),
    ]
    scorer = TableScorer(
        {
            ("how do i log in", "credential passage"): -6.0,
            ("user authentication steps", "credential passage"): -1.0,
            ("how do i log in", "already relevant"): 2.0,
        }
    )

    kept = relevant_candidates("how do i log in", candidates, scorer)

    assert kept == [(1, 2.0), (0, -1.0)]
    # One batch for the question, one for the single chunk worth re-reading.
    assert scorer.calls[1] == [("user authentication steps", "credential passage")]


def test_a_scorer_that_miscounts_is_an_error():
    with pytest.raises(RerankerError):
        relevant_candidates("q", [RerankCandidate("a")], lambda pairs: [])


def test_a_question_with_no_subject_retrieves_nothing():
    assert not has_content("ok, continue")
    assert not has_content("thanks, that's all for now")
    assert not has_content("yes, do that")
    assert has_content("CVE-2025-12345?")
    assert has_content("10.20.0.5")
    assert has_content("How does a user log in?")


def _pinned(payloads: dict[str, bytes]) -> tuple[ModelFile, ...]:
    return tuple(
        ModelFile(path, len(data), hashlib.sha256(data).hexdigest())
        for path, data in payloads.items()
    )


PAYLOADS = {"onnx/model_quantized.onnx": b"onnx-bytes" * 100, "tokenizer.json": b"{}"}


def _reranker(tmp_path, handler, *, loaded=None, **options):
    loaded = loaded if loaded is not None else []

    def loader(model: Path, tokenizer: Path):
        loaded.append((model, tokenizer))
        return lambda pairs: [0.0 for _ in pairs]

    return CrossEncoderReranker(
        tmp_path / "models",
        files=_pinned(PAYLOADS),
        transport=httpx.MockTransport(handler),
        loader=loader,
        **options,
    )


def test_pinned_files_are_downloaded_verified_and_loaded(tmp_path):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        path = request.url.path.split(f"/resolve/{rerank_module.RERANKER_REVISION}/")[1]
        return httpx.Response(200, content=PAYLOADS[path])

    loaded: list[tuple[Path, Path]] = []
    reranker = _reranker(tmp_path, handler, loaded=loaded)
    assert reranker.status.state == "required"

    reranker.prepare()

    assert reranker.ready and reranker.status.state == "ready"
    assert reranker.status.downloaded_bytes == reranker.status.total_bytes
    assert requested == [
        "https://huggingface.co/mixedbread-ai/mxbai-rerank-xsmall-v1/resolve/"
        f"{rerank_module.RERANKER_REVISION}/{path}"
        for path in PAYLOADS
    ]
    model, tokenizer = loaded[0]
    assert model.read_bytes() == PAYLOADS["onnx/model_quantized.onnx"]
    assert tokenizer.read_bytes() == PAYLOADS["tokenizer.json"]
    assert model.stat().st_mode & 0o777 == 0o600
    assert reranker.directory.stat().st_mode & 0o777 == 0o700
    assert reranker.score([("q", "p")]) == [0.0]

    # A restarted Core verifies what is on disk and fetches nothing.
    def offline(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a verified model must not be fetched again")

    again = _reranker(tmp_path, offline)
    again.prepare()
    assert again.ready


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"tampered"),
        httpx.Response(200, content=b"x" * 5_000),
        httpx.Response(404),
    ],
    ids=["digest-mismatch", "oversized", "missing"],
)
def test_a_file_that_does_not_match_its_pin_is_never_loaded(tmp_path, response):
    loaded: list[tuple[Path, Path]] = []
    reranker = _reranker(tmp_path, lambda request: response, loaded=loaded)

    with pytest.raises(RerankerError):
        reranker.prepare()

    assert loaded == []
    assert reranker.status.state == "error" and reranker.status.detail
    assert not reranker.ready
    # Neither the rejected file nor its temporary copy is left behind.
    assert [path for path in reranker.directory.rglob("*") if path.is_file()] == []
    with pytest.raises(RerankerError):
        reranker.score([("q", "p")])


def test_a_download_redirected_off_https_is_refused(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://mirror.invalid/x"})
        return httpx.Response(200, content=PAYLOADS["onnx/model_quantized.onnx"])

    reranker = _reranker(tmp_path, handler)

    with pytest.raises(RerankerError, match="left HTTPS"):
        reranker.prepare()


def test_background_preparation_is_disabled_retried_later_and_started_once(
    tmp_path, monkeypatch
):
    calls: list[str] = []

    def failing(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(503)

    disabled = _reranker(tmp_path / "off", failing, enabled=False)
    disabled.ensure_started()
    assert disabled.status.state == "disabled" and calls == []

    reranker = _reranker(tmp_path, failing)
    reranker.ensure_started()
    assert reranker._thread is not None
    reranker._thread.join(timeout=10)
    assert reranker.status.state == "error"
    first = reranker._thread
    # Within the retry window a failed preparation is not attempted again.
    reranker.ensure_started()
    assert reranker._thread is first
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        rerank_module.time,
        "monotonic",
        lambda: real_monotonic() + rerank_module.RETRY_AFTER_SECONDS + 1,
    )
    reranker.ensure_started()
    assert reranker._thread is not first
    reranker._thread.join(timeout=10)


class FakeReranker:
    """A ready or not-yet-ready reranker around a scoring function."""

    def __init__(self, scorer, *, ready: bool = True):
        self.scorer = scorer
        self.ready = ready
        self.started = 0

    def ensure_started(self) -> None:
        self.started += 1

    def score(self, pairs):
        return self.scorer(pairs)

    @property
    def status(self):
        return rerank_module.RerankerStatus(state="ready" if self.ready else "required")


def keyword_scorer(pairs):
    """Stand-in cross-encoder: sign-in questions match the credential text."""

    scores = []
    for question, passage in pairs:
        folded = question.casefold()
        if "authenticate" in passage and ("log in" in folded or "sign" in folded):
            scores.append(1.5)
        elif "harbor" in passage and "harbor" in folded:
            scores.append(3.0)
        else:
            scores.append(-9.0)
    return scores


def _knowledge_project(tmp_path, reranker):
    index = ChromaKnowledgeIndex(
        tmp_path / "knowledge-index",
        embedding_function=SecurityEmbeddingFunction(),
        reranker=reranker,
    )
    store = NebulaStore(tmp_path / "nebula.db")
    store.create(Engagement(id="eng-a", name="A"))
    client = TestClient(
        create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="test-token",
            knowledge_index=index,
        )
    )
    ids: dict[str, str] = {}
    for filename, content in (("access.txt", ACCESS), ("harbor.md", HARBOR)):
        response = client.post(
            "/api/v1/knowledge/ingest",
            headers={"Authorization": "Bearer test-token"},
            json={
                "engagement_id": "eng-a",
                "filename": filename,
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        )
        assert response.status_code == 201
        ids[filename] = response.json()["id"]
    return store, index, client, ids


def test_unrelated_questions_attach_no_knowledge_and_paraphrases_still_do(tmp_path):
    reranker = FakeReranker(keyword_scorer)
    store, index, client, ids = _knowledge_project(tmp_path, reranker)
    chat = ChatService(store, knowledge_index=index)
    # Ingesting knowledge is when the relevance model is fetched.
    assert reranker.started == 2

    unrelated = chat.harness_knowledge_search(
        "eng-a", RUNNER_QUESTION, allow_local_only=True
    )
    paraphrase = chat.harness_knowledge_search(
        "eng-a", "How does a user log in?", allow_local_only=True
    )
    context = chat.harness_knowledge_context("eng-a", "How does a user log in?")

    assert unrelated.matches == []
    assert [match.citation.source_id for match in paraphrase.matches] == [
        ids["access.txt"]
    ]
    assert [citation.source_id for citation in context.citations] == [ids["access.txt"]]
    status = client.get(
        "/api/v1/knowledge/index-status",
        headers={"Authorization": "Bearer test-token"},
    ).json()
    assert status["reranker"]["state"] == "ready"
    assert status["reranker"]["model"] == "mixedbread-ai/mxbai-rerank-xsmall-v1"


def test_until_the_model_is_ready_retrieval_is_unchanged_and_it_is_prepared(
    tmp_path,
):
    reranker = FakeReranker(keyword_scorer, ready=False)
    store, index, _, ids = _knowledge_project(tmp_path, reranker)
    chat = ChatService(store, knowledge_index=index)
    started = reranker.started

    unrelated = chat.harness_knowledge_search(
        "eng-a", RUNNER_QUESTION, allow_local_only=True
    )

    # Today's behaviour: the nearest chunks, whatever the question.
    assert {match.citation.source_id for match in unrelated.matches} <= set(
        ids.values()
    )
    assert unrelated.matches
    assert reranker.started == started + 1


def test_a_scoring_failure_falls_back_to_the_retrieval_ranking(tmp_path):
    def broken(pairs):
        raise RuntimeError("onnx session failed")

    store, index, _, _ = _knowledge_project(tmp_path, FakeReranker(broken))
    chat = ChatService(store, knowledge_index=index)

    result = chat.harness_knowledge_search(
        "eng-a", RUNNER_QUESTION, allow_local_only=True
    )

    assert result.matches


def _prepare(tmp_path, monkeypatch, reranker, content):
    store, index, _, ids = _knowledge_project(tmp_path, reranker)
    profile = store.create(_profile(local=True))
    provider = FakeProvider(profile.id, local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    prepared = ChatService(store, knowledge_index=index).prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id="eng-a",
            include_knowledge=True,
            messages=[{"role": "user", "content": content}],
        )
    )
    planned = [
        request
        for request in provider.requests
        if request.metadata.get("operation") == "agentic_knowledge_retrieval"
    ]
    return prepared, planned, ids


def test_a_chat_turn_carries_only_knowledge_that_answers_it(tmp_path, monkeypatch):
    unrelated, planned, ids = _prepare(
        tmp_path / "a", monkeypatch, FakeReranker(keyword_scorer), RUNNER_QUESTION
    )
    assert planned
    assert not any(
        citation.source_id in ids.values() for citation in unrelated.citations
    )
    assert "harbor service listens" not in json.dumps(
        unrelated.model_request.model_dump(mode="json")
    )

    relevant, _, ids = _prepare(
        tmp_path / "b",
        monkeypatch,
        FakeReranker(keyword_scorer),
        "Which TCP port does the harbor service listen on?",
    )
    assert [
        citation.source_id
        for citation in relevant.citations
        if citation.source_id in ids.values()
    ] == [ids["harbor.md"]]


def test_a_turn_with_no_subject_plans_and_attaches_nothing(tmp_path, monkeypatch):
    prepared, planned, ids = _prepare(
        tmp_path, monkeypatch, FakeReranker(keyword_scorer, ready=False), "ok, thanks"
    )

    assert planned == []
    assert not any(
        citation.source_id in ids.values() for citation in prepared.citations
    )


CALIBRATION = (
    Path(__file__).parent / "fixtures" / "knowledge_relevance_calibration.json"
)


@pytest.mark.skipif(
    not os.environ.get("NEBULA_TEST_RERANKER_DIR"),
    reason="needs the pinned model files in NEBULA_TEST_RERANKER_DIR",
)
def test_the_pinned_model_meets_its_calibration():  # pragma: no cover - local only
    """Re-check the thresholds against the real model and labelled questions."""

    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
        ONNXMiniLM_L6_V2,
    )

    from nebula.v3.knowledge import _split_text

    reranker = CrossEncoderReranker(
        Path(os.environ["NEBULA_TEST_RERANKER_DIR"]),
        transport=httpx.MockTransport(lambda request: httpx.Response(403)),
    )
    reranker.prepare()
    calibration: dict[str, Any] = json.loads(CALIBRATION.read_text(encoding="utf-8"))
    chunks = [
        (name, text)
        for name, body in calibration["documents"].items()
        for text in _split_text(body)
    ]
    embed = ONNXMiniLM_L6_V2()
    chunk_vectors = embed([text for _, text in chunks])

    def cosine(left, right) -> float:
        return float(
            sum(a * b for a, b in zip(left, right))
            / (sum(a * a for a in left) ** 0.5 * sum(b * b for b in right) ** 0.5)
        )

    outcome: dict[str, list[bool]] = {}
    for item in calibration["questions"]:
        question, relevant, kind = item["question"], set(item["relevant"]), item["kind"]
        variants = [question, *item["variants"]]
        vectors = embed(variants)
        candidates = []
        for (_, text), vector in zip(chunks, chunk_vectors):
            similarities = [cosine(query, vector) for query in vectors]
            best = max(range(len(variants)), key=similarities.__getitem__)
            candidates.append(
                RerankCandidate(
                    text=text,
                    variant=variants[best] if best else None,
                    similarity=max(similarities),
                )
            )
        kept = (
            relevant_candidates(question, candidates, reranker.score)
            if has_content(question)
            else []
        )
        names = {chunks[index][0] for index, _ in kept}
        outcome.setdefault(kind, []).append(
            bool(names & relevant) if relevant else not names
        )
    print({kind: f"{sum(values)}/{len(values)}" for kind, values in outcome.items()})
    assert all(outcome["unrelated"])
    assert all(outcome["lexical"])
    assert sum(outcome["paraphrase"]) >= 12
    assert sum(outcome["hard_negative"]) >= 4
