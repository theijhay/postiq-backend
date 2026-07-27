import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.dependencies import get_current_user
from api.db.database import get_db
from api.utils.success_response import success_response
from api.v1.models.connected_account import ConnectedAccount
from api.v1.models.user import User
from api.v1.schemas.account import ConnectedAccountResponse
from api.v1.services import meta_account_service

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
