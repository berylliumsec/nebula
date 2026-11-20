"""Linked memory vocabulary and pure rules, shared by Core and the helper.

Standard library only, so the curation helper process stays light.
"""

from __future__ import annotations

import hashlib
import os
import re

KILL_SWITCH_ENV = "NEBULA_LINKED_MEMORY_DISABLED"
CURATOR_VERSION = "lm-2"

# Stages from the design. Only ``off`` and ``shadow`` exist in this release.
AVAILABLE_STAGES: tuple[str, ...] = ("off", "shadow", "inject")
LATER_STAGES: tuple[str, ...] = ("recall",)

CARD_KINDS = (
    "decision",
    "fact",
    "outcome",
    "constraint",
    "lesson",
    "open_question",
)
# Strongest first (design: Authority and trust).
AUTHORITY_ORDER = (
    "operator_decision",
    "operator_statement",
    "runtime_observation",
    "tool_result",
    "source_inspection",
    "agent_inference",
    "external_record",
)
LINK_TYPES = (
    "supersedes",
    "updates",
    "contradicts",
    "depends_on",
    "continues",
    "followed_by",
    "similar_to",
)
CARD_STATES = (
    "active",
    "superseded",
    "contradicted",
    "retracted",
    "quarantined",
)
RUN_STATES = ("pending", "running", "paused", "done", "capped", "failed", "skipped")
OPEN_RUN_STATES = ("pending", "running", "paused")


def kill_switch_engaged() -> bool:
    """The Core-wide switch overrides every project's stage."""

    return os.environ.get(KILL_SWITCH_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def authority_rank(authority: str) -> int:
    try:
        return AUTHORITY_ORDER.index(authority)
    except ValueError:
        # diagnostic-expected: an unknown authority ranks weakest.
        return len(AUTHORITY_ORDER)


def normalize_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def card_content_hash(kind: str, title: str, summary: str) -> str:
    return hashlib.sha256(
        "\0".join([kind, normalize_text(title), normalize_text(summary)]).encode()
    ).hexdigest()


def project_stream_id(engagement_id: str) -> str:
    """The project's oversight event stream (``butler_oversight.project_stream_id``)."""

    return "butler-project:" + hashlib.sha256(engagement_id.encode("utf-8")).hexdigest()


# Entry cards (turn start) ---------------------------------------------------

ENTRY_CARD_LIMIT = 3
ENTRY_SUMMARY_CHARS = 240
ENTRY_MIN_SCORE = 0.18
ENTRY_TIME_CAP_SECONDS = 0.005

_STOPWORDS = frozenset(
    """a about after again all also am an and any are as at be been before being
    both but by can could did do does doing done for from had has have having he
    her here hers him his how i if in into is it its just me more most my no nor
    not now of off on once only or other our ours out over own same she should so
    some such than that the their theirs them then there these they this those
    through to too under until up very was we were what when where which while who
    whom why will with would you your yours please tell know need want let us use
    using used one two get got make made like just still also each every
    say says said""".split()
)
_ENTRY_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_ENTRY_ID = re.compile(r"(?=[0-9a-f]*[0-9])[0-9a-f]{8,}")
_ENTRY_WORD = re.compile(r"[a-z0-9][a-z0-9_.@/]*[a-z0-9]|[a-z0-9]")


def _entry_stem(word: str) -> str:
    # Plural and verb-form folding keeps "tests" and "testing" with "test".
    for suffix in ("ing", "es", "s", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def entry_words(text: str) -> frozenset[str]:
    """Content words for the in-memory overlap check (no model, no I/O).

    Each word appears once, in one form, so one shared word never counts
    as two.
    """

    lowered = text.casefold()
    # An ID is one word, named by its first eight characters: "goal 1af359e5".
    words = {match[:8] for match in _ENTRY_UUID.findall(lowered)}
    lowered = _ENTRY_UUID.sub(" ", lowered)
    # "rate-limits" must meet "rate limit": hyphens separate words here.
    for word in _ENTRY_WORD.findall(lowered.replace("-", " ")):
        # "R2" and "L11" name things; bare numbers and other short words do not.
        if word in _STOPWORDS or word.isdigit() or len(word) < 2:
            continue
        if len(word) == 2 and word.isalpha():
            continue
        words.add(word if _ENTRY_ID.fullmatch(word) else _entry_stem(word))
    return frozenset(words)


# Words that name Nebula's own objects and routine actions. Nearly every
# message and card in a project uses them, so sharing one says nothing about
# whether a card fits the message.
_PRODUCT_WORDS = entry_words(
    """goal goals chat chats conversation conversations turn turns run runs
    running project projects task tasks agent agents subagent subagents nebula
    core tool tools work working worked status create created start started
    continue continued review operator butler step steps call calls called
    result results model models provider providers session sessions message
    messages complete completed completion progress report reported check
    current new latest now today show list find read write available"""
)
DEFAULT_COMMON_WORDS = _PRODUCT_WORDS
# A word in more than this share of the project's cards is common there.
ENTRY_COMMON_SHARE = 1 / 3
ENTRY_COMMON_MIN_CARDS = 6


def common_entry_words(
    cards: list[dict], project_id: str = ""
) -> frozenset[str]:
    """Words too common in this project's cards to tell them apart."""

    counts: dict[str, int] = {}
    for card in cards:
        for word in card["words"]:
            counts[word] = counts.get(word, 0) + 1
    common = set(_PRODUCT_WORDS) | entry_words(project_id)
    if len(cards) >= ENTRY_COMMON_MIN_CARDS:
        limit = len(cards) * ENTRY_COMMON_SHARE
        common.update(word for word, count in counts.items() if count > limit)
    return frozenset(common)


def select_entry_cards(
    text: str,
    cards: list[dict],
    prefetch: dict[str, float] | None = None,
    *,
    common: frozenset[str] = DEFAULT_COMMON_WORDS,
    limit: int = ENTRY_CARD_LIMIT,
    min_score: float = ENTRY_MIN_SCORE,
    deadline: float | None = None,
    clock=None,
) -> tuple[list[tuple[str, float]], bool]:
    """Rank cached cards for a new message. Returns (picks, completed).

    The score is word overlap with the new message, plus half the semantic
    score the helper gave the card for the chat's previous exchange. Words in
    ``common`` (Nebula's own vocabulary, the project's ID and words most of
    the project's cards use) do not count. A card needs two shared words, or
    one word and a strong prefetch score; a shared ID counts as two. Past the
    deadline the scan stops and the caller injects nothing.
    """

    import time as _time

    now = clock or _time.perf_counter
    words = entry_words(text) - common
    prefetch = prefetch or {}
    scored: list[tuple[str, float]] = []
    if not words:
        return [], True
    for index, card in enumerate(cards):
        if deadline is not None and index % 16 == 0 and now() > deadline:
            return [], False
        card_words = card["words"] - common
        overlap = words & card_words
        # A shared ID names the very thing the card is about: it counts twice.
        shared = len(overlap) + sum(
            1 for word in overlap if _ENTRY_ID.fullmatch(word)
        )
        semantic = prefetch.get(card["id"], 0.0)
        # Every card must share words with the new message itself; the
        # previous exchange's semantic score only lowers the bar to one word.
        if shared < 2 and not (shared >= 1 and semantic >= 0.45):
            continue
        lexical = shared / ((len(words) * len(card_words)) ** 0.5) if shared else 0.0
        score = lexical + 0.5 * semantic
        if score >= min_score:
            scored.append((card["id"], round(score, 4)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit], True
