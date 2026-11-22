#!/bin/sh
# Least-privilege login for the rollout controller: reads lineage + gate decisions, appends
# rollout transitions. Idempotent (make migrate runs it on an existing database).
set -e  # (no -u: the postgres entrypoint may source this file)
: "${SENTINEL_ROLLOUT_DB_PASSWORD:?SENTINEL_ROLLOUT_DB_PASSWORD must be set}"
psql -v ON_ERROR_STOP=1 -v rollout_pw="$SENTINEL_ROLLOUT_DB_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
SELECT 'CREATE ROLE sentinel_rollout LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_rollout') \gexec
ALTER ROLE sentinel_rollout LOGIN PASSWORD :'rollout_pw';
GRANT USAGE ON SCHEMA public TO sentinel_rollout;
GRANT SELECT ON predictions, gate_decisions TO sentinel_rollout;
GRANT INSERT, SELECT ON rollout_transitions TO sentinel_rollout;
SQL
