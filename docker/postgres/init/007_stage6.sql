-- Stage 6: ring rollout. Idempotent: safe to run again (make migrate).

-- Which ring served each live verdict (computed by the API from tenant_id). NULL on older rows.
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS ring text
    CHECK (ring IN ('ring0', 'ring1', 'ring2'));
CREATE INDEX IF NOT EXISTS predictions_model_ring_ts ON predictions (model_version, ring, ts);

-- Every state change of every rollout, with its reason and the signals behind it.
-- The current state of a file type is its latest row. Append-only for every role.
CREATE TABLE IF NOT EXISTS rollout_transitions (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rollout_id         text NOT NULL,
    seq                integer NOT NULL CHECK (seq >= 1),
    file_type          text NOT NULL,
    model_name         text NOT NULL,
    candidate_version  text NOT NULL,
    production_version text NOT NULL,
    from_state         text CHECK (from_state IN
                           ('APPROVED', 'RING0', 'RING1', 'RING2', 'COMPLETE', 'ROLLED_BACK')),
    to_state           text NOT NULL CHECK (to_state IN
                           ('APPROVED', 'RING0', 'RING1', 'RING2', 'COMPLETE', 'ROLLED_BACK')),
    actor              text NOT NULL CHECK (length(actor) > 0),
    reason             text NOT NULL CHECK (length(reason) > 0),
    signals            jsonb NOT NULL DEFAULT '{}'::jsonb,
    ts                 timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rollout_id, seq),
    CONSTRAINT rollout_first_row_has_no_from CHECK ((seq = 1) = (from_state IS NULL)),
    CONSTRAINT rollout_starts_approved CHECK (seq > 1 OR to_state = 'APPROVED')
);
CREATE INDEX IF NOT EXISTS rollout_transitions_type_id ON rollout_transitions (file_type, id DESC);

CREATE OR REPLACE TRIGGER rollout_transitions_no_update_delete
    BEFORE UPDATE OR DELETE ON rollout_transitions
    FOR EACH ROW EXECUTE FUNCTION sentinel_append_only();
CREATE OR REPLACE TRIGGER rollout_transitions_no_truncate
    BEFORE TRUNCATE ON rollout_transitions
    FOR EACH STATEMENT EXECUTE FUNCTION sentinel_append_only();

-- The API's kill switch records ROLLED_BACK itself (it never changes a past row).
GRANT INSERT, SELECT ON rollout_transitions TO sentinel_api;
