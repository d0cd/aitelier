-- Token columns: NULL means "backend reported no usage" or "not yet
-- finalized" — distinct from a real 0. Drop the DEFAULT 0 so a freshly
-- created run starts NULL until finalize_run writes the measured value
-- (or NULL when the backend reported nothing). Idempotent: dropping an
-- absent default is a no-op in Postgres.
ALTER TABLE runs ALTER COLUMN input_tokens DROP DEFAULT;
ALTER TABLE runs ALTER COLUMN output_tokens DROP DEFAULT;
ALTER TABLE runs ALTER COLUMN total_tokens DROP DEFAULT;
