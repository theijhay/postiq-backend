"""Individual posts pulled from the Graph API (PROJECT_SPEC.md §9.3)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.v1.models.base_model import BaseTableModel

if TYPE_CHECKING:
    from api.v1.models.connected_account import ConnectedAccount
    from api.v1.models.post_metrics_snapshot import PostMetricsSnapshot


class PostSource:
    """Which Meta surface a post came from.

    An account can have both — a Facebook Page with a linked Instagram
    Professional account produces two independent streams. Insights must be
    computed per source and never pooled: organic Page reach and Instagram
    reach are driven by different ranking systems, so an engagement rate
    averaged across the two describes neither.
    """

    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"


class PostType:
    IMAGE = "image"
    CAROUSEL = "carousel"
    VIDEO = "video"
    REEL = "reel"
    # Facebook-only formats. Instagram has no equivalent of a link share or a
    # text-only post, so these never appear on an instagram-sourced row.
    LINK = "link"
    TEXT = "text"


class Post(BaseTableModel):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint(
            "connected_account_id", "platform_post_id", name="uq_post_account_platform_id"
        ),
    )

    # Covered by uq_post_account_platform_id, whose unique index leads with this
    # column.
    connected_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connected_accounts.id", ondelete="CASCADE"),
    )
    platform_post_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Not part of uq_post_account_platform_id: Meta ids are unique across
    # surfaces (Facebook post ids are "{page_id}_{post_id}", Instagram media
    # ids are bare numerics), so the pair already cannot collide.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PostSource.INSTAGRAM
    )
    post_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Indexed: the best-time-to-post insight groups by hour/weekday over this.
    posted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    permalink: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    media_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    connected_account: Mapped["ConnectedAccount"] = relationship(back_populates="posts")
    metrics_snapshots: Mapped[list["PostMetricsSnapshot"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Post {self.platform_post_id} {self.post_type}>"
