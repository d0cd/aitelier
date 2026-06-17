"""Tests for the durable webhook delivery worker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aitelier import webhook_worker as wh
from aitelier.storage import get_store


def _mock_http_response(status_code: int, text: str = "ok"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


@pytest.fixture(autouse=True)
def _bypass_ssrf_check(monkeypatch):
    """Webhook-worker tests target delivery logic (retry, signing,
    state-machine transitions) rather than the SSRF guard's behavior.
    Force the guard to pass so the placeholder `https://example/` URLs
    these tests use don't get rejected for DNS-resolution failure."""
    async def _always_public(_url):
        return True
    monkeypatch.setattr("aitelier.webhook_worker.is_public_url", _always_public, raising=False)
    monkeypatch.setattr("aitelier.security.is_public_url", _always_public)


@pytest.mark.asyncio
async def test_worker_delivers_2xx_and_marks_delivered():
    store = await get_store()
    wid = await store.enqueue_webhook("https://example/", {"hello": "world"})

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_mock_http_response(200))

    async def fake_get_shared():
        return fake_client

    with patch("aitelier.providers.llm.get_shared_client",
                side_effect=fake_get_shared):
        await wh._worker_tick()

    # Should have been called once with the right body
    fake_client.post.assert_awaited_once()
    # Delivery state should be `delivered`
    assert store._webhooks[wid].state == "delivered"


@pytest.mark.asyncio
async def test_worker_schedules_retry_on_5xx():
    store = await get_store()
    wid = await store.enqueue_webhook("https://example/", {"foo": 1})

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_mock_http_response(500, "boom"))

    async def fake_get_shared():
        return fake_client

    with patch("aitelier.providers.llm.get_shared_client",
                side_effect=fake_get_shared):
        await wh._worker_tick()

    delivery = store._webhooks[wid]
    assert delivery.state == "pending"
    assert delivery.attempts == 1
    assert delivery.next_attempt_at is not None
    assert "HTTP 500" in (delivery.last_error or "")


@pytest.mark.asyncio
async def test_worker_marks_failed_after_max_attempts():
    """After the backoff schedule is exhausted (6th attempt) the delivery is
    marked failed and won't retry."""
    store = await get_store()
    wid = await store.enqueue_webhook("https://example/", {})
    # Pre-bump attempts to 5 (the last backoff entry, 1hr) so the next
    # failure becomes attempt 6 and exceeds the cap.
    store._webhooks[wid].attempts = 5

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_mock_http_response(500))

    async def fake_get_shared():
        return fake_client

    with patch("aitelier.providers.llm.get_shared_client",
                side_effect=fake_get_shared):
        await wh._worker_tick()

    assert store._webhooks[wid].state == "failed"


@pytest.mark.asyncio
async def test_worker_handles_network_error():
    store = await get_store()
    wid = await store.enqueue_webhook("https://example/", {})

    fake_client = MagicMock()
    fake_client.post = AsyncMock(side_effect=ConnectionError("refused"))

    async def fake_get_shared():
        return fake_client

    with patch("aitelier.providers.llm.get_shared_client",
                side_effect=fake_get_shared):
        await wh._worker_tick()

    delivery = store._webhooks[wid]
    assert delivery.state == "pending"
    assert "ConnectionError" in (delivery.last_error or "")


def test_next_attempt_at_cap():
    """The full backoff schedule is reachable: attempt 5 still gets the
    final (1hr) delay; only attempt 6+ gives up (None)."""
    assert wh._next_attempt_at(5) is not None   # 1hr delay is NOT dead
    assert wh._next_attempt_at(6) is None
    assert wh._next_attempt_at(7) is None
    assert wh._next_attempt_at(0) is not None


def test_next_attempt_at_backoff_schedule():
    """`attempts` is pre-incremented by claim, so the first failed delivery
    (attempts=1) must back off by _BACKOFF_SECONDS[0] (1s), not skip it.
    The last entry (1hr, at attempts=5) must be reachable too."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    # attempts=1 → first backoff (1s) … attempts=5 → fifth (1hr).
    for attempts, expected in ((1, 1), (2, 5), (3, 30), (4, 300), (5, 3600)):
        nxt = wh._next_attempt_at(attempts)
        assert nxt is not None
        delay = (nxt - now).total_seconds()
        assert abs(delay - expected) < 2, (attempts, delay, expected)


@pytest.mark.asyncio
async def test_claim_does_not_burn_attempt_then_record_counts_it():
    """A claim must not increment attempts (so a crash before delivery doesn't
    burn one); the attempt is counted when record_webhook_attempt runs."""
    from datetime import UTC, datetime, timedelta
    store = await get_store()
    wid = await store.enqueue_webhook("https://example/hook", {"x": 1}, run_id="r-burn")
    claimed = await store.claim_pending_webhooks()
    d = next(c for c in claimed if c.id == wid)
    # Claim sets a visibility timeout but does NOT count an attempt.
    assert d.attempts == 0
    assert d.next_attempt_at is not None and d.next_attempt_at > datetime.now(UTC)
    # Simulated crash: no record call → attempts stays 0 (reclaimable later).
    assert store._webhooks[wid].attempts == 0
    # A real recorded attempt counts.
    await store.record_webhook_attempt(
        wid, status_code=None, error="boom",
        next_attempt_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert store._webhooks[wid].attempts == 1
    assert store._webhooks[wid].state == "pending"
