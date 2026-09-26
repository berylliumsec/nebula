"""Deterministic retrieval over the bundled Nebula 3 operator-help corpus."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

CORPUS_ID = "nebula.operator-help/v1"
_ARTICLE_HEADER = re.compile(r"^(?P<article_id>[a-z0-9-]+) \| (?P<title>.+)$")
_WORD = re.compile(r"[a-z0-9][a-z0-9_.:/-]{2,}")
_STOP_WORDS = {
    "about",
    "after",
    "and",
    "are",
    "can",
    "does",
    "for",
    "from",
    "how",
    "into",
    "nebula",
    "operator",
    "that",
    "the",
    "this",
    "use",
    "what",
    "when",
    "with",
}
_PRODUCT_MARKERS = {
    "approval",
    "artifact",
    "assistant",
    "automate",
    "browser",
    "compaction",
    "core",
    "desktop",
    "docker",
    "doctor",
    "export",
    "image",
    "import",
    "migration",
    "model",
    "nebula",
    "podman",
    "provider",
    "runner",
    "sandbox",
    "scope",
    "sidecar",
    "terminal",
    "runtime",
    "workspace",
}
_FAILURE_MARKERS = {
    "blocked",
    "cancelled",
    "corrupt",
    "denied",
    "disabled",
    "error",
    "exit_code",
    "failed",
    "failure",
    "missing",
    "offline",
    "rejected",
    "stopped",
    "timed",
    "timed_out",
    "timeout",
    "unavailable",
    "unhealthy",
}

# Every runbook shares product vocabulary ("runner", "image", "terminal",
# "failed"), so a question also scores a tail of articles that merely mention
# its words. A match must hold up against the best one: an article whose own
# keyword phrase the question contains is on topic and needs a third of the
# best score, so a question about two problems gets both runbooks; one that
# matches shared words alone must come within three quarters of it.
_PHRASE_RELATIVE_FLOOR = 1 / 3
_TERM_RELATIVE_FLOOR = 3 / 4
_MIN_SCORE = 6
# Scores grow with the length of what is searched: a long project note shares
# dozens of ordinary words with some runbook's body and outscores a real
# question, so a floor relative to the best match cannot tell that nothing is
# on topic. An operator message qualifies for an article only when at least
# this share of its content words name the article's topic (its title and
# keywords): a runbook question, even a chatty one, clears it; ordinary
# project conversation does not.
_MIN_TOPIC_SHARE = 0.15
# Words that make an operator's message a troubleshooting request even when
# it names nothing of Nebula itself ("why does this fail?", "what's wrong
# here?"): its subject is then the failure in the context they selected.
_TROUBLESHOOTING_WORDS = {
    "broken",
    "fail",
    "fails",
    "failing",
    "fix",
    "help",
    "issue",
    "problem",
    "stuck",
    "why",
    "working",
    "wrong",
}
# What a selected line must name, beside a failure, to report a Nebula one:
# the product's own surfaces, not words any project uses ("docker", "image",
# "model"), so a selected build log of the operator's own project stays theirs.
_NEBULA_SURFACES = {
    "automate",
    "automation",
    "compaction",
    "core",
    "doctor",
    "mcp",
    "nebula",
    "podman",
    "provider",
    "runner",
    "runtime",
    "sandbox",
    "sidecar",
    "terminal",
    "workstation",
}
# A Nebula diagnostic reference, as failure receipts and error notices carry.
_DIAGNOSTIC_REFERENCE = re.compile(r"\berr_[0-9a-f]{32}\b")
# Selected lines that can join the subject: enough for an error and its
# context, never a whole log.
_SELECTION_FAILURE_LINES = 5
_SELECTION_LINE_CHARS = 500
_WORD_ENDINGS = ("ing", "ed", "s")


@dataclass(frozen=True)
class OperatorHelpArticle:
    article_id: str
    title: str
    keywords: tuple[str, ...]
    sources: tuple[str, ...]
    body: str

    @property
    def source_id(self) -> str:
        return f"nebula-help:{self.article_id}"

    @property
    def chunk_id(self) -> str:
        digest = hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:16]
        return f"{self.article_id}:{digest}"

    @property
    def reference_text(self) -> str:
        return (
            f"{self.title}\n\n{self.body}\n\n"
            f"Implementation references: {', '.join(self.sources)}"
        )


@dataclass(frozen=True)
class OperatorHelpMatch:
    article: OperatorHelpArticle
    score: int


@lru_cache(maxsize=1)
def operator_help_articles() -> tuple[OperatorHelpArticle, ...]:
    """Load the release-bundled, reviewable Markdown corpus."""

    text = (
        resources.files("nebula.v3")
        .joinpath("operator_help.md")
        .read_text(encoding="utf-8")
    )
    if f"Corpus: `{CORPUS_ID}`" not in text:
        raise RuntimeError(
            "bundled operator-help corpus version is invalid"
        )  # pragma: no cover
    articles: list[OperatorHelpArticle] = []
    for block in text.split("\n## ")[1:]:
        header, remainder = block.split("\n\n", 1)
        match = _ARTICLE_HEADER.fullmatch(header.strip())
        if match is None:
            raise RuntimeError(
                "bundled operator-help article header is invalid"
            )  # pragma: no cover
        keywords_line, sources_line, body = remainder.split("\n\n", 2)
        if not keywords_line.startswith("Keywords:") or not sources_line.startswith(
            "Sources:"
        ):
            raise RuntimeError(
                "bundled operator-help metadata is invalid"
            )  # pragma: no cover
        keywords = tuple(
            item.strip() for item in keywords_line.removeprefix("Keywords:").split(",")
        )
        sources = tuple(
            item.strip() for item in sources_line.removeprefix("Sources:").split(",")
        )
        articles.append(
            OperatorHelpArticle(
                article_id=match.group("article_id"),
                title=match.group("title").strip(),
                keywords=keywords,
                sources=sources,
                body=body.strip(),
            )
        )
    identifiers = [article.article_id for article in articles]
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise RuntimeError(
            "bundled operator-help identifiers are invalid"
        )  # pragma: no cover
    return tuple(articles)


def _stem(word: str) -> str:
    """``word`` without a plural or tense ending, so "disconnecting" names
    the same topic as "disconnected"."""

    for ending in _WORD_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            return word[: -len(ending)]
    return word


@lru_cache(maxsize=None)
def _topic_words(article: OperatorHelpArticle) -> frozenset[str]:
    text = f"{article.title} {' '.join(article.keywords)}".casefold()
    return frozenset(_stem(word) for word in _WORD.findall(text))


def _topic_share(messages: list[set[str]], article: OperatorHelpArticle) -> float:
    """The largest share of one message's content words that name ``article``."""

    topic = _topic_words(article)
    return max(
        (
            sum(_stem(word) in topic for word in words) / len(words)
            for words in messages
            if words
        ),
        default=0.0,
    )


def _contains_phrase(text: str, phrase: str) -> bool:
    """Whether ``text`` contains ``phrase`` as whole words, not inside one."""

    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _plain_words(text: str) -> set[str]:
    """``text``'s words without the punctuation that ends a clause
    ("error:", "unavailable.")."""

    return {word.rstrip(".:/-") for word in _WORD.findall(text)}


def _selection_failure_lines(selection: str) -> list[str]:
    """The lines of a selection that report a Nebula failure.

    A line qualifies when it states a failure and names a Nebula product
    surface or carries a Nebula diagnostic reference. Long lines are split
    into sentences first, so a one-line log dump yields the failing sentence.
    """

    lines: list[str] = []
    for raw in selection.splitlines():
        for line in (
            re.split(r"(?<=[.!?])\s+", raw)
            if len(raw) > _SELECTION_LINE_CHARS
            else [raw]
        ):
            line = line.strip()[:_SELECTION_LINE_CHARS]
            folded = line.casefold()
            words = _plain_words(folded)
            if (words & _FAILURE_MARKERS and words & _NEBULA_SURFACES) or (
                _DIAGNOSTIC_REFERENCE.search(folded)
            ):
                lines.append(line)
                if len(lines) >= _SELECTION_FAILURE_LINES:
                    return lines
    return lines


def help_subject(operator_text: str, selections: Sequence[str]) -> list[str]:
    """What decides whether a chat turn is about operating Nebula.

    The operator's own words, which a large selected document must neither
    drown nor, by mentioning a runner, turn into a runbook question. Only
    when those words ask about a problem ("why does this fail?") do the
    selected lines that report a Nebula failure join them: the question's
    subject is then the error the operator selected. Search with the result
    (``search_operator_help``).
    """

    folded = operator_text.casefold()
    if "?" not in folded and not _plain_words(folded) & (
        _TROUBLESHOOTING_WORDS | _FAILURE_MARKERS
    ):
        return [operator_text]
    subject = [operator_text]
    for selection in selections:
        for line in _selection_failure_lines(selection):
            if len(subject) > _SELECTION_FAILURE_LINES:
                return subject
            subject.append(line)
    return subject


def search_operator_help(
    queries: list[str],
    *,
    limit: int = 4,
    observed_failure: bool = False,
) -> tuple[OperatorHelpMatch, ...]:
    """Return only high-signal product-help matches in deterministic order.

    ``queries`` must carry a product or failure word, and an article must be
    what one of them is about (``_MIN_TOPIC_SHARE``). A chat passes what the
    turn asks about operating Nebula (``help_subject``), so a large selected
    document neither drowns a runbook question nor, by mentioning a runner,
    turns an ordinary note into one. With ``observed_failure`` the queries
    are Core's receipts of tool steps that failed in this turn, not
    conversation, and the failure itself is the reason to look for a
    recovery procedure. The best remaining match qualifies; the others must
    hold up against it (see ``_PHRASE_RELATIVE_FLOOR``), so a weakly related
    tail is not sent beside the runbook the question is about.
    """

    if limit < 1:
        return ()
    query_text = " ".join(queries).casefold()
    raw_terms = set(_WORD.findall(query_text))
    if not _plain_words(query_text) & (_PRODUCT_MARKERS | _FAILURE_MARKERS):
        return ()
    # Failure words decide whether recovery lookup is appropriate, but they are
    # intentionally excluded from ranking because nearly every runbook describes
    # a failure. Specific product nouns and observed identifiers choose the article.
    terms = raw_terms - _STOP_WORDS - _FAILURE_MARKERS
    messages = [
        set(_WORD.findall(text.casefold())) - _STOP_WORDS - _FAILURE_MARKERS
        for text in queries
    ]
    ranked: list[tuple[int, int, bool, OperatorHelpArticle]] = []
    for ordinal, article in enumerate(operator_help_articles()):
        keyword_text = " ".join(article.keywords).casefold()
        title_text = article.title.casefold()
        searchable = f"{title_text} {keyword_text} {article.body.casefold()}"
        score = 0
        for term in terms:
            occurrences = min(searchable.count(term), 3)
            score += occurrences * 2
            if term in title_text or term in keyword_text:
                score += 3
        score += sum(
            10 for keyword in article.keywords if keyword.casefold() in query_text
        )
        if score >= _MIN_SCORE and (
            observed_failure or _topic_share(messages, article) >= _MIN_TOPIC_SHARE
        ):
            phrase = any(
                _contains_phrase(query_text, keyword.casefold())
                for keyword in article.keywords
            )
            ranked.append((score, ordinal, phrase, article))
    if not ranked:
        return ()
    ranked.sort(key=lambda item: (-item[0], item[1]))
    best = ranked[0][0]
    return tuple(
        OperatorHelpMatch(article=article, score=score)
        for score, _ordinal, phrase, article in ranked
        if score >= best * (_PHRASE_RELATIVE_FLOOR if phrase else _TERM_RELATIVE_FLOOR)
    )[:limit]


__all__ = [
    "CORPUS_ID",
    "OperatorHelpArticle",
    "OperatorHelpMatch",
    "help_subject",
    "operator_help_articles",
    "search_operator_help",
]
