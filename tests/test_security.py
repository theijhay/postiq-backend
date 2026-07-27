import pytest

from api.core.security import (
    decrypt_bytes,
    decrypt_str,
    encrypt_bytes,
    encrypt_str,
    generate_secret,
    hash_password,
    hash_secret,
    verify_password,
)


def test_aes_gcm_round_trip():
    plaintext = b"embedding-bytes-\x00\x01\x02" * 20
    blob = encrypt_bytes(plaintext)
    assert blob != plaintext
    assert decrypt_bytes(blob) == plaintext


def test_aes_gcm_unique_nonces():
    assert encrypt_bytes(b"same") != encrypt_bytes(b"same")


def test_aes_gcm_tamper_detected():
    blob = bytearray(encrypt_bytes(b"payload"))
    blob[-1] ^= 0xFF
    with pytest.raises(Exception):
        decrypt_bytes(bytes(blob))


def test_hash_secret_deterministic():
    assert hash_secret("abc") == hash_secret("abc")
    assert hash_secret("abc") != hash_secret("abd")


def test_generate_secret_prefix_and_uniqueness():
    a, b = generate_secret("blk"), generate_secret("blk")
    assert a.startswith("blk_") and b.startswith("blk_")
    assert a != b


def test_token_vault_str_round_trip():
    """The shape actually used for Meta access tokens."""
    token = "EAABsbCS1i" + "x" * 180
    blob = encrypt_str(token)
    assert isinstance(blob, bytes)
    assert token.encode() not in blob  # never stored recoverably
    assert decrypt_str(blob) == token


def test_password_hash_is_salted():
    a, b = hash_password("correct horse"), hash_password("correct horse")
    assert a != b  # distinct salts
    assert verify_password("correct horse", a)
    assert verify_password("correct horse", b)


def test_password_rejects_wrong_value():
    stored = hash_password("correct horse")
    assert not verify_password("wrong horse", stored)


def test_password_beyond_bcrypt_72_byte_limit():
    """bcrypt truncates at 72 bytes; the sha256 pre-hash must remove that.

    Without pre-hashing, these two differ only past byte 72 and would verify
    against each other.
    """
    base = "x" * 72
    stored = hash_password(base + "AAAA")
    assert verify_password(base + "AAAA", stored)
    assert not verify_password(base + "BBBB", stored)


def test_verify_password_tolerates_malformed_hash():
    """A corrupt hash in the DB is a failed login, not a 500."""
    assert not verify_password("anything", "not-a-bcrypt-hash")
