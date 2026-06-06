"""Storage layer for aitelier — single owner of all SQL.

Nothing outside this package touches a database connection. Consumers
(server.py, runner.py, schedules.py) call functions exported here and
receive plain dicts / dataclasses back.

Two implementations satisfy the same interface:
  - `PostgresStore`  — production, asyncpg-backed
  - `InMemoryStore`  — tests, no infra needed

The active implementation is chosen at startup time by `get_store()` based
on whether DATABASE_URL is set.
"""

from __future__ import annotations

from aitelier.storage._store import (
    InMemoryStore,
    PostgresStore,
    Store,
    close_store,
    get_store,
)
from aitelier.storage.models import (
    Run,
    RunEvent,
    RunFilter,
    RunSpec,
    RunState,
    Schedule,
    WebhookDelivery,
)

__all__ = [
    "Store",
    "PostgresStore",
    "InMemoryStore",
    "get_store",
    "close_store",
    "Run",
    "RunEvent",
    "RunFilter",
    "RunSpec",
    "RunState",
    "Schedule",
    "WebhookDelivery",
]
