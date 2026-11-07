"""The settings assistant can discover the terminal Snapshot from product guidance."""

from __future__ import annotations

from scripts.build_product_knowledge import ensure, source_digest, source_paths


def test_terminal_snapshot_guidance_is_searchable_and_syncs_to_core(
    tmp_path, monkeypatch
):
    ensure()
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from nebula.v3.product_knowledge import search_product_knowledge

    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How do I open and refresh a project snapshot in nebula-chat?"
    )
    content = " ".join(str(row["text"]) for row in matches)
    assert "/snapshot" in content
    assert "F11" in content

    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    synced = index.sync_product_knowledge()
    assert synced["source_digest"] == source_digest(source_paths())
    assert (
        index._product_collection.metadata["source_digest"] == synced["source_digest"]
    )
