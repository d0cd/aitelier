"""Schedule-tick service. State lives in storage; tick loop lives here.

The public API is async — all functions delegate to the durable store. The
tick loop runs a background coroutine that wakes every 10s, claims due
schedules, and dispatches a caller-supplied handler. State (next_run_at,
last_run_at) is persisted in Postgres so schedules survive aitelier restarts.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aitelier.storage import Schedule, get_store

logger = logging.getLogger("aitelier.schedules")

_tick_task: asyncio.Task | None = None
_TICK_SECONDS = 10.0

# Strong references to in-flight handler tasks. Without this the event loop
# only holds a weak reference and may GC a fired schedule mid-run.
_inflight: set[asyncio.Task] = set()


def _now() -> datetime:
    return datetime.now(UTC)


def _next_run_after(now: datetime, schedule: Schedule) -> datetime | None:
    """Compute when this schedule should next fire, or None if it's done."""
    if schedule.interval_seconds:
        base = schedule.last_run_at or now
        nxt = base + timedelta(seconds=schedule.interval_seconds)
        while nxt < now:
            nxt += timedelta(seconds=schedule.interval_seconds)
        return nxt
    if schedule.at_iso:
        if schedule.last_run_at:
            return None  # one-shot already fired
        return schedule.at_iso
    return None


async def list_schedules() -> list[dict]:
    store = await get_store()
    rows = await store.list_schedules()
    return [_to_dict(s) for s in rows]


async def get_schedule(schedule_id: str) -> dict | None:
    store = await get_store()
    s = await store.get_schedule(schedule_id)
    return _to_dict(s) if s else None


async def create_schedule(spec: dict) -> dict:
    """Persist a schedule.

    Required: `task`. At least one of `interval_seconds` or `at_iso`.
    Optional: `name`, `webhook_url`.
    """
    if "task" not in spec:
        raise ValueError("schedule requires a `task` spec")
    # Validate `task` as a chat-completions body now, not silently at every
    # fire. Without this a schedule with a malformed task returns 201 and
    # then fails on every tick with nothing surfaced at create time.
    from aitelier.openai_compat import ChatCompletionRequest
    task = spec["task"]
    if not isinstance(task, dict):
        raise ValueError("schedule `task` must be a chat-completions request object")
    try:
        # pydantic.ValidationError subclasses ValueError, so this also covers
        # bad/missing fields and extra="forbid" violations.
        ChatCompletionRequest(**task)
    except ValueError as exc:
        raise ValueError(
            f"schedule `task` is not a valid chat-completions request: {exc}"
        ) from None
    if "interval_seconds" not in spec and "at_iso" not in spec:
        raise ValueError("schedule requires `interval_seconds` or `at_iso`")

    now = _now()
    at_iso = spec.get("at_iso")
    if isinstance(at_iso, str):
        at_iso = datetime.fromisoformat(
            at_iso.replace("Z", "+00:00") if at_iso.endswith("Z") else at_iso
        )
    if isinstance(at_iso, datetime) and at_iso.tzinfo is None:
        # Naive timestamps would raise TypeError when compared to the
        # tz-aware tick `now`, poisoning the whole tick loop. Assume UTC.
        at_iso = at_iso.replace(tzinfo=UTC)

    schedule = Schedule(
        id=str(uuid.uuid4()),
        name=spec.get("name", "scheduled"),
        task=spec["task"],
        interval_seconds=spec.get("interval_seconds"),
        at_iso=at_iso,
        webhook_url=spec.get("webhook_url"),
        next_run_at=None,
        last_run_at=None,
        created_at=now,
    )
    schedule.next_run_at = _next_run_after(now, schedule)

    store = await get_store()
    await store.create_schedule(schedule)
    return _to_dict(schedule)


async def delete_schedule(schedule_id: str) -> bool:
    store = await get_store()
    return await store.delete_schedule(schedule_id)


async def _run_tick(now: datetime,
                    handler: Callable[[dict], Awaitable[None]]) -> None:
    """One tick: fire any schedules whose next_run_at <= now.

    Single-process assumption: this list-then-update is not an atomic
    claim, so two aitelier instances pointed at the same database would
    double-fire every due schedule. The runtime is single-process by
    design (same assumption as `_active_runs` in server.py). Adding a
    distributed `claim_due_schedules` would require recomputing
    next_run_at in SQL — duplicating `_next_run_after` and inviting the
    store-divergence bug class — so it's deliberately deferred until
    horizontal scaling is actually on the table.
    """
    store = await get_store()
    schedules = await store.list_schedules()
    for s in schedules:
        if not s.next_run_at or s.next_run_at > now:
            continue
        # Per-schedule isolation: a dispatch or run-time-update failure on one
        # schedule must not abort the remaining due schedules in this tick. A
        # schedule whose `update_schedule_run_times` failed keeps its old
        # next_run_at and simply re-fires next tick (at-least-once).
        try:
            logger.info("Firing schedule %s (%s)", s.id, s.name)
            # Hand the handler the unredacted task — it needs real
            # `headers` / `env` values to dispatch the inference call.
            # The HTTP projection (_to_dict) redacts; this in-process path
            # doesn't cross a trust boundary.
            entry = _to_dict(s)
            entry["task"] = s.task
            t = asyncio.create_task(handler(entry))
            _inflight.add(t)
            t.add_done_callback(_inflight.discard)
            nxt = _next_run_after(now, Schedule(
                id=s.id, name=s.name, task=s.task,
                interval_seconds=s.interval_seconds, at_iso=s.at_iso,
                webhook_url=s.webhook_url,
                next_run_at=s.next_run_at, last_run_at=now,
                created_at=s.created_at,
            ))
            await store.update_schedule_run_times(s.id, last_run_at=now,
                                                     next_run_at=nxt)
        except Exception as exc:
            logger.exception("Schedule %s (%s) tick failed: %s", s.id, s.name, exc)


async def _tick_loop(handler: Callable[[dict], Awaitable[None]]) -> None:
    while True:
        try:
            await asyncio.sleep(_TICK_SECONDS)
            await _run_tick(_now(), handler)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Schedule tick error: %s", exc)


def start_tick_loop(handler: Callable[[dict], Awaitable[None]]) -> None:
    global _tick_task
    if _tick_task is None or _tick_task.done():
        _tick_task = asyncio.create_task(_tick_loop(handler))


def stop_tick_loop() -> None:
    global _tick_task
    if _tick_task and not _tick_task.done():
        _tick_task.cancel()
    _tick_task = None


def _to_dict(s: Schedule) -> dict[str, Any]:
    # Late import: schedules.py is imported during config setup so it can't
    # reach back into server at module load.
    from aitelier.server import _redact_secrets
    return {
        "id": s.id,
        "name": s.name,
        "task": _redact_secrets(s.task),
        "interval_seconds": s.interval_seconds,
        "at_iso": s.at_iso.isoformat() if s.at_iso else None,
        "webhook_url": s.webhook_url,
        "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }
