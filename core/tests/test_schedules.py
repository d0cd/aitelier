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
        "task": {"model": "agent:claude/claude-sonnet-4-5",
                 "messages": [{"role": "user", "content": "audit"}]},
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
        "task": {"model": "claude-sonnet", "messages": [{"role": "user", "content": "ad-hoc"}]},
        "at_iso": when,
    })
    assert entry["next_run_at"] is not None


@pytest.mark.asyncio
async def test_create_oneshot_naive_at_iso_normalized_to_utc():
    """A tz-less at_iso must not produce a naive next_run_at — comparing it to
    the tz-aware tick `now` would raise TypeError and poison the tick loop."""
    naive = "2099-01-01T00:00:00"  # no offset
    entry = await sch.create_schedule({
        "task": {"model": "claude-sonnet", "messages": [{"role": "user", "content": "x"}]},
        "at_iso": naive,
    })
    next_run = datetime.fromisoformat(entry["next_run_at"])
    assert next_run.tzinfo is not None
    # Tick comparison against tz-aware now must not raise.
    assert next_run > datetime.now(UTC)


@pytest.mark.asyncio
async def test_create_rejects_missing_task():
    with pytest.raises(ValueError):
        await sch.create_schedule({"interval_seconds": 60})


@pytest.mark.asyncio
async def test_create_rejects_malformed_task():
    """A `task` that isn't a valid chat-completions body is rejected at
    create time, not silently accepted to fail on every fire."""
    with pytest.raises(ValueError, match="task"):
        await sch.create_schedule({
            "task": {"messages": [{"role": "user", "content": "hi"}]},  # no model
            "interval_seconds": 60,
        })
    with pytest.raises(ValueError, match="task"):
        await sch.create_schedule({
            "task": "not-an-object",
            "interval_seconds": 60,
        })


@pytest.mark.asyncio
async def test_create_rejects_no_trigger():
    with pytest.raises(ValueError):
        await sch.create_schedule({
            "task": {
                "model": "claude-sonnet",
                "messages": [{"role": "user", "content": "x"}],
            },
        })


@pytest.mark.asyncio
async def test_list_and_get_and_delete():
    entry = await sch.create_schedule({
        "task": {"model": "claude-sonnet", "messages": [{"role": "user", "content": "x"}]},
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
        "task": {"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]},
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
        "task": {"model": "claude-sonnet", "messages": [{"role": "user", "content": "x"}]},
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
        "task": {"model": "claude-sonnet", "messages": [{"role": "user", "content": "x"}]},
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


# --- End-to-end: real _schedule_handler routes through chat-completion helpers


@pytest.mark.asyncio
async def test_schedule_handler_routes_agent_task_and_enqueues_webhook(monkeypatch):
    """When a schedule fires, _schedule_handler should build a
    ChatCompletionRequest, route through the right helper (agent here), and
    deliver the result via the durable webhook queue."""
    from aitelier.server import _schedule_handler

    called: dict = {}

    async def fake_call_via_sandbox(name, prompt, **kw):
        called["name"] = name
        called["prompt"] = prompt
        return {
            "kind": "agent", "provider": name, "status": "ok",
            "duration_s": 0.05, "run_id": kw.get("run_id", ""),
            "trace_id": kw.get("run_id", ""),
            "content": "scheduled ok",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "finish_reason": "completed", "tool_calls": [],
            "cost_usd": None, "error_type": None, "error_msg": None,
        }

    monkeypatch.setattr(
        "aitelier.providers.sandbox_agent.call_via_sandbox", fake_call_via_sandbox,
    )

    enqueued: list[dict] = []

    async def fake_enqueue(url, payload, **kw):
        enqueued.append({"url": url, "payload": payload, **kw})

    monkeypatch.setattr("aitelier.server._enqueue_webhook", fake_enqueue)

    await _schedule_handler({
        "id": "s-1",
        "name": "nightly",
        "task": {
            "model": "agent:claude/claude-sonnet-4-5",
            "messages": [{"role": "user", "content": "scheduled task"}],
        },
        "webhook_url": "https://hooks.example.com/done",
    })

    assert called["name"] == "claude"
    assert called["prompt"] == "scheduled task"
    assert len(enqueued) == 1
    entry = enqueued[0]
    assert entry["url"] == "https://hooks.example.com/done"
    assert entry["schedule_id"] == "s-1"
    # Webhook payload wraps the ChatCompletion under `result`.
    assert "result" in entry["payload"]
    inner = entry["payload"]["result"]
    assert inner["choices"][0]["message"]["content"] == "scheduled ok"


@pytest.mark.asyncio
async def test_schedule_handler_routes_llm_task(monkeypatch):
    """LLM-path schedules route through chat_completion()."""
    from aitelier.server import _schedule_handler

    async def fake_chat_completion(body, *, timeout=60):
        return {
            "id": "chatcmpl-x", "object": "chat.completion",
            "created": 0, "model": body["model"],
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "scheduled llm"},
                "finish_reason": "stop", "logprobs": None,
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr("aitelier.inference_exec.chat_completion", fake_chat_completion)
    monkeypatch.setattr(
        "aitelier.server._enqueue_webhook",
        AsyncMock(),
    )

    await _schedule_handler({
        "id": "s-2",
        "name": "llm-job",
        "task": {
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "scheduled task"}],
        },
    })
    # No webhook URL → enqueue should not have been called; handler still ran.


@pytest.mark.asyncio
async def test_tick_isolates_per_schedule_failure():
    """One schedule's run-time-update failure must not abort the other due
    schedules in the same tick."""
    import asyncio

    from aitelier.storage import get_store

    await sch.create_schedule({
        "name": "a",
        "task": {"model": "claude-sonnet", "messages": [{"role": "user", "content": "1"}]},
        "interval_seconds": 60,
    })
    await sch.create_schedule({
        "name": "b",
        "task": {"model": "claude-sonnet", "messages": [{"role": "user", "content": "2"}]},
        "interval_seconds": 60,
    })
    later = datetime.now(UTC) + timedelta(hours=1)
    fired: list[str] = []

    async def handler(entry: dict) -> None:
        fired.append(entry["name"])

    # Make the FIRST update_schedule_run_times call blow up; later calls work.
    store = await get_store()
    orig = store.update_schedule_run_times
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB error")
        return await orig(*args, **kwargs)

    store.update_schedule_run_times = flaky
    try:
        await sch._run_tick(later, handler)
        await asyncio.sleep(0)
    finally:
        store.update_schedule_run_times = orig

    # Both schedules dispatched their handler despite the first's update failing.
    assert sorted(fired) == ["a", "b"]
