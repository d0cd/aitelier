"""Offline concurrency test for the idempotency per-key lock.

The whole point of `idempotency.py`'s per-key asyncio lock is that N concurrent
requests with the same Idempotency-Key collapse to a single run. The rest of
the idempotency suite only exercises sequential behavior (cache hit, 422 on body
mismatch, expiry); this exercises the lock under genuine concurrency — and the
GC path that drains `_IDEMPOTENCY_LOCKS` — without any live infra.
"""

from __future__ import annotations

import asyncio

import pytest
from aitelier.idempotency import (
    _IDEMPOTENCY_LOCKS,
    check_idempotency,
    record_idempotency,
)


class _FakeRequest:
    """Minimal stand-in: check_idempotency only reads the header and body."""

    def __init__(self, key: str, body: bytes):
        self.headers = {"Idempotency-Key": key}
        self._body = body

    async def body(self) -> bytes:
        return self._body


@pytest.mark.asyncio
async def test_concurrent_same_key_collapses_to_single_run():
    """8 concurrent requests with one key → exactly one does fresh work; the
    other 7 replay its cached response (same run_id)."""
    key = "idem-concurrency-collapse"
    body = b'{"model":"x","messages":[]}'

    async def handler(i: int):
        ctx = await check_idempotency(_FakeRequest(key, body), "/v1/chat/completions")
        if ctx and ctx.cached is not None:
            return ("cached", ctx.cached["run_id"])
        # Genuine miss — we hold the lock and own the run.
        response = {"run_id": f"run-{i}", "object": "chat.completion"}
        await record_idempotency(ctx, response)
        return ("fresh", response["run_id"])

    results = await asyncio.gather(*(handler(i) for i in range(8)))

    fresh = [r for r in results if r[0] == "fresh"]
    cached = [r for r in results if r[0] == "cached"]
    assert len(fresh) == 1, results
    assert len(cached) == 7
    winner_run_id = fresh[0][1]
    assert all(run_id == winner_run_id for _, run_id in cached)
    # GC path: the lock dict is drained once all holders release.
    assert key not in _IDEMPOTENCY_LOCKS


@pytest.mark.asyncio
async def test_concurrent_distinct_keys_each_run_independently():
    """Different keys must not serialize against each other — each is its own
    fresh run."""
    body = b"{}"

    async def handler(i: int):
        ctx = await check_idempotency(
            _FakeRequest(f"idem-distinct-{i}", body), "/v1/chat/completions"
        )
        assert ctx is not None and ctx.cached is None
        await record_idempotency(ctx, {"run_id": f"r-{i}"})
        return f"r-{i}"

    results = await asyncio.gather(*(handler(i) for i in range(5)))
    assert sorted(results) == [f"r-{i}" for i in range(5)]
    for i in range(5):
        assert f"idem-distinct-{i}" not in _IDEMPOTENCY_LOCKS
