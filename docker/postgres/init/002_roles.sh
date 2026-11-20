#!/bin/sh
# Least-privilege login for the API: it may INSERT lineage rows and READ overrides, nothing
# else (no UPDATE/DELETE on predictions: lineage is append-only for the service).
set -e  # (no -u: the postgres entrypoint may source this file)
: "${SENTINEL_API_DB_PASSWORD:?SENTINEL_API_DB_PASSWORD must be set}"
psql -v ON_ERROR_STOP=1 -v api_pw="$SENTINEL_API_DB_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE sentinel_api LOGIN PASSWORD :'api_pw';
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO sentinel_api;
GRANT INSERT, SELECT ON predictions TO sentinel_api;
GRANT SELECT ON tenant_overrides TO sentinel_api;
SQL
