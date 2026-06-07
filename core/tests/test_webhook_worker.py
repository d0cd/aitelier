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
    """After 5 failed attempts the delivery is marked failed and won't retry."""
    store = await get_store()
    wid = await store.enqueue_webhook("https://example/", {})
    # Pre-bump attempts to 4 so the next failure exceeds the cap.
    store._webhooks[wid].attempts = 4

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
    """5th attempt → None (failed)."""
    assert wh._next_attempt_at(5) is None
    assert wh._next_attempt_at(6) is None
    assert wh._next_attempt_at(0) is not None
