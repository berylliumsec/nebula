"""The settings assistant can explain terminal type-ahead and picker search."""

from __future__ import annotations

from scripts.build_product_knowledge import ensure, source_digest, source_paths


def test_terminal_input_guidance_is_searchable_and_syncs(tmp_path, monkeypatch):
    ensure()
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from nebula.v3.product_knowledge import search_product_knowledge

    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    content = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "nebula-chat Enter while a command finishes next input Space search multi-select"
        )
    )
    assert "Next:" in content
    assert "multi-word" in content

    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    synced = index.sync_product_knowledge()
    assert synced["source_digest"] == source_digest(source_paths())
    assert (
        index._product_collection.metadata["source_digest"] == synced["source_digest"]
    )
