"""Rank passages of archived conversation for the model's current turn.

When a conversation outgrows its model window, older messages leave the
request for a derived working memory. The originals stay canonical, and this
module finds the passages of them that matter now: for the excerpts attached to
each compacted request, and for the ``conversation.search`` tool a model calls
on demand.

Everything here is a pure function of canonical text. Nothing reads storage or
calls a provider; the optional dense re-rank receives an embedding callable
that the caller supplies only when a local model is already loaded.

Design, in the order a query meets it:

* **Chunks, not messages.** A message is split into paragraph-aligned passages
  of at most ``CHUNK_TOKENS`` estimated tokens, so the one relevant paragraph of
  a long message can be retrieved within a small excerpt budget.
* **BM25 over chunks** (Robertson/Sparck Jones IDF, length normalisation) with
  the strong-identifier boost the previous ranker had, now weighted by the
  identifier's own rarity so a common port number cannot dominate.
* **A tail-aware query.** "yes, do that" carries no content of its own; the
  operator's previous messages and the assistant's last reply in the kept tail
  join the query at lower weights that fade as the current message says more,
  and a passage found only through the tail must match rare words.
* **Optional dense fusion.** When a local embedding model is ready, cosine
  similarity over a bounded candidate set is fused with the lexical ranking by
  reciprocal rank fusion. Any failure there leaves the lexical ranking.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from array import array
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from operator import mul
from typing import Any

from .context import _SECURITY_IDENTIFIER, estimate_tokens
from .diagnostics import record_caught_exception

# A passage small enough that several fit a modest excerpt budget, large
# enough to carry a paragraph with its surrounding sentence or two, and within
# what a MiniLM-class embedding model reads (256 word pieces) so the dense
# ranking sees all of it.
CHUNK_TOKENS = 320
MAX_EXCERPTS = 8
BM25_K1 = 1.2
BM25_B = 0.75
# An exact identifier in the query (CVE, path, address, hash, record id) is
# worth about three saturated term matches of the same rarity.
IDENTIFIER_WEIGHT = 3.0
# Passages scoring below this share of the best lexical score are noise that
# happens to share a word, not the thing the turn is about.
MIN_RELATIVE_SCORE = 0.2
# A passage found only through the recent tail (none of the current message's
# words) must match words the archive does not share everywhere: under IDF a
# word most passages contain scores near zero, while one rare word at a tail
# weight clears this several times over. Otherwise a contentful question
# whose words the archive lacks would retrieve whatever resembles the latest
# exchange.
MIN_TAIL_SCORE = 0.3
RRF_K = 60
# Below this cosine a MiniLM-class model is matching topic, not content.
DENSE_MIN_SIMILARITY = 0.35
DENSE_CANDIDATES = 24
# Embedding runs on the request path at roughly 30-50 ms per passage on a
# laptop CPU whatever its length, so a turn embeds at most this many new
# passages; vectors are cached, and the archive fills in over later turns.
DENSE_MAX_NEW = 16
QUERY_PART_CHARS = 4_000
CURRENT_WEIGHT = 1.0
# Most recent first. The operator's own recent words say what "that" is; the
# assistant's last reply, which often proposed it, counts for less.
TAIL_OPERATOR_WEIGHTS = (0.5, 0.35)
TAIL_ASSISTANT_WEIGHT = 0.25
# The tail speaks for a request only as far as the request does not speak for
# itself: its weights fade with each content term of the current message and
# vanish at this many, so a precise question is not diluted by whatever the
# last exchange was about. Only its opening counts: a short follow-up whole, a
# pasted log or listing by what it says it is.
TAIL_FADE_TERMS = 4
TAIL_PART_CHARS = 1_000

_TOKEN = re.compile(r"[^\W_][\w.:/@+-]*")
_TOKEN_PARTS = re.compile(r"[._:/@+-]+")
_TOKEN_EDGE = "._:/@+-"
_IDENTIFIER_EDGE = ".,;:!?)]}>'\""
# Function words and conversational filler. Common content words are left to
# IDF; these are dropped so a query made only of them retrieves nothing rather
# than whatever happens to repeat "that".
_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because
    been before being below between both but by can could did do does doing done
    down during each few for from further had has have having he her here hers
    herself him himself his how if in into is it its itself just let me more most
    my myself no nor not now of off on once only or other our ours ourselves out
    over own same she should so some such than that the their theirs them
    themselves then there these they this those through to too under until up
    very was we were what when where which while who whom why will with would you
    your yours yourself yourselves i i'm it's that's we'll you're don't can't
    yes yeah yep ok okay sure please thanks thank go ahead right great good fine
    cool alright proceed continue
    """.split()
)
_SEPARATORS = (
    re.compile(r"\n[ \t]*\n\s*"),
    re.compile(r"\n"),
    re.compile(r"(?<=[.!?;])\s+"),
    re.compile(r"\s+"),
)
_CHUNK_BYTES = CHUNK_TOKENS * 3


@dataclass(frozen=True)
class ArchivedMessage:
    """One canonical message, as the text it stood for in model context."""

    id: str
    sequence: int
    role: str
    text: str


@dataclass(frozen=True)
class TranscriptChunk:
    """A passage of one archived message; ``start``/``end`` index its text."""

    message_id: str
    sequence: int
    role: str
    index: int
    count: int
    start: int
    end: int
    text: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text, message_count=1)

    def payload(self) -> dict[str, Any]:
        """The model-facing form, identifying the passage within its message."""

        item: dict[str, Any] = {
            "message_id": self.message_id,
            "sequence": self.sequence,
            "role": self.role,
        }
        if self.count > 1:
            item["part"] = f"{self.index + 1}/{self.count}"
        item["content"] = self.text
        return item


@dataclass(frozen=True)
class QueryPart:
    text: str
    weight: float
    # Context for a follow-up (the recent tail), not the request itself.
    tail: bool = False


@dataclass(frozen=True)
class RankedChunk:
    chunk: TranscriptChunk
    score: float
    lexical: float
    dense: float | None = None


Encoder = Callable[[list[str]], Sequence[Sequence[float]]]


class VectorCache:
    """Bounded, thread-safe embeddings of passage text for one model.

    Archived messages do not change, so a passage embedded once serves every
    later turn of the conversation; entries are keyed by model and text.
    """

    def __init__(self, capacity: int = 4_096) -> None:
        self.capacity = capacity
        self._items: OrderedDict[str, tuple[array[float], float]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(model: str, text: str) -> str:
        return hashlib.sha256(f"{model}\0{text}".encode("utf-8")).hexdigest()

    def get(self, key: str) -> tuple[array[float], float] | None:
        with self._lock:
            found = self._items.get(key)
            if found is not None:
                self._items.move_to_end(key)
            return found

    def put(self, key: str, vector: Sequence[float]) -> tuple[array[float], float]:
        stored = array("f", (float(value) for value in vector))
        entry = (stored, math.sqrt(sum(value * value for value in stored)))
        with self._lock:
            self._items[key] = entry
            self._items.move_to_end(key)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)
        return entry

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@dataclass(frozen=True)
class DenseEncoder:
    """A ready local embedding model, its identity, and the vectors it made."""

    encode: Encoder
    model: str
    cache: VectorCache
    max_new: int = DENSE_MAX_NEW


def terms(text: str) -> list[str]:
    """Casefolded index terms: whole tokens plus the parts of compound ones.

    ``payments-gw``, ``/etc/nginx/nginx.conf`` and ``api.example.com`` stay
    whole for exact matches and also yield their parts, so "payments" finds the
    first and "nginx" the second.
    """

    found: list[str] = []
    for match in _TOKEN.finditer(text):
        token = match.group().casefold().strip(_TOKEN_EDGE)
        if len(token) < 2:
            continue
        parts = [part for part in _TOKEN_PARTS.split(token) if len(part) >= 2]
        if token not in _STOPWORDS:
            found.append(token)
        if len(parts) > 1 or (parts and parts[0] != token):
            found.extend(part for part in parts if part not in _STOPWORDS)
    return found


def identifiers(text: str) -> set[str]:
    return {
        cleaned
        for item in _SECURITY_IDENTIFIER.findall(text)
        if len(cleaned := item.casefold().rstrip(_IDENTIFIER_EDGE)) >= 2
    }


def _byte_size(text: str, start: int, end: int) -> int:
    return len(text[start:end].encode("utf-8"))


def _merge(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Join adjacent spans while the joined passage still fits one chunk."""

    merged: list[tuple[int, int]] = []
    merged_bytes = 0
    for start, end in spans:
        size = _byte_size(text, start, end)
        if merged and merged_bytes + size <= _CHUNK_BYTES:
            merged[-1] = (merged[-1][0], end)
            merged_bytes += size
            continue
        merged.append((start, end))
        merged_bytes = size
    return merged


def _hard_split(text: str, start: int, end: int) -> list[tuple[int, int]]:
    if text[start:end].isascii():
        # One byte per character: a pasted blob need not be walked by hand.
        return [
            (position, min(end, position + _CHUNK_BYTES))
            for position in range(start, end, _CHUNK_BYTES)
        ]
    spans: list[tuple[int, int]] = []
    cursor = start
    size = 0
    for position in range(start, end):
        width = len(text[position].encode("utf-8"))
        if size + width > _CHUNK_BYTES and position > cursor:
            spans.append((cursor, position))
            cursor, size = position, 0
        size += width
    if cursor < end:
        spans.append((cursor, end))
    return spans


def _split(text: str, start: int, end: int, level: int) -> list[tuple[int, int]]:
    if _byte_size(text, start, end) <= _CHUNK_BYTES:
        return [(start, end)]
    if level >= len(_SEPARATORS):
        return _hard_split(text, start, end)
    pieces: list[tuple[int, int]] = []
    cursor = start
    for match in _SEPARATORS[level].finditer(text, start, end):
        if match.end() > cursor and match.start() > cursor:
            # A separator stays with the passage it ends.
            pieces.append((cursor, match.end()))
            cursor = match.end()
    if cursor < end:
        pieces.append((cursor, end))
    if len(pieces) <= 1:
        return _split(text, start, end, level + 1)
    spans: list[tuple[int, int]] = []
    for piece_start, piece_end in pieces:
        spans.extend(_split(text, piece_start, piece_end, level + 1))
    return _merge(text, spans)


def chunk_message(message: ArchivedMessage) -> list[TranscriptChunk]:
    """Split one message into paragraph-aligned passages covering its text.

    Paragraphs are kept whole and packed together while they fit; a longer
    paragraph splits at lines, then sentences, then words, and a single
    unbroken run at the byte limit. Leading and trailing whitespace is trimmed
    from each passage, so ``start``/``end`` point at its visible text.
    """

    spans: list[tuple[int, int]] = []
    for start, end in _split(message.text, 0, len(message.text), 0):
        segment = message.text[start:end]
        stripped = segment.strip()
        if not stripped:
            continue
        offset = start + (len(segment) - len(segment.lstrip()))
        spans.append((offset, offset + len(stripped)))
    return [
        TranscriptChunk(
            message_id=message.id,
            sequence=message.sequence,
            role=message.role,
            index=index,
            count=len(spans),
            start=start,
            end=end,
            text=message.text[start:end],
        )
        for index, (start, end) in enumerate(spans)
    ]


def chunk_messages(messages: Iterable[ArchivedMessage]) -> list[TranscriptChunk]:
    return [chunk for message in messages for chunk in chunk_message(message)]


def turn_query(
    current: str, earlier: Sequence[tuple[str, str]] = ()
) -> tuple[QueryPart, ...]:
    """The weighted query for a turn: its message, then the recent tail.

    ``earlier`` is the kept tail before the current message as ``(role,
    text)`` pairs, oldest first. The operator's two latest messages and the
    assistant's latest reply join at lower weights, so a follow-up such as
    "yes, do that" still retrieves what "that" was about; they fade out as the
    current message carries content of its own.
    """

    parts = [QueryPart(current[:QUERY_PART_CHARS], CURRENT_WEIGHT)]
    fade = max(0.0, 1.0 - len(set(terms(current))) / TAIL_FADE_TERMS)
    if fade > 0:
        operator = [text for role, text in reversed(earlier) if role == "user"]
        for weight, text in zip(TAIL_OPERATOR_WEIGHTS, operator, strict=False):
            parts.append(QueryPart(text[:TAIL_PART_CHARS], weight * fade, tail=True))
        assistant = next(
            (text for role, text in reversed(earlier) if role == "assistant"), None
        )
        if assistant:
            parts.append(
                QueryPart(
                    assistant[:TAIL_PART_CHARS],
                    TAIL_ASSISTANT_WEIGHT * fade,
                    tail=True,
                )
            )
    return tuple(part for part in parts if part.text.strip())


def _query_weights(
    query: Sequence[QueryPart],
) -> tuple[dict[str, float], dict[str, float]]:
    """The highest weight each term and identifier carries in any query part."""

    term_weights: dict[str, float] = {}
    identifier_weights: dict[str, float] = {}
    for part in query:
        for term in set(terms(part.text)):
            term_weights[term] = max(term_weights.get(term, 0.0), part.weight)
        for identifier in identifiers(part.text):
            identifier_weights[identifier] = max(
                identifier_weights.get(identifier, 0.0), part.weight
            )
    return term_weights, identifier_weights


def _idf(total: int, containing: int) -> float:
    return math.log(1.0 + (total - containing + 0.5) / (containing + 0.5))


class _LexicalIndex:
    """BM25 statistics over one set of chunks, scored for any query."""

    def __init__(self, chunks: Sequence[TranscriptChunk]) -> None:
        self.chunks = chunks
        self.frequencies = [Counter(terms(chunk.text)) for chunk in chunks]
        self.lengths = [sum(counts.values()) for counts in self.frequencies]
        self.average = (sum(self.lengths) / len(chunks) if chunks else 0.0) or 1.0
        self._folded: list[str] | None = None

    def scores(self, query: Sequence[QueryPart]) -> list[float]:
        total = len(self.chunks)
        scores = [0.0] * total
        term_weights, identifier_weights = _query_weights(query)
        if not total:
            return scores
        postings: dict[str, list[tuple[int, int]]] = {term: [] for term in term_weights}
        for index, counts in enumerate(self.frequencies):
            for term in term_weights.keys() & counts.keys():
                postings[term].append((index, counts[term]))
        for term, weight in term_weights.items():
            found = postings[term]
            if not found:
                continue
            idf = _idf(total, len(found))
            for index, frequency in found:
                normaliser = BM25_K1 * (
                    1 - BM25_B + BM25_B * self.lengths[index] / self.average
                )
                scores[index] += (
                    weight * idf * frequency * (BM25_K1 + 1) / (frequency + normaliser)
                )
        if identifier_weights:
            if self._folded is None:
                self._folded = [chunk.text.casefold() for chunk in self.chunks]
            for identifier, weight in identifier_weights.items():
                matched = [
                    index
                    for index, text in enumerate(self._folded)
                    if identifier in text
                ]
                if not matched:
                    continue
                boost = weight * IDENTIFIER_WEIGHT * _idf(total, len(matched))
                for index in matched:
                    scores[index] += boost
        return scores


def lexical_scores(
    chunks: Sequence[TranscriptChunk], query: Sequence[QueryPart]
) -> list[float]:
    """BM25 plus a rarity-weighted exact-identifier boost, one score per chunk."""

    return _LexicalIndex(chunks).scores(query)


def _cosine(
    left: tuple[array[float], float], right: tuple[array[float], float]
) -> float:
    (left_vector, left_norm), (right_vector, right_norm) = left, right
    if not left_norm or not right_norm or len(left_vector) != len(right_vector):
        return 0.0
    return sum(map(mul, left_vector, right_vector)) / (left_norm * right_norm)


def _dense_similarities(
    chunks: Sequence[TranscriptChunk],
    query: Sequence[QueryPart],
    lexical_order: Sequence[int],
    dense: DenseEncoder,
) -> dict[int, float]:
    """Cosine similarity for a bounded candidate set of chunks.

    Candidates are the best lexical matches plus every passage already
    embedded; at most ``max_new`` passages are embedded per call, best lexical
    matches first, then the newest, so later turns see more of the archive
    without any one turn paying for all of it. Query parts are cached too: a
    turn's message is the next turn's tail.
    """

    # A sentence embedding already carries a contentful request's meaning,
    # and averaging in the tail would pull it toward the last exchange; the
    # tail stands in only for a request with no content of its own.
    content = [part for part in query if terms(part.text)]
    content_parts = [part for part in content if not part.tail] or content
    if not content_parts:
        return {}
    keys = [VectorCache.key(dense.model, chunk.text) for chunk in chunks]
    part_keys = [VectorCache.key(dense.model, part.text) for part in content_parts]
    vectors: dict[int, tuple[array[float], float]] = {}
    for index, key in enumerate(keys):
        cached = dense.cache.get(key)
        if cached is not None:
            vectors[index] = cached
    part_vectors = [dense.cache.get(key) for key in part_keys]
    wanted = list(lexical_order[:DENSE_CANDIDATES])
    wanted.extend(
        index
        for index in sorted(
            range(len(chunks)),
            key=lambda item: (-chunks[item].sequence, chunks[item].index),
        )
        if index not in vectors
    )
    missing = list(dict.fromkeys(index for index in wanted if index not in vectors))[
        : dense.max_new
    ]
    missing_parts = [
        position for position, found in enumerate(part_vectors) if found is None
    ]
    texts = [content_parts[position].text for position in missing_parts]
    texts.extend(chunks[index].text for index in missing)
    encoded = dense.encode(texts) if texts else []
    if len(encoded) != len(texts):
        raise ValueError("the embedding model returned an unexpected vector count")
    for position, vector in zip(missing_parts, encoded, strict=False):
        part_vectors[position] = dense.cache.put(part_keys[position], vector)
    for index, vector in zip(missing, encoded[len(missing_parts) :], strict=True):
        vectors[index] = dense.cache.put(keys[index], vector)
    # One query vector: the parts' unit vectors summed by weight.
    combined: list[float] = []
    for part, entry in zip(content_parts, part_vectors, strict=True):
        assert entry is not None
        vector, norm = entry
        if not combined:
            combined = [0.0] * len(vector)
        if not norm or len(vector) != len(combined):
            continue
        for position, value in enumerate(vector):
            combined[position] += part.weight * value / norm
    query_vector = array("f", combined)
    query_entry = (
        query_vector,
        math.sqrt(sum(value * value for value in query_vector)),
    )
    return {index: _cosine(query_entry, vector) for index, vector in vectors.items()}


def rank_chunks(
    chunks: Sequence[TranscriptChunk],
    query: Sequence[QueryPart],
    *,
    dense: DenseEncoder | None = None,
) -> list[RankedChunk]:
    """Relevant chunks, best first; ties go to the newer passage.

    Without ``dense`` the order is lexical. With it, the lexical and dense
    orders are fused by reciprocal rank, so a passage that says the same thing
    in other words can join, and one both agree on rises. A dense failure is
    recorded and the lexical order stands.
    """

    bm25 = _LexicalIndex(chunks)
    lexical = bm25.scores(query)
    request = [part for part in query if not part.tail]
    direct = bm25.scores(request) if len(request) < len(query) else lexical
    best = max(lexical, default=0.0)
    lexical_order = sorted(
        (
            position
            for position, score in enumerate(lexical)
            if score > 0
            and score >= best * MIN_RELATIVE_SCORE
            and (direct[position] > 0 or score >= MIN_TAIL_SCORE)
        ),
        key=lambda index: (
            -lexical[index],
            -chunks[index].sequence,
            chunks[index].index,
        ),
    )
    similarities: dict[int, float] = {}
    if dense is not None and chunks:
        try:
            similarities = _dense_similarities(chunks, query, lexical_order, dense)
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.context_retrieval.dense_fallback",
                "Semantic ranking of archived conversation failed; keyword ranking was used.",
                exc,
                stage="context-retrieval",
            )
            similarities = {}
    if not similarities:
        return [
            RankedChunk(chunks[index], lexical[index], lexical[index])
            for index in lexical_order
        ]
    dense_order = sorted(
        (
            index
            for index, similarity in similarities.items()
            if similarity >= DENSE_MIN_SIMILARITY
        ),
        key=lambda index: (
            -similarities[index],
            -chunks[index].sequence,
            chunks[index].index,
        ),
    )
    fused: dict[int, float] = {}
    for order in (lexical_order, dense_order):
        for rank, index in enumerate(order, start=1):
            fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank)
    return [
        RankedChunk(
            chunks[index], fused[index], lexical[index], similarities.get(index)
        )
        for index in sorted(
            fused,
            key=lambda index: (
                -fused[index],
                -lexical[index],
                -chunks[index].sequence,
                chunks[index].index,
            ),
        )
    ]


def select_excerpts(
    ranked: Iterable[RankedChunk],
    *,
    token_budget: int,
    limit: int = MAX_EXCERPTS,
) -> list[TranscriptChunk]:
    """The best passages that fit, in transcript order, each text once."""

    chosen: list[TranscriptChunk] = []
    seen: set[str] = set()
    used = 0
    for item in ranked:
        if len(chosen) >= limit:
            break
        digest = hashlib.sha256(item.chunk.text.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        size = item.chunk.tokens
        if used + size > token_budget:
            continue
        chosen.append(item.chunk)
        seen.add(digest)
        used += size
    return sorted(chosen, key=lambda chunk: (chunk.sequence, chunk.index))


def archived_messages(
    messages: Iterable[Any], text_of: Mapping[str, str] | Callable[[Any], str]
) -> list[ArchivedMessage]:
    """Adapt stored messages (``id``/``sequence``/``role``) for chunking."""

    def text(message: Any) -> str:
        if callable(text_of):
            return text_of(message)
        return text_of[message.id]

    return [
        ArchivedMessage(
            id=message.id,
            sequence=message.sequence,
            role=getattr(message.role, "value", str(message.role)),
            text=text(message),
        )
        for message in messages
    ]


__all__ = [
    "CHUNK_TOKENS",
    "MAX_EXCERPTS",
    "ArchivedMessage",
    "DenseEncoder",
    "QueryPart",
    "RankedChunk",
    "TranscriptChunk",
    "VectorCache",
    "archived_messages",
    "chunk_message",
    "chunk_messages",
    "identifiers",
    "lexical_scores",
    "rank_chunks",
    "select_excerpts",
    "terms",
    "turn_query",
]
