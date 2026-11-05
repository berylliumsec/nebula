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


def test_product_search_finds_core_performance_telemetry(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Core performance bottlenecks latency SQL worker queue event loop"
    )
    content = "\n".join(str(item["text"]) for item in matches)
    assert "diagnostics/performance" in content
    assert "performance.json" in content


def test_product_search_finds_headless_core_and_paired_butler_cli(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    for question, expected in (
        ("How can I chat with Butler from a paired CLI terminal?", "nebula-chat"),
        ("Run Nebula Core headless without a desktop or browser", "headless"),
        ("Create a terminal pairing invitation on the Core host", "invite"),
        (
            "Use a lightweight Python terminal client for remote agent conversations",
            "nebula-client",
        ),
        ("Choose skills and goals from the terminal Assistant shell", "shell"),
    ):
        matches = search_product_knowledge(question)
        assert any(expected in str(item["text"]) for item in matches), question


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
        "Butler can save the main runtime" in str(item["text"])
        and "provider or harness child runtime independently on the same chat"
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
        and "pin or flag one chat" in str(item["text"])
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


def test_product_search_explains_core_goal_capacity_recovery(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Does Core retry a harness goal when the model is overloaded?"
    )
    assert any(
        "Recover a harness goal from model capacity" in str(item["text"])
        and "policy refusal" in str(item["text"])
        for item in matches
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
    relink = search_product_knowledge(
        "Can Butler relink an existing project to another host folder?"
    )
    assert any(
        "nebula.projects.relink" in str(item["text"])
        and "existing project" in str(item["text"])
        for item in relink
    )


def test_product_search_finds_butler_scoped_browser_mission(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler start a scoped browser automation mission for my project?"
    )
    assert any(
        "nebula.work.browser_sessions" in str(item["text"])
        and "nebula.work.start_mission" in str(item["text"])
        for item in matches
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


def test_product_search_finds_butler_storage_token_handoff(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler rotate or revoke a storage source token on this device?"
    )
    assert any(
        "storage" in str(item["text"]).lower()
        and "requesting browser" in str(item["text"]).lower()
        and "token" in str(item["text"]).lower()
        for item in matches
    )
    configuration = search_product_knowledge(
        "Can Butler change a storage source reporting interval and edit its callback credential?"
    )
    assert any(
        "reporting" in str(item["text"]).lower()
        and "callback" in str(item["text"]).lower()
        and "requesting device" in str(item["text"]).lower()
        for item in configuration
    )


def test_product_search_finds_butler_device_pairing_boundary(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How can Butler pair a phone and revoke a paired device without seeing the pairing link?"
    )
    assert any(
        "paired" in str(item["text"]).lower()
        and "browser" in str(item["text"]).lower()
        and "pairing link" in str(item["text"]).lower()
        for item in matches
    )


def test_product_search_finds_butler_write_only_credentials(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler create a write-only credential without seeing the secret?"
    )
    assert any(
        "write-only" in str(item["text"]).lower()
        and "browser" in str(item["text"]).lower()
        for item in matches
    )


def test_product_search_finds_private_typesafe_and_vpn_handoffs(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How can Butler open private TypeSafe, VPN and MCP configuration import?"
    )
    assert any(
        all(
            word in " ".join(str(item["text"]).lower().split())
            for word in ("typesafe", "vpn", "mcp", "requesting browser", "private")
        )
        for item in matches
    )


def test_product_search_finds_butler_provider_credential_attachment(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler attach a saved credential reference to an OpenRouter provider?"
    )
    assert any(
        "provider" in str(item["text"]).lower()
        and "credential" in str(item["text"]).lower()
        and "browser" in str(item["text"]).lower()
        and "revision" in str(item["text"]).lower()
        for item in matches
    )


def test_product_search_finds_butler_harness_credential_attachment(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler attach a credential reference to an Assistant harness?"
    )
    assert any(
        "harness" in str(item["text"]).lower()
        and "credential" in str(item["text"]).lower()
        and "browser" in str(item["text"]).lower()
        and "revision" in str(item["text"]).lower()
        for item in matches
    )


def test_product_search_finds_butler_mcp_credential_attachment(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler attach a saved credential to an MCP server HTTP header?"
    )
    assert any(
        "mcp" in str(item["text"]).lower()
        and "credential" in str(item["text"]).lower()
        and "browser" in str(item["text"]).lower()
        for item in matches
    )
    scoped = search_product_knowledge(
        "Can Butler export or probe an MCP profile owned by another project?"
    )
    assert any(
        "selected project" in str(item["text"]).lower()
        and "ownership" in str(item["text"]).lower()
        and "probe" in str(item["text"]).lower()
        for item in scoped
    )


def test_product_search_finds_butler_integration_profile_lifecycle(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler create rename or delete an integration provider profile at its current revision?"
    )
    assert any(
        "profile" in str(item["text"]).lower()
        and "revision" in str(item["text"]).lower()
        and "read" in str(item["text"]).lower()
        for item in matches
    )


def test_product_search_finds_butler_runtime_trust_boundaries(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Butler runner executable trust and SSH standing Allow approval metadata revision"
    )
    assert any(
        "standing" in str(item["text"]).lower()
        and "browser" in str(item["text"]).lower()
        and "revision" in str(item["text"]).lower()
        for item in matches
    )
    consent = search_product_knowledge(
        "Butler follow-up cloud consent backend identity body expected_revision requesting browser"
    )
    assert any(
        "consent" in str(item["text"]).lower()
        and "backend" in str(item["text"]).lower()
        and "revision" in str(item["text"]).lower()
        for item in consent
    )


def test_product_search_finds_butler_project_sources_lifecycle(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler add a project source URL, reindex it or remove it at its current revision?"
    )
    assert any(
        "nebula.sources" in str(item["text"]).lower()
        and "browser" in str(item["text"]).lower()
        and "revision" in str(item["text"]).lower()
        for item in matches
    )


def test_product_search_explains_butler_host_policy_consent(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler grant the first Host access acknowledgment through the generic project policy Settings API?"
    )
    assert any(
        "host" in str(item["text"]).lower()
        and "requesting" in str(item["text"]).lower()
        and "consent" in str(item["text"]).lower()
        for item in matches
    )


def test_product_search_finds_butler_project_notes_lifecycle(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler create edit or delete a project note linked to a report?"
    )
    assert any(
        "notes" in str(item["text"]).lower()
        and "revision" in str(item["text"]).lower()
        and "report" in str(item["text"]).lower()
        for item in matches
    )


def test_product_search_explains_manual_note_review_and_saved_provenance(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "manual execution note draft harness operator acceptance Project Notes"
    )
    text = " ".join(" ".join(str(item["text"]).split()) for item in matches)
    assert "manual draft remains ready" in text
    assert "explicit operator acceptance" in text
    assert "operator-accepted" in text
    assert "Butler's note catalog" in text


def test_product_search_finds_butler_diagnostics_export(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler export a diagnostics support ZIP without seeing log bytes?"
    )
    assert any(
        "diagnostics" in str(item["text"]).lower()
        and "ZIP" in str(item["text"])
        and "browser" in str(item["text"]).lower()
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


def test_product_search_finds_butler_report_note_transform_review(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler transform a linked report note with AI and save the reviewed section?"
    )
    text = "\n".join(str(item["text"]) for item in matches).lower()
    assert "nebula.reports.transform_note" in text
    assert "cloud consent" in text
    assert "review" in text


def test_product_search_finds_butler_evidence_file_boundary(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How can Butler upload project evidence from my device and download the original?"
    )
    text = "\n".join(str(item["text"]) for item in matches).lower()
    assert "nebula.evidence.upload" in text
    assert "nebula.evidence.download" in text
    assert "requesting-browser" in text
    assert "sha-256" in text


def test_product_search_finds_butler_workspace_file_guidance(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How can Butler edit and download a project workspace file?"
    )
    text = "\n".join(str(item["text"]) for item in matches)
    assert "nebula.workspace.list" in text
    assert "nebula.workspace.download_file" in text
    assert "file picker" in text


def test_product_search_finds_workspace_preservation_and_reset_boundary(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Can Butler preserve an exact workspace file as Evidence and reset the workspace?"
    )
    text = "\n".join(str(item["text"]) for item in matches)
    assert "nebula.workspace.preserve_evidence" in text
    assert "workspace_reset_status" in text
    assert "types the exact project name" in text


def test_product_search_finds_operator_owned_workflow_reviews(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Butler scope document import storage plan dispatch retry sensitive diagnostic remediation checkpoint rewind"
    )
    text = "\n".join(str(item["text"]) for item in matches)
    assert "private scope-document import" in text
    assert "source-plan approval" in text
    assert "sensitive detail and remediation" in text
    assert "destructive checkpoint rewind" in text
    assert "does not report" in text


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


def test_product_search_finds_fresh_butler_host_load_reads(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Butler fresh current host-load CPU memory sample age nebula.work.host_load"
    )
    assert any("nebula.work.host_load" in str(item["text"]) for item in matches)


def test_product_search_finds_per_host_cpu_budget_configuration(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "per host CPU budget automatic custom applied inactive host-cpu-budget"
    )
    assert any(
        "host-cpu-budget" in str(item["text"]) or "CPU budget" in str(item["text"])
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


def test_product_search_finds_butler_inventory_and_candidate_records(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How can Butler create inventory assets and edit candidate finding records without verification?"
    )
    assert any(
        "nebula.assets.create" in str(item["text"])
        and "nebula.findings.update" in str(item["text"])
        and "verification" in str(item["text"])
        for item in matches
    )


def test_product_search_finds_butler_saved_results_and_bookmarks(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How can Butler read saved Results JSON and set a message bookmark?"
    )
    text = "\n".join(str(item["text"]) for item in matches)
    assert "nebula.data.read_result" in text
    assert "nebula.data.delete_result" in text
    assert "nebula.data.set_bookmark" in text
    assert "4,096" in text
    assert "requesting" in text


def test_product_search_finds_butler_inspection_and_sensitive_original_boundary(
    monkeypatch,
):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "How can Butler inspect tool output, cancel an execution and download an original artifact?"
    )
    text = "\n".join(str(item["text"]) for item in matches)
    assert "nebula.inspect.output" in text
    assert "nebula.inspect.cancel_execution" in text
    assert "nebula.inspect.download_artifact" in text
    assert "redacted" in text
    assert "requesting browser" in text


def test_product_search_finds_butler_core_observations_and_guide_progress(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Butler Core observations resource search guide progress"
    )
    text = "\n".join(str(item["text"]) for item in matches)
    assert "nebula.data.system" in text
    assert "nebula.data.search_resources" in text
    assert "nebula.data.guides" in text


def test_product_search_finds_butler_fixed_setup_and_terminal_egress(monkeypatch):
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    matches = search_product_knowledge(
        "Butler fixed runtime setup image prepare retry cancellation egress"
    )
    text = "\n".join(str(item["text"]) for item in matches)
    assert "candidate_id" in text
    assert "operation_id" in text
    assert "terminal_public_ip" in text
