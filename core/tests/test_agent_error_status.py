"""The agent error path must map failure classes to HTTP status the same way
the LLM path does — not collapse every agent failure to HTTP 500, which breaks
status-code-based retry/backoff for OpenAI-SDK consumers."""

from __future__ import annotations

from aitelier.openai_compat import chat_completion_error_envelope
from aitelier.server import _http_status_for_agent_error


def test_agent_error_status_mapping():
    """Each failure class maps to the same status the LLM path uses
    (504/503/429/401), request-schema errors to 400, and everything else to
    502 — never a misleading 500."""
    assert _http_status_for_agent_error({"error_type": "Timeout"}) == 504
    assert _http_status_for_agent_error({"error_type": "ProviderUnavailable"}) == 503
    assert _http_status_for_agent_error({"error_type": "RateLimited"}) == 429
    assert _http_status_for_agent_error({"error_type": "AuthError"}) == 401
    assert _http_status_for_agent_error({"error_type": "SchemaViolation"}) == 400
    assert _http_status_for_agent_error({"error_type": "ProviderError"}) == 502
    # The whole point of the fix: an opaque agent error is 502, never 500.
    assert _http_status_for_agent_error({"error_type": None}) == 502
    assert _http_status_for_agent_error({}) == 502


def test_agent_timeout_error_envelope_yields_504_not_500():
    """End-to-end: a timeout result, stamped and run through the same envelope
    the agent path uses, yields 504 — the status retry middleware branches on —
    not 500."""
    result = {
        "status": "error", "error_type": "Timeout",
        "error_msg": "agent timed out", "finish_reason": "timeout", "run_id": "r-1",
    }
    result.setdefault("_aitelier_http_status", _http_status_for_agent_error(result))
    body = chat_completion_error_envelope(result, run_id="r-1", correlation_id="c-1")
    assert body["aitelier_status_code"] == 504
    assert body["error"]["type"] == "Timeout"
