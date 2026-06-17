"""Durable webhook delivery — background task with exponential backoff.

Consumers register webhooks via store.enqueue_webhook(); this worker claims
pending deliveries every few seconds and POSTs them. On non-2xx or network
error, schedules a retry with exponential backoff. After the backoff
schedule is exhausted it gives up and marks the delivery `failed`.

Retry delays after attempts 1-5: 1s, 5s, 30s, 5min, 1hr. The 6th attempt
failing → `state='failed'`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("aitelier.webhooks")

_worker_task: asyncio.Task | None = None
_TICK_SECONDS = 5.0
_BACKOFF_SECONDS = [1, 5, 30, 5 * 60, 60 * 60]
# One delivery attempt per backoff entry, plus the final attempt that
# exhausts the schedule and fails. Derived so the cap can't drift out of
# sync with the schedule (which would strand the last backoff entry dead).
_MAX_ATTEMPTS = len(_BACKOFF_SECONDS) + 1


def _next_attempt_at(attempts: int) -> datetime | None:
    """Return when the next attempt should run, or None to mark `failed`.

    `attempts` is the count *including* the attempt just made (the claim no
    longer pre-increments — record_webhook_attempt does). So the first failed
    delivery passes attempts=1 here and waits `_BACKOFF_SECONDS[0]` (1s) — hence
    the `attempts - 1` index.
    """
    if attempts >= _MAX_ATTEMPTS:
        return None
    idx = min(max(attempts - 1, 0), len(_BACKOFF_SECONDS) - 1)
    delay = _BACKOFF_SECONDS[idx]
    return datetime.now(UTC) + timedelta(seconds=delay)


async def _deliver_once(delivery) -> None:
    """Try one delivery. Updates state via store.record_webhook_attempt."""
    from aitelier.config import get_config
    from aitelier.providers.llm import get_shared_client
    from aitelier.security import is_public_url
    from aitelier.storage import get_store

    status_code: int | None = None
    error: str | None = None

    # Re-validate the URL at delivery time, not just at enqueue time.
    # An operator may have flipped `allow_loopback_webhooks` off after
    # the row was queued, or DNS may have rebinding-shifted what the
    # hostname resolves to. If the URL no longer passes the SSRF guard,
    # mark this delivery failed instead of firing the request.
    if not get_config().service.allow_loopback_webhooks:
        if not await is_public_url(delivery.url):
            store = await get_store()
            await store.record_webhook_attempt(
                delivery.id, status_code=None,
                error="SSRF guard: URL is loopback/private/link-local",
                next_attempt_at=None,
            )
            logger.warning(
                "Webhook %s rejected at delivery: SSRF guard tripped on %s",
                delivery.id, delivery.url,
            )
            return

    # Authenticate the delivery via a pre-shared Bearer token in the
    # Authorization header. Only active when `service.webhook_secret`
    # is set. Receiver verifies with a constant-time string compare:
    #   import hmac
    #   token = auth_header.removeprefix("Bearer ")
    #   if not hmac.compare_digest(token, expected_secret):
    #       reject()
    #
    # Why Bearer rather than HMAC body signatures? Body-byte fidelity
    # between sender serialization and receiver reception is easy to
    # get wrong (header-set order, trailing newlines, intermediate
    # proxies re-encoding) and provides no value over Bearer when
    # transport is HTTPS. HTTPS already protects body integrity in
    # transit; Bearer authenticates that the delivery came from a
    # process holding the shared secret.
    body_bytes = json.dumps(delivery.payload, default=str).encode()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    secret = get_config().service.webhook_secret
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    try:
        client = await get_shared_client()
        resp = await client.post(
            delivery.url,
            content=body_bytes,
            headers=headers,
            timeout=10.0,
        )
        status_code = resp.status_code
        if status_code < 200 or status_code >= 300:
            # Status only — webhook receivers may echo secrets/PII in their
            # error bodies, and last_error is persisted to Postgres + logs.
            error = f"HTTP {status_code}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    # Decide next state. 2xx → delivered. Non-2xx with retries left → pending.
    # Otherwise → failed.
    success = status_code is not None and 200 <= status_code < 300
    # The attempt just made is delivery.attempts + 1 (the claim no longer
    # increments — record_webhook_attempt does). Compute the backoff from it.
    this_attempt = delivery.attempts + 1
    if success:
        next_at = None  # not reused — record_webhook_attempt sees 2xx and marks delivered
    else:
        next_at = _next_attempt_at(this_attempt)

    store = await get_store()
    await store.record_webhook_attempt(
        delivery.id,
        status_code=status_code,
        error=error,
        next_attempt_at=next_at,
    )

    if success:
        logger.info("Webhook %s delivered (status %s)", delivery.id, status_code)
    elif next_at is None:
        logger.warning(
            "Webhook %s failed after %d attempts: %s",
            delivery.id, this_attempt, error,
        )
    else:
        logger.info(
            "Webhook %s retry %d/%d at %s: %s",
            delivery.id, this_attempt, _MAX_ATTEMPTS,
            next_at.isoformat(), error,
        )


async def _worker_tick() -> None:
    """One pass: claim what's due, deliver each, record outcome."""
    from aitelier.storage import get_store
    store = await get_store()
    due = await store.claim_pending_webhooks(limit=20)
    if not due:
        return
    await asyncio.gather(
        *(_deliver_once(d) for d in due), return_exceptions=True,
    )


async def _worker_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_TICK_SECONDS)
            await _worker_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Webhook worker tick error: %s", exc)


def start_webhook_worker() -> None:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker_loop())


def stop_webhook_worker() -> None:
    global _worker_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
    _worker_task = None


# Expose internal hooks for unit tests
__all__ = [
    "start_webhook_worker", "stop_webhook_worker",
    "_worker_tick", "_next_attempt_at", "_MAX_ATTEMPTS",
]
