"""Tests for the pure parts of ingestion, against realistic Graph API payloads.

No network here. Post classification and engagement maths are where the real
bugs would hide, and both are testable against recorded shapes.
"""

from datetime import timezone

import pytest

from api.v1.models.post import PostType
from api.v1.services.ingestion_service import (
    build_snapshot_values,
    classify_post_type,
    compute_engagement_rate,
    parse_timestamp,
)
from api.v1.services.meta_client import metrics_for


class TestClassifyPostType:
    """Meta splits post kind across two fields; neither alone is sufficient."""

    def test_reel_is_video_with_reels_product_type(self):
        # The important case: a reel IS a VIDEO, so media_type alone mislabels it.
        assert classify_post_type("VIDEO", "REELS") == PostType.REEL

    def test_feed_video_is_not_a_reel(self):
        assert classify_post_type("VIDEO", "FEED") == PostType.VIDEO

    def test_carousel(self):
        assert classify_post_type("CAROUSEL_ALBUM", "FEED") == PostType.CAROUSEL

    def test_image(self):
        assert classify_post_type("IMAGE", "FEED") == PostType.IMAGE

    def test_reels_wins_even_for_a_carousel_album(self):
        assert classify_post_type("CAROUSEL_ALBUM", "REELS") == PostType.REEL

    def test_case_insensitive(self):
        assert classify_post_type("video", "reels") == PostType.REEL

    @pytest.mark.parametrize("media,product", [(None, None), ("SOMETHING_NEW", "FEED")])
    def test_unknown_types_return_none_rather_than_guessing(self, media, product):
        assert classify_post_type(media, product) is None


class TestEngagementRate:
    def test_interactions_over_reach(self):
        rate = compute_engagement_rate(
            reach=1000, likes=30, comments=10, shares=5, saves=5
        )
        assert rate == 0.05

    def test_missing_reach_is_unknown_not_zero(self):
        """A rate with no denominator is unknown. Returning 0.0 would drag
        every average down and quietly corrupt the format comparison."""
        assert compute_engagement_rate(
            reach=None, likes=30, comments=1, shares=0, saves=0
        ) is None

    def test_zero_reach_is_unknown(self):
        assert compute_engagement_rate(
            reach=0, likes=5, comments=0, shares=0, saves=0
        ) is None

    def test_missing_metrics_count_as_zero_interactions(self):
        # Meta omits metrics a media type doesn't support; absent != invalid.
        assert compute_engagement_rate(
            reach=100, likes=10, comments=None, shares=None, saves=None
        ) == 0.1

    def test_no_interactions_is_zero_not_none(self):
        assert compute_engagement_rate(
            reach=500, likes=0, comments=0, shares=0, saves=0
        ) == 0.0


class TestMetricsFor:
    def test_story_omits_unsupported_metrics(self):
        """Requesting an unsupported metric makes Meta reject the entire call,
        so story metrics must stay narrow."""
        metrics = metrics_for("STORY")
        assert "saved" not in metrics
        assert "reach" in metrics

    def test_reels_includes_engagement_metrics(self):
        assert {"likes", "shares", "saved"} <= set(metrics_for("REELS"))

    def test_impressions_is_never_requested(self):
        """Deprecated from Graph API v21 and folded into `views`."""
        for product in ("FEED", "REELS", "STORY", None, "UNKNOWN"):
            assert "impressions" not in metrics_for(product)

    def test_unknown_product_type_falls_back(self):
        assert metrics_for("SOMETHING_NEW") == metrics_for(None)


class TestParseTimestamp:
    def test_parses_meta_format_as_utc(self):
        parsed = parse_timestamp("2026-07-21T09:30:00+0000")
        assert parsed is not None
        assert parsed.year == 2026 and parsed.hour == 9
        assert parsed.tzinfo is not None

    def test_naive_input_is_assumed_utc(self):
        parsed = parse_timestamp("2026-07-21T09:30:00")
        assert parsed is not None and parsed.tzinfo == timezone.utc

    @pytest.mark.parametrize("value", [None, "", "not-a-date"])
    def test_bad_input_returns_none_rather_than_raising(self, value):
        assert parse_timestamp(value) is None


class TestBuildSnapshotValues:
    """Recorded shape of a real IG media object + its insights response."""

    MEDIA = {
        "id": "17925...",
        "caption": "5 things we learned",
        "media_type": "CAROUSEL_ALBUM",
        "media_product_type": "FEED",
        "like_count": 288,
        "comments_count": 41,
    }

    def test_prefers_insight_values_over_media_counts(self):
        values = build_snapshot_values(
            self.MEDIA,
            {"reach": 3201, "likes": 290, "comments": 42, "shares": 33, "saved": 214},
        )
        assert values["likes"] == 290
        assert values["comments"] == 42

    def test_falls_back_to_media_counts_when_insights_omit_them(self):
        values = build_snapshot_values(self.MEDIA, {"reach": 3201})
        assert values["likes"] == 288
        assert values["comments"] == 41

    def test_saved_maps_to_saves(self):
        """Meta calls it `saved`; our column is `saves`."""
        values = build_snapshot_values(self.MEDIA, {"reach": 100, "saved": 12})
        assert values["saves"] == 12

    def test_raw_payload_is_retained(self):
        insights = {"reach": 3201, "views": 5120}
        values = build_snapshot_values(self.MEDIA, insights)
        assert values["raw"]["media"] == self.MEDIA
        assert values["raw"]["insights"] == insights

    def test_empty_insights_still_produces_a_usable_row(self):
        """Insights can fail for one post; the post is still worth recording."""
        values = build_snapshot_values(self.MEDIA, {})
        assert values["reach"] is None
        assert values["engagement_rate"] is None
        assert values["likes"] == 288  # from the media object
