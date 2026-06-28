"""Cross-store contract tests.

The Store protocol has two implementations that independently reimplement the
same business logic (run rollups, claiming, idempotency). The InMemoryStore is
what the suite trusts; the PostgresStore is what production runs. These tests
pin the shared contract: an offline rollup check that runs against InMemoryStore
every CI run, plus Postgres-gated parity/concurrency tests that assert the two
backends agree (so a SQL-vs-Python divergence — e.g. day-bucket timezone
normalization, ON CONFLICT claiming — can't slip through).

Postgres-gated tests assert (not skip) when no test DSN is set — the project's
"no silent skips" policy. Set AITELIER_TEST_DATABASE_URL to run them.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from aitelier.storage import (
    IdempotencyRecord,
    InMemoryStore,
    PostgresStore,
    RunFilter,
    RunScore,
    RunSpec,
)


def _require_database_url() -> str:
    url = os.environ.get("AITELIER_TEST_DATABASE_URL")
    assert url, (
        "AITELIER_TEST_DATABASE_URL must be set for Postgres integration tests. "
        "Try: export AITELIER_TEST_DATABASE_URL="
        "postgresql://aitelier:aitelier_local@localhost:5433/aitelier"
    )
    return url


_SEED = [
    ("ct-a", "tagX", 100, 0.01),
    ("ct-b", "tagX", 50, 0.005),
    ("ct-c", "tagY", 10, 0.001),
]
_SEED_IDS = [rid for rid, *_ in _SEED]


async def _seed_contract_runs(store) -> None:
    """Seed identical completed runs (create → running → finalize) so both
    stores compute rollups over the same data."""
    for rid, tag, tokens, cost in _SEED:
        await store.create_run(RunSpec(run_id=rid, kind="agent",
                                       agent_id="claude", trace_tag=tag))
        await store.update_run_state(rid, "running")
        await store.finalize_run(rid, {
            "status": "ok", "finish_reason": "completed",
            "usage": {"total_tokens": tokens}, "cost_usd": cost,
        })


def _norm_agg(agg: dict) -> dict:
    """Comparable shape: per-key int counters (skip float cost) + total count."""
    return {
        "groups": {
            g["key"]: {k: g[k] for k in ("count", "total_tokens", "error_count")}
            for g in agg["groups"]
        },
        "total_count": agg["total"]["count"],
        "total_tokens": agg["total"]["total_tokens"],
    }


@pytest.mark.asyncio
async def test_aggregate_runs_rollup_inmemory():
    """Offline: the InMemory rollup groups + sums correctly (also verifies the
    seeding helper the gated parity test reuses)."""
    store = InMemoryStore()
    await _seed_contract_runs(store)
    agg = await store.aggregate_runs(group_by="trace_tag")
    norm = _norm_agg(agg)
    assert norm["groups"]["tagX"] == {"count": 2, "total_tokens": 150, "error_count": 0}
    assert norm["groups"]["tagY"] == {"count": 1, "total_tokens": 10, "error_count": 0}
    assert norm["total_count"] == 3
    assert norm["total_tokens"] == 160
    listed = sorted(r.run_id for r in await store.list_runs(RunFilter(trace_tag="tagX")))
    assert listed == ["ct-a", "ct-b"]


@pytest.mark.asyncio
async def test_aggregate_and_list_parity_across_stores():
    """Postgres-gated: InMemoryStore (Python rollup) and PostgresStore (SQL
    rollup) produce identical aggregate_runs + list_runs output on identical
    data — the two impls can silently diverge otherwise."""
    mem = InMemoryStore()
    await _seed_contract_runs(mem)

    pg = PostgresStore(_require_database_url())
    await pg.connect()
    try:
        await _seed_contract_runs(pg)
        assert _norm_agg(await mem.aggregate_runs(group_by="trace_tag")) == \
            _norm_agg(await pg.aggregate_runs(group_by="trace_tag"))
        mem_x = sorted(r.run_id for r in await mem.list_runs(RunFilter(trace_tag="tagX")))
        pg_x = sorted(r.run_id for r in await pg.list_runs(RunFilter(trace_tag="tagX")))
        assert mem_x == pg_x == ["ct-a", "ct-b"]
    finally:
        async with pg._pool.acquire() as conn:
            await conn.execute("DELETE FROM runs WHERE run_id = ANY($1)", _SEED_IDS)
        await pg.close()


@pytest.mark.asyncio
async def test_postgres_record_idempotent_on_conflict_keeps_first_row():
    """Postgres-gated: a second record_idempotent with the same key is a no-op
    (ON CONFLICT DO NOTHING) — the cross-process safety net behind the lock."""
    store = PostgresStore(_require_database_url())
    await store.connect()
    try:
        def _rec(run_id: str) -> IdempotencyRecord:
            return IdempotencyRecord(
                key="ct-idem-1", body_hash="h", endpoint="/v1/chat/completions",
                status_code=200, response={"run_id": run_id}, run_id=run_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        await store.record_idempotent(_rec("ct-r1"))
        await store.record_idempotent(_rec("ct-r2"))  # same key, must not overwrite
        got = await store.get_idempotent("ct-idem-1")
        assert got is not None and got.response["run_id"] == "ct-r1"
    finally:
        async with store._pool.acquire() as conn:
            await conn.execute("DELETE FROM idempotency_keys WHERE key = $1", "ct-idem-1")
        await store.close()


@pytest.mark.asyncio
async def test_postgres_add_run_score_missing_run_raises_keyerror():
    """Postgres-gated parity: a score against a non-existent run raises KeyError
    (FK violation translated), matching InMemoryStore — not a raw asyncpg error."""
    store = PostgresStore(_require_database_url())
    await store.connect()
    try:
        with pytest.raises(KeyError):
            await store.add_run_score(RunScore(
                run_id="ct-nonexistent", name="x", value=1.0, evaluator="e"))
    finally:
        await store.close()
