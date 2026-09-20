import io

import pytest
from PIL import Image, ImageFile

from nebula.v3.chat_media import ChatImageError, validate_chat_image


def _png(size: tuple[int, int], mode: str = "RGB") -> bytes:
    output = io.BytesIO()
    Image.new(mode, size, 1 if mode == "1" else "red").save(output, format="PNG")
    return output.getvalue()


def test_a_multi_picture_jpeg_from_a_phone_is_accepted_as_jpeg():
    first = Image.new("RGB", (8, 8), "red")
    second = Image.new("RGB", (8, 8), "blue")
    output = io.BytesIO()
    first.save(output, format="MPO", save_all=True, append_images=[second])
    assert Image.open(io.BytesIO(output.getvalue())).format == "MPO"

    validated = validate_chat_image(output.getvalue(), "image/jpeg")

    assert validated.media_type == "image/jpeg"
    assert validated.preview_media_type == "image/jpeg"
    assert (validated.width, validated.height) == (8, 8)
    assert Image.open(io.BytesIO(validated.preview)).format == "JPEG"


def test_declared_type_must_still_match_the_bytes():
    with pytest.raises(ChatImageError, match="does not match"):
        validate_chat_image(_png((4, 4)), "image/jpeg")


def test_oversized_image_is_rejected_from_its_header_without_decoding(monkeypatch):
    data = _png((7_000, 7_000), mode="1")

    def decoded(*_args, **_kwargs):
        raise RuntimeError("pixels were decoded before the size check")

    monkeypatch.setattr(ImageFile.ImageFile, "load", decoded)

    with pytest.raises(ChatImageError, match="40 megapixel"):
        validate_chat_image(data, "image/png")


def test_small_png_still_decodes_and_previews():
    validated = validate_chat_image(_png((4, 4), mode="RGBA"), "image/png")

    assert validated.preview_media_type == "image/png"
    assert (validated.width, validated.height) == (4, 4)
