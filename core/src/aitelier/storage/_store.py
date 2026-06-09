"""Store interface + two implementations.

The Protocol defines the surface; PostgresStore is production; InMemoryStore
exists so tests don't need infra. Both must satisfy the same contract.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from aitelier.storage.models import (
    IdempotencyRecord,
    Run,
    RunEvent,
    RunFilter,
    RunSpec,
    RunState,
    Schedule,
    WebhookDelivery,
    can_transition,
    is_terminal,
)

logger = logging.getLogger("aitelier.storage")

# Valid `group_by` values for aggregate_runs. Both PostgresStore (which maps
# them to SQL expressions) and InMemoryStore (which maps them to attribute
# lookups) reference this single set so the two impls can't drift.
AGGREGATE_GROUP_KEYS = frozenset({
    "trace_tag", "kind", "model", "agent_id",
    "status", "error_type", "day",
})


class Store(Protocol):
    """Contract every store implementation must satisfy."""

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def migrate(self) -> None: ...

    # Runs
    async def create_run(self, spec: RunSpec) -> Run: ...
    async def get_run(self, run_id: str) -> Run | None: ...
    async def list_runs(self, flt: RunFilter) -> list[Run]: ...
    async def update_run_state(self, run_id: str, new_state: RunState,
                                 *, ended_at: datetime | None = None) -> None: ...
    async def update_run_sandbox(self, run_id: str, *,
                                   sandbox_url: str | None = None,
                                   sandbox_server_id: str | None = None,
                                   sandbox_backend: str | None = None) -> None: ...
    async def finalize_run(self, run_id: str, result: dict[str, Any],
                            *, state: RunState = "completed") -> None: ...
    async def mark_orphaned_running_runs(self) -> int: ...
    async def aggregate_runs(self, *, group_by: str = "trace_tag",
                              since: datetime | None = None,
                              until: datetime | None = None,
                              trace_tag: str | None = None) -> dict: ...
    async def purge_old_runs(self, max_age_days: int = 30) -> int: ...

    # Events
    async def append_event(self, event: RunEvent) -> RunEvent: ...
    async def list_events(self, run_id: str, *, since_seq: int = 0,
                            limit: int = 1000) -> list[RunEvent]: ...
    async def purge_old_run_events(self, max_age_days: int = 30) -> int: ...

    # Schedules
    async def create_schedule(self, schedule: Schedule) -> Schedule: ...
    async def get_schedule(self, schedule_id: str) -> Schedule | None: ...
    async def list_schedules(self) -> list[Schedule]: ...
    async def update_schedule_run_times(self, schedule_id: str, *,
                                          last_run_at: datetime,
                                          next_run_at: datetime | None) -> None: ...
    async def delete_schedule(self, schedule_id: str) -> bool: ...

    # Webhook deliveries
    async def enqueue_webhook(self, url: str, payload: dict[str, Any],
                                *, run_id: str | None = None,
                                schedule_id: str | None = None) -> int: ...
    async def claim_pending_webhooks(self, limit: int = 10) -> list[WebhookDelivery]: ...
    async def record_webhook_attempt(self, delivery_id: int, *,
                                       status_code: int | None,
                                       error: str | None,
                                       next_attempt_at: datetime | None) -> None: ...
    async def purge_old_webhook_deliveries(self, max_age_days: int = 7) -> int: ...
    async def count_pending_webhooks(self) -> int: ...

    # Idempotency keys
    async def get_idempotent(self, key: str) -> IdempotencyRecord | None: ...
    async def record_idempotent(self, rec: IdempotencyRecord) -> None: ...
    async def purge_expired_idempotency_keys(self) -> int: ...


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


class PostgresStore:
    """asyncpg-backed implementation. Pool is lazy; migrations run on connect()."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool: Any = None  # asyncpg.Pool — not imported at module top

    async def connect(self) -> None:
        if self._pool is not None:
            return
        import asyncpg
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)
        await self.migrate()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def migrate(self) -> None:
        """Apply pending migrations in order. Idempotent."""
        migrations_dir = Path(__file__).parent / "migrations"
        files = sorted(migrations_dir.glob("*.sql"))
        async with self._pool.acquire() as conn:
            # Ensure schema_version table exists before reading from it
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            row = await conn.fetchrow("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
            current = row["v"] if row else 0
            for f in files:
                # Filename convention: 001_initial.sql → version 1
                try:
                    version = int(f.name.split("_", 1)[0])
                except ValueError:
                    continue
                if version <= current:
                    continue
                logger.info("Applying migration %s", f.name)
                async with conn.transaction():
                    await conn.execute(f.read_text())

    # --- Runs ---

    async def create_run(self, spec: RunSpec) -> Run:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO runs (
                    run_id, state, kind, agent_id, model,
                    trace_tag, correlation_id, parent_run_id,
                    sandbox_backend, sandbox_url, sandbox_server_id, workspace,
                    environment_json, system_prompt_hash, metadata_json
                )
                VALUES ($1, 'pending', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12::jsonb, $13, $14::jsonb)
                RETURNING *
                """,
                spec.run_id, spec.kind, spec.agent_id, spec.model,
                spec.trace_tag, spec.correlation_id, spec.parent_run_id,
                spec.sandbox_backend, spec.sandbox_url, spec.sandbox_server_id, spec.workspace,
                json.dumps(spec.environment), spec.system_prompt_hash,
                json.dumps(spec.metadata),
            )
        return _row_to_run(row)

    async def get_run(self, run_id: str) -> Run | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM runs WHERE run_id = $1", run_id)
        return _row_to_run(row) if row else None

    async def list_runs(self, flt: RunFilter) -> list[Run]:
        where: list[str] = []
        params: list[Any] = []

        def add(value: Any, predicate: str) -> None:
            params.append(value)
            where.append(predicate.format(idx=len(params)))

        if flt.state:
            add(flt.state, "state = ${idx}")
        if flt.kind:
            add(flt.kind, "kind = ${idx}")
        if flt.agent_id:
            add(flt.agent_id, "agent_id = ${idx}")
        if flt.trace_tag:
            add(flt.trace_tag, "trace_tag = ${idx}")
        if flt.correlation_id:
            add(flt.correlation_id, "correlation_id = ${idx}")
        if flt.parent_run_id:
            add(flt.parent_run_id, "parent_run_id = ${idx}")
        if flt.since:
            add(flt.since, "started_at >= ${idx}")
        if flt.until:
            add(flt.until, "started_at <= ${idx}")

        where_clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(flt.limit)
        sql = (
            f"SELECT * FROM runs{where_clause} "
            f"ORDER BY started_at DESC LIMIT ${len(params)}"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_run(r) for r in rows]

    async def update_run_state(self, run_id: str, new_state: RunState,
                                 *, ended_at: datetime | None = None) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT state FROM runs WHERE run_id = $1 FOR UPDATE",
                    run_id,
                )
                if row is None:
                    raise KeyError(f"Run not found: {run_id}")
                current = row["state"]
                if not can_transition(current, new_state):
                    raise ValueError(f"Illegal state transition: {current} → {new_state}")
                ended = ended_at or (datetime.now(UTC) if is_terminal(new_state) else None)
                await conn.execute(
                    "UPDATE runs SET state = $1, ended_at = $2 WHERE run_id = $3",
                    new_state, ended, run_id,
                )

    async def update_run_sandbox(self, run_id: str, *,
                                   sandbox_url: str | None = None,
                                   sandbox_server_id: str | None = None,
                                   sandbox_backend: str | None = None) -> None:
        """Stamp the run row with the live SA endpoint + server_id (+ backend
        classification) so a restart-time recovery step can find the session
        it was bound to and dashboards can distinguish local vs remote runs.
        Only fields passed as non-None are written — COALESCE preserves any
        column already set."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE runs SET
                  sandbox_url       = COALESCE($1, sandbox_url),
                  sandbox_server_id = COALESCE($2, sandbox_server_id),
                  sandbox_backend   = COALESCE($3, sandbox_backend)
                WHERE run_id = $4
                """,
                sandbox_url, sandbox_server_id, sandbox_backend, run_id,
            )

    async def mark_orphaned_running_runs(self) -> int:
        """Sweep on startup: any run still in `running` from a previous
        aitelier process is unrecoverable (SA has no session-resume API yet).
        Flip to `orphaned` so dashboards stop treating it as live."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE runs SET state = 'orphaned', ended_at = COALESCE(ended_at, now()) "
                "WHERE state IN ('pending', 'running')"
            )
        try:
            return int(result.rsplit(" ", 1)[-1])
        except ValueError:
            return 0

    async def finalize_run(self, run_id: str, result: dict[str, Any],
                            *, state: RunState = "completed") -> None:
        """Single transaction: update result columns + transition to terminal state."""
        usage = result.get("usage") or {}
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT state FROM runs WHERE run_id = $1 FOR UPDATE",
                    run_id,
                )
                if row is None:
                    raise KeyError(f"Run not found: {run_id}")
                if not can_transition(row["state"], state):
                    raise ValueError(f"Illegal state transition: {row['state']} → {state}")
                await conn.execute(
                    """
                    UPDATE runs SET
                        state = $1, ended_at = now(),
                        result_json = $2::jsonb,
                        input_tokens = $3, output_tokens = $4, total_tokens = $5,
                        cost_usd = $6, finish_reason = $7, tool_call_count = $8,
                        status = $9, error_type = $10, error_msg = $11
                    WHERE run_id = $12
                    """,
                    state, json.dumps(result),
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("total_tokens", 0),
                    result.get("cost_usd"), result.get("finish_reason"),
                    len(result.get("tool_calls") or []),
                    result.get("status"), result.get("error_type"), result.get("error_msg"),
                    run_id,
                )

    # SQL projections for AGGREGATE_GROUP_KEYS. Postgres-only; the in-memory
    # store uses getattr on the same key set.
    _AGGREGATE_GROUP_EXPRS = {
        "trace_tag":  "COALESCE(trace_tag, '<none>')",
        "kind":       "COALESCE(kind, '<none>')",
        "model":      "COALESCE(model, '<none>')",
        "agent_id":   "COALESCE(agent_id, '<none>')",
        "status":     "COALESCE(status, '<none>')",
        "error_type": "COALESCE(error_type, '<none>')",
        "day":        "to_char(started_at, 'YYYY-MM-DD')",
    }

    async def aggregate_runs(self, *, group_by: str = "trace_tag",
                              since: datetime | None = None,
                              until: datetime | None = None,
                              trace_tag: str | None = None) -> dict:
        if group_by not in AGGREGATE_GROUP_KEYS:
            raise ValueError(
                f"group_by must be one of: "
                f"{', '.join(sorted(AGGREGATE_GROUP_KEYS))}"
            )
        expr = self._AGGREGATE_GROUP_EXPRS[group_by]
        where: list[str] = []
        params: list[Any] = []
        if since:
            params.append(since)
            where.append(f"started_at >= ${len(params)}")
        if until:
            params.append(until)
            where.append(f"started_at <= ${len(params)}")
        if trace_tag:
            params.append(trace_tag)
            where.append(f"trace_tag = ${len(params)}")
        where_clause = (" WHERE " + " AND ".join(where)) if where else ""

        group_sql = f"""
            SELECT
              {expr} AS key,
              COUNT(*) AS count,
              COALESCE(SUM(total_tokens), 0) AS total_tokens,
              COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
              SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count
            FROM runs{where_clause}
            GROUP BY key
            ORDER BY count DESC
        """
        total_sql = f"""
            SELECT
              COUNT(*) AS count,
              COALESCE(SUM(total_tokens), 0) AS total_tokens,
              COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
              SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count
            FROM runs{where_clause}
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(group_sql, *params)
            total = await conn.fetchrow(total_sql, *params)
        return {
            "group_by": group_by,
            "groups": [dict(r) for r in rows],
            "total": dict(total) if total else {
                "count": 0, "total_tokens": 0,
                "cost_usd": 0.0, "error_count": 0,
            },
        }

    async def purge_old_runs(self, max_age_days: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM runs WHERE started_at < $1", cutoff,
            )
        # asyncpg returns 'DELETE N'
        try:
            return int(result.rsplit(" ", 1)[-1])
        except ValueError:
            return 0

    # --- Events ---

    async def append_event(self, event: RunEvent) -> RunEvent:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO run_events (run_id, seq, kind, payload_json)
                VALUES ($1, $2, $3, $4::jsonb)
                RETURNING event_id, ts
                """,
                event.run_id, event.seq, event.kind, json.dumps(event.payload),
            )
        return RunEvent(
            run_id=event.run_id, seq=event.seq, kind=event.kind,
            payload=event.payload, ts=row["ts"], event_id=row["event_id"],
        )

    async def list_events(self, run_id: str, *, since_seq: int = 0,
                            limit: int = 1000) -> list[RunEvent]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_id, run_id, seq, kind, payload_json, ts
                FROM run_events
                WHERE run_id = $1 AND seq > $2
                ORDER BY seq ASC
                LIMIT $3
                """,
                run_id, since_seq, limit,
            )
        return [_row_to_event(r) for r in rows]

    # --- Schedules ---

    async def create_schedule(self, s: Schedule) -> Schedule:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO schedules (id, name, task_json, interval_seconds, at_iso,
                                        webhook_url, next_run_at, last_run_at, created_at)
                VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9)
                """,
                s.id, s.name, json.dumps(s.task), s.interval_seconds, s.at_iso,
                s.webhook_url, s.next_run_at, s.last_run_at, s.created_at,
            )
        return s

    async def get_schedule(self, schedule_id: str) -> Schedule | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM schedules WHERE id = $1", schedule_id)
        return _row_to_schedule(row) if row else None

    async def list_schedules(self) -> list[Schedule]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM schedules ORDER BY created_at DESC")
        return [_row_to_schedule(r) for r in rows]

    async def update_schedule_run_times(self, schedule_id: str, *,
                                          last_run_at: datetime,
                                          next_run_at: datetime | None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE schedules SET last_run_at = $1, next_run_at = $2 WHERE id = $3",
                last_run_at, next_run_at, schedule_id,
            )

    async def delete_schedule(self, schedule_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM schedules WHERE id = $1", schedule_id)
        return result.endswith(" 1")

    # --- Webhooks ---

    async def enqueue_webhook(self, url: str, payload: dict[str, Any],
                                *, run_id: str | None = None,
                                schedule_id: str | None = None) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO webhook_deliveries (run_id, schedule_id, url, payload_json,
                                                  state, next_attempt_at)
                VALUES ($1, $2, $3, $4::jsonb, 'pending', now())
                RETURNING id
                """,
                run_id, schedule_id, url, json.dumps(payload),
            )
        return row["id"]

    async def claim_pending_webhooks(self, limit: int = 10) -> list[WebhookDelivery]:
        """Atomically claim due deliveries by bumping their next_attempt_at far out
        so concurrent workers don't grab them. Caller must record_webhook_attempt
        after each attempt."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE webhook_deliveries
                SET next_attempt_at = now() + interval '5 minutes',
                    last_attempt_at = now(),
                    attempts = attempts + 1
                WHERE id IN (
                    SELECT id FROM webhook_deliveries
                    WHERE state = 'pending' AND next_attempt_at <= now()
                    ORDER BY next_attempt_at
                    LIMIT $1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                limit,
            )
        return [_row_to_webhook(r) for r in rows]

    async def record_webhook_attempt(self, delivery_id: int, *,
                                       status_code: int | None,
                                       error: str | None,
                                       next_attempt_at: datetime | None) -> None:
        new_state = "delivered" if (status_code and 200 <= status_code < 300) else (
            "pending" if next_attempt_at else "failed"
        )
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE webhook_deliveries
                SET last_status_code = $1, last_error = $2,
                    next_attempt_at = $3, state = $4
                WHERE id = $5
                """,
                status_code, error, next_attempt_at, new_state, delivery_id,
            )

    async def purge_old_webhook_deliveries(self, max_age_days: int = 7) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM webhook_deliveries "
                "WHERE state IN ('delivered', 'failed') AND created_at < $1",
                cutoff,
            )
        try:
            return int(result.rsplit(" ", 1)[-1])
        except ValueError:
            return 0

    async def count_pending_webhooks(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM webhook_deliveries "
                "WHERE state = 'pending'",
            )
        return int(row["n"]) if row else 0

    async def purge_old_run_events(self, max_age_days: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM run_events WHERE ts < $1", cutoff,
            )
        try:
            return int(result.rsplit(" ", 1)[-1])
        except ValueError:
            return 0

    # --- Idempotency keys ---

    async def get_idempotent(self, key: str) -> IdempotencyRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM idempotency_keys "
                "WHERE key = $1 AND expires_at > now()",
                key,
            )
        if row is None:
            return None
        return IdempotencyRecord(
            key=row["key"], body_hash=row["body_hash"],
            endpoint=row["endpoint"], status_code=row["status_code"],
            response=_as_dict(row["response_json"]),
            run_id=row["run_id"], created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    async def record_idempotent(self, rec: IdempotencyRecord) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO idempotency_keys
                  (key, body_hash, endpoint, status_code, response_json,
                   run_id, expires_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                ON CONFLICT (key) DO NOTHING
                """,
                rec.key, rec.body_hash, rec.endpoint, rec.status_code,
                json.dumps(rec.response), rec.run_id, rec.expires_at,
            )

    async def purge_expired_idempotency_keys(self) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM idempotency_keys WHERE expires_at <= now()",
            )
        try:
            return int(result.rsplit(" ", 1)[-1])
        except ValueError:
            return 0


# ---------------------------------------------------------------------------
# In-memory implementation — for tests + when [database] url is unset
# ---------------------------------------------------------------------------


class InMemoryStore:
    """Volatile, single-process. Used by tests and as the fallback when
    [database] url isn't set (e.g., dev without docker compose). NO persistence.
    """

    def __init__(self):
        self._runs: dict[str, Run] = {}
        self._events: dict[str, list[RunEvent]] = {}
        self._schedules: dict[str, Schedule] = {}
        self._webhooks: dict[int, WebhookDelivery] = {}
        self._idempotency: dict[str, IdempotencyRecord] = {}
        self._next_webhook_id = 1
        self._next_event_id = 1

    async def connect(self) -> None: pass
    async def close(self) -> None: pass
    async def migrate(self) -> None: pass

    async def create_run(self, spec: RunSpec) -> Run:
        run = Run(
            run_id=spec.run_id, state="pending", kind=spec.kind,
            started_at=datetime.now(UTC),
            agent_id=spec.agent_id, model=spec.model,
            trace_tag=spec.trace_tag, correlation_id=spec.correlation_id,
            parent_run_id=spec.parent_run_id,
            sandbox_backend=spec.sandbox_backend, sandbox_url=spec.sandbox_url,
            sandbox_server_id=spec.sandbox_server_id, workspace=spec.workspace,
            environment=spec.environment,
            system_prompt_hash=spec.system_prompt_hash,
            metadata=spec.metadata,
        )
        self._runs[spec.run_id] = run
        self._events[spec.run_id] = []
        return run

    async def get_run(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    async def list_runs(self, flt: RunFilter) -> list[Run]:
        def _match(r: Run) -> bool:
            if flt.state and r.state != flt.state:
                return False
            if flt.kind and r.kind != flt.kind:
                return False
            if flt.agent_id and r.agent_id != flt.agent_id:
                return False
            if flt.trace_tag and r.trace_tag != flt.trace_tag:
                return False
            if flt.correlation_id and r.correlation_id != flt.correlation_id:
                return False
            if flt.parent_run_id and r.parent_run_id != flt.parent_run_id:
                return False
            if flt.since and r.started_at < flt.since:
                return False
            if flt.until and r.started_at > flt.until:
                return False
            return True
        rows = sorted(
            filter(_match, self._runs.values()),
            key=lambda r: r.started_at, reverse=True,
        )
        return rows[: flt.limit]

    async def update_run_state(self, run_id: str, new_state: RunState,
                                 *, ended_at: datetime | None = None) -> None:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"Run not found: {run_id}")
        if not can_transition(run.state, new_state):
            raise ValueError(f"Illegal state transition: {run.state} → {new_state}")
        run.state = new_state
        if is_terminal(new_state):
            run.ended_at = ended_at or datetime.now(UTC)

    async def update_run_sandbox(self, run_id: str, *,
                                   sandbox_url: str | None = None,
                                   sandbox_server_id: str | None = None,
                                   sandbox_backend: str | None = None) -> None:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"Run not found: {run_id}")
        if sandbox_url is not None:
            run.sandbox_url = sandbox_url
        if sandbox_server_id is not None:
            run.sandbox_server_id = sandbox_server_id
        if sandbox_backend is not None:
            run.sandbox_backend = sandbox_backend

    async def mark_orphaned_running_runs(self) -> int:
        n = 0
        now = datetime.now(UTC)
        for run in self._runs.values():
            if run.state in ("pending", "running"):
                run.state = "orphaned"
                run.ended_at = run.ended_at or now
                n += 1
        return n

    async def finalize_run(self, run_id: str, result: dict[str, Any],
                            *, state: RunState = "completed") -> None:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"Run not found: {run_id}")
        if not can_transition(run.state, state):
            raise ValueError(f"Illegal state transition: {run.state} → {state}")
        run.state = state
        run.ended_at = datetime.now(UTC)
        usage = result.get("usage") or {}
        run.result = result
        run.input_tokens = usage.get("input_tokens", 0)
        run.output_tokens = usage.get("output_tokens", 0)
        run.total_tokens = usage.get("total_tokens", 0)
        run.cost_usd = result.get("cost_usd")
        run.finish_reason = result.get("finish_reason")
        run.tool_call_count = len(result.get("tool_calls") or [])
        run.status = result.get("status")
        run.error_type = result.get("error_type")
        run.error_msg = result.get("error_msg")

    async def aggregate_runs(self, *, group_by: str = "trace_tag",
                              since: datetime | None = None,
                              until: datetime | None = None,
                              trace_tag: str | None = None) -> dict:
        if group_by not in AGGREGATE_GROUP_KEYS:
            raise ValueError(
                f"group_by must be one of: "
                f"{', '.join(sorted(AGGREGATE_GROUP_KEYS))}"
            )

        def _matches(r: Run) -> bool:
            if since and r.started_at < since:
                return False
            if until and r.started_at > until:
                return False
            if trace_tag and r.trace_tag != trace_tag:
                return False
            return True

        def _key(r: Run) -> str:
            if group_by == "day":
                return r.started_at.strftime("%Y-%m-%d")
            return getattr(r, group_by) or "<none>"

        groups: dict[str, dict] = {}
        total = {"count": 0, "total_tokens": 0,
                  "cost_usd": 0.0, "error_count": 0}
        for r in self._runs.values():
            if not _matches(r):
                continue
            k = _key(r)
            g = groups.setdefault(k, {
                "key": k, "count": 0, "total_tokens": 0,
                "cost_usd": 0.0, "error_count": 0,
            })
            g["count"] += 1
            g["total_tokens"] += r.total_tokens or 0
            g["cost_usd"] += r.cost_usd or 0.0
            err = 1 if r.status == "error" else 0
            g["error_count"] += err
            total["count"] += 1
            total["total_tokens"] += r.total_tokens or 0
            total["cost_usd"] += r.cost_usd or 0.0
            total["error_count"] += err
        return {
            "group_by": group_by,
            "groups": sorted(groups.values(),
                              key=lambda g: g["count"], reverse=True),
            "total": total,
        }

    async def purge_old_runs(self, max_age_days: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        old = [rid for rid, r in self._runs.items() if r.started_at < cutoff]
        for rid in old:
            self._runs.pop(rid, None)
            self._events.pop(rid, None)
        return len(old)

    async def append_event(self, event: RunEvent) -> RunEvent:
        events = self._events.setdefault(event.run_id, [])
        stored = RunEvent(
            run_id=event.run_id, seq=event.seq, kind=event.kind,
            payload=event.payload, ts=event.ts or datetime.now(UTC),
            event_id=self._next_event_id,
        )
        self._next_event_id += 1
        events.append(stored)
        return stored

    async def list_events(self, run_id: str, *, since_seq: int = 0,
                            limit: int = 1000) -> list[RunEvent]:
        return [e for e in self._events.get(run_id, []) if e.seq > since_seq][:limit]

    async def purge_old_run_events(self, max_age_days: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        purged = 0
        for run_id, events in list(self._events.items()):
            kept = [e for e in events if (e.ts or datetime.now(UTC)) >= cutoff]
            purged += len(events) - len(kept)
            if kept:
                self._events[run_id] = kept
            else:
                self._events.pop(run_id, None)
        return purged

    async def create_schedule(self, s: Schedule) -> Schedule:
        self._schedules[s.id] = s
        return s

    async def get_schedule(self, schedule_id: str) -> Schedule | None:
        return self._schedules.get(schedule_id)

    async def list_schedules(self) -> list[Schedule]:
        return list(self._schedules.values())

    async def update_schedule_run_times(self, schedule_id: str, *,
                                          last_run_at: datetime,
                                          next_run_at: datetime | None) -> None:
        s = self._schedules.get(schedule_id)
        if s is None:
            raise KeyError(schedule_id)
        s.last_run_at = last_run_at
        s.next_run_at = next_run_at

    async def delete_schedule(self, schedule_id: str) -> bool:
        return self._schedules.pop(schedule_id, None) is not None

    async def enqueue_webhook(self, url: str, payload: dict[str, Any],
                                *, run_id: str | None = None,
                                schedule_id: str | None = None) -> int:
        wid = self._next_webhook_id
        self._next_webhook_id += 1
        self._webhooks[wid] = WebhookDelivery(
            id=wid, url=url, payload=payload, state="pending",
            attempts=0, last_status_code=None, last_error=None,
            last_attempt_at=None, next_attempt_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            run_id=run_id, schedule_id=schedule_id,
        )
        return wid

    async def claim_pending_webhooks(self, limit: int = 10) -> list[WebhookDelivery]:
        now = datetime.now(UTC)
        due = [
            w for w in self._webhooks.values()
            if w.state == "pending" and (w.next_attempt_at is None or w.next_attempt_at <= now)
        ][:limit]
        for w in due:
            w.attempts += 1
            w.last_attempt_at = now
            w.next_attempt_at = now + timedelta(minutes=5)
        return due

    async def record_webhook_attempt(self, delivery_id: int, *,
                                       status_code: int | None,
                                       error: str | None,
                                       next_attempt_at: datetime | None) -> None:
        w = self._webhooks.get(delivery_id)
        if w is None:
            return
        w.last_status_code = status_code
        w.last_error = error
        w.next_attempt_at = next_attempt_at
        if status_code and 200 <= status_code < 300:
            w.state = "delivered"
        elif next_attempt_at is None:
            w.state = "failed"

    async def purge_old_webhook_deliveries(self, max_age_days: int = 7) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        old = [
            wid for wid, w in self._webhooks.items()
            if w.state in ("delivered", "failed") and w.created_at < cutoff
        ]
        for wid in old:
            self._webhooks.pop(wid, None)
        return len(old)

    async def count_pending_webhooks(self) -> int:
        return sum(1 for w in self._webhooks.values() if w.state == "pending")

    # --- Idempotency keys ---

    async def get_idempotent(self, key: str) -> IdempotencyRecord | None:
        rec = self._idempotency.get(key)
        if rec is None:
            return None
        if rec.expires_at <= datetime.now(UTC):
            self._idempotency.pop(key, None)
            return None
        return rec

    async def record_idempotent(self, rec: IdempotencyRecord) -> None:
        self._idempotency.setdefault(rec.key, rec)

    async def purge_expired_idempotency_keys(self) -> int:
        now = datetime.now(UTC)
        expired = [k for k, r in self._idempotency.items() if r.expires_at <= now]
        for k in expired:
            self._idempotency.pop(k, None)
        return len(expired)


# ---------------------------------------------------------------------------
# Module-level singleton — chosen by env at startup
# ---------------------------------------------------------------------------

_store: Store | None = None


def _build_store() -> Store:
    from aitelier.config import get_config

    dsn = get_config().database.url
    if dsn:
        return PostgresStore(dsn)
    logger.warning(
        "[database] url unset in aitelier.toml — falling back to InMemoryStore "
        "(no persistence). Set [database] url for durable state, or use "
        "`make start` which writes runs/.session.toml with the dev DSN."
    )
    return InMemoryStore()


async def get_store() -> Store:
    global _store
    if _store is None:
        _store = _build_store()
        await _store.connect()
    return _store


async def close_store() -> None:
    global _store
    if _store is not None:
        await _store.close()
        _store = None


def _set_store_for_tests(store: Store) -> None:
    """Test hook: inject an InMemoryStore (or similar) directly."""
    global _store
    _store = store


# ---------------------------------------------------------------------------
# Row → dataclass helpers
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, dict):
        return value
    return {}


def _row_to_run(row: Any) -> Run:
    # `parent_run_id` is read defensively: old Postgres rows from before
    # migration 003 don't have the column. asyncpg raises KeyError on
    # missing columns; we treat that as None so the runtime stays
    # backward-compatible across rolling upgrades.
    try:
        parent_run_id = row["parent_run_id"]
    except (KeyError, IndexError):
        parent_run_id = None
    return Run(
        run_id=row["run_id"], state=row["state"], kind=row["kind"],
        started_at=row["started_at"], ended_at=row["ended_at"],
        agent_id=row["agent_id"], model=row["model"],
        trace_tag=row["trace_tag"], correlation_id=row["correlation_id"],
        parent_run_id=parent_run_id,
        sandbox_backend=row["sandbox_backend"],
        sandbox_url=row["sandbox_url"],
        sandbox_server_id=row["sandbox_server_id"],
        workspace=row["workspace"],
        environment=_as_dict(row["environment_json"]),
        result=_as_dict(row["result_json"]),
        input_tokens=row["input_tokens"] or 0,
        output_tokens=row["output_tokens"] or 0,
        total_tokens=row["total_tokens"] or 0,
        cost_usd=row["cost_usd"],
        finish_reason=row["finish_reason"],
        tool_call_count=row["tool_call_count"] or 0,
        system_prompt_hash=row["system_prompt_hash"],
        status=row["status"],
        error_type=row["error_type"],
        error_msg=row["error_msg"],
        metadata=_as_dict(row["metadata_json"]),
    )


def _row_to_event(row: Any) -> RunEvent:
    return RunEvent(
        run_id=row["run_id"], seq=row["seq"], kind=row["kind"],
        payload=_as_dict(row["payload_json"]), ts=row["ts"],
        event_id=row["event_id"],
    )


def _row_to_schedule(row: Any) -> Schedule:
    return Schedule(
        id=row["id"], name=row["name"],
        task=_as_dict(row["task_json"]),
        interval_seconds=row["interval_seconds"],
        at_iso=row["at_iso"],
        webhook_url=row["webhook_url"],
        next_run_at=row["next_run_at"],
        last_run_at=row["last_run_at"],
        created_at=row["created_at"],
    )


def _row_to_webhook(row: Any) -> WebhookDelivery:
    return WebhookDelivery(
        id=row["id"], run_id=row.get("run_id") if isinstance(row, dict) else row["run_id"],
        schedule_id=row.get("schedule_id") if isinstance(row, dict) else row["schedule_id"],
        url=row["url"], payload=_as_dict(row["payload_json"]),
        state=row["state"], attempts=row["attempts"],
        last_status_code=row["last_status_code"],
        last_error=row["last_error"],
        last_attempt_at=row["last_attempt_at"],
        next_attempt_at=row["next_attempt_at"],
        created_at=row["created_at"],
    )
