# Sentinel Gate — task runner.
# Every tool runs inside a hardened Docker sandbox (docker/sandbox.Dockerfile), never on the host:
#   non-root uid 10001, read-only root fs, read-only source mount, all capabilities dropped,
#   no-new-privileges, pid/memory/cpu limits, and NO network except for the download step.
.DEFAULT_GOAL := help
-include .env

IMAGE      ?= sentinel-sandbox:dev
FILE_TYPES ?= Win32 Win64 Dot_Net APK ELF PDF
DATA_DIR   ?= $(SENTINEL_DATA_DIR)
SRC        := $(CURDIR)

HARDEN = --rm --init --read-only \
	--tmpfs /tmp:rw,nosuid,nodev,noexec,size=2g \
	--cap-drop=ALL --security-opt=no-new-privileges \
	--pids-limit=256 --memory=6g --memory-swap=6g --cpus=4 \
	--user 10001:10001 \
	"--mount=type=bind,source=$(SRC),target=/work,readonly"
SANDBOX     = docker run $(HARDEN) --network=none
SANDBOX_NET = docker run $(HARDEN)
DATA_RO = "--mount=type=bind,source=$(DATA_DIR),target=/data,readonly"
DATA_RW = "--mount=type=bind,source=$(DATA_DIR),target=/data"
DOCS_RW = "--mount=type=bind,source=$(SRC)/docs,target=/work/docs"

.PHONY: help lock sandbox setup hooks format lint test data download inspect report bundle \
	clean-data cleaning-report train baseline-report train-smoke gate up down sample-request \
	test-integration migrate import-replay register-candidate replay replay-counts replay-check \
	gate-verify rollout-init demo-models rollout rollout-watch rollout-status kill-switch demo-reset \
	rollout-timeline experiment-import experiment v1-zip dashboard dashboard-check demo \
	load-bodies load-test load-report load-bench load-render profile-api db-outage-check \
	backtest-import-meta backtest-import backtest-pass1

help:
	@echo Targets: setup lint test test-integration data download inspect report clean-data cleaning-report bundle lock sandbox train gate up down sample-request dashboard demo

# Resolve dependencies into uv.lock (in a container; only uv.lock is written back).
lock:
	docker build -f docker/sandbox.Dockerfile --target uvbase -t sentinel-uvbase:dev .
	docker run --rm --cap-drop=ALL --security-opt=no-new-privileges \
		"--mount=type=bind,source=$(SRC),target=/build" sentinel-uvbase:dev uv lock

sandbox:
	docker build -f docker/sandbox.Dockerfile --target runtime -t $(IMAGE) .

setup: sandbox

# Run once yourself after you create the git repo (pre-commit hooks call `make lint`).
hooks:
	@echo "Install pre-commit on the host yourself (pipx install pre-commit) then: pre-commit install"

lint:
	$(SANDBOX) $(IMAGE) sh -c "ruff check src tests scripts loadtest && ruff format --check src tests scripts loadtest && mypy"

# Auto-fix formatting: only src/, tests/, scripts/ are mounted writable.
format:
	$(SANDBOX) "--mount=type=bind,source=$(SRC)/src,target=/work/src" \
		"--mount=type=bind,source=$(SRC)/tests,target=/work/tests" \
		"--mount=type=bind,source=$(SRC)/scripts,target=/work/scripts" \
		"--mount=type=bind,source=$(SRC)/loadtest,target=/work/loadtest" \
		$(IMAGE) sh -c "ruff check --fix src tests scripts loadtest; ruff format src tests scripts loadtest"

# Unit tests only; integration tests need the running stack (make up && make test-integration).
test:
	$(SANDBOX) $(IMAGE) pytest -m "not integration" --cov --cov-report=term-missing:skip-covered

# Stage 1: download the chosen file types, then clean them into weekly splits + manifest.json.
# Owner's choice: Win64 + Win32 (run on Colab; see notebooks/stage1_clean_win64_win32.ipynb).
CLEAN_TYPES ?= Win64 Win32
CLEAN_WORKERS ?= 4
data:
	$(MAKE) download FILE_TYPES="$(CLEAN_TYPES)"
	$(MAKE) clean-data CLEAN_TYPES="$(CLEAN_TYPES)"

# Writes only $(DATA_DIR)/{splits,clean} and docs/cleaning + docs/CLEANING_REPORT.md.
clean-data:
	$(SANDBOX) $(DATA_RW) $(DOCS_RW) $(IMAGE) python scripts/clean_data.py --file-types $(CLEAN_TYPES) --workers $(CLEAN_WORKERS)

cleaning-report:
	$(SANDBOX) $(DOCS_RW) $(IMAGE) python scripts/clean_data.py --report-only

# Only step with network access; writes only into $(DATA_DIR).
download:
	$(SANDBOX_NET) $(DATA_RW) -e HF_HOME=/data/.hf-home $(IMAGE) python scripts/download_ember.py --file-types $(FILE_TYPES)

inspect:
	$(SANDBOX) $(DATA_RO) $(DOCS_RW) $(IMAGE) python scripts/inspect_dataset.py --file-types $(FILE_TYPES)

# Rebuild docs/DATASET_REPORT.md from docs/dataset_stats/*.json (e.g. after adding Colab results).
report:
	$(SANDBOX) $(DOCS_RW) $(IMAGE) python scripts/inspect_dataset.py --report-only

# Code-only zip for Kaggle/Colab (no data, no .env, no .git): dist/sentinel_gate_code.zip
bundle:
	$(SANDBOX) "--mount=type=bind,source=$(SRC)/dist,target=/work/dist" $(IMAGE) python scripts/make_bundle.py

# Stage 2. Heavy training runs on Kaggle (notebooks/stage2_train.ipynb) and returns a run
# bundle zip; `make train BUNDLE=<path to zip>` registers it in the local MLflow registry
# ($(DATA_DIR)/mlflow) as v1 with alias `production` and re-renders docs/BASELINE.md.
BUNDLE ?=
train:
	@test -n "$(BUNDLE)" || (echo "usage: make train BUNDLE=<path to sentinel_stage2_<type>.zip>" && exit 1)
	$(SANDBOX) $(DATA_RW) $(DOCS_RW) "--mount=type=bind,source=$(BUNDLE),target=/in/bundle.zip,readonly" \
		$(IMAGE) python scripts/register_model.py --bundle /in/bundle.zip

baseline-report:
	$(SANDBOX) $(DOCS_RW) $(IMAGE) python scripts/register_model.py --report-only

# Local real-data smoke run of the full training code (small type, results kept out of docs/).
SMOKE_TYPE ?= PDF
train-smoke:
	$(SANDBOX) $(DATA_RW) $(IMAGE) python scripts/train_baseline.py --file-type $(SMOKE_TYPE) \
		--skip-manifest-check --out /data/smoke/models/$(SMOKE_TYPE) --threads 4

# Stage 5 release gate (stack running): e.g. make gate CANDIDATE=2 [FILE_TYPE=Win32]
# [GATE_WEEKS="53 54 55"]. Reads live + shadow scores as `sentinel_gate`, appends to
# gate_decisions, writes docs/gate/, and on PROMOTE moves the `candidate_approved` alias
# (needs the registry mounted writable).
# FILE_TYPE also picks the type for `make replay` and `make replay-counts`.
FILE_TYPE ?= Win64
CANDIDATE ?=
GATE_WEEKS ?=
gate: export SENTINEL_GATE_DSN = postgresql://sentinel_gate:$(SENTINEL_GATE_DB_PASSWORD)@postgres:5432/sentinel
gate:
	@test -n "$(CANDIDATE)" || (echo "usage: make gate CANDIDATE=<registered version>" && exit 1)
	$(SANDBOX_NET) --network=sentinel_default -e SENTINEL_GATE_DSN -e GIT_PYTHON_REFRESH=quiet \
		$(DATA_RW) $(DOCS_RW) $(IMAGE) python -m sentinel.gate.run --candidate $(CANDIDATE) \
		--file-type $(FILE_TYPE) $(if $(GATE_WEEKS),--weeks $(GATE_WEEKS))

# Re-hash every stored gate decision and compare with docs/gate/*.json (tamper check).
gate-verify: export SENTINEL_GATE_DSN = postgresql://sentinel_gate:$(SENTINEL_GATE_DB_PASSWORD)@postgres:5432/sentinel
gate-verify:
	$(SANDBOX_NET) --network=sentinel_default -e SENTINEL_GATE_DSN $(IMAGE) \
		python -m sentinel.gate.run verify

# Stage 3: local stack (api, postgres, mlflow, jaeger), hardened, ports on 127.0.0.1 only.
COMPOSE = docker compose
APP_IMAGE ?= sentinel-app:dev
up:
	docker build -f docker/sandbox.Dockerfile --target app -t $(APP_IMAGE) .
	$(COMPOSE) up -d --wait
	@echo "api: http://127.0.0.1:$${SENTINEL_API_PORT:-18080}  mlflow: http://127.0.0.1:5000  jaeger: http://127.0.0.1:16686  dashboard: http://127.0.0.1:8501"

down:
	$(COMPOSE) down

# Build real request bodies for curl from local EMBER2024 feature records (no binaries).
SAMPLE_TYPE ?= Win64
SAMPLE_SOURCE ?= Dot_Net
sample-request:
	$(SANDBOX) $(DATA_RW) "--mount=type=bind,source=$(SRC)/docs/models,target=/work/docs/models,readonly" \
		$(IMAGE) python scripts/make_sample_request.py --file-type $(SAMPLE_TYPE) --source $(SAMPLE_SOURCE)

# Integration tests against the running stack (make up first): joins the compose network.
# DSNs travel through the environment (-e NAME, no value) so make never echoes the secrets.
test-integration: export SENTINEL_IT_ADMIN_DSN = postgresql://sentinel_admin:$(SENTINEL_PG_ADMIN_PASSWORD)@postgres:5432/sentinel
test-integration: export SENTINEL_IT_API_DSN = postgresql://sentinel_api:$(SENTINEL_API_DB_PASSWORD)@postgres:5432/sentinel
test-integration: export SENTINEL_IT_GATE_DSN = postgresql://sentinel_gate:$(SENTINEL_GATE_DB_PASSWORD)@postgres:5432/sentinel
test-integration: export SENTINEL_IT_ROLLOUT_DSN = postgresql://sentinel_rollout:$(SENTINEL_ROLLOUT_DB_PASSWORD)@postgres:5432/sentinel
test-integration: export SENTINEL_IT_MONITOR_DSN = postgresql://sentinel_monitor:$(SENTINEL_MONITOR_DB_PASSWORD)@postgres:5432/sentinel
test-integration: export SENTINEL_IT_DASHBOARD_DSN = postgresql://sentinel_dashboard:$(SENTINEL_DASHBOARD_DB_PASSWORD)@postgres:5432/sentinel
test-integration:
	$(SANDBOX_NET) --network=sentinel_default -e GIT_PYTHON_REFRESH=quiet \
		-e SENTINEL_API_URL=http://api:8000 -e SENTINEL_IT_ADMIN_DSN -e SENTINEL_IT_API_DSN \
		-e SENTINEL_IT_GATE_DSN -e SENTINEL_IT_ROLLOUT_DSN -e SENTINEL_IT_MONITOR_DSN \
		-e SENTINEL_IT_DASHBOARD_DSN \
		$(DATA_RO) $(IMAGE) pytest -m integration --no-cov tests/integration

# Stage 8 Streamlit app (also started by `make up`): http://127.0.0.1:8501
dashboard:
	$(COMPOSE) up -d --wait dashboard
	@echo "dashboard: http://127.0.0.1:8501  jaeger: http://127.0.0.1:16686"

# Render every Operations page against the live database and print what it shows.
MINUTES ?= 15
dashboard-check: export SENTINEL_DASHBOARD_DSN = postgresql://sentinel_dashboard:$(SENTINEL_DASHBOARD_DB_PASSWORD)@postgres:5432/sentinel
dashboard-check:
	@$(SANDBOX_NET) --network=sentinel_default -e SENTINEL_DASHBOARD_DSN -e HOME=/tmp $(IMAGE) \
		python scripts/dashboard_check.py --minutes $(MINUTES)

demo:
	@echo "demo: implemented in Stage 10" && exit 1

# ---------------------------------------------------------------- Stage 4: replay + shadow
# Apply the idempotent Stage 4 schema + shadow role to an existing database.
migrate:
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sentinel_admin -d sentinel \
		-f /docker-entrypoint-initdb.d/003_stage4.sql
	$(COMPOSE) exec -T postgres sh /docker-entrypoint-initdb.d/004_shadow_role.sh
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sentinel_admin -d sentinel \
		-f /docker-entrypoint-initdb.d/005_stage5.sql
	$(COMPOSE) exec -T postgres sh /docker-entrypoint-initdb.d/006_gate_role.sh
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sentinel_admin -d sentinel \
		-f /docker-entrypoint-initdb.d/007_stage6.sql
	$(COMPOSE) exec -T postgres sh /docker-entrypoint-initdb.d/008_rollout_role.sh
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sentinel_admin -d sentinel \
		-f /docker-entrypoint-initdb.d/009_stage7.sql
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sentinel_admin -d sentinel \
		-f /docker-entrypoint-initdb.d/010_stage8.sql
	$(COMPOSE) exec -T postgres sh /docker-entrypoint-initdb.d/011_monitor_roles.sh

# Verify + install a Kaggle replay export (test weeks + never-break set).
ZIP ?=
import-replay:
	@test -n "$(ZIP)" || (echo "usage: make import-replay ZIP=<path to sentinel_stage4_replay_<type>.zip>" && exit 1)
	$(SANDBOX) $(DATA_RW) "--mount=type=bind,source=$(ZIP),target=/in/replay.zip,readonly" \
		$(IMAGE) python scripts/import_replay.py --zip /in/replay.zip

# Register a Kaggle candidate bundle under the `candidate` alias (production untouched).
register-candidate:
	@test -n "$(BUNDLE)" || (echo "usage: make register-candidate BUNDLE=<path to sentinel_stage4_candidate_<type>.zip>" && exit 1)
	$(SANDBOX) $(DATA_RW) "--mount=type=bind,source=$(BUNDLE),target=/in/candidate.zip,readonly" \
		-e GIT_PYTHON_REFRESH=quiet $(IMAGE) python scripts/register_candidate.py --bundle /in/candidate.zip

# Publish weeks to file-events (e.g. make replay WEEKS="53 54"); optional EPS=events/second.
WEEKS ?= 53 54
EPS ?=
replay:
	$(COMPOSE) --profile tools run --rm producer --weeks $(WEEKS) --file-type $(FILE_TYPE) \
		$(if $(EPS),--events-per-second $(EPS))

# Counts for replayed weeks (sandbox on the compose network), e.g. ARGS="snapshot --weeks 53".
ARGS ?= snapshot --weeks 53 54
replay-counts: export SENTINEL_IT_ADMIN_DSN = postgresql://sentinel_admin:$(SENTINEL_PG_ADMIN_PASSWORD)@postgres:5432/sentinel
replay-counts:
	@$(SANDBOX_NET) --network=sentinel_default -e SENTINEL_IT_ADMIN_DSN $(DATA_RO) $(IMAGE) \
		python scripts/replay_counts.py $(ARGS) --file-type $(FILE_TYPE)

# The three Stage 4 checks end to end (2-week replay, kill shadow mid-run, replay twice).
replay-check:
	bash scripts/replay_check.sh

# ---------------------------------------------------------------- Stage 6: ring rollout
# The controller runs in the sandbox on the compose network as `sentinel_rollout`; it moves
# registry aliases through the MLflow server and reads the API's metrics.
ROLLOUT_RUN = $(SANDBOX_NET) --network=sentinel_default -e SENTINEL_ROLLOUT_DSN \
	-e SENTINEL_MLFLOW_URI=http://mlflow:5000 -e MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR=false \
	-e GIT_PYTHON_REFRESH=quiet -e HOME=/tmp $(DATA_RO)
ROLLOUT_DSN = postgresql://sentinel_rollout:$(SENTINEL_ROLLOUT_DB_PASSWORD)@postgres:5432/sentinel
DEMO ?=
NO_CANARIES ?=
REASON ?= manual kill switch
ROLLOUT_ID ?=
OUT ?=

# Create any missing ring aliases (ring0_model, ring1_model, ring2_model) at production.
rollout-init: export SENTINEL_ROLLOUT_DSN = $(ROLLOUT_DSN)
rollout-init:
	$(ROLLOUT_RUN) $(IMAGE) python -m sentinel.rollout.run init

# Register the two Stage 6 demo versions (GOOD copy / BAD 0.5 cut-off) of production.
DEMO_KINDS ?= good bad
demo-models:
	$(SANDBOX) $(DATA_RW) -e GIT_PYTHON_REFRESH=quiet $(IMAGE) python scripts/register_demo_models.py \
		--file-type $(FILE_TYPE) --kinds $(DEMO_KINDS)

# Start a rollout and watch it to COMPLETE or ROLLED_BACK, e.g.
#   make rollout CANDIDATE=3 [FILE_TYPE=Win64] [DEMO=1]
rollout: export SENTINEL_ROLLOUT_DSN = $(ROLLOUT_DSN)
rollout:
	@test -n "$(CANDIDATE)" || (echo "usage: make rollout CANDIDATE=<version> [DEMO=1]" && exit 1)
	$(ROLLOUT_RUN) $(IMAGE) python -m sentinel.rollout.run start --file-type $(FILE_TYPE) \
		--candidate $(CANDIDATE) $(if $(DEMO),--demo) $(if $(NO_CANARIES),--no-canaries) --watch

rollout-watch: export SENTINEL_ROLLOUT_DSN = $(ROLLOUT_DSN)
rollout-watch:
	$(ROLLOUT_RUN) $(IMAGE) python -m sentinel.rollout.run watch --file-type $(FILE_TYPE)

rollout-status: export SENTINEL_ROLLOUT_DSN = $(ROLLOUT_DSN)
rollout-status:
	@$(ROLLOUT_RUN) $(IMAGE) python -m sentinel.rollout.run status --file-type $(FILE_TYPE)

# After a completed DEMO rollout: production + ring aliases back to the demo's source version
# (refuses unless production is a version tagged demo=true).
demo-reset:
	$(ROLLOUT_RUN) $(IMAGE) python -m sentinel.rollout.run demo-reset --file-type $(FILE_TYPE)

# Kill switch through the API (POST /admin/rollback), timed until every ring serves production.
kill-switch: export SENTINEL_ADMIN_API_KEY := $(SENTINEL_ADMIN_API_KEY)
kill-switch:
	@$(ROLLOUT_RUN) -e SENTINEL_ADMIN_API_KEY $(IMAGE) python -m sentinel.rollout.run kill \
		--reason "$(REASON)"

# Write a rollout's transitions as a Markdown table, e.g. OUT=docs/rollout/bad.md
rollout-timeline: export SENTINEL_ROLLOUT_DSN = $(ROLLOUT_DSN)
rollout-timeline:
	@test -n "$(OUT)" || (echo "usage: make rollout-timeline OUT=docs/rollout/<name>.md" && exit 1)
	$(ROLLOUT_RUN) $(DOCS_RW) $(IMAGE) python -m sentinel.rollout.run timeline \
		--file-type $(FILE_TYPE) $(if $(ROLLOUT_ID),--rollout-id $(ROLLOUT_ID)) --out /work/$(OUT)

# ---------------------------------------------------------------- Stage 7: main experiment
# Kaggle runs notebooks/stage7_experiment.ipynb (candidates + score export); locally:
#   make experiment-import ZIP=<path to sentinel_stage7_<type>_<part>.zip>   (once per zip)
#   make experiment [RESAMPLES=50]   -> data/results/experiment.parquet, docs/RESULTS.md, MLflow
RESAMPLES ?=
experiment-import:
	@test -n "$(ZIP)" || (echo "usage: make experiment-import ZIP=<path to sentinel_stage7_*.zip>" && exit 1)
	$(SANDBOX) $(DATA_RW) "--mount=type=bind,source=$(ZIP),target=/in/$(notdir $(ZIP)),readonly" \
		$(IMAGE) python scripts/run_experiment.py import --zip /in/$(notdir $(ZIP))

experiment:
	$(SANDBOX) $(DATA_RW) $(DOCS_RW) -e GIT_PYTHON_REFRESH=quiet -e HOME=/tmp $(IMAGE) \
		python scripts/run_experiment.py run $(if $(RESAMPLES),--resamples $(RESAMPLES))

# The v1 bundle the Kaggle experiment starts from: dist/sentinel_v1_<type>.zip (model files only).
v1-zip:
	$(SANDBOX) $(DATA_RO) "--mount=type=bind,source=$(SRC)/dist,target=/work/dist" $(IMAGE) \
		python scripts/pack_v1.py --file-type $(FILE_TYPE)

# ---------------------------------------------------------------- Stage 8: load test
# make load-bodies                          real Win64 feature records -> $(DATA_DIR)/loadtest
# make load-test RUN=before [USERS=500 DURATION=5m]   Locust, headless, compose network
# make load-report RUN=before LABEL="..."   summary.json (Locust CSV + server ops_metrics)
# make load-bench                           metrics-recorder cost per request
# make load-render                          docs/LOAD_TEST.md from every stored summary
USERS ?= 500
SPAWN ?= 100
DURATION ?= 5m
RUN ?= run
LABEL ?= $(RUN)
LOAD_DSN = postgresql://sentinel_dashboard:$(SENTINEL_DASHBOARD_DB_PASSWORD)@postgres:5432/sentinel
load-bodies:
	$(SANDBOX) $(DATA_RW) $(IMAGE) python scripts/make_load_bodies.py --file-type Win64 --week 53 --n 2000

load-test:
	$(SANDBOX_NET) --network=sentinel_default $(DATA_RW) -e HOME=/tmp $(IMAGE) sh -c \
		"mkdir -p /data/loadtest/$(RUN) && locust -f loadtest/locustfile.py --headless \
		-u $(USERS) -r $(SPAWN) -t $(DURATION) --host http://api:8000 \
		--csv /data/loadtest/$(RUN)/locust --csv-full-history --only-summary --stop-timeout 10"

load-report: export SENTINEL_DASHBOARD_DSN = $(LOAD_DSN)
load-report:
	@$(SANDBOX_NET) --network=sentinel_default $(DATA_RW) -e SENTINEL_DASHBOARD_DSN $(IMAGE) \
		python scripts/load_report.py run --run /data/loadtest/$(RUN) --label "$(LABEL)" --users $(USERS)

load-bench:
	$(SANDBOX) $(DATA_RW) $(IMAGE) python scripts/load_report.py bench

load-render:
	$(SANDBOX) $(DATA_RO) $(DOCS_RW) $(IMAGE) python scripts/load_report.py render --out docs/LOAD_TEST.md

# Where a /v1/score request spends its time (real production models, in-process, no DB).
profile-api:
	$(SANDBOX_NET) --network=sentinel_default $(DATA_RO) -e SENTINEL_MLFLOW_URI=http://mlflow:5000 \
		-e MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR=false -e GIT_PYTHON_REFRESH=quiet -e HOME=/tmp \
		$(IMAGE) python scripts/profile_api.py --n 1000

# With PostgreSQL stopped the API keeps answering; afterwards lineage writing resumes.
db-outage-check:
	bash scripts/db_outage_check.sh

# ---------------------------------------------------------------- Stage 7b: backtest
# Kaggle: notebooks/stage7b_metadata.ipynb (once) + notebooks/stage7b_candidates.ipynb (x3).
#   make backtest-import-meta ZIP=<sentinel_stage7b_meta_Win64.zip>
#   make backtest-import ZIP=<sentinel_stage7b_Win64_<policy>.zip>     (once per zip)
#   make backtest-pass1 [RESAMPLES=200]   -> data/results/backtest_pass1_*.parquet, docs/BACKTEST.md
backtest-import-meta:
	@test -n "$(ZIP)" || (echo "usage: make backtest-import-meta ZIP=<zip>" && exit 1)
	$(SANDBOX) $(DATA_RW) "--mount=type=bind,source=$(ZIP),target=/in/$(notdir $(ZIP)),readonly" \
		$(IMAGE) python scripts/run_backtest.py import-meta --zip /in/$(notdir $(ZIP))

backtest-import:
	@test -n "$(ZIP)" || (echo "usage: make backtest-import ZIP=<zip>" && exit 1)
	$(SANDBOX) $(DATA_RW) "--mount=type=bind,source=$(ZIP),target=/in/$(notdir $(ZIP)),readonly" \
		$(IMAGE) python scripts/run_backtest.py import --zip /in/$(notdir $(ZIP))

backtest-pass1:
	$(SANDBOX) $(DATA_RW) $(DOCS_RW) -e GIT_PYTHON_REFRESH=quiet -e HOME=/tmp $(IMAGE) \
		python scripts/run_backtest.py pass1 $(if $(RESAMPLES),--resamples $(RESAMPLES))
