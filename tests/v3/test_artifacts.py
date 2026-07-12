import pytest

from nebula.v3.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    ArtifactStoreError,
)


def test_content_addressed_store_deduplicates_and_verifies(tmp_path):
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
