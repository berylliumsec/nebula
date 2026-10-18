from __future__ import annotations

import sqlite3

import pytest

from nebula.v3.product_knowledge import search_product_knowledge
from nebula.v3.knowledge_index import ChromaKnowledgeIndex


@pytest.fixture(scope="module", autouse=True)
def generated_index():
    from scripts.build_product_knowledge import ensure

    ensure()


def test_packaged_product_index_has_current_source_digest():
    from scripts.build_product_knowledge import (
        OUTPUT,
        database_bytes,
        source_digest,
        source_paths,
    )

    with sqlite3.connect(":memory:") as connection:
        connection.deserialize(database_bytes())
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        count = connection.execute("SELECT count(*) FROM passages").fetchone()[0]
    assert metadata["schema"] == "nebula.product-knowledge/v1"
    assert metadata["source_digest"] == source_digest(source_paths())
    assert count == int(metadata["passage_count"])
    assert 50 <= count <= 600
    assert OUTPUT.stat().st_size < 1_000_000
    assert OUTPUT.read_text(encoding="ascii").startswith(
        "# Generated SQLite index; zlib plus base85;"
    )


def test_product_search_finds_docker_host_access_guidance(monkeypatch):
    # Search must work without a warmed embedding model or a network request.
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Why can't my assistant edit a host file in Docker mode?"
    )
    assert matches
    assert any(
        "docker" in str(item["text"]).lower() and "host" in str(item["text"]).lower()
        for item in matches
    )
    assert all(
        str(item["source"]).startswith(
            ("docs/", "README.md", "ui/src/settingsCatalog.ts")
        )
        for item in matches
    )
    assert search_product_knowledge(" ") == []


def test_product_search_finds_other_operator_destinations(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    for question, destination in (
        ("How do I pair a phone with Nebula?", "Paired devices"),
        ("Where do I configure an MCP server?", "MCP servers"),
        (
            "Where is project policy and network scope?",
            "Project policy and network scope",
        ),
        ("Where do I connect a storage webhook?", "Storage sources"),
    ):
        matches = search_product_knowledge(question)
        assert any(item["heading"] == destination for item in matches), question


def test_product_search_explains_harness_goal_blocking(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Why does blocking a Work item not stop Core goal continuation?"
    )
    assert any(
        "goal.block" in str(item["text"]) and "work item" in str(item["text"]).lower()
        for item in matches
    )


def test_product_search_finds_nebula_wide_butler_instructions(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Where do I put the Nebula-wide BUTLER.md instructions for idle projects?"
    )
    assert any(
        ".butler/BUTLER.md" in str(item["text"])
        and "15 minutes" in str(item["text"])
        for item in matches
    )


def test_packaged_product_guidance_syncs_to_chroma_by_source_digest(tmp_path):
    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    first = index.sync_product_knowledge()
    assert first["updated"] is True
    assert index._product_collection.count() == first["passage_count"]
    assert index._product_collection.metadata["source_digest"] == first["source_digest"]
    assert index.sync_product_knowledge()["updated"] is False
    index._product_collection.modify(metadata={"source_digest": "stale"})
    replaced = index.sync_product_knowledge()
    assert replaced["updated"] is True
    assert index._product_collection.metadata["source_digest"] == first["source_digest"]
