"""Platform users of PostIQ — the business owner (PROJECT_SPEC.md §9.3)."""

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.v1.models.base_model import BaseTableModel

if TYPE_CHECKING:
    from api.v1.models.connected_account import ConnectedAccount


class SubscriptionStatus:
    """Billing states. Plain strings rather than a native PG enum — adding a
    tier later is a no-op instead of an ALTER TYPE migration."""

    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"


class User(BaseTableModel):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(
        String(320), unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    subscription_status: Mapped[str] = mapped_column(
        String(32), default=SubscriptionStatus.TRIALING, nullable=False
    )

    connected_accounts: Mapped[list["ConnectedAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User {self.email}>"
