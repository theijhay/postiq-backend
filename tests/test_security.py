import pytest

from api.core.security import decrypt_bytes, encrypt_bytes, generate_secret, hash_secret


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
