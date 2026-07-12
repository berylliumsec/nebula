import hashlib
import json
import zipfile

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import AgentRun, Engagement, Evidence
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
