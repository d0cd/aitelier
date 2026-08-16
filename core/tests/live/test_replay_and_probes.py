"""Live tests for run replay, the liveness/readiness split, and the
score-name aggregate.

These three shipped together and share one property: the unit suite
exercises them against mocked dispatch, so the contract they promise —
"a finalized run can be re-dispatched from its captured body", "readyz
answers for traffic, livez for the process", "graded runs roll up by
score name" — is only actually proven here, against a real Sandbox
Agent and a real database.
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture
def agent_model(http, agent_backend) -> str:
    """`agent:<backend>` alone is rejected — the inner model must be named so
    the run records what it actually used. Read it from what the backend
    advertises rather than pinning a model id that drifts."""
    models = http.get("/v1/models", params={"agent_backend": agent_backend}).json()
    for entry in models.get("data", []):
        if entry.get("id") == f"agent:{agent_backend}":
            inner = entry.get("aitelier_inner_llms") or []
            assert inner, f"{agent_backend} advertises no inner models: {entry}"
            return f"agent:{agent_backend}/{inner[0]}"
    raise AssertionError(
        f"/v1/models did not advertise agent:{agent_backend}: {models}")


def _wait_until_in_store(http, run_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = http.get(f"/v1/runs/{run_id}")
        if r.status_code == 200:
            return r.json()
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never appeared within {timeout}s")


def _finished_run(http, agent_model, trace_tag) -> str:
    """Submit a minimal async agent run and block until it finalizes."""
    submit = http.post("/v1/runs", json={
        "model": agent_model,
        "messages": [{"role": "user", "content": "ack"}],
        "timeout": 240,
        # No `max_turns` — it's claude-only, and these tests run across every
        # curated backend. The top-level `timeout` bounds the run instead.
        "aitelier": {"trace_tag": trace_tag},
    })
    submit.raise_for_status()
    run_id = submit.json()["run_id"]
    _wait_until_in_store(http, run_id)
    wait = http.post(f"/v1/runs/{run_id}/wait", params={"timeout": 180})
    wait.raise_for_status()
    return run_id


# --- Liveness vs readiness ---------------------------------------------------


def test_livez_is_cheap_and_unconditional(http):
    """livez answers for the process. A running service always passes it."""
    r = http.get("/v1/livez")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["draining"] is False


def test_readyz_agrees_with_discovery_dependency_state(http):
    """readyz is what a load balancer reads, so it must not disagree with
    the dependency probe that /v1/discovery just performed."""
    deps = (http.get("/v1/discovery").json().get("dependencies") or {})
    any_down = any(
        not (info.get("reachable") if isinstance(info, dict) else True)
        for info in deps.values()
    )

    r = http.get("/v1/readyz")
    body = r.json()

    if any_down:
        assert r.status_code == 503, body
        assert body["ready"] is False
        assert body["unreachable"], body
    else:
        assert r.status_code == 200, body
        assert body["ready"] is True


def test_livez_and_readyz_are_listed_in_discovery(http):
    """A consumer configures probes from /v1/discovery, so both must be
    enumerated there rather than being undocumented paths."""
    endpoints = http.get("/v1/discovery").json().get("endpoints") or []
    paths = {
        e.get("path") if isinstance(e, dict) else e
        for e in endpoints
    }

    assert "/v1/livez" in paths, paths
    assert "/v1/readyz" in paths, paths


# --- Replay ------------------------------------------------------------------


def test_replay_redispatches_a_finalized_run(http, agent_model, trace_tag):
    """The captured request_body really is a sufficient replay input: the
    child run reaches a terminal state and points back at its parent."""
    parent_id = _finished_run(http, agent_model, trace_tag)

    replay = http.post(f"/v1/runs/{parent_id}/replay")
    replay.raise_for_status()
    accepted = replay.json()

    assert accepted["status"] == "accepted"
    assert accepted["parent_run_id"] == parent_id
    child_id = accepted["run_id"]
    assert child_id != parent_id

    _wait_until_in_store(http, child_id)
    final = http.post(f"/v1/runs/{child_id}/wait", params={"timeout": 180})
    final.raise_for_status()
    child = final.json()
    assert child["state"] in ("completed", "failed", "cancelled", "orphaned")
    assert child["parent_run_id"] == parent_id

    # The parent linkage is queryable, which is what makes A/B comparison work.
    listed = http.get("/v1/runs", params={"parent_run_id": parent_id}).json()
    assert child_id in {r["run_id"] for r in listed}


def test_replay_of_in_flight_run_is_refused(http, agent_model, trace_tag):
    """Replaying a run that hasn't finalized would compare against a
    moving target, so the server refuses until it settles."""
    submit = http.post("/v1/runs", json={
        "model": agent_model,
        "messages": [{"role": "user", "content": "count slowly to ten"}],
        "timeout": 240,
        "aitelier": {"trace_tag": trace_tag},
    })
    submit.raise_for_status()
    run_id = submit.json()["run_id"]
    _wait_until_in_store(http, run_id)

    early = http.post(f"/v1/runs/{run_id}/replay")

    # Either the run is still in flight (409) or it already finalized
    # between the two calls (200) — both are correct; a 5xx is not.
    assert early.status_code in (200, 409), early.text
    if early.status_code == 409:
        assert "finalized" in early.json()["detail"]

    http.post(f"/v1/runs/{run_id}/wait", params={"timeout": 180})


def test_replay_of_unknown_run_is_404(http):
    r = http.post(f"/v1/runs/{'0' * 32}/replay")

    assert r.status_code == 404, r.text


# --- Score-name aggregate ----------------------------------------------------


def test_score_name_aggregate_rolls_up_graded_runs(
    http, agent_model, trace_tag,
):
    """A grader writes back, then the aggregate answers 'average score
    across runs in this trace' in one query — the whole point of the
    scoring sink."""
    run_id = _finished_run(http, agent_model, trace_tag)

    for value in (1.0, 0.5):
        posted = http.post(f"/v1/runs/{run_id}/scores", json={
            "name": "live-helpfulness", "value": value,
            "evaluator": "live-suite",
        })
        posted.raise_for_status()

    agg = http.get("/v1/traces/aggregates", params={
        "group_by": "score_name", "trace_tag": trace_tag,
    })
    agg.raise_for_status()
    body = agg.json()

    assert body["group_by"] == "score_name"
    groups = {g["key"]: g for g in body["groups"]}
    assert "live-helpfulness" in groups, body
    bucket = groups["live-helpfulness"]
    # One graded run, two score rows behind it.
    assert bucket["count"] == 1
    assert abs(bucket["avg_value"] - 0.75) < 1e-6, bucket


def test_score_name_aggregate_omits_ungraded_runs(
    http, agent_model, trace_tag,
):
    """An ungraded run must not appear in any score-name group, or the
    denominator of every average is wrong."""
    _finished_run(http, agent_model, trace_tag)

    agg = http.get("/v1/traces/aggregates", params={
        "group_by": "score_name", "trace_tag": trace_tag,
    })
    agg.raise_for_status()
    body = agg.json()

    assert body["groups"] == [], body
    assert body["total"]["count"] == 0, body
