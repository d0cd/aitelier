"""Tests for the `aitelier traces` CLI subcommand."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from aitelier.cli import _cmd_traces
from aitelier.storage._store import _store as _module_store_ref  # noqa: F401
from aitelier.storage.models import Run


def _ns(**kwargs):
    """Simple namespace-like object."""
    class _NS:
        pass
    ns = _NS()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def _seed_runs():
    """Populate the conftest-provided InMemoryStore directly (sync, no loop)."""
    from aitelier.storage._store import _store
    assert _store is not None
    now = datetime.now(UTC)
    _store._runs["r1"] = Run(
        run_id="r1", state="completed", kind="complete",
        started_at=now, ended_at=now, model="claude-sonnet",
        trace_tag="test", total_tokens=120, status="ok",
        finish_reason="stop",
    )
    _store._runs["r2"] = Run(
        run_id="r2", state="failed", kind="agent",
        started_at=now, ended_at=now, model="claude",
        total_tokens=50, status="error", finish_reason="error",
        error_type="Timeout", error_msg="boom",
    )


def test_traces_list_empty(capsys):
    _cmd_traces(_ns(trace_id=None, tag=None, status=None,
                    since=None, limit=20, json=False))
    out = capsys.readouterr().out
    assert "No traces match." in out


def test_traces_list_table(capsys):
    _seed_runs()
    _cmd_traces(_ns(trace_id=None, tag=None, status=None,
                    since=None, limit=20, json=False))
    out = capsys.readouterr().out
    assert "r1" in out
    assert "r2" in out
    assert "claude-sonnet" in out
    assert "test" in out


def test_traces_list_json(capsys):
    _seed_runs()
    _cmd_traces(_ns(trace_id=None, tag=None, status=None,
                    since=None, limit=20, json=True))
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, list)
    assert {p["trace_id"] for p in parsed} == {"r1", "r2"}


def test_traces_filter_by_status(capsys):
    _seed_runs()
    _cmd_traces(_ns(trace_id=None, tag=None, status="error",
                    since=None, limit=20, json=False))
    out = capsys.readouterr().out
    assert "r2" in out
    assert "r1" not in out


def test_traces_detail_view(capsys):
    _seed_runs()
    _cmd_traces(_ns(trace_id="r1", tag=None, status=None,
                    since=None, limit=20, json=False))
    out = capsys.readouterr().out
    assert "r1" in out
    assert "claude-sonnet" in out


def test_traces_detail_not_found(capsys):
    failed = False
    try:
        _cmd_traces(_ns(trace_id="missing", tag=None, status=None,
                        since=None, limit=20, json=False))
    except SystemExit as e:
        failed = (e.code != 0)
    assert failed
    err = capsys.readouterr().err
    assert "not found" in err.lower()
