# Sentinel Gate — task runner. Recipes are shell-agnostic (work with cmd.exe and sh).
.DEFAULT_GOAL := help
UV ?= uv
FILE_TYPES ?= PDF

.PHONY: help setup hooks lint test data inspect train gate up down dashboard demo

help:
	@echo Targets: setup lint test data inspect train gate up down dashboard demo

setup:
	$(UV) sync --all-groups

# Run once yourself after you create the git repo.
hooks:
	$(UV) run pre-commit install

lint:
	$(UV) run ruff check src tests scripts
	$(UV) run ruff format --check src tests scripts
	$(UV) run mypy

test:
	$(UV) run pytest --cov --cov-report=term-missing:skip-covered

# Stage 0: download + inspect feature records. Stage 1 extends this with cleaning/splits.
data:
	$(UV) run python scripts/download_ember.py --file-types $(FILE_TYPES)
	$(UV) run python scripts/inspect_dataset.py --file-types $(FILE_TYPES)

inspect:
	$(UV) run python scripts/inspect_dataset.py --file-types $(FILE_TYPES)

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
