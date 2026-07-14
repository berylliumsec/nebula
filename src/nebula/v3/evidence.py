"""Safe, artifact-backed operator evidence ingestion for Nebula 3."""

from __future__ import annotations

import base64
import binascii
from io import BytesIO
import json
import re
import struct
from typing import Any
from uuid import uuid4
import warnings

from PIL import Image, UnidentifiedImageError
from pydantic import Field, field_validator

from .artifacts import ArtifactStore
from .domain import Artifact, Asset, Engagement, Evidence, Finding, NebulaModel
from .storage import NebulaStore

MAX_EVIDENCE_BYTES = 25 * 1024 * 1024
MAX_EVIDENCE_METADATA_BYTES = 16 * 1024
EVIDENCE_UPLOAD_VERSION = "nebula.evidence-upload.v1"
MAX_DECODED_IMAGE_DIMENSION = 16_384
MAX_DECODED_IMAGE_PIXELS = 100_000_000
IMAGE_EDIT_RECIPE_VERSION = 1
MAX_IMAGE_EDIT_OPERATIONS = 200

_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_EVIDENCE_TYPE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_IMAGE_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}


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
    parent_artifact_id: str | None = Field(default=None, max_length=200)
    source_context: dict[str, Any] = Field(default_factory=dict)
    edit_recipe: dict[str, Any] | None = None
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

    @field_validator("metadata", "source_context")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
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

    @field_validator("edit_recipe")
    @classmethod
    def valid_edit_recipe(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            encoded = json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("edit_recipe must be valid JSON") from exc
        if len(encoded) > MAX_EVIDENCE_METADATA_BYTES:
            raise ValueError("edit_recipe exceeds the 16 KiB limit")
        _validate_edit_recipe(value)
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
        image_details = _validate_image_content(data, self.filename, self.media_type)
        if self.edit_recipe is not None:
            if image_details is None:
                raise InvalidEvidenceUploadError(
                    "edit_recipe may be attached only to PNG, JPEG, or WebP evidence"
                )
            _, width, height = image_details
            if (
                self.edit_recipe["output_width"] != width
                or self.edit_recipe["output_height"] != height
            ):
                raise InvalidEvidenceUploadError(
                    "edit_recipe output dimensions do not match the uploaded image"
                )
        return data


def _validate_image_content(
    data: bytes, filename: str, media_type: str | None
) -> tuple[str, int, int] | None:
    """Validate raster signatures, bounds, and a complete decoder pass."""

    suffix = filename.casefold().rsplit(".", 1)[-1] if "." in filename else ""
    leading = data[:2_048].lstrip().lower()
    if (
        suffix == "svg"
        or media_type == "image/svg+xml"
        or leading.startswith(b"<svg")
        or (leading.startswith(b"<?xml") and b"<svg" in leading)
    ):
        raise InvalidEvidenceUploadError(
            "SVG evidence is not accepted; use PNG, JPEG, or WebP"
        )

    image_requested = bool(
        (media_type and media_type.startswith("image/"))
        or suffix in {"png", "jpg", "jpeg", "webp"}
    )
    if not image_requested:
        return None
    allowed = {"image/png", "image/jpeg", "image/webp"}
    if media_type and media_type not in allowed:
        raise InvalidEvidenceUploadError("image evidence must be PNG, JPEG, or WebP")

    detected, width, height = _image_dimensions(data)
    if media_type and detected != media_type:
        raise InvalidEvidenceUploadError(
            "image content does not match the declared media type"
        )
    if width < 1 or height < 1:
        raise InvalidEvidenceUploadError("image dimensions are invalid")
    if (
        width > MAX_DECODED_IMAGE_DIMENSION
        or height > MAX_DECODED_IMAGE_DIMENSION
        or width * height > MAX_DECODED_IMAGE_PIXELS
    ):
        raise InvalidEvidenceUploadError(
            "decoded image dimensions exceed the safety limit"
        )
    _verify_image_decode(data, detected, width, height)
    return detected, width, height


def _verify_image_decode(data: bytes, media_type: str, width: int, height: int) -> None:
    """Run Pillow verification and a full, bounded single-frame decode."""

    expected_format = _IMAGE_FORMATS[media_type]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != expected_format or image.size != (width, height):
                    raise InvalidEvidenceUploadError(
                        "image decoder metadata does not match the raster header"
                    )
                if getattr(image, "n_frames", 1) != 1:
                    raise InvalidEvidenceUploadError(
                        "animated image evidence is not accepted"
                    )
                image.verify()
            # verify() deliberately invalidates the decoder. Reopen and load every
            # pixel so header-valid truncated streams cannot pass ingestion.
            with Image.open(BytesIO(data)) as image:
                if image.format != expected_format or image.size != (width, height):
                    raise InvalidEvidenceUploadError(
                        "image decoder metadata does not match the raster header"
                    )
                image.load()
    except InvalidEvidenceUploadError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise InvalidEvidenceUploadError(
            "image evidence is truncated or corrupt"
        ) from exc


def _validate_edit_recipe(recipe: dict[str, Any]) -> None:
    required_recipe_keys = {
        "version",
        "source_width",
        "source_height",
        "output_width",
        "output_height",
        "operations",
    }
    if set(recipe) != required_recipe_keys:
        raise ValueError(
            "edit_recipe must contain only the version, dimensions, and operations"
        )
    if (
        not isinstance(recipe["version"], int)
        or isinstance(recipe["version"], bool)
        or recipe["version"] != IMAGE_EDIT_RECIPE_VERSION
    ):
        raise ValueError("edit_recipe version is unsupported")

    source_width = _recipe_integer(
        recipe["source_width"],
        "source_width",
        minimum=1,
        maximum=MAX_DECODED_IMAGE_DIMENSION,
    )
    source_height = _recipe_integer(
        recipe["source_height"],
        "source_height",
        minimum=1,
        maximum=MAX_DECODED_IMAGE_DIMENSION,
    )
    output_width = _recipe_integer(
        recipe["output_width"],
        "output_width",
        minimum=1,
        maximum=MAX_DECODED_IMAGE_DIMENSION,
    )
    output_height = _recipe_integer(
        recipe["output_height"],
        "output_height",
        minimum=1,
        maximum=MAX_DECODED_IMAGE_DIMENSION,
    )
    if (
        source_width * source_height > MAX_DECODED_IMAGE_PIXELS
        or output_width * output_height > MAX_DECODED_IMAGE_PIXELS
    ):
        raise ValueError("edit_recipe dimensions exceed the decoded image safety limit")

    operations = recipe["operations"]
    if not isinstance(operations, list):
        raise ValueError("edit_recipe operations must be a list")
    if len(operations) > MAX_IMAGE_EDIT_OPERATIONS:
        raise ValueError(
            f"edit_recipe may contain at most {MAX_IMAGE_EDIT_OPERATIONS} operations"
        )

    width = source_width
    height = source_height
    operation_ids: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("each edit_recipe operation must be an object")
        operation_type = operation.get("type")
        common = {"id", "type"}
        required: set[str]
        if operation_type == "crop":
            required = common | {"rect"}
        elif operation_type == "rectangle":
            required = common | {"rect", "color", "thickness"}
        elif operation_type == "arrow":
            required = common | {"from", "to", "color", "thickness"}
        elif operation_type == "blur":
            required = common | {"rect", "radius"}
        elif operation_type == "redact":
            required = common | {"rect", "color"}
        elif operation_type == "text":
            required = common | {"at", "text", "color", "fontSize"}
        else:
            raise ValueError("edit_recipe contains an unsupported operation type")
        if set(operation) != required:
            raise ValueError(
                f"edit_recipe {operation_type} operation has missing or unknown fields"
            )

        operation_id = operation["id"]
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or len(operation_id) > 128
            or any(ord(character) < 32 for character in operation_id)
        ):
            raise ValueError(
                "edit_recipe operation id must contain 1 to 128 characters"
            )
        if operation_id in operation_ids:
            raise ValueError("edit_recipe operation ids must be unique")
        operation_ids.add(operation_id)

        if operation_type in {"crop", "rectangle", "blur", "redact"}:
            rect = _recipe_rect(operation["rect"], width, height)
            if operation_type == "crop":
                width, height = rect[2], rect[3]
        if operation_type == "arrow":
            _recipe_point(operation["from"], width, height)
            _recipe_point(operation["to"], width, height)
        elif operation_type == "text":
            _recipe_point(operation["at"], width, height)
            text = operation["text"]
            if not isinstance(text, str) or not text or len(text) > 1_000:
                raise ValueError("edit_recipe text must contain 1 to 1,000 characters")
            _recipe_integer(operation["fontSize"], "fontSize", minimum=8, maximum=256)

        if operation_type in {"rectangle", "arrow"}:
            _recipe_integer(operation["thickness"], "thickness", minimum=1, maximum=64)
        elif operation_type == "blur":
            _recipe_integer(operation["radius"], "radius", minimum=1, maximum=64)
        if operation_type in {"rectangle", "arrow", "redact", "text"}:
            color = operation["color"]
            if not isinstance(color, str) or not _HEX_COLOR.fullmatch(color):
                raise ValueError(
                    "edit_recipe colors must be opaque six-digit hex values"
                )

    if (width, height) != (output_width, output_height):
        raise ValueError(
            "edit_recipe output dimensions do not match its ordered operations"
        )


def _recipe_integer(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise ValueError(
            f"edit_recipe {label} must be an integer from {minimum} to {maximum}"
        )
    return value


def _recipe_point(value: Any, width: int, height: int) -> tuple[int, int]:
    if not isinstance(value, dict) or set(value) != {"x", "y"}:
        raise ValueError("edit_recipe points must contain only x and y")
    x = _recipe_integer(value["x"], "x", minimum=0, maximum=width)
    y = _recipe_integer(value["y"], "y", minimum=0, maximum=height)
    return x, y


def _recipe_rect(value: Any, width: int, height: int) -> tuple[int, int, int, int]:
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        raise ValueError(
            "edit_recipe rectangles must contain only x, y, width, and height"
        )
    x = _recipe_integer(value["x"], "x", minimum=0, maximum=width - 1)
    y = _recipe_integer(value["y"], "y", minimum=0, maximum=height - 1)
    rect_width = _recipe_integer(value["width"], "width", minimum=1, maximum=width)
    rect_height = _recipe_integer(value["height"], "height", minimum=1, maximum=height)
    if x + rect_width > width or y + rect_height > height:
        raise ValueError("edit_recipe rectangle lies outside the current image")
    return x, y, rect_width, rect_height


def _image_dimensions(data: bytes) -> tuple[str, int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height
    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset : offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if (
                marker
                in {
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                }
                and length >= 7
            ):
                height = int.from_bytes(data[offset + 3 : offset + 5], "big")
                width = int.from_bytes(data[offset + 5 : offset + 7], "big")
                return "image/jpeg", width, height
            offset += length
        raise InvalidEvidenceUploadError("JPEG dimensions could not be read safely")
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
        kind = data[12:16]
        if kind == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return "image/webp", width, height
        if kind == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return "image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return "image/webp", width, height
        raise InvalidEvidenceUploadError("WebP dimensions could not be read safely")
    raise InvalidEvidenceUploadError(
        "image evidence has an unsupported or corrupt signature"
    )


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
    if request.source_context:
        artifact_metadata["source_context"] = request.source_context
    if request.edit_recipe is not None:
        artifact_metadata["edit_recipe"] = request.edit_recipe
    stored = artifact_store.put_bytes_with_status(
        data,
        engagement_id=request.engagement_id,
        filename=request.filename,
        media_type=request.media_type,
        source=request.source,
        parent_artifact_id=request.parent_artifact_id,
        metadata=artifact_metadata,
    )
    evidence_metadata = dict(request.metadata)
    if request.source_context:
        evidence_metadata["source_context"] = request.source_context
    if request.edit_recipe is not None:
        evidence_metadata["edit_recipe"] = request.edit_recipe
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
    if request.parent_artifact_id:
        parent = store.get(Artifact, request.parent_artifact_id)
        if parent.engagement_id != request.engagement_id:
            raise EvidenceReferenceError(
                "parent artifact does not belong to the evidence engagement"
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
