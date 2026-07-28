"""Read-side queries for posts and their latest metrics."""

import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.models.post import Post
from api.v1.models.post_metrics_snapshot import PostMetricsSnapshot
from api.v1.schemas.post import PaginatedPosts, PostMetrics, PostResponse


def _latest_snapshot_subquery() -> Select:
    """One row per post: its most recent snapshot.

    DISTINCT ON is Postgres-specific but is the right tool here — it lets the
    database pick the latest capture per post in a single index-ordered pass
    over ix_post_metrics_post_captured, instead of a window function or a
    correlated subquery per row.
    """
    return (
        select(PostMetricsSnapshot)
        .distinct(PostMetricsSnapshot.post_id)
        .order_by(
            PostMetricsSnapshot.post_id,
            PostMetricsSnapshot.captured_at.desc(),
        )
        .subquery()
    )


async def list_posts(
    db: AsyncSession,
    connected_account_id: uuid.UUID,
    page: int = 1,
    page_size: int = 25,
) -> PaginatedPosts:
    page = max(1, page)
    page_size = min(max(1, page_size), 100)

    total = (
        await db.execute(
            select(func.count())
            .select_from(Post)
            .where(Post.connected_account_id == connected_account_id)
        )
    ).scalar_one()

    latest = _latest_snapshot_subquery()

    rows = (
        await db.execute(
            select(Post, latest)
            .outerjoin(latest, latest.c.post_id == Post.id)
            .where(Post.connected_account_id == connected_account_id)
            # Nulls last: a post we haven't dated yet shouldn't top the list.
            .order_by(Post.posted_at.desc().nullslast())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    items: list[PostResponse] = []
    for row in rows:
        post = row[0]
        response = PostResponse.model_validate(post)
        if row.post_id is not None:
            response.metrics = PostMetrics(
                views=row.views,
                reach=row.reach,
                likes=row.likes,
                comments=row.comments,
                shares=row.shares,
                saves=row.saves,
                engagement_rate=(
                    float(row.engagement_rate)
                    if row.engagement_rate is not None
                    else None
                ),
            )
        items.append(response)

    return PaginatedPosts(
        items=items, total=total, page=page, page_size=page_size
    )
