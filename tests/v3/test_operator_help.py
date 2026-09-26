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
        "automation-runtime",
        "scope-approval",
        "provider-model",
        "reviewed-execution",
        "workspace-limits",
        "context-compaction",
        "migration-import-export",
        "mcp-servers",
        "release-boundary",
    ]
    assert len({article.source_id for article in articles}) == len(articles)
    assert len({article.chunk_id for article in articles}) == len(articles)
    assert all(article.keywords and article.sources for article in articles)
    assert all(
        "Implementation references:" in article.reference_text for article in articles
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
    mcp = search_operator_help(
        ["How do I import MCP servers from my claude_desktop_config.json?"]
    )

    assert runner[0].article.article_id == "runner-setup"
    assert terminal[0].article.article_id == "human-terminal"
    assert nmap[0].article.article_id == "human-terminal"
    assert restore[0].article.article_id == "migration-import-export"
    assert mcp[0].article.article_id == "mcp-servers"
    assert all(
        match.score >= 6 for match in [*runner, *terminal, *nmap, *restore, *mcp]
    )


def _articles(question: str) -> list[str]:
    return [
        match.article.article_id
        for match in search_operator_help(
            [question], limit=len(operator_help_articles())
        )
    ]


def test_one_strong_match_leaves_out_the_weakly_related_tail():
    # Every runbook shares product vocabulary, so each of these questions used
    # to pull in three to seven more articles that only mention its words.
    assert _articles(
        "Nebula says no rootless container runner is available. "
        "What should I check first?"
    ) == ["runner-setup"]
    assert _articles("nmap failed with operation not permitted") == ["human-terminal"]
    assert _articles("terminal disconnected after ten minutes") == ["human-terminal"]
    assert _articles("Why was my command denied by scope approval?") == [
        "scope-approval"
    ]
    assert _articles("Core is offline and the desktop is blank") == ["core-startup"]
    # A keyword inside another word ("apt" in "adapter") scores, but does not
    # put an article on topic.
    assert _articles("The adapter for the model provider failed") == ["provider-model"]
    # An observed failure, as final synthesis searches it, still finds its runbook.
    assert _articles(
        '{"error":"podman unavailable: no rootless runtime was detected"}'
    ) == ["runner-setup"]


def test_each_problem_a_question_names_keeps_its_runbook():
    assert _articles(
        "The terminal disconnected and importing MCP servers from "
        "claude_desktop_config.json failed"
    ) == ["mcp-servers", "human-terminal"]
    assert _articles(
        "The workstation image failed to build and the provider says model unavailable"
    ) == ["workstation-image", "provider-model"]
    assert _articles("context compaction failed and the terminal stopped") == [
        "context-compaction",
        "human-terminal",
    ]
    # Two equally strong matches on shared words alone are both kept.
    assert _articles(
        "The model provider returned 429 and the docker runner is unhealthy"
    ) == ["runner-setup", "provider-model"]
    # Restoring an export: the recovery runbook, and the release boundary that
    # says engagement bundle restore is not available.
    assert _articles("How do I restore a Nebula zip export?") == [
        "migration-import-export",
        "release-boundary",
    ]


def test_chat_sends_at_most_three_runbooks():
    from nebula.v3.chat import ChatService

    question = (
        "context compaction failed, terminal stopped, workstation image failed "
        "and model unavailable"
    )
    assert len(_articles(question)) == 4

    chunks = ChatService._retrieve_operator_help([question], token_budget=100_000)

    assert [chunk.citation.source_id for chunk in chunks] == [
        f"nebula-help:{article}" for article in _articles(question)[:3]
    ]
