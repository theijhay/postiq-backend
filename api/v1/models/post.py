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


class PostType:
    IMAGE = "image"
    CAROUSEL = "carousel"
    VIDEO = "video"
    REEL = "reel"


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
