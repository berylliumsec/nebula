import pytest

from nebula.v3.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    ArtifactStoreError,
)


def test_content_addressed_store_deduplicates_and_verifies(tmp_path, monkeypatch):
    diagnostics = []
    monkeypatch.setattr(
        "nebula.v3.artifacts.record_diagnostic",
        lambda *args, **kwargs: diagnostics.append((args, kwargs)),
    )
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.put_bytes_with_status(
        b"immutable evidence",
        engagement_id="eng-1",
        filename="proof.txt",
        media_type="text/plain",
    )
    second = store.put_bytes_with_status(
        b"immutable evidence",
        engagement_id="eng-1",
        filename="same-content.txt",
    )
    assert first.artifact.sha256 == second.artifact.sha256
    assert first.created_blob is True
    assert second.created_blob is False
    assert list(store.iter_digests()) == [first.artifact.sha256]
    assert store.read(first.artifact) == b"immutable evidence"
    assert store.verify(first.artifact) is True
    assert oct(store.path_for(first.artifact).stat().st_mode & 0o777) == "0o400"
    assert diagnostics == [
        (
            (
                "debug",
                "storage",
                "storage.artifacts.blob_reused",
                "An immutable artifact blob already existed and passed integrity verification.",
            ),
            {
                "outcome": "deduplicated",
                "stage": "artifacts",
                "metadata": {"byte_count": len(b"immutable evidence")},
            },
        )
    ]


def test_integrity_verification_detects_tampering(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_bytes(b"before", engagement_id="eng-1")
    path = store.path_for(artifact)
    path.chmod(0o644)
    path.write_bytes(b"after!")
    assert store.verify(artifact) is False
    with pytest.raises(ArtifactIntegrityError, match="does not match digest"):
        store.put_bytes(b"before", engagement_id="eng-1")


def test_failed_write_cleanup_never_deletes_a_potentially_shared_blob(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    new = store.put_bytes_with_status(b"new", engagement_id="eng-1")
    duplicate = store.put_bytes_with_status(b"new", engagement_id="eng-1")
    store.discard_new_blob(duplicate)
    assert store.path_for(new.artifact).exists()
    store.discard_new_blob(new)
    assert store.path_for(new.artifact).exists()


def test_artifact_path_must_match_digest(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_bytes(b"safe", engagement_id="eng-1")
    forged = artifact.model_copy(update={"storage_path": "../outside"})
    with pytest.raises(ArtifactStoreError):
        store.path_for(forged)


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [
        ("log.txt.gz", "application/gzip"),
        ("scan.json.gz", "application/gzip"),
        ("capture.tar.bz2", "application/x-bzip2"),
        ("scan.log", "text/plain"),
        ("session.har", "application/json"),
        ("events.ndjson", "application/x-ndjson"),
        ("report.json", "application/json"),
        ("blob.bin", "application/octet-stream"),
    ],
)
def test_media_type_inference_keeps_the_compression_encoding(
    tmp_path, filename, media_type
):
    store = ArtifactStore(tmp_path / "artifacts")

    stored = store.put_bytes_with_status(
        b"payload", engagement_id="eng-1", filename=filename, source="test"
    )

    # A gzip member must never be served or searched as the text it wraps.
    assert stored.artifact.media_type == media_type


def test_explicit_media_type_wins_over_filename_inference(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")

    stored = store.put_bytes_with_status(
        b"payload",
        engagement_id="eng-1",
        filename="log.txt.gz",
        media_type="application/x-custom",
        source="test",
    )

    assert stored.artifact.media_type == "application/x-custom"


def test_blob_write_fsyncs_the_shard_directory_after_the_file(tmp_path, monkeypatch):
    import os
    import stat

    synced: list[tuple[bool, int]] = []
    original_fsync = os.fsync

    def recording_fsync(descriptor):
        info = os.fstat(descriptor)
        synced.append((stat.S_ISDIR(info.st_mode), info.st_ino))
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_bytes(b"durable evidence", engagement_id="eng-1")

    shard_directory = store.path_for(artifact).parent
    directory_syncs = [index for index, (is_dir, _) in enumerate(synced) if is_dir]
    file_syncs = [index for index, (is_dir, _) in enumerate(synced) if not is_dir]
    assert file_syncs, "the blob file itself must be fsynced"
    assert (True, shard_directory.stat().st_ino) in synced
    assert min(directory_syncs) > min(file_syncs)


def test_verify_reports_a_corrupt_storage_path_as_unverified(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_bytes(b"verified", engagement_id="eng-1")
    other = store.put_bytes(b"different", engagement_id="eng-1")

    escaped = artifact.model_copy(update={"storage_path": "../outside"})
    mismatched = artifact.model_copy(update={"storage_path": other.storage_path})

    assert store.verify(escaped) is False
    assert store.verify(mismatched) is False
    assert store.verify(artifact) is True
