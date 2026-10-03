"""Watermark behaviour: non-destructive, correctly placed, correctly sized."""

from __future__ import annotations

import pytest
from PIL import Image

from amn.watermark import apply_watermark


@pytest.fixture
def photo() -> Image.Image:
    return Image.new("RGB", (1200, 900), (30, 90, 200))


def test_source_image_is_not_modified(photo):
    apply_watermark(photo, "@amn")
    assert photo.getpixel((600, 450)) == (30, 90, 200)


def test_dimensions_are_preserved(photo):
    watermarked = apply_watermark(photo, "@amn")
    assert watermarked.size == photo.size


def test_mode_is_preserved(photo):
    assert apply_watermark(photo, "@amn").mode == photo.mode


def test_empty_text_is_a_no_op_copy(photo):
    watermarked = apply_watermark(photo, "")
    assert watermarked is not photo
    assert watermarked.tobytes() == photo.tobytes()


def test_text_lands_in_the_bottom_right_corner(photo):
    watermarked = apply_watermark(photo, "@amn")
    pixels = watermarked.load()
    width, height = watermarked.size

    corner_changed = any(
        pixels[x, y] != photo.getpixel((x, y))
        for x in range(width - 120, width)
        for y in range(height - 80, height)
    )
    untouched = all(
        pixels[x, y] == photo.getpixel((x, y))
        for x in range(0, width - 200)
        for y in range(0, height - 200)
    )
    assert corner_changed, "watermark never reached the corner"
    assert untouched, "watermark bled outside the bottom-right region"


@pytest.mark.parametrize("opacity", [0.0, 1.0])
def test_opacity_is_clamped_to_valid_range(photo, opacity):
    watermarked = apply_watermark(photo, "@amn", opacity=opacity)
    assert watermarked.size == photo.size


def test_transparent_source_survives(photo):
    transparent = photo.convert("RGBA")
    assert apply_watermark(transparent, "@amn").mode == "RGBA"
