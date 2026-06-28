"""Store protocol + factory. Implementations live in sibling modules.

Two implementations satisfy the Store protocol:
  - `PostgresStore` (storage/postgres.py) — asyncpg-backed, production.
  - `InMemoryStore` (storage/inmemory.py) — process-local, tests + DSN-less dev.

`get_store()` picks the active impl based on whether `[database] url` is
set in aitelier.toml. Nothing outside this package touches a database
connection — consumers call functions on the Store instance and receive
plain dataclasses back.

This module is kept narrow on purpose: the Protocol definition + the
factory + the test-injection hook. Implementation churn happens in the
sibling files without rippling through `from aitelier.storage._store
import …` callers.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from aitelier.storage.inmemory import InMemoryStore
from aitelier.storage.models import (
    IdempotencyRecord,
    Run,
    RunEvent,
    RunFilter,
    RunScore,
    RunSpec,
    RunState,
    Schedule,
    WebhookDelivery,
)
from aitelier.storage.postgres import PostgresStore

logger = logging.getLogger("aitelier.storage")


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
    async def mark_orphaned_running_runs(self) -> list[str]: ...
    # Terminal runs (completed/failed/cancelled) with a `metadata.webhook_url`
    # but no webhook_delivery row — i.e. the process crashed between finalizing
    # the run and enqueuing its completion webhook. Used by the startup sweep to
    # deliver the webhook the async caller is still waiting on. `since` bounds
    # the result to runs that ended at/after it (the caller passes the
    # webhook-retention window so an already-purged delivery isn't re-fired).
    async def runs_awaiting_webhook(self, since: datetime | None = None) -> list[Run]: ...
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

    # Run scores (eval framework write-back)
    async def add_run_score(self, score: RunScore) -> RunScore: ...
    async def list_run_scores(self, run_id: str) -> list[RunScore]: ...


# ---------------------------------------------------------------------------
# Factory + lifecycle
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
