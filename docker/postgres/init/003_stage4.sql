-- Stage 4: stream events. Idempotent: safe to run again (make migrate).

-- Live verdicts from the stream carry the event id; one row per (event, model version).
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS event_id text;
CREATE UNIQUE INDEX IF NOT EXISTS predictions_event_model
    ON predictions (event_id, model_version) WHERE event_id IS NOT NULL;

-- Shadow scores of the candidate model on the SAME events. Never affects any verdict.
CREATE TABLE IF NOT EXISTS shadow_predictions (
    event_id               text NOT NULL,
    model_version          text NOT NULL,          -- "<registered model>/<version>"
    file_id                text NOT NULL,
    tenant_id              text NOT NULL,
    ring                   text NOT NULL CHECK (ring IN ('ring0', 'ring1', 'ring2')),
    week                   integer NOT NULL,
    feature_schema_version text NOT NULL,
    threshold              double precision NOT NULL,
    score                  double precision NOT NULL,
    verdict                text NOT NULL CHECK (verdict IN ('malicious', 'benign')),
    ts                     timestamptz NOT NULL,
    PRIMARY KEY (event_id, model_version)
);
CREATE INDEX IF NOT EXISTS shadow_predictions_model_week ON shadow_predictions (model_version, week);
