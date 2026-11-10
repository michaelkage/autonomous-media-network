"""Image intelligence: perceptual duplicate detection and captioning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image

__all__ = ["DuplicateDetector", "Captioner", "dhash", "hamming"]

HASH_WIDTH, HASH_HEIGHT, MAX_DISTANCE = 9, 8, 5


def dhash(image: Image.Image) -> int:
    grey = image.convert("L").resize((HASH_WIDTH, HASH_HEIGHT), Image.Resampling.LANCZOS)
    pixels, bits = grey.tobytes(), 0
    for row in range(HASH_HEIGHT):
        offset = row * HASH_WIDTH
        for col in range(HASH_WIDTH - 1):
            if pixels[offset + col] > pixels[offset + col + 1]:
                bits |= 1 << (row * (HASH_WIDTH - 1) + col)
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


class DuplicateDetector:
    def __init__(
        self, db_path: Path = Path("build/hashes.json"), max_distance: int = MAX_DISTANCE
    ) -> None:
        if not 0 <= max_distance <= 64:
            raise ValueError("max_distance must be between 0 and 64")
        self.db_path, self.max_distance = Path(db_path), max_distance
        self.hashes = self._load_hashes()

    def _load_hashes(self) -> set[int]:
        if not self.db_path.exists():
            return set()
        try:
            data = json.loads(self.db_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"duplicate database is corrupt: {self.db_path}: {exc}") from exc
        if not isinstance(data, list):
            raise ValueError(f"duplicate database must contain a JSON array: {self.db_path}")
        hashes: set[int] = set()
        for item in data:
            if not isinstance(item, str) or len(item) != 16:
                raise ValueError(f"invalid dHash entry in {self.db_path}: {item!r}")
            try:
                value = int(item, 16)
            except ValueError as exc:
                raise ValueError(f"invalid dHash entry in {self.db_path}: {item!r}") from exc
            hashes.add(value)
        return hashes

    def save(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.db_path.with_name(f".{self.db_path.name}.tmp")
        temporary.write_text(
            json.dumps([f"{h:016x}" for h in sorted(self.hashes)], indent=2), encoding="utf-8"
        )
        temporary.replace(self.db_path)

    def distance_to_known(self, image: Image.Image) -> int | None:
        candidate = dhash(image)
        return min((hamming(candidate, known) for known in self.hashes), default=None)

    def is_duplicate(self, image: Image.Image) -> bool:
        nearest = self.distance_to_known(image)
        return nearest is not None and nearest <= self.max_distance

    def record(self, image: Image.Image) -> None:
        self.hashes.add(dhash(image))
        self.save()

    def record_from_path(self, path: Path) -> None:
        with Image.open(path) as image:
            image.load()
            self.record(image)


class Captioner:
    @staticmethod
    def generate(image: Image.Image, metrics: dict[str, Any]) -> str:
        score = metrics.get("likes", 0)
        return (
            "A viral moment worth sharing."
            if score > 1000
            else "A beautiful moment captured in time."
        )
