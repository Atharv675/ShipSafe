#!/bin/sh
# Stage 8 least-privilege logins. Idempotent (make migrate runs it on an existing database).
#   sentinel_monitor   - alert worker: writes consumer lag, evaluates rules, appends alert
#                        transitions, runs the ops_metrics retention job (the only DELETE).
#   sentinel_dashboard - Streamlit: read-only.
set -e  # (no -u: the postgres entrypoint may source this file)
: "${SENTINEL_MONITOR_DB_PASSWORD:?SENTINEL_MONITOR_DB_PASSWORD must be set}"
: "${SENTINEL_DASHBOARD_DB_PASSWORD:?SENTINEL_DASHBOARD_DB_PASSWORD must be set}"
psql -v ON_ERROR_STOP=1 -v monitor_pw="$SENTINEL_MONITOR_DB_PASSWORD" \
     -v dashboard_pw="$SENTINEL_DASHBOARD_DB_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
SELECT 'CREATE ROLE sentinel_monitor LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_monitor') \gexec
ALTER ROLE sentinel_monitor LOGIN PASSWORD :'monitor_pw';
GRANT USAGE ON SCHEMA public TO sentinel_monitor;
GRANT SELECT, INSERT, DELETE ON ops_metrics TO sentinel_monitor;
GRANT SELECT, INSERT ON alerts TO sentinel_monitor;

SELECT 'CREATE ROLE sentinel_dashboard LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_dashboard') \gexec
ALTER ROLE sentinel_dashboard LOGIN PASSWORD :'dashboard_pw';
GRANT USAGE ON SCHEMA public TO sentinel_dashboard;
GRANT SELECT ON predictions, ops_metrics, alerts, rollout_transitions, gate_decisions,
    tenant_overrides TO sentinel_dashboard;
SQL
