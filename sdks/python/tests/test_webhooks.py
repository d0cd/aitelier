"""Tests for the webhook bearer-token verification helper."""

from __future__ import annotations

from aitelier_client import verify_webhook_bearer


def _auth_header(secret: str) -> str:
    """The exact Authorization header value aitelier's worker emits."""
    return f"Bearer {secret}"


def test_verifies_valid_bearer():
    secret = "supersecret"
    assert verify_webhook_bearer(_auth_header(secret), secret) is True


def test_rejects_wrong_secret():
    assert verify_webhook_bearer(_auth_header("right-secret"), "wrong-secret") is False


def test_handles_missing_header():
    """Absent header → False, never raise. Receivers branch on the return."""
    assert verify_webhook_bearer(None, "s") is False


def test_rejects_non_bearer_scheme():
    """A non-`Bearer ` scheme (e.g. a forged Basic header) is rejected."""
    assert verify_webhook_bearer("Basic c3VwZXJzZWNyZXQ=", "supersecret") is False


def test_rejects_bare_token_without_scheme():
    """The secret alone, without the `Bearer ` prefix, must not verify."""
    assert verify_webhook_bearer("supersecret", "supersecret") is False


def test_constant_time_comparison_does_not_short_circuit():
    """Smoke check: hmac.compare_digest is used, so two near-misses take
    similar time. We can't measure timing reliably in a unit test; the
    substantive guarantee is that we use compare_digest and not `==`."""
    import inspect

    from aitelier_client.webhooks import verify_webhook_bearer as fn
    src = inspect.getsource(fn)
    assert "compare_digest" in src, (
        "verify_webhook_bearer must use hmac.compare_digest "
        "(not `==`) to avoid timing leaks"
    )
