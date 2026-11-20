#!/bin/sh
# Least-privilege login for the shadow consumer: INSERT/SELECT on shadow_predictions only.
# Idempotent (make migrate runs it on an existing database).
set -e  # (no -u: the postgres entrypoint may source this file)
: "${SENTINEL_SHADOW_DB_PASSWORD:?SENTINEL_SHADOW_DB_PASSWORD must be set}"
psql -v ON_ERROR_STOP=1 -v shadow_pw="$SENTINEL_SHADOW_DB_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
SELECT 'CREATE ROLE sentinel_shadow LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_shadow') \gexec
ALTER ROLE sentinel_shadow LOGIN PASSWORD :'shadow_pw';
GRANT USAGE ON SCHEMA public TO sentinel_shadow;
GRANT INSERT, SELECT ON shadow_predictions TO sentinel_shadow;
SQL
