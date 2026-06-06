"""Trace store — persists per-run summaries to SQLite for debugging."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from aitelier.config import get_config

_DB_NAME = "traces.db"


def _db_path() -> Path:
    return Path(get_config().runs_dir) / _DB_NAME


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            model TEXT,
            kind TEXT,
            finish_reason TEXT,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_usd REAL,
            system_prompt_hash TEXT,
            trace_tag TEXT,
            status TEXT,
            error_type TEXT,
            error_msg TEXT,
            metadata TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_traces_tag ON traces(trace_tag)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at)
    """)
    conn.commit()


def record_trace(
    trace_id: str,
    started_at: str,
    result: dict,
    system_prompt: str | None = None,
    trace_tag: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Record a trace from a result dict.

    Tool calls (when present in the result) are merged into the metadata JSON
    so post-hoc analysis can answer "what did the agent actually do?"
    """
    conn = _connect()
    try:
        usage = result.get("usage") or {}
        tool_calls = result.get("tool_calls") or []
        prompt_hash = (
            hashlib.sha256(system_prompt.encode()).hexdigest()[:16]
            if system_prompt else None
        )

        merged_metadata = dict(metadata or {})
        if tool_calls:
            merged_metadata["tool_calls"] = tool_calls

        conn.execute("""
            INSERT OR REPLACE INTO traces
            (trace_id, started_at, ended_at, model, kind, finish_reason,
             tool_call_count, input_tokens, output_tokens, total_tokens,
             cost_usd, system_prompt_hash, trace_tag, status,
             error_type, error_msg, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trace_id,
            started_at,
            datetime.now(UTC).isoformat(),
            result.get("provider"),
            result.get("kind"),
            result.get("finish_reason"),
            len(tool_calls),
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("total_tokens", 0),
            result.get("cost_usd"),
            prompt_hash,
            trace_tag,
            result.get("status"),
            result.get("error_type"),
            result.get("error_msg"),
            json.dumps(merged_metadata) if merged_metadata else None,
        ))
        conn.commit()
    finally:
        conn.close()


def recent_traces(
    *,
    since: str | None = None,
    trace_tag: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query recent traces with optional filters."""
    conn = _connect()
    try:
        query = "SELECT * FROM traces WHERE 1=1"
        params: list = []

        if since:
            query += " AND started_at >= ?"
            params.append(since)
        if trace_tag:
            query += " AND trace_tag = ?"
            params.append(trace_tag)
        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_trace(trace_id: str) -> dict | None:
    """Get a single trace by ID."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM traces WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


_GROUPBY_COLUMNS = {
    "trace_tag":   "COALESCE(trace_tag, '<none>')",
    "kind":        "COALESCE(kind, '<none>')",
    "model":       "COALESCE(model, '<none>')",
    "status":      "COALESCE(status, '<none>')",
    "error_type":  "COALESCE(error_type, '<none>')",
    # SQLite stores ISO-8601 timestamps; substr(0..10) is the YYYY-MM-DD date.
    "day":         "substr(started_at, 1, 10)",
}


def aggregate_traces(
    *,
    group_by: str = "trace_tag",
    since: str | None = None,
    until: str | None = None,
    trace_tag: str | None = None,
) -> dict:
    """Roll up trace stats by a column (trace_tag/kind/model/status/error_type/day).

    Returns {"groups": [{key, count, total_tokens, cost_usd, error_count}, ...],
             "total": {...}}.
    """
    if group_by not in _GROUPBY_COLUMNS:
        raise ValueError(
            f"group_by must be one of: {', '.join(sorted(_GROUPBY_COLUMNS))}"
        )
    expr = _GROUPBY_COLUMNS[group_by]

    where: list[str] = []
    params: list = []
    if since:
        where.append("started_at >= ?")
        params.append(since)
    if until:
        where.append("started_at <= ?")
        params.append(until)
    if trace_tag:
        where.append("trace_tag = ?")
        params.append(trace_tag)
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""

    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT
              {expr} AS key,
              COUNT(*) AS count,
              COALESCE(SUM(total_tokens), 0) AS total_tokens,
              COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
              SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count
            FROM traces{where_clause}
            GROUP BY key
            ORDER BY count DESC
            """,
            params,
        ).fetchall()

        total = conn.execute(
            f"""
            SELECT
              COUNT(*) AS count,
              COALESCE(SUM(total_tokens), 0) AS total_tokens,
              COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
              SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count
            FROM traces{where_clause}
            """,
            params,
        ).fetchone()

        return {
            "group_by": group_by,
            "groups": [dict(r) for r in rows],
            "total": dict(total) if total else {
                "count": 0, "total_tokens": 0, "cost_usd": 0.0, "error_count": 0,
            },
        }
    finally:
        conn.close()


def purge_traces(max_age_days: int = 30) -> int:
    """Delete traces older than max_age_days. Returns count deleted."""
    cutoff = datetime.now(UTC) - __import__("datetime").timedelta(days=max_age_days)
    cutoff_iso = cutoff.isoformat()
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM traces WHERE started_at < ?", (cutoff_iso,))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
