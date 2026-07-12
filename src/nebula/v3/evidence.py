"""Safe, artifact-backed operator evidence ingestion for Nebula 3."""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any
from uuid import uuid4

from pydantic import Field, field_validator

from .artifacts import ArtifactStore
from .domain import Asset, Engagement, Evidence, Finding, NebulaModel
from .storage import NebulaStore

MAX_EVIDENCE_BYTES = 25 * 1024 * 1024
MAX_EVIDENCE_METADATA_BYTES = 16 * 1024
EVIDENCE_UPLOAD_VERSION = "nebula.evidence-upload.v1"

_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_EVIDENCE_TYPE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


class EvidenceUploadError(ValueError):
    """Base class for a safe, operator-visible upload failure."""


class InvalidEvidenceUploadError(EvidenceUploadError):
    """The upload envelope or content is malformed."""


class EvidenceTooLargeError(EvidenceUploadError):
    """The evidence content exceeds the bounded upload limit."""


class EvidenceReferenceError(EvidenceUploadError):
    """A linked entity does not belong to the evidence engagement."""


class EvidenceUploadRequest(NebulaModel):
    engagement_id: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=1024)
    title: str = Field(min_length=1, max_length=500)
    evidence_type: str = Field(min_length=1, max_length=100)
    content_base64: str = Field(
        max_length=4 * ((MAX_EVIDENCE_BYTES + 2) // 3),
    )
    media_type: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=20_000)
    source: str = Field(default="operator-upload", min_length=1, max_length=500)
    finding_id: str | None = Field(default=None, max_length=200)
    asset_ids: list[str] = Field(default_factory=list, max_length=500)
    captured_by: str | None = Field(default=None, max_length=300)
    source_version: str | None = Field(default=None, max_length=300)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("filename")
    @classmethod
    def safe_display_filename(cls, value: str) -> str:
        name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or name in {".", ".."}:
            raise ValueError("a valid evidence filename is required")
        if len(name) > 255:
            raise ValueError("evidence filename must be at most 255 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise ValueError("evidence filename cannot contain control characters")
        return name

    @field_validator("media_type")
    @classmethod
    def normalized_media_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.partition(";")[0].strip().lower()
        if not _MEDIA_TYPE.fullmatch(normalized):
            raise ValueError("media_type must be a valid MIME type")
        return normalized

    @field_validator("evidence_type")
    @classmethod
    def normalized_evidence_type(cls, value: str) -> str:
        normalized = value.casefold().replace(" ", "-")
        if not _EVIDENCE_TYPE.fullmatch(normalized):
            raise ValueError(
                "evidence_type may contain only letters, numbers, dots, dashes, and underscores"
            )
        return normalized

    @field_validator("asset_ids")
    @classmethod
    def unique_asset_ids(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 200 for value in values):
            raise ValueError("asset identifiers must contain 1 to 200 characters")
        return list(dict.fromkeys(values))

    @field_validator("source", "captured_by", "source_version")
    @classmethod
    def provenance_has_no_control_characters(cls, value: str | None) -> str | None:
        if value is not None and any(
            ord(character) < 32 and character not in {"\t"} for character in value
        ):
            raise ValueError("provenance fields cannot contain control characters")
        return value

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 100:
            raise ValueError("evidence metadata may contain at most 100 keys")
        try:
            encoded = json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence metadata must be valid JSON") from exc
        if len(encoded) > MAX_EVIDENCE_METADATA_BYTES:
            raise ValueError("evidence metadata exceeds the 16 KiB limit")
        return value

    def decoded_content(self) -> bytes:
        try:
            data = base64.b64decode(self.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidEvidenceUploadError(
                "content_base64 must be valid base64"
            ) from exc
        if not data:
            raise InvalidEvidenceUploadError("evidence content cannot be empty")
        if len(data) > MAX_EVIDENCE_BYTES:
            raise EvidenceTooLargeError(
                f"evidence exceeds the {MAX_EVIDENCE_BYTES // (1024 * 1024)} MiB limit"
            )
        return data


def upload_evidence(
    *,
    store: NebulaStore,
    artifact_store: ArtifactStore,
    request: EvidenceUploadRequest,
) -> Evidence:
    """Persist the immutable blob, Artifact, and Evidence as one logical upload."""

    store.get(Engagement, request.engagement_id)
    finding = _validate_links(store, request)
    data = request.decoded_content()
    evidence_id = str(uuid4())
    artifact_metadata: dict[str, Any] = {
        "evidence_id": evidence_id,
        "evidence_type": request.evidence_type,
        "upload_version": EVIDENCE_UPLOAD_VERSION,
    }
    if request.metadata:
        artifact_metadata["evidence_metadata"] = request.metadata
    stored = artifact_store.put_bytes_with_status(
        data,
        engagement_id=request.engagement_id,
        filename=request.filename,
        media_type=request.media_type,
        source=request.source,
        metadata=artifact_metadata,
    )
    evidence_metadata = dict(request.metadata)
    evidence_metadata.update(
        {
            "artifact_id": stored.artifact.id,
            "filename": stored.artifact.filename,
            "media_type": stored.artifact.media_type,
            "size": stored.artifact.size,
            "source": request.source,
            "upload_version": EVIDENCE_UPLOAD_VERSION,
        }
    )
    evidence = Evidence(
        id=evidence_id,
        engagement_id=request.engagement_id,
        evidence_type=request.evidence_type,
        title=request.title,
        description=request.description,
        artifact_id=stored.artifact.id,
        finding_id=request.finding_id,
        asset_ids=request.asset_ids,
        sha256=stored.artifact.sha256,
        captured_by=request.captured_by,
        source_version=request.source_version,
        metadata=evidence_metadata,
    )
    try:
        if finding is None:
            store.create_many([stored.artifact, evidence])
        else:
            with store.transaction() as transaction:
                transaction.add_all([stored.artifact, evidence])
                transaction.update(
                    Finding,
                    finding.id,
                    {
                        "evidence_ids": list(
                            dict.fromkeys([*finding.evidence_ids, evidence.id])
                        )
                    },
                    expected_revision=finding.revision,
                )
    except Exception:
        artifact_store.discard_new_blob(stored)
        raise
    return evidence


def _validate_links(
    store: NebulaStore, request: EvidenceUploadRequest
) -> Finding | None:
    finding: Finding | None = None
    if request.finding_id:
        finding = store.get(Finding, request.finding_id)
        if finding.engagement_id != request.engagement_id:
            raise EvidenceReferenceError(
                "finding does not belong to the evidence engagement"
            )
    for asset_id in request.asset_ids:
        asset = store.get(Asset, asset_id)
        if asset.engagement_id != request.engagement_id:
            raise EvidenceReferenceError(
                f"asset {asset_id!r} does not belong to the evidence engagement"
            )
    return finding


__all__ = [
    "EVIDENCE_UPLOAD_VERSION",
    "MAX_EVIDENCE_BYTES",
    "EvidenceReferenceError",
    "EvidenceTooLargeError",
    "EvidenceUploadError",
    "EvidenceUploadRequest",
    "InvalidEvidenceUploadError",
    "upload_evidence",
]
