from pathlib import Path

from nebula.v3.operator_help import (
    CORPUS_ID,
    operator_help_articles,
    search_operator_help,
)
from nebula.v3.domain import RiskClass
from nebula.v3.tool_failures import tool_failure
from nebula.v3.tool_results import serialize_model_result
from nebula.v3.tools import ToolSpec
from scripts.context_retention_eval import DEFAULT_SEED, SCENARIOS, build_scenario


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


def test_ordinary_project_conversation_gets_no_operator_help():
    # The retention eval's turns are ordinary project conversation: status
    # notes that mention a storage migration and a vendor comparison, and
    # questions about planted facts. Their length made some runbook share
    # dozens of words with them, and the best of those was sent with two of
    # them (mcp-servers and runner-setup, about 5.6K characters).
    for name in SCENARIOS:
        for turn in build_scenario(name, DEFAULT_SEED).turns:
            assert _articles(turn.content) == [], (name, turn.role)
    assert (
        _articles(
            "Here is the storage migration plan for the audit log store: move to "
            "PostgreSQL next sprint, and keep the old export until the model "
            "review is done."
        )
        == []
    )
    assert _articles("What port is relevant?") == []


def test_a_chatty_runbook_question_still_gets_its_runbook():
    assert _articles(
        "Hi! I've been trying all morning and I keep seeing that Nebula says no "
        "rootless container runner is available, even though I installed Docker "
        "yesterday following the guide. What should I check first?"
    ) == ["runner-setup"]
    assert _articles(
        "My terminal keeps disconnecting every ten minutes or so while I'm "
        "running long scans, is that expected?"
    ) == ["human-terminal"]
    assert _articles(
        "Can you help? The assistant says the model is unavailable for the "
        "provider I configured this morning."
    ) == ["provider-model"]
    assert _articles(
        "Why does my command keep waiting for approval? It is a simple port "
        "scan on the target in scope."
    ) == ["scope-approval"]


def test_a_failed_tool_receipt_is_searched_for_its_recovery_procedure():
    # Final synthesis searches Core's own receipts of the turn's failed steps.
    # They are not conversation and name no topic of their own; the failure is
    # the reason to look, and Settings > Diagnostics is where an error
    # reference is inspected.
    spec = ToolSpec(
        name="safe_read",
        description="Return one bounded value.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
    )
    receipt = serialize_model_result(
        tool_failure(
            spec,
            {"value": "a"},
            RuntimeError("the probe failed"),
            phase="after_execution",
            call_id="call-1",
        )
    )

    assert search_operator_help([receipt]) == ()
    observed = search_operator_help([receipt], observed_failure=True)
    assert observed[0].article.article_id == "diagnostics"


def test_chat_attaches_no_help_to_a_status_note_but_keeps_it_for_a_failed_step(
    tmp_path,
):
    import asyncio

    from nebula.v3.chat import ChatCompletionRequest, ChatService
    from nebula.v3.domain import Engagement
    from nebula.v3.providers import ToolCall
    from nebula.v3.storage import NebulaStore
    from tests.v3.test_chat import FakeProvider, _profile
    from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response

    store = NebulaStore(tmp_path / "status-note.db")
    engagement = store.create(Engagement(id="eng-notes", name="Status notes"))
    profile = store.create(_profile(local=True))
    provider = FakeProvider(profile.id, local=True)
    note = build_scenario("s1", DEFAULT_SEED).turns[0].content
    prepared = ChatService(store, provider_factory=lambda _: provider).prepare(
        ChatCompletionRequest(
            engagement_id=engagement.id,
            provider_id=profile.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": note}],
        )
    )
    assert prepared.citations == []
    assert prepared.reference_material == ""
    assert prepared.model_request.messages[-1].content == note

    class FailingBroker(RecordingBroker):
        async def execute(self, invocation, scope, *, approval=None):
            del scope, approval
            self.calls.append(invocation)
            raise RuntimeError("the probe failed")

    _, service, turn, scripted = _prepared(
        tmp_path / "failed-step",
        [
            _response(
                calls=[
                    ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})
                ]
            ),
            _response(),
            _response(text="The read failed."),
        ],
        FailingBroker(),
    )
    completion = asyncio.run(service.complete(turn))

    assert [citation.source_id for citation in completion.citations] == [
        "nebula-help:diagnostics"
    ]
    final = [request for request in scripted.requests if not request.metadata]
    assert "nebula-help:diagnostics" in (final[-1].instructions or "")


def _prepare_with_selection(tmp_path, question: str, selected: str):
    import hashlib

    from nebula.v3.chat import ChatCompletionRequest, ChatService
    from nebula.v3.domain import Engagement
    from nebula.v3.storage import NebulaStore
    from tests.v3.test_chat import FakeProvider, _profile

    store = NebulaStore(tmp_path / "selection.db")
    engagement = store.create(Engagement(id="eng-selection", name="Selection"))
    profile = store.create(_profile(local=True))
    provider = FakeProvider(profile.id, local=True)
    return ChatService(store, provider_factory=lambda _: provider).prepare(
        ChatCompletionRequest(
            engagement_id=engagement.id,
            provider_id=profile.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": question}],
            context_attachments=[
                {
                    "source_kind": "document",
                    "source_label": "Selected text",
                    "text": selected,
                    "sha256": hashlib.sha256(selected.encode("utf-8")).hexdigest(),
                }
            ],
        )
    )


def test_selected_context_neither_hides_nor_invents_a_runbook_question(tmp_path):
    # Whether a turn is about operating Nebula is decided by the operator's
    # own words. A large selection used to dilute a runbook question below
    # the topic share, and a selection that mentions a runner must not turn
    # an ordinary note into one.
    note = build_scenario("s1", DEFAULT_SEED).turns[0].content
    selection = (note * 2)[:9_000]
    question = (
        "Nebula says no rootless container runner is available. "
        "What should I check first?"
    )

    prepared = _prepare_with_selection(tmp_path / "question", question, selection)

    assert [item.source_id for item in prepared.citations] == [
        "nebula-help:runner-setup"
    ]
    current = prepared.model_request.messages[-1].content
    assert isinstance(current, str) and "BEGIN SELECTED CONTEXT" in current

    ordinary = _prepare_with_selection(
        tmp_path / "note",
        "Keep this for later.",
        "Runner unavailable: the rootless docker runner was not detected on the "
        "build host, so the podman machine took over.",
    )

    assert ordinary.citations == []
    assert ordinary.reference_material == ""
