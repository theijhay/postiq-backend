"""Append-only time-series metric snapshots per post (PROJECT_SPEC.md §9.3).

Two deliberate deviations from the spec's draft schema:

1. ``views`` replaces ``impressions``. Meta deprecated media impressions (plus
   Reel plays and Story impressions) from Graph API v21 and consolidated them
   into a single ``views`` metric. Writing an ``impressions`` column today would
   only buy a rename migration later.
2. ``raw`` keeps the untouched insights payload. Meta renames metrics roughly
   annually; storing the raw response alongside the typed columns turns the next
   deprecation into a backfill instead of permanent data loss.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.v1.models.base_model import BaseTableModel, utcnow

if TYPE_CHECKING:
    from api.v1.models.post import Post


class PostMetricsSnapshot(BaseTableModel):
    __tablename__ = "post_metrics_snapshots"
    __table_args__ = (
        # Every dashboard and insight query is either latest-per-post or a time
        # window per post. Without this both are sequential scans.
        Index(
            "ix_post_metrics_post_captured",
            "post_id",
            "captured_at",
            postgresql_using="btree",
        ),
    )

    # No standalone index: ix_post_metrics_post_captured leads with post_id, so
    # a second one would only cost write throughput on an append-only table.
    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE")
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    views: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reach: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    likes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comments: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shares: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    saves: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    engagement_rate: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 6), nullable=True
    )

    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    post: Mapped["Post"] = relationship(back_populates="metrics_snapshots")

    def __repr__(self) -> str:
        return f"<PostMetricsSnapshot post={self.post_id} at={self.captured_at}>"
