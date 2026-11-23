-- Stage 7: paired scoring during a rollout. Idempotent: safe to run again (make migrate).
-- In a ring that serves a model other than production, the API also scores the same file
-- with production and stores that score here. NULL when the ring serves production itself
-- (nothing to compare) or the reference score failed.
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS reference_version text;
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS reference_score double precision;
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS reference_verdict text
    CHECK (reference_verdict IN ('malicious', 'benign'));
