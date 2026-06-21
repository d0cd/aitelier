-- kind is a closed set {complete, embed, agent}; enforce it at the DB
-- boundary as defense in depth behind the app-level RunSpec validation.
--
-- NOT VALID: the constraint applies to every new/updated row but does NOT
-- scan existing rows, so the migration can't fail on pre-existing fuzz/test
-- data a deployment may carry. Going forward nothing invalid is writable.
-- Run a one-time purge + `VALIDATE CONSTRAINT chk_runs_kind` to also enforce
-- it over history once the bad rows are gone.
ALTER TABLE runs
  ADD CONSTRAINT chk_runs_kind CHECK (kind IN ('complete', 'embed', 'agent')) NOT VALID;
