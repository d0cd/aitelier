-- aitelier storage schema v3: parent_run_id passthrough field on runs.
--
-- Optional pointer from a child run to its parent for multi-agent
-- workflows. No FK on purpose — aitelier doesn't enforce hierarchy
-- semantics; the consumer (or an orchestrator above aitelier) owns
-- meaning. NULLABLE; safe to add to an existing populated table.

ALTER TABLE runs ADD COLUMN IF NOT EXISTS parent_run_id TEXT;

CREATE INDEX IF NOT EXISTS idx_runs_parent_run_id ON runs(parent_run_id);
