"""Live tests for `Idempotency-Key` on the agent path of /v1/chat/completions.

Retried agent submissions must not re-trigger side effects (prepare.commands,
sidecars, etc.). The server-side cache hangs off the durable store.
"""

from __future__ import annotations

import uuid


def _agent_body(agent: str, content: str = "Reply: ack",
                aitelier_opts: dict | None = None) -> dict:
    body = {
        "model": f"agent:{agent}",
        "messages": [{"role": "user", "content": content}],
        "timeout": 30,
    }
    if aitelier_opts:
        body["aitelier"] = aitelier_opts
    return body


def test_same_key_and_body_returns_cached_response(http, trace_tag, picked_agent):
    """Second POST with the same Idempotency-Key + body returns the same
    body byte-for-byte. The work runs exactly once."""
    agent = picked_agent
    key = str(uuid.uuid4())
    body = _agent_body(agent, aitelier_opts={"max_turns": 1, "trace_tag": trace_tag})
    from .conftest import skip_on_upstream_unavailable
    r1 = http.post("/v1/chat/completions", headers={"Idempotency-Key": key},
                    json=body)
    r2 = http.post("/v1/chat/completions", headers={"Idempotency-Key": key},
                    json=body)
    skip_on_upstream_unavailable(r1)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["aitelier_run_id"] == r2.json()["aitelier_run_id"]
    assert r1.json() == r2.json()


def test_same_key_different_body_returns_422(http, picked_agent):
    """Reusing a key with a different body is almost always a consumer bug;
    the server should refuse loud rather than treat as a new request."""
    agent = picked_agent
    key = str(uuid.uuid4())
    from .conftest import skip_on_upstream_unavailable
    r1 = http.post("/v1/chat/completions",
                    headers={"Idempotency-Key": key},
                    json=_agent_body(agent, "first call",
                                      aitelier_opts={"max_turns": 1}))
    r2 = http.post("/v1/chat/completions",
                    headers={"Idempotency-Key": key},
                    json=_agent_body(agent, "DIFFERENT",
                                      aitelier_opts={"max_turns": 1}))
    skip_on_upstream_unavailable(r1)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 422
    assert "Idempotency-Key" in r2.json()["detail"]


def test_distinct_keys_produce_distinct_runs(http, trace_tag, picked_agent):
    """Sanity check: fresh keys don't share state."""
    agent = picked_agent
    body = _agent_body(agent, aitelier_opts={"max_turns": 1,
                                              "trace_tag": trace_tag})
    from .conftest import skip_on_upstream_unavailable
    r1 = http.post("/v1/chat/completions",
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                    json=body)
    r2 = http.post("/v1/chat/completions",
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                    json=body)
    skip_on_upstream_unavailable(r1)
    skip_on_upstream_unavailable(r2)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["aitelier_run_id"] != r2.json()["aitelier_run_id"]
