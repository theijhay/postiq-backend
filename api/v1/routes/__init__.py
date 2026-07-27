from fastapi import APIRouter

from api.v1.routes.accounts import accounts
from api.v1.routes.auth import auth
from api.v1.routes.meta_oauth import meta_oauth

api_version_one = APIRouter(prefix="/api/v1")

api_version_one.include_router(auth)
api_version_one.include_router(meta_oauth)
api_version_one.include_router(accounts)
