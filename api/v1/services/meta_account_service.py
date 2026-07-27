"""Persistence side of the Meta connect/disconnect lifecycle."""

import secrets
import uuid
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.redis_client import redis_client
from api.core.security import decrypt_str, encrypt_str
from api.utils.logger import logger
from api.utils.settings import settings
from api.v1.models.base_model import utcnow
from api.v1.models.connected_account import AccountStatus, ConnectedAccount
from api.v1.models.user import User
from api.v1.services import meta_client

_STATE_KEY = "meta:oauth:state:{state}"


async def create_oauth_state(user: User) -> str:
    """Mint a single-use CSRF state and park it in Redis.

    The state maps back to the user id, which is what lets the callback know
    who is connecting without trusting anything in the query string.
    """
    state = secrets.token_urlsafe(32)
    await redis_client.setex(
        _STATE_KEY.format(state=state),
        settings.META_OAUTH_STATE_TTL_SECONDS,
        str(user.id),
    )
    return state


async def consume_oauth_state(state: str) -> uuid.UUID:
    """Validate and burn the state. Raises if unknown, expired, or replayed."""
    key = _STATE_KEY.format(state=state)
    user_id = await redis_client.get(key)

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OAuth state. Please start the connection again.",
        )

    # Single-use: delete before doing any work so a replayed callback can't
    # produce a second account.
    await redis_client.delete(key)
    return uuid.UUID(user_id)


async def complete_connection(
    db: AsyncSession, user_id: uuid.UUID, code: str
) -> ConnectedAccount:
    """Exchange the code and persist an active connected account.

    Chooses the first Page with a linked Instagram Business account, falling
    back to the first Page. v1 is single-business-per-account (§5), so picking
    one here is correct; multi-page selection is a v2 concern.
    """
    short_lived = await meta_client.exchange_code_for_token(code)
    long_lived = await meta_client.exchange_for_long_lived_token(
        short_lived["access_token"]
    )
    user_token = long_lived["access_token"]
    expires_in = long_lived.get("expires_in")

    me = await meta_client.get_me(user_token)
    pages = await meta_client.get_pages(user_token)

    if not pages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "No Facebook Page found on this account. PostIQ needs a Page with "
                "a linked Instagram Business account."
            ),
        )

    page = next((p for p in pages if p.get("instagram_business_account")), pages[0])
    ig_account = page.get("instagram_business_account") or {}

    # Reconnecting the same Meta user should update the existing row rather than
    # accumulate duplicates.
    account = (
        await db.execute(
            select(ConnectedAccount).where(
                ConnectedAccount.user_id == user_id,
                ConnectedAccount.meta_user_id == me["id"],
            )
        )
    ).scalar_one_or_none()

    if account is None:
        account = ConnectedAccount(user_id=user_id, meta_user_id=me["id"])
        db.add(account)

    account.platform = "meta"
    account.fb_page_id = page.get("id")
    account.ig_business_id = ig_account.get("id")
    account.account_name = ig_account.get("username") or page.get("name")
    account.access_token_encrypted = encrypt_str(user_token)
    account.page_access_token_encrypted = (
        encrypt_str(page["access_token"]) if page.get("access_token") else None
    )
    account.token_expires_at = (
        utcnow() + timedelta(seconds=int(expires_in)) if expires_in else None
    )
    account.connected_at = utcnow()
    account.status = AccountStatus.ACTIVE

    await db.commit()
    await db.refresh(account)

    if not account.ig_business_id:
        logger.warning(
            "Page %s connected without a linked Instagram Business account — "
            "IG insights will be unavailable for account %s",
            account.fb_page_id,
            account.id,
        )

    return account


async def list_accounts(db: AsyncSession, user_id: uuid.UUID) -> list[ConnectedAccount]:
    result = await db.execute(
        select(ConnectedAccount)
        .where(ConnectedAccount.user_id == user_id)
        .order_by(ConnectedAccount.connected_at.desc())
    )
    return list(result.scalars().all())


async def purge_account(db: AsyncSession, account: ConnectedAccount) -> None:
    """Revoke upstream, then hard-delete.

    §5 and §9.6 want a real purge, not a soft flag — posts, snapshots, and
    insights go with it via ON DELETE CASCADE. Upstream revocation is
    best-effort and must not block the local delete.
    """
    if account.access_token_encrypted and account.meta_user_id:
        try:
            await meta_client.revoke_permissions(
                account.meta_user_id, decrypt_str(account.access_token_encrypted)
            )
        except Exception:  # noqa: BLE001 - deletion must proceed regardless
            logger.exception("Upstream revoke failed for account %s", account.id)

    await db.execute(
        delete(ConnectedAccount).where(ConnectedAccount.id == account.id)
    )
    await db.commit()


async def purge_by_meta_user_id(db: AsyncSession, meta_user_id: str) -> int:
    """Delete every account tied to a Meta user — the data-deletion callback path."""
    accounts = list(
        (
            await db.execute(
                select(ConnectedAccount).where(
                    ConnectedAccount.meta_user_id == meta_user_id
                )
            )
        )
        .scalars()
        .all()
    )

    for account in accounts:
        await db.execute(
            delete(ConnectedAccount).where(ConnectedAccount.id == account.id)
        )

    await db.commit()
    return len(accounts)


async def mark_disconnected(db: AsyncSession, meta_user_id: str) -> int:
    """Deauthorize callback: the user removed our app on Meta's side.

    Distinct from deletion — they revoked access but did not ask us to erase
    their data, so we drop the tokens and keep the historical metrics.
    """
    accounts = list(
        (
            await db.execute(
                select(ConnectedAccount).where(
                    ConnectedAccount.meta_user_id == meta_user_id
                )
            )
        )
        .scalars()
        .all()
    )

    for account in accounts:
        account.status = AccountStatus.DISCONNECTED
        account.access_token_encrypted = None
        account.page_access_token_encrypted = None
        account.token_expires_at = None

    await db.commit()
    return len(accounts)
