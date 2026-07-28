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


async def _diagnose_empty_pages(user_id: uuid.UUID, user_token: str) -> dict[str, str]:
    """Log everything that distinguishes the causes of an empty /me/accounts.

    Meta reports "no Pages" identically whether the user has none, declined the
    grant, or granted a Page that the listing endpoint then omits. Each call
    here is wrapped separately so one failing still leaves the others useful.
    """
    granted: dict[str, str] = {}

    try:
        granted = await meta_client.get_granted_permissions(user_token)
    except Exception:  # noqa: BLE001
        logger.warning("diagnose: could not read /me/permissions")

    try:
        info = await meta_client.debug_token(user_token)
        # granular_scopes names the exact asset ids behind each permission.
        logger.warning(
            "diagnose: token type=%s app_id=%s granular_scopes=%s",
            info.get("type"),
            info.get("app_id"),
            info.get("granular_scopes"),
        )
    except Exception:  # noqa: BLE001
        logger.warning("diagnose: could not debug_token")

    try:
        businesses = await meta_client.get_businesses(user_token)
        logger.warning(
            "diagnose: businesses=%s",
            [(b.get("id"), b.get("name")) for b in businesses] or "none",
        )
    except Exception:  # noqa: BLE001
        logger.warning("diagnose: could not read /me/businesses")

    logger.warning(
        "Connect failed for user %s — /me/accounts empty. Granted: %s",
        user_id,
        granted or "unknown",
    )
    return granted


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
        # An empty /me/accounts has two very different causes that look
        # identical from here: the user has no Page at all, or they were not
        # granted access to one. Ask Meta which, so the message can be useful
        # and so the logs say what actually happened.
        granted = await _diagnose_empty_pages(user_id, user_token)

        if granted.get("pages_show_list") != "granted":
            detail = (
                "PostIQ wasn't given access to any Facebook Page. Reconnect and, "
                "on the Meta consent screen, make sure you select the Page you "
                "want to analyse before continuing."
            )
        else:
            detail = (
                "No Facebook Page is available on this Meta account. PostIQ needs "
                "a Page with an Instagram Professional account (Business or "
                "Creator) linked to it. Create or get admin access to a Page, "
                "link your Instagram account to it, then reconnect."
            )

        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

    # Resolve the Instagram link per Page, tolerating failure. Prefer a Page
    # that has one; fall back to the first Page so the account still connects
    # and the user gets a clear "no Instagram linked" state rather than an error.
    page = pages[0]
    ig_account: dict = {}

    for candidate in pages:
        try:
            linked = await meta_client.get_instagram_business_account(
                candidate["id"], candidate.get("access_token") or user_token
            )
        except Exception:  # noqa: BLE001 - missing instagram_basic is expected here
            logger.info(
                "Could not read Instagram link for page %s (Instagram scopes may "
                "not be granted yet)",
                candidate.get("id"),
            )
            continue

        if linked:
            page, ig_account = candidate, linked
            break

    # The edge is named instagram_business_account for historical reasons, but
    # it returns any linked Professional account. Read the real account_type so
    # ingestion can adapt: Creator accounts are fully supported and are common
    # among the people this product targets.
    ig_profile: dict = {}
    if ig_account.get("id"):
        try:
            ig_profile = await meta_client.get_instagram_account(
                ig_account["id"], page.get("access_token") or user_token
            )
        except Exception:  # noqa: BLE001 - profile detail is not worth failing the connect over
            logger.warning(
                "Could not read IG profile for %s; continuing without account_type",
                ig_account.get("id"),
            )

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
    account.ig_username = ig_profile.get("username") or ig_account.get("username")
    account.ig_account_type = ig_profile.get("account_type")
    account.account_name = account.ig_username or page.get("name")
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
            "Page %s connected without a linked Instagram Professional account — "
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
