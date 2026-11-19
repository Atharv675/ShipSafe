# Progress

> **HANDOFF (2026-11-19)**
> - **Owner decisions:** Win64 + Win32 as separate datasets; all heavy runs on **Google Colab free** (~12.7 GB RAM, ephemeral disk); cleaned data is rebuilt each session (not stored).
> - **Stage 1 code done + verified locally:** pipeline, schema, rules, tests (70 passed, 97%), real-data smoke run on PDF in the sandbox (64 weeks, 52,000/12,000 rows, 690 features, 500 never-break, 2m15s).
> - **Next:** owner runs `notebooks/stage1_clean_win64_win32.ipynb` on Colab with a fresh `dist/sentinel_gate_code.zip` (`make bundle`). It returns `sentinel_stage1_results.zip`; copy `cleaning/Win64.json` + `cleaning/Win32.json` into `docs/cleaning/`, run `make cleaning-report`, update this file, then STOP.
> - **Before uploading anything to Kaggle/Colab:** the owner does it (outward-facing).

| Stage | Title | Status |
|---|---|---|
| 0 | Repo setup and dataset check [STOP] | DONE |
| 1 | Data cleaning and data contracts | IN PROGRESS (waiting for Colab run) |
| 2 | Baseline models and evaluation harness | TODO |
| 3 | Serving service | TODO |
| 4 | Replay stream and shadow scoring | TODO |
| 5 | The release gate | TODO |
| 6 | Ring rollout and automatic rollback [OPTIONAL] | TODO |
| 7 | The main experiment | TODO |
| 8 | Observability, alerts and load testing | TODO |
| 9 | ML dashboard (Streamlit) | TODO |
| 10 | Hardening, CI and handover [STOP] | TODO |

Owner rules: no git/GitHub commands by the assistant; wait for go-ahead after every stage; all tools run in the hardened Docker sandbox.

---

## Stage 0 — Repo setup and dataset check

### Plan
- Files: `pyproject.toml`, `uv.lock`, `Makefile`, `docker/sandbox.Dockerfile`, `.dockerignore`, `.gitignore`, `.env.example`, `.pre-commit-config.yaml`, `CLAUDE.md`, `configs/data.yaml`, `src/sentinel/{config,logging}.py`, `src/sentinel/data/{ember,report}.py`, empty subpackages, `scripts/{download_ember,inspect_dataset}.py`, `docs/{BUILD_PLAN,PROGRESS,DECISIONS,DATASET_REPORT}.md`.
- Tests: `tests/unit/test_smoke.py` (imports, config consistency, settings, JSON logging); `tests/unit/test_ember_inspect.py` (member-name parsing, timestamp parsing, per-week aggregation, synthetic-zip round trip, mismatch flags, recommendation, report rendering).

### Check results (interim, 2026-11-18)
- `make setup` (builds hardened sandbox image) — OK.
- `make lint` — `ruff check`: All checks passed; `ruff format --check`: 19 files already formatted; `mypy` (strict): Success, no issues in 14 source files.
- `make test` — 37 passed, coverage 97%.
- Sandbox verified: uid 10001, CapEff=0, NoNewPrivs=1, read-only rootfs + source, no DNS/network, no git/pip/uv in image.
- `make download` — PDF, ELF, APK, Dot_Net zips downloaded + SHA-256 verified; Win64/Win32 in progress.
- `make inspect` (PDF, ELF, APK, Dot_Net) — unique counts match published figures; raw JSONL has every record twice (see DATASET_REPORT.md). Final report pending Win64/Win32.

### Check results (final, 2026-11-19)
- Win64 + Win32 downloaded and inspected on Kaggle via `notebooks/stage0_inspect_win64_win32.ipynb` + `dist/sentinel_gate_code.zip`; SHA-256 of all four zips recorded in the report's provenance; zips deleted on Kaggle afterwards. Only counts came back (no file hashes).
- Unique counts match the published figures for all six types (52 train / 12 test weeks each, 50/50 labels).
- Benign test files: Win32 180,000; Win64 60,000; Dot_Net 30,000; APK 24,000; PDF 6,000; ELF 3,000. Only Win32 and Win64 reach 50,000.
- `make report`, `make lint` (ruff + mypy strict, 15 files) and `make test` (43 passed, 97%) green in the sandbox.

### Issues found and fixed (Stage 0)
- `thrember` import chain broke in a clean environment: oscrypto 1.3.0 vs OpenSSL 3.0.22, signify 0.9 removed `SignedPEFile`, LightGBM needs libgomp1. All fixed (DECISIONS.md).
- xet download error mid-file → retries + persistent chunk cache.

---

## Stage 1 — Data cleaning and data contracts

### Plan
- Config: `configs/clean.yaml` (seed, label values, non-finite fill, never-break size, late-label distribution, Parquet settings) → `CleanConfig` in `src/sentinel/config.py`.
- Rules: `src/sentinel/data/clean.py` (pure functions). Contract: `src/sentinel/data/schema.py` (pandera). Streaming pipeline: `src/sentinel/data/pipeline.py`. Readers + late-label filter: `src/sentinel/data/splits.py`. Report: `src/sentinel/data/clean_report.py`.
- Script `scripts/clean_data.py`; Make targets `data` (download + clean, `CLEAN_TYPES=Win64 Win32`), `clean-data`, `cleaning-report`.
- Colab notebook `notebooks/stage1_clean_win64_win32.ipynb` (owner runs it).
- Tests: `tests/unit/test_clean_rules.py` (one per rule, tiny inputs), `tests/unit/test_pipeline.py` (synthetic zips end to end: every rule counted, test-week row hashes never in train, re-run byte-identical with 1 vs 2 workers, schema accepts outputs and rejects bad rows, never-break benign/train-only, late-label filter).

### Check results (local, 2026-11-19)
- `make lint` — ruff + format clean; mypy strict: no issues in 20 source files.
- `make test` — 70 passed; coverage 97% total; `src/sentinel/data/*` 95–100% (pipeline 96%, schema 95%, clean 100%).
- Real-data smoke run (PDF, sandbox, 4 workers, 2m15s): 104,000→52,000 train and 24,000→12,000 test rows (raw repeats), 0 bad labels/timestamps/non-finite, 0 leakage, 0 duplicate vectors, 2,568→690 features, 500 never-break rows.
- Profiling one Dot_Net (PE) week: ~1.8 ms/file pass 1, ~0.6 ms/file pass 2 → Colab estimate ~25 min Win64, ~60–80 min Win32 (2 CPUs) plus downloads.
- Pending: Colab run for Win64 + Win32 (final counts, manifests, CLEANING_REPORT.md).

### Issues found and fixed (Stage 1)
- Determinism test was intermittently red: the synthetic test zips carried the wall-clock entry timestamp, so two runs built in different seconds had genuinely different raw bytes (different fingerprint). Fixed the test fixture with a fixed zip entry time; the pipeline itself was deterministic.
