"""Thin async wrapper over the Meta Graph API.

This is the *only* module that knows Graph API URL shapes and response schemas.
Keeping that boundary means the insight engine works against our own models and
never against Meta's wire format — which PROJECT_SPEC.md §12 calls out as the
prerequisite for reusing the scoring logic on a different data source in v2.
"""

import asyncio
import base64
import hashlib
import hmac
import json
from typing import Any

import httpx

from api.utils.logger import logger
from api.utils.settings import settings

# Requested at OAuth time (PROJECT_SPEC.md §8, P0). Each of these needs Advanced
# Access via App Review before it works on accounts the developer doesn't own.
META_SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "instagram_basic",
    "instagram_manage_insights",
]

# Meta rate-limits aggressively and returns 5xx under load; retry those.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_BASE_BACKOFF_SECONDS = 1.0


class MetaAPIError(RuntimeError):
    """A Graph API call failed in a way we can't recover from."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _graph_url(path: str) -> str:
    return f"{settings.META_GRAPH_BASE}/{settings.META_GRAPH_VERSION}/{path.lstrip('/')}"


def appsecret_proof(access_token: str) -> str:
    """HMAC of the token under the app secret.

    Meta recommends sending this on every server-side call; it proves the call
    came from us and not from someone who merely stole a token.
    """
    return hmac.new(
        settings.META_APP_SECRET.encode(), access_token.encode(), hashlib.sha256
    ).hexdigest()


def build_authorization_url(state: str) -> str:
    """The URL we redirect the user to in order to start the OAuth dance."""
    params = httpx.QueryParams(
        {
            "client_id": settings.META_APP_ID,
            "redirect_uri": settings.META_REDIRECT_URI,
            "state": state,
            "scope": ",".join(META_SCOPES),
            "response_type": "code",
        }
    )
    return f"https://www.facebook.com/{settings.META_GRAPH_VERSION}/dialog/oauth?{params}"


async def _request(
    client: httpx.AsyncClient, path: str, params: dict[str, Any]
) -> dict[str, Any]:
    """GET a Graph endpoint with exponential backoff on transient failures."""
    last_error: str = "unknown error"

    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = await client.get(_graph_url(path), params=params)
        except httpx.RequestError as exc:
            last_error = f"network error: {exc}"
        else:
            if response.status_code == httpx.codes.OK:
                return response.json()

            # Meta puts the useful detail in body.error.message, not the status line.
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except ValueError:
                detail = response.text
            last_error = f"HTTP {response.status_code}: {detail}"

            if response.status_code not in _RETRY_STATUS:
                raise MetaAPIError(last_error, response.status_code)

        if attempt < _MAX_ATTEMPTS - 1:
            delay = _BASE_BACKOFF_SECONDS * (2**attempt)
            logger.warning(
                "Graph API %s failed (%s) — retrying in %.1fs", path, last_error, delay
            )
            await asyncio.sleep(delay)

    raise MetaAPIError(f"Graph API {path} failed after {_MAX_ATTEMPTS} attempts: {last_error}")


async def exchange_code_for_token(code: str) -> dict[str, Any]:
    """Authorization code -> short-lived user access token."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        return await _request(
            client,
            "oauth/access_token",
            {
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "redirect_uri": settings.META_REDIRECT_URI,
                "code": code,
            },
        )


async def exchange_for_long_lived_token(short_lived_token: str) -> dict[str, Any]:
    """Short-lived (~1h) -> long-lived (~60d) user access token.

    The long-lived token is what we persist; §9.6 requires refreshing it before
    the ~60-day expiry.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        return await _request(
            client,
            "oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "fb_exchange_token": short_lived_token,
            },
        )


async def get_me(access_token: str) -> dict[str, Any]:
    """The authorising Meta user. Their ID keys the data-deletion callback."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        return await _request(
            client,
            "me",
            {
                "fields": "id,name",
                "access_token": access_token,
                "appsecret_proof": appsecret_proof(access_token),
            },
        )


async def get_pages(access_token: str) -> list[dict[str, Any]]:
    """Facebook Pages the user administers, each with its own Page token.

    Page tokens are a distinct credential from the user token — Page-level
    insights calls must use these.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        payload = await _request(
            client,
            "me/accounts",
            {
                "fields": "id,name,access_token,instagram_business_account{id,username}",
                "access_token": access_token,
                "appsecret_proof": appsecret_proof(access_token),
            },
        )
    return payload.get("data", [])


async def get_instagram_business_account(
    page_id: str, page_access_token: str
) -> dict[str, Any] | None:
    """The IG Business account linked to a Page, if the user linked one.

    Returns None when the Page has no linked IG account — a common and
    non-exceptional state that the caller should surface to the user rather
    than treat as a failure.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        payload = await _request(
            client,
            page_id,
            {
                "fields": "instagram_business_account{id,username}",
                "access_token": page_access_token,
                "appsecret_proof": appsecret_proof(page_access_token),
            },
        )
    return payload.get("instagram_business_account")


def parse_signed_request(signed_request: str) -> dict[str, Any]:
    """Verify and decode Meta's ``signed_request``.

    Sent to the deauthorize and data-deletion callbacks. It is the *only* proof
    those requests came from Meta, so a signature mismatch must be rejected —
    otherwise anyone who can reach the endpoint can delete arbitrary accounts.
    """
    try:
        encoded_sig, encoded_payload = signed_request.split(".", 1)
    except ValueError:
        raise MetaAPIError("Malformed signed_request: expected '<sig>.<payload>'.")

    def _b64_decode(value: str) -> bytes:
        # Meta uses base64url without padding.
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    try:
        signature = _b64_decode(encoded_sig)
        payload = json.loads(_b64_decode(encoded_payload))
    except (ValueError, TypeError) as exc:
        raise MetaAPIError(f"Could not decode signed_request: {exc}")

    if payload.get("algorithm", "").upper() != "HMAC-SHA256":
        raise MetaAPIError(f"Unsupported signed_request algorithm: {payload.get('algorithm')}")

    expected = hmac.new(
        settings.META_APP_SECRET.encode(), encoded_payload.encode(), hashlib.sha256
    ).digest()

    if not hmac.compare_digest(signature, expected):
        raise MetaAPIError("signed_request signature mismatch.")

    return payload


async def revoke_permissions(user_id: str, access_token: str) -> None:
    """Revoke our app's grant on Meta's side.

    Best-effort: if it fails we still purge locally, because the user asked us
    to delete their data and an upstream error is not a reason to keep it.
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.delete(
                _graph_url(f"{user_id}/permissions"),
                params={
                    "access_token": access_token,
                    "appsecret_proof": appsecret_proof(access_token),
                },
            )
    except (httpx.RequestError, MetaAPIError) as exc:
        logger.warning("Could not revoke Meta permissions for %s: %s", user_id, exc)
