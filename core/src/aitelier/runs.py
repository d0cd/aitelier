"""Run orchestration helpers — record state transitions around an awaitable.

Used by both `runner.execute()` and the direct-provider endpoints
(`/v1/complete`, `/v1/embed`, streaming variants, `/v1/agent/stream`)
so all paths produce the same durable state-machine flow:

    create_run (pending) → update_run_state (running) → finalize_run

Errors during the awaitable get recorded as state=failed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable
from typing import Any

from aitelier.storage import RunSpec, get_store


def hash_system_prompt(system_prompt: str | None) -> str | None:
    if not system_prompt:
        return None
    return hashlib.sha256(system_prompt.encode()).hexdigest()[:16]


async def record_run(spec: RunSpec, work: Awaitable[dict[str, Any]]) -> dict:
    """Persist a run around an awaitable. Single state-machine path."""
    store = await get_store()
    await store.create_run(spec)
    await store.update_run_state(spec.run_id, "running")
    try:
        result = await work
    except Exception as exc:
        await store.finalize_run(
            spec.run_id,
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "error_msg": str(exc),
                "finish_reason": "error",
            },
            state="failed",
        )
        raise
    final_state = "failed" if result.get("status") == "error" else "completed"
    await store.finalize_run(spec.run_id, result, state=final_state)
    return result
