-- Stage 5: release-gate decisions. Idempotent: safe to run again (make migrate).
-- Append-only: triggers block UPDATE, DELETE and TRUNCATE for every role, the owner included.

CREATE TABLE IF NOT EXISTS gate_decisions (
    decision_id        text PRIMARY KEY,
    created_at         timestamptz NOT NULL DEFAULT now(),
    file_type          text NOT NULL,
    policy_version     text NOT NULL,
    production_version text NOT NULL,          -- "<registered model>/<version>"
    candidate_version  text NOT NULL,
    decision           text NOT NULL CHECK (decision IN ('PROMOTE', 'REJECT', 'INSUFFICIENT_EVIDENCE')),
    reason             text NOT NULL CHECK (length(reason) > 0),
    bundle_sha256      text NOT NULL CHECK (bundle_sha256 ~ '^[0-9a-f]{64}$'),
    bundle_json        text NOT NULL,
    -- the stored hash must be the hash of the stored bundle (checked by the database itself)
    CONSTRAINT gate_decisions_hash_matches
        CHECK (bundle_sha256 = encode(sha256(convert_to(bundle_json, 'UTF8')), 'hex'))
);
CREATE INDEX IF NOT EXISTS gate_decisions_candidate ON gate_decisions (candidate_version, created_at);

CREATE OR REPLACE FUNCTION sentinel_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only: % is not allowed', TG_TABLE_NAME, TG_OP;
END
$$;

CREATE OR REPLACE TRIGGER gate_decisions_no_update_delete
    BEFORE UPDATE OR DELETE ON gate_decisions
    FOR EACH ROW EXECUTE FUNCTION sentinel_append_only();
CREATE OR REPLACE TRIGGER gate_decisions_no_truncate
    BEFORE TRUNCATE ON gate_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION sentinel_append_only();
