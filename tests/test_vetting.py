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
    verdict = vet("JPEG", (2000, 2000), Engagement(views=25_000, likes=500), THRESHOLDS)
    assert verdict.passed


def test_missing_views_skips_the_rate_check():
    # Reddit reports upvotes but never views. Rather than invent a denominator,
    # the rate rule is skipped and the absolute likes floor decides.
    engagement = Engagement(views=0, likes=900)
    assert not engagement.has_views
    assert engagement.rate == 0.0

    verdict = vet("JPEG", (2000, 2000), engagement, THRESHOLDS)
    assert verdict.passed
    assert not any("engagement rate" in reason for reason in verdict.reasons)


def test_all_failures_are_reported_at_once():
    verdict = vet("BMP", (100, 100), Engagement(views=100, likes=1), THRESHOLDS)
    assert not verdict.passed
    # format, resolution, likes floor, rate
    assert len(verdict.reasons) == 4


def test_low_likes_is_rejected_even_with_a_high_rate():
    # The floor is absolute: a tiny audience that loves it is not enough.
    engagement = Engagement(views=100, likes=99, comments=1)
    assert engagement.rate == 1.0  # 100% rate
    verdict = vet("JPEG", (2000, 2000), engagement, THRESHOLDS)
    assert not verdict.passed
    assert any("likes 99 below minimum 500" in reason for reason in verdict.reasons)


def test_custom_like_floor_is_honoured():
    lenient = Thresholds(min_likes=10)
    assert vet("JPEG", (2000, 2000), Engagement(likes=50), lenient).passed
    strict = Thresholds(min_likes=10_000)
    assert not vet("JPEG", (2000, 2000), Engagement(likes=50), strict).passed


def test_rate_still_catches_a_source_that_reports_views():
    # A source with real views and huge likes can still fail on rate.
    engagement = Engagement(views=100_000, likes=600)
    verdict = vet("JPEG", (2000, 2000), engagement, THRESHOLDS)
    assert engagement.likes >= THRESHOLDS.min_likes
    assert not verdict.passed
    assert any("engagement rate 0.60%" in reason for reason in verdict.reasons)


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
            # `views` is optional: sources that cannot report reach omit it.
            ({}, Engagement()),
            ({"likes": 10}, Engagement(likes=10)),
        ],
    )
    def test_from_mapping_fills_defaults_and_coerces(self, data, expected):
        assert Engagement.from_mapping(data) == expected

    @pytest.mark.parametrize(
        "data",
        [
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
