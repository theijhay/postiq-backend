"""Pull posts and metrics from Meta and persist them.

Split deliberately into pure mapping functions and the orchestration that uses
them, so the parts with real logic — post classification, engagement maths —
are testable against recorded payloads with no network involved.

Metric snapshots are append-only: each run writes a new row rather than
updating the last one. Engagement settles over the first 24-48h, so the history
of how a post accrued is itself the data the insight engine needs.
"""

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.security import decrypt_str
from api.utils.logger import logger
from api.v1.models.base_model import utcnow
from api.v1.models.connected_account import AccountStatus, ConnectedAccount
from api.v1.models.post import Post, PostType
from api.v1.models.post_metrics_snapshot import PostMetricsSnapshot
from api.v1.services import meta_client
from api.v1.services.meta_client import MetaAPIError

# How many media pages to walk in one run. 10 x 50 = 500 posts, comfortably
# more than a small business publishes between syncs, while bounding the
# number of Graph calls a single job can make.
MAX_PAGES = 10
PAGE_SIZE = 50


def classify_post_type(
    media_type: str | None, media_product_type: str | None
) -> str | None:
    """Map Meta's two overlapping type fields onto our single post_type.

    Meta splits this across ``media_type`` (IMAGE / VIDEO / CAROUSEL_ALBUM) and
    ``media_product_type`` (FEED / REELS / STORY). A reel is a VIDEO whose
    product type is REELS, so neither field alone is enough.
    """
    product = (media_product_type or "").upper()
    media = (media_type or "").upper()

    if product == "REELS":
        return PostType.REEL
    if media == "CAROUSEL_ALBUM":
        return PostType.CAROUSEL
    if media == "VIDEO":
        return PostType.VIDEO
    if media == "IMAGE":
        return PostType.IMAGE
    return None


def compute_engagement_rate(
    *,
    reach: int | None,
    likes: int | None,
    comments: int | None,
    shares: int | None,
    saves: int | None,
) -> float | None:
    """Interactions divided by reach.

    Reach rather than follower count: it measures how well the post did with
    the people who actually saw it, which is the question the insight engine
    asks. Returns None when reach is missing or zero — a rate with no
    denominator is not zero, it is unknown, and the difference matters when
    these get averaged.
    """
    if not reach or reach <= 0:
        return None

    interactions = sum(value or 0 for value in (likes, comments, shares, saves))
    return round(interactions / reach, 6)


def parse_timestamp(value: str | None) -> datetime | None:
    """Meta sends ISO-8601 with a +0000 offset."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable timestamp from Meta: %r", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_snapshot_values(
    media: dict[str, Any], insights: dict[str, int]
) -> dict[str, Any]:
    """Combine insight metrics with the counts that ride on the media object.

    ``like_count``/``comments_count`` come back on the media itself and are the
    fallback when a media type doesn't expose the matching insight metric.
    """
    likes = insights.get("likes", media.get("like_count"))
    comments = insights.get("comments", media.get("comments_count"))
    reach = insights.get("reach")
    shares = insights.get("shares")
    saves = insights.get("saved")
    views = insights.get("views")

    return {
        "views": views,
        "reach": reach,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "saves": saves,
        "engagement_rate": compute_engagement_rate(
            reach=reach, likes=likes, comments=comments, shares=shares, saves=saves
        ),
        # Keep the untouched payload — Meta renames metrics roughly annually and
        # this turns the next deprecation into a backfill, not data loss.
        "raw": {"media": media, "insights": insights},
    }


async def _upsert_post(
    db: AsyncSession, account: ConnectedAccount, media: dict[str, Any]
) -> Post:
    """Insert or refresh the post row. Captions and permalinks can change."""
    platform_post_id = media["id"]

    post = (
        await db.execute(
            select(Post).where(
                Post.connected_account_id == account.id,
                Post.platform_post_id == platform_post_id,
            )
        )
    ).scalar_one_or_none()

    if post is None:
        post = Post(
            connected_account_id=account.id, platform_post_id=platform_post_id
        )
        db.add(post)

    post.post_type = classify_post_type(
        media.get("media_type"), media.get("media_product_type")
    )
    post.caption = media.get("caption")
    post.posted_at = parse_timestamp(media.get("timestamp"))
    post.permalink = media.get("permalink")
    post.media_url = media.get("media_url") or media.get("thumbnail_url")

    return post


async def _iter_media(
    ig_user_id: str, token: str
) -> Iterable[dict[str, Any]]:  # pragma: no cover - thin paging loop
    """Walk media pages up to MAX_PAGES, newest first."""
    collected: list[dict[str, Any]] = []
    after: str | None = None

    for _ in range(MAX_PAGES):
        payload = await meta_client.get_instagram_media(
            ig_user_id, token, limit=PAGE_SIZE, after=after
        )
        batch = payload.get("data", [])
        collected.extend(batch)

        after = (payload.get("paging", {}).get("cursors", {}) or {}).get("after")
        if not after or len(batch) < PAGE_SIZE:
            break

    return collected


async def ingest_account(db: AsyncSession, account: ConnectedAccount) -> dict[str, int]:
    """Sync one connected account. Returns a small summary for logging.

    Raises MetaAPIError only for failures that make the whole run pointless
    (expired token, unreachable API). Per-post problems are logged and skipped.
    """
    if not account.ig_business_id:
        logger.info(
            "Account %s has no linked Instagram Professional account; nothing to ingest",
            account.id,
        )
        return {"posts": 0, "snapshots": 0, "skipped": 0}

    # Page tokens are the right credential for Page-linked IG calls; fall back
    # to the user token for accounts connected before we stored one.
    encrypted = account.page_access_token_encrypted or account.access_token_encrypted
    if not encrypted:
        raise MetaAPIError(f"Account {account.id} has no usable access token.")
    token = decrypt_str(encrypted)

    try:
        media_items = await _iter_media(account.ig_business_id, token)
    except MetaAPIError as exc:
        # An expired or revoked token is the common cause; surface it to the
        # user as a reconnect prompt rather than failing silently for weeks.
        if exc.status_code in (400, 401, 403):
            account.status = AccountStatus.TOKEN_EXPIRED
            await db.commit()
            logger.warning("Marking account %s token_expired: %s", account.id, exc)
        raise

    posts = 0
    snapshots = 0
    skipped = 0

    for media in media_items:
        if not media.get("id"):
            skipped += 1
            continue

        post = await _upsert_post(db, account, media)
        # Flush so the new post has an id before the snapshot references it.
        await db.flush()
        posts += 1

        insights = await meta_client.get_media_insights(
            media["id"], token, media.get("media_product_type")
        )
        db.add(
            PostMetricsSnapshot(
                post_id=post.id,
                captured_at=utcnow(),
                **build_snapshot_values(media, insights),
            )
        )
        snapshots += 1

    account.last_ingested_at = utcnow()
    if account.status == AccountStatus.TOKEN_EXPIRED:
        account.status = AccountStatus.ACTIVE

    await db.commit()

    summary = {"posts": posts, "snapshots": snapshots, "skipped": skipped}
    logger.info("Ingested account %s: %s", account.id, summary)
    return summary
