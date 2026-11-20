# Progress

> **HANDOFF (2026-11-19)**
> - **Owner decisions:** Win64 + Win32 as separate datasets. Heavy runs moved from Colab free (VM recycled after long runs, results lost) to **Kaggle CPU notebooks** (4 CPUs, ~34 GB RAM, background "Save & Run All"). Cleaned data is rebuilt each session (not stored); manifests prove it.
> - **Stage 1 DONE (2026-11-19).** Win64 + Win32 cleaned on Kaggle (~50 min total); manifests in `docs/cleaning/`, report `docs/CLEANING_REPORT.md`.
> - **Stage 2 DONE (2026-11-20):** v1 LightGBM trained on Kaggle for both datasets, registered as `sentinel-win64` / `sentinel-win32` (alias production) in `$SENTINEL_DATA_DIR/mlflow`; golden test covers both (95 passed). Next: Stage 3 after owner go-ahead. History: two Kaggle runs of `notebooks/stage2_train.ipynb` (FILE_TYPE=Win64, then Win32) with the new `dist/sentinel_gate_code.zip`. For each returned `sentinel_stage2_<type>.zip`: `make train BUNDLE=<path>` (registers v1/production, writes docs/models/<type>/ + docs/BASELINE.md), then write `tests/golden/v1_metrics.json` from the real metrics, run lint/test, STOP.
> - **For Stage 2:** train with `sentinel.data.splits.labelled_as_of`; each Kaggle session first rebuilds splits with `scripts/clean_data.py` and checks per-week sha256 against `docs/cleaning/<type>.json`. Win32 train ≈ 1.56M × 2536 float32 ≈ 16 GB: fits in Kaggle's 34 GB, but load per week / LightGBM Dataset bins. Notebook platform check: detect Kaggle FIRST (Kaggle images contain `google.colab` and `/content`).
> - **Before uploading anything to Kaggle/Colab:** the owner does it (outward-facing).

| Stage | Title | Status |
|---|---|---|
| 0 | Repo setup and dataset check [STOP] | DONE |
| 1 | Data cleaning and data contracts | DONE (awaiting owner go-ahead) |
| 2 | Baseline models and evaluation harness | DONE (awaiting owner go-ahead) |
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

### Check results (final, Kaggle run 2026-11-19)
- `make data` equivalent (`download_ember.py` + `clean_data.py`) ran on Kaggle CPU (4 workers) from `dist/sentinel_gate_code.zip`: Win64 cleaned in 12.6 min, whole run ~50 min. Raw zip SHA-256 identical to Stage 0.
- `manifest.json` written for both types (copies in `docs/cleaning/`); `docs/CLEANING_REPORT.md` rebuilt in the sandbox (`make cleaning-report`).
- Win64: train 519,991 / test 119,990 (benign test 59,990); 2,538 / 2,568 features; removed 9 duplicate vectors in train (1 label conflict), 7 cross-week repeats + 3 test-vector-in-train in test; 13 early-dated rows kept; 500 never-break.
- Win32: train 1,559,966 / test 359,986 (benign test 179,987); 2,536 / 2,568 features; removed 34 duplicate vectors in train (0 conflicts), 6 cross-week repeats + 5 test-vector-in-train + 3 duplicate vectors in test; 23 early-dated rows kept; 500 never-break.
- Both: 52 train weeks (01–52), 12 test weeks (53–64); 0 bad labels, 0 bad timestamps, 0 non-finite values; every step's before − after = removed (checked in the sandbox).
- `make lint` clean; `make test` 70 passed, 97% coverage (data package ≥ 95%).
- Provenance note: the bundle records commit `63babe6`, but it was built from the Stage 1 working tree before that code was committed; `BUNDLE_INFO.json` per-file sha256s identify the exact code.

### Notebook issues found and fixed (Stage 1)
- Colab free recycled the VM after a long run → results lost: results zip is now handed back after each type.
- Platform detection: Colab images contain `/kaggle`, Kaggle images contain `google.colab` + `/content` → results were written to a discarded folder. Now Kaggle is detected first (env markers / `/kaggle/input`) and results are always also copied to `/kaggle/working`.
- Bulk data is never written under `/kaggle/working` (Kaggle's 20 GB output cap); cleaned splits are deleted after their manifest is saved.

### Issues found and fixed (Stage 1)
- Determinism test was intermittently red: the synthetic test zips carried the wall-clock entry timestamp, so two runs built in different seconds had genuinely different raw bytes (different fingerprint). Fixed the test fixture with a fixed zip entry time; the pipeline itself was deterministic.

---

## Stage 2 — Baseline models and evaluation harness

### Plan
- Config `configs/train.yaml` (K=51, 4 calibration weeks, eval week 52, as-of week 52, target FPR 0.1%, LightGBM params from the thrember grid, LR settings, MLflow names) → `TrainConfig`.
- `src/sentinel/models/evaluate.py` (pure): `threshold_at_fpr`, `rates_at_threshold`, `recall_at_fpr`, `wilson_interval`, `auc`, `auprc`, `reliability_curve`, `expected_calibration_error`, `per_week_metrics`.
- `src/sentinel/models/{calibration,logreg,train,registry,baseline_report}.py`; `src/sentinel/data/splits.py` gains `read_matrix` (preallocated float32, late-label filter) and `compare_manifests`.
- Scripts: `scripts/train_baseline.py` (Kaggle), `scripts/register_model.py` (local). Make: `train BUNDLE=...`, `baseline-report`, `train-smoke`.
- Notebook `notebooks/stage2_train.ipynb` (one dataset per run).
- Tests: `tests/unit/test_evaluate.py` (thresholds incl. ties, Wilson vs textbook, AUC/AUPRC vs sklearn, ECE, per-week), `tests/unit/test_train.py` (synthetic splits through the real Stage 1 pipeline: windows refuse test weeks, bundle + metrics, late labels, determinism, matrix reader, manifest comparison, MLflow register → load by alias → predict, report, safe extraction); golden `tests/golden/v1_metrics.json` once real metrics exist.

### Check results (local, 2026-11-19)
- `make lint` — ruff + format clean; mypy strict: no issues in 26 source files.
- `make test` — 91 passed, coverage 98% (models/train 98%, baseline_report 97%, registry covered by the MLflow test). 1 warning from inside MLflow's SQLAlchemy store (deprecation, not our code).
- `make train-smoke` (real PDF splits, full config, 4 CPUs): LightGBM 1000 trees in 357 s, LR 39 s; week 52 recall 98.2% (LR 78.8%) at measured FPR 0.4% (2/500 benign: PDF too small, as Stage 0 said); 0 never-break flags; calibration weeks had 1,561 of 4,000 rows not yet labelled at as-of time.
- Registered that real bundle into a throwaway registry: `sentinel-pdf` v1 → alias `production`, loads by alias as PyFuncModel; BASELINE.md rendered.
- `make bundle` → `dist/sentinel_gate_code.zip` (40 files, includes `docs/cleaning/*.json`).

### Check results (Kaggle, Win64, 2026-11-19)
- Kaggle CPU run of `notebooks/stage2_train.ipynb` (FILE_TYPE=Win64). `rebuild_check.json`: "identical content" vs `docs/cleaning/Win64.json`. Result zip sha256 `2ec58838e6e9b778b176a48b8a2d959601c07e711d7daf9a518d8c2587e9b9e3`.
- Fit 469,991 rows (weeks 1-47, 0 unlabelled); calibration 24,782 rows (12,342 benign; 15,218 not yet labelled at as-of); LR on a 200,000-row seeded subsample. LightGBM 2,634 s, LR 103 s (LightGBM 4.7.0, Python 3.11.18).
- Week 52, LightGBM v1: recall **89.70%** [88.83, 90.51] at measured FPR **0.36%** [0.23, 0.57] (18/5,000); AUC 0.9982; ECE 0.0041. LR: recall 59.74% at FPR 0.08%. Gap +29.96 points, intervals do not overlap. 0/500 never-break flags (both models).
- Test weeks 53-64 (report only): LightGBM recall 92.06%, FPR 0.197% (118/59,990).
- Observation: the threshold hits 0.097% FPR on calibration weeks but 0.36% on week 52 and 0.20% on test weeks: benign scores drift, so the fixed threshold does not hold the 0.1% target out of time. Recorded, not tuned (test weeks never used); this is what the Stage 4+ gate must watch.
- `make train BUNDLE=...` (sandbox): registered `sentinel-win64` v1, alias `production`, loads by alias as PyFuncModel; `docs/models/Win64/`, `docs/BASELINE.md` written.
- Golden `tests/golden/v1_metrics.json` (Win64) + `tests/unit/test_golden_v1.py`. `make lint` clean; `make test` 94 passed, coverage 98%.

### Check results (Kaggle, Win32, 2026-11-20)
- Kaggle CPU run (FILE_TYPE=Win32). `rebuild_check.json`: "identical content" vs `docs/cleaning/Win32.json`. Result zip sha256 `56308c8a3b45588b6151e8fea8a173ae82bec1b59ced7c2df11f0b62ed1c6c07`.
- Fit 1,409,967 rows (0 unlabelled); calibration 74,071 rows (37,068 benign; 45,928 not yet labelled at as-of). LightGBM 4,318 s, LR 79 s.
- Week 52, LightGBM v1: recall **92.15%** [91.71, 92.57] at measured FPR **0.080%** [0.046, 0.140] (12/15,000); AUC 0.9988; ECE 0.0022. LR: recall 59.82% at FPR 0.053%. Gap +32.33 points, intervals do not overlap. Never-break flags: LightGBM 0/500, LR 2/500.
- Test weeks 53-64 (report only): LightGBM recall 91.83%, FPR 0.166% (299/179,987); FPR rises late (week 58 0.28%, week 64 0.49%): same benign drift as Win64, appearing later.
- Registered `sentinel-win32` v1, alias `production`; BASELINE.md now covers Win32 + Win64; golden extended to Win32.
- `make lint` clean; `make test` 95 passed, coverage 98%.

### Stage 2 DONE (2026-11-20)
- Done-when met: `make train` registers v1 as production (both datasets); `docs/BASELINE.md` compares LightGBM vs LR at fixed FPR with Wilson intervals; golden test guards v1 recall at 0.1% FPR on week 52.
- Carried forward: the fixed threshold drifts above target out of time (Win64 at once, Win32 from ~week 56). Candidate v2 fix: conservative threshold (upper Wilson bound) chosen on weeks <= 51 only; for the gate to judge in Stage 4+.

### Issues found and fixed (Stage 2)
- Late-label filter compared milliseconds with seconds (Parquet stores timestamp[s] as ms) → every row looked unlabelled; fixed + regression assertion.
- LightGBM Dataset built with different params than training (min_data_in_leaf) → error; Dataset now gets the training params.
- structlog crash "I/O operation on closed file" in tests → logger looks up sys.stderr per call.
- Synthetic test weeks reused train seeds, so Stage 1's leakage rule (correctly) removed every test row; fixed the test data.
