"""The assistant can explain a project held for a Core update (#1232)."""

from nebula.v3.knowledge_index import ChromaKnowledgeIndex
from nebula.v3.product_knowledge import (
    packaged_product_passages,
    search_product_knowledge,
)


def test_bundled_guidance_explains_a_held_project_and_syncs_digest(
    tmp_path, monkeypatch
):
    from scripts.build_product_knowledge import ensure, source_digest, source_paths

    ensure()
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    digest, _ = packaged_product_passages()
    assert digest == source_digest(source_paths())
    for question in (
        "Why is work in my project paused and Send disabled?",
        "The goal says paused by update, how do I resume it?",
        "Should I release the project hold?",
    ):
        matches = search_product_knowledge(question)
        assert any(
            item["heading"] == "Projects paused for a Core update" for item in matches
        ), question
    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    result = index.sync_product_knowledge()
    assert result["source_digest"] == digest
    assert index._product_collection.metadata["source_digest"] == digest
