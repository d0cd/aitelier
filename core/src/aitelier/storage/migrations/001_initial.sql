-- aitelier storage schema v1
-- All tables created idempotently. The storage layer reads schema_version
-- and applies migrations 001..N in order; this file is migration 001.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- runs: every aitelier invocation that creates a trace
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    state               TEXT NOT NULL,    -- pending|running|completed|failed|cancelled|orphaned
    kind                TEXT NOT NULL,    -- complete|embed|agent
    agent_id            TEXT,             -- claude|codex|... (NULL for LLM)
    model               TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at            TIMESTAMPTZ,
    -- consumer-supplied
    trace_tag           TEXT,
    correlation_id      TEXT,
    -- environment snapshot at run start
    sandbox_backend     TEXT,             -- local|remote|null
    sandbox_url         TEXT,
    sandbox_server_id   TEXT,             -- ACP server_id for reconnect
    workspace           TEXT,
    environment_json    JSONB,
    -- result
    result_json         JSONB,
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    total_tokens        INTEGER DEFAULT 0,
    cost_usd            DOUBLE PRECISION,
    finish_reason       TEXT,
    tool_call_count     INTEGER DEFAULT 0,
    system_prompt_hash  TEXT,
    status              TEXT,             -- ok|error (derived; kept for query speed)
    error_type          TEXT,
    error_msg           TEXT,
    metadata_json       JSONB
);
CREATE INDEX IF NOT EXISTS idx_runs_state         ON runs(state);
CREATE INDEX IF NOT EXISTS idx_runs_trace_tag     ON runs(trace_tag);
CREATE INDEX IF NOT EXISTS idx_runs_started_at    ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_correlation   ON runs(correlation_id);

-- run_events: append-only timeline within a single run
CREATE TABLE IF NOT EXISTS run_events (
    event_id    BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL,    -- start|delta|tool_call|tool_result|finish|error|cancelled|orphaned
    payload_json JSONB,
    UNIQUE(run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_run         ON run_events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_ts          ON run_events(ts DESC);

-- schedules: persistent scheduled task registry
CREATE TABLE IF NOT EXISTS schedules (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    task_json       JSONB NOT NULL,
    interval_seconds INTEGER,
    at_iso          TIMESTAMPTZ,
    webhook_url     TEXT,
    next_run_at     TIMESTAMPTZ,
    last_run_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_schedules_next     ON schedules(next_run_at);

-- webhook_deliveries: durable record of webhook attempts + retries
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
    schedule_id     TEXT REFERENCES schedules(id) ON DELETE SET NULL,
    url             TEXT NOT NULL,
    payload_json    JSONB NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',  -- pending|delivered|failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_status_code INTEGER,
    last_error      TEXT,
    last_attempt_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_webhook_pending    ON webhook_deliveries(state, next_attempt_at)
    WHERE state = 'pending';

INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;
