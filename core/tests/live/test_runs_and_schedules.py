"""Live tests for /v1/runs*, /v1/schedules, /v1/traces/aggregates.

Strict mode: missing curated models or upstream failures fail the test
rather than skipping. See ./conftest.py.

LLM-mode tests target `local` (Ollama via LiteLLM) so no external
provider key is required.
"""

from __future__ import annotations

import uuid

from .conftest import assert_upstream_ok


def _assert_local(litellm_models: list[str]) -> None:
    assert "local" in litellm_models, (
        f"`local` (Ollama) must be advertised by /v1/discovery for this test. "
        f"Curated models: {sorted(m for m in litellm_models if '/' not in m)}"
    )


def test_runs_list_filterable_by_trace_tag(http, trace_tag, litellm_models):
    _assert_local(litellm_models)
    # Create a run with a trace tag via aitelier headers (since OpenAI shape
    # has no trace_tag field, use correlation_id and look up by run_id).
    r = http.post(
        "/v1/chat/completions",
        json={
            "model": "local",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 10, "temperature": 0,
        },
        headers={"X-Correlation-Id": trace_tag},
    )
    assert_upstream_ok(r)
    run_id = r.json()["aitelier_run_id"]

    runs = http.get("/v1/runs", params={"correlation_id": trace_tag}).json()
    assert any(x["run_id"] == run_id for x in runs)
    mine = next(x for x in runs if x["run_id"] == run_id)
    assert mine["state"] in ("completed", "failed"), mine
    assert mine["kind"] == "complete"


def test_traces_aggregates_groups_by_correlation(http, trace_tag, litellm_models):
    _assert_local(litellm_models)
    for _ in range(2):
        r = http.post(
            "/v1/chat/completions",
            json={
                "model": "local",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5, "temperature": 0,
            },
            headers={"X-Correlation-Id": trace_tag},
        )
        assert_upstream_ok(r)

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
            "model": "local",
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


def test_schedule_accepts_agent_task_shape(http, agent_backend):
    """Schedules can carry agent tasks too — same body shape as chat/completions.
    Parameterized over backends since the schedule task body validation
    depends on the backend being recognized."""
    name = f"live-agent-sched-{uuid.uuid4().hex[:6]}"
    created = http.post("/v1/schedules", json={
        "name": name,
        "task": {
            "model": f"agent:{agent_backend}",
            "messages": [{"role": "user", "content": "scheduled work"}],
            "aitelier": {"max_turns": 1},
        },
        "at_iso": "2099-01-01T00:00:00Z",  # far future — won't fire during test
    }).json()
    sid = created["id"]
    # Cleanup.
    http.delete(f"/v1/schedules/{sid}")
