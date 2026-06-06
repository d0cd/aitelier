"""Tests for generated Pydantic models."""

from __future__ import annotations

from aitelier_client._generated.models import Event, Result, TaskSpec, TraceRecord, Usage


def test_task_spec_complete():
    task = TaskSpec(name="test", kind="complete", prompt="Hello")
    assert task.kind == "complete"
    assert task.workspace_mode == "copy"


def test_task_spec_agent_with_mcp():
    task = TaskSpec(
        name="curator",
        kind="agent",
        model="claude-code",
        system_prompt="You are a curator",
        mcp_servers=[{"name": "deepread", "transport": "http", "url": "http://localhost:3001"}],
        tool_allowlist=["deepread.query_corpus", "deepread.fact_check"],
        max_turns=25,
    )
    assert task.mcp_servers is not None
    assert len(task.mcp_servers) == 1
    assert task.tool_allowlist == ["deepread.query_corpus", "deepread.fact_check"]


def test_result_complete():
    result = Result(
        kind="complete",
        provider="claude-sonnet",
        status="ok",
        duration_s=1.5,
        run_id="test-run",
        content="Hello!",
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        finish_reason="stop",
    )
    assert result.content == "Hello!"
    assert result.usage.total_tokens == 15


def test_result_embed():
    result = Result(
        kind="embed",
        provider="nomic-embed-text",
        status="ok",
        duration_s=0.2,
        run_id="test-run",
        embeddings=[[0.1, 0.2, 0.3]],
        dimensions=3,
    )
    assert result.embeddings is not None
    assert result.dimensions == 3


def test_result_agent():
    result = Result(
        kind="agent",
        provider="claude-code",
        status="ok",
        duration_s=30.0,
        run_id="test-run",
        content="Done. Created 3 files.",
        finish_reason="completed",
        tool_calls=[{"server": "deepread", "tool": "query_corpus", "elapsed_ms": 500}],
    )
    assert result.finish_reason == "completed"
    assert result.tool_calls is not None


def test_result_error():
    result = Result(
        kind="complete",
        provider="test",
        status="error",
        duration_s=0.5,
        run_id="test-run",
        error_type="ProviderUnavailable",
        error_msg="Connection refused",
    )
    assert result.error_type == "ProviderUnavailable"


def test_trace_record():
    trace = TraceRecord(
        trace_id="abc-123",
        started_at="2026-05-07T10:00:00Z",
        model="claude-sonnet",
        status="ok",
        total_tokens=100,
        tool_call_count=3,
    )
    assert trace.trace_id == "abc-123"
    assert trace.tool_call_count == 3


def test_event():
    event = Event(
        type="run.started",
        timestamp="2026-05-07T10:00:00Z",
        run_id="test-run",
    )
    assert event.type == "run.started"
