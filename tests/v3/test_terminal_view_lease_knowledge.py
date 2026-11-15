"""The settings assistant can explain the terminal's project-view lease and /board track title."""

from __future__ import annotations

from scripts.build_product_knowledge import ensure, source_digest, source_paths


def test_terminal_view_lease_guidance_is_searchable_and_syncs_to_core(
    tmp_path, monkeypatch
):
    ensure()
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from nebula.v3.product_knowledge import search_product_knowledge

    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    lease = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "terminal nebula-chat project view lease renews releases quit"
        )
    )
    assert "The terminal Assistant (`nebula-chat`) holds the same lease" in lease
    assert "releases it when you switch Core or quit" in lease
    track = " ".join(
        str(row["text"])
        for row in search_product_knowledge("/board track default title from Core")
    )
    assert "current title from Core" in track

    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    synced = index.sync_product_knowledge()
    assert synced["source_digest"] == source_digest(source_paths())
    assert (
        index._product_collection.metadata["source_digest"] == synced["source_digest"]
    )
