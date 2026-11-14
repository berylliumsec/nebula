"""The settings assistant can explain terminal goals, approvals and subagents."""

from __future__ import annotations

from scripts.build_product_knowledge import ensure, source_digest, source_paths


def test_terminal_goal_approval_and_subagent_guidance_is_searchable_and_syncs(
    tmp_path, monkeypatch
):
    ensure()
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from nebula.v3.product_knowledge import search_product_knowledge

    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    goals = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "nebula-chat /goal complete evidence /goal child child goal"
        )
    )
    assert "/goal complete" in goals and "/goal child" in goals
    approvals = " ".join(
        str(row["text"])
        for row in search_product_knowledge(
            "nebula-chat /approvals subagent edit request always allow SSH host /agents stop all"
        )
    )
    assert "/approvals" in approvals and "/agents" in approvals

    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    synced = index.sync_product_knowledge()
    assert synced["source_digest"] == source_digest(source_paths())
    assert (
        index._product_collection.metadata["source_digest"] == synced["source_digest"]
    )
