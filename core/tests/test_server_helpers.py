"""Unit tests for server.py projection + finalization helpers."""

from __future__ import annotations

from aitelier.server import _redact_secrets, _terminal_state_from_final


def test_redact_secrets_redacts_credential_keys_case_insensitively():
    """`password` / `access_token` / a capitalized `Authorization` must be
    redacted — not just the lowercase api_key/token/secret/authorization set."""
    payload = {
        "api_key": "sk-aaa",
        "Authorization": "Bearer xyz",          # capitalized — was leaking
        "password": "hunter2",                  # was leaking
        "access_token": "at-123",
        "nested": {"client_secret": "cs-9", "keep": "visible"},
    }
    out = _redact_secrets(payload)
    assert out["api_key"] == "[redacted]"
    assert out["Authorization"] == "[redacted]"
    assert out["password"] == "[redacted]"
    assert out["access_token"] == "[redacted]"
    assert out["nested"]["client_secret"] == "[redacted]"
    assert out["nested"]["keep"] == "visible"


def test_redact_secrets_headers_value_shape():
    """The ACP [{name, value}] header shape: name kept, value redacted."""
    out = _redact_secrets({"headers": [{"name": "Authorization", "value": "Bearer t"}]})
    assert out["headers"] == [{"name": "Authorization", "value": "[redacted]"}]


def test_redact_secrets_passes_through_non_dict_items_under_headers_env():
    """A list of bare strings under a key named env/headers (e.g. env-var NAMES
    to inherit) is not credential-shaped — values must pass through, not be
    clobbered to [redacted]."""
    out = _redact_secrets({"env": ["PATH", "HOME"], "headers": ["Accept"]})
    assert out["env"] == ["PATH", "HOME"]
    assert out["headers"] == ["Accept"]


def test_redact_secrets_leaves_non_secret_scalars():
    out = _redact_secrets({"model": "claude", "count": 3, "ok": True})
    assert out == {"model": "claude", "count": 3, "ok": True}


def test_terminal_state_from_final():
    assert _terminal_state_from_final({"error_type": "Cancelled"}) == "cancelled"
    assert _terminal_state_from_final({"status": "error"}) == "failed"
    assert _terminal_state_from_final(
        {"status": "ok", "finish_reason": "stop"}) == "completed"
    # Cancelled wins even if status is also error.
    assert _terminal_state_from_final(
        {"error_type": "Cancelled", "status": "error"}) == "cancelled"
