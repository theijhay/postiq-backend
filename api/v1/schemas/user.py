import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class TokenData(BaseModel):
    """Decoded JWT subject. Imported by api/utils/jwt.py."""

    id: str


class UserCreate(BaseModel):
    email: EmailStr
    # 8 is the floor, not a recommendation. bcrypt is pre-hashed in
    # api.core.security, so there is no upper bound to enforce here.
    password: str = Field(min_length=8, max_length=1024)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    subscription_status: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse
