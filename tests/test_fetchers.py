"""Tests for the fetchers. No network: the requests session is stubbed.

These exist because both fetchers previously invented engagement metrics, and
nothing caught it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from amn.fetchers.pexels import PexelsFetcher
from amn.fetchers.reddit import RedditFetcher

PNG_BYTES = b"\x89PNG\r\n\x1a\n not a real png but it does not matter here"


class FakeResponse:
    def __init__(self, payload: Any = None, content: bytes = b"") -> None:
        self._payload = payload
        self.content = content

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    """Returns canned responses and records every URL requested."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def get(self, url: str, timeout: int = 10) -> FakeResponse:
        self.requested.append(url)
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


class UnreachableSession:
    """Stands in for a session that cannot reach the host at all."""

    def get(self, url: str, timeout: int = 10) -> FakeResponse:
        raise requests.ConnectionError("name resolution failed")


def _reddit_payload(score: int, comments: int, ext: str = ".jpg") -> dict:
    return {
        "data": {
            "children": [
                {
                    "data": {
                        "id": "abc123",
                        "url": f"https://i.redd.it/x{ext}",
                        "score": score,
                        "num_comments": comments,
                    }
                }
            ]
        }
    }


class TestRedditFetcher:
    def _fetcher(self, *responses: FakeResponse) -> RedditFetcher:
        fetcher = RedditFetcher("test-agent")
        fetcher.session = FakeSession(list(responses))
        return fetcher

    def test_saves_image_and_sidecar(self, tmp_path: Path):
        fetcher = self._fetcher(
            FakeResponse(_reddit_payload(900, 40)),
            FakeResponse(content=PNG_BYTES),
        )
        assert fetcher.fetch("earthporn", inbox_dir=tmp_path) == 1
        assert (tmp_path / "abc123.jpg").read_bytes() == PNG_BYTES
        assert (tmp_path / "abc123.json").is_file()

    def test_never_fabricates_views(self, tmp_path: Path):
        """The bug: views was set to score * 20, pinning the rate at ~5%."""
        fetcher = self._fetcher(
            FakeResponse(_reddit_payload(900, 40)),
            FakeResponse(content=PNG_BYTES),
        )
        fetcher.fetch("earthporn", inbox_dir=tmp_path)
        metrics = json.loads((tmp_path / "abc123.json").read_text(encoding="utf-8"))
        assert "views" not in metrics

    def test_records_real_counts(self, tmp_path: Path):
        fetcher = self._fetcher(
            FakeResponse(_reddit_payload(900, 40)),
            FakeResponse(content=PNG_BYTES),
        )
        fetcher.fetch("earthporn", inbox_dir=tmp_path)
        metrics = json.loads((tmp_path / "abc123.json").read_text(encoding="utf-8"))
        assert metrics == {"likes": 900, "comments": 40, "shares": 0}

    def test_low_score_is_recorded_honestly(self, tmp_path: Path):
        # 3 upvotes. The fetcher must not round this up to clear the floor.
        fetcher = self._fetcher(
            FakeResponse(_reddit_payload(3, 0)),
            FakeResponse(content=PNG_BYTES),
        )
        fetcher.fetch("earthporn", inbox_dir=tmp_path)
        metrics = json.loads((tmp_path / "abc123.json").read_text(encoding="utf-8"))
        assert metrics["likes"] == 3

    def test_skips_non_image_urls(self, tmp_path: Path):
        fetcher = self._fetcher(FakeResponse(_reddit_payload(900, 40, ext=".gif")))
        assert fetcher.fetch("earthporn", inbox_dir=tmp_path) == 0
        assert not list(tmp_path.glob("*.jpg"))

    def test_skips_existing_file(self, tmp_path: Path):
        (tmp_path / "abc123.jpg").write_bytes(PNG_BYTES)
        fetcher = self._fetcher(FakeResponse(_reddit_payload(900, 40)))
        assert fetcher.fetch("earthporn", inbox_dir=tmp_path) == 0

    def test_network_failure_returns_zero(self, tmp_path: Path, capsys):
        fetcher = RedditFetcher("test-agent")
        fetcher.session = UnreachableSession()
        assert fetcher.fetch("earthporn", inbox_dir=tmp_path) == 0
        assert "failed to reach reddit" in capsys.readouterr().out

    def test_raise_for_status_is_not_swallowed(self, tmp_path: Path):
        class Forbidden(FakeSession):
            def get(self, url: str, timeout: int = 10) -> FakeResponse:
                raise requests.HTTPError("403")

        fetcher = RedditFetcher("test-agent")
        fetcher.session = Forbidden([])
        assert fetcher.fetch("earthporn", inbox_dir=tmp_path) == 0


class TestPexelsFetcher:
    def _fetcher(self, *responses: FakeResponse) -> PexelsFetcher:
        fetcher = PexelsFetcher("key")
        fetcher.session = FakeSession(list(responses))
        return fetcher

    @staticmethod
    def _payload() -> dict:
        return {"photos": [{"id": 7, "src": {"large": "https://images.pexels.com/x.jpg"}}]}

    def test_saves_image_and_sidecar(self, tmp_path: Path):
        fetcher = self._fetcher(FakeResponse(self._payload()), FakeResponse(content=PNG_BYTES))
        assert fetcher.fetch("forest", inbox_dir=tmp_path) == 1
        assert (tmp_path / "pexels_7.jpg").read_bytes() == PNG_BYTES

    def test_never_fabricates_views(self, tmp_path: Path):
        """Pexels reports no per-photo engagement at all."""
        fetcher = self._fetcher(FakeResponse(self._payload()), FakeResponse(content=PNG_BYTES))
        fetcher.fetch("forest", inbox_dir=tmp_path)
        metrics = json.loads((tmp_path / "pexels_7.json").read_text(encoding="utf-8"))
        assert "views" not in metrics
        assert metrics["likes"] == 0

    def test_skips_existing_file(self, tmp_path: Path):
        (tmp_path / "pexels_7.jpg").write_bytes(PNG_BYTES)
        fetcher = self._fetcher(FakeResponse(self._payload()))
        assert fetcher.fetch("forest", inbox_dir=tmp_path) == 0

    def test_network_failure_returns_zero(self, tmp_path: Path, capsys):
        fetcher = PexelsFetcher("key")
        fetcher.session = UnreachableSession()
        assert fetcher.fetch("forest", inbox_dir=tmp_path) == 0
        assert "failed to reach pexels" in capsys.readouterr().out


class TestFetchedAssetsCannotBypassTheGate:
    """End of the regression: real fetch output, run through the real vet."""

    def test_one_upvote_post_is_rejected(self, tmp_path: Path):
        from amn.vetting import Engagement, Thresholds, vet

        fetcher = RedditFetcher("test-agent")
        fetcher.session = FakeSession(
            [FakeResponse(_reddit_payload(1, 0)), FakeResponse(content=PNG_BYTES)]
        )
        fetcher.fetch("earthporn", inbox_dir=tmp_path)

        metrics = json.loads((tmp_path / "abc123.json").read_text(encoding="utf-8"))
        verdict = vet("JPEG", (1600, 1200), Engagement.from_mapping(metrics), Thresholds())
        assert not verdict.passed
        assert any("likes 1 below minimum 500" in reason for reason in verdict.reasons)

    def test_popular_post_passes(self, tmp_path: Path):
        from amn.vetting import Engagement, Thresholds, vet

        fetcher = RedditFetcher("test-agent")
        fetcher.session = FakeSession(
            [FakeResponse(_reddit_payload(4_000, 200)), FakeResponse(content=PNG_BYTES)]
        )
        fetcher.fetch("earthporn", inbox_dir=tmp_path)

        metrics = json.loads((tmp_path / "abc123.json").read_text(encoding="utf-8"))
        verdict = vet("JPEG", (1600, 1200), Engagement.from_mapping(metrics), Thresholds())
        assert verdict.passed
