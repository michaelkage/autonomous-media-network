"""The Vetting Engine is the gate that decides what gets published.

These tests cover the rules directly; no disk, no network, no Pillow.
"""

from __future__ import annotations

import pytest

from amn.vetting import Engagement, Thresholds, vet

THRESHOLDS = Thresholds()


def test_clean_asset_passes():
    verdict = vet("JPEG", (1200, 1200), Engagement(views=10_000, likes=500), THRESHOLDS)
    assert verdict.passed
    assert verdict.reasons == ()


def test_below_minimum_resolution_is_rejected():
    verdict = vet("JPEG", (640, 480), Engagement(views=10_000, likes=500), THRESHOLDS)
    assert not verdict.passed
    assert any("resolution 640x480" in reason for reason in verdict.reasons)


def test_one_short_edge_is_enough_to_reject():
    verdict = vet("PNG", (1200, 1079), Engagement(views=10_000, likes=500), THRESHOLDS)
    assert not verdict.passed


def test_unknown_format_is_rejected():
    verdict = vet("GIF", (2000, 2000), Engagement(views=10_000, likes=500), THRESHOLDS)
    assert not verdict.passed
    assert any("format GIF" in reason for reason in verdict.reasons)


def test_missing_format_is_rejected():
    verdict = vet(None, (2000, 2000), Engagement(views=10_000, likes=500), THRESHOLDS)
    assert not verdict.passed


def test_missing_metrics_are_rejected():
    verdict = vet("JPEG", (2000, 2000), None, THRESHOLDS)
    assert not verdict.passed
    assert any("no engagement metrics" in reason for reason in verdict.reasons)


def test_low_engagement_is_rejected():
    verdict = vet("JPEG", (2000, 2000), Engagement(views=10_000, likes=10), THRESHOLDS)
    assert not verdict.passed
    assert any("engagement rate 0.10%" in reason for reason in verdict.reasons)


def test_engagement_exactly_at_threshold_passes():
    # views * rate == interactions, so this is the boundary, not a rounding case.
    verdict = vet("JPEG", (2000, 2000), Engagement(views=10_000, likes=200), THRESHOLDS)
    assert verdict.passed


def test_zero_views_rejected_without_dividing_by_zero():
    verdict = vet("JPEG", (2000, 2000), Engagement(views=0, likes=999), THRESHOLDS)
    assert not verdict.passed
    assert any("engagement rate 0.00%" in reason for reason in verdict.reasons)


def test_all_failures_are_reported_at_once():
    verdict = vet("BMP", (100, 100), Engagement(views=100, likes=1), THRESHOLDS)
    assert not verdict.passed
    assert len(verdict.reasons) == 3


class TestEngagement:
    def test_rate_counts_every_interaction_type(self):
        engagement = Engagement(views=1_000, likes=10, comments=5, shares=5)
        assert engagement.interactions == 20
        assert engagement.rate == pytest.approx(0.02)

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({"views": 100}, Engagement(views=100)),
            ({"views": 100, "likes": 5}, Engagement(views=100, likes=5)),
            ({"views": "100", "shares": "3"}, Engagement(views=100, shares=3)),
        ],
    )
    def test_from_mapping_fills_defaults_and_coerces(self, data, expected):
        assert Engagement.from_mapping(data) == expected

    @pytest.mark.parametrize(
        "data",
        [
            {},  # no views at all
            {"likes": 10},  # metrics without reach
            {"views": -1},  # nonsense
            {"views": 10, "likes": -5},  # nonsense
            {"views": "many"},  # unparseable
            {"views": None},  # null
            [1, 2, 3],  # not an object
        ],
    )
    def test_from_mapping_rejects_bad_input(self, data):
        with pytest.raises(ValueError):
            Engagement.from_mapping(data)


class TestThresholds:
    def test_defaults_apply_when_config_is_empty(self):
        assert Thresholds.from_mapping({}) == Thresholds()

    def test_unknown_keys_are_ignored(self):
        thresholds = Thresholds.from_mapping({"min_width": 800, "nonsense": True})
        assert thresholds.min_width == 800
        assert thresholds.min_height == Thresholds().min_height

    def test_format_allowlist_is_normalized(self):
        thresholds = Thresholds.from_mapping({"allowed_formats": ["jpeg", "png"]})
        assert thresholds.allowed_formats == ("JPEG", "PNG")

    def test_configured_thresholds_actually_bind(self):
        thresholds = Thresholds(min_width=100, min_height=100, min_engagement_rate=0.5)
        verdict = vet("JPEG", (200, 200), Engagement(views=100, likes=10), thresholds)
        assert not verdict.passed
        assert any("50.00%" in reason for reason in verdict.reasons)
