-- Stage 9: ML dashboard. Idempotent: safe to run again (make migrate).
-- Prediction Lookup shows the shadow scores of a file next to its live verdicts: the
-- read-only dashboard role may SELECT shadow_predictions (nothing else changes).
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_dashboard') THEN
        GRANT SELECT ON shadow_predictions TO sentinel_dashboard;
    END IF;
END
$$;
