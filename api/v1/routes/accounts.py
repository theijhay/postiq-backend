import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import mq
from api.core.dependencies import get_current_user
from api.db.database import get_db
from api.utils.logger import logger
from api.utils.success_response import success_response
from api.v1.models.connected_account import AccountStatus, ConnectedAccount
from api.v1.models.user import User
from api.v1.schemas.account import ConnectedAccountResponse
from api.v1.services import meta_account_service, post_service

accounts = APIRouter(prefix="/accounts", tags=["Accounts"])


async def _owned_account(
    account_id: uuid.UUID, db: AsyncSession, user: User
) -> ConnectedAccount:
    """Fetch an account, scoped to the caller.

    Scoping the query by user_id (rather than fetching then comparing) means an
    id belonging to someone else is indistinguishable from one that doesn't
    exist — no cross-tenant probing.
    """
    account = (
        await db.execute(
            select(ConnectedAccount).where(
                ConnectedAccount.id == account_id,
                ConnectedAccount.user_id == user.id,
            )
        )
    ).scalar_one_or_none()

    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found."
        )
    return account


@accounts.get("")
async def list_connected_accounts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = await meta_account_service.list_accounts(db, current_user.id)
    return success_response(
        status_code=status.HTTP_200_OK,
        message="Connected accounts retrieved.",
        data={
            "accounts": [
                ConnectedAccountResponse.model_validate(row).model_dump() for row in rows
            ]
        },
    )


@accounts.delete("/{account_id}")
async def disconnect_account(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Disconnect and permanently delete all data for this account (§5, §9.6)."""
    account = await _owned_account(account_id, db, current_user)
    await meta_account_service.purge_account(db, account)

    return success_response(
        status_code=status.HTTP_200_OK,
        message="Account disconnected and all associated data deleted.",
    )


@accounts.get("/{account_id}/posts")
async def list_posts(
    account_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Paginated posts with their latest metrics."""
    await _owned_account(account_id, db, current_user)
    result = await post_service.list_posts(db, account_id, page, page_size)

    return success_response(
        status_code=status.HTTP_200_OK,
        message="Posts retrieved.",
        data=result.model_dump(),
    )


@accounts.post("/{account_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def trigger_sync(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Queue an immediate ingestion run for this account.

    Returns 202 rather than waiting: a sync walks many Graph API pages and can
    take minutes. The scheduler covers the routine case; this exists so a user
    who just connected doesn't sit staring at an empty dashboard, and so the
    flow is testable without waiting for the interval.
    """
    account = await _owned_account(account_id, db, current_user)

    if account.status == AccountStatus.DISCONNECTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This account is disconnected. Reconnect it before syncing.",
        )

    try:
        await mq.publish_ingestion_job(str(account.id), reason="manual")
    except Exception:
        # The broker being down shouldn't read as "your account is broken".
        logger.exception("Could not queue ingestion for account %s", account.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sync could not be queued right now. Please try again shortly.",
        )

    return success_response(
        status_code=status.HTTP_202_ACCEPTED,
        message="Sync queued. New data will appear within a few minutes.",
    )
