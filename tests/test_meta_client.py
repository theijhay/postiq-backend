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
    page_ids_from_granular_scopes,
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
    # Pin the scopes rather than inheriting whatever .env happens to hold —
    # META_SCOPES is deliberately environment-tunable, so reading the real value
    # here would make this test fail on a developer machine mid-configuration.
    monkeypatch.setattr(
        settings, "META_SCOPES", "pages_read_engagement,instagram_manage_insights"
    )

    url = build_authorization_url("state-abc")

    assert "client_id=app-123" in url
    assert "state=state-abc" in url
    assert "instagram_manage_insights" in url
    assert "pages_read_engagement" in url


class TestPageIdsFromGranularScopes:
    """Recovering granted Pages when /me/accounts returns nothing.

    Shape below is a real debug_token response observed on 2026-07-28, where
    the Page was granted but the listing endpoint reported none.
    """

    REAL_PAYLOAD = {
        "type": "USER",
        "app_id": "2051818322130149",
        "granular_scopes": [
            {"scope": "pages_show_list", "target_ids": ["104177568454287"]},
            {"scope": "pages_read_engagement", "target_ids": ["104177568454287"]},
        ],
    }

    def test_extracts_granted_page_id(self):
        assert page_ids_from_granular_scopes(self.REAL_PAYLOAD) == ["104177568454287"]

    def test_deduplicates_across_scopes(self):
        """The same Page appears under every page scope; report it once."""
        assert len(page_ids_from_granular_scopes(self.REAL_PAYLOAD)) == 1

    def test_ignores_non_page_scopes(self):
        payload = {
            "granular_scopes": [
                {"scope": "instagram_basic", "target_ids": ["ig-999"]},
                {"scope": "pages_show_list", "target_ids": ["page-1"]},
            ]
        }
        assert page_ids_from_granular_scopes(payload) == ["page-1"]

    def test_preserves_order_across_multiple_pages(self):
        payload = {
            "granular_scopes": [
                {"scope": "pages_show_list", "target_ids": ["p1", "p2"]},
                {"scope": "pages_read_engagement", "target_ids": ["p2", "p3"]},
            ]
        }
        assert page_ids_from_granular_scopes(payload) == ["p1", "p2", "p3"]

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"granular_scopes": []},
            {"granular_scopes": None},
            {"granular_scopes": [{"scope": "pages_show_list"}]},  # no target_ids
            {"granular_scopes": [{"scope": "pages_show_list", "target_ids": None}]},
        ],
    )
    def test_missing_or_malformed_data_yields_empty_list(self, payload):
        assert page_ids_from_granular_scopes(payload) == []


def test_authorization_url_reflects_configured_scopes(monkeypatch):
    """Narrowing META_SCOPES must actually narrow the dialog request."""
    monkeypatch.setattr(settings, "META_APP_ID", "app-123")
    monkeypatch.setattr(settings, "META_SCOPES", "pages_show_list, pages_read_engagement")

    url = build_authorization_url("state-abc")

    assert "instagram_manage_insights" not in url
    assert "pages_show_list" in url
    # Whitespace around the comma must not leak into the request.
    assert "+pages_read_engagement" not in url and "%20" not in url
