-- Sentinel Gate schema (Stage 3). Runs once, when the postgres volume is first created.

-- Lineage: one row per scoring decision.
CREATE TABLE predictions (
    request_id             text PRIMARY KEY,
    file_id                text NOT NULL,
    tenant_id              text NOT NULL,
    model_version          text NOT NULL,          -- "<registered model>/<version>"
    feature_schema_version text NOT NULL,
    threshold              double precision NOT NULL,
    score                  double precision NOT NULL,
    verdict                text NOT NULL CHECK (verdict IN ('malicious', 'benign')),
    override_applied       boolean NOT NULL,
    ts                     timestamptz NOT NULL
);
CREATE INDEX predictions_file_id ON predictions (file_id);
CREATE INDEX predictions_tenant_ts ON predictions (tenant_id, ts);
CREATE INDEX predictions_model_ts ON predictions (model_version, ts);

-- Per-tenant allow/block lists, applied outside the model (overrides win).
CREATE TABLE tenant_overrides (
    tenant_id  text NOT NULL,
    file_id    text NOT NULL,
    action     text NOT NULL CHECK (action IN ('allow', 'block')),
    reason     text NOT NULL CHECK (length(reason) > 0),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, file_id)
);
