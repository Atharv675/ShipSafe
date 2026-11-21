#!/bin/sh
# Least-privilege login for the release gate: reads live + shadow scores, appends decisions.
# Idempotent (make migrate runs it on an existing database).
set -e  # (no -u: the postgres entrypoint may source this file)
: "${SENTINEL_GATE_DB_PASSWORD:?SENTINEL_GATE_DB_PASSWORD must be set}"
psql -v ON_ERROR_STOP=1 -v gate_pw="$SENTINEL_GATE_DB_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
SELECT 'CREATE ROLE sentinel_gate LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_gate') \gexec
ALTER ROLE sentinel_gate LOGIN PASSWORD :'gate_pw';
GRANT USAGE ON SCHEMA public TO sentinel_gate;
GRANT SELECT ON predictions, shadow_predictions TO sentinel_gate;
GRANT INSERT, SELECT ON gate_decisions TO sentinel_gate;
SQL
