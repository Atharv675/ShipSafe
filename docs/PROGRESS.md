# Progress

> **HANDOFF (2026-11-20)**
> - **Done:** Stages 0-3. Owner decisions: Win64 + Win32 as separate datasets; heavy data/training runs on **Kaggle CPU notebooks** (owner uploads/downloads; Claude never does). Cleaned data is rebuilt per Kaggle session; manifests in `docs/cleaning/` prove it.
> - **Models:** `sentinel-win64` v1 and `sentinel-win32` v1, alias `production`, MLflow SQLite registry in `$SENTINEL_DATA_DIR/mlflow`; `docs/BASELINE.md`; golden test `tests/golden/v1_metrics.json`.
> - **Stack:** `make up` (api on http://127.0.0.1:18080, mlflow :5000, jaeger :16686, postgres :5432; secrets in `.env`), `make sample-request`, `make test-integration`, `make down`.
> - **Next:** Stage 4 (replay stream + shadow scoring) after the owner's go-ahead. Note for Stage 4: Win64/Win32 week files are not stored locally (only PDF/ELF/APK/Dot_Net raw zips are); replay needs the cleaned test weeks, so decide where they come from (rebuild locally from a download, or a Kaggle export) before building it.
> - **Open issue:** the fixed threshold drifts above 0.1% FPR out of time (Win64 week 52, Win32 from ~week 56); candidate fix for a v2 challenger, for the gate to judge.
> - **Rules:** no git/GitHub commands at all; stop after every stage; no real malware; everything runs in the hardened sandbox.

| Stage | Title | Status |
|---|---|---|
| 0 | Repo setup and dataset check [STOP] | DONE |
| 1 | Data cleaning and data contracts | DONE |
| 2 | Baseline models and evaluation harness | DONE |
| 3 | Serving service | DONE (awaiting owner go-ahead) |
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

## Stage 3 — Serving service

### Plan
- `configs/serving.yaml` → `ServingConfig` (file types, alias, poll 30 s, lineage batch/flush/queue, override refresh); `Settings.pg_dsn` from `.env` (never in git).
- `src/sentinel/serving/models.py`: `LoadedModel` (booster + calibrator + meta, scores numpy directly), `MlflowModelSource` (alias → version, load by version), `ModelManager` (one slot per file type, keeps previous in memory, atomic swap of an immutable schema→model map, instant rollback, failed load keeps current). Requests are routed by `feature_schema_version` (Win64 and Win32 have different ones); unknown → 409.
- `src/sentinel/serving/overrides.py`: per-tenant allow/block in Postgres `tenant_overrides`, cached snapshot refreshed every few seconds; `apply_override` (overrides win); log when an override changes a verdict. Not ready until the first snapshot loads.
- `src/sentinel/serving/lineage.py`: `predictions` table, background batched inserts with a bounded queue; DB trouble never fails a request (drops counted).
- `src/sentinel/serving/app.py`: `POST /v1/score`, `/healthz`, `/readyz`, `GET /v1/models`, `GET /v1/ops/stats`; `src/sentinel/monitoring/metrics.py` (requests, errors, latency by model_version); percentiles via `evaluate.latency_summary`.
- Compose: postgres (pinned digest, init SQL + least-privilege API role), mlflow (our image, SQLite registry on `$SENTINEL_DATA_DIR/mlflow`), api, jaeger (pinned digest; tracing wired in Stage 8). Hardened: read-only, cap_drop ALL, no-new-privileges, ports on 127.0.0.1 only. `make up` / `make down`.
- `scripts/make_sample_request.py`: a real request built from a local EMBER2024 feature record (Dot_Net test zip, same PE feature extractor) projected onto the model's columns, for curl.
- Tests: unit (fake + real MLflow registry): hot reload under 200 concurrent requests → zero 5xx; schema mismatch → 409; allow-listed file never blocked; bad length / non-finite → 422; rollback; failed load keeps serving; lineage batching/drops; metrics. Integration (`make test-integration`, compose network): Postgres overrides + predictions round trip, curl-equivalent against the running api.

### Check results (2026-11-20)
- `make lint` — ruff + format clean; mypy strict: no issues in 31 source files.
- `make test` (unit, sandbox, no network) — 124 passed, coverage 96% (serving/app 98%, models 97%; the PostgreSQL code paths are covered by the integration tests). Includes: hot reload under 200 concurrent requests with a real MLflow registry, alias v1→v2→v1→v2, 600/600 HTTP 200, zero 5xx; schema mismatch → 409; allow-listed file never blocked (50 random vectors on a model that flags everything); 422 on wrong length / NaN / float32 overflow; failed load, failed warm-up, schema clash and wrong file type all keep the current model; instant rollback; lineage batching, queue-full drops, retry-then-drop, flush on close; metrics percentiles = numpy.
- `make up` — api, postgres, mlflow, jaeger all healthy; api ready ~7 s after start with the REAL models: `sentinel-win64/1` (2,538 features, schema `b9521b9b93166b91`) and `sentinel-win32/1` (2,536 features, schema `6bad8af2adf3d418`); api memory ~320 MiB.
- curl `POST /v1/score` with real request bodies (`make sample-request`, Dot_Net test records projected onto Win64's columns): 3/3 benign → benign (scores 8.6e-5, 0.043, 7.8e-7); malicious → 2/3 flagged (0.99999985, 0.99999996), one at 0.99699 just under the strict threshold 0.99992 (out-of-distribution records; not an accuracy measure). Wrong schema → HTTP 409 with the served versions.
- `make test-integration` (sandbox on the compose network) — 6 passed: both real models served by alias; lineage row matches the response field by field; 409; allow-list turns a real malicious verdict into benign (score unchanged) and is written to lineage; block-list wins; `sentinel_api` cannot UPDATE/DELETE predictions, write overrides or DROP tables.
- Hardening verified with `docker inspect`: every service read-only, cap_drop ALL, no-new-privileges, ports bound to 127.0.0.1 only; api/mlflow uid 10001.
- In-service latency (200 sequential curl requests, handler time only): p50 1.5 ms, p95 2.4 ms, p99 4.7 ms. End-to-end load test is Stage 8.

### Issues found and fixed (Stage 3)
- MLflow writes metadata next to artifacts when loading a `models:/` URI → failed on the read-only mount; the api now loads from a private temp copy (regression test). The failure itself showed the safety path working: api stayed up, not ready, retrying every 30 s.
- Both compose services built the same image at once ("already exists") → `make up` builds it once.
- Host ports 8000 (Splunk) and 8080 (Java) in use → api published on 18080 (`SENTINEL_API_PORT`).
- `close()` of the lineage writer waited for the flush timer → it now notices shutdown within 50 ms.
- Serving set MLflow's registry URI globally, which leaked into a Stage 2 test → removed (registry defaults to the tracking URI).
- `make test-integration` echoed the database passwords in its command line → DSNs now passed via environment; both passwords rotated and the postgres volume recreated (it held only test rows).

### Stage 3 DONE (2026-11-20)
- Done-when met: `make up` works; curl to `/v1/score` works; tests pass (unit + integration); hot-reload design in DECISIONS.md.
