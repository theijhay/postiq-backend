"""Insight endpoints (PROJECT_SPEC.md §12).

Read-only and computed on demand. Insights are cheap to derive from stored
snapshots and stale ones are worse than slightly slower ones, so nothing is
cached until there is evidence the query cost matters.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.dependencies import get_current_user
from api.db.database import get_db
from api.utils.success_response import success_response
from api.v1.models.connected_account import ConnectedAccount
from api.v1.models.post import PostSource
from api.v1.models.user import User
from api.v1.services import insight_service

insights = APIRouter(prefix="/accounts", tags=["Insights"])

_SOURCES = (PostSource.INSTAGRAM, PostSource.FACEBOOK)


async def _owned_account_id(
    account_id: uuid.UUID, db: AsyncSession, user: User
) -> uuid.UUID:
    """Confirm the caller owns this account.

    Scoped by user_id in the query rather than fetched-then-compared, so an id
    belonging to someone else is indistinguishable from one that does not
    exist — no cross-tenant probing.
    """
    owned = (
        await db.execute(
            select(ConnectedAccount.id).where(
                ConnectedAccount.id == account_id,
                ConnectedAccount.user_id == user.id,
            )
        )
    ).scalar_one_or_none()

    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found."
        )
    return owned


def _validate_source(source: str | None) -> str | None:
    if source is not None and source not in _SOURCES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"source must be one of {', '.join(_SOURCES)}.",
        )
    return source


@insights.get("/{account_id}/insights/summary")
async def account_summary(
    account_id: uuid.UUID,
    days: int = Query(30, ge=1, le=365),
    source: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _owned_account_id(account_id, db, current_user)
    data = await insight_service.compute_summary(
        db, account_id, days=days, source=_validate_source(source)
    )
    return success_response(
        status_code=status.HTTP_200_OK,
        message="Account summary retrieved.",
        data=data.model_dump(mode="json"),
    )


@insights.get("/{account_id}/insights/best-time")
async def best_time(
    account_id: uuid.UUID,
    source: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _owned_account_id(account_id, db, current_user)
    data = await insight_service.compute_best_time(
        db, account_id, source=_validate_source(source)
    )
    return success_response(
        status_code=status.HTTP_200_OK,
        message="Best time to post retrieved.",
        data=data.model_dump(mode="json"),
    )


@insights.get("/{account_id}/insights/format-comparison")
async def format_comparison(
    account_id: uuid.UUID,
    source: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _owned_account_id(account_id, db, current_user)
    data = await insight_service.compute_format_comparison(
        db, account_id, source=_validate_source(source)
    )
    return success_response(
        status_code=status.HTTP_200_OK,
        message="Format comparison retrieved.",
        data=data.model_dump(mode="json"),
    )
