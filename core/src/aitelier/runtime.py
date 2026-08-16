"""Shared runtime infrastructure: in-flight registry + saturation cap, SSE
framing, and webhook enqueue/SSRF guard.

A leaf module (imports only config/security/storage/serializers — never server),
so both server.py and inference_exec.py can depend on it without a cycle.
`_active_runs` is shared mutable state: importers reference the same dict, so
the cap, the /v1/runs/active count, and cancellation all see one registry.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from aitelier.config import get_config
from aitelier.security import is_public_url
from aitelier.serializers import _redact_secrets
from aitelier.storage import get_store

logger = logging.getLogger("aitelier")


# Per-process registry of in-flight runs, for cancellation.
# Single-process assumption; if aitelier ever scales horizontally this
# moves to a shared store.
_active_runs: dict[str, asyncio.Task] = {}


# Detached finalize tasks spawned by streaming agent responses. The
# `event_generator`'s finally clause can be interrupted by client
# disconnect, so storage finalize + idempotency cache run in a
# background task. We track them here so tests can await pending
# finalizes deterministically; production drops entries on completion
# via add_done_callback.
_pending_finalize_tasks: set[asyncio.Task] = set()


# Set once shutdown begins. New work is refused from that moment so in-flight
# runs can finish, while `/v1/livez` keeps answering — a draining process is
# alive and must not be restarted out from under its own runs. Held in a dict
# so importers observe mutations rather than a copied bool.
_shutdown = {"draining": False}


def begin_draining() -> None:
    """Stop accepting new work. Idempotent."""
    _shutdown["draining"] = True


def end_draining() -> None:
    """Resume accepting work. Exists for tests and for a cancelled shutdown."""
    _shutdown["draining"] = False


def is_draining() -> bool:
    return _shutdown["draining"]


async def drain_active_runs(timeout: float) -> None:
    """Wait up to `timeout` for in-flight runs to finish, then cancel the rest.

    A rolling deploy wants in-flight agent runs to complete rather than be
    cancelled; a stuck run must not hold the process open forever, so the
    grace period is bounded and anything still running past it is cancelled
    the way shutdown always did it.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    logged = False
    # Re-derive the set each pass: a background producer can register a run
    # after any single snapshot is taken, and a run abandoned that way is never
    # awaited nor cancelled — it lands as a stuck `running` row that the next
    # startup sweep flips to `orphaned`, which is the exact failure draining
    # exists to prevent.
    while True:
        tasks = [t for t in _active_runs.values() if not t.done()]
        if not tasks:
            return
        if not logged:
            logger.info("Draining %d in-flight run(s), up to %.1fs",
                        len(tasks), timeout)
            logged = True
        remaining_time = deadline - asyncio.get_running_loop().time()
        if remaining_time <= 0:
            break
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=remaining_time,
            )
        except TimeoutError:
            break

    remaining = [t for t in _active_runs.values() if not t.done()]
    if not remaining:
        return
    logger.warning(
        "Drain grace period elapsed with %d run(s) still active; cancelling",
        len(remaining),
    )
    for task in remaining:
        task.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*remaining, return_exceptions=True), timeout=2.0,
        )
    except TimeoutError:
        logger.warning("Some runs did not cleanly cancel within 2s")


def _reject_if_saturated() -> None:
    """Cap concurrent inference. Beyond `service.max_in_flight_runs`,
    return 503 typed as `ProviderUnavailable` so SDK retry policies
    treat overload as a transient failure rather than crashing the
    consumer. Cap of 0 disables the check (single-tenant dev).

    Also the single gate where a draining process refuses new work — every
    inference entry point already calls this.
    """
    if is_draining():
        raise HTTPException(
            status_code=503,
            detail=(
                "aitelier is draining for shutdown and is not accepting new "
                "runs. Retry against another instance or after restart."
            ),
        )
    cap = get_config().service.max_in_flight_runs
    if cap and len(_active_runs) >= cap:
        raise HTTPException(
            status_code=503,
            detail=(
                f"aitelier is at capacity ({len(_active_runs)} in-flight "
                f"runs, cap={cap}). Retry after current runs drain."
            ),
        )


@contextmanager
def _track_inflight_run(run_id: str):
    """Register the current task in `_active_runs` for its duration so the
    `service.max_in_flight_runs` cap and `/v1/runs/active` count LLM and
    embeddings runs the same way they count agent runs. The agent path
    registers its own run task directly; this covers the inline-awaited
    LLM/embed paths, which would otherwise slip past the cap entirely."""
    task = asyncio.current_task()
    if task is not None:
        _active_runs[run_id] = task
    try:
        yield
    finally:
        _active_runs.pop(run_id, None)


# SSE comment cadence during silent agent-planning phases. SSE clients
# ignore lines starting with `:`; the frame keeps reverse proxies and
# consumer read timeouts from tearing down a connection mid-run.
_SSE_KEEPALIVE_SECONDS = 25.0


def _cancelled_result(run_id: str, kind: str) -> dict:
    """Result shape returned when a run is cancelled mid-flight."""
    return {
        "kind": kind,
        "provider": "",
        "status": "error",
        "duration_s": 0.0,
        "run_id": run_id,
        "trace_id": run_id,
        "content": None,
        "parsed": None,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "finish_reason": "cancelled",
        "cost_usd": None,
        "error_type": "Cancelled",
        "error_msg": "run cancelled",
    }


async def _check_webhook_url_or_die(url: str) -> None:
    """SSRF guard on aitelier-initiated outbound URLs.

    Always on unless the operator opts in to loopback callbacks via
    `service.allow_loopback_webhooks = true`. Without it a caller could
    POST a `webhook_url` pointing at AWS IMDS (`169.254.169.254`) or
    arbitrary RFC1918 targets and the durable worker would fire at them.
    """
    if get_config().service.allow_loopback_webhooks:
        return
    if not await is_public_url(url):
        raise HTTPException(
            status_code=400,
            detail=(
                "webhook_url must resolve to a public, non-loopback host. "
                "For dev workflows where localhost callbacks are needed, "
                "set [service] allow_loopback_webhooks = true in aitelier.toml."
            ),
        )


async def _enqueue_webhook(
    url: str, payload: dict, *, run_id: str | None = None,
    schedule_id: str | None = None,
) -> None:
    """Enqueue a webhook for durable delivery by the background worker.

    The worker retries with exponential backoff (1s/5s/30s/5min/1hr),
    failing the delivery on the 6th attempt.
    """
    try:
        store = await get_store()
        # Scrub before delivery — the same projection /v1/runs applies to
        # `result`/`metadata`. A webhook receiver shouldn't get credentials in
        # the result/headers/env that the HTTP read path redacts.
        await store.enqueue_webhook(url, _redact_secrets(payload),
                                     run_id=run_id, schedule_id=schedule_id)
    except Exception as exc:
        # Enqueue is the only delivery path: an inline POST fallback would
        # skip the Bearer auth header AND the delivery-time SSRF re-check,
        # both of which the worker applies on the durable path. Better to
        # log + lose this single delivery than to emit an unauthenticated,
        # un-SSRF-checked one.
        logger.warning(
            "Webhook enqueue failed (%s); delivery dropped. run_id=%s schedule_id=%s",
            exc, run_id, schedule_id,
        )


def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Disable nginx response buffering; otherwise events stall until the
    # consumer connection idles and a buffer flush is forced.
    "X-Accel-Buffering": "no",
}


def _sse_response(generator) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
