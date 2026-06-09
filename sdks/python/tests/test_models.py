"""Tests for the SDK's control-plane Pydantic models."""

from __future__ import annotations

from aitelier_client._generated.models import (
    ActiveRuns,
    CancelAck,
    Run,
    RunEvent,
    Schedule,
    TraceRecord,
)


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


def test_run():
    run = Run(run_id="r-1", state="running", kind="agent",
              model="agent:claude")
    assert run.run_id == "r-1"
    assert run.environment == {}
    assert run.metadata == {}


def test_run_event():
    ev = RunEvent(run_id="r-1", seq=0, kind="started")
    assert ev.seq == 0


def test_schedule_round_trip():
    sched = Schedule(
        id="s-1", name="daily",
        task={"model": "agent:claude", "messages": []},
        interval_seconds=86400,
    )
    assert sched.task["model"] == "agent:claude"


def test_active_runs_and_cancel_ack():
    a = ActiveRuns(active=["r-1", "r-2"])
    assert len(a.active) == 2
    c = CancelAck(run_id="r-1", cancelled=True)
    assert c.cancelled is True
