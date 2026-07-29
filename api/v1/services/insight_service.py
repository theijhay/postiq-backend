"""Turn stored metrics into prescriptive insights (PROJECT_SPEC.md §12).

Split into pure scoring functions and the queries that feed them, so the
statistics are testable without a database.

The governing principle: **say "not enough data" rather than assert something
the sample cannot support.** A small account posts perhaps thirty times a
month. Best-time spreads that over 168 weekly slots, so the modal slot holds
zero posts and the best slot might hold one. Reporting "post at 7pm Thursday"
off a single lucky post is worse than reporting nothing — it is confident,
wrong, and unfalsifiable to the user.
"""

import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.models.base_model import utcnow
from api.v1.models.post import Post, PostSource
from api.v1.models.post_metrics_snapshot import PostMetricsSnapshot
from api.v1.schemas.insight import (
    AccountSummary,
    BestTimeCell,
    BestTimeInsight,
    FormatComparisonInsight,
    FormatComparisonRow,
    InsightConfidence,
    SummaryMetric,
    TrendPoint,
)
from api.v1.schemas.post import PostMetrics, PostResponse

# Sample-size floors. Deliberately conservative, and deliberately different per
# insight: best-time partitions the sample across 168 slots, format comparison
# across at most six. The same 30 posts are therefore far weaker evidence for a
# time-of-day claim than for a format claim.
BEST_TIME_THRESHOLDS = (30, 60, 120)
FORMAT_THRESHOLDS = (15, 30, 60)

# A format needs at least this many posts before it is worth ranking. Below it
# a single viral post defines the whole average.
MIN_POSTS_PER_FORMAT = 3

# Insights are drawn from a rolling window; older posts describe an audience
# and an algorithm that no longer exist.
DEFAULT_WINDOW_DAYS = 90


def confidence_for(sample_size: int, thresholds: tuple[int, int, int]) -> InsightConfidence:
    """Map a sample size onto a confidence band.

    Thresholds are (low, moderate, high) floors. Anything under the first is
    ``insufficient_data``, which the UI renders as "keep posting" rather than
    as an insight.
    """
    low, moderate, high = thresholds
    if sample_size < low:
        return "insufficient_data"
    if sample_size < moderate:
        return "low"
    if sample_size < high:
        return "moderate"
    return "high"


def _mean(values: Iterable[float | None]) -> float | None:
    """Average of the present values. None when nothing is measured.

    Absent metrics are skipped rather than counted as zero — Facebook has no
    reach at all, and treating that as reach=0 would invent a denominator and
    drag every average toward zero while looking like a measurement.
    """
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _interactions(row: Any) -> int:
    """Total interactions on a snapshot, treating absent counts as zero.

    Zero is the right default here, unlike in ``_mean``: a post with no
    recorded shares genuinely contributed no shares to the total.
    """
    return sum(
        int(getattr(row, name) or 0) for name in ("likes", "comments", "shares", "saves")
    )


def score_cells(
    rows: Sequence[Any],
) -> tuple[list[BestTimeCell], tuple[int, int] | None]:
    """Bucket posts into weekday/hour cells and normalise their scores 0-1.

    Ranks on engagement rate where it exists and on absolute interactions where
    it does not, because Facebook posts have no reach and therefore no rate.
    Returns the cells plus the best (weekday, hour), or None when no cell has
    enough posts behind it to name.
    """
    buckets: dict[tuple[int, int], list[float]] = defaultdict(list)

    for row in rows:
        if row.posted_at is None:
            continue
        value = (
            float(row.engagement_rate)
            if row.engagement_rate is not None
            else float(_interactions(row))
        )
        buckets[(row.posted_at.weekday(), row.posted_at.hour)].append(value)

    if not buckets:
        return [], None

    averages = {key: sum(v) / len(v) for key, v in buckets.items()}
    peak = max(averages.values()) or 1.0

    cells = [
        BestTimeCell(
            weekday=weekday,
            hour=hour,
            score=round(averages[(weekday, hour)] / peak, 4),
            post_count=len(buckets[(weekday, hour)]),
        )
        for weekday, hour in sorted(buckets)
    ]

    # Only name a winner backed by more than one post AND by a non-zero signal.
    #
    # The post-count rule stops a single lucky post from defining a best time.
    # The non-zero rule stops something subtler that real data exposed: when
    # metrics are missing — a Facebook Page synced before insights were
    # available, say — every slot scores 0.0, `max` picks an arbitrary one, and
    # we confidently recommend a time chosen by dictionary ordering. A
    # degenerate signal has no best slot, and saying so is the honest answer.
    eligible = {
        k: v for k, v in averages.items() if len(buckets[k]) >= 2 and v > 0
    }
    best = max(eligible, key=lambda k: eligible[k]) if eligible else None
    return cells, best


def build_format_rows(
    rows: Sequence[Any], ranked_by: str
) -> tuple[list[FormatComparisonRow], str | None]:
    """Average each format's performance and express it as lift over a baseline.

    The baseline is the *most frequently posted* format, not the worst — the
    useful claim is "the thing you rarely do beats the thing you always do",
    which is only meaningful against the user's actual habit.
    """
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        if row.post_type:
            grouped[row.post_type].append(row)

    built: list[FormatComparisonRow] = []
    for post_type, group in grouped.items():
        built.append(
            FormatComparisonRow(
                post_type=post_type,
                post_count=len(group),
                avg_engagement_rate=round(
                    _mean(r.engagement_rate for r in group) or 0.0, 6
                ),
                avg_reach=round(_mean(r.reach for r in group) or 0.0, 2),
                avg_saves=round(_mean(r.saves for r in group) or 0.0, 2),
                avg_interactions=round(_mean(_interactions(r) for r in group) or 0.0, 2),
            )
        )

    rankable = [r for r in built if r.post_count >= MIN_POSTS_PER_FORMAT]
    if not rankable:
        return sorted(built, key=lambda r: r.post_count, reverse=True), None

    def measure(row: FormatComparisonRow) -> float:
        return (
            row.avg_engagement_rate
            if ranked_by == "engagement_rate"
            else row.avg_interactions
        )

    baseline = max(rankable, key=lambda r: r.post_count)
    baseline_value = measure(baseline)

    for row in built:
        # Lift is undefined against a zero baseline; None says so rather than
        # dividing and reporting an infinity as a recommendation.
        if baseline_value > 0 and row.post_count >= MIN_POSTS_PER_FORMAT:
            row.lift_vs_baseline = round(measure(row) / baseline_value - 1, 4)

    built.sort(key=measure, reverse=True)
    return built, baseline.post_type


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _format_hour(hour: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    display = hour % 12 or 12
    return f"{display}{suffix}"


async def _dominant_source(db: AsyncSession, account_id: uuid.UUID) -> str:
    """The source with the most posts, used when the caller doesn't pick one.

    Insights are computed per source and never pooled (see the schema), so a
    default has to be chosen somewhere; the surface the user posts to most is
    the one they came to look at.
    """
    result = (
        await db.execute(
            select(Post.source, func.count())
            .where(Post.connected_account_id == account_id)
            .group_by(Post.source)
            .order_by(func.count().desc())
        )
    ).first()
    return result[0] if result else PostSource.INSTAGRAM


async def _windowed_rows(
    db: AsyncSession, account_id: uuid.UUID, source: str, days: int
) -> Sequence[Any]:
    """Latest snapshot per post, for one source, inside the window.

    DISTINCT ON picks the most recent capture per post in a single
    index-ordered pass rather than a correlated subquery per row.
    """
    latest = (
        select(PostMetricsSnapshot)
        .distinct(PostMetricsSnapshot.post_id)
        .order_by(
            PostMetricsSnapshot.post_id, PostMetricsSnapshot.captured_at.desc()
        )
        .subquery()
    )

    return (
        await db.execute(
            select(
                Post.id,
                Post.post_type,
                Post.posted_at,
                latest.c.reach,
                latest.c.views,
                latest.c.likes,
                latest.c.comments,
                latest.c.shares,
                latest.c.saves,
                latest.c.engagement_rate,
            )
            .outerjoin(latest, latest.c.post_id == Post.id)
            .where(
                Post.connected_account_id == account_id,
                Post.source == source,
                Post.posted_at.isnot(None),
                Post.posted_at >= utcnow() - timedelta(days=days),
            )
        )
    ).all()


def _ranking_measure(rows: Sequence[Any]) -> str:
    """Rate where reach exists, absolute interactions where it does not."""
    return (
        "engagement_rate"
        if any(r.engagement_rate is not None for r in rows)
        else "interactions"
    )


async def compute_best_time(
    db: AsyncSession,
    account_id: uuid.UUID,
    source: str | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    timezone_name: str = "UTC",
) -> BestTimeInsight:
    source = source or await _dominant_source(db, account_id)
    rows = await _windowed_rows(db, account_id, source, days)
    cells, best = score_cells(rows)
    confidence = confidence_for(len(rows), BEST_TIME_THRESHOLDS)

    recommendation = None
    if confidence != "insufficient_data" and best is not None:
        weekday, hour = best
        recommendation = (
            f"Your {source.capitalize()} posts do best around "
            f"{_format_hour(hour)} on {WEEKDAYS[weekday]}."
        )

    return BestTimeInsight(
        id=f"best-time:{account_id}:{source}",
        generated_at=utcnow(),
        sample_size=len(rows),
        confidence=confidence,
        source=source,
        timezone=timezone_name,
        cells=cells,
        recommendation=recommendation,
    )


async def compute_format_comparison(
    db: AsyncSession,
    account_id: uuid.UUID,
    source: str | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
) -> FormatComparisonInsight:
    source = source or await _dominant_source(db, account_id)
    rows = await _windowed_rows(db, account_id, source, days)
    ranked_by = _ranking_measure(rows)
    built, baseline = build_format_rows(rows, ranked_by)
    confidence = confidence_for(len(rows), FORMAT_THRESHOLDS)

    recommendation = None
    if confidence != "insufficient_data" and baseline and built:
        top = built[0]
        if top.post_type != baseline and top.lift_vs_baseline:
            recommendation = (
                f"{top.post_type.capitalize()} posts earn "
                f"{top.lift_vs_baseline + 1:.1f}x what your {baseline} posts do. "
                f"You published {top.post_count} of them against "
                f"{next(r.post_count for r in built if r.post_type == baseline)} "
                f"{baseline} posts."
            )

    return FormatComparisonInsight(
        id=f"format-comparison:{account_id}:{source}",
        generated_at=utcnow(),
        sample_size=len(rows),
        confidence=confidence,
        source=source,
        baseline_post_type=baseline,
        ranked_by=ranked_by,
        rows=built,
        recommendation=recommendation,
    )


_METRIC_LABELS = {
    "reach": "Reach",
    "engagement_rate": "Engagement rate",
    "saves": "Saves",
    "views": "Views",
}


async def compute_summary(
    db: AsyncSession,
    account_id: uuid.UUID,
    days: int = 30,
    source: str | None = None,
) -> AccountSummary:
    """Headline metrics, a daily trend, and the best posts in the window."""
    source = source or await _dominant_source(db, account_id)
    current = await _windowed_rows(db, account_id, source, days)
    # The preceding window of equal length, for the delta.
    previous_all = await _windowed_rows(db, account_id, source, days * 2)
    cutoff = utcnow() - timedelta(days=days)
    previous = [r for r in previous_all if r.posted_at and r.posted_at < cutoff]

    daily: dict[str, list[Any]] = defaultdict(list)
    for row in current:
        daily[row.posted_at.date().isoformat()].append(row)

    trend = [
        TrendPoint(
            date=day,
            reach=int(_mean(r.reach for r in rows) or 0) or None,
            views=int(_mean(r.views for r in rows) or 0) or None,
            engagement_rate=_mean(r.engagement_rate for r in rows),
        )
        for day, rows in sorted(daily.items())
    ]

    metrics: list[SummaryMetric] = []
    for key in ("reach", "engagement_rate", "saves", "views"):
        value = _mean(getattr(r, key) for r in current)
        prior = _mean(getattr(r, key) for r in previous)
        metrics.append(
            SummaryMetric(
                key=key,  # type: ignore[arg-type]
                label=_METRIC_LABELS[key],
                value=round(value, 6) if value is not None else None,
                # Delta needs both a prior period and a non-zero base; None
                # means "no comparison available", not "no change".
                delta=(
                    round(value / prior - 1, 4)
                    if value is not None and prior
                    else None
                ),
                spark=[
                    point.engagement_rate if key == "engagement_rate" else 0.0
                    for point in trend
                    if point.engagement_rate is not None
                ]
                if key == "engagement_rate"
                else [],
            )
        )

    top = sorted(
        current,
        key=lambda r: (
            float(r.engagement_rate) if r.engagement_rate is not None else 0.0,
            _interactions(r),
        ),
        reverse=True,
    )[:5]

    post_rows = {
        p.id: p
        for p in (
            await db.execute(select(Post).where(Post.id.in_([r.id for r in top])))
        ).scalars()
    }

    top_posts: list[PostResponse] = []
    for row in top:
        post = post_rows.get(row.id)
        if post is None:
            continue
        response = PostResponse.model_validate(post)
        response.metrics = PostMetrics(
            views=row.views,
            reach=row.reach,
            likes=row.likes,
            comments=row.comments,
            shares=row.shares,
            saves=row.saves,
            engagement_rate=(
                float(row.engagement_rate) if row.engagement_rate is not None else None
            ),
        )
        top_posts.append(response)

    return AccountSummary(
        account_id=account_id,
        period_days=days,
        source=source,
        metrics=metrics,
        trend=trend,
        top_posts=top_posts,
    )
