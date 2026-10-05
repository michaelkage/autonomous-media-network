"""Tests for the intelligence layer. No downloads, no ImageHash."""

from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageEnhance

from amn.intelligence import MAX_DISTANCE, Captioner, DuplicateDetector, dhash, hamming


def _noise_image(size: tuple[int, int] = (256, 256), seed: int = 0) -> Image.Image:
    """A deterministic image with real structure, so hashes are meaningful.

    Uses smooth low-frequency waves rather than per-pixel static: dHash reads
    gradients, so random noise changes completely when downscaled and would
    test the fixture instead of the hash. `seed` shifts the wave phase, which
    yields a visibly different scene with a far-away hash.
    """
    image = Image.new("RGB", size)
    pixels = image.load()
    span = size[0]
    for y in range(size[1]):
        for x in range(size[0]):
            u, v = x / span, y / span
            pixels[x, y] = (
                int(128 + 100 * math.sin(3 * u + seed)),
                int(128 + 100 * math.sin(4 * v + seed * 2)),
                int(128 + 100 * math.sin(2 * (u + v) + seed * 3)),
            )
    return image


class TestDhash:
    def test_is_64_bits(self):
        assert 0 <= dhash(_noise_image()) < 2**64

    def test_is_deterministic(self):
        image = _noise_image()
        assert dhash(image) == dhash(image)

    def test_survives_resize(self):
        original = _noise_image((400, 400))
        small = original.resize((80, 80))
        assert hamming(dhash(original), dhash(small)) <= 2

    def test_survives_recompression(self, tmp_path: Path):
        image = _noise_image((400, 400))
        path = tmp_path / "a.jpg"
        image.save(path, "JPEG", quality=25)
        with Image.open(path) as reloaded:
            assert hamming(dhash(image), dhash(reloaded)) <= 5

    def test_survives_mild_brightness_shift(self):
        # dHash compares relative luminance, so it tolerates small exposure
        # changes. It is not invariant to heavy shifts: past roughly +20% the
        # sine peaks clip to white and comparisons flip. That is a real limit of
        # the algorithm, not something to assert away.
        image = _noise_image()
        bright = ImageEnhance.Brightness(image).enhance(1.1)
        assert hamming(dhash(image), dhash(bright)) <= MAX_DISTANCE

    def test_distinct_images_differ(self):
        assert hamming(dhash(_noise_image(seed=0)), dhash(_noise_image(seed=7))) > 5

    def test_hamming_counts_bits(self):
        assert hamming(0b1011, 0b1000) == 2


class TestDuplicateDetector:
    def test_first_sighting_is_not_duplicate(self, tmp_path: Path):
        detector = DuplicateDetector(tmp_path / "hashes.json")
        assert not detector.is_duplicate(_noise_image())

    def test_recorded_image_is_duplicate(self, tmp_path: Path):
        detector = DuplicateDetector(tmp_path / "hashes.json")
        image = _noise_image()
        detector.record(image)
        assert detector.is_duplicate(image)

    def test_detects_rescaled_duplicate(self, tmp_path: Path):
        detector = DuplicateDetector(tmp_path / "hashes.json")
        detector.record(_noise_image((400, 400)))
        assert detector.is_duplicate(_noise_image((100, 100)))

    def test_distinct_image_not_duplicate(self, tmp_path: Path):
        detector = DuplicateDetector(tmp_path / "hashes.json")
        detector.record(_noise_image(seed=0))
        assert not detector.is_duplicate(_noise_image(seed=9))

    def test_persists_across_instances(self, tmp_path: Path):
        db = tmp_path / "hashes.json"
        image = _noise_image()
        DuplicateDetector(db).record(image)
        assert DuplicateDetector(db).is_duplicate(image)

    def test_distance_to_known_is_none_when_empty(self, tmp_path: Path):
        assert DuplicateDetector(tmp_path / "hashes.json").distance_to_known(_noise_image()) is None

    def test_saves_hex_strings(self, tmp_path: Path):
        db = tmp_path / "hashes.json"
        detector = DuplicateDetector(db)
        detector.record(_noise_image())
        saved = json.loads(db.read_text(encoding="utf-8"))
        assert all(isinstance(item, str) and len(item) == 16 for item in saved)

    def test_corrupt_db_is_ignored(self, tmp_path: Path):
        db = tmp_path / "hashes.json"
        db.write_text("{not json", encoding="utf-8")
        detector = DuplicateDetector(db)
        assert not detector.is_duplicate(_noise_image())

    def test_garbage_entries_are_skipped(self, tmp_path: Path):
        db = tmp_path / "hashes.json"
        db.write_text(json.dumps(["nope", 123, "ffffffffffffffff"]), encoding="utf-8")
        detector = DuplicateDetector(db)
        assert len(detector.hashes) == 1

    def test_wrong_shape_db_is_ignored(self, tmp_path: Path):
        db = tmp_path / "hashes.json"
        db.write_text(json.dumps({"a": 1}), encoding="utf-8")
        assert not DuplicateDetector(db).hashes

    def test_creates_parent_dir(self, tmp_path: Path):
        detector = DuplicateDetector(tmp_path / "nested" / "deep" / "hashes.json")
        detector.record(_noise_image())
        assert detector.db_path.is_file()

    def test_custom_distance_is_respected(self, tmp_path: Path):
        image = _noise_image()
        shifted = ImageEnhance.Brightness(image).enhance(1.15)

        # A small exposure change gives a small non-zero distance: inside the
        # default threshold, outside an exact-match threshold.
        distance = hamming(dhash(image), dhash(shifted))
        assert 0 < distance <= MAX_DISTANCE

        lenient = DuplicateDetector(tmp_path / "lenient.json")
        lenient.record(image)
        assert lenient.is_duplicate(shifted)

        strict = DuplicateDetector(tmp_path / "strict.json", max_distance=0)
        strict.record(image)
        assert not strict.is_duplicate(shifted)


class TestCaptioner:
    def test_low_engagement_caption(self):
        caption = Captioner.generate(Image.new("RGB", (8, 8)), {"likes": 5})
        assert caption == "A beautiful moment captured in time."

    def test_high_engagement_caption(self):
        caption = Captioner.generate(Image.new("RGB", (8, 8)), {"likes": 5000})
        assert "viral" in caption

    def test_missing_metrics_is_safe(self):
        assert Captioner.generate(Image.new("RGB", (8, 8)), {})
