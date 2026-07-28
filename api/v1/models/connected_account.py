"""A connected Meta business account (PROJECT_SPEC.md §9.3)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.v1.models.base_model import BaseTableModel, utcnow

if TYPE_CHECKING:
    from api.v1.models.insight import Insight
    from api.v1.models.post import Post
    from api.v1.models.user import User


class AccountStatus:
    ACTIVE = "active"
    TOKEN_EXPIRED = "token_expired"
    DISCONNECTED = "disconnected"


class IGAccountType:
    """Instagram *Professional* account flavours.

    Both are fully supported by the Graph API — only genuinely personal
    accounts are excluded. Creator accounts are common among the people this
    product targets, so nothing here may treat them as second-class. The value
    matters because a few metrics differ between the two, so ingestion adapts
    its metric requests rather than assuming Business.
    """

    BUSINESS = "BUSINESS"
    CREATOR = "MEDIA_CREATOR"
    PERSONAL = "PERSONAL"


class ConnectedAccount(BaseTableModel):
    __tablename__ = "connected_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(32), default="meta", nullable=False)

    # Meta's own user ID for the person who authorised us. Needed to resolve the
    # data-deletion and deauthorize callbacks, which identify the user by this
    # ID and nothing else.
    meta_user_id: Mapped[Optional[str]] = mapped_column(
        String(64), index=True, nullable=True
    )
    fb_page_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Column name kept for continuity; holds any linked IG *Professional*
    # account id, Business or Creator alike.
    ig_business_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ig_account_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ig_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    account_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Ciphertext from api.core.security.encrypt_str — never plaintext (§9.6).
    # The user token and the Page token are separate credentials: Page-level
    # insights calls must use the Page token from /me/accounts.
    access_token_encrypted: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True
    )
    page_access_token_encrypted: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True
    )
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default=AccountStatus.ACTIVE, nullable=False, index=True
    )
    last_ingested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship(back_populates="connected_accounts")
    posts: Mapped[list["Post"]] = relationship(
        back_populates="connected_account", cascade="all, delete-orphan"
    )
    insights: Mapped[list["Insight"]] = relationship(
        back_populates="connected_account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ConnectedAccount {self.platform} page={self.fb_page_id} {self.status}>"
