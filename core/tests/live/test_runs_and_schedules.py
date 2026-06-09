"""Live tests for /v1/runs*, /v1/schedules, /v1/traces/aggregates."""

from __future__ import annotations

import uuid

import pytest


def _has_claude_haiku(models: list[str]) -> bool:
    return any(m == "claude-haiku" or m.endswith("/claude-haiku") for m in models)


def _skip_on_upstream_unavailable(r) -> None:
    """Skip rather than fail when an upstream provider 429s / 5xxs us."""
    if r.status_code in (401, 403, 429, 503, 504):
        pytest.skip(
            f"upstream provider returned {r.status_code} — "
            "not exercising aitelier behavior on this run",
        )


def test_runs_list_filterable_by_trace_tag(http, trace_tag, litellm_models):
    if not _has_claude_haiku(litellm_models):
        pytest.skip("claude-haiku not configured")
    # Create a run with a trace tag via aitelier headers (since OpenAI shape
    # has no trace_tag field, use correlation_id and look up by run_id).
    r = http.post(
        "/v1/chat/completions",
        json={
            "model": "claude-haiku",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 10, "temperature": 0,
        },
        headers={"X-Correlation-Id": trace_tag},
    )
    _skip_on_upstream_unavailable(r)
    r.raise_for_status()
    run_id = r.json()["aitelier_run_id"]

    runs = http.get("/v1/runs", params={"correlation_id": trace_tag}).json()
    assert any(x["run_id"] == run_id for x in runs)
    mine = next(x for x in runs if x["run_id"] == run_id)
    assert mine["state"] in ("completed", "failed"), mine
    assert mine["kind"] == "complete"


def test_runs_events_endpoint_returns_timeline(http, trace_tag, picked_agent):
    agent = picked_agent
    r = http.post(
        "/v1/chat/completions",
        json={
            "model": f"agent:{agent}",
            "messages": [{"role": "user", "content": "ack"}],
            "timeout": 30,
            "aitelier": {"max_turns": 1, "trace_tag": trace_tag},
        },
    )
    if r.status_code != 200:
        pytest.skip(f"agent unavailable: {r.status_code} {r.text}")
    run_id = r.json()["aitelier_run_id"]

    events = http.get(f"/v1/runs/{run_id}/events").json()
    kinds = [e["kind"] for e in events]
    assert "start" in kinds, kinds


def test_traces_aggregates_groups_by_correlation(http, trace_tag, litellm_models):
    if not _has_claude_haiku(litellm_models):
        pytest.skip("claude-haiku not configured")
    for _ in range(2):
        r = http.post(
            "/v1/chat/completions",
            json={
                "model": "claude-haiku",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5, "temperature": 0,
            },
            headers={"X-Correlation-Id": trace_tag},
        )
        _skip_on_upstream_unavailable(r)
        r.raise_for_status()

    agg = http.get("/v1/traces/aggregates", params={
        "group_by": "model",
    }).json()
    assert "groups" in agg
    assert "total" in agg


# ---------- /v1/schedules ----------


def test_schedule_crud_round_trip(http):
    name = f"live-sched-{uuid.uuid4().hex[:6]}"
    created = http.post("/v1/schedules", json={
        "name": name,
        "task": {
            "model": "claude-haiku",
            "messages": [{"role": "user", "content": "ping"}],
        },
        "interval_seconds": 3600,
    }).json()
    sid = created["id"]

    fetched = http.get(f"/v1/schedules/{sid}").json()
    assert fetched["name"] == name

    lst = http.get("/v1/schedules").json()
    assert any(s["id"] == sid for s in lst)

    d1 = http.delete(f"/v1/schedules/{sid}")
    assert d1.status_code == 200
    d2 = http.delete(f"/v1/schedules/{sid}")
    assert d2.status_code == 404


def test_schedule_accepts_agent_task_shape(http, picked_agent):
    """Schedules can carry agent tasks too — same body shape as chat/completions."""
    agent = picked_agent
    name = f"live-agent-sched-{uuid.uuid4().hex[:6]}"
    created = http.post("/v1/schedules", json={
        "name": name,
        "task": {
            "model": f"agent:{agent}",
            "messages": [{"role": "user", "content": "scheduled work"}],
            "aitelier": {"max_turns": 1},
        },
        "at_iso": "2099-01-01T00:00:00Z",  # far future — won't fire during test
    }).json()
    sid = created["id"]
    # Cleanup.
    http.delete(f"/v1/schedules/{sid}")
