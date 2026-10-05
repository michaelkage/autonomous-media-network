"""The Vetting Engine.

Pure decision logic. No filesystem, no network, no Pillow -- inputs arrive as
plain values so the rules can be tested in microseconds and reused by any future
source or sink.

An image must clear *every* rule to be published. Failures are hard rejections;
there is deliberately no "warn but ship" path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["METRIC_FIELDS", "Engagement", "Thresholds", "Verdict", "vet"]

#: Sidecar metric fields, in the order they are reported.
METRIC_FIELDS = ("views", "likes", "comments", "shares")


@dataclass(frozen=True)
class Thresholds:
    """The minimum bar an image must clear before it may be published."""

    min_width: int = 1080
    min_height: int = 1080
    min_likes: int = 500
    min_engagement_rate: float = 0.02
    allowed_formats: tuple[str, ...] = ("JPEG", "PNG", "WEBP")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Thresholds:
        """Build thresholds from a config mapping, ignoring unknown keys."""
        defaults = cls()
        formats = data.get("allowed_formats", defaults.allowed_formats)
        if isinstance(formats, str):  # tolerate `allowed_formats = "JPEG"`
            formats = (formats,)
        return cls(
            min_width=int(data.get("min_width", defaults.min_width)),
            min_height=int(data.get("min_height", defaults.min_height)),
            min_likes=int(data.get("min_likes", defaults.min_likes)),
            min_engagement_rate=float(
                data.get("min_engagement_rate", defaults.min_engagement_rate)
            ),
            allowed_formats=tuple(str(fmt).upper() for fmt in formats),
        )


@dataclass(frozen=True)
class Engagement:
    """Engagement metrics for one asset, as measured at the source."""

    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0

    @property
    def interactions(self) -> int:
        return self.likes + self.comments + self.shares

    @property
    def has_views(self) -> bool:
        """Whether the source reported a real view count.

        Reddit's public API never returns views, so a fabricated denominator
        would make the rate a meaningless constant. When this is False the
        rate check is skipped and the absolute `min_likes` floor does the work.
        """
        return self.views > 0

    @property
    def rate(self) -> float:
        """Interactions per view. Zero when no real view count is available."""
        return self.interactions / self.views if self.views > 0 else 0.0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Engagement:
        """Parse a sidecar manifest. Raises ValueError on anything unusable.

        `views` is optional: sources that do not report it (Reddit) omit it
        rather than inventing a denominator.
        """
        if not isinstance(data, Mapping):
            raise ValueError(f"manifest must be a JSON object, got {type(data).__name__}")
        try:
            values = {field: int(data.get(field, 0)) for field in METRIC_FIELDS}
        except (TypeError, ValueError) as exc:
            raise ValueError(f"metrics must be integers: {exc}") from exc
        negative = sorted(field for field, value in values.items() if value < 0)
        if negative:
            raise ValueError(f"metrics must not be negative: {', '.join(negative)}")
        return cls(**values)


@dataclass(frozen=True)
class Verdict:
    """Outcome of vetting one asset. ``reasons`` is empty exactly when passed."""

    passed: bool
    reasons: tuple[str, ...]
    width: int
    height: int
    engagement: Engagement | None = None


def vet(
    image_format: str | None,
    size: tuple[int, int],
    engagement: Engagement | None,
    thresholds: Thresholds,
) -> Verdict:
    """Decide whether an asset may be watermarked and published.

    Args:
        image_format: Format reported by the decoder, e.g. ``"JPEG"``.
        size: ``(width, height)`` in pixels.
        engagement: Source metrics, or ``None`` when unavailable.
        thresholds: The configured bar to clear.

    Returns:
        A :class:`Verdict`. Rejection reasons are human-readable so they can go
        straight into the run manifest.
    """
    width, height = size
    reasons: list[str] = []

    normalized = (image_format or "").upper()
    if normalized not in thresholds.allowed_formats:
        allowed = ", ".join(thresholds.allowed_formats)
        reasons.append(f"format {normalized or 'unknown'} not in allowed formats ({allowed})")

    if width < thresholds.min_width or height < thresholds.min_height:
        reasons.append(
            f"resolution {width}x{height} below minimum "
            f"{thresholds.min_width}x{thresholds.min_height}"
        )

    if engagement is None:
        reasons.append("no engagement metrics available, cannot verify this asset")
    else:
        # Absolute floor, enforced for every source. Unlike the rate below this
        # cannot be satisfied by inventing a denominator.
        if engagement.likes < thresholds.min_likes:
            reasons.append(f"likes {engagement.likes} below minimum {thresholds.min_likes}")
        # Rate check only where a real view count exists.
        if engagement.has_views and engagement.rate < thresholds.min_engagement_rate:
            minimum = thresholds.min_engagement_rate * 100
            actual = engagement.rate * 100
            reasons.append(f"engagement rate {actual:.2f}% below minimum {minimum:.2f}%")

    return Verdict(
        passed=not reasons,
        reasons=tuple(reasons),
        width=width,
        height=height,
        engagement=engagement,
    )
