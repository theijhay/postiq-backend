"""Shared FastAPI dependencies."""

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.database import get_db
from api.utils.jwt import verify_access_token
from api.v1.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise CREDENTIALS_EXCEPTION

    token_data = verify_access_token(credentials.credentials, CREDENTIALS_EXCEPTION)

    try:
        user_id = uuid.UUID(token_data.id)
    except ValueError:
        # Well-formed JWT carrying a non-UUID subject — reject rather than
        # letting it reach the query as a bad bind parameter.
        raise CREDENTIALS_EXCEPTION

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()

    if user is None:
        # Valid signature, but the user is gone (deleted account).
        raise CREDENTIALS_EXCEPTION

    return user
