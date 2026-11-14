"""Butler can discover terminal board reads and actions and synchronize guidance."""

from scripts.build_product_knowledge import ensure, source_digest, source_paths


def test_terminal_board_guidance_is_searchable_and_syncs_to_core(tmp_path, monkeypatch):
    ensure()
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from nebula.v3.product_knowledge import search_product_knowledge

    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge("nebula-chat /board Work check-ins")
    content = " ".join(str(row["text"]) for row in matches)
    assert "/board" in content
    actions = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "nebula-chat /board check in track conversation link agent Work tools"
        )
    )
    assert "/board" in actions
    assert "check-in" in actions.lower() and "agent work tools" in actions.lower()
    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    synced = index.sync_product_knowledge()
    assert synced["source_digest"] == source_digest(source_paths())
    assert (
        index._product_collection.metadata["source_digest"] == synced["source_digest"]
    )
