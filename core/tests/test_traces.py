"""Tests for the trace store."""

from __future__ import annotations

from unittest.mock import patch

from aitelier.traces import get_trace, purge_traces, recent_traces, record_trace


def test_record_and_retrieve(tmp_path):
    db_path = tmp_path / "traces.db"

    with patch("aitelier.traces._db_path", return_value=db_path):
        record_trace(
            trace_id="test-123",
            started_at="2026-05-07T10:00:00Z",
            result={
                "kind": "complete",
                "provider": "claude-sonnet",
                "status": "ok",
                "finish_reason": "stop",
                "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                "cost_usd": 0.01,
                "tool_calls": [],
            },
            system_prompt="You are a helpful assistant",
            trace_tag="test-tag",
        )

        trace = get_trace("test-123")
        assert trace is not None
        assert trace["trace_id"] == "test-123"
        assert trace["model"] == "claude-sonnet"
        assert trace["total_tokens"] == 30
        assert trace["trace_tag"] == "test-tag"
        assert trace["system_prompt_hash"] is not None


def test_recent_traces_filter(tmp_path):
    db_path = tmp_path / "traces.db"

    with patch("aitelier.traces._db_path", return_value=db_path):
        for i in range(5):
            record_trace(
                trace_id=f"trace-{i}",
                started_at=f"2026-05-07T10:0{i}:00Z",
                result={
                    "kind": "complete",
                    "provider": "claude-sonnet",
                    "status": "ok" if i % 2 == 0 else "error",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                },
                trace_tag="batch-a" if i < 3 else "batch-b",
            )

        # Filter by tag
        results = recent_traces(trace_tag="batch-a")
        assert len(results) == 3

        # Filter by status
        results = recent_traces(status="error")
        assert len(results) == 2

        # Limit
        results = recent_traces(limit=2)
        assert len(results) == 2


def test_purge_traces_removes_old(tmp_path):
    db_path = tmp_path / "traces.db"

    with patch("aitelier.traces._db_path", return_value=db_path):
        # Insert an old trace (60 days ago) and a recent one
        record_trace(
            trace_id="old-trace",
            started_at="2026-03-01T10:00:00Z",
            result={
                "kind": "complete", "provider": "test",
                "status": "ok", "finish_reason": "stop",
            },
        )
        record_trace(
            trace_id="recent-trace",
            started_at="2026-05-06T10:00:00Z",
            result={
                "kind": "complete", "provider": "test",
                "status": "ok", "finish_reason": "stop",
            },
        )

        assert len(recent_traces()) == 2

        purge_traces(max_age_days=30)

        remaining = recent_traces()
        assert len(remaining) == 1
        assert remaining[0]["trace_id"] == "recent-trace"
