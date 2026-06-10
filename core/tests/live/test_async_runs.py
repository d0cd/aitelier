"""Live tests for the async run lifecycle: POST /v1/runs → poll → events → terminal.

Sync agent runs are covered in test_agent_contract.py. This file
exercises the *async* dispatch path: aitelier returns a run_id
immediately, the run executes in the background, and the consumer
polls or waits until terminal.

Parametrized over `agent_backend` since the async path is agent-only
(`/v1/runs` rejects LLM models with 400).
"""

from __future__ import annotations

import time


def _wait_until_in_store(http, run_id, timeout=5.0):
    """`POST /v1/runs` returns the run_id before the durable store has
    written the row (the inner task writes it). Poll until the row is
    visible so dependent tests aren't racy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = http.get(f"/v1/runs/{run_id}")
        if r.status_code == 200:
            return r.json()
        time.sleep(0.05)
    raise AssertionError(
        f"run {run_id} never appeared in /v1/runs/{{id}} within {timeout}s — "
        f"async write didn't land. Check the runner."
    )


def test_async_run_lifecycle(http, agent_backend, trace_tag):
    """End-to-end async path: POST → row appears → poll until terminal
    → events present → final state recorded."""
    submit = http.post("/v1/runs", json={
        "model": f"agent:{agent_backend}",
        "messages": [{"role": "user", "content": "ack"}],
        "timeout": 240,
        "aitelier": {"max_turns": 1, "trace_tag": trace_tag},
    })
    submit.raise_for_status()
    accepted = submit.json()
    assert accepted["status"] == "accepted"
    run_id = accepted["run_id"]
    assert accepted["correlation_id"]

    _wait_until_in_store(http, run_id, timeout=10)

    # Poll until terminal. Use /v1/runs/{id}/wait — clean long-poll path
    # rather than busy-polling ourselves.
    wait = http.post(f"/v1/runs/{run_id}/wait", params={"timeout": 180})
    wait.raise_for_status()
    final = wait.json()
    assert final["run_id"] == run_id
    assert final["state"] in ("completed", "failed", "cancelled", "orphaned"), final
    assert final["kind"] == "agent"
    assert final["agent_id"] == agent_backend

    # Events recorded (start at minimum).
    events = http.get(f"/v1/runs/{run_id}/events").json()
    kinds = {e["kind"] for e in events}
    assert "start" in kinds, kinds


def test_async_run_rejects_llm_model(http):
    """/v1/runs is agent-only — LLM models should be rejected upfront so
    consumers don't pay for an async wrapper that adds no value."""
    r = http.post("/v1/runs", json={
        "model": "local",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 400, r.text
    assert "/v1/chat/completions" in r.json()["detail"]


def test_async_run_idempotency_same_key_returns_same_run_id(
    http, agent_backend, trace_tag,
):
    """Re-POSTing with the same Idempotency-Key returns the original
    run_id; the work is enqueued exactly once."""
    import uuid as _uuid
    key = str(_uuid.uuid4())
    body = {
        "model": f"agent:{agent_backend}",
        "messages": [{"role": "user", "content": "ack"}],
        "timeout": 240,
        "aitelier": {"max_turns": 1, "trace_tag": trace_tag},
    }
    r1 = http.post("/v1/runs", headers={"Idempotency-Key": key}, json=body)
    r2 = http.post("/v1/runs", headers={"Idempotency-Key": key}, json=body)
    r1.raise_for_status()
    r2.raise_for_status()
    assert r1.json()["run_id"] == r2.json()["run_id"]
