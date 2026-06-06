"""SSE streaming utilities for the aitelier Python SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator


async def stream_events(event_iter: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """Process and yield SSE events, handling event types."""
    async for event in event_iter:
        event_type = event.get("type", "")
        yield event
        if event_type in ("run.completed", "run.error"):
            return
