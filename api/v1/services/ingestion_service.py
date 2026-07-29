"""Pull posts and metrics from Meta and persist them.

Split deliberately into pure mapping functions and the orchestration that uses
them, so the parts with real logic — post classification, engagement maths —
are testable against recorded payloads with no network involved.

Metric snapshots are append-only: each run writes a new row rather than
updating the last one. Engagement settles over the first 24-48h, so the history
of how a post accrued is itself the data the insight engine needs.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.security import decrypt_str
from api.utils.logger import logger
from api.v1.models.base_model import utcnow
from api.v1.models.connected_account import AccountStatus, ConnectedAccount
from api.v1.models.post import Post, PostSource, PostType
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


# Facebook describes a post's format in two places, neither complete on its own.
# `attachments.data[0].media_type` is the reliable one when an attachment
# exists; `status_type` covers the posts that have none.
_FB_MEDIA_TYPE_TO_POST_TYPE = {
    "album": PostType.CAROUSEL,
    "photo": PostType.IMAGE,
    "video": PostType.VIDEO,
    "link": PostType.LINK,
}

_FB_STATUS_TYPE_TO_POST_TYPE = {
    "added_photos": PostType.IMAGE,
    "added_video": PostType.VIDEO,
    "shared_story": PostType.LINK,
    "mobile_status_update": PostType.TEXT,
    "wall_post": PostType.TEXT,
}


def classify_facebook_post_type(
    attachment_media_type: str | None,
    status_type: str | None,
    permalink: str | None = None,
) -> str | None:
    """Map a Page post onto our post_type vocabulary.

    Reels need the permalink. On ``published_posts`` a Reel is indistinguishable
    from an ordinary video by its type fields — both report ``media_type:
    video``, ``status_type: added_video``, ``type: video_inline`` — but Meta
    routes them to ``/reel/{id}`` rather than ``/posts/{id}``. Confirmed
    against live data on 2026-07-29. This matters because format comparison is
    one of the product's core insights, and silently folding Reels into "video"
    would hide the format most likely to be outperforming.
    """
    if permalink and "/reel/" in permalink.lower():
        return PostType.REEL

    media = (attachment_media_type or "").lower()
    if media in _FB_MEDIA_TYPE_TO_POST_TYPE:
        return _FB_MEDIA_TYPE_TO_POST_TYPE[media]

    status = (status_type or "").lower()
    if status in _FB_STATUS_TYPE_TO_POST_TYPE:
        return _FB_STATUS_TYPE_TO_POST_TYPE[status]

    # No attachment and no recognised status_type leaves only a text update.
    return PostType.TEXT if not media else None


def facebook_attachment(post: dict[str, Any]) -> dict[str, Any]:
    """First attachment on a Page post, or an empty dict.

    Buried three levels deep (``attachments.data[0]``) and absent entirely on
    text posts, so every read of it needs this guard.
    """
    data = (post.get("attachments") or {}).get("data") or []
    return data[0] if data and isinstance(data[0], dict) else {}


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


def _as_int(value: Any) -> int | None:
    """Ints only. Meta mixes scalars and objects in the same values array."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _summary_count(node: Any) -> int | None:
    """Read ``{...}.summary.total_count`` off a likes/comments edge."""
    if not isinstance(node, dict):
        return None
    summary = node.get("summary")
    if not isinstance(summary, dict):
        return None
    return _as_int(summary.get("total_count"))


def _first_present(*values: int | None) -> int | None:
    """First non-None value, or None. Zero is a real measurement, not a miss."""
    return next((v for v in values if v is not None), None)


def sum_reactions(value: Any) -> int | None:
    """Total a ``post_reactions_by_type_total`` breakdown.

    Arrives as ``{"like": 12, "love": 3, "wow": 1}``. Returns None for anything
    that isn't that shape, so the caller can fall back rather than record a
    fabricated zero.
    """
    if not isinstance(value, dict):
        return None
    return sum(count for count in value.values() if _as_int(count) is not None)


def build_facebook_snapshot_values(
    post: dict[str, Any], insights: dict[str, Any]
) -> dict[str, Any]:
    """Map a Page post plus its insights onto our snapshot columns.

    Four deliberate decisions:

    1. **Reaction total wins over ``likes.summary``.** On a Page post the
       summary count and the reaction breakdown can disagree, and the
       breakdown is the one that explicitly covers every reaction type. They
       are never summed — that would count the same reaction twice.
    2. **``reach`` is always None.** Meta removed post-level reach and
       impressions for Pages; every candidate metric is rejected outright (see
       ``_PAGE_POST_METRICS``). Facebook posts therefore have **no engagement
       rate** — the denominator does not exist — and must be compared on
       absolute interactions. Inventing a denominator from video views would
       produce a number that looks like Instagram's but is not comparable to
       it, which is worse than an honest null.
    3. **``saves`` is always None.** Facebook has no save equivalent. None
       rather than 0, because zero would drag down any average computed over a
       mixed set of posts while claiming to be a measurement.
    4. **Every count has a fallback path.** The post object's like and comment
       summaries need ``pages_read_user_content``; the insights activity
       breakdown does not. Reading both means a Page without that scope still
       records real numbers rather than nulls.
    """
    activity = insights.get("post_activity_by_action_type")
    activity = activity if isinstance(activity, dict) else {}

    reactions = sum_reactions(insights.get("post_reactions_by_type_total"))
    likes = _first_present(
        reactions,
        _as_int(insights.get("post_reactions_like_total")),
        _summary_count(post.get("likes")),
        _as_int(activity.get("like")),
    )
    # The post object's counts need pages_read_user_content; the activity
    # breakdown does not. Trying both means a Page without that scope still
    # gets real comment and share numbers.
    comments = _first_present(
        _summary_count(post.get("comments")), _as_int(activity.get("comment"))
    )
    shares_node = post.get("shares")
    shares = _first_present(
        _as_int(shares_node.get("count")) if isinstance(shares_node, dict) else None,
        _as_int(activity.get("share")),
    )

    # No reach metric exists for Page posts — see _PAGE_POST_METRICS. Video
    # views are the closest thing to an exposure count and only exist on video
    # posts, so views is populated where possible and reach never is.
    reach = None
    views = _as_int(insights.get("post_video_views"))

    return {
        "views": views,
        "reach": reach,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "saves": None,
        "engagement_rate": compute_engagement_rate(
            reach=reach, likes=likes, comments=comments, shares=shares, saves=None
        ),
        "raw": {"post": post, "insights": insights},
    }


def normalize_instagram_post(media: dict[str, Any]) -> dict[str, Any]:
    """Instagram media -> the columns on our posts table."""
    return {
        "platform_post_id": media["id"],
        "source": PostSource.INSTAGRAM,
        "post_type": classify_post_type(
            media.get("media_type"), media.get("media_product_type")
        ),
        "caption": media.get("caption"),
        "posted_at": parse_timestamp(media.get("timestamp")),
        "permalink": media.get("permalink"),
        "media_url": media.get("media_url") or media.get("thumbnail_url"),
    }


def normalize_facebook_post(post: dict[str, Any]) -> dict[str, Any]:
    """Facebook Page post -> the columns on our posts table.

    Every field is named differently from the Instagram equivalent — message
    not caption, created_time not timestamp, permalink_url not permalink —
    which is exactly why both sides normalise here instead of letting the
    difference leak into the upsert.
    """
    attachment = facebook_attachment(post)
    return {
        "platform_post_id": post["id"],
        "source": PostSource.FACEBOOK,
        "post_type": classify_facebook_post_type(
            attachment.get("media_type"),
            post.get("status_type"),
            post.get("permalink_url"),
        ),
        "caption": post.get("message"),
        "posted_at": parse_timestamp(post.get("created_time")),
        "permalink": post.get("permalink_url"),
        "media_url": post.get("full_picture"),
    }


async def _upsert_post(
    db: AsyncSession, account: ConnectedAccount, fields: dict[str, Any]
) -> Post:
    """Insert or refresh the post row. Captions and permalinks can change.

    Takes already-normalised fields, so it is identical for both sources.
    """
    platform_post_id = fields["platform_post_id"]

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

    for column, value in fields.items():
        setattr(post, column, value)

    return post


async def _collect_pages(
    fetch: Any, owner_id: str, token: str
) -> list[dict[str, Any]]:  # pragma: no cover - thin paging loop
    """Walk a cursor-paginated edge up to MAX_PAGES, newest first.

    Both the Instagram media edge and the Page published_posts edge return the
    same envelope shape, so one loop serves both.
    """
    collected: list[dict[str, Any]] = []
    after: str | None = None

    for _ in range(MAX_PAGES):
        payload = await fetch(owner_id, token, limit=PAGE_SIZE, after=after)
        batch = payload.get("data", [])
        collected.extend(batch)

        after = (payload.get("paging", {}).get("cursors", {}) or {}).get("after")
        if not after or len(batch) < PAGE_SIZE:
            break

    return collected


async def _persist(
    db: AsyncSession,
    account: ConnectedAccount,
    fetched: list[tuple[dict[str, Any], dict[str, Any]]],
    normalize: Any,
    build_values: Any,
) -> dict[str, int]:
    """Write already-fetched posts and their metrics.

    Separate from fetching on purpose. Interleaving Graph API calls with an
    open transaction leaves a Postgres session ``idle in transaction`` for the
    entire run — observed at minutes on a small account, since it is one HTTP
    round-trip per post — which pins a pooled connection and holds back
    autovacuum the whole time. Doing all the network first means the
    transaction is open only for the writes.
    """
    posts = snapshots = 0

    for payload, insights in fetched:
        post = await _upsert_post(db, account, normalize(payload))
        # Flush so the new post has an id before the snapshot references it.
        await db.flush()
        posts += 1

        db.add(
            PostMetricsSnapshot(
                post_id=post.id,
                captured_at=utcnow(),
                **build_values(payload, insights),
            )
        )
        snapshots += 1

    return {"posts": posts, "snapshots": snapshots, "skipped": 0}


async def _ingest_instagram(
    db: AsyncSession, account: ConnectedAccount, token: str
) -> dict[str, int]:
    """Sync the account's Instagram media."""
    media_items = await _collect_pages(
        meta_client.get_instagram_media, account.ig_business_id, token
    )

    fetched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    skipped = 0

    for media in media_items:
        if not media.get("id"):
            skipped += 1
            continue
        insights = await meta_client.get_media_insights(
            media["id"], token, media.get("media_product_type")
        )
        fetched.append((media, insights))

    result = await _persist(
        db, account, fetched, normalize_instagram_post, build_snapshot_values
    )
    result["skipped"] = skipped
    return result


def make_page_post_fetcher() -> Any:
    """A Page-posts fetcher that learns, once, whether it may ask for
    like and comment counts.

    Those fields need ``pages_read_user_content``, and one refused field fails
    the whole request. Without this the degraded retry repeats on every page of
    results — a rejected round-trip per page, for an answer that cannot change
    mid-run.
    """
    include_engagement = True

    async def fetch(
        page_id: str, token: str, limit: int = PAGE_SIZE, after: str | None = None
    ) -> dict[str, Any]:
        nonlocal include_engagement
        try:
            return await meta_client.get_page_posts(
                page_id, token, limit=limit, after=after,
                include_engagement=include_engagement,
            )
        except MetaAPIError as exc:
            # Only a permission refusal is worth degrading for. An auth failure
            # or a 5xx means retrying with fewer fields would fail identically.
            if (
                not include_engagement
                or meta_client.is_auth_error(exc)
                or exc.status_code != 400
            ):
                raise
            logger.warning(
                "Page %s refused the like/comment fields — continuing without "
                "them for this run; they need pages_read_user_content (%s)",
                page_id,
                exc,
            )
            include_engagement = False
            return await meta_client.get_page_posts(
                page_id, token, limit=limit, after=after, include_engagement=False
            )

    return fetch


async def _ingest_facebook(
    db: AsyncSession, account: ConnectedAccount, token: str
) -> dict[str, int]:
    """Sync the account's Facebook Page posts."""
    page_posts = await _collect_pages(
        make_page_post_fetcher(), account.fb_page_id, token
    )

    fetched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    skipped = 0

    for page_post in page_posts:
        if not page_post.get("id"):
            skipped += 1
            continue
        insights = await meta_client.get_page_post_insights(page_post["id"], token)
        fetched.append((page_post, insights))

    result = await _persist(
        db, account, fetched, normalize_facebook_post, build_facebook_snapshot_values
    )
    result["skipped"] = skipped
    return result


async def ingest_account(db: AsyncSession, account: ConnectedAccount) -> dict[str, int]:
    """Sync every source this account has. Returns a summary for logging.

    An account may have a Facebook Page, an Instagram Professional account, or
    both. Each is ingested independently: a Page-only account is a perfectly
    normal customer, not a broken Instagram connection, and one source failing
    must not discard what the other already fetched.

    Raises MetaAPIError only when *every* attempted source failed — that is the
    signature of a problem with the connection itself (expired token, API
    down) rather than with one surface. Per-post problems are logged and
    skipped.
    """
    # Page tokens are the right credential for both Page posts and Page-linked
    # Instagram calls; fall back to the user token for accounts connected
    # before we stored one.
    encrypted = account.page_access_token_encrypted or account.access_token_encrypted
    if not encrypted:
        raise MetaAPIError(f"Account {account.id} has no usable access token.")
    token = decrypt_str(encrypted)

    sources: list[tuple[str, Any]] = []
    if account.ig_business_id:
        sources.append((PostSource.INSTAGRAM, _ingest_instagram))
    if account.fb_page_id:
        sources.append((PostSource.FACEBOOK, _ingest_facebook))

    if not sources:
        logger.info(
            "Account %s has neither a Facebook Page nor an Instagram "
            "Professional account; nothing to ingest",
            account.id,
        )
        return {"posts": 0, "snapshots": 0, "skipped": 0}

    totals = {"posts": 0, "snapshots": 0, "skipped": 0}
    failures: list[MetaAPIError] = []

    for name, ingest in sources:
        try:
            result = await ingest(db, account, token)
        except MetaAPIError as exc:
            logger.warning("Ingesting %s for account %s failed: %s", name, account.id, exc)
            failures.append(exc)
            continue
        for key in totals:
            totals[key] += result[key]

    if len(failures) == len(sources):
        exc = failures[0]
        # Only a genuinely dead token becomes token_expired. A permission error
        # is also an HTTP 400, and flagging it as expired would tell the user to
        # reconnect — which cannot fix a missing scope and just loops them
        # through the consent dialog.
        if meta_client.is_auth_error(exc):
            account.status = AccountStatus.TOKEN_EXPIRED
            await db.commit()
            logger.warning("Marking account %s token_expired: %s", account.id, exc)
        raise exc

    account.last_ingested_at = utcnow()
    if account.status == AccountStatus.TOKEN_EXPIRED:
        account.status = AccountStatus.ACTIVE

    await db.commit()

    logger.info("Ingested account %s: %s", account.id, totals)
    return totals
