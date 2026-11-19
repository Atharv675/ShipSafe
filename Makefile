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
	train gate up down dashboard demo

help:
	@echo Targets: setup lint test data download inspect lock sandbox train gate up down dashboard demo

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

test:
	$(SANDBOX) $(IMAGE) pytest --cov --cov-report=term-missing:skip-covered

# Stage 0: download + inspect feature records. Stage 1 extends this with cleaning/splits.
data: download inspect

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

train:
	@echo "train: implemented in Stage 2" && exit 1

gate:
	@echo "gate: implemented in Stage 5" && exit 1

up:
	@echo "up: implemented in Stage 3" && exit 1

down:
	@echo "down: implemented in Stage 3" && exit 1

dashboard:
	@echo "dashboard: implemented in Stage 8" && exit 1

demo:
	@echo "demo: implemented in Stage 10" && exit 1
