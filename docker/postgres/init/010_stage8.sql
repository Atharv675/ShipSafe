-- Stage 8: observability. Idempotent: safe to run again (make migrate).
-- No Prometheus / Grafana: operational metrics live here, traces in Jaeger.

-- Every live verdict links to its trace in Jaeger (W3C trace id, 32 hex chars). NULL when
-- tracing is off or the row predates Stage 8.
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS trace_id text
    CHECK (trace_id ~ '^[0-9a-f]{32}$');

-- One row per (time bucket, service, metric, labels): count, sum and latency percentiles.
-- Writers flush every 10 s (bucket_seconds = 10); the daily retention job rolls rows older
-- than a day into 1-minute buckets (bucket_seconds = 60) and deletes rows older than 14 days.
-- Operational data, not an audit record: the retention job may delete it.
CREATE TABLE IF NOT EXISTS ops_metrics (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts_bucket      timestamptz NOT NULL,
    bucket_seconds integer NOT NULL CHECK (bucket_seconds > 0),
    service        text NOT NULL CHECK (length(service) > 0),
    metric         text NOT NULL CHECK (length(metric) > 0),
    labels         jsonb NOT NULL DEFAULT '{}'::jsonb,
    count          bigint NOT NULL CHECK (count >= 0),
    sum            double precision NOT NULL,
    p50            double precision,
    p95            double precision,
    p99            double precision
);
CREATE INDEX IF NOT EXISTS ops_metrics_service_metric_ts ON ops_metrics (service, metric, ts_bucket);
CREATE INDEX IF NOT EXISTS ops_metrics_ts ON ops_metrics (ts_bucket);

-- Alert state changes. Only transitions are stored: the current state of an alert is its
-- latest row. Append-only for every role, and de-duplicated by the database itself: a row
-- must change the state of its dedup_key, and the first row of a key must be FIRING.
CREATE TABLE IF NOT EXISTS alerts (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts        timestamptz NOT NULL DEFAULT now(),
    rule      text NOT NULL CHECK (length(rule) > 0),
    dedup_key text NOT NULL CHECK (length(dedup_key) > 0),
    labels    jsonb NOT NULL DEFAULT '{}'::jsonb,
    state     text NOT NULL CHECK (state IN ('FIRING', 'RESOLVED')),
    severity  text NOT NULL CHECK (length(severity) > 0),
    value     double precision,
    message   text NOT NULL CHECK (length(message) > 0)
);
CREATE INDEX IF NOT EXISTS alerts_key_id ON alerts (dedup_key, id DESC);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts (ts);

CREATE OR REPLACE FUNCTION sentinel_alert_dedup() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    last_state text;
BEGIN
    -- serialise writers of the same key so two workers cannot both insert FIRING
    PERFORM pg_advisory_xact_lock(hashtext('sentinel-alert:' || NEW.dedup_key));
    SELECT state INTO last_state FROM alerts WHERE dedup_key = NEW.dedup_key
        ORDER BY id DESC LIMIT 1;
    IF last_state IS NULL AND NEW.state <> 'FIRING' THEN
        RAISE EXCEPTION 'alert %: the first state must be FIRING', NEW.dedup_key;
    END IF;
    IF last_state = NEW.state THEN
        RAISE EXCEPTION 'alert % is already %', NEW.dedup_key, NEW.state;
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER alerts_dedup
    BEFORE INSERT ON alerts FOR EACH ROW EXECUTE FUNCTION sentinel_alert_dedup();
CREATE OR REPLACE TRIGGER alerts_no_update_delete
    BEFORE UPDATE OR DELETE ON alerts
    FOR EACH ROW EXECUTE FUNCTION sentinel_append_only();
CREATE OR REPLACE TRIGGER alerts_no_truncate
    BEFORE TRUNCATE ON alerts
    FOR EACH STATEMENT EXECUTE FUNCTION sentinel_append_only();

-- The API writes its own metrics; the rollout controller reads the same rows the alert
-- worker reads (Stage 6 health signals). sentinel_monitor / sentinel_dashboard: 011 script.
GRANT INSERT ON ops_metrics TO sentinel_api;
GRANT SELECT ON ops_metrics, alerts TO sentinel_rollout;
