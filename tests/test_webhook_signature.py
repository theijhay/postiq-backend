import hashlib
import hmac

from api.providers.paystack_adapter import PaystackAdapter


def _sign(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()


def test_paystack_signature_valid():
    adapter = PaystackAdapter(secret_key="sk_test_abc")
    payload = b'{"event":"charge.success"}'
    assert adapter.verify_webhook_signature(payload, _sign("sk_test_abc", payload))


def test_paystack_signature_invalid():
    adapter = PaystackAdapter(secret_key="sk_test_abc")
    payload = b'{"event":"charge.success"}'
    assert not adapter.verify_webhook_signature(payload, _sign("wrong-key", payload))
    assert not adapter.verify_webhook_signature(payload, "")
    assert not adapter.verify_webhook_signature(payload + b" ", _sign("sk_test_abc", payload))
