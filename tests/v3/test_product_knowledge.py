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


def test_product_passages_keep_short_sentences_at_chunk_boundaries(
    tmp_path, monkeypatch
):
    from scripts import build_product_knowledge as builder

    monkeypatch.setattr(builder, "ROOT", tmp_path)
    source = tmp_path / "guide.md"
    sentence = "Butler can save a different main harness and provider subagent on the same chat."
    source.write_text(
        "# Assistant\n\n" + "Previous instruction. " * 62 + sentence,
        encoding="utf-8",
    )

    entries = builder.passages([source])
    assert len(entries) == 2
    assert any(sentence in text for _, _, _, text in entries)
    assert all(
        len(text) <= builder.MAX_CHARS + len("Assistant\n\n")
        for _, _, _, text in entries
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


def test_product_search_explains_mcp_directory_and_full_schema_cost(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "MCP tool directory on demand checked servers always loaded full schemas"
    )
    assert any(
        "compact" in str(item["text"]).lower()
        and "full" in str(item["text"]).lower()
        and "schema" in str(item["text"]).lower()
        for item in matches
    )


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
        ".butler/BUTLER.md" in str(item["text"]) and "five minutes" in str(item["text"])
        for item in matches
    )


def test_product_search_finds_direct_butler_editor_navigation(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How do I edit Butler instructions from the Settings popup?"
    )
    assert any(
        "butler" in str(item["text"]).lower()
        and "code editor" in str(item["text"]).lower()
        and "settings popup" in str(item["text"]).lower()
        for item in matches
    )


def test_product_search_explains_butler_conversation_grouping(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Where are chats Butler creates in Workbench conversations?"
    )
    assert any(
        "chats Butler creates appear below Butler" in str(item["text"])
        for item in matches
    )


def test_product_search_finds_chat_planning_guidance(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Where is Ask for a plan first and can Butler request one?"
    )
    assert any(
        item["heading"] == "Assistant harnesses"
        and "ask for a plan" in str(item["text"]).lower()
        and "/settings#harness-settings" in str(item["text"])
        for item in matches
    )
    assert any(
        "planning_enabled" in str(item["text"])
        or "Assistant settings" in str(item["text"])
        for item in matches
    )
    codex_matches = search_product_knowledge(
        "Can providers, Codex and Grok share an advisory plan without blocking tools and give it to subagents?"
    )
    assert any(
        "Provider, Codex, and Grok agents" in str(item["text"])
        and "normal tools remain available" in str(item["text"])
        for item in codex_matches
    )
    grok_matches = search_product_knowledge(
        "Can Butler switch the main agent to Grok while keeping advisory planning and a provider subagent?"
    )
    assert any(
        "Butler can save a different main harness and provider subagent"
        in str(item["text"])
        for item in grok_matches
    )


def test_product_search_finds_butler_goal_handoff(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How does Butler hand off a paused harness planning goal to DeepSeek?"
    )
    assert any("nebula.global.handoff_goal" in str(item["text"]) for item in matches)


def test_product_search_explains_pins_flags_and_butler_archive(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Where are pinned flagged chats and archived Butler chats?"
    )
    assert any(
        "Butler's own collapsed Archived section" in str(item["text"])
        and "pin a chat" in str(item["text"])
        for item in matches
    )


def test_product_search_explains_bulk_chat_actions(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How do I select multiple chats and bulk archive or delete them?"
    )
    assert any(
        "Select conversations loads the full project chat list" in str(item["text"])
        and "Delete permanently" in str(item["text"])
        for item in matches
    )


def test_product_search_finds_goal_resume_instruction_editor(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge("Where do I edit a goal resume instruction?")
    assert any(
        "Edit a goal's resume instruction" in str(item["text"]) for item in matches
    )


def test_product_search_finds_butler_work_agent_access_and_history(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    access = search_product_knowledge(
        "Can Butler disable Work agent tools for a project?"
    )
    assert any("nebula.work.set_work_setting" in str(item["text"]) for item in access)
    history = search_product_knowledge(
        "How does Butler read older Work check-in history?"
    )
    assert any("nebula.work.work_updates" in str(item["text"]) for item in history)


def test_product_search_finds_butler_chat_rewind_guidance(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler rewind an older chat message and send a replacement?"
    )
    assert any(
        "nebula.work.chat_messages" in str(item["text"])
        and "nebula.work.rewind_chat" in str(item["text"])
        for item in matches
    )


def test_product_search_finds_butler_notebook_guidance(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How does Butler edit a project Notebook cell and check Host consent?"
    )
    assert any(
        "nebula.notebook" in str(item["text"]) and "Host consent" in str(item["text"])
        for item in matches
    )
    runs = search_product_knowledge(
        "How does Butler execute an exact Notebook cell, check uncertain runs and interrupt it?"
    )
    assert any(
        "nebula.notebook.execute" in str(item["text"])
        and "nebula.notebook.recent_runs" in str(item["text"])
        and "nebula.notebook.interrupt" in str(item["text"])
        for item in runs
    )
    resources = search_product_knowledge(
        "Can Butler install an exact Notebook package and restart the current kernel?"
    )
    assert any(
        "installed_package" in str(item["text"])
        and "kernel_status" in str(item["text"])
        and "name==version" in str(item["text"])
        for item in resources
    )
    file_index = search_product_knowledge(
        "Can Butler connect and remove the project's Notebook file index without seeing the host path?"
    )
    assert any(
        "file_index_status" in str(item["text"])
        and "requesting browser" in str(item["text"])
        for item in file_index
    )


def test_product_search_finds_butler_project_lifecycle_and_confirmation(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler archive restore or delete a project with confirmation?"
    )
    assert any(
        "nebula.projects.list" in str(item["text"])
        and "confirmations" in str(item["text"])
        for item in matches
    )
    retry = search_product_knowledge("Butler create receipt retry ID")
    assert any(
        "request fingerprint" in str(item["text"])
        and "same transaction" in str(item["text"])
        for item in retry
    )
    linked = search_product_knowledge(
        "Can Butler create a linked project using a Nebula host folder?"
    )
    assert any(
        "nebula.projects.create_linked" in str(item["text"])
        and "host folder picker" in str(item["text"])
        for item in linked
    )


def test_product_search_finds_butler_device_transcript_export(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How does Butler export a chat transcript on my browser?"
    )
    assert any(
        "nebula.chats.export" in str(item["text"])
        and "requesting browser" in str(item["text"])
        for item in matches
    )


def test_product_search_finds_butler_report_drafts_and_operator_signoff(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler create a report draft and sign off or export its PDF?"
    )
    text = "\n".join(str(item["text"]) for item in matches)
    assert "nebula.reports" in text
    assert "operator" in text.lower()
    assert "requesting" in text.lower()


def test_product_search_finds_butler_terminal_stop_boundary(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler list terminal sessions and stop a project terminal?"
    )
    text = "\n".join(str(item["text"]) for item in matches)
    assert "nebula.terminal.list" in text
    assert "nebula.terminal.stop" in text
    assert "WebSocket ticket" in text
    assert "requesting browser" in text


def test_product_search_finds_butler_report_deletion_boundary(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler delete a draft report and what if it has a saved PDF render?"
    )
    text = "\n".join(str(item["text"]) for item in matches).lower()
    assert "nebula.reports.delete" in text
    assert "render" in text
    assert "signed" in text


def test_product_search_finds_butler_workspace_file_guidance(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How can Butler edit and download a project workspace file?"
    )
    text = "\n".join(str(item["text"]) for item in matches)
    assert "nebula.workspace.list" in text
    assert "nebula.workspace.download_file" in text
    assert "file picker" in text


def test_product_search_finds_butler_device_upload_and_folder_guidance(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler upload a binary workspace file and create or delete a folder?"
    )
    assert any(
        "nebula.workspace.upload_from_device" in str(item["text"])
        and "nebula.workspace.create_directory" in str(item["text"])
        and "directory identities" in str(item["text"])
        for item in matches
    )


def test_product_search_finds_butler_shared_library_confirmation(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How can Butler reindex or remove a shared Library item?"
    )
    assert any(
        "shared Library" in str(item["text"])
        and "revision" in str(item["text"])
        and "confirmation" in str(item["text"])
        for item in matches
    )
    handoffs = search_product_knowledge(
        "How does Butler add a file from my device or download the shared Library original?"
    )
    assert any(
        "shared Library" in str(item["text"])
        and "picker" in str(item["text"])
        and "download" in str(item["text"])
        for item in handoffs
    )


def test_product_search_finds_butler_effort_setting(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Where do I set Butler to max reasoning effort for scheduled checks?"
    )
    assert any(item["heading"] == "Butler model and effort" for item in matches)


def test_product_search_explains_butler_goal_budget_and_assistant_authority(
    monkeypatch,
):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Butler extend paused goal time step budget resume"
    )
    assert any(
        "Increasing an exhausted budget does not itself resume a goal"
        in str(item["text"])
        and "every saved next-turn Assistant setting" in str(item["text"])
        for item in matches
    )


def test_product_search_finds_scheduled_history_and_grok_quota(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    history = search_product_knowledge(
        "Where can I read Butler scheduled check transcript and linked recovery goal reports?"
    )
    quota = search_product_knowledge("Where do I see Grok remaining quota?")
    credits = search_product_knowledge(
        "Can I read Grok prepaid credits or Codex credits after included quota runs out?"
    )
    assert any(item["heading"] == "Butler scheduled activity" for item in history)
    assert any(item["heading"] == "Usage and credits" for item in quota)
    assert any(item["heading"] == "Usage and credits" for item in credits)


def test_product_search_explains_blocked_checkin_dialogue(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Does Butler wait for a blocked agent reply in the same check-in?"
    )
    assert any(
        "exact saved reply" in str(item["text"])
        and "next scheduled check" in str(item["text"])
        for item in matches
    )


def test_product_search_finds_butler_check_interval_setting(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Where do I change Butler check frequency from the five minute default?"
    )
    assert any(item["heading"] == "Butler check interval" for item in matches)


def test_product_search_explains_harness_to_provider_chat_switch(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "switch saved harness conversation to provider same chat idle goal"
    )
    assert any(
        "provider" in str(item["text"]).lower()
        and "same chat" in str(item["text"]).lower()
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
