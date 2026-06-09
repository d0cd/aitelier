"""Tests for the webhook-signature verification helper."""

from __future__ import annotations

import hashlib
import hmac

from aitelier_client import verify_webhook_signature


def _sign(body: bytes, secret: str) -> str:
    """The exact header value aitelier emits."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verifies_valid_signature():
    body = b'{"run_id":"r-1","status":"completed"}'
    secret = "supersecret"
    sig = _sign(body, secret)
    assert verify_webhook_signature(body, sig, secret) is True


def test_rejects_tampered_body():
    body = b'{"run_id":"r-1","status":"completed"}'
    secret = "supersecret"
    sig = _sign(body, secret)
    tampered = b'{"run_id":"r-1","status":"FAILED"}'
    assert verify_webhook_signature(tampered, sig, secret) is False


def test_rejects_wrong_secret():
    body = b'{"hello":"world"}'
    sig = _sign(body, "right-secret")
    assert verify_webhook_signature(body, sig, "wrong-secret") is False


def test_handles_missing_header():
    """Absent header → False, never raise. Receivers branch on the return."""
    assert verify_webhook_signature(b"anything", None, "s") is False


def test_rejects_unknown_signature_scheme():
    """The header must start with `sha256=`. Anything else (e.g. a
    forged `md5=...` prefix) is rejected immediately."""
    body = b"{}"
    secret = "s"
    sig = "md5=" + hashlib.md5(body).hexdigest()  # noqa: S324 — intentional bad sig
    assert verify_webhook_signature(body, sig, secret) is False


def test_constant_time_comparison_does_not_short_circuit():
    """Smoke check: hmac.compare_digest is used, so two near-misses
    take similar time. We can't measure timing in a unit test reliably;
    the substantive guarantee is that we use compare_digest and not `==`."""
    import inspect

    from aitelier_client.webhooks import verify_webhook_signature as fn
    src = inspect.getsource(fn)
    assert "compare_digest" in src, (
        "verify_webhook_signature must use hmac.compare_digest "
        "(not `==`) to avoid timing leaks"
    )
