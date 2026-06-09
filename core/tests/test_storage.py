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


def test_runspec_rejects_oversized_metadata():
    """64KB cap is enforced at construction so no caller can sneak a giant
    blob into Postgres."""
    huge = {"x": "a" * (70 * 1024)}
    with pytest.raises(ValueError, match="too large"):
        RunSpec(run_id="r", kind="agent", metadata=huge)


def test_build_store_reads_dsn_from_config(monkeypatch):
    """_build_store consults get_config().database.url — not os.environ.
    Setting DATABASE_URL in env without a config value yields InMemoryStore."""
    from aitelier import config as cfg_mod
    from aitelier.storage._store import _build_store

    monkeypatch.setenv("DATABASE_URL", "postgresql://from-env-ignored/db")

    cfg_mod.reset_config()
    cfg_mod.set_config(cfg_mod.Config())  # database.url stays at default None
    store = _build_store()
    assert isinstance(store, InMemoryStore), (
        "env should be ignored; with no [database] url in config, "
        "we expect the InMemoryStore fallback"
    )

    # Now point config.database.url at a (bogus) DSN and confirm PostgresStore
    # is chosen. We don't connect — just verify the dispatch.
    cfg_mod.set_config(cfg_mod.Config(
        database=cfg_mod.DatabaseConfig(url="postgresql://from-config/db"),
    ))
    store = _build_store()
    assert isinstance(store, PostgresStore)
    assert store.dsn == "postgresql://from-config/db"
    cfg_mod.reset_config()


def test_runspec_accepts_typical_metadata():
    # Typical: correlation_id + a couple of tags. Well under the cap.
    RunSpec(run_id="r", kind="agent",
            metadata={"correlation_id": "abc", "trace_tag": "ingest", "n": 1})


@pytest.mark.asyncio
async def test_create_and_get_run(store):
    spec = RunSpec(
        run_id="run-1", kind="agent", agent_id="claude",
        model="claude", trace_tag="curator",
        sandbox_backend="local", sandbox_url="http://localhost:2468",
        sandbox_server_id="srv-abc",
        environment={"mcp_servers": [{"name": "example-mcp"}]},
    )
    created = await store.create_run(spec)
    assert created.state == "pending"
    assert created.sandbox_server_id == "srv-abc"

    fetched = await store.get_run("run-1")
    assert fetched is not None
    assert fetched.environment == {"mcp_servers": [{"name": "example-mcp"}]}


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
async def test_record_run_stamps_status_ok_on_success(_fresh_store):
    """Successful runs land in the store with status='ok' even when the
    awaitable's result dict doesn't mention status — so /v1/traces can
    report success vs error without consumers cross-referencing `state`."""
    from aitelier.runs import record_run

    async def work():
        return {"finish_reason": "stop", "usage": {"total_tokens": 12}}

    await record_run(RunSpec(run_id="r-ok", kind="complete"), work())
    run = await _fresh_store.get_run("r-ok")
    assert run.state == "completed"
    assert run.status == "ok"


@pytest.mark.asyncio
async def test_record_run_preserves_explicit_status(_fresh_store):
    """A caller-supplied status survives — record_run only fills None."""
    from aitelier.runs import record_run

    async def work():
        return {"status": "error", "error_type": "Boom", "error_msg": "x"}

    await record_run(RunSpec(run_id="r-bad", kind="complete"), work())
    run = await _fresh_store.get_run("r-bad")
    assert run.state == "failed"
    assert run.status == "error"


@pytest.mark.asyncio
async def test_record_run_marks_cancellation_distinct_from_error(_fresh_store):
    """Cancellation is user-initiated; failure is server-side. They share
    a terminal lifecycle but the outcome category is different — consumers
    filtering for `status="error"` should not see cancelled runs."""
    import asyncio as _asyncio

    from aitelier.runs import record_run

    async def work():
        raise _asyncio.CancelledError()

    with pytest.raises(_asyncio.CancelledError):
        await record_run(RunSpec(run_id="r-cancel", kind="complete"), work())
    run = await _fresh_store.get_run("r-cancel")
    assert run.state == "cancelled"
    assert run.status == "cancelled"
    assert run.error_type == "Cancelled"


@pytest.mark.asyncio
async def test_record_run_marks_unexpected_exception_as_error(_fresh_store):
    from aitelier.runs import record_run

    async def work():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await record_run(RunSpec(run_id="r-exc", kind="complete"), work())
    run = await _fresh_store.get_run("r-exc")
    assert run.state == "failed"
    assert run.status == "error"
    assert run.error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_parent_run_id_round_trip(store):
    """The passthrough field survives create → get → list-filter without
    aitelier imposing any semantics (no FK, no cycle check)."""
    await store.create_run(RunSpec(run_id="p", kind="agent"))
    await store.create_run(RunSpec(run_id="c1", kind="agent", parent_run_id="p"))
    await store.create_run(RunSpec(run_id="c2", kind="agent", parent_run_id="p"))
    await store.create_run(RunSpec(run_id="other", kind="agent"))

    p = await store.get_run("p")
    c1 = await store.get_run("c1")
    assert p.parent_run_id is None
    assert c1.parent_run_id == "p"

    children = await store.list_runs(RunFilter(parent_run_id="p"))
    ids = sorted(r.run_id for r in children)
    assert ids == ["c1", "c2"]

    # No FK constraint: child can point at a parent that doesn't exist.
    await store.create_run(RunSpec(
        run_id="orphan", kind="agent", parent_run_id="ghost",
    ))
    assert (await store.get_run("orphan")).parent_run_id == "ghost"


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
async def test_purge_old_run_events_drops_only_old_ones(store):
    """`purge_old_run_events` removes rows older than the cutoff and
    leaves recent ones alone."""
    from datetime import timedelta

    await store.create_run(RunSpec(run_id="r", kind="agent"))
    now = datetime.now(UTC)
    old = RunEvent(run_id="r", seq=1, kind="start", payload={},
                    ts=now - timedelta(days=60))
    recent = RunEvent(run_id="r", seq=2, kind="finish", payload={},
                       ts=now - timedelta(days=1))
    await store.append_event(old)
    await store.append_event(recent)

    removed = await store.purge_old_run_events(max_age_days=30)
    assert removed == 1
    remaining = await store.list_events("r")
    assert [e.kind for e in remaining] == ["finish"]


@pytest.mark.asyncio
async def test_purge_old_webhook_deliveries_keeps_pending(store):
    """Pending webhooks must not be purged regardless of age — they
    haven't reached a terminal state."""
    from datetime import timedelta

    # Inject one delivered, one failed, one pending — all aged past cutoff.
    wid_d = await store.enqueue_webhook("https://h/", {"v": 1})
    wid_f = await store.enqueue_webhook("https://h/", {"v": 2})
    wid_p = await store.enqueue_webhook("https://h/", {"v": 3})
    store._webhooks[wid_d].state = "delivered"
    store._webhooks[wid_f].state = "failed"
    store._webhooks[wid_p].state = "pending"
    for wid in (wid_d, wid_f, wid_p):
        store._webhooks[wid].created_at = datetime.now(UTC) - timedelta(days=30)

    removed = await store.purge_old_webhook_deliveries(max_age_days=7)
    assert removed == 2
    assert wid_p in store._webhooks
    assert wid_d not in store._webhooks
    assert wid_f not in store._webhooks


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
async def test_update_run_sandbox_stamps_fields(store):
    await store.create_run(RunSpec(run_id="r", kind="agent"))
    await store.update_run_sandbox(
        "r", sandbox_url="http://sa.example.com",
        sandbox_server_id="srv-123", sandbox_backend="remote",
    )
    run = await store.get_run("r")
    assert run.sandbox_url == "http://sa.example.com"
    assert run.sandbox_server_id == "srv-123"
    assert run.sandbox_backend == "remote"


@pytest.mark.asyncio
async def test_mark_orphaned_running_runs_sweeps_pending_and_running(store):
    await store.create_run(RunSpec(run_id="a", kind="agent"))               # pending
    await store.create_run(RunSpec(run_id="b", kind="agent"))
    await store.update_run_state("b", "running")                            # running
    await store.create_run(RunSpec(run_id="c", kind="agent"))
    await store.update_run_state("c", "running")
    await store.update_run_state("c", "completed")                          # terminal

    swept = await store.mark_orphaned_running_runs()
    assert swept == 2
    assert (await store.get_run("a")).state == "orphaned"
    assert (await store.get_run("b")).state == "orphaned"
    assert (await store.get_run("c")).state == "completed"   # untouched
    # Orphaned rows get an ended_at stamp so dashboards can render duration.
    assert (await store.get_run("a")).ended_at is not None


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


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("AITELIER_TEST_DATABASE_URL"),
    reason="set AITELIER_TEST_DATABASE_URL to run Postgres integration tests",
)
async def test_postgres_update_run_sandbox_and_orphan_sweep():
    """Verify the Postgres-specific COALESCE semantics on update_run_sandbox
    and the startup orphan sweep — these don't exercise on InMemoryStore."""
    store = PostgresStore(os.environ["AITELIER_TEST_DATABASE_URL"])
    await store.connect()
    try:
        # Partial updates: only url first, then only server_id; both should
        # be present afterward thanks to COALESCE.
        await store.create_run(RunSpec(run_id="pg-sandbox-1", kind="agent"))
        await store.update_run_sandbox(
            "pg-sandbox-1", sandbox_url="https://sa.example.com",
        )
        await store.update_run_sandbox(
            "pg-sandbox-1", sandbox_server_id="srv-xyz",
            sandbox_backend="remote",
        )
        run = await store.get_run("pg-sandbox-1")
        assert run.sandbox_url == "https://sa.example.com"
        assert run.sandbox_server_id == "srv-xyz"
        assert run.sandbox_backend == "remote"

        # Orphan sweep: a row in `running` from a notional prior process
        # flips to `orphaned`.
        await store.create_run(RunSpec(run_id="pg-orphan-1", kind="agent"))
        await store.update_run_state("pg-orphan-1", "running")
        swept = await store.mark_orphaned_running_runs()
        assert swept >= 1
        run = await store.get_run("pg-orphan-1")
        assert run.state == "orphaned"
        assert run.ended_at is not None
    finally:
        async with store._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM runs WHERE run_id IN ($1, $2)",
                "pg-sandbox-1", "pg-orphan-1",
            )
        await store.close()
