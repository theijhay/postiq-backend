"""Insight payloads (PROJECT_SPEC.md §12).

``confidence`` and ``sample_size`` ride on every insight and are not
decorative. A small account posts perhaps thirty times a month, and best-time
spreads those over 168 weekly slots — most slots hold zero or one post. Without
a confidence signal the UI would state a preference it cannot support, which is
worse than showing nothing: the whole product promise is prescriptive advice,
and advice drawn from one post is how you lose a user's trust permanently.
"""

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

from api.v1.schemas.post import PostResponse

InsightConfidence = Literal["insufficient_data", "low", "moderate", "high"]


class InsightBase(BaseModel):
    id: str
    generated_at: datetime
    sample_size: int
    confidence: InsightConfidence
    # Which surface this was computed over. Insights are never pooled across
    # sources — see BestTimeInsight for why.
    source: str


class BestTimeCell(BaseModel):
    weekday: int  # 0 = Monday
    hour: int  # 0-23
    score: float  # normalised 0-1
    post_count: int


class BestTimeInsight(InsightBase):
    """When to post, by weekday and hour.

    Computed for a single source. Instagram and Facebook audiences are ranked
    by different systems and behave differently by hour, so a combined heatmap
    would describe neither.
    """

    insight_type: Literal["best_time"] = "best_time"
    timezone: str
    cells: list[BestTimeCell]
    recommendation: Optional[str] = None


class FormatComparisonRow(BaseModel):
    post_type: str
    post_count: int
    avg_engagement_rate: float
    avg_reach: float
    avg_saves: float
    # Absolute interactions, which is the only comparable measure on Facebook —
    # it has no reach metric, so avg_engagement_rate is meaningless there.
    avg_interactions: float
    lift_vs_baseline: Optional[float] = None


class FormatComparisonInsight(InsightBase):
    insight_type: Literal["format_comparison"] = "format_comparison"
    baseline_post_type: Optional[str] = None
    # Which measure the rows are ranked by: engagement rate where reach exists,
    # absolute interactions where it does not.
    ranked_by: Literal["engagement_rate", "interactions"]
    rows: list[FormatComparisonRow]
    recommendation: Optional[str] = None


class TrendPoint(BaseModel):
    date: str
    reach: Optional[int] = None
    engagement_rate: Optional[float] = None
    views: Optional[int] = None


class SummaryMetric(BaseModel):
    key: Literal["reach", "engagement_rate", "saves", "views"]
    label: str
    value: Optional[float] = None
    # Ratio change vs the preceding window of equal length. None when there is
    # no prior period to compare against, which is distinct from zero change.
    delta: Optional[float] = None
    spark: list[float] = []


class AccountSummary(BaseModel):
    account_id: uuid.UUID
    period_days: int
    source: str
    metrics: list[SummaryMetric]
    trend: list[TrendPoint]
    top_posts: list[PostResponse]
