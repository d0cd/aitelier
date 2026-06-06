"""Tests for the `aitelier traces` CLI subcommand."""

from __future__ import annotations

import json
from unittest.mock import patch

from aitelier.cli import _cmd_traces


def _ns(**kwargs):
    """Construct a simple namespace-like object."""
    class _NS:
        pass
    ns = _NS()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def test_traces_list_empty(capsys):
    with patch("aitelier.traces.recent_traces", return_value=[]):
        _cmd_traces(_ns(trace_id=None, tag=None, status=None,
                        since=None, limit=20, json=False))
    out = capsys.readouterr().out
    assert "No traces match." in out


def test_traces_list_table(capsys):
    rows = [
        {"trace_id": "r1", "kind": "complete", "model": "claude-sonnet",
         "status": "ok", "total_tokens": 120, "trace_tag": "test"},
        {"trace_id": "r2", "kind": "agent", "model": "claude-code",
         "status": "error", "total_tokens": 50, "trace_tag": None},
    ]
    with patch("aitelier.traces.recent_traces", return_value=rows):
        _cmd_traces(_ns(trace_id=None, tag=None, status=None,
                        since=None, limit=20, json=False))
    out = capsys.readouterr().out
    assert "r1" in out
    assert "r2" in out
    assert "claude-sonnet" in out
    assert "claude-code" in out
    assert "test" in out  # tag visible


def test_traces_list_json(capsys):
    rows = [{"trace_id": "r1", "kind": "complete", "status": "ok"}]
    with patch("aitelier.traces.recent_traces", return_value=rows):
        _cmd_traces(_ns(trace_id=None, tag=None, status=None,
                        since=None, limit=20, json=True))
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == rows


def test_traces_passes_filters_to_recent_traces():
    captured = {}

    def fake_recent(**kwargs):
        captured.update(kwargs)
        return []

    with patch("aitelier.traces.recent_traces", side_effect=fake_recent):
        _cmd_traces(_ns(trace_id=None, tag="curator-daily",
                        status="error", since="2026-05-01T00:00:00Z",
                        limit=5, json=False))
    assert captured["trace_tag"] == "curator-daily"
    assert captured["status"] == "error"
    assert captured["since"] == "2026-05-01T00:00:00Z"
    assert captured["limit"] == 5


def test_traces_detail_view(capsys):
    trace = {
        "trace_id": "r-xyz", "started_at": "2026-05-12T10:00:00Z",
        "ended_at": "2026-05-12T10:00:05Z",
        "kind": "agent", "model": "claude-code",
        "status": "ok", "finish_reason": "completed",
        "tool_call_count": 3, "input_tokens": 100,
        "output_tokens": 50, "total_tokens": 150,
        "cost_usd": None, "trace_tag": "daily",
        "error_type": None, "error_msg": None,
        "metadata": '{"correlation_id": "abc-123"}',
    }
    with patch("aitelier.traces.get_trace", return_value=trace):
        _cmd_traces(_ns(trace_id="r-xyz", tag=None, status=None,
                        since=None, limit=20, json=False))
    out = capsys.readouterr().out
    assert "r-xyz" in out
    assert "claude-code" in out
    assert "correlation_id" in out
    assert "abc-123" in out


def test_traces_detail_not_found(capsys):
    with patch("aitelier.traces.get_trace", return_value=None):
        try:
            _cmd_traces(_ns(trace_id="missing", tag=None, status=None,
                            since=None, limit=20, json=False))
            failed = False
        except SystemExit as e:
            failed = (e.code != 0)
    assert failed
    err = capsys.readouterr().err
    assert "not found" in err.lower()
