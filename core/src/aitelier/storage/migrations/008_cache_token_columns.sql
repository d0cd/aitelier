-- Prompt-cache token counts. claude (and other backends) report how many
-- input tokens were served from / written to the prompt cache. Cache-read
-- dominates warm-run volume and is priced far below fresh input, so these
-- are tracked first-class for cost estimation and cache-hit observability.
-- NULL = the backend reported no cache info (distinct from a real 0), so no
-- DEFAULT. IF NOT EXISTS keeps the migration idempotent on re-apply.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS cached_read_tokens  INTEGER;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS cached_write_tokens INTEGER;
