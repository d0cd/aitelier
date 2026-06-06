"""Tests for agent provider — error/timeout helpers re-exported from sandbox_agent."""

from __future__ import annotations

from aitelier.providers.agent import (
    _error_result,
    _timeout_result,
    call_agent,
)


def test_timeout_result_is_error():
    r = _timeout_result("claude-code", "run-1", 600.0)
    assert r["status"] == "error"
    assert r["error_type"] == "Timeout"
    assert r["finish_reason"] == "timeout"
    assert r["duration_s"] == 600.0
    assert r["provider"] == "claude-code"


def test_error_result_classifies_connection_error():
    r = _error_result("codex", "run-2", ConnectionError("refused"), 1.0)
    assert r["status"] == "error"
    assert r["error_type"] == "ProviderUnavailable"
    assert "refused" in r["error_msg"]


def test_error_result_classifies_unknown_exception():
    r = _error_result("claude-code", "run-3", RuntimeError("weird"), 0.5)
    assert r["status"] == "error"
    # RuntimeError isn't in the error map, so it falls through as its class name
    assert r["error_type"] == "RuntimeError"


def test_call_agent_is_sandbox_call():
    """call_agent in providers.agent is an alias for call_via_sandbox."""
    from aitelier.providers.sandbox_agent import call_via_sandbox
    assert call_agent is call_via_sandbox
