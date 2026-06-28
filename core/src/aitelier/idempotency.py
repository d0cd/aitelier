"""Idempotency-Key handling — atomic check-then-act + cached response replay.

Endpoints that mutate state (`POST /v1/chat/completions`, `POST /v1/runs`)
accept an `Idempotency-Key` header. The handler flow is:

    idem = await check_idempotency(request, "/v1/chat/completions")
    if idem and idem.cached is not None:
        return render_from_cache(idem.cached)
    try:
        result = await do_work()
        await record_idempotency(idem, result)
        return result
    except BaseException:
        release_idempotency_ctx(idem)  # release the per-key lock
        raise

`check_idempotency` returns None when no header is present (no
idempotency in play), or an `IdempotencyContext` carrying either a
`cached` response (hit) or a held per-key lock (miss → caller owns the
run). The per-key lock makes the check-run-record sequence atomic
within a single aitelier process so two concurrent POSTs with the same
key can't both kick off independent runs.

This module is process-local. Cross-process aitelier deployments would
need a DB-level claim — `_store.py`'s `ON CONFLICT (key) DO NOTHING`
on the record path is the existing safety net.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request

from aitelier.storage import get_store

logger = logging.getLogger("aitelier")

IDEMPOTENCY_TTL = timedelta(hours=24)

# Cap the chunk buffer cached for stream-idempotency replay. A long
# agent run emitting megabyte-class deltas would otherwise persist a
# multi-MB JSONB row for 24h. When exceeded, the stream stays caching
# nothing — replay is best-effort, the consumer's first request still
# succeeds.
STREAM_IDEMPOTENCY_MAX_CHUNKS = 2000


@dataclass
class IdempotencyContext:
    """Carried between `check_idempotency` and `record_idempotency`. Cleaner
    than stashing fields on request.state where intervening middleware
    could shadow them.

    `_lock` is held when this context represents a fresh claim — the
    caller is the first to see this key, and the lock is released when
    `record_idempotency` writes the result (or via the explicit
    `release_idempotency_ctx` helper on the error path). Cached-hit
    contexts have `_lock = None`.
    """
    key: str
    body_hash: str
    endpoint: str
    cached: dict | None
    _lock: asyncio.Lock | None = None


_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{1,200}$")


# Per-key locks serialize the check-then-act window for `check_idempotency`.
# Without this, two parallel POSTs with the same Idempotency-Key both miss
# the cache, both kick off runs, and end up with different run_ids — the
# idempotency contract is broken. The lock makes the check + run + record
# sequence atomic *within a single aitelier process*; cross-process aitelier
# deployments would still need a DB-level claim (see _store.py's
# `ON CONFLICT (key) DO NOTHING` on the record path).
_IDEMPOTENCY_LOCKS: dict[str, asyncio.Lock] = {}
_IDEMPOTENCY_LOCKS_GUARD: asyncio.Lock | None = None


def _idempotency_locks_guard() -> asyncio.Lock:
    """Lazy-init guard for the locks dict. asyncio.Lock binds to the
    running loop at construction, so we can't make this a module-level
    constant — that'd bind to whatever loop existed at import time."""
    global _IDEMPOTENCY_LOCKS_GUARD
    if _IDEMPOTENCY_LOCKS_GUARD is None:
        _IDEMPOTENCY_LOCKS_GUARD = asyncio.Lock()
    return _IDEMPOTENCY_LOCKS_GUARD


async def _acquire_idempotency_lock(key: str) -> asyncio.Lock:
    async with _idempotency_locks_guard():
        lock = _IDEMPOTENCY_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _IDEMPOTENCY_LOCKS[key] = lock
    await lock.acquire()
    return lock


def _release_idempotency_lock(key: str, lock: asyncio.Lock) -> None:
    if lock.locked():
        lock.release()
    # GC entries that have no waiters left so the dict doesn't grow
    # unbounded under heavy key churn. asyncio.Lock has no public API
    # for "do you have waiters" — _waiters is the private deque; check
    # for emptiness defensively.
    waiters = getattr(lock, "_waiters", None)
    if (not lock.locked()) and (not waiters):
        _IDEMPOTENCY_LOCKS.pop(key, None)


async def check_idempotency(
    request: Request, endpoint: str,
) -> IdempotencyContext | None:
    """If the request carries `Idempotency-Key`, look up a prior response.

    Returns None if no header (no idempotency in play). Otherwise returns
    an `IdempotencyContext`: `.cached` is the prior response on hit,
    None on miss/expiry. Raises 422 if the same key was used for a
    different body — almost always a consumer bug.

    On miss, acquires a per-key asyncio lock and re-checks the cache
    under the lock. This makes the check-then-act window atomic within a
    single aitelier process. The lock is held by the returned context
    and released by `record_idempotency` (or `release_idempotency_ctx`
    on the error path).

    Keys are length-capped and charset-restricted at the boundary so a
    misbehaving (or hostile) client can't flood the `idempotency_keys`
    table with megabyte rows or inject control characters into error
    messages that echo the key back.
    """
    key = request.headers.get("Idempotency-Key")
    if not key:
        return None
    if not _IDEMPOTENCY_KEY_PATTERN.match(key):
        raise HTTPException(
            status_code=400,
            detail=(
                "Idempotency-Key must be 1–200 chars of "
                "[A-Za-z0-9._:-]. UUIDs work; opaque tokens with that "
                "charset work; arbitrary user input does not."
            ),
        )
    body_bytes = await request.body()
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    store = await get_store()

    rec = await store.get_idempotent(key)
    if rec is not None:
        _enforce_body_hash_match(key, rec.body_hash, body_hash)
        return IdempotencyContext(key, body_hash, endpoint,
                                  cached=rec.response, _lock=None)

    # Cache miss: acquire the per-key lock + re-check. If another
    # concurrent request claimed the slot while we were waiting, we'll
    # see their cached response here and return it without re-running.
    lock = await _acquire_idempotency_lock(key)
    try:
        rec = await store.get_idempotent(key)
        if rec is not None:
            _enforce_body_hash_match(key, rec.body_hash, body_hash)
            _release_idempotency_lock(key, lock)
            return IdempotencyContext(key, body_hash, endpoint,
                                      cached=rec.response, _lock=None)
        # Genuine miss + we hold the lock. The caller owns the run and
        # must call record_idempotency (or release_idempotency_ctx)
        # to release.
        return IdempotencyContext(key, body_hash, endpoint,
                                  cached=None, _lock=lock)
    except BaseException:
        _release_idempotency_lock(key, lock)
        raise


def _enforce_body_hash_match(key: str, stored: str, incoming: str) -> None:
    if stored != incoming:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Idempotency-Key {key!r} was already used for a different "
                f"request body. Use a fresh UUID for distinct requests."
            ),
        )


async def record_idempotency(
    ctx: IdempotencyContext | None, response: dict,
) -> None:
    """Persist the response under this Idempotency-Key. No-op if ctx is None.
    Best-effort on storage failure: we already have the response, and
    the lock release MUST happen regardless. Lock is released here on
    every path."""
    if ctx is None:
        return
    from aitelier.storage import IdempotencyRecord
    try:
        # Never cache error responses (timeout / 503 / rate-limit). Caching
        # would replay a transient failure under this key for the full TTL,
        # defeating retry recovery. Mirrors the streaming path's guard.
        if _is_error_response(response):
            return
        store = await get_store()
        await store.record_idempotent(IdempotencyRecord(
            key=ctx.key, body_hash=ctx.body_hash, endpoint=ctx.endpoint,
            status_code=200, response=response,
            run_id=response.get("run_id"),
            expires_at=datetime.now(UTC) + IDEMPOTENCY_TTL,
        ))
    except Exception as exc:
        logger.warning("Failed to record idempotency key %s: %s", ctx.key, exc)
    finally:
        if ctx._lock is not None:
            _release_idempotency_lock(ctx.key, ctx._lock)
            ctx._lock = None  # symmetric with release_idempotency_ctx


def _is_error_response(response: dict) -> bool:
    """An agent/LLM error envelope carries an `aitelier_status_code` >= 400
    and/or a top-level `error` block."""
    status = response.get("aitelier_status_code")
    if isinstance(status, int) and status >= 400:
        return True
    return "error" in response


def release_idempotency_ctx(ctx: IdempotencyContext | None) -> None:
    """Error-path lock release. Used when the caller can't reach
    record_idempotency because the run raised before producing a
    response. Safe to call multiple times."""
    if ctx is None or ctx._lock is None:
        return
    _release_idempotency_lock(ctx.key, ctx._lock)
    ctx._lock = None
