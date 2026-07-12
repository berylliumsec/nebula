import hashlib
import json
import sqlite3

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import Artifact, Asset, Engagement, Observation
from nebula.v3.importer import LegacyEngagementImporter
from nebula.v3.storage import NebulaStore, StoreTransaction


def _make_legacy_engagement(root):
    root.mkdir()
    details = {
        "engagement_name": "Legacy Acme",
        "ip_addresses": ["10.0.0.5", "10.0.1.0/24"],
        "urls": ["https://app.example.com/login"],
        "lookout_items": ["passwords"],
        "model": "legacy-model",
        "ollama_url": "http://127.0.0.1:11434",
        "chromadb_dir": str(root),
    }
    (root / "engagement_details.json").write_text(json.dumps(details))
    (root / "config.json").write_text(json.dumps({"SELECTED_TOOLS": ["nmap", "nikto"]}))
    (root / "history.txt").write_text("nmap -sV 10.0.0.5\n")
    (root / "command_output").mkdir()
    (root / "command_output" / "nmap.txt").write_text("80/tcp open http")
    (root / "screenshots").mkdir()
    (root / "screenshots" / "proof.png").write_bytes(b"\x89PNG\r\nproof")
    (root / "suggestions_notes").mkdir()
    (root / "suggestions_notes" / "ai_notes.html").write_text(
        "<p>Investigate the HTTP service</p>"
    )
    connection = sqlite3.connect(root / "chroma.sqlite3")
    connection.execute(
        "CREATE TABLE embedding_metadata "
        "(id INTEGER, key TEXT, string_value TEXT, int_value INTEGER, float_value REAL, bool_value INTEGER)"
    )
    connection.execute(
        "INSERT INTO embedding_metadata(id, key, string_value) VALUES (1, 'chroma:document', 'legacy knowledge')"
    )
    connection.execute(
        "INSERT INTO embedding_metadata(id, key, string_value) VALUES (1, 'source', 'notes.txt')"
    )
    connection.commit()
    connection.close()


def _checksums(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_import_is_side_by_side_typed_and_source_preserving(tmp_path):
    source = tmp_path / "legacy"
    _make_legacy_engagement(source)
    before = _checksums(source)
    store = NebulaStore(tmp_path / "v3.db")
    artifacts = ArtifactStore(tmp_path / "artifact-store")

    report = LegacyEngagementImporter(store, artifacts).import_engagement(source)

    assert report.status == "completed"
    assert report.source_unchanged is True
    assert _checksums(source) == before
    assert report.source_file_checksums == before
    assert report.imported_counts["engagements"] == 1
    assert report.imported_counts["assets"] == 3
    assert report.imported_counts["evidence"] == 4
    assert report.imported_counts["tool_selections"] == 2
    assert report.imported_counts["chroma_documents"] == 1

    engagement = store.get(Engagement, report.target_engagement_id)
    assert engagement.name == "Legacy Acme"
    assert engagement.metadata["legacy"]["selected_tools"] == ["nmap", "nikto"]
    assert len(store.list_entities(Asset, engagement_id=engagement.id)) == 3
    observations = store.list_entities(Observation, engagement_id=engagement.id)
    assert any(item.body == "legacy knowledge" for item in observations)
    assert any("Investigate the HTTP service" in item.body for item in observations)
    for artifact in store.list_entities(Artifact, engagement_id=engagement.id):
        assert artifacts.verify(artifact)


def test_invalid_legacy_data_rolls_back_and_reports_failure(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "engagement_details.json").write_text("not-json")
    before = _checksums(source)
    store = NebulaStore(tmp_path / "v3.db")
    artifacts = ArtifactStore(tmp_path / "artifact-store")

    report = LegacyEngagementImporter(store, artifacts).import_engagement(source)

    assert report.status == "failed"
    assert report.errors
    assert report.source_unchanged is True
    assert _checksums(source) == before
    assert store.count(Engagement) == 0
    assert list(artifacts.iter_digests()) == []


def test_database_failure_compensates_new_artifacts(tmp_path, monkeypatch):
    source = tmp_path / "legacy"
    _make_legacy_engagement(source)
    store = NebulaStore(tmp_path / "v3.db")
    artifacts = ArtifactStore(tmp_path / "artifact-store")

    def fail_commit(self, entities):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(StoreTransaction, "add_all", fail_commit)
    report = LegacyEngagementImporter(store, artifacts).import_engagement(source)

    assert report.status == "failed"
    assert "simulated database failure" in report.errors[0]
    assert report.source_unchanged is True
    assert store.count(Engagement) == 0
    assert list(artifacts.iter_digests()) == []


def test_importer_refuses_to_place_destination_inside_source(tmp_path):
    source = tmp_path / "legacy"
    _make_legacy_engagement(source)
    store = NebulaStore(tmp_path / "v3.db")
    artifacts = ArtifactStore(source / "v3-artifacts")
    report = LegacyEngagementImporter(store, artifacts).import_engagement(source)
    assert report.status == "failed"
    assert "outside the source engagement" in report.errors[0]
