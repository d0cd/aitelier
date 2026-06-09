"""Live tests for /v1/runs/active and /v1/runs/{id}/cancel.

Cancellation has its own per-process active-run registry; this verifies
the registry actually surfaces in-flight runs and that cancel signals
land on them.
"""

from __future__ import annotations

import time

import pytest


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


def test_cancel_active_run_returns_cancelled_ack(http, trace_tag, picked_agent):
    """Start an async agent run, cancel while it's active, verify two contracts:
      1. /v1/runs/{id}/cancel returns 200 with cancelled=True
      2. The run reaches a terminal state durably (any of cancelled / failed /
         completed — a fast backend can race the cancel to a natural finish)
    """
    agent = picked_agent
    r = http.post("/v1/runs", json={
        "model": f"agent:{agent}",
        "messages": [{"role": "user", "content": "a" * 1000}],
        "timeout": 60,
        "aitelier": {"max_turns": 5, "trace_tag": trace_tag},
    })
    r.raise_for_status()
    run_id = r.json()["run_id"]

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        active = http.get("/v1/runs/active").json()["active"]
        if run_id in active:
            break
        time.sleep(0.05)
    else:
        pytest.skip("run never reached the active registry (backend too fast)")

    cancel = http.post(f"/v1/runs/{run_id}/cancel")
    if cancel.status_code == 404:
        # Run finalized (e.g. upstream rate-limited) between our active-check
        # and the cancel POST. Not a cancellation-contract bug.
        pytest.skip("run finished between active-check and cancel — no live test possible")
    assert cancel.status_code == 200, cancel.text
    assert cancel.json() == {"run_id": run_id, "cancelled": True}

    # The run must transition to *some* terminal state, never stuck in
    # pending/running. The exact terminal label depends on which step the
    # cancel signal interrupted. Real coding-agent backends can take ~30s
    # to wind down a Claude/Codex subprocess after CancelledError fires.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        runs = http.get("/v1/runs", params={"trace_tag": trace_tag}).json()
        mine = next((r for r in runs if r["run_id"] == run_id), None)
        if mine and mine["state"] in ("cancelled", "completed", "failed"):
            return
        time.sleep(0.5)
    pytest.fail(f"run {run_id} never reached a terminal state after cancel")
