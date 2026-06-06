"""Tests for the schedule-tick service.

State persists in the storage layer (InMemoryStore under test); schedules.py
is the async wrapper that orchestrates create/list/delete + the tick loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aitelier import schedules as sch


@pytest.mark.asyncio
async def test_create_interval_schedule_computes_next_run():
    entry = await sch.create_schedule({
        "name": "every-hour",
        "task": {"name": "audit", "kind": "agent", "model": "claude"},
        "interval_seconds": 3600,
    })
    assert entry["name"] == "every-hour"
    assert entry["next_run_at"] is not None
    nxt = datetime.fromisoformat(entry["next_run_at"])
    diff = nxt - datetime.now(UTC)
    assert 3590 < diff.total_seconds() < 3610


@pytest.mark.asyncio
async def test_create_oneshot_schedule_at_iso():
    when = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    entry = await sch.create_schedule({
        "task": {"name": "ad-hoc", "kind": "complete"},
        "at_iso": when,
    })
    assert entry["next_run_at"] is not None


@pytest.mark.asyncio
async def test_create_rejects_missing_task():
    with pytest.raises(ValueError):
        await sch.create_schedule({"interval_seconds": 60})


@pytest.mark.asyncio
async def test_create_rejects_no_trigger():
    with pytest.raises(ValueError):
        await sch.create_schedule({"task": {"name": "x", "kind": "complete"}})


@pytest.mark.asyncio
async def test_list_and_get_and_delete():
    entry = await sch.create_schedule({
        "task": {"name": "x", "kind": "complete"},
        "interval_seconds": 60,
    })
    assert len(await sch.list_schedules()) == 1
    fetched = await sch.get_schedule(entry["id"])
    assert fetched["id"] == entry["id"]
    assert await sch.delete_schedule(entry["id"]) is True
    assert await sch.get_schedule(entry["id"]) is None
    assert await sch.delete_schedule("nonexistent") is False


@pytest.mark.asyncio
async def test_tick_fires_due_schedule_and_advances_next_run():
    import asyncio

    await sch.create_schedule({
        "task": {"name": "x", "kind": "complete", "prompt": "hi"},
        "interval_seconds": 60,
    })
    later = datetime.now(UTC) + timedelta(hours=1)
    fired: list[dict] = []

    async def handler(entry: dict) -> None:
        fired.append(entry)

    await sch._run_tick(later, handler)
    # Handler dispatched via asyncio.create_task — yield so it runs.
    await asyncio.sleep(0)
    assert len(fired) == 1

    schedules = await sch.list_schedules()
    [persisted] = schedules
    nxt = datetime.fromisoformat(persisted["next_run_at"])
    assert nxt > later


@pytest.mark.asyncio
async def test_tick_skips_not_yet_due():
    import asyncio
    await sch.create_schedule({
        "task": {"name": "x", "kind": "complete"},
        "interval_seconds": 3600,
    })
    now = datetime.now(UTC)
    fired = []

    async def handler(entry: dict) -> None:
        fired.append(entry)

    await sch._run_tick(now, handler)
    await asyncio.sleep(0)
    assert fired == []


@pytest.mark.asyncio
async def test_oneshot_does_not_refire():
    import asyncio
    when = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    await sch.create_schedule({
        "task": {"name": "x", "kind": "complete"},
        "at_iso": when,
    })
    later = datetime.now(UTC)
    handler = AsyncMock()
    await sch._run_tick(later, handler)
    await asyncio.sleep(0)
    assert handler.await_count == 1
    handler.reset_mock()
    await sch._run_tick(later, handler)
    await asyncio.sleep(0)
    assert handler.await_count == 0
