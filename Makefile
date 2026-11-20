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
	dashboard demo

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
	$(SANDBOX) $(IMAGE) sh -c "ruff check src tests scripts && ruff format --check src tests scripts && mypy"

# Auto-fix formatting: only src/, tests/, scripts/ are mounted writable.
format:
	$(SANDBOX) "--mount=type=bind,source=$(SRC)/src,target=/work/src" \
		"--mount=type=bind,source=$(SRC)/tests,target=/work/tests" \
		"--mount=type=bind,source=$(SRC)/scripts,target=/work/scripts" \
		$(IMAGE) sh -c "ruff check --fix src tests scripts; ruff format src tests scripts"

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

gate:
	@echo "gate: implemented in Stage 5" && exit 1

# Stage 3: local stack (api, postgres, mlflow, jaeger), hardened, ports on 127.0.0.1 only.
COMPOSE = docker compose
APP_IMAGE ?= sentinel-app:dev
up:
	docker build -f docker/sandbox.Dockerfile --target app -t $(APP_IMAGE) .
	$(COMPOSE) up -d --wait
	@echo "api: http://127.0.0.1:$${SENTINEL_API_PORT:-18080}  mlflow: http://127.0.0.1:5000  jaeger: http://127.0.0.1:16686"

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
test-integration:
	$(SANDBOX_NET) --network=sentinel_default -e GIT_PYTHON_REFRESH=quiet \
		-e SENTINEL_API_URL=http://api:8000 -e SENTINEL_IT_ADMIN_DSN -e SENTINEL_IT_API_DSN \
		$(DATA_RO) $(IMAGE) pytest -m integration --no-cov tests/integration

dashboard:
	@echo "dashboard: implemented in Stage 8" && exit 1

demo:
	@echo "demo: implemented in Stage 10" && exit 1

# ---------------------------------------------------------------- Stage 4: replay + shadow
# Apply the idempotent Stage 4 schema + shadow role to an existing database.
migrate:
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U sentinel_admin -d sentinel \
		-f /docker-entrypoint-initdb.d/003_stage4.sql
	$(COMPOSE) exec -T postgres sh /docker-entrypoint-initdb.d/004_shadow_role.sh

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
	$(COMPOSE) --profile tools run --rm producer --weeks $(WEEKS) $(if $(EPS),--events-per-second $(EPS))

# Counts for replayed weeks (sandbox on the compose network), e.g. ARGS="snapshot --weeks 53".
ARGS ?= snapshot --weeks 53 54
replay-counts: export SENTINEL_IT_ADMIN_DSN = postgresql://sentinel_admin:$(SENTINEL_PG_ADMIN_PASSWORD)@postgres:5432/sentinel
replay-counts:
	@$(SANDBOX_NET) --network=sentinel_default -e SENTINEL_IT_ADMIN_DSN $(DATA_RO) $(IMAGE) \
		python scripts/replay_counts.py $(ARGS)

# The three Stage 4 checks end to end (2-week replay, kill shadow mid-run, replay twice).
replay-check:
	bash scripts/replay_check.sh
