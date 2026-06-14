"""Run orchestration helpers — record state transitions around an awaitable.

Used by every endpoint that executes inference (`/v1/chat/completions`,
`/v1/embeddings`, `/v1/runs`) so all paths produce the same durable
state-machine flow:

    create_run (pending) → update_run_state (running) → finalize_run

Errors during the awaitable get recorded as state=failed.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable
from typing import Any

from aitelier.errors import classify_error, scrub_error_text
from aitelier.storage import RunSpec, get_store

logger = logging.getLogger("aitelier.runs")


def hash_system_prompt(system_prompt: str | None) -> str | None:
    if not system_prompt:
        return None
    return hashlib.sha256(system_prompt.encode()).hexdigest()[:16]


async def start_run(spec: RunSpec) -> None:
    """Persist a fresh run and transition to `running`. Used by streaming
    paths that own their own state machine — `record_run` handles the
    `pending → running → terminal` lifecycle for non-streaming flows,
    but streaming generators need to yield chunks before the terminal
    state is known and so finalize separately."""
    store = await get_store()
    await store.create_run(spec)
    await store.update_run_state(spec.run_id, "running")


async def record_run(spec: RunSpec, work: Awaitable[dict[str, Any]]) -> dict:
    """Persist a run around an awaitable. Single state-machine path.

    Cancellation can land at any await point — including INSIDE create_run
    *after* the Postgres INSERT has committed. `_finalize_terminal`
    tolerates a missing row (KeyError), so we attempt finalize on every
    cancellation/exception unconditionally rather than tracking a `created`
    flag that lies about the database state.
    """
    import asyncio

    store = await get_store()
    try:
        await store.create_run(spec)
        await store.update_run_state(spec.run_id, "running")
        result = await work
    except asyncio.CancelledError:
        await _finalize_terminal(
            store, spec.run_id,
            status="cancelled",
            error_type="Cancelled", error_msg="run cancelled",
            finish_reason="cancelled", state="cancelled",
        )
        raise
    except Exception as exc:
        await _finalize_terminal(
            store, spec.run_id,
            status="error",
            error_type=classify_error(exc), error_msg=scrub_error_text(str(exc)),
            finish_reason="error", state="failed",
        )
        raise
    final_state = "failed" if result.get("status") == "error" else "completed"
    # Stamp a canonical outcome so TraceRecord.status reports success/error
    # without consumers cross-referencing `state`. Error paths already set
    # status="error" via the dict; we only fill the success case here.
    if result.get("status") is None:
        result = {**result, "status": "ok"}
    await store.finalize_run(spec.run_id, result, state=final_state)
    return result


async def _finalize_terminal(
    store, run_id: str, *, status: str, error_type: str, error_msg: str,
    finish_reason: str, state: str,
) -> None:
    """Finalize a run with a terminal error/cancelled state. Tolerant of
    races where the run was already finalized by another path — we never
    re-raise from here, since the cleanup runs from an `except` block.

    `status` is the outcome category surfaced in TraceRecord.status;
    `state` is the lifecycle position. They diverge only on cancellation
    (status="cancelled", state="cancelled") vs failure (both "error" /
    "failed"). Consumers filtering for genuine failures should query
    `status="error"`; consumers wanting user-initiated stops query
    `status="cancelled"`."""
    try:
        await store.finalize_run(
            run_id,
            {
                "status": status, "error_type": error_type,
                "error_msg": error_msg, "finish_reason": finish_reason,
            },
            state=state,
        )
    except (KeyError, ValueError):
        # Row missing (KeyError) or already terminal (ValueError from the
        # state-machine transition check) — either way, the run is in a
        # state someone else already settled.
        pass
    except Exception as exc:
        # Don't let cleanup-time storage errors swallow the original
        # cancellation/failure — log and continue.
        logger.warning(
            "finalize_run(state=%s) failed for %s: %s", state, run_id, exc,
        )
