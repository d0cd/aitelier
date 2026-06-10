"""Concurrency live tests: simultaneous runs don't interfere; idempotency
collapses parallel duplicates to a single run.

Threading + httpx.Client is intentional — runs the requests in parallel
from independent clients so aitelier sees genuine concurrent traffic
(rather than serialized async POSTs on a single connection).
"""

from __future__ import annotations

import threading
import uuid

import httpx


def _post_run(base_url, json_body, headers, results, idx):
    with httpx.Client(timeout=180) as c:
        r = c.post(f"{base_url}/v1/chat/completions",
                   json=json_body, headers=headers or {})
        results[idx] = r


def test_concurrent_runs_dont_share_state(http, base_url, agent_backend, trace_tag):
    """Two concurrent agent runs with distinct keys complete
    independently — distinct run_ids, distinct events."""
    body_template = {
        "model": f"agent:{agent_backend}",
        "messages": [{"role": "user", "content": "ack"}],
        "timeout": 180,
        "aitelier": {"max_turns": 1, "trace_tag": trace_tag},
    }
    keys = [str(uuid.uuid4()), str(uuid.uuid4())]
    results: dict[int, httpx.Response] = {}
    threads = [
        threading.Thread(
            target=_post_run,
            args=(base_url, body_template,
                  {"Idempotency-Key": keys[i]}, results, i),
        )
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(2):
        assert results[i].status_code == 200, (
            f"concurrent run {i} failed: {results[i].text}"
        )
    rid0 = results[0].json()["aitelier_run_id"]
    rid1 = results[1].json()["aitelier_run_id"]
    assert rid0 != rid1, "concurrent runs collided on run_id"

    # Each run row has its own event sequence.
    events0 = http.get(f"/v1/runs/{rid0}/events").json()
    events1 = http.get(f"/v1/runs/{rid1}/events").json()
    assert events0, f"no events for {rid0}"
    assert events1, f"no events for {rid1}"
    # Sanity: every event_id from one set is absent from the other.
    eids0 = {e["event_id"] for e in events0}
    eids1 = {e["event_id"] for e in events1}
    assert eids0.isdisjoint(eids1), (
        f"event_id collision between concurrent runs: "
        f"intersection={eids0 & eids1}"
    )


def test_concurrent_idempotency_collapse_to_single_run(
    base_url, agent_backend, trace_tag,
):
    """Two parallel POSTs with the same Idempotency-Key + body must end
    up at the same run_id. One races to "first," the other waits on the
    per-key lock, then sees the cached result.

    Before the in-process per-key lock landed, this consistently failed:
    both POSTs missed the cache, both kicked off runs, distinct run_ids
    resulted. Locking the check-then-act window in `_check_idempotency`
    (server.py) closes the race for single-process aitelier deployments
    — the standard shape."""
    body = {
        "model": f"agent:{agent_backend}",
        "messages": [{"role": "user", "content": "ack"}],
        "timeout": 180,
        "aitelier": {"max_turns": 1, "trace_tag": trace_tag},
    }
    key = str(uuid.uuid4())
    results: dict[int, httpx.Response] = {}
    threads = [
        threading.Thread(
            target=_post_run,
            args=(base_url, body, {"Idempotency-Key": key}, results, i),
        )
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(2):
        assert results[i].status_code == 200, (
            f"concurrent idempotent run {i} failed: {results[i].text}"
        )
    rid0 = results[0].json()["aitelier_run_id"]
    rid1 = results[1].json()["aitelier_run_id"]
    assert rid0 == rid1, (
        f"idempotency-collapsed runs returned different run_ids: "
        f"{rid0} vs {rid1}. The per-key lock should have serialized them."
    )
