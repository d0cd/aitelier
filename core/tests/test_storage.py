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
    RunScore,
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
async def test_request_body_and_rendered_messages_round_trip(store):
    """v4 migration columns survive a write → read cycle. Both NULL when
    unset (backward-compat with older code paths) and verbatim when set."""
    # Default: both fields absent → None at the row level.
    await store.create_run(RunSpec(run_id="r-no-body", kind="agent"))
    no_body = await store.get_run("r-no-body")
    assert no_body.request_body is None
    assert no_body.rendered_messages is None

    # Explicit: both fields populated → verbatim round-trip.
    rb = {
        "model": "agent:claude",
        "messages": [{"role": "user", "content": "hi"}],
        "aitelier": {"max_turns": 1},
    }
    rm = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    await store.create_run(RunSpec(
        run_id="r-with-body", kind="agent",
        request_body=rb, rendered_messages=rm,
    ))
    with_body = await store.get_run("r-with-body")
    assert with_body.request_body == rb
    assert with_body.rendered_messages == rm


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
async def test_run_score_round_trip_and_history(store):
    """Scoring sink: write, read back, allow multiple rows per
    (run_id, name, evaluator) — re-grading is a write, not an update."""
    await store.create_run(RunSpec(run_id="scored", kind="agent"))
    s1 = await store.add_run_score(RunScore(
        run_id="scored", name="helpfulness", value=0.8,
        evaluator="gpt-4o-judge", comment="clear",
    ))
    s2 = await store.add_run_score(RunScore(
        run_id="scored", name="helpfulness", value=0.9,
        evaluator="gpt-4o-judge", comment="re-grade after rubric update",
        metadata={"rubric_version": 2},
    ))
    assert s1.id is not None and s2.id is not None and s1.id != s2.id
    rows = await store.list_run_scores("scored")
    assert [r.value for r in rows] == [0.8, 0.9]
    assert rows[1].metadata == {"rubric_version": 2}


@pytest.mark.asyncio
async def test_run_score_rejects_unknown_run(store):
    """Foreign-key analog: writing a score against a non-existent run
    raises so consumers see the error immediately instead of
    accumulating orphan rows."""
    with pytest.raises(KeyError):
        await store.add_run_score(RunScore(
            run_id="does-not-exist", name="x", value=1.0, evaluator="e",
        ))


@pytest.mark.asyncio
async def test_run_score_empty_when_run_unscored(store):
    """A run with no scores returns an empty list, not None."""
    await store.create_run(RunSpec(run_id="unscored", kind="agent"))
    assert await store.list_run_scores("unscored") == []


@pytest.mark.asyncio
async def test_get_idempotent_expired_is_nonmutating(store):
    """An expired idempotency key reads as absent but is NOT removed on read,
    matching PostgresStore (which filters expired rows in its WHERE clause and
    leaves them for the purge worker). A read that mutated would let a
    re-record succeed in memory while Postgres still blocked it."""
    from datetime import timedelta

    from aitelier.storage.models import IdempotencyRecord

    past = datetime.now(UTC) - timedelta(hours=1)
    await store.record_idempotent(IdempotencyRecord(
        key="expired-k", body_hash="h", endpoint="/v1/runs",
        status_code=200, response={"ok": True}, expires_at=past,
    ))
    assert await store.get_idempotent("expired-k") is None   # expired → absent
    assert "expired-k" in store._idempotency                 # but not popped


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
    assert set(swept) == {"a", "b"}                          # returns the flipped run_ids
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
    # Claim does NOT count an attempt (so a crash before delivery doesn't burn
    # one); record_webhook_attempt counts it.
    assert due[0].attempts == 0

    # Mark delivered → counts the attempt.
    await store.record_webhook_attempt(wid, status_code=200, error=None,
                                          next_attempt_at=None)
    # Same delivery shouldn't be claimed again
    due_again = await store.claim_pending_webhooks(limit=10)
    assert len(due_again) == 0


# --- Postgres integration tests (strict — require DATABASE_URL) -------------
#
# These hit a real Postgres. In strict mode the env var must be set; an
# unset var fails the test rather than skipping. Set:
#   export AITELIER_TEST_DATABASE_URL=postgresql://aitelier:aitelier_local@localhost:5433/aitelier
# (matches the dev Postgres that `make start` boots.) To exclude these
# without running them, deselect via `-k 'not postgres'`.


def _require_database_url() -> str:
    url = os.environ.get("AITELIER_TEST_DATABASE_URL")
    assert url, (
        "AITELIER_TEST_DATABASE_URL must be set for Postgres integration "
        "tests. Try: "
        "export AITELIER_TEST_DATABASE_URL="
        "postgresql://aitelier:aitelier_local@localhost:5433/aitelier"
    )
    return url


@pytest.mark.asyncio
async def test_postgres_round_trip():
    """Smoke test against a real Postgres. Verifies the migration + a CRUD cycle."""
    store = PostgresStore(_require_database_url())
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
async def test_postgres_run_scores_round_trip():
    """v5 migration: write + list scores against a real Postgres.
    Verifies the table, indices, and JSONB metadata column."""
    store = PostgresStore(_require_database_url())
    await store.connect()
    try:
        await store.create_run(RunSpec(run_id="pg-score-1", kind="agent"))
        s1 = await store.add_run_score(RunScore(
            run_id="pg-score-1", name="helpfulness", value=0.75,
            evaluator="gpt-4o-judge",
        ))
        await store.add_run_score(RunScore(
            run_id="pg-score-1", name="helpfulness", value=0.85,
            evaluator="gpt-4o-judge", comment="re-grade",
            metadata={"rubric_version": 2},
        ))
        rows = await store.list_run_scores("pg-score-1")
        assert [r.value for r in rows] == [0.75, 0.85]
        assert rows[1].metadata == {"rubric_version": 2}
        assert s1.id is not None and rows[0].id == s1.id
    finally:
        async with store._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM runs WHERE run_id = $1", "pg-score-1",
            )
        await store.close()


@pytest.mark.asyncio
async def test_postgres_update_run_sandbox_and_orphan_sweep():
    """Verify the Postgres-specific COALESCE semantics on update_run_sandbox
    and the startup orphan sweep — these don't exercise on InMemoryStore."""
    store = PostgresStore(_require_database_url())
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
        assert "pg-orphan-1" in swept
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


@pytest.mark.asyncio
async def test_runs_awaiting_webhook(store):
    """Terminal runs with a webhook_url but no delivery row are surfaced for
    startup reconciliation; runs with a delivery, no url, or still running are not."""
    # owed: terminal + webhook_url + no delivery
    await store.create_run(RunSpec(run_id="r-owed", kind="agent",
                                   metadata={"webhook_url": "https://h/1"}))
    await store.update_run_state("r-owed", "running")
    await store.finalize_run("r-owed", {"status": "ok", "content": "x"}, state="completed")
    # already delivered: terminal + webhook_url + a delivery row exists
    await store.create_run(RunSpec(run_id="r-done", kind="agent",
                                   metadata={"webhook_url": "https://h/2"}))
    await store.update_run_state("r-done", "running")
    await store.finalize_run("r-done", {"status": "ok"}, state="completed")
    await store.enqueue_webhook("https://h/2", {"x": 1}, run_id="r-done")
    # no webhook requested
    await store.create_run(RunSpec(run_id="r-nohook", kind="agent"))
    await store.update_run_state("r-nohook", "running")
    await store.finalize_run("r-nohook", {"status": "ok"}, state="completed")
    # still running (not terminal)
    await store.create_run(RunSpec(run_id="r-running", kind="agent",
                                   metadata={"webhook_url": "https://h/3"}))

    owed = await store.runs_awaiting_webhook()
    assert {r.run_id for r in owed} == {"r-owed"}


@pytest.mark.asyncio
async def test_runs_awaiting_webhook_respects_since_window(store):
    """A long-completed run whose delivery row has purged must not be re-fired:
    the `since` window excludes runs that ended before it."""
    from datetime import UTC, datetime, timedelta
    await store.create_run(RunSpec(run_id="r-old", kind="agent",
                                   metadata={"webhook_url": "https://h"}))
    await store.update_run_state("r-old", "running")
    await store.finalize_run("r-old", {"status": "ok"}, state="completed")
    store._runs["r-old"].ended_at = datetime.now(UTC) - timedelta(days=10)
    since = datetime.now(UTC) - timedelta(days=7)
    assert await store.runs_awaiting_webhook(since=since) == []
    # Unbounded still surfaces it (base behavior).
    assert [r.run_id for r in await store.runs_awaiting_webhook()] == ["r-old"]


@pytest.mark.asyncio
async def test_runs_awaiting_webhook_includes_orphaned(store):
    """Orphaned runs with a webhook_url but no delivery are reconciled too
    (crash mid orphan-webhook-loop recovery)."""
    await store.create_run(RunSpec(run_id="r-orph", kind="agent",
                                   metadata={"webhook_url": "https://h"}))
    await store.update_run_state("r-orph", "running")
    await store.mark_orphaned_running_runs()  # → orphaned, no delivery enqueued
    owed = await store.runs_awaiting_webhook()
    assert "r-orph" in {r.run_id for r in owed}
