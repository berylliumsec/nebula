import hashlib
import json
import sqlite3

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import Artifact, Asset, Engagement, Observation, ScopePolicy
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
    assert "tool_selections" not in report.imported_counts
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
    assert list(artifacts.iter_digests())


def test_importer_refuses_to_place_destination_inside_source(tmp_path):
    source = tmp_path / "legacy"
    _make_legacy_engagement(source)
    store = NebulaStore(tmp_path / "v3.db")
    artifacts = ArtifactStore(source / "v3-artifacts")
    report = LegacyEngagementImporter(store, artifacts).import_engagement(source)
    assert report.status == "failed"
    assert "outside the source engagement" in report.errors[0]


def _write_details(root, details):
    root.mkdir()
    (root / "engagement_details.json").write_text(json.dumps(details))


def test_legacy_targets_expand_ranges_and_skip_unusable_values_with_warnings(
    tmp_path,
):
    source = tmp_path / "legacy"
    _write_details(
        source,
        {
            "engagement_name": "Ranges",
            "ip_addresses": [
                "10.0.0.1-10.0.0.4",
                "10.0.0.5:8080",
                "10.0.0.0/24 (DMZ)",
                "app.example.com",
            ],
        },
    )
    store = NebulaStore(tmp_path / "v3.db")
    artifacts = ArtifactStore(tmp_path / "artifact-store")

    report = LegacyEngagementImporter(store, artifacts).import_engagement(source)

    assert report.status == "completed", report.errors
    engagement = store.get(Engagement, report.target_engagement_id)
    scope = store.get(ScopePolicy, engagement.scope_policy_id)
    assert scope.allowed_cidrs == ["10.0.0.1/32", "10.0.0.2/31", "10.0.0.4/32"]
    assert scope.allowed_domains == ["app.example.com"]
    assert any("10.0.0.5:8080" in warning for warning in report.warnings)
    assert any("10.0.0.0/24 (DMZ)" in warning for warning in report.warnings)
    assets = store.list_entities(Asset, engagement_id=engagement.id)
    assert sorted(item.address for item in assets if item.address) == [
        "10.0.0.1/32",
        "10.0.0.2/31",
        "10.0.0.4/32",
    ]
    assert [item.hostname for item in assets if item.asset_type == "domain"] == [
        "app.example.com"
    ]
    assert report.imported_counts["assets"] == 4


def test_legacy_ipv6_networks_are_typed_by_prefix_length(tmp_path):
    source = tmp_path / "legacy"
    _write_details(
        source,
        {
            "engagement_name": "IPv6",
            "ip_addresses": ["2001:db8::/32", "2001:db8::1", "10.0.0.5"],
        },
    )
    store = NebulaStore(tmp_path / "v3.db")
    artifacts = ArtifactStore(tmp_path / "artifact-store")

    report = LegacyEngagementImporter(store, artifacts).import_engagement(source)

    assert report.status == "completed", report.errors
    assets = store.list_entities(Asset, engagement_id=report.target_engagement_id)
    assert {item.address: item.asset_type for item in assets} == {
        "2001:db8::/32": "network",
        "2001:db8::1/128": "host",
        "10.0.0.5/32": "host",
    }


def test_legacy_receipt_counts_observations_and_omits_fabricated_entries(tmp_path):
    source = tmp_path / "legacy"
    _make_legacy_engagement(source)
    store = NebulaStore(tmp_path / "v3.db")
    artifacts = ArtifactStore(tmp_path / "artifact-store")

    report = LegacyEngagementImporter(store, artifacts).import_engagement(source)

    assert report.status == "completed", report.errors
    observations = store.list_entities(
        Observation, engagement_id=report.target_engagement_id
    )
    assert len(observations) == 3
    assert report.imported_counts["observations"] == 3
    assert report.imported_counts["chroma_documents"] == 1
    assert "tool_selections" not in report.imported_counts


def test_legacy_external_chroma_refusal_warns_once_without_a_sentinel_path(
    tmp_path,
):
    external = tmp_path / "external-chroma"
    external.mkdir()
    source = tmp_path / "legacy"
    _write_details(
        source,
        {
            "engagement_name": "External knowledge",
            "ip_addresses": ["10.0.0.5"],
            "chromadb_dir": str(external),
        },
    )
    store = NebulaStore(tmp_path / "v3.db")
    artifacts = ArtifactStore(tmp_path / "artifact-store")

    report = LegacyEngagementImporter(store, artifacts).import_engagement(source)

    assert report.status == "completed", report.errors
    assert report.warnings == [
        "external Chroma path was not imported without explicit approval"
    ]
    assert "knowledge" not in report.imported_counts
    assert "chroma_documents" not in report.imported_counts
