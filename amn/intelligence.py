"""Intelligence Layer for the Vetting Engine.

Duplicate detection via a difference hash (dHash) built on Pillow alone. The
original design pulled in ImageHash, which drags in scipy and numpy (~36MB of
wheels) for a hash that is a dozen lines of arithmetic. Dropped on purpose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image

__all__ = ["DuplicateDetector", "Captioner", "dhash"]

# dHash resizes to this width and compares adjacent pixels, so height is
# HASH_WIDTH - 1 pixels. 9x8 gives a 64-bit hash with enough resolution to
# catch re-crops and re-encodes without flagging genuinely different images.
HASH_WIDTH = 9
HASH_HEIGHT = 8

# Two hashes count as the same image when they differ by at most this many bits.
# 0 is exact-match only, which re-encoding breaks constantly. 5 of 64 is tight
# enough that visually distinct photos stay distinct.
MAX_DISTANCE = 5


def dhash(image: Image.Image) -> int:
    """Return a 64-bit difference hash of `image` as an int.

    Each bit records whether a pixel is brighter than its right-hand
    neighbour, which survives resizing, re-compression and colour shifts.
    """
    grey = image.convert("L").resize((HASH_WIDTH, HASH_HEIGHT), Image.Resampling.LANCZOS)
    pixels = grey.tobytes()
    bits = 0
    for row in range(HASH_HEIGHT):
        offset = row * HASH_WIDTH
        for col in range(HASH_WIDTH - 1):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            if left > right:
                bits |= 1 << (row * (HASH_WIDTH - 1) + col)
    return bits


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two hashes."""
    return (a ^ b).bit_count()


class DuplicateDetector:
    """Prevents posting the same image twice using perceptual hashing.

    Stores hashes as hex strings in a local JSON file.
    """

    def __init__(
        self,
        db_path: Path = Path("build/hashes.json"),
        max_distance: int = MAX_DISTANCE,
    ) -> None:
        self.db_path = db_path
        self.max_distance = max_distance
        self.hashes: set[int] = self._load_hashes()

    def _load_hashes(self) -> set[int]:
        if not self.db_path.is_file():
            return set()
        try:
            data = json.loads(self.db_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return set()
        if not isinstance(data, list):
            return set()
        hashes: set[int] = set()
        for item in data:
            try:
                hashes.add(int(item, 16))
            except (TypeError, ValueError):
                continue
        return hashes

    def save(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [f"{h:016x}" for h in sorted(self.hashes)]
        self.db_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def is_duplicate(self, image: Image.Image) -> bool:
        """True if a near-identical image has been seen before."""
        nearest = self.distance_to_known(image)
        return nearest is not None and nearest <= self.max_distance

    def distance_to_known(self, image: Image.Image) -> int | None:
        """Smallest bit distance to any recorded hash, or None if unseen."""
        candidate = dhash(image)
        if not self.hashes:
            return None
        return min(hamming(candidate, known) for known in self.hashes)

    def record(self, image: Image.Image) -> None:
        """Add image hash to the database."""
        self.hashes.add(dhash(image))
        self.save()


class Captioner:
    """Generates captions for images.

    In v1, this uses a simple rule-based approach.
    Future versions can integrate LLava or Moondream.
    """

    @staticmethod
    def generate(image: Image.Image, metrics: dict[str, Any]) -> str:
        # Placeholder for AI logic
        score = metrics.get("likes", 0)
        if score > 1000:
            return "A stunning viral shot that captured everyone's attention!"
        return "A beautiful moment captured in time."
