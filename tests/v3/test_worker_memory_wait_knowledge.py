"""The settings assistant can explain waiting for memory and the default Core reserve."""

from __future__ import annotations

from scripts.build_product_knowledge import ensure, source_digest, source_paths


def test_waiting_for_memory_guidance_is_searchable_and_syncs_to_core(
    tmp_path, monkeypatch
):
    ensure()
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from nebula.v3.product_knowledge import search_product_knowledge

    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    waiting = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "project waiting for memory Core reserve full project workers"
        )
    )
    assert "Waiting for memory" in waiting
    assert "NEBULA_WORKER_MEMORY_WAIT_SECONDS" in waiting
    default = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "default Core reserve sized from host memory saved policy never changed"
        )
    )
    assert "a quarter of the host's capacity" in default
    assert "A saved policy is never changed" in default
    butler = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "Butler run more projects at once set_memory_budget Core reserve"
        )
    )
    assert "must not archive" in butler

    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    synced = index.sync_product_knowledge()
    assert synced["source_digest"] == source_digest(source_paths())
    assert (
        index._product_collection.metadata["source_digest"] == synced["source_digest"]
    )
