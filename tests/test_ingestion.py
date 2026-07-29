"""Tests for the pure parts of ingestion, against realistic Graph API payloads.

No network here. Post classification and engagement maths are where the real
bugs would hide, and both are testable against recorded shapes.
"""

from datetime import timezone

import pytest

from api.v1.models.post import PostSource, PostType
from api.v1.services.ingestion_service import (
    build_facebook_snapshot_values,
    build_snapshot_values,
    classify_facebook_post_type,
    classify_post_type,
    compute_engagement_rate,
    facebook_attachment,
    normalize_facebook_post,
    normalize_instagram_post,
    parse_timestamp,
    sum_reactions,
)
from api.v1.services.meta_client import MetaAPIError, is_auth_error, metrics_for


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


class TestClassifyFacebookPostType:
    """Facebook describes format across two fields, neither complete alone."""

    @pytest.mark.parametrize(
        "media_type,expected",
        [
            ("photo", PostType.IMAGE),
            ("album", PostType.CAROUSEL),
            ("video", PostType.VIDEO),
            ("link", PostType.LINK),
        ],
    )
    def test_attachment_media_type_wins(self, media_type, expected):
        assert classify_facebook_post_type(media_type, None) == expected

    def test_attachment_beats_status_type(self):
        """status_type is the fallback, not a tiebreaker."""
        assert (
            classify_facebook_post_type("video", "added_photos") == PostType.VIDEO
        )

    @pytest.mark.parametrize(
        "status_type,expected",
        [
            ("added_photos", PostType.IMAGE),
            ("added_video", PostType.VIDEO),
            ("shared_story", PostType.LINK),
            ("mobile_status_update", PostType.TEXT),
        ],
    )
    def test_status_type_used_when_no_attachment(self, status_type, expected):
        assert classify_facebook_post_type(None, status_type) == expected

    def test_no_attachment_and_no_status_is_a_text_post(self):
        assert classify_facebook_post_type(None, None) == PostType.TEXT

    def test_unrecognised_attachment_returns_none_rather_than_guessing(self):
        assert classify_facebook_post_type("event", None) is None

    def test_case_insensitive(self):
        assert classify_facebook_post_type("PHOTO", None) == PostType.IMAGE


class TestFacebookAttachment:
    def test_reads_first_attachment(self):
        post = {"attachments": {"data": [{"media_type": "photo"}]}}
        assert facebook_attachment(post) == {"media_type": "photo"}

    @pytest.mark.parametrize(
        "post",
        [
            {},
            {"attachments": {}},
            {"attachments": {"data": []}},
            {"attachments": None},
            {"attachments": {"data": ["not-a-dict"]}},
        ],
    )
    def test_missing_or_malformed_yields_empty_dict(self, post):
        assert facebook_attachment(post) == {}


class TestSumReactions:
    def test_totals_every_reaction_type(self):
        assert sum_reactions({"like": 12, "love": 3, "wow": 1}) == 16

    def test_empty_breakdown_is_zero(self):
        assert sum_reactions({}) == 0

    @pytest.mark.parametrize("value", [None, 12, "like", []])
    def test_non_mapping_is_unknown_not_zero(self, value):
        assert sum_reactions(value) is None

    def test_ignores_non_integer_counts(self):
        assert sum_reactions({"like": 5, "bogus": "x"}) == 5


class TestBuildFacebookSnapshotValues:
    """Shape follows the published_posts + insights payloads we request."""

    POST = {
        "id": "104177568454287_9988776655",
        "message": "New drop today",
        "created_time": "2026-07-20T10:15:00+0000",
        "permalink_url": "https://facebook.com/104177568454287/posts/9988776655",
        "status_type": "added_photos",
        "attachments": {"data": [{"media_type": "photo"}]},
        "likes": {"summary": {"total_count": 40}},
        "comments": {"summary": {"total_count": 7}},
        "shares": {"count": 3},
    }

    def test_reaction_breakdown_beats_likes_summary(self):
        """The breakdown covers every reaction type; they are never summed."""
        values = build_facebook_snapshot_values(
            self.POST, {"post_reactions_by_type_total": {"like": 40, "love": 5}}
        )
        assert values["likes"] == 45

    def test_falls_back_to_likes_summary_without_the_breakdown(self):
        values = build_facebook_snapshot_values(self.POST, {})
        assert values["likes"] == 40

    def test_reads_comments_and_shares_off_the_post(self):
        values = build_facebook_snapshot_values(self.POST, {})
        assert values["comments"] == 7
        assert values["shares"] == 3

    def test_video_views_populate_views(self):
        values = build_facebook_snapshot_values(self.POST, {"post_video_views": 900})
        assert values["views"] == 900

    def test_reach_is_always_none(self):
        """Meta removed post-level reach for Pages — every candidate metric is
        rejected with error #100. Verified against the live API 2026-07-29."""
        values = build_facebook_snapshot_values(
            self.POST, {"post_impressions": 900, "post_impressions_unique": 500}
        )
        assert values["reach"] is None

    def test_saves_is_unknown_not_zero(self):
        """Facebook has no save; 0 would pollute averages over mixed sources."""
        assert build_facebook_snapshot_values(self.POST, {})["saves"] is None

    def test_engagement_rate_is_none_without_a_denominator(self):
        """No reach means no rate. A fabricated denominator would produce a
        number that looks like Instagram's but cannot be compared to it."""
        assert build_facebook_snapshot_values(self.POST, {})["engagement_rate"] is None

    def test_activity_breakdown_supplies_comments_and_shares(self):
        """The route to these counts without pages_read_user_content."""
        bare = {"id": self.POST["id"]}
        values = build_facebook_snapshot_values(
            bare,
            {"post_activity_by_action_type": {"like": 40, "comment": 7, "share": 3}},
        )
        assert (values["likes"], values["comments"], values["shares"]) == (40, 7, 3)

    def test_post_object_counts_beat_the_activity_breakdown(self):
        values = build_facebook_snapshot_values(
            self.POST,
            {"post_activity_by_action_type": {"comment": 99, "share": 99}},
        )
        assert values["comments"] == 7 and values["shares"] == 3

    def test_zero_from_the_breakdown_is_kept_not_treated_as_missing(self):
        values = build_facebook_snapshot_values(
            {"id": "x"}, {"post_activity_by_action_type": {"comment": 0}}
        )
        assert values["comments"] == 0

    def test_missing_shares_key_is_none(self):
        """Meta omits `shares` entirely when nothing shared the post."""
        post = {k: v for k, v in self.POST.items() if k != "shares"}
        assert build_facebook_snapshot_values(post, {})["shares"] is None

    def test_empty_insights_still_produces_a_usable_row(self):
        """Insights are the volatile half; the post's own counts still land."""
        values = build_facebook_snapshot_values(self.POST, {})
        assert values["reach"] is None
        assert values["engagement_rate"] is None
        assert values["likes"] == 40

    def test_raw_payload_is_retained(self):
        insights = {"post_impressions": 900}
        values = build_facebook_snapshot_values(self.POST, insights)
        assert values["raw"]["post"] == self.POST
        assert values["raw"]["insights"] == insights


class TestNormalizePost:
    """Both sources must land on the same column names."""

    def test_facebook_field_names_are_translated(self):
        fields = normalize_facebook_post(TestBuildFacebookSnapshotValues.POST)
        assert fields["source"] == PostSource.FACEBOOK
        assert fields["caption"] == "New drop today"
        assert fields["post_type"] == PostType.IMAGE
        assert fields["permalink"].endswith("/posts/9988776655")
        assert fields["posted_at"].tzinfo is not None

    def test_instagram_is_tagged_with_its_source(self):
        fields = normalize_instagram_post(
            {"id": "1", "media_type": "IMAGE", "timestamp": "2026-07-20T10:15:00+0000"}
        )
        assert fields["source"] == PostSource.INSTAGRAM
        assert fields["post_type"] == PostType.IMAGE

    def test_both_sources_produce_identical_column_sets(self):
        """A drift here would silently stop writing one source's columns."""
        assert set(normalize_instagram_post({"id": "1"})) == set(
            normalize_facebook_post({"id": "2"})
        )

    def test_text_only_facebook_post_has_no_media_url(self):
        fields = normalize_facebook_post({"id": "2", "message": "hello"})
        assert fields["media_url"] is None
        assert fields["post_type"] == PostType.TEXT


class TestFacebookReelDetection:
    """A Reel is only distinguishable by its permalink on published_posts.

    Shape recorded from live data on 2026-07-29: Meta reports a Reel as
    media_type=video, status_type=added_video, type=video_inline — identical to
    an ordinary video post — but routes it to /reel/ instead of /posts/.
    """

    REEL = {
        "id": "104177568454287_1025702073688527",
        "message": "If you're a junior following seniors...",
        "created_time": "2026-07-29T12:53:18+0000",
        "permalink_url": "https://www.facebook.com/reel/1025702073688527/",
        "status_type": "added_video",
        "attachments": {"data": [{"media_type": "video", "type": "video_inline"}]},
    }

    def test_reel_permalink_beats_the_video_type_fields(self):
        assert normalize_facebook_post(self.REEL)["post_type"] == PostType.REEL

    def test_ordinary_video_post_stays_a_video(self):
        post = dict(
            self.REEL,
            permalink_url="https://www.facebook.com/104177568454287/posts/998877",
        )
        assert normalize_facebook_post(post)["post_type"] == PostType.VIDEO

    def test_missing_permalink_falls_back_to_type_fields(self):
        assert classify_facebook_post_type("video", "added_video", None) == PostType.VIDEO

    def test_match_is_case_insensitive(self):
        assert (
            classify_facebook_post_type(
                "video", None, "https://www.facebook.com/REEL/123/"
            )
            == PostType.REEL
        )

    def test_reel_in_a_page_name_does_not_false_positive(self):
        """Only the /reel/ path segment counts, not the substring anywhere."""
        post = dict(
            self.REEL,
            permalink_url="https://www.facebook.com/reelmagic/posts/998877",
        )
        assert normalize_facebook_post(post)["post_type"] == PostType.VIDEO


class TestIsAuthError:
    """Permission errors and dead tokens are both HTTP 400."""

    def test_invalid_token_code_is_an_auth_error(self):
        assert is_auth_error(MetaAPIError("expired", 400, 190))

    def test_session_expired_code_is_an_auth_error(self):
        assert is_auth_error(MetaAPIError("session", 400, 102))

    def test_401_is_an_auth_error_whatever_the_code(self):
        assert is_auth_error(MetaAPIError("nope", 401, None))

    def test_permission_error_is_not_an_auth_error(self):
        """Code 10 means 'grant more scopes', not 'reconnect'. Marking this
        token_expired loops the user through consent for no reason."""
        assert not is_auth_error(MetaAPIError("needs pages_read_user_content", 400, 10))

    def test_unknown_400_is_not_treated_as_expiry(self):
        assert not is_auth_error(MetaAPIError("something else", 400, None))
