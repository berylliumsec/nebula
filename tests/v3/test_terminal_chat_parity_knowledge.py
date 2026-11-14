"""The settings assistant can explain terminal chat lifecycle commands."""

from __future__ import annotations

from scripts.build_product_knowledge import ensure, source_digest, source_paths


def test_terminal_chat_lifecycle_guidance_is_searchable_and_syncs_to_core(
    tmp_path, monkeypatch
):
    ensure()
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from nebula.v3.product_knowledge import search_product_knowledge

    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    thinking = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "nebula-chat /thinking collapsed thinking elapsed time tokens"
        )
    )
    assert "/thinking" in thinking
    edits = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "nebula-chat /chat edit rewinds and sends the edited text /chat bulk"
        )
    )
    assert "/chat edit" in edits and "/chat bulk" in edits
    found = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "nebula-chat /search bookmarked messages /bookmark /side side chat"
        )
    )
    assert "/search" in found and "/bookmark" in found and "/side" in found

    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    synced = index.sync_product_knowledge()
    assert synced["source_digest"] == source_digest(source_paths())
    assert (
        index._product_collection.metadata["source_digest"] == synced["source_digest"]
    )
