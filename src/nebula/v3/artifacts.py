"""Immutable, content-addressed SHA-256 artifact storage."""

from __future__ import annotations

from .diagnostics import record_caught_exception, record_diagnostic

import hashlib
import io
import mimetypes
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .domain import Artifact

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# ``mimetypes`` reports a compressed file as the type it wraps plus a separate
# encoding; the artifact is the compressed bytes, so the encoding wins.
_ENCODING_MEDIA_TYPES = {
    "gzip": "application/gzip",
    "bzip2": "application/x-bzip2",
    "xz": "application/x-xz",
    "compress": "application/x-compress",
    "br": "application/x-brotli",
}
# Common capture formats the platform tables leave unregistered.
_EXTENSION_MEDIA_TYPES = {
    ".log": "text/plain",
    ".har": "application/json",
    ".ndjson": "application/x-ndjson",
}


def infer_media_type(filename: str | None) -> str:
    """Infer a stored artifact's media type from its filename."""

    kind, encoding = mimetypes.guess_type(filename or "")
    if encoding is not None:
        return _ENCODING_MEDIA_TYPES.get(encoding, "application/octet-stream")
    if kind is None:
        suffix = os.path.splitext(filename or "")[1].casefold()
        kind = _EXTENSION_MEDIA_TYPES.get(suffix)
    return kind or "application/octet-stream"


class ArtifactStoreError(RuntimeError):
    pass


class ArtifactIntegrityError(ArtifactStoreError):
    pass


@dataclass(frozen=True)
class StoredArtifact:
    artifact: Artifact
    created_blob: bool


def _fsync_directory(path: Path) -> None:
    """Flush a directory so a freshly linked entry survives power loss."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ArtifactStore:
    """Store bytes once and address them by their verified SHA-256 digest."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._blob_root = self.root / "sha256"
        self._temporary_root = self.root / ".tmp"
        self._blob_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (self.root, self._blob_root, self._temporary_root):
            directory.chmod(0o700)

    def put_bytes(
        self,
        data: bytes,
        *,
        engagement_id: str,
        filename: str | None = None,
        media_type: str | None = None,
        source: str | None = None,
        parent_artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        return self.put_bytes_with_status(
            data,
            engagement_id=engagement_id,
            filename=filename,
            media_type=media_type,
            source=source,
            parent_artifact_id=parent_artifact_id,
            metadata=metadata,
        ).artifact

    def put_bytes_with_status(
        self,
        data: bytes,
        *,
        engagement_id: str,
        filename: str | None = None,
        media_type: str | None = None,
        source: str | None = None,
        parent_artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StoredArtifact:
        return self._put_stream(
            io.BytesIO(data),
            engagement_id=engagement_id,
            filename=filename,
            media_type=media_type,
            source=source,
            parent_artifact_id=parent_artifact_id,
            metadata=metadata,
        )

    def put_file(
        self,
        path: str | Path,
        *,
        engagement_id: str,
        filename: str | None = None,
        media_type: str | None = None,
        source: str | None = None,
        parent_artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        return self.put_file_with_status(
            path,
            engagement_id=engagement_id,
            filename=filename,
            media_type=media_type,
            source=source,
            parent_artifact_id=parent_artifact_id,
            metadata=metadata,
        ).artifact

    def put_file_with_status(
        self,
        path: str | Path,
        *,
        engagement_id: str,
        filename: str | None = None,
        media_type: str | None = None,
        source: str | None = None,
        parent_artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StoredArtifact:
        source_path = Path(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source_path, flags)
        except OSError as exc:
            record_caught_exception(
                "storage",
                "storage.artifacts.caught_failure_001",
                "A handled storage operation raised an exception.",
                exc,
                stage="artifacts",
            )
            raise FileNotFoundError(source_path) from exc
        metadata_result = os.fstat(descriptor)
        if not stat.S_ISREG(metadata_result.st_mode):
            os.close(descriptor)
            raise ArtifactStoreError(
                "artifact source must be a regular non-symlink file"
            )
        with os.fdopen(descriptor, "rb") as stream:
            return self._put_stream(
                stream,
                engagement_id=engagement_id,
                filename=filename or source_path.name,
                media_type=media_type,
                source=source or str(source_path),
                parent_artifact_id=parent_artifact_id,
                metadata=metadata,
            )

    def put_stream_with_status(
        self,
        stream: BinaryIO,
        *,
        engagement_id: str,
        filename: str | None = None,
        media_type: str | None = None,
        source: str | None = None,
        parent_artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StoredArtifact:
        """Copy an already-open, caller-validated stream into immutable storage."""

        return self._put_stream(
            stream,
            engagement_id=engagement_id,
            filename=filename,
            media_type=media_type,
            source=source,
            parent_artifact_id=parent_artifact_id,
            metadata=metadata,
        )

    def _put_stream(
        self,
        stream: BinaryIO,
        *,
        engagement_id: str,
        filename: str | None,
        media_type: str | None,
        source: str | None,
        parent_artifact_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> StoredArtifact:
        descriptor, temporary_name = tempfile.mkstemp(dir=self._temporary_root)
        temporary_path = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as destination:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())

            digest_value = digest.hexdigest()
            destination_path = self.path_for_digest(digest_value)
            shard_directory = destination_path.parent
            shard_existed = shard_directory.is_dir()
            shard_directory.mkdir(parents=True, exist_ok=True)
            created = False
            try:
                # Hard-linking within the same store atomically refuses to replace
                # an existing immutable digest in concurrent writers.
                os.link(temporary_path, destination_path)
                created = True
                destination_path.chmod(0o400)
            except FileExistsError:
                # The content-addressed destination can already exist during
                # ordinary deduplication or a concurrent immutable write. Only
                # classify the branch after the existing blob passes the same
                # size and digest checks as the incoming stream.
                if destination_path.stat().st_size != size:
                    raise ArtifactIntegrityError(
                        f"existing blob size does not match digest {digest_value}"
                    )
                existing_digest = hashlib.sha256()
                with destination_path.open("rb") as existing_stream:
                    while True:
                        chunk = existing_stream.read(1024 * 1024)
                        if not chunk:
                            break
                        existing_digest.update(chunk)
                if existing_digest.hexdigest() != digest_value:
                    raise ArtifactIntegrityError(
                        f"existing blob content does not match digest {digest_value}"
                    )
                record_diagnostic(
                    "debug",
                    "storage",
                    "storage.artifacts.blob_reused",
                    "An immutable artifact blob already existed and passed integrity verification.",
                    outcome="deduplicated",
                    stage="artifacts",
                    metadata={"byte_count": size},
                )
            if created:
                # The bytes were fsynced above, but the new directory entry (and
                # any shard directories created for it) must reach disk too, or
                # the Artifact row committed next can outlive its blob.
                _fsync_directory(shard_directory)
                if not shard_existed:
                    _fsync_directory(shard_directory.parent)
                    _fsync_directory(self._blob_root)

            relative_path = destination_path.relative_to(self.root).as_posix()
            artifact = Artifact(
                engagement_id=engagement_id,
                sha256=digest_value,
                size=size,
                filename=filename,
                media_type=media_type or infer_media_type(filename),
                storage_path=relative_path,
                source=source,
                parent_artifact_id=parent_artifact_id,
                metadata=metadata or {},
            )
            return StoredArtifact(artifact=artifact, created_blob=created)
        finally:
            temporary_path.unlink(missing_ok=True)

    def path_for_digest(self, digest: str) -> Path:
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError("digest must be a lowercase SHA-256 hex string")
        return self._blob_root / digest[:2] / digest[2:4] / digest

    def path_for(self, artifact: Artifact) -> Path:
        path = (self.root / artifact.storage_path).resolve()
        if self.root not in path.parents:
            raise ArtifactStoreError("artifact path escapes the artifact store")
        expected = self.path_for_digest(artifact.sha256)
        if path != expected:
            raise ArtifactIntegrityError("artifact path does not match its digest")
        return path

    def open(self, artifact: Artifact) -> BinaryIO:
        return self.path_for(artifact).open("rb")

    def read(self, artifact: Artifact) -> bytes:
        with self.open(artifact) as stream:
            return stream.read()

    def verify(self, artifact: Artifact) -> bool:
        try:
            path = self.path_for(artifact)
        except ArtifactStoreError as exc:
            # A row whose storage_path escapes the store or disagrees with its
            # digest is unverifiable; callers turn False into their own
            # structured integrity failure instead of an unexpected 500.
            record_caught_exception(
                "storage",
                "storage.artifacts.caught_failure_002",
                "An artifact row's storage path failed verification.",
                exc,
                stage="artifacts",
            )
            return False
        if not path.is_file() or path.stat().st_size != artifact.size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest() == artifact.sha256

    def iter_digests(self) -> Iterator[str]:
        for path in self._blob_root.glob("*/*/*"):
            if path.is_file() and _DIGEST_RE.fullmatch(path.name):
                yield path.name

    def discard_new_blob(self, stored: StoredArtifact) -> None:
        """Retain a failed-write blob so concurrent committed references stay valid.

        Blob creation and relational commits cannot share one transaction. Removing
        a blob here can race another writer that already deduplicated the digest but
        has not committed its Artifact row yet. Unreferenced blobs are therefore
        retained for a future serialized garbage-collection pass.
        """

        del stored
