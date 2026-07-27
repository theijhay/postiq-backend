"""Cryptographic primitives: token vault, secret hashing, password hashing.

Two distinct hashing concerns live here, deliberately kept apart:

* ``hash_secret``   — deterministic SHA-256. For values we need to *look up* by
  hash (API keys, OAuth state). Fast and unsalted by design.
* ``hash_password`` — salted bcrypt. For user passwords. Slow by design.

Never substitute one for the other.
"""

import base64
import functools
import hashlib
import secrets

import bcrypt
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from api.utils.settings import settings

# AES-GCM standard nonce length. Prepended to every ciphertext blob.
_NONCE_BYTES = 12
_KEY_BYTES = 32  # AES-256


@functools.lru_cache(maxsize=1)
def _key() -> bytes:
    """Decode and validate the vault key.

    Resolved lazily (not at import) so that a missing key surfaces as a clear
    error when crypto is first used, rather than breaking module import.
    """
    try:
        raw = base64.b64decode(settings.ENCRYPTION_KEY, validate=True)
    except Exception as exc:  # noqa: BLE001 - re-raised with actionable guidance
        raise ValueError(
            "ENCRYPTION_KEY is not valid base64. Generate one with: "
            'python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"'
        ) from exc

    if len(raw) != _KEY_BYTES:
        raise ValueError(
            f"ENCRYPTION_KEY must decode to exactly {_KEY_BYTES} bytes "
            f"(AES-256), got {len(raw)}."
        )
    return raw


def encrypt_bytes(plaintext: bytes) -> bytes:
    """Encrypt with AES-256-GCM. Returns ``nonce || ciphertext || tag``.

    A fresh nonce per call is mandatory for GCM — reusing one against the same
    key breaks confidentiality *and* authentication.
    """
    nonce = secrets.token_bytes(_NONCE_BYTES)
    return nonce + AESGCM(_key()).encrypt(nonce, plaintext, None)


def decrypt_bytes(blob: bytes) -> bytes:
    """Reverse of :func:`encrypt_bytes`.

    Raises ``InvalidTag`` if the blob was tampered with or encrypted under a
    different key. Callers should treat that as a hard failure, not a miss.
    """
    if len(blob) <= _NONCE_BYTES:
        raise InvalidTag("Ciphertext too short to contain a nonce.")
    nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    return AESGCM(_key()).decrypt(nonce, ciphertext, None)


def encrypt_str(plaintext: str) -> bytes:
    """Convenience wrapper for the common case of encrypting an access token."""
    return encrypt_bytes(plaintext.encode())


def decrypt_str(blob: bytes) -> str:
    return decrypt_bytes(blob).decode()


def hash_secret(secret: str) -> str:
    """Deterministic SHA-256 hex digest — for hash-based *lookup*, not passwords."""
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_secret(prefix: str) -> str:
    """Generate a prefixed, URL-safe random secret, e.g. ``pk_xTf9...``."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _prehash(password: str) -> bytes:
    """SHA-256 -> base64 before bcrypt.

    bcrypt silently truncates input at 72 bytes, so a long passphrase would
    only be validated up to that point. Pre-hashing normalises every password
    to 44 bytes and removes the limit. Applied on both hash and verify, so the
    two stay consistent.
    """
    return base64.b64encode(hashlib.sha256(password.encode()).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode())
    except (ValueError, TypeError):
        # Malformed/legacy hash in the DB — treat as a failed login, not a 500.
        return False
