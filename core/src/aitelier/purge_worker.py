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

    # Each pass is a zero-arg callable so the coroutine is created only when
    # we're about to await it — a tuple of pre-built coroutines would leak
    # "never awaited" warnings if the worker is cancelled mid-tick.
    for name, call in (
        ("idempotency_keys", store.purge_expired_idempotency_keys),
        ("webhook_deliveries",
         lambda: store.purge_old_webhook_deliveries(
             max_age_days=cfg.webhook_retention_days,
         )),
        ("run_events",
         lambda: store.purge_old_run_events(
             max_age_days=cfg.event_retention_days,
         )),
    ):
        try:
            removed = await call()
            if removed:
                logger.info("purge_%s removed %d row(s)", name, removed)
        except Exception as exc:
            logger.exception("purge_%s failed: %s: %s",
                             name, type(exc).__name__, exc)


async def _loop() -> None:
    # Config is read once (get_config caches); the interval is fixed for the
    # life of the process. The worker is only started when interval > 0
    # (see start_purge_worker), so enabling/disabling requires a restart.
    interval = get_config().purge.interval_seconds
    # Tick once on boot: with a 1h default interval, a frequently-restarting
    # process would otherwise never purge idempotency_keys / webhooks /
    # events. The in-loop sleep-first still rate-limits retries on failure.
    try:
        await _purge_tick()
    except Exception as exc:
        logger.exception("purge worker initial tick errored: %s", exc)
    while True:
        try:
            await asyncio.sleep(interval)
            await _purge_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("purge worker tick errored: %s", exc)


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
