"""The settings assistant can explain terminal composer and picker controls."""

from __future__ import annotations

from scripts.build_product_knowledge import ensure, source_digest, source_paths


def test_terminal_message_recall_guidance_is_searchable_and_syncs_to_core(
    tmp_path, monkeypatch
):
    ensure()
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from nebula.v3.product_knowledge import search_product_knowledge

    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge("Up Down composer draft message recall")
    content = " ".join(str(row["text"]) for row in matches)
    assert "Up" in content
    assert "Down" in content
    assert "draft" in content
    picker = search_product_knowledge(
        "/skills /goal skills /mcp select several skills and MCP servers"
    )
    picker_content = " ".join(str(row["text"]) for row in picker)
    assert "/skills" in picker_content
    assert "/goal skills" in picker_content
    assert "/mcp" in picker_content

    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    synced = index.sync_product_knowledge()
    assert synced["source_digest"] == source_digest(source_paths())
    assert (
        index._product_collection.metadata["source_digest"] == synced["source_digest"]
    )
