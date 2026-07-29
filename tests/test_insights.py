"""Tests for the scoring logic behind insights.

These are the functions that make claims to the user, so the cases that matter
most are the ones where the honest answer is "I don't know": a thin sample, a
lucky one-post slot, a missing denominator. Getting those wrong produces
confident bad advice, which is the one failure mode the product cannot absorb.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from api.v1.services.insight_service import (
    BEST_TIME_THRESHOLDS,
    FORMAT_THRESHOLDS,
    MIN_POSTS_PER_FORMAT,
    build_format_rows,
    confidence_for,
    score_cells,
)


def row(
    post_type="image",
    posted_at=None,
    engagement_rate=None,
    reach=None,
    likes=0,
    comments=0,
    shares=0,
    saves=0,
):
    return SimpleNamespace(
        id=None,
        post_type=post_type,
        posted_at=posted_at,
        engagement_rate=engagement_rate,
        reach=reach,
        views=None,
        likes=likes,
        comments=comments,
        shares=shares,
        saves=saves,
    )


def at(weekday: int, hour: int) -> datetime:
    """A datetime on a known weekday. 2026-07-27 is a Monday (weekday 0)."""
    return datetime(2026, 7, 27 + weekday, hour, 0, tzinfo=timezone.utc)


class TestConfidenceFor:
    def test_below_the_floor_is_insufficient(self):
        assert confidence_for(29, BEST_TIME_THRESHOLDS) == "insufficient_data"

    def test_bands_step_at_the_thresholds(self):
        assert confidence_for(30, BEST_TIME_THRESHOLDS) == "low"
        assert confidence_for(60, BEST_TIME_THRESHOLDS) == "moderate"
        assert confidence_for(120, BEST_TIME_THRESHOLDS) == "high"

    def test_zero_samples_is_insufficient(self):
        assert confidence_for(0, FORMAT_THRESHOLDS) == "insufficient_data"

    def test_best_time_is_stricter_than_format_comparison(self):
        """168 weekly slots vs at most six formats — the same sample is much
        weaker evidence for a time-of-day claim."""
        assert BEST_TIME_THRESHOLDS[0] > FORMAT_THRESHOLDS[0]
        assert confidence_for(20, FORMAT_THRESHOLDS) == "low"
        assert confidence_for(20, BEST_TIME_THRESHOLDS) == "insufficient_data"


class TestScoreCells:
    def test_buckets_by_weekday_and_hour(self):
        cells, _ = score_cells(
            [row(posted_at=at(0, 9), engagement_rate=0.1),
             row(posted_at=at(0, 9), engagement_rate=0.3),
             row(posted_at=at(2, 18), engagement_rate=0.2)]
        )
        assert len(cells) == 2
        monday = next(c for c in cells if c.weekday == 0)
        assert monday.hour == 9 and monday.post_count == 2

    def test_scores_are_normalised_to_the_peak(self):
        cells, _ = score_cells(
            [row(posted_at=at(0, 9), engagement_rate=0.1),
             row(posted_at=at(1, 9), engagement_rate=0.2)]
        )
        assert max(c.score for c in cells) == 1.0

    def test_a_single_post_slot_never_wins(self):
        """One lucky post is an anecdote. Naming it a best time is precisely
        the overclaim these thresholds exist to prevent."""
        _, best = score_cells(
            [row(posted_at=at(3, 19), engagement_rate=0.9),   # one post, huge
             row(posted_at=at(0, 9), engagement_rate=0.1),
             row(posted_at=at(0, 9), engagement_rate=0.1)]
        )
        assert best == (0, 9)

    def test_no_eligible_slot_returns_no_winner(self):
        _, best = score_cells([row(posted_at=at(0, 9), engagement_rate=0.5)])
        assert best is None

    def test_falls_back_to_interactions_without_engagement_rate(self):
        """Facebook has no reach, so no rate — rank on absolute interactions."""
        _, best = score_cells(
            [row(posted_at=at(4, 20), likes=50), row(posted_at=at(4, 20), likes=70),
             row(posted_at=at(1, 8), likes=1), row(posted_at=at(1, 8), likes=2)]
        )
        assert best == (4, 20)

    def test_posts_without_a_timestamp_are_skipped(self):
        cells, best = score_cells([row(posted_at=None, engagement_rate=0.5)])
        assert cells == [] and best is None

    def test_empty_input_is_not_an_error(self):
        assert score_cells([]) == ([], None)

    def test_all_zero_signal_names_no_winner(self):
        """Regression from real data: a Page synced before insights were
        available scores every slot 0.0, and max() would then pick a slot by
        dictionary order and present it as a recommendation."""
        cells, best = score_cells(
            [row(posted_at=at(0, 9)), row(posted_at=at(0, 9)),
             row(posted_at=at(3, 20)), row(posted_at=at(3, 20))]
        )
        assert len(cells) == 2  # the cells still render, showing the gaps
        assert best is None     # but nothing is claimed

    def test_a_nonzero_slot_still_wins_among_zeros(self):
        _, best = score_cells(
            [row(posted_at=at(0, 9)), row(posted_at=at(0, 9)),
             row(posted_at=at(3, 20), likes=4), row(posted_at=at(3, 20), likes=6)]
        )
        assert best == (3, 20)


class TestBuildFormatRows:
    def _mixed(self):
        return (
            [row(post_type="carousel", engagement_rate=0.2, reach=100) for _ in range(4)]
            + [row(post_type="image", engagement_rate=0.1, reach=100) for _ in range(10)]
        )

    def test_baseline_is_the_most_posted_format_not_the_worst(self):
        """The useful claim is about the user's actual habit."""
        _, baseline = build_format_rows(self._mixed(), "engagement_rate")
        assert baseline == "image"

    def test_lift_is_expressed_against_that_baseline(self):
        rows, _ = build_format_rows(self._mixed(), "engagement_rate")
        carousel = next(r for r in rows if r.post_type == "carousel")
        assert carousel.lift_vs_baseline == pytest.approx(1.0)  # 0.2/0.1 - 1

    def test_rows_are_sorted_best_first(self):
        rows, _ = build_format_rows(self._mixed(), "engagement_rate")
        assert rows[0].post_type == "carousel"

    def test_thin_formats_get_no_lift(self):
        """Fewer than MIN_POSTS_PER_FORMAT and one viral post defines the mean."""
        data = self._mixed() + [row(post_type="reel", engagement_rate=9.0)]
        rows, _ = build_format_rows(data, "engagement_rate")
        reel = next(r for r in rows if r.post_type == "reel")
        assert reel.post_count < MIN_POSTS_PER_FORMAT
        assert reel.lift_vs_baseline is None

    def test_zero_baseline_yields_no_lift_rather_than_infinity(self):
        data = [row(post_type="text", engagement_rate=0.0) for _ in range(5)] + [
            row(post_type="image", engagement_rate=0.5) for _ in range(3)
        ]
        rows, _ = build_format_rows(data, "engagement_rate")
        assert all(r.lift_vs_baseline is None for r in rows)

    def test_interactions_ranking_for_sources_without_reach(self):
        data = [row(post_type="video", likes=100) for _ in range(4)] + [
            row(post_type="text", likes=5) for _ in range(6)
        ]
        rows, baseline = build_format_rows(data, "interactions")
        assert baseline == "text"
        assert rows[0].post_type == "video"
        assert rows[0].avg_interactions == 100.0

    def test_missing_metrics_are_skipped_not_counted_as_zero(self):
        """Averaging None as 0 would invent a measurement that never happened."""
        data = [
            row(post_type="image", engagement_rate=0.4, reach=None),
            row(post_type="image", engagement_rate=None, reach=None),
            row(post_type="image", engagement_rate=0.6, reach=None),
        ]
        rows, _ = build_format_rows(data, "engagement_rate")
        assert rows[0].avg_engagement_rate == pytest.approx(0.5)
        assert rows[0].avg_reach == 0.0

    def test_posts_without_a_type_are_excluded(self):
        rows, _ = build_format_rows([row(post_type=None, engagement_rate=0.5)], "engagement_rate")
        assert rows == []
