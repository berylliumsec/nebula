"""Ranking archived conversation passages for a compacted request."""

from __future__ import annotations

import math

from nebula.v3.context import estimate_tokens, lexical_score
from nebula.v3.context_retrieval import (
    CHUNK_TOKENS,
    ArchivedMessage,
    DenseEncoder,
    VectorCache,
    chunk_message,
    chunk_messages,
    rank_chunks,
    select_excerpts,
    terms,
    turn_query,
)


def _message(identity: str, sequence: int, text: str, role: str = "user"):
    return ArchivedMessage(id=identity, sequence=sequence, role=role, text=text)


def _long_message(planted: str, *, paragraphs: int = 20, at: int = 13) -> str:
    filler = "routine status update with nothing notable to report today. " * 12
    return "\n\n".join(
        f"Paragraph {index}: " + (planted + " " if index == at else "") + filler
        for index in range(paragraphs)
    )


def test_chunks_cover_the_message_in_bounded_paragraph_aligned_passages():
    text = _long_message("The vault path is secret/staging/db.")
    chunks = chunk_message(_message("m1", 4, text, role="assistant"))

    assert len(chunks) > 1
    assert all(chunk.count == len(chunks) for chunk in chunks)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert text[chunk.start : chunk.end] == chunk.text
        assert estimate_tokens(chunk.text) <= CHUNK_TOKENS
        # Whole paragraphs: every passage starts where a paragraph starts.
        assert chunk.text.startswith("Paragraph ")
        assert chunk.message_id == "m1" and chunk.sequence == 4
        assert chunk.role == "assistant"
    # Nothing but paragraph separators is left out.
    covered = "".join(text[chunk.start : chunk.end] for chunk in chunks)
    assert covered.replace(" ", "") == text.replace("\n", "").replace(" ", "")


def test_unbroken_and_multibyte_text_splits_within_the_limit():
    text = "x" * 5_000 + " " + "界" * 1_500
    chunks = chunk_message(_message("m1", 1, text))

    assert all(estimate_tokens(chunk.text) <= CHUNK_TOKENS for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == text.replace(" ", "")
    assert chunk_message(_message("empty", 2, "   \n\n  ")) == []


def test_compound_identifiers_index_whole_and_by_part():
    found = terms("Restart payments-gw; see /etc/nginx/nginx.conf. Yes, do that.")

    assert "payments-gw" in found and "payments" in found and "gw" in found
    assert "/etc/nginx/nginx.conf" not in found  # leading slash is not a token start
    assert "etc/nginx/nginx.conf" in found and "nginx" in found
    # Function words and filler carry no retrieval signal.
    assert not {"yes", "do", "that"} & set(found)


def test_idf_and_length_normalisation_beat_raw_term_counts():
    repeated = "the server config server server server config restart server"
    archive = [_message(f"common-{index}", index, repeated) for index in range(1, 6)]
    archive.append(
        _message("rare", 9, "We decided the kerberos keytab lives on the auth host.")
    )
    query = "kerberos server config"

    # The previous ranker counted raw occurrences, so repetition of common
    # words outranked the one message about the rare subject.
    assert lexical_score(query, repeated) > lexical_score(query, archive[-1].text)

    ranked = rank_chunks(chunk_messages(archive), turn_query(query))

    assert ranked[0].chunk.message_id == "rare"


def test_exact_identifier_outranks_general_word_overlap():
    archive = [
        _message(
            "general",
            1,
            "The login page vulnerability scan found several login issues on "
            "the login endpoint; the login flow needs review.",
        ),
        _message("exact", 2, "Tracking CVE-2025-12345 separately; patch Friday."),
    ]

    ranked = rank_chunks(
        chunk_messages(archive), turn_query("Is CVE-2025-12345 the login issue?")
    )

    assert ranked[0].chunk.message_id == "exact"


def test_a_long_message_contributes_its_relevant_paragraph_within_budget():
    planted = "The staging database password rotates Thursday via secret/staging/db."
    long_text = _long_message(planted)
    archive = [
        _message("long", 3, long_text, role="assistant"),
        _message("other", 5, "Unrelated note about the office coffee machine."),
    ]
    budget = 600
    # A whole-message excerpt never fits: this is why the old ranker could
    # not retrieve anything from it.
    assert estimate_tokens(long_text, message_count=1) > budget

    ranked = rank_chunks(
        chunk_messages(archive),
        turn_query("When does the staging database password rotate?"),
    )
    excerpts = select_excerpts(ranked, token_budget=budget)

    assert [chunk.message_id for chunk in excerpts] == ["long"]
    payload = excerpts[0].payload()
    assert planted in payload["content"]
    assert payload["part"] == "14/20"
    assert sum(chunk.tokens for chunk in excerpts) <= budget


def test_follow_up_without_content_retrieves_through_the_tail_query():
    archive = [
        _message(
            "plan",
            1,
            "Plan: rotate the signing key for payments-gw after the audit and "
            "keep the old key valid for 24h.",
        ),
        _message(
            "noise",
            2,
            "Noted. That covers what is pending; do that later, that and that.",
            role="assistant",
        ),
        _message("noise-2", 3, "Yes, that one is fine, do go ahead."),
    ]
    tail = [
        ("user", "Can we get the payments-gw key rotation going?"),
        ("assistant", "I can start the rotation now if you want."),
    ]
    current = "yes, do that"

    # The current message alone matched only filler, so the plan was never
    # the excerpt a bare confirmation received.
    old_best = max(archive, key=lambda item: lexical_score(current, item.text))
    assert old_best.id != "plan"
    # Without the tail there is nothing to look for.
    assert rank_chunks(chunk_messages(archive), turn_query(current)) == []

    ranked = rank_chunks(chunk_messages(archive), turn_query(current, tail))

    assert [item.chunk.message_id for item in ranked] == ["plan"]


def test_a_question_the_archive_cannot_answer_does_not_echo_the_last_exchange():
    filler = "status update with nothing notable to report today"
    archive = [
        _message(f"filler-{index}", index, f"message {index}: {filler}")
        for index in range(1, 30)
    ]
    tail = [("user", f"message 40: {filler}"), ("assistant", f"message 41: {filler}")]

    # Every archived passage shares the tail's words, so under IDF they say
    # nothing; the question's own words match nothing.
    ranked = rank_chunks(
        chunk_messages(archive), turn_query("Which credential store was picked?", tail)
    )

    assert ranked == []


def test_tail_query_weighs_the_current_message_above_earlier_turns():
    query = turn_query(
        "what about the redis cache?",
        [
            ("user", "old question about postgres"),
            ("assistant", "an answer"),
            ("user", "newer question about nginx"),
            ("assistant", "latest assistant reply mentions kafka"),
        ],
    )

    assert [(part.text, part.weight) for part in query] == [
        ("what about the redis cache?", 1.0),
        ("newer question about nginx", 0.5),
        ("old question about postgres", 0.35),
        ("latest assistant reply mentions kafka", 0.25),
    ]
    archive = [
        _message("redis", 1, "The redis cache is sized at 2 GB."),
        _message("nginx", 2, "The nginx upstream timeout is 30s."),
    ]
    ranked = rank_chunks(chunk_messages(archive), query)
    assert [item.chunk.message_id for item in ranked] == ["redis", "nginx"]


def test_selection_respects_budget_limit_order_and_duplicates():
    archive = [
        _message(f"m{index}", index, f"deploy window note {index}: deploy after 22:00")
        for index in range(1, 12)
    ]
    archive.append(_message("dup", 20, archive[0].text))
    ranked = rank_chunks(chunk_messages(archive), turn_query("deploy window"))

    chosen = select_excerpts(ranked, token_budget=10_000, limit=8)

    assert len(chosen) == 8
    assert [chunk.sequence for chunk in chosen] == sorted(
        chunk.sequence for chunk in chosen
    )
    assert len({chunk.text for chunk in chosen}) == len(chosen)
    small = select_excerpts(ranked, token_budget=ranked[0].chunk.tokens)
    assert len(small) == 1
    assert select_excerpts(ranked, token_budget=0) == []


class _TopicEncoder:
    """A deterministic stand-in for an embedding model: one axis per topic."""

    topics = {
        "database": ("database", "db", "postgres", "reboot", "restart", "prod"),
        "frontend": ("vite", "cdn", "front-end", "css"),
        "people": ("on-call", "handoff", "rotation", "tone"),
    }

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            folded = text.casefold()
            vector = [
                float(sum(folded.count(word) for word in words))
                for words in self.topics.values()
            ]
            vectors.append(vector if any(vector) else [0.01, 0.01, 0.01])
        return vectors


def test_dense_fusion_adds_a_paraphrase_the_keywords_miss_and_caches_vectors():
    archive = [
        _message(
            "rule",
            1,
            "The production database must never be restarted during business "
            "hours; only after 22:00 UTC.",
        ),
        _message("build", 2, "The front-end build uses vite and ships to the CDN."),
    ]
    chunks = chunk_messages(archive)
    query = turn_query("When may I reboot prod?")
    assert rank_chunks(chunks, query) == []

    encoder = _TopicEncoder()
    dense = DenseEncoder(encode=encoder, model="topics", cache=VectorCache())
    ranked = rank_chunks(chunks, query, dense=dense)

    assert [item.chunk.message_id for item in ranked] == ["rule"]
    assert ranked[0].lexical == 0 and ranked[0].dense is not None
    assert math.isclose(ranked[0].dense, 1.0, rel_tol=1e-6)
    # Passages and query parts are embedded once: a turn's message is the
    # next turn's tail, and archived passages never change.
    # No lexical candidate, so the newest passages are embedded first.
    assert encoder.calls == [
        ["When may I reboot prod?", *(c.text for c in reversed(chunks))]
    ]
    assert len(dense.cache) == 3
    assert rank_chunks(chunks, query, dense=dense) == ranked
    assert len(encoder.calls) == 1


def test_dense_embedding_is_bounded_per_call():
    archive = [
        _message(f"m{index}", index, f"database note {index}") for index in range(1, 41)
    ]
    encoder = _TopicEncoder()
    dense = DenseEncoder(encode=encoder, model="topics", cache=VectorCache(), max_new=5)

    rank_chunks(chunk_messages(archive), turn_query("database"), dense=dense)

    assert len(encoder.calls[0]) == 1 + 5
    # Five passages and the query.
    assert len(dense.cache) == 6


def test_a_failing_embedding_model_leaves_the_keyword_ranking():
    def broken(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("model unavailable")

    archive = [_message("a", 1, "kerberos keytab on the auth host")]
    dense = DenseEncoder(encode=broken, model="broken", cache=VectorCache())

    ranked = rank_chunks(chunk_messages(archive), turn_query("kerberos"), dense=dense)

    assert [item.chunk.message_id for item in ranked] == ["a"]
    assert ranked[0].dense is None
