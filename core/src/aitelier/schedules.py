"""Persistent scheduled task runner.

File-backed registry of schedules with a background tick loop that fires them
at the right time. Two schedule kinds:

- interval: `{interval_seconds: 3600}` — runs every N seconds
- one-shot: `{at_iso: "2026-05-12T20:00:00Z"}` — runs once at that time

Each schedule carries a TaskSpec dict (any task aitelier can run) plus an
optional webhook URL that's POSTed to on completion.

Single-process: state lives in `runs/schedules.json`. Persists across restarts.
Not safe under concurrent aitelier processes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aitelier.config import get_config

logger = logging.getLogger("aitelier.schedules")

_schedules: dict[str, dict] = {}
_loaded = False
_tick_task: asyncio.Task | None = None
_TICK_SECONDS = 10.0


def _registry_path() -> Path:
    return Path(get_config().runs_dir) / "schedules.json"


def _load() -> None:
    global _schedules, _loaded
    if _loaded:
        return
    path = _registry_path()
    if path.exists():
        try:
            _schedules = json.loads(path.read_text())
        except Exception as exc:
            logger.warning("Failed to load schedules registry: %s", exc)
            _schedules = {}
    _loaded = True


def _save() -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_schedules, indent=2, default=str))


def _now() -> datetime:
    return datetime.now(UTC)


def _next_run_after(now: datetime, schedule: dict) -> datetime | None:
    """Compute when this schedule should next fire, or None if it's done."""
    if interval := schedule.get("interval_seconds"):
        last = schedule.get("last_run_at")
        base = datetime.fromisoformat(last) if last else now
        nxt = base + timedelta(seconds=int(interval))
        # If we've slept past several intervals, skip to the next one in the future.
        while nxt < now:
            nxt += timedelta(seconds=int(interval))
        return nxt
    if at := schedule.get("at_iso"):
        if schedule.get("last_run_at"):
            return None  # one-shot already fired
        return datetime.fromisoformat(at.replace("Z", "+00:00") if at.endswith("Z") else at)
    return None


def list_schedules() -> list[dict]:
    _load()
    return list(_schedules.values())


def get_schedule(schedule_id: str) -> dict | None:
    _load()
    return _schedules.get(schedule_id)


def create_schedule(spec: dict) -> dict:
    """Persist a schedule. Required keys: `task` (dict). At least one of
    `interval_seconds` or `at_iso`. Optional: `name`, `webhook_url`.
    """
    _load()
    if "task" not in spec:
        raise ValueError("schedule requires a `task` spec")
    if "interval_seconds" not in spec and "at_iso" not in spec:
        raise ValueError("schedule requires `interval_seconds` or `at_iso`")

    now = _now()
    sid = str(uuid.uuid4())
    entry: dict = {
        "id": sid,
        "created_at": now.isoformat(),
        "name": spec.get("name", "scheduled"),
        "task": spec["task"],
        "webhook_url": spec.get("webhook_url"),
        "interval_seconds": spec.get("interval_seconds"),
        "at_iso": spec.get("at_iso"),
        "last_run_at": None,
    }
    nxt = _next_run_after(now, entry)
    entry["next_run_at"] = nxt.isoformat() if nxt else None
    _schedules[sid] = entry
    _save()
    return entry


def delete_schedule(schedule_id: str) -> bool:
    _load()
    if schedule_id not in _schedules:
        return False
    _schedules.pop(schedule_id)
    _save()
    return True


async def _run_tick(now: datetime,
                    handler: Callable[[dict], Awaitable[None]]) -> None:
    """One tick: fire any schedules whose next_run_at <= now."""
    for sid, entry in list(_schedules.items()):
        nra = entry.get("next_run_at")
        if not nra:
            continue
        try:
            nra_dt = datetime.fromisoformat(nra)
        except ValueError:
            continue
        if nra_dt > now:
            continue
        logger.info("Firing schedule %s (%s)", sid, entry.get("name"))
        # Detached: don't block the tick loop on a long agent run.
        asyncio.create_task(handler(entry))
        entry["last_run_at"] = now.isoformat()
        nxt = _next_run_after(now, entry)
        entry["next_run_at"] = nxt.isoformat() if nxt else None
    _save()


async def _tick_loop(handler: Callable[[dict], Awaitable[None]]) -> None:
    while True:
        try:
            await asyncio.sleep(_TICK_SECONDS)
            await _run_tick(_now(), handler)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Schedule tick error: %s", exc)


def start_tick_loop(handler: Callable[[dict], Awaitable[None]]) -> None:
    global _tick_task
    _load()
    if _tick_task is None or _tick_task.done():
        _tick_task = asyncio.create_task(_tick_loop(handler))


def stop_tick_loop() -> None:
    global _tick_task
    if _tick_task and not _tick_task.done():
        _tick_task.cancel()
    _tick_task = None


# --- Test helpers --------------------------------------------------------

def _reset_for_tests() -> None:
    """Clear in-memory state. Use a tmp_path for the registry in tests."""
    global _schedules, _loaded
    _schedules = {}
    _loaded = False
