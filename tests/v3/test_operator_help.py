from pathlib import Path

from nebula.v3.operator_help import (
    CORPUS_ID,
    operator_help_articles,
    search_operator_help,
)


ROOT = Path(__file__).resolve().parents[2]


def test_bundled_operator_help_is_complete_auditable_and_documented():
    articles = operator_help_articles()

    assert CORPUS_ID == "nebula.operator-help/v1"
    assert [article.article_id for article in articles] == [
        "core-startup",
        "diagnostics",
        "runner-setup",
        "workstation-image",
        "human-terminal",
        "toolbox-availability",
        "scope-approval",
        "provider-model",
        "reviewed-execution",
        "workspace-limits",
        "context-compaction",
        "migration-import-export",
        "release-boundary",
    ]
    assert len({article.source_id for article in articles}) == len(articles)
    assert len({article.chunk_id for article in articles}) == len(articles)
    assert all(article.keywords and article.sources for article in articles)
    assert all(
        "Implementation references:" in article.reference_text
        for article in articles
    )

    guide = (ROOT / "docs/NEBULA3.md").read_text(encoding="utf-8")
    assert "Built-in operator help" in guide
    assert "../src/nebula/v3/operator_help.md" in guide


def test_operator_help_search_requires_product_or_failure_intent_and_ranks_details():
    assert search_operator_help(["What port is relevant?"]) == ()
    assert search_operator_help(["Nebula runner unavailable"], limit=0) == ()

    runner = search_operator_help(
        ["Nebula says no supported rootless container runner is available"]
    )
    terminal = search_operator_help(["terminal disconnected after ten minutes"])
    nmap = search_operator_help(["nmap failed with operation not permitted"])
    restore = search_operator_help(["How do I restore a Nebula zip export?"])

    assert runner[0].article.article_id == "runner-setup"
    assert terminal[0].article.article_id == "human-terminal"
    assert nmap[0].article.article_id == "human-terminal"
    assert restore[0].article.article_id == "migration-import-export"
    assert all(match.score >= 6 for match in [*runner, *terminal, *nmap, *restore])
