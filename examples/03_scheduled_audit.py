"""Recurring background audit: schedule fires every N minutes, posts to webhook.

`POST /v1/schedules` registers a job that aitelier ticks server-side. Each
fire builds an inference request from the schedule's `task` dict, runs it
end-to-end (LLM or agent path), then POSTs the result to `webhook_url`.
State (next_run_at, last_run_at) lives in Postgres, so schedules survive
restarts.

Pair with `examples/04_webhook_receiver.py` to verify and process the
delivered payload.

Run with `uv run python 03_scheduled_audit.py`.
"""

from __future__ import annotations

import asyncio

from aitelier_client import Aitelier


async def register_audit() -> None:
    ait = Aitelier(base_url="http://localhost:7777")

    # The `task` dict mirrors a /v1/chat/completions request body. Same
    # `aitelier.*` knobs apply — workspace, trace_tag, mcp_servers, etc.
    # The schedule tick handler constructs a ChatCompletionRequest from
    # this dict and routes it through the normal execution path.
    schedule = await ait.create_schedule(
        name="nightly-dependency-audit",
        task={
            "model": "agent:codex",
            "messages": [{
                "role": "user",
                "content": (
                    "Audit /workspace/package.json for outdated and "
                    "vulnerable dependencies. Reply with a JSON list of "
                    "{name, current, latest, severity}."
                ),
            }],
            "aitelier": {
                "workspace": "/path/to/repo",
                "trace_tag":  "nightly-deps",
                "max_turns":  20,
            },
        },
        interval_seconds=60 * 60 * 24,  # daily
        webhook_url="http://localhost:8000/webhooks/aitelier",
    )

    print(f"Registered: {schedule.id} ({schedule.name})")
    print(f"Next fire:  {schedule.next_run_at}")
    print("Webhook URL must be running before the next tick "
          "(see examples/04_webhook_receiver.py).")

    # One-shot variant: `at_iso="2026-06-01T03:00:00Z"` instead of
    # interval_seconds. After firing, last_run_at is set and the
    # schedule never fires again (but the row stays in the table for
    # audit / introspection — DELETE explicitly to clean up).

    # House-cleaning: list all schedules, optionally remove this one.
    all_schedules = await ait.list_schedules()
    print(f"Active schedules: {len(all_schedules)}")
    # await ait.delete_schedule(schedule.id)


if __name__ == "__main__":
    asyncio.run(register_audit())
