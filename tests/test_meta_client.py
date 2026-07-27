"""Tests for the Meta wire-format boundary.

``parse_signed_request`` is the security-critical one: it is the only thing
standing between the public data-deletion endpoint and an attacker who wants to
delete arbitrary accounts.
"""

import base64
import hashlib
import hmac
import json

import pytest

from api.utils.settings import settings
from api.v1.services.meta_client import (
    MetaAPIError,
    build_authorization_url,
    parse_signed_request,
)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _make_signed_request(payload: dict, secret: str | None = None) -> str:
    secret = secret if secret is not None else settings.META_APP_SECRET
    encoded_payload = _b64(json.dumps(payload).encode())
    signature = hmac.new(
        secret.encode(), encoded_payload.encode(), hashlib.sha256
    ).digest()
    return f"{_b64(signature)}.{encoded_payload}"


@pytest.fixture(autouse=True)
def _app_secret(monkeypatch):
    monkeypatch.setattr(settings, "META_APP_SECRET", "test-app-secret")


def test_valid_signed_request_decodes():
    payload = {"algorithm": "HMAC-SHA256", "user_id": "1234567890"}
    assert parse_signed_request(_make_signed_request(payload))["user_id"] == "1234567890"


def test_wrong_secret_is_rejected():
    payload = {"algorithm": "HMAC-SHA256", "user_id": "1234567890"}
    forged = _make_signed_request(payload, secret="attacker-guess")
    with pytest.raises(MetaAPIError, match="signature mismatch"):
        parse_signed_request(forged)


def test_tampered_payload_is_rejected():
    """Swap the user_id but keep the original signature."""
    original = _make_signed_request({"algorithm": "HMAC-SHA256", "user_id": "111"})
    signature = original.split(".", 1)[0]
    swapped = _b64(json.dumps({"algorithm": "HMAC-SHA256", "user_id": "999"}).encode())
    with pytest.raises(MetaAPIError, match="signature mismatch"):
        parse_signed_request(f"{signature}.{swapped}")


def test_unsupported_algorithm_is_rejected():
    """An attacker must not be able to downgrade to 'none'."""
    payload = {"algorithm": "none", "user_id": "1234567890"}
    with pytest.raises(MetaAPIError, match="Unsupported"):
        parse_signed_request(_make_signed_request(payload))


@pytest.mark.parametrize("value", ["", "no-dot-separator", "!!!.!!!"])
def test_malformed_input_raises_meta_error(value):
    """Garbage must surface as MetaAPIError (-> HTTP 400), never an unhandled 500."""
    with pytest.raises(MetaAPIError):
        parse_signed_request(value)


def test_authorization_url_carries_state_and_scopes(monkeypatch):
    monkeypatch.setattr(settings, "META_APP_ID", "app-123")
    monkeypatch.setattr(settings, "META_REDIRECT_URI", "https://x.test/cb")

    url = build_authorization_url("state-abc")

    assert "client_id=app-123" in url
    assert "state=state-abc" in url
    assert "instagram_manage_insights" in url
    assert "pages_read_engagement" in url
