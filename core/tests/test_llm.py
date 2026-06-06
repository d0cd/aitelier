"""Tests for providers/llm.py — the LiteLLM-facing client primitives."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aitelier.providers.llm import complete_stream


def _fake_stream_response(lines: list[str]):
    """Build a MagicMock that emulates httpx.AsyncClient.stream(...) context manager."""

    class _StreamCM:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for line in lines:
                yield line

    return _StreamCM()


@pytest.mark.asyncio
async def test_complete_stream_yields_deltas_and_done(monkeypatch):
    """Parse a typical LiteLLM SSE response: two delta chunks, then finish, then usage."""
    lines = [
        'data: {"choices":[{"delta":{"content":"Hello"},"index":0,"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":" world"},"index":0,"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}]}',
        'data: {"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}',
        "data: [DONE]",
    ]

    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=_fake_stream_response(lines))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr("aitelier.providers.llm.httpx.AsyncClient",
                        lambda **kw: fake_client)

    events = []
    async for ev in complete_stream(
        model="claude-sonnet",
        messages=[{"role": "user", "content": "hi"}],
        timeout=10,
        run_id="r-1",
    ):
        events.append(ev)

    deltas = [e for e in events if e["type"] == "delta"]
    done = [e for e in events if e["type"] == "done"]

    assert [d["content"] for d in deltas] == ["Hello", " world"]
    assert len(done) == 1
    assert done[0]["content"] == "Hello world"
    assert done[0]["finish_reason"] == "stop"
    assert done[0]["usage"]["total_tokens"] == 7
    assert done[0]["run_id"] == "r-1"


@pytest.mark.asyncio
async def test_complete_stream_skips_malformed_lines(monkeypatch):
    """A garbage SSE line should be ignored, not crash the stream."""
    lines = [
        "data: not valid json",
        'data: {"choices":[{"delta":{"content":"ok"},"index":0,"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]

    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=_fake_stream_response(lines))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr("aitelier.providers.llm.httpx.AsyncClient",
                        lambda **kw: fake_client)

    events = []
    async for ev in complete_stream(
        model="claude-sonnet",
        messages=[{"role": "user", "content": "hi"}],
        timeout=10,
    ):
        events.append(ev)

    deltas = [e for e in events if e["type"] == "delta"]
    assert [d["content"] for d in deltas] == ["ok"]


@pytest.mark.asyncio
async def test_complete_stream_handles_empty_response(monkeypatch):
    """Stream that emits only [DONE] still produces a terminal 'done' event."""
    lines = ["data: [DONE]"]

    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=_fake_stream_response(lines))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr("aitelier.providers.llm.httpx.AsyncClient",
                        lambda **kw: fake_client)

    events = []
    async for ev in complete_stream(
        model="local",
        messages=[{"role": "user", "content": "hi"}],
        timeout=10,
    ):
        events.append(ev)

    assert len(events) == 1
    assert events[0]["type"] == "done"
    assert events[0]["content"] == ""
    assert events[0]["finish_reason"] == "stop"  # default
