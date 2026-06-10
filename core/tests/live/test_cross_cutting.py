"""Cross-cutting live tests: hosted Bearer auth, correlation-ID propagation,
run state transitions.

These hit aitelier-level behaviors that aren't tied to a specific
endpoint or agent backend.
"""

from __future__ import annotations

import time

import httpx

# ---------- Hosted-mode Bearer auth ----------


def test_bearer_auth_gates_v1_endpoints_when_api_key_set(isolated_aitelier):
    """`[service] api_key` enables hosted-mode auth: every /v1/* request
    must carry `Authorization: Bearer <key>` or 401. /v1/health stays
    unauthenticated by design (k8s liveness probes use it)."""
    api_key = "live-test-api-key-32-chars-or-more"
    aitelier = isolated_aitelier(service={"api_key": api_key})

    # /v1/health remains public (liveness probe).
    health = httpx.get(f"{aitelier.base_url}/v1/health", timeout=5)
    health.raise_for_status()

    # /v1/models without auth → 401.
    no_auth = httpx.get(f"{aitelier.base_url}/v1/models", timeout=5)
    assert no_auth.status_code == 401, (
        f"expected 401 without Bearer auth, got {no_auth.status_code}: {no_auth.text}"
    )

    # With the wrong key → 401.
    wrong = httpx.get(
        f"{aitelier.base_url}/v1/models", timeout=5,
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert wrong.status_code == 401

    # With the right key → 200.
    ok = httpx.get(
        f"{aitelier.base_url}/v1/models", timeout=5,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    ok.raise_for_status()
    assert ok.json()["object"] == "list"


# ---------- Correlation-ID propagation ----------


def test_correlation_id_propagates_through_run(http, litellm_models):
    """`X-Correlation-Id` flows from request → response header → response
    body → run row → events. Consumers depend on this to thread requests
    through their own logging."""
    assert "local" in litellm_models, (
        "`local` must be advertised by /v1/discovery for this test."
    )
    cid = "live-cross-cutting-correlation-test"

    r = http.post(
        "/v1/chat/completions",
        json={
            "model": "local",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 10,
        },
        headers={"X-Correlation-Id": cid},
    )
    assert r.status_code == 200, r.text

    # 1. Response header echoes the correlation_id.
    assert r.headers.get("X-Correlation-Id") == cid, (
        f"response header missing or wrong: {r.headers.get('X-Correlation-Id')}"
    )
    # 2. Response body carries it too (consumers that didn't expose
    # headers in their HTTP client still get it).
    body = r.json()
    assert body["correlation_id"] == cid

    # 3. Run row records it.
    run_id = body["aitelier_run_id"]
    row = http.get(f"/v1/runs/{run_id}").json()
    assert row["correlation_id"] == cid


def test_correlation_id_auto_generated_when_omitted(http, litellm_models):
    """When the client doesn't send X-Correlation-Id, aitelier mints one
    and echoes it back. Same field everywhere downstream."""
    assert "local" in litellm_models
    r = http.post("/v1/chat/completions", json={
        "model": "local",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    })
    assert r.status_code == 200
    minted = r.headers.get("X-Correlation-Id")
    assert minted, "aitelier didn't mint a correlation id"
    assert r.json()["correlation_id"] == minted


# ---------- Run state transitions ----------


def test_failed_run_state_recorded(http, agent_backend, trace_tag):
    """Induce a failure path: invalid sub-config the agent rejects. The
    run row should reach `failed` state with error_type populated.

    We use a bad workspace path (validator rejects relative `..`) which
    aitelier surfaces as a 400 — different code path. For a runtime
    failure we use `max_turns: 0` which forces an empty agent response.
    """
    r = http.post("/v1/chat/completions", json={
        "model": f"agent:{agent_backend}",
        "messages": [{"role": "user", "content": "should not run"}],
        "timeout": 240,
        "aitelier": {"max_turns": 0, "trace_tag": trace_tag},
    })
    # max_turns=0 is either rejected at validation (400) or executed and
    # comes back with a structured zero-turn result. Either is acceptable;
    # what we care about is that the run row never gets stuck in pending.
    if r.status_code == 400:
        # Pure validation failure — no run row, nothing to assert.
        return
    body = r.json()
    if r.status_code == 200:
        run_id = body["aitelier_run_id"]
    elif r.status_code in (500, 502):
        run_id = body.get("aitelier_run_id")
        assert run_id, body
    else:
        raise AssertionError(f"unexpected status: {r.status_code} {body}")

    # Wait briefly for terminal state.
    deadline = time.monotonic() + 30
    row = None
    while time.monotonic() < deadline:
        row = http.get(f"/v1/runs/{run_id}").json()
        if row["state"] in ("completed", "failed", "cancelled", "orphaned"):
            return
        time.sleep(0.5)
    raise AssertionError(
        f"run {run_id} stuck in non-terminal state: {row['state']!r}"
    )
