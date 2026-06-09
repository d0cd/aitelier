"""Background purge worker — keeps Postgres bounded on long-lived processes.

`purge_old_runs(30d)` already runs at startup; on a long-uptime aitelier
that's not enough — `idempotency_keys`, terminal `webhook_deliveries`,
and `run_events` accumulate between restarts. This worker wakes every
`purge.interval_seconds`, calls all three store-level purges, and logs
the counts.

The interval is configurable. Set `[purge] interval_seconds = 0` to
disable the worker entirely (the startup `purge_old_runs` still runs).
"""

from __future__ import annotations

import asyncio
import logging

from aitelier.config import get_config
from aitelier.storage import get_store

logger = logging.getLogger("aitelier.purge_worker")

_task: asyncio.Task | None = None


async def _purge_tick() -> None:
    """One sweep. Best-effort: a failure in any pass logs but does not
    block the others — we'd rather purge two of three than zero."""
    cfg = get_config().purge
    store = await get_store()

    for name, coro in (
        ("idempotency_keys", store.purge_expired_idempotency_keys()),
        ("webhook_deliveries",
         store.purge_old_webhook_deliveries(
             max_age_days=cfg.webhook_retention_days,
         )),
        ("run_events",
         store.purge_old_run_events(
             max_age_days=cfg.event_retention_days,
         )),
    ):
        try:
            removed = await coro
            if removed:
                logger.info("purge_%s removed %d row(s)", name, removed)
        except Exception as exc:
            logger.warning("purge_%s failed: %s: %s",
                            name, type(exc).__name__, exc)


async def _loop() -> None:
    while True:
        try:
            # Re-read on every tick so operator config edits take effect
            # without a restart. A zero/negative value pauses the worker
            # without killing it.
            interval = get_config().purge.interval_seconds
            if interval <= 0:
                await asyncio.sleep(60)
                continue
            await asyncio.sleep(interval)
            await _purge_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("purge worker tick errored: %s", exc)


def start_purge_worker() -> None:
    global _task
    if get_config().purge.interval_seconds <= 0:
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def stop_purge_worker() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
