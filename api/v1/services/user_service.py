from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.security import hash_password, verify_password
from api.utils.jwt import create_access_token, create_refresh_token
from api.utils.settings import settings
from api.v1.models.user import User
from api.v1.schemas.user import TokenResponse, UserCreate, UserLogin, UserResponse


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


def _issue_tokens(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(
            user.id, timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        ),
        refresh_token=create_refresh_token(
            user.id, timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        ),
        user=UserResponse.model_validate(user),
    )


async def register_user(db: AsyncSession, payload: UserCreate) -> TokenResponse:
    email = payload.email.lower()

    if await get_user_by_email(db, email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    user = User(email=email, password_hash=hash_password(payload.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return _issue_tokens(user)


async def authenticate_user(db: AsyncSession, payload: UserLogin) -> TokenResponse:
    user = await get_user_by_email(db, payload.email)

    # Same response whether the email is unknown or the password is wrong —
    # distinguishing them lets an attacker enumerate registered emails.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    return _issue_tokens(user)
