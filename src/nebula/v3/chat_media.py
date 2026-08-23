"""Validated, metadata-free chat image ingestion."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MAX_CHAT_IMAGE_BYTES = 20 * 1024 * 1024
MAX_CHAT_IMAGE_PIXELS = 40_000_000
ALLOWED_CHAT_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}


class ChatImageError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedChatImage:
    original: bytes
    preview: bytes
    media_type: str
    preview_media_type: str
    width: int
    height: int


def validate_chat_image(data: bytes, declared_media_type: str) -> ValidatedChatImage:
    if not data or len(data) > MAX_CHAT_IMAGE_BYTES:
        raise ChatImageError("image must contain between 1 byte and 20 MiB")
    if declared_media_type not in ALLOWED_CHAT_IMAGE_TYPES:
        raise ChatImageError("image must be PNG, JPEG, or WebP")
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_CHAT_IMAGE_PIXELS:
                raise ChatImageError("image exceeds the 40 megapixel limit")
            detected = Image.MIME.get(source.format or "")
            if detected != declared_media_type:
                raise ChatImageError("declared image type does not match its bytes")
            image = source.copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ChatImageError("image bytes could not be decoded safely") from exc

    output = io.BytesIO()
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        image.save(output, format="PNG", optimize=True)
        preview_type = "image/png"
    else:
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.save(output, format="JPEG", quality=88, optimize=True)
        preview_type = "image/jpeg"
    return ValidatedChatImage(
        original=data,
        preview=output.getvalue(),
        media_type=declared_media_type,
        preview_media_type=preview_type,
        width=width,
        height=height,
    )
