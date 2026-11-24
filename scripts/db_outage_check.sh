#!/usr/bin/env bash
# Stage 8 check: with PostgreSQL stopped, the API still answers every request (lineage and
# ops_metrics rows are dropped and counted); after it is back, writing resumes by itself.
#   make db-outage-check     (the stack must be up; postgres is ALWAYS started again)
set -euo pipefail
cd "$(dirname "$0")/.."
# read single keys (values may contain spaces, so .env is not sourced; nothing is printed)
envval() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }
SENTINEL_PG_ADMIN_PASSWORD="$(envval SENTINEL_PG_ADMIN_PASSWORD)"
SENTINEL_DATA_DIR="$(envval SENTINEL_DATA_DIR)"
export MSYS_NO_PATHCONV=1
ADMIN_DSN="postgresql://sentinel_admin:${SENTINEL_PG_ADMIN_PASSWORD}@postgres:5432/sentinel"
probe() {
  SENTINEL_IT_ADMIN_DSN="$ADMIN_DSN" docker run --rm --init --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m --cap-drop=ALL \
    --security-opt=no-new-privileges --user 10001:10001 \
    "--mount=type=bind,source=$(pwd),target=/work,readonly" \
    "--mount=type=bind,source=${SENTINEL_DATA_DIR},target=/data,readonly" \
    --network=sentinel_default -e SENTINEL_IT_ADMIN_DSN \
    sentinel-sandbox:dev python scripts/db_outage_probe.py "$@"
}
trap 'docker compose start postgres >/dev/null 2>&1 || true' EXIT
echo "== stopping postgres"
docker compose stop postgres
probe --phase down --n 200
echo "== starting postgres"
docker compose start postgres
docker compose up -d --wait postgres >/dev/null
sleep 10   # let the API's writers reconnect on their next flush
probe --phase up --n 50
