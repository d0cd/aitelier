"""Live tests for /v1/runs/active and /v1/runs/{id}/cancel that are
backend-agnostic. Agent-path cancellation lives in test_agent_contract.py
(parameterized over backends)."""

from __future__ import annotations


def test_active_runs_endpoint_shape(http):
    """Even with nothing in flight, the endpoint should respond with the
    documented shape: {active: list[str]}."""
    r = http.get("/v1/runs/active")
    r.raise_for_status()
    body = r.json()
    assert isinstance(body, dict)
    assert isinstance(body.get("active"), list)


def test_cancel_unknown_run_returns_404(http):
    """Cancelling a run that isn't in the active registry should 404,
    not 500 — the consumer can tell missing vs failure apart."""
    r = http.post("/v1/runs/some-nonexistent-run/cancel")
    assert r.status_code == 404
