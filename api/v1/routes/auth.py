from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.dependencies import get_current_user
from api.db.database import get_db
from api.utils.success_response import success_response
from api.v1.models.user import User
from api.v1.schemas.user import UserCreate, UserLogin, UserResponse
from api.v1.services.user_service import authenticate_user, register_user

auth = APIRouter(prefix="/auth", tags=["Auth"])


@auth.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    tokens = await register_user(db, payload)
    return success_response(
        status_code=status.HTTP_201_CREATED,
        message="Account created successfully.",
        data=tokens.model_dump(),
    )


@auth.post("/login")
async def login(payload: UserLogin, db: AsyncSession = Depends(get_db)):
    tokens = await authenticate_user(db, payload)
    return success_response(
        status_code=status.HTTP_200_OK,
        message="Login successful.",
        data=tokens.model_dump(),
    )


@auth.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return success_response(
        status_code=status.HTTP_200_OK,
        message="Current user retrieved.",
        data=UserResponse.model_validate(current_user).model_dump(),
    )
