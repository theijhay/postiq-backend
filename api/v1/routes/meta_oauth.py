"""Meta OAuth connect flow + the two callbacks Meta requires for App Review."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.dependencies import get_current_user
from api.db.database import get_db
from api.utils.logger import logger
from api.utils.settings import settings
from api.utils.success_response import success_response
from api.v1.models.user import User
from api.v1.services import meta_account_service
from api.v1.services.meta_client import MetaAPIError, build_authorization_url, parse_signed_request

meta_oauth = APIRouter(prefix="/auth/meta", tags=["Meta OAuth"])


@meta_oauth.get("/connect")
async def connect(current_user: User = Depends(get_current_user)):
    """Start the OAuth dance.

    Returns the URL rather than a 307 so the browser-based frontend can decide
    between a redirect and a popup; a bare redirect would break the popup flow.
    """
    state = await meta_account_service.create_oauth_state(current_user)
    return success_response(
        status_code=status.HTTP_200_OK,
        message="Authorization URL generated.",
        data={"authorization_url": build_authorization_url(state)},
    )


@meta_oauth.get("/callback")
async def callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Meta redirects the user here after they approve (or deny)."""
    params = request.query_params

    if error := params.get("error"):
        # User hit "Cancel", or Meta refused. Not an exception — send them back.
        logger.info("Meta OAuth declined: %s", params.get("error_description", error))
        return RedirectResponse(
            f"{settings.META_POST_CONNECT_REDIRECT}?connected=false&reason={error}"
        )

    code, state = params.get("code"), params.get("state")
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing 'code' or 'state' in callback.",
        )

    user_id = await meta_account_service.consume_oauth_state(state)

    try:
        account = await meta_account_service.complete_connection(db, user_id, code)
    except MetaAPIError as exc:
        logger.exception("Meta token exchange failed for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not complete Meta connection: {exc}",
        )

    return RedirectResponse(
        f"{settings.META_POST_CONNECT_REDIRECT}?connected=true&account_id={account.id}"
    )


@meta_oauth.post("/deauthorize")
async def deauthorize(
    signed_request: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Meta calls this when a user removes PostIQ from their Meta settings.

    Tokens are dropped and the account is marked disconnected, but historical
    metrics are kept — revoking access is not the same as requesting erasure.
    """
    payload = _verified_payload(signed_request)
    count = await meta_account_service.mark_disconnected(db, payload["user_id"])
    logger.info("Deauthorized %s account(s) for meta_user_id=%s", count, payload["user_id"])
    return {"success": True}


@meta_oauth.post("/data-deletion")
async def data_deletion(
    signed_request: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Meta's Data Deletion Callback — mandatory for Facebook Login for Business.

    Must respond with a status URL and a confirmation code, which Meta surfaces
    to the user so they can check on the request.
    """
    payload = _verified_payload(signed_request)
    meta_user_id = payload["user_id"]

    count = await meta_account_service.purge_by_meta_user_id(db, meta_user_id)
    logger.info("Purged %s account(s) for meta_user_id=%s", count, meta_user_id)

    return {
        "url": f"{settings.META_POST_CONNECT_REDIRECT}?deletion_status={meta_user_id}",
        "confirmation_code": f"postiq_del_{meta_user_id}",
    }


def _verified_payload(signed_request: str) -> dict:
    try:
        payload = parse_signed_request(signed_request)
    except MetaAPIError as exc:
        # Anyone can POST here; the signature is the only thing that makes this
        # request trustworthy.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    if not payload.get("user_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="signed_request contained no user_id.",
        )
    return payload
