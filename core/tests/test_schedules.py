"""Tests for the persistent schedule runner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from aitelier import schedules as sch


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path):
    """Each test gets its own schedules.json under a temp dir."""
    with patch("aitelier.schedules._registry_path",
               return_value=tmp_path / "schedules.json"):
        sch._reset_for_tests()
        yield
        sch._reset_for_tests()


def test_create_interval_schedule_computes_next_run():
    entry = sch.create_schedule({
        "name": "every-hour",
        "task": {"name": "audit", "kind": "agent", "model": "claude"},
        "interval_seconds": 3600,
    })
    assert entry["name"] == "every-hour"
    assert entry["next_run_at"] is not None
    nxt = datetime.fromisoformat(entry["next_run_at"])
    diff = nxt - datetime.now(UTC)
    # Roughly 3600s out, allowing for test runtime
    assert 3590 < diff.total_seconds() < 3610


def test_create_oneshot_schedule_at_iso():
    when = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    entry = sch.create_schedule({
        "task": {"name": "ad-hoc", "kind": "complete"},
        "at_iso": when,
    })
    assert entry["next_run_at"] is not None


def test_create_rejects_missing_task():
    with pytest.raises(ValueError):
        sch.create_schedule({"interval_seconds": 60})


def test_create_rejects_no_trigger():
    with pytest.raises(ValueError):
        sch.create_schedule({"task": {"name": "x", "kind": "complete"}})


def test_list_and_get_and_delete():
    entry = sch.create_schedule({
        "task": {"name": "x", "kind": "complete"},
        "interval_seconds": 60,
    })
    assert len(sch.list_schedules()) == 1
    assert sch.get_schedule(entry["id"])["id"] == entry["id"]
    assert sch.delete_schedule(entry["id"]) is True
    assert sch.get_schedule(entry["id"]) is None
    assert sch.delete_schedule("nonexistent") is False


def test_persistence_across_reset():
    entry = sch.create_schedule({
        "task": {"name": "x", "kind": "complete"},
        "interval_seconds": 60,
    })
    # Simulate a fresh process — module-level state reset, file remains.
    sch._reset_for_tests()
    assert sch.get_schedule(entry["id"]) is not None


@pytest.mark.asyncio
async def test_tick_fires_due_schedule_and_advances_next_run():
    """Once next_run_at is in the past, the handler should fire and the next
    run should be scheduled in the future."""
    import asyncio
    sch.create_schedule({
        "task": {"name": "x", "kind": "complete", "prompt": "hi"},
        "interval_seconds": 60,
    })
    # Pretend it's an hour later — schedule is due.
    later = datetime.now(UTC) + timedelta(hours=1)
    fired: list[dict] = []

    async def handler(entry: dict) -> None:
        fired.append(entry)

    await sch._run_tick(later, handler)
    # Handler is dispatched via asyncio.create_task — yield once so it runs.
    await asyncio.sleep(0)
    assert len(fired) == 1

    # Next run_at should be in the future relative to `later`
    [persisted] = sch.list_schedules()
    nxt = datetime.fromisoformat(persisted["next_run_at"])
    assert nxt > later


@pytest.mark.asyncio
async def test_tick_skips_not_yet_due():
    import asyncio
    sch.create_schedule({
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
    sch.create_schedule({
        "task": {"name": "x", "kind": "complete"},
        "at_iso": when,
    })
    later = datetime.now(UTC)
    handler = AsyncMock()
    await sch._run_tick(later, handler)
    await asyncio.sleep(0)
    assert handler.await_count == 1
    # Second tick: should NOT fire again (next_run_at cleared)
    handler.reset_mock()
    await sch._run_tick(later, handler)
    await asyncio.sleep(0)
    assert handler.await_count == 0
