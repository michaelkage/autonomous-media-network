"""Watermarking via Pillow."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

__all__ = ["DEFAULT_OPACITY", "apply_watermark"]

#: Alpha of the watermark text, 0.0-1.0.
DEFAULT_OPACITY = 0.45
#: Font size and edge margin, as a fraction of the image's shorter side.
FONT_RATIO = 0.045
MARGIN_RATIO = 0.03

# Pillow bundles no font, so probe the usual platform locations before falling
# back to Pillow's own scaled default (Aileron, Pillow >= 10.1).
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _load_font(size: int) -> ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES:
        if not Path(candidate).exists():
            continue
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no size argument
        return ImageFont.load_default()


def apply_watermark(
    image: Image.Image,
    text: str,
    *,
    opacity: float = DEFAULT_OPACITY,
    fill: tuple[int, int, int] = (255, 255, 255),
) -> Image.Image:
    """Stamp ``text`` in the bottom-right corner and return a new image.

    The source image is never modified. Output keeps the source mode, so JPEG in
    gives JPEG out. An empty ``text`` is a no-op copy.
    """
    base = image.convert("RGBA")
    if not text:
        return base.convert(image.mode)

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font = _load_font(max(12, round(min(base.size) * FONT_RATIO)))
    # textbbox anchors on the glyph origin, not the drawn box, so keep the
    # returned left/top deltas to land text exactly inside the margin.
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)

    margin = round(min(base.size) * MARGIN_RATIO)
    x = base.width - right - margin
    y = base.height - bottom - margin

    alpha = round(max(0.0, min(1.0, opacity)) * 255)
    draw.text((x, y), text, font=font, fill=(*fill, alpha))

    return Image.alpha_composite(base, overlay).convert(image.mode)
