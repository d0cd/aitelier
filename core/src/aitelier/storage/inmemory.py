"""InMemoryStore — process-local Store implementation for tests + DSN-less dev.

Satisfies the same Store protocol (in `storage/_store.py`) as
PostgresStore. No persistence — every restart starts fresh. Used when
`[database] url` is unset in aitelier.toml.

Tests can inject this directly via `_set_store_for_tests` in
`storage/_store.py` to avoid needing Postgres in CI.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from aitelier.storage.models import (
    AGGREGATE_GROUP_KEYS,
    IdempotencyRecord,
    Run,
    RunEvent,
    RunFilter,
    RunScore,
    RunSpec,
    RunState,
    Schedule,
    WebhookDelivery,
    can_transition,
    is_terminal,
)

logger = logging.getLogger("aitelier.storage")


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
        self._scores: list[RunScore] = []
        self._next_webhook_id = 1
        self._next_event_id = 1
        self._next_score_id = 1

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
            request_body=spec.request_body,
            rendered_messages=spec.rendered_messages,
        )
        self._runs[spec.run_id] = run
        self._events[spec.run_id] = []
        return run

    async def get_run(self, run_id: str) -> Run | None:
        run = self._runs.get(run_id)
        # Return a copy so callers can't mutate the canonical row — matches
        # PostgresStore, which rebuilds a fresh dataclass per read.
        return replace(run) if run is not None else None

    async def list_runs(self, flt: RunFilter) -> list[Run]:
        def _match(r: Run) -> bool:
            if flt.state and r.state != flt.state:
                return False
            if flt.status and r.status != flt.status:
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
        return [replace(r) for r in rows[: flt.limit]]

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

    async def mark_orphaned_running_runs(self) -> list[str]:
        flipped: list[str] = []
        now = datetime.now(UTC)
        for run in self._runs.values():
            if run.state in ("pending", "running"):
                run.state = "orphaned"
                run.ended_at = run.ended_at or now
                flipped.append(run.run_id)
        return flipped

    async def runs_awaiting_webhook(self, since: datetime | None = None) -> list[Run]:
        with_delivery = {w.run_id for w in self._webhooks.values() if w.run_id}
        out: list[Run] = []
        for run in self._runs.values():
            # `orphaned` included so a crash mid orphan-webhook-loop (which
            # leaves some orphaned runs without a delivery row) is recovered.
            if run.state not in ("completed", "failed", "cancelled", "orphaned"):
                continue
            if since is not None and not (run.ended_at and run.ended_at >= since):
                continue
            meta = run.metadata if isinstance(run.metadata, dict) else {}
            if meta.get("webhook_url") and run.run_id not in with_delivery:
                out.append(run)
        return out

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
            # Visibility timeout so a concurrent worker doesn't re-grab it.
            # Attempts are counted in record_webhook_attempt (after the delivery
            # actually runs) so a crash between claim and record doesn't burn an
            # attempt — the row is reclaimed after the timeout.
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
        w.attempts += 1
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
            # Expired: treat as absent but DON'T pop — PostgresStore filters
            # expired rows in the WHERE clause and leaves them for the purge
            # worker to delete. Popping here would let a re-record succeed in
            # memory while Postgres (ON CONFLICT DO NOTHING on the lingering
            # row) blocks it until purge — a store-divergence bug.
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

    # Run scores --------------------------------------------------------------

    async def add_run_score(self, score: RunScore) -> RunScore:
        if score.run_id not in self._runs:
            raise KeyError(f"run not found: {score.run_id}")
        stored = RunScore(
            run_id=score.run_id, name=score.name, value=score.value,
            evaluator=score.evaluator, comment=score.comment,
            metadata=score.metadata,
            created_at=datetime.now(UTC),
            id=self._next_score_id,
        )
        self._next_score_id += 1
        self._scores.append(stored)
        return stored

    async def list_run_scores(self, run_id: str) -> list[RunScore]:
        # Order by (created_at, id) to match PostgresStore — `id` breaks ties
        # when two scores share a created_at so latest-wins (`[-1]`) is
        # deterministic across both stores.
        return sorted(
            (s for s in self._scores if s.run_id == run_id),
            key=lambda s: (s.created_at, s.id),
        )


# ---------------------------------------------------------------------------
# Module-level singleton — chosen by env at startup
