import hashlib
import json
import zipfile

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Advisory,
    AgentRun,
    ChatMessage,
    ChatRole,
    ChatSession,
    Correlation,
    ContextMemory,
    ContextOwnerType,
    ContextSnapshot,
    ContextSnapshotStatus,
    ContextSourceReference,
    Engagement,
    Evidence,
    OperatorProfile,
    ProviderProfile,
    SourceSnapshot,
)
from nebula.v3.exporter import ExportError, export_engagement
from nebula.v3.storage import NebulaStore


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def test_export_contains_entities_events_deduplicated_blobs_and_hash_manifest(
    tmp_path,
):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Exported engagement"))
    first = artifacts.put_bytes(
        b"immutable evidence",
        engagement_id=engagement.id,
        filename="proof.txt",
    )
    second = artifacts.put_bytes(
        b"immutable evidence",
        engagement_id=engagement.id,
        filename="copy.txt",
    )
    store.create(first)
    store.create(second)
    store.create(
        Evidence(
            engagement_id=engagement.id,
            evidence_type="command-output",
            title="Proof",
            artifact_id=first.id,
            sha256=first.sha256,
        )
    )
    run = store.create(
        AgentRun(engagement_id=engagement.id, objective="Export this run")
    )
    store.append_event(
        run.id,
        "run.started",
        {"objective": run.objective},
        idempotency_key="run:started",
    )
    destination = tmp_path / "engagement.nebula.zip"

    manifest = export_engagement(
        engagement_id=engagement.id,
        destination=destination,
        store=store,
        artifact_store=artifacts,
    )

    assert manifest.entity_counts["engagements"] == 1
    assert manifest.entity_counts["artifacts"] == 2
    assert manifest.entity_counts["evidence"] == 1
    assert manifest.entity_counts["runs"] == 1
    assert manifest.event_count == 1
    blob_name = f"blobs/sha256/{first.sha256}"
    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist().count(blob_name) == 1
        assert archive.read(blob_name) == b"immutable evidence"
        archived_manifest = json.loads(archive.read("manifest.json"))
        assert archived_manifest == manifest.model_dump(mode="json")
        for name, expected_digest in manifest.files.items():
            assert _sha256(archive.read(name)) == expected_digest
        archived_events = json.loads(archive.read("events.json"))
        assert archived_events[0]["run_id"] == run.id
        assert archived_events[0]["sequence"] == 1


def test_export_is_atomic_on_integrity_failure_and_refuses_overwrite(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Integrity failure"))
    artifact = artifacts.put_bytes(b"evidence", engagement_id=engagement.id)
    store.create(artifact)
    destination = tmp_path / "bundle.zip"
    artifacts.path_for(artifact).unlink()

    with pytest.raises(ExportError, match="integrity verification"):
        export_engagement(
            engagement_id=engagement.id,
            destination=destination,
            store=store,
            artifact_store=artifacts,
        )
    assert not destination.exists()

    destination.write_bytes(b"existing export")
    with pytest.raises(ExportError, match="destination already exists"):
        export_engagement(
            engagement_id=engagement.id,
            destination=destination,
            store=store,
            artifact_store=artifacts,
        )
    assert destination.read_bytes() == b"existing export"


def test_export_includes_successful_and_failed_context_snapshots(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Context export"))
    provider = store.create(
        ProviderProfile(name="Context provider", provider_type="openai")
    )
    session = store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Exported chat",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    message = store.create(
        ChatMessage(
            engagement_id=engagement.id,
            session_id=session.id,
            sequence=1,
            role=ChatRole.USER,
            content="Remember CVE-2026-4242.",
        )
    )
    snapshot = store.create(
        ContextSnapshot(
            engagement_id=engagement.id,
            owner_type=ContextOwnerType.CHAT_SESSION,
            owner_id=session.id,
            status=ContextSnapshotStatus.READY,
            compacted_through=1,
            memory=ContextMemory(summary="CVE context retained."),
            source_references=[
                ContextSourceReference(
                    source_kind="chat_message",
                    source_id=message.id,
                    sequence=message.sequence,
                )
            ],
            provider_profile_id=provider.id,
            model="model-a",
            prompt_version="nebula-context-v1",
            source_sha256="a" * 64,
        )
    )
    failed = store.create(
        ContextSnapshot(
            engagement_id=engagement.id,
            owner_type=ContextOwnerType.CHAT_SESSION,
            owner_id=session.id,
            version=2,
            status=ContextSnapshotStatus.FAILED,
            compacted_through=1,
            source_references=[
                ContextSourceReference(
                    source_kind="chat_message",
                    source_id=message.id,
                    sequence=message.sequence,
                )
            ],
            provider_profile_id=provider.id,
            model="model-a",
            prompt_version="nebula-context-v1",
            source_sha256="b" * 64,
            error="context compaction failed",
        )
    )
    destination = tmp_path / "context.nebula.zip"

    manifest = export_engagement(
        engagement_id=engagement.id,
        destination=destination,
        store=store,
        artifact_store=artifacts,
    )

    assert manifest.entity_counts["context_snapshots"] == 2
    with zipfile.ZipFile(destination) as archive:
        archived = json.loads(archive.read("entities/context_snapshots.json"))
    assert {item["id"] for item in archived} == {snapshot.id, failed.id}
    ready = next(item for item in archived if item["status"] == "ready")
    assert ready["memory"]["summary"] == "CVE context retained."


def test_export_includes_only_referenced_global_entities_and_system_blobs(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    operator = store.create(OperatorProfile(display_name="Export Owner"))
    store.create(OperatorProfile(display_name="Unrelated Operator"))
    provider = store.create(
        ProviderProfile(name="Export Provider", provider_type="openai")
    )
    store.create(ProviderProfile(name="Unrelated Provider", provider_type="openai"))
    engagement = store.create(
        Engagement(name="Referenced globals", owner_id=operator.id)
    )
    store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Use the configured provider",
            supervisor_provider_id=provider.id,
        )
    )
    system_artifact = artifacts.put_bytes(
        b"raw advisory feed",
        engagement_id="system:vulnerability-intelligence",
        filename="feed.json",
    )
    store.create(system_artifact)
    snapshot = store.create(
        SourceSnapshot(
            source="test-feed",
            sha256=system_artifact.sha256,
            artifact_id=system_artifact.id,
        )
    )
    advisory = store.create(
        Advisory(
            advisory_id="CVE-2099-0001",
            source="test-feed",
            title="Referenced advisory",
            source_snapshot_id=snapshot.id,
        )
    )
    store.create(
        Advisory(
            advisory_id="CVE-2099-9999",
            source="test-feed",
            title="Unrelated advisory",
        )
    )
    store.create(
        Correlation(
            engagement_id=engagement.id,
            advisory_id=advisory.advisory_id,
            method="purl",
            confidence=1,
            rationale="Exact package match",
            analyst_id=operator.id,
        )
    )
    destination = tmp_path / "globals.zip"

    manifest = export_engagement(
        engagement_id=engagement.id,
        destination=destination,
        store=store,
        artifact_store=artifacts,
    )

    assert manifest.entity_counts["operator_profiles"] == 1
    assert manifest.entity_counts["providers"] == 1
    assert manifest.entity_counts["advisories"] == 1
    assert manifest.entity_counts["source_snapshots"] == 1
    assert manifest.entity_counts["artifacts"] == 1
    with zipfile.ZipFile(destination) as archive:
        assert [
            item["id"]
            for item in json.loads(archive.read("entities/operator_profiles.json"))
        ] == [operator.id]
        assert [
            item["id"] for item in json.loads(archive.read("entities/providers.json"))
        ] == [provider.id]
        assert [
            item["id"] for item in json.loads(archive.read("entities/advisories.json"))
        ] == [advisory.id]
        assert archive.read(f"blobs/sha256/{system_artifact.sha256}") == (
            b"raw advisory feed"
        )


def test_export_global_advisory_lookup_is_paginated(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Paginated intelligence"))
    store.create_many(
        [
            Advisory(
                advisory_id=f"CVE-2098-{index:04d}",
                source="unrelated",
                title=f"Unrelated advisory {index}",
            )
            for index in range(1_001)
        ]
    )
    referenced = store.create(
        Advisory(
            advisory_id="CVE-2099-4242",
            source="last-page",
            title="Referenced advisory",
        )
    )
    store.create(
        Correlation(
            engagement_id=engagement.id,
            advisory_id=referenced.advisory_id,
            method="purl",
            confidence=1,
            rationale="Exact match",
        )
    )
    destination = tmp_path / "paginated.zip"

    manifest = export_engagement(
        engagement_id=engagement.id,
        destination=destination,
        store=store,
        artifact_store=artifacts,
    )

    assert manifest.entity_counts["advisories"] == 1
    with zipfile.ZipFile(destination) as archive:
        archived = json.loads(archive.read("entities/advisories.json"))
    assert [item["id"] for item in archived] == [referenced.id]


def test_export_rejects_missing_required_global_reference_without_output(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Dangling provider"))
    store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Cannot be made portable",
            supervisor_provider_id="missing-provider",
        )
    )
    destination = tmp_path / "dangling.zip"

    with pytest.raises(ExportError, match="missing providers entity"):
        export_engagement(
            engagement_id=engagement.id,
            destination=destination,
            store=store,
            artifact_store=artifacts,
        )

    assert not destination.exists()
