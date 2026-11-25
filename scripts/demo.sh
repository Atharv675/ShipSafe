#!/usr/bin/env bash
# Stage 10: `make demo` - the whole release path on a small dataset, end to end, in minutes.
#
#   PDF splits (52,000 train / 12,000 test files) -> v1 + a retrained candidate (fewer trees)
#   -> isolated demo stack (API, PostgreSQL, MLflow, Redpanda, live + shadow consumers,
#   Jaeger, alert worker, dashboard) -> replay of the 12 test weeks -> release-gate decision
#   -> ML results -> every dashboard page filled.
#
# Isolation: compose project `sentinel-demo` (its own volumes and network, ports below), data
# in $SENTINEL_DATA_DIR/demo, configs/demo/ overlay. The real stack, registry and docs are
# never written. Every tool runs in the hardened sandbox, as everywhere else.
#   make demo        (re)build the demo from scratch      make demo-down    remove it
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1
t0=$(date +%s)
step() { printf '\n== [%4ss] %s\n' "$(( $(date +%s) - t0 ))" "$*"; }

# .env is read key by key (never sourced: the data path may contain spaces)
envval() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
[ -f .env ] || { echo "copy .env.example to .env first" >&2; exit 1; }
REAL="$(envval SENTINEL_DATA_DIR)"
DEMO="${DEMO_DATA_DIR:-$REAL/demo}"
mkdir -p "$DEMO"

# demo ports (the real stack keeps 5432 / 5000 / 18080 / 16686 / 8501)
export SENTINEL_PG_PORT=15432 SENTINEL_MLFLOW_PORT=15000 SENTINEL_API_PORT=18081
export SENTINEL_JAEGER_PORT=16687 SENTINEL_DASHBOARD_PORT=8502
export SENTINEL_CONFIG_OVERLAY=demo
M=(make -s OVERLAY=demo PROJECT=sentinel-demo FILE_TYPE=PDF
   "DATA_DIR=$DEMO" "SENTINEL_DATA_DIR=$DEMO")
SB=(docker run --rm --init --read-only --tmpfs /tmp:rw,nosuid,nodev,noexec,size=2g
    --cap-drop=ALL --security-opt=no-new-privileges --pids-limit=256 --memory=6g
    --memory-swap=6g --cpus=4 --user 10001:10001 --network=none
    -e SENTINEL_CONFIG_OVERLAY=demo -e GIT_PYTHON_REFRESH=quiet
    "--mount=type=bind,source=$PWD,target=/work,readonly"
    "--mount=type=bind,source=$DEMO,target=/data")
IMAGE=sentinel-sandbox:dev
WEEKS="53 54 55 56 57 58 59 60 61 62 63 64"

if [ "${1:-}" = "rollout" ]; then  # make demo-rollout: on the running demo stack
  step "register the Stage 6 BAD demo model (production with the default 0.5 cut-off)"
  "${M[@]}" demo-models DEMO_KINDS=bad | tail -1
  bad=$(docker compose -p sentinel-demo exec -T mlflow python -c "
from mlflow.tracking import MlflowClient
c = MlflowClient('sqlite:////data/mlflow/mlflow.db')
print(max(int(v.version) for v in c.search_model_versions(\"name='sentinel-pdf'\")))")
  bad=$(echo "$bad" | tr -d '\r' | tail -1)
  step "ring rollout of v$bad (demo: gate skipped, recorded) with live traffic replayed"
  "${M[@]}" rollout CANDIDATE="$bad" DEMO=1 > "$DEMO/rollout.log" 2>&1 &
  ro=$!
  sleep 20
  "${M[@]}" replay WEEKS="53 54 55 56 57 58" EPS=300 >/dev/null 2>&1 &
  rp=$!
  wait "$ro" || true
  kill "$rp" 2>/dev/null || true
  grep -E "ROLLED_BACK|COMPLETE|RING[0-2]" "$DEMO/rollout.log" | tail -5 | cut -c1-220
  step "done in $(( $(date +%s) - t0 )) s: see Rollout Timeline at http://127.0.0.1:$SENTINEL_DASHBOARD_PORT"
  exit 0
fi

step "fresh start: remove any earlier demo stack and demo data"
docker compose -p sentinel-demo down -v --remove-orphans >/dev/null 2>&1 || true
"${SB[@]}" "$IMAGE" sh -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} +'

step "data: PDF weekly splits + never-break set"
if [ -f "$REAL/splits/PDF/manifest.json" ] && [ -f "$REAL/clean/PDF/never_break.parquet" ]; then
  "${SB[@]}" "--mount=type=bind,source=$REAL,target=/real,readonly" "$IMAGE" sh -c \
    'mkdir -p /data/splits /data/clean && cp -r /real/splits/PDF /data/splits/ \
     && cp -r /real/clean/PDF /data/clean/'
else  # fresh machine: the only network step (official EMBER2024 PDF records, verified)
  "${M[@]}" download FILE_TYPES=PDF
  "${SB[@]}" "$IMAGE" python scripts/clean_data.py --file-types PDF --workers 4 \
    --manifest-dir /data/docs/cleaning --report /data/docs/CLEANING_REPORT.md
fi

step "infrastructure (PostgreSQL, Redpanda, Jaeger) starts in the background"
# shell env wins over .env for compose interpolation, so the demo data dir is used
SENTINEL_DATA_DIR="$DEMO" docker compose -p sentinel-demo up -d --wait postgres redpanda jaeger \
  > "$DEMO/infra.log" 2>&1 &
infra=$!

step "train in parallel (4 CPUs each): v1 (LightGBM + LR baseline) and the candidate (T=52)"
"${SB[@]}" "$IMAGE" python scripts/train_baseline.py --file-type PDF --out /data/runs/v1 \
  --threads 4 > "$DEMO/train_v1.log" 2>&1 &
v1=$!
"${SB[@]}" "$IMAGE" python scripts/retrain_candidate.py --file-type PDF \
  --out /data/runs/candidate --policy last_n_weeks --threads 4 > "$DEMO/train_candidate.log" 2>&1 &
cand=$!
wait "$v1" || { tail -20 "$DEMO/train_v1.log"; exit 1; }
wait "$cand" || { tail -20 "$DEMO/train_candidate.log"; exit 1; }
grep -h '"event": "train_done"\|"event": "candidate_trained"' "$DEMO"/train_*.log | cut -c1-160 || true

step "register: v1 -> alias production, candidate -> alias candidate"
"${SB[@]}" "$IMAGE" python scripts/register_model.py --bundle /data/runs/v1 \
  --models-docs /data/docs/models --baseline /data/docs/BASELINE.md
"${SB[@]}" "$IMAGE" python -c "
import zipfile, pathlib
src = pathlib.Path('/data/runs/candidate')
with zipfile.ZipFile('/data/runs/candidate.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for f in sorted(src.iterdir()):
        if f.is_file():
            z.write(f, f.name)
"
"${SB[@]}" "$IMAGE" python scripts/register_candidate.py --bundle /data/runs/candidate.zip

step "in parallel: ML results for the dashboards, and the rest of the demo stack"
(
  "${SB[@]}" "$IMAGE" python scripts/demo_export.py --v1-dir /data/runs/v1 \
    --candidate-dir /data/runs/candidate
  "${SB[@]}" "$IMAGE" python scripts/build_ml_results.py
  if [ -d "$REAL/results" ]; then  # the project's own Stage 7/7b results (Experiment page)
    "${SB[@]}" "--mount=type=bind,source=$REAL/results,target=/real,readonly" "$IMAGE" sh -c \
      'for f in /real/experiment* /real/backtest_pass*; do [ -f "$f" ] && cp "$f" /data/results/; done; true'
  fi
) > "$DEMO/results.log" 2>&1 &
results=$!
wait "$infra" || { cat "$DEMO/infra.log"; exit 1; }
"${M[@]}" up
"${M[@]}" rollout-init
wait "$results" || { tail -20 "$DEMO/results.log"; exit 1; }

step "replay the 12 PDF test weeks: live verdicts through the API + shadow scores"
"${M[@]}" replay WEEKS="$WEEKS"
"${M[@]}" replay-counts ARGS="wait --weeks $WEEKS --tables live shadow --timeout 900"

step "release gate: candidate v2 vs production v1 (demo policy: recall at 1% FPR)"
"${M[@]}" gate CANDIDATE=2 GATE_OUT=/data/gate || true   # any outcome is a valid demo result

step "done in $(( $(date +%s) - t0 )) s"
cat <<EOF
  dashboard  http://127.0.0.1:$SENTINEL_DASHBOARD_PORT   (Operations + ML pages)
  api        http://127.0.0.1:$SENTINEL_API_PORT
  mlflow     http://127.0.0.1:$SENTINEL_MLFLOW_PORT
  jaeger     http://127.0.0.1:$SENTINEL_JAEGER_PORT
  data       $DEMO
Remove everything with: make demo-down
EOF
