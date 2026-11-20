#!/usr/bin/env bash
# Stage 4 end-to-end checks on the running stack (make up, import-replay, register-candidate).
#   1. two-week replay (weeks 53-54): live and shadow tables end with the same event counts
#   2. shadow consumer KILLED mid-replay (week 55): live keeps going with no gaps; shadow
#      catches up after restart, without duplicates
#   3. a week replayed twice (week 53): no duplicate rows anywhere
# Output: one JSON line per step (copied into docs/PROGRESS.md).
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

counts() { make -s replay-counts ARGS="$*"; }
lag_zero() {  # wait until a consumer group has consumed everything
  for _ in $(seq 1 300); do
    lag=$(docker compose exec -T redpanda rpk group describe "$1" 2>/dev/null \
          | awk '/^TOTAL-LAG/ {print $2}')
    [ "${lag:-x}" = "0" ] && return 0
    sleep 2
  done
  echo "consumer group $1 never caught up" >&2; return 1
}

echo "== 1. two-week replay (53, 54)"
make -s replay WEEKS="53 54"
counts wait --weeks 53 54 --tables live shadow --timeout 1800

echo "== 2. kill the shadow consumer mid-replay (55)"
make -s replay WEEKS="55" EPS=200 &
producer=$!
sleep 15
docker compose kill shadow-consumer
wait "$producer"
counts wait --weeks 55 --tables live --timeout 900
echo "-- shadow while stopped (expected: behind):"
counts snapshot --weeks 55
docker compose start shadow-consumer
counts wait --weeks 55 --tables live shadow --timeout 900

echo "== 3. replay week 53 again"
make -s replay WEEKS="53"
lag_zero live-scorer
lag_zero shadow-scorer
counts snapshot --weeks 53
