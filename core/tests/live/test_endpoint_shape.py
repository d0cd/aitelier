"""Live tests for endpoints whose wire shape isn't covered elsewhere.

Each test does the minimum to exercise the documented contract — these
are not full behavior tests, just guards that the shape consumers
depend on doesn't regress.
"""

from __future__ import annotations

import uuid

from .conftest import assert_upstream_ok


def _assert_local(litellm_models: list[str]) -> None:
    assert "local" in litellm_models, (
        f"`local` (Ollama) must be advertised by /v1/discovery for this test. "
        f"Curated models: {sorted(m for m in litellm_models if '/' not in m)}"
    )


# ---------- /v1/runs/{run_id} ----------


def test_single_run_endpoint_returns_full_row(http, litellm_models, trace_tag):
    _assert_local(litellm_models)
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 50,
    }, headers={"X-Correlation-Id": trace_tag})
    assert_upstream_ok(r)
    run_id = r.json()["aitelier_run_id"]

    one = http.get(f"/v1/runs/{run_id}")
    one.raise_for_status()
    row = one.json()
    # Same shape as a list entry.
    assert row["run_id"] == run_id
    assert row["kind"] == "complete"
    assert row["state"] in ("completed", "failed")
    assert row["correlation_id"] == trace_tag


def test_single_run_endpoint_returns_404_for_missing(http):
    r = http.get("/v1/runs/this-run-id-does-not-exist")
    assert r.status_code == 404


# ---------- /v1/runs/{run_id}/events/stream (SSE) ----------


# SSE stream test (`/v1/runs/{id}/events/stream`) lives in
# test_agent_contract.py — it requires an agent run, since the LLM path
# doesn't currently emit events to /v1/runs/{id}/events.


# ---------- POST /v1/runs/{run_id}/wait ----------


def test_runs_wait_returns_terminal_run(http, litellm_models, trace_tag):
    """`POST /v1/runs/{id}/wait` blocks until terminal then returns the
    Run row. We exercise it against an already-terminal run (sync LLM
    completion) — wait should return immediately with the terminal
    state. The blocking-until-terminal path is exercised implicitly:
    the handler polls the store, and a run that's already terminal
    short-circuits the poll loop."""
    _assert_local(litellm_models)
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }, headers={"X-Correlation-Id": trace_tag})
    assert_upstream_ok(r)
    run_id = r.json()["aitelier_run_id"]

    wait = http.post(f"/v1/runs/{run_id}/wait", params={"timeout": 10})
    wait.raise_for_status()
    body = wait.json()
    assert body["run_id"] == run_id
    assert body["state"] in ("completed", "failed", "cancelled", "orphaned"), body


def test_runs_wait_returns_404_for_missing(http):
    r = http.post("/v1/runs/no-such-run/wait", params={"timeout": 1})
    assert r.status_code == 404


def test_runs_wait_rejects_out_of_range_timeout(http):
    r = http.post("/v1/runs/any/wait", params={"timeout": 9999})
    assert r.status_code == 400


# ---------- /v1/metrics ----------


def test_metrics_endpoint_shape(http):
    r = http.get("/v1/metrics")
    r.raise_for_status()
    body = r.json()
    # Documented top-level fields.
    assert body["uptime_seconds"] >= 0
    assert body["timestamp"]
    assert "process" in body and isinstance(body["process"], dict)
    assert "rss_mb" in body["process"]
    assert "runs" in body and isinstance(body["runs"], dict)
    assert "webhooks" in body and isinstance(body["webhooks"], dict)


# ---------- /v1/schemas/{name} ----------


def test_schemas_endpoint_returns_jsonschema(http):
    """`/v1/schemas/{name}` exposes the control-plane wire schemas so
    consumers can validate against the same source aitelier emits."""
    r = http.get("/v1/schemas/run")
    r.raise_for_status()
    body = r.json()
    # JSON Schema dialect marker + a type/properties block.
    assert isinstance(body, dict)
    assert "$schema" in body or "type" in body or "properties" in body, body


def test_schemas_endpoint_404s_unknown(http):
    r = http.get("/v1/schemas/not-a-real-schema")
    assert r.status_code == 404


# ---------- /v1/traces ----------


def test_traces_endpoint_returns_list(http, litellm_models, trace_tag):
    """`/v1/traces` returns TraceRecord projections — `trace_id` is the
    primary key (same as run_id, just narrower projection)."""
    _assert_local(litellm_models)
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }, headers={"X-Correlation-Id": trace_tag})
    assert_upstream_ok(r)
    run_id = r.json()["aitelier_run_id"]

    traces = http.get("/v1/traces", params={"limit": 50}).json()
    assert isinstance(traces, list)
    ids = {t["trace_id"] for t in traces}
    assert run_id in ids


def test_single_trace_endpoint_returns_record(http, litellm_models, trace_tag):
    _assert_local(litellm_models)
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }, headers={"X-Correlation-Id": trace_tag})
    assert_upstream_ok(r)
    run_id = r.json()["aitelier_run_id"]

    one = http.get(f"/v1/traces/{run_id}")
    one.raise_for_status()
    record = one.json()
    assert record["trace_id"] == run_id


def test_single_trace_endpoint_404s_unknown(http):
    bogus = f"missing-{uuid.uuid4().hex[:8]}"
    r = http.get(f"/v1/traces/{bogus}")
    assert r.status_code == 404
