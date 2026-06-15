"""PostgresStore — asyncpg-backed implementation of the Store protocol.

Production durable storage for runs, run_events, schedules, webhook
deliveries, idempotency keys. Migrations live in `storage/migrations/`
and run idempotently on `connect()`.

The Store protocol contract is defined in `storage/_store.py`. Row →
dataclass conversion helpers (`_row_to_run`, `_row_to_event`,
`_row_to_schedule`, `_row_to_webhook`, `_as_dict`) live at the bottom
of this module since only the Postgres impl needs them — InMemoryStore
constructs dataclasses directly from kwargs.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aitelier.storage.models import (
    AGGREGATE_GROUP_KEYS,
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
                    environment_json, system_prompt_hash, metadata_json,
                    request_body_json, rendered_messages_json
                )
                VALUES ($1, 'pending', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12::jsonb, $13, $14::jsonb, $15::jsonb, $16::jsonb)
                RETURNING *
                """,
                spec.run_id, spec.kind, spec.agent_id, spec.model,
                spec.trace_tag, spec.correlation_id, spec.parent_run_id,
                spec.sandbox_backend, spec.sandbox_url, spec.sandbox_server_id, spec.workspace,
                json.dumps(spec.environment), spec.system_prompt_hash,
                json.dumps(spec.metadata),
                json.dumps(spec.request_body) if spec.request_body is not None else None,
                json.dumps(spec.rendered_messages) if spec.rendered_messages is not None else None,
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
    # backward-compatible across rolling upgrades. Same pattern for
    # the v4 columns (`request_body_json`, `rendered_messages_json`).
    try:
        parent_run_id = row["parent_run_id"]
    except (KeyError, IndexError):
        parent_run_id = None
    try:
        request_body = _as_dict_or_none(row["request_body_json"])
    except (KeyError, IndexError):
        request_body = None
    try:
        rendered_messages = _as_list_or_none(row["rendered_messages_json"])
    except (KeyError, IndexError):
        rendered_messages = None
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
        request_body=request_body,
        rendered_messages=rendered_messages,
    )


def _as_dict_or_none(value: Any) -> dict[str, Any] | None:
    """JSONB read that preserves the distinction between NULL (column
    unset — historical run from before migration v4) and `{}` (caller
    explicitly sent an empty body). `_as_dict` collapses both to `{}`."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(value, dict):
        return value
    return None


def _as_list_or_none(value: Any) -> list[dict[str, Any]] | None:
    """JSONB read for the rendered_messages column. Same NULL-preserving
    semantics as `_as_dict_or_none`."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None
    if isinstance(value, list):
        return value
    return None


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
