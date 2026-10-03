"""Shared fixtures. Images are generated, never downloaded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project root with an empty inbox."""
    (tmp_path / "inbox").mkdir()
    return tmp_path


@pytest.fixture
def add_asset(project: Path):
    """Drop an image plus its sidecar metrics manifest into the inbox."""

    def _add(
        name: str = "photo.jpg",
        size: tuple[int, int] = (1200, 1200),
        metrics: dict | None = None,
        color: tuple[int, int, int] = (30, 90, 200),
        fmt: str = "JPEG",
        write_metrics: bool = True,
    ) -> Path:
        path = project / "inbox" / name
        Image.new("RGB", size, color).save(path, fmt)
        if write_metrics:
            payload = {"views": 10_000, "likes": 500}
            payload.update(metrics or {})
            path.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
        return path

    return _add
