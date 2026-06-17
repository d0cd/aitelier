-- aitelier storage schema v2: idempotency keys
--
-- Lets the SDK retry POSTs that have side effects (the agent path of
-- /v1/chat/completions with `aitelier.prepare.commands`, or /v1/runs)
-- without re-triggering them. The retry resends the same Idempotency-Key
-- header; if we've already executed it, return the stored response
-- instead of running the work again.
--
-- `body_hash` lets us detect "same key, different body" — a consumer bug
-- worth surfacing loud (HTTP 422) rather than silently treating as a new
-- request, which would defeat the dedup purpose.

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key              TEXT PRIMARY KEY,
    body_hash        TEXT NOT NULL,            -- sha256(request body bytes)
    endpoint         TEXT NOT NULL,            -- e.g. "/v1/chat/completions"
    status_code      INTEGER NOT NULL,
    response_json    JSONB NOT NULL,
    run_id           TEXT,                     -- link back to runs table when applicable
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL      -- 24h after created_at by default
);
CREATE INDEX IF NOT EXISTS idx_idempotency_expiry ON idempotency_keys(expires_at);

-- Version recorded by migrate(), not self-inserted here. See 001_initial.sql.
