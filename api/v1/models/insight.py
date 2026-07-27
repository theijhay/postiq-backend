"""Precomputed insight results, so dashboard reads stay cheap (§9.3)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.v1.models.base_model import BaseTableModel, utcnow

if TYPE_CHECKING:
    from api.v1.models.connected_account import ConnectedAccount


class InsightType:
    BEST_TIME = "best_time"
    FORMAT_COMPARISON = "format_comparison"
    ANOMALY = "anomaly"


class Insight(BaseTableModel):
    __tablename__ = "insights"
    __table_args__ = (
        # Dashboard reads the newest insight of a given type for an account.
        Index("ix_insights_account_type_generated", "connected_account_id", "insight_type", "generated_at"),
    )

    # Covered by the leading column of ix_insights_account_type_generated.
    connected_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connected_accounts.id", ondelete="CASCADE"),
    )
    insight_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # Shape varies per insight_type and will evolve as the engine improves;
    # JSONB keeps that from becoming a migration every iteration.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    connected_account: Mapped["ConnectedAccount"] = relationship(
        back_populates="insights"
    )

    def __repr__(self) -> str:
        return f"<Insight {self.insight_type} account={self.connected_account_id}>"
