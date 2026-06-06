"""Tests for the storage layer.

Run against InMemoryStore by default — no infra required. The Postgres
implementation shares the same interface; an opt-in integration test
(below, gated on DATABASE_URL) verifies it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from aitelier.storage import (
    InMemoryStore,
    PostgresStore,
    RunEvent,
    RunFilter,
    RunSpec,
    Schedule,
)


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.mark.asyncio
async def test_create_and_get_run(store):
    spec = RunSpec(
        run_id="run-1", kind="agent", agent_id="claude",
        model="claude", trace_tag="curator",
        sandbox_backend="local", sandbox_url="http://localhost:2468",
        sandbox_server_id="srv-abc",
        environment={"mcp_servers": [{"name": "deepread"}]},
    )
    created = await store.create_run(spec)
    assert created.state == "pending"
    assert created.sandbox_server_id == "srv-abc"

    fetched = await store.get_run("run-1")
    assert fetched is not None
    assert fetched.environment == {"mcp_servers": [{"name": "deepread"}]}


@pytest.mark.asyncio
async def test_run_state_transitions(store):
    await store.create_run(RunSpec(run_id="r", kind="agent"))
    await store.update_run_state("r", "running")
    run = await store.get_run("r")
    assert run.state == "running"
    await store.update_run_state("r", "completed")
    run = await store.get_run("r")
    assert run.state == "completed"
    assert run.ended_at is not None


@pytest.mark.asyncio
async def test_illegal_state_transition_raises(store):
    await store.create_run(RunSpec(run_id="r", kind="agent"))
    await store.update_run_state("r", "running")
    await store.update_run_state("r", "completed")
    with pytest.raises(ValueError):
        await store.update_run_state("r", "running")  # completed → running disallowed


@pytest.mark.asyncio
async def test_finalize_run_sets_result_and_state(store):
    await store.create_run(RunSpec(run_id="r", kind="agent"))
    await store.update_run_state("r", "running")
    await store.finalize_run("r", {
        "status": "ok", "finish_reason": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        "cost_usd": 0.05,
        "tool_calls": [{"tool": "Read"}, {"tool": "Write"}],
    })
    run = await store.get_run("r")
    assert run.state == "completed"
    assert run.total_tokens == 30
    assert run.cost_usd == 0.05
    assert run.tool_call_count == 2


@pytest.mark.asyncio
async def test_list_runs_filters(store):
    for i in range(3):
        await store.create_run(RunSpec(run_id=f"r-{i}", kind="agent", trace_tag="A"))
    await store.create_run(RunSpec(run_id="r-x", kind="complete", trace_tag="B"))

    a_only = await store.list_runs(RunFilter(trace_tag="A"))
    assert len(a_only) == 3

    complete_only = await store.list_runs(RunFilter(kind="complete"))
    assert len(complete_only) == 1
    assert complete_only[0].run_id == "r-x"


@pytest.mark.asyncio
async def test_events_append_and_list(store):
    await store.create_run(RunSpec(run_id="r", kind="agent"))
    await store.append_event(RunEvent(run_id="r", seq=1, kind="start",
                                       payload={"hello": "world"}))
    await store.append_event(RunEvent(run_id="r", seq=2, kind="delta",
                                       payload={"content": "hi"}))
    events = await store.list_events("r")
    assert len(events) == 2
    assert events[0].kind == "start"
    assert events[1].payload == {"content": "hi"}


@pytest.mark.asyncio
async def test_events_since_seq(store):
    await store.create_run(RunSpec(run_id="r", kind="agent"))
    for i in range(1, 6):
        await store.append_event(RunEvent(run_id="r", seq=i, kind="delta",
                                           payload={"i": i}))
    tail = await store.list_events("r", since_seq=3)
    assert [e.seq for e in tail] == [4, 5]


@pytest.mark.asyncio
async def test_schedule_crud(store):
    s = Schedule(
        id="s1", name="daily-audit",
        task={"name": "audit", "kind": "agent"},
        interval_seconds=86400, at_iso=None, webhook_url=None,
        next_run_at=datetime.now(UTC), last_run_at=None,
        created_at=datetime.now(UTC),
    )
    await store.create_schedule(s)
    fetched = await store.get_schedule("s1")
    assert fetched.name == "daily-audit"
    all_ = await store.list_schedules()
    assert len(all_) == 1
    assert await store.delete_schedule("s1") is True
    assert await store.delete_schedule("s1") is False


@pytest.mark.asyncio
async def test_webhook_enqueue_claim_record(store):
    wid = await store.enqueue_webhook("https://x/", {"foo": 1}, run_id="r")
    due = await store.claim_pending_webhooks(limit=10)
    assert len(due) == 1
    assert due[0].id == wid
    assert due[0].attempts == 1

    # Mark delivered
    await store.record_webhook_attempt(wid, status_code=200, error=None,
                                          next_attempt_at=None)
    # Same delivery shouldn't be claimed again
    due_again = await store.claim_pending_webhooks(limit=10)
    assert len(due_again) == 0


# --- Optional Postgres integration test (gated on DATABASE_URL) -------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("AITELIER_TEST_DATABASE_URL"),
    reason="set AITELIER_TEST_DATABASE_URL to run Postgres integration tests",
)
async def test_postgres_round_trip():
    """Smoke test against a real Postgres. Verifies the migration + a CRUD cycle."""
    store = PostgresStore(os.environ["AITELIER_TEST_DATABASE_URL"])
    await store.connect()
    try:
        await store.create_run(RunSpec(run_id="pg-test-1", kind="agent",
                                         agent_id="claude"))
        await store.update_run_state("pg-test-1", "running")
        await store.finalize_run("pg-test-1", {
            "status": "ok", "finish_reason": "completed",
            "usage": {"total_tokens": 1},
        })
        run = await store.get_run("pg-test-1")
        assert run.state == "completed"
    finally:
        # Cleanup so reruns work
        async with store._pool.acquire() as conn:
            await conn.execute("DELETE FROM runs WHERE run_id = $1", "pg-test-1")
        await store.close()
