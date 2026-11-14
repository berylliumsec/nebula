"""The settings assistant can explain terminal tool follow-up and VPN routes."""

from __future__ import annotations

from scripts.build_product_knowledge import ensure, source_digest, source_paths


def test_terminal_tool_followup_and_vpn_guidance_is_searchable_and_syncs(
    tmp_path, monkeypatch
):
    ensure()
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from nebula.v3.product_knowledge import search_product_knowledge

    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    content = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "nebula-chat /project followup tool follow-up VPN route OpenVPN profile"
        )
    )
    assert "/project followup" in content
    assert "VPN" in content

    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    synced = index.sync_product_knowledge()
    assert synced["source_digest"] == source_digest(source_paths())
    assert (
        index._product_collection.metadata["source_digest"] == synced["source_digest"]
    )
