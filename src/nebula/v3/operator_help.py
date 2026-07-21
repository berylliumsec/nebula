"""Deterministic retrieval over the bundled Nebula 3 operator-help corpus."""

from __future__ import annotations

import hashlib
import re
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


def search_operator_help(
    queries: list[str], *, limit: int = 4
) -> tuple[OperatorHelpMatch, ...]:
    """Return only high-signal product-help matches in deterministic order."""

    if limit < 1:
        return ()
    query_text = " ".join(queries).casefold()
    raw_terms = set(_WORD.findall(query_text))
    if not raw_terms & (_PRODUCT_MARKERS | _FAILURE_MARKERS):
        return ()
    # Failure words decide whether recovery lookup is appropriate, but they are
    # intentionally excluded from ranking because nearly every runbook describes
    # a failure. Specific product nouns and observed identifiers choose the article.
    terms = raw_terms - _STOP_WORDS - _FAILURE_MARKERS
    ranked: list[tuple[int, int, OperatorHelpArticle]] = []
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
        if score >= 6:
            ranked.append((score, ordinal, article))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return tuple(
        OperatorHelpMatch(article=article, score=score)
        for score, _ordinal, article in ranked[:limit]
    )


__all__ = [
    "CORPUS_ID",
    "OperatorHelpArticle",
    "OperatorHelpMatch",
    "operator_help_articles",
    "search_operator_help",
]
