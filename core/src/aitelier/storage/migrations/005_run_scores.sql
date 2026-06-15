-- aitelier storage schema v5: scoring sink for bolt-on eval frameworks.
--
-- Aitelier already records per-call rows with model/tokens/cost/timing,
-- append-only event timelines, trace_tag grouping, and (as of v4) the
-- captured request_body + rendered_messages. The one missing piece for
-- eval frameworks (Langfuse, Phoenix, PromptFoo, internal tools) is a
-- write path back into aitelier: a grader produces a score for a run,
-- aitelier stores it next to the run, dashboards group by score.
--
-- One row = one (run, score_name, evaluator) triple. No uniqueness
-- constraint on (run_id, name, evaluator): re-grading is common — a
-- rubric is updated, a model grader is re-run on history, a human
-- second-passes a sampled subset. Consumers that want "latest score
-- per (run, name, evaluator)" use ORDER BY created_at DESC LIMIT 1;
-- consumers that want history just SELECT *.
--
-- value is REAL (double-precision-ish) — wide enough for normalized
-- 0..1 scores, count-based scores, latency-budget scores, anything
-- numeric. comment is free-form text the grader emits alongside.

CREATE TABLE IF NOT EXISTS run_scores (
    id          BIGSERIAL    PRIMARY KEY,
    run_id      TEXT         NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    name        TEXT         NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    evaluator   TEXT         NOT NULL,
    comment     TEXT,
    metadata    JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Lookup by run is the dominant access path (grader writes score by
-- run; dashboards read all scores for a run).
CREATE INDEX IF NOT EXISTS run_scores_run_id_idx ON run_scores(run_id);

-- Group-by-name aggregates ("avg helpfulness across runs in trace X")
-- benefit from a name-first index.
CREATE INDEX IF NOT EXISTS run_scores_name_idx ON run_scores(name);
