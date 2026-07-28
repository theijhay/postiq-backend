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

def _scopes() -> list[str]:
    """Scopes requested at OAuth time (PROJECT_SPEC.md §8, P0).

    Read from settings on each call rather than frozen at import, so the set can
    be narrowed via env while the Meta app dashboard is being configured. The
    Instagram scopes only exist once the app has the Instagram product added —
    until then Meta rejects the entire dialog with "Invalid Scopes".

    Each of these also needs Advanced Access via App Review before it works on
    accounts the developer does not own.
    """
    return [s.strip() for s in settings.META_SCOPES.split(",") if s.strip()]


# Kept as a module attribute for callers that just want to display the list.
META_SCOPES = _scopes()

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
            "scope": ",".join(_scopes()),
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


async def debug_token(access_token: str) -> dict[str, Any]:
    """Inspect a token with the app token.

    ``granular_scopes`` is the useful part: for Facebook Login for Business it
    lists the exact asset ids each permission was granted against. If a Page id
    appears there but ``/me/accounts`` is empty, the grant succeeded and the
    problem is on the listing side, not the consent side — a distinction no
    other endpoint makes visible.
    """
    app_token = f"{settings.META_APP_ID}|{settings.META_APP_SECRET}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        payload = await _request(
            client,
            "debug_token",
            {"input_token": access_token, "access_token": app_token},
        )
    return payload.get("data", {})


async def get_businesses(access_token: str) -> list[dict[str, Any]]:
    """Business portfolios the user belongs to.

    A Page owned by a Business portfolio can be invisible to ``/me/accounts``
    when the app was not granted access to that portfolio.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        payload = await _request(
            client,
            "me/businesses",
            {
                "fields": "id,name",
                "access_token": access_token,
                "appsecret_proof": appsecret_proof(access_token),
            },
        )
    return payload.get("data", [])


async def get_granted_permissions(access_token: str) -> dict[str, str]:
    """What the user actually approved, as ``{permission: granted|declined}``.

    The consent dialog lets people approve some scopes and decline others, and
    Facebook Login for Business additionally asks them to *select which Pages*
    to share. A user who clicks through without picking one grants
    ``pages_show_list`` yet still returns an empty ``/me/accounts`` — which is
    indistinguishable from "has no Page" unless we look here.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        payload = await _request(
            client,
            "me/permissions",
            {
                "access_token": access_token,
                "appsecret_proof": appsecret_proof(access_token),
            },
        )
    return {
        entry["permission"]: entry["status"]
        for entry in payload.get("data", [])
        if entry.get("permission")
    }


# Scopes whose granted assets are Pages.
_PAGE_SCOPES = frozenset(
    {
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_metadata",
        "pages_read_user_content",
    }
)


def page_ids_from_granular_scopes(token_info: dict[str, Any]) -> list[str]:
    """Extract granted Page ids from a debug_token payload, order preserved."""
    ids: list[str] = []
    for entry in token_info.get("granular_scopes") or []:
        if entry.get("scope") in _PAGE_SCOPES:
            for target in entry.get("target_ids") or []:
                if target not in ids:
                    ids.append(target)
    return ids


async def get_page(page_id: str, access_token: str) -> dict[str, Any]:
    """Fetch one Page directly, including its own access token."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        return await _request(
            client,
            page_id,
            {
                "fields": "id,name,access_token",
                "access_token": access_token,
                "appsecret_proof": appsecret_proof(access_token),
            },
        )


async def get_pages(access_token: str) -> list[dict[str, Any]]:
    """Facebook Pages this token can act on, each with its own Page token.

    Page tokens are a distinct credential from the user token — Page-level
    insights calls must use these.

    Two lookup strategies, because ``/me/accounts`` alone is not reliable:

    1. ``/me/accounts`` — the conventional listing.
    2. If that comes back empty, read the Page ids out of the token's
       ``granular_scopes`` and fetch each Page directly.

    Step 2 exists because Facebook Login for Business grants *specific assets*
    chosen by the user, and those grants are recorded in ``granular_scopes``
    while ``/me/accounts`` can still return nothing. Observed directly: a token
    with ``pages_show_list`` granted against Page 1041775684… listed zero
    accounts. Treating the listing as authoritative reports "you have no Page"
    to someone who just picked one, which is both wrong and unfixable by them.

    Only base fields are requested here. ``instagram_business_account`` is
    resolved separately per Page, so a missing Instagram grant costs us the
    Instagram link rather than the whole connect.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        payload = await _request(
            client,
            "me/accounts",
            {
                "fields": "id,name,access_token",
                "access_token": access_token,
                "appsecret_proof": appsecret_proof(access_token),
            },
        )

    pages = payload.get("data", [])
    if pages:
        return pages

    try:
        granted_ids = page_ids_from_granular_scopes(await debug_token(access_token))
    except MetaAPIError as exc:
        logger.warning("Could not inspect token for granted Pages: %s", exc)
        return []

    if not granted_ids:
        return []

    logger.info(
        "/me/accounts was empty; resolving %s Page(s) from granular_scopes",
        len(granted_ids),
    )

    resolved: list[dict[str, Any]] = []
    for page_id in granted_ids:
        try:
            resolved.append(await get_page(page_id, access_token))
        except MetaAPIError as exc:
            logger.warning("Granted Page %s could not be read: %s", page_id, exc)

    return resolved


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


async def get_instagram_account(ig_user_id: str, access_token: str) -> dict[str, Any]:
    """Profile fields for a linked IG Professional account.

    ``account_type`` is either BUSINESS or MEDIA_CREATOR — both are supported;
    we read it so ingestion can adapt its metric requests.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        return await _request(
            client,
            ig_user_id,
            {
                "fields": "id,username,account_type,followers_count,media_count",
                "access_token": access_token,
                "appsecret_proof": appsecret_proof(access_token),
            },
        )


# Fields that describe the post itself, as opposed to its performance.
_MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,permalink,media_url,"
    "thumbnail_url,timestamp,like_count,comments_count"
)


async def get_instagram_media(
    ig_user_id: str, access_token: str, limit: int = 50, after: str | None = None
) -> dict[str, Any]:
    """One page of the account's media, newest first.

    Returns the raw envelope so the caller can follow ``paging.cursors.after``.
    """
    params: dict[str, Any] = {
        "fields": _MEDIA_FIELDS,
        "limit": limit,
        "access_token": access_token,
        "appsecret_proof": appsecret_proof(access_token),
    }
    if after:
        params["after"] = after

    async with httpx.AsyncClient(timeout=30.0) as client:
        return await _request(client, f"{ig_user_id}/media", params)


# Insight metric names vary by what kind of media it is. Requesting a metric a
# media type doesn't support makes Meta reject the WHOLE call, so these sets are
# deliberately conservative rather than a union of everything available.
#
# `impressions` is absent on purpose: deprecated from v21 and folded into
# `views`. See post_metrics_snapshot.py.
_METRICS_BY_PRODUCT_TYPE: dict[str, tuple[str, ...]] = {
    "REELS": ("reach", "likes", "comments", "shares", "saved", "views"),
    "STORY": ("reach", "views"),
    "FEED": ("reach", "likes", "comments", "shares", "saved", "views"),
    "AD": ("reach",),
}
_DEFAULT_METRICS = ("reach", "likes", "comments", "shares", "saved", "views")


def metrics_for(media_product_type: str | None) -> tuple[str, ...]:
    """Pick the metric set for a media item. Public so tests can pin it."""
    if not media_product_type:
        return _DEFAULT_METRICS
    return _METRICS_BY_PRODUCT_TYPE.get(media_product_type.upper(), _DEFAULT_METRICS)


async def get_media_insights(
    media_id: str, access_token: str, media_product_type: str | None = None
) -> dict[str, int]:
    """Flatten a media item's insights into ``{metric: value}``.

    Returns an empty dict rather than raising when Meta refuses the whole call.
    A single post whose metrics we cannot read must not abort a sync of two
    hundred others — the post row is still worth storing, and the next run will
    try again. Genuinely fatal problems (expired token) surface earlier, on the
    media fetch.
    """
    metrics = metrics_for(media_product_type)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            payload = await _request(
                client,
                f"{media_id}/insights",
                {
                    "metric": ",".join(metrics),
                    "access_token": access_token,
                    "appsecret_proof": appsecret_proof(access_token),
                },
            )
    except MetaAPIError as exc:
        logger.warning("Insights unavailable for media %s: %s", media_id, exc)
        return {}

    values: dict[str, int] = {}
    for entry in payload.get("data", []):
        name = entry.get("name")
        series = entry.get("values") or []
        if name and series and isinstance(series[0].get("value"), int):
            values[name] = series[0]["value"]
    return values


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
