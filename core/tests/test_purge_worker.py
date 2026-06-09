"""Tests for the background purge worker."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_purge_tick_calls_all_three_purges():
    """One sweep hits idempotency, webhook_deliveries, and run_events."""
    from aitelier.purge_worker import _purge_tick

    fake_store = AsyncMock()
    fake_store.purge_expired_idempotency_keys = AsyncMock(return_value=2)
    fake_store.purge_old_webhook_deliveries = AsyncMock(return_value=5)
    fake_store.purge_old_run_events = AsyncMock(return_value=10)

    with patch("aitelier.purge_worker.get_store",
                new=AsyncMock(return_value=fake_store)):
        await _purge_tick()

    fake_store.purge_expired_idempotency_keys.assert_awaited_once()
    fake_store.purge_old_webhook_deliveries.assert_awaited_once()
    fake_store.purge_old_run_events.assert_awaited_once()


@pytest.mark.asyncio
async def test_purge_tick_continues_after_individual_failure():
    """A failure in one pass must not block the others — partial cleanup
    is better than none."""
    from aitelier.purge_worker import _purge_tick

    fake_store = AsyncMock()
    fake_store.purge_expired_idempotency_keys = AsyncMock(
        side_effect=RuntimeError("postgres hiccup"),
    )
    fake_store.purge_old_webhook_deliveries = AsyncMock(return_value=0)
    fake_store.purge_old_run_events = AsyncMock(return_value=0)

    with patch("aitelier.purge_worker.get_store",
                new=AsyncMock(return_value=fake_store)):
        await _purge_tick()  # should not raise

    fake_store.purge_old_webhook_deliveries.assert_awaited_once()
    fake_store.purge_old_run_events.assert_awaited_once()


def test_start_purge_worker_skipped_when_interval_zero():
    """[purge] interval_seconds = 0 disables the worker."""
    from aitelier.config import get_config
    from aitelier.purge_worker import start_purge_worker, stop_purge_worker

    cfg = get_config()
    original = cfg.purge.interval_seconds
    cfg.purge.interval_seconds = 0
    try:
        start_purge_worker()
        from aitelier import purge_worker
        assert purge_worker._task is None
    finally:
        cfg.purge.interval_seconds = original
        stop_purge_worker()
