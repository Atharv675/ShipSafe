# Progress

> **HANDOFF (2026-11-22)**
> - **Stage 6 DONE (2026-11-22):** API routes by ring (`ring0_model`/`ring1_model`/`ring2_model`, fallback `production`); controller `make rollout CANDIDATE=<v> [FILE_TYPE=] [DEMO=1]`, `make rollout-status`, `make kill-switch`, `make rollout-timeline`, `make demo-models`, `make demo-reset`; append-only `rollout_transitions`. On an existing database: add `SENTINEL_ROLLOUT_DB_PASSWORD` + `SENTINEL_ADMIN_API_KEY` to `.env`, `docker compose up -d postgres`, `make migrate`, `make rollout-init`, `make up`. Demo results in `docs/ROLLOUT_DEMO.md`. Registry left on v1 everywhere. Unreplayed weeks left: Win64 63-64, Win32 59-64. **Next: Stage 7 (main experiment)** after the owner's go-ahead; start a fresh session.
> - **Win32 through Stages 4-5 (2026-11-21):** `sentinel-win32` v2 candidate registered, weeks 53-55 replayed (89,998 = 89,998), gate → **REJECT** (`docs/gate/Win32-v2-vs-v1-20261121T062053Z.md`). Use `FILE_TYPE=Win32` with `make replay`, `make replay-counts` and `make gate`. **Next: Stage 6 (OPTIONAL) or Stage 7** after the owner's go-ahead.
> - **Stage 5 DONE (2026-11-20):** `make gate CANDIDATE=<v> [GATE_WEEKS=...]`, `make gate-verify`, policy `configs/gate_policy.yaml` (v1.0.0), decisions in append-only `gate_decisions` + `docs/gate/`. First real decision: Win64 v2 vs v1 on weeks 53-55 → **REJECT**. Run `make migrate` on an existing database (adds gate_decisions + `sentinel_gate`; needs `SENTINEL_GATE_DB_PASSWORD` in `.env` and `docker compose up -d postgres` first). **Next: Stage 6 (ring rollout, OPTIONAL)** after the owner's go-ahead.
> - **Stage 4 DONE (2026-11-20):** Win64 test weeks 53-64 + never-break set imported to `$SENTINEL_DATA_DIR` (`make import-replay`); candidate `sentinel-win64` v2 (last 26 weeks, T = 52) under alias `candidate`; Redpanda + live/shadow consumers in compose; `make replay WEEKS=...`, `make replay-counts`, `make replay-check`. Weeks 53-55 are already replayed (rows in `predictions` / `shadow_predictions`). **Next: Stage 5 (release gate)** after the owner's go-ahead. Win32 has no candidate or local test weeks yet (same notebook with `FILE_TYPE = "Win32"`).
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
| 3 | Serving service | DONE |
| 4 | Replay stream and shadow scoring | DONE |
| 5 | The release gate | DONE |
| 6 | Ring rollout and automatic rollback [OPTIONAL] | DONE (awaiting owner go-ahead) |
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

## Stage 4 — Replay stream and shadow scoring

### Owner decisions (2026-11-20)
- A candidate retrained at time T may learn from any week whose labels are known by T (earlier test weeks included); threshold/calibration only from weeks <= T; judged only on weeks after T. v1 and reported baselines stay untouched by test weeks. CLAUDE.md updated.
- Data comes from Kaggle: one run per dataset (Win64 first) exports the verified test weeks 53-64 + never-break set, and trains ONE candidate (policy last_n_weeks, 26 weeks, T = end of week 52). Local machine: 15.8 GB RAM / Docker 8 GB, too small for full-size retraining.

### Plan
- `configs/retrain.yaml` + `src/sentinel/models/retrain.py` (policies all_history / last_n_weeks / recency_weighted; reads only weeks <= T, labels known by T); `scripts/retrain_candidate.py` (Kaggle).
- `src/sentinel/replay/transfer.py` + `scripts/export_replay.py` (Kaggle) / `scripts/import_replay.py` (local): only allow-listed files, sha256 per file, week files must match the rebuilt manifest, manifest must match docs/cleaning (content). `notebooks/stage4_export_and_candidate.ipynb`.
- Registry: `register_candidate` (new version of sentinel-<type>, alias `candidate`, production untouched).
- Redpanda in compose (pinned), topic `file-events` keyed by file_id. `src/sentinel/replay/events.py` (event format, stable tenant + ring hashing: ring0 5%, ring1 25%, ring2 rest), `producer.py` (week files in first_seen order, configurable speed), `live_consumer.py` (calls the API; idempotent via (event_id, model_version)), `shadow_consumer.py` (separate process/container, candidate alias, writes `shadow_predictions`; cannot affect live).
- Tests: retraining never uses a label after T (done); transfer refusals (done); event/ring hashing; producer ordering; idempotency; integration: 2-week replay with matching live/shadow counts, kill shadow mid-run, replay a week twice.

### Check results (2026-11-20)
- Kaggle run (Win64): rebuild check `identical content` vs `docs/cleaning/Win64.json`. Replay zip 402 MB (sha256 `9a1b4c3d…`), candidate zip 31 MB (sha256 `60a5c252…`); both listed before use, only allow-listed names.
- `make import-replay` — every file re-hashed and matched; weeks 53-64 (12) + never-break set installed.
- `make register-candidate` — `sentinel-win64` **v2**, alias `candidate`; `production` still v1. The shadow consumer loaded v2 by itself within 30 s.
- Candidate v2 (policy last_n_weeks 26, T = 52, fit weeks 23-48 = 259,995 rows, calibration weeks 49-52 = 14,782 rows, seed 20241122, fingerprint `c824866c…`): on its calibration weeks recall 89.84% (CI 89.13-90.50%), FPR 0.095% (7/7,342), threshold 0.9999800; never-break 0/500 flagged. Not a test-week result: the gate (Stage 5) judges it on weeks 53+.
- `make replay-check`:
  1. Two-week replay, weeks 53-54: 19,999 events published in 14 s; **live 19,999 events / 19,999 rows (v1), shadow 19,999 / 19,999 (v2)** — counts match, no duplicates.
  2. Week 55 at 200 events/s, shadow consumer KILLED after 15 s: live finished 9,997/9,997 with no gaps; shadow stopped at 2,270; after restart it caught up to 9,997/9,997 rows — no gaps, no duplicates.
  3. Week 53 replayed again (9,999 events) and both consumer groups drained to lag 0: still 9,999 rows in each table — no duplicates.
- Consumer logs over the run: 0 errors in live-consumer and api; shadow's only errors are the expected "alias candidate not found" before registration.
- `make lint` — ruff + format clean; mypy strict: no issues in 37 source files.
- `make test` — 155 passed, 1 skipped, coverage 96%.
- `make test-integration` — 6 passed.

### Issues found and fixed (Stage 4)
- Redpanda could not write `/etc/redpanda` on a read-only root → tmpfs owned by uid 101; it also rejected the default RPC advertise address → explicit `--rpc-addr` / `--advertise-rpc-addr`.
- Both consumers crashed (`_NO_OFFSET`) when a batch held only broker errors (topic not yet created) → commit only if a batch has a real message; unit test added.
- Provenance inside a Kaggle code bundle now records the bundle's file hash next to the commit.

### Stage 4 DONE (2026-11-20)
- Done-when met: a 2-week replay finished with matching event counts in live and shadow tables (19,999 = 19,999). Plan tests met: shadow killed mid-run → live had no gaps; a week replayed twice → no duplicates; retraining never uses a label after T (unit tests).

## Stage 5 — The release gate

### Plan
- `configs/gate_policy.yaml` (versioned, `policy_version` 1.0.0) → `GatePolicy`.
- `evaluate.py`: `paired_table` (2x2 correctness table) and `mcnemar_exact` (exact two-sided, integer arithmetic).
- `src/sentinel/gate/checks.py` (pure): coverage, (a) recall at fixed FPR + Wilson CIs, (a') false alarms at shipped thresholds, (b) disagreement + McNemar per class + flipped files, (c) never-break, (d) per-week regression, (e) calibration ECE (soft); `decide`.
- `src/sentinel/gate/evidence.py`: canonical JSON, SHA-256, verify, Markdown report. `src/sentinel/gate/run.py`: pairs live/shadow scores by event id, labels known at decision time, refuses weeks <= a model's as_of_week, stores the decision, writes `docs/gate/`, moves `candidate_approved` on PROMOTE only.
- `docker/postgres/init/005_stage5.sql` (append-only `gate_decisions`, hash CHECK, triggers) + `006_gate_role.sh` (`sentinel_gate`). `make gate CANDIDATE=<v>`, `make gate-verify`.

### Check results (2026-11-20)
- `make lint` — clean; mypy strict: no issues in 40 source files.
- `make test` — 188 passed, 2 skipped, coverage 97% (gate/checks 97%, gate/evidence 93%, evaluate 99%). Plan tests: clearly better synthetic candidate → PROMOTE; 10× false alarms → REJECT; identical candidate and 6+6 balanced flips → INSUFFICIENT_EVIDENCE; never-break hit → REJECT; one collapsed week → REJECT; editing a stored bundle (DB text or report file) is detected by hash; McNemar = `scipy.stats.binomtest` on 9 cases incl. (0,0) and (450,520). Also: low coverage / too few clean files → insufficient; soft calibration failure does not block; no evaluation on weeks <= as_of; late labels respected; stored-threshold mismatch stops the gate.
- `make test-integration` — 9 passed (3 new): `sentinel_gate` can insert/read but not update/delete; even the owner cannot UPDATE/DELETE/TRUNCATE (trigger); the DB rejects a hash that does not match the bundle. Run in rolled-back transactions.
- **Real gate run** `make gate CANDIDATE=2` (Win64 v2 last-26-weeks retrain vs production v1, weeks 53-55, 29,996 / 29,996 events paired, all labels known by 2024-11-09) → **REJECT**, report `docs/gate/Win64-v2-vs-v1-20261120T171839Z.md`, bundle SHA-256 `580301ef…`:
  - recall at 0.1% FPR: candidate 86.86% (CI 86.31-87.39) vs production 87.49% (86.95-88.01), −0.63 points → fail;
  - shipped thresholds: candidate 17 false alarms / 14,996 clean (0.11%) vs production 27 (0.18%) → pass (better);
  - disagreements: malware 525 production-only catches vs 85 candidate-only (McNemar p = 2.4e-78) → candidate significantly worse; clean files 12 false alarms cleared vs 2 added (p = 0.013) → candidate better;
  - never-break 0/500 (both) → pass; week 53 recall drop 4.42 points (> 2) → fail; weeks 54/55 candidate better (+1.18, +0.58);
  - ECE 0.0044 vs 0.0024 (soft, within +0.01) → pass.
  - Plain reading: the recent-data-only candidate trades ~440 extra missed malware files for 10 fewer false alarms over three weeks, and is worse at equal false alarms. Not safe to replace v1.
- `make gate-verify` — 1 decision, 0 problems. Production alias unchanged (v1); `candidate_approved` not set.

### Stage 5 DONE (2026-11-20)
- Done-when met: the gate ran on a real candidate and wrote a readable decision (`docs/gate/*.md`); DECISIONS.md explains McNemar and the fixed FPR in plain words.

### Win32 through Stages 4-5 (2026-11-21)
- Code: `replay.yaml` and `gate_policy.yaml` now list `file_types: [Win64, Win32]`; the shadow consumer loads one candidate per type (waits until all exist); `make replay|replay-counts|gate ... FILE_TYPE=Win32` (default Win64); `GatePolicy.file_type()` refuses an uncovered or unchosen type. Rules unchanged (policy 1.0.0). See DECISIONS.md "Win32 added".
- Kaggle Stage 4 notebook run with `FILE_TYPE = "Win32"`. `make import-replay`: weeks 53-64 + never-break verified and installed. `make register-candidate`: `sentinel-win32` v2 (last 26 weeks, T = 52) under `candidate`; `rebuild_check.json` "identical content" vs `docs/cleaning/Win32.json`; never-break 0/500; calibration-weeks recall 86.3% at FPR 0.10%.
- `make up` (app image rebuilt): shadow consumer loaded Win64 v2 and Win32 v2 (311 MiB of 2 GiB).
- `make replay FILE_TYPE=Win32 WEEKS="53 54 55"`: 89,998 events in 92 s; live 89,998 rows (`sentinel-win32/1`) = shadow 89,998 rows (`sentinel-win32/2`), no duplicates.
- **Gate** `make gate CANDIDATE=2 FILE_TYPE=Win32` → **REJECT**, `docs/gate/Win32-v2-vs-v1-20261121T062053Z.md`, SHA-256 `6faa08fc…`:
  - recall at 0.1% FPR: candidate 92.91% (CI 92.67-93.15) vs production 94.40% (94.18-94.61), −1.49 points → fail;
  - shipped thresholds: 23 vs 26 false alarms on 44,998 clean files → pass;
  - disagreements: malware 1,191 production-only catches vs 193 candidate-only (p = 1.1e-175) → candidate significantly worse; clean files 12 cleared vs 9 added (p = 0.66) → no real difference;
  - never-break 0/500 → pass; weekly drops 0.84 / 1.53 / 1.43 points (all < 2) → pass; ECE 0.0033 vs 0.0028 (soft) → pass.
  - Plain reading: same pattern as Win64. Retraining on the last 26 weeks only, with a stricter threshold, misses about 1,000 more malware files in three weeks without a real cut in false alarms. Not safe to replace v1.
- `make lint` clean; `make test` 189 passed (2 skipped, 97%); `make test-integration` 9 passed; `make gate-verify` 2 decisions, 0 problems. Production aliases unchanged; `candidate_approved` not set.
- Note: both bundles record git `fd6009a…` (the last commit); the Stage 5 code was uncommitted when they ran.

## Stage 6 — Ring rollout and automatic rollback [OPTIONAL — owner chose to build it, 2026-11-21]

### Plan
- `configs/rollout.yaml` → `RolloutConfig`: ring aliases (`ring0_model`, `ring1_model`, `ring2_model`, fallback `production`), check interval, minimum events per ring, canary settings, watch-signal limits.
- Serving (`src/sentinel/serving/rings.py`): the API computes the tenant's ring itself (same stable hash as the producer, rings from `replay.yaml`); one `ModelManager` per ring alias with `production` as fallback; one shared model cache, so a version is loaded once. Lineage rows get a `ring` column. `POST /admin/rollback` (header `X-API-Key`, secret in `.env`) points every ring alias back at `production`, reloads at once and records ROLLED_BACK.
- `src/sentinel/rollout/controller.py` (pure): states APPROVED → RING0 → RING1 → RING2 → COMPLETE, plus ROLLED_BACK; `desired_aliases(state)`; `decide(state, signals)` → advance / hold / rollback with a plain-English reason. `src/sentinel/rollout/run.py`: CLI loop (start, watch, reconcile aliases to the recorded state every tick, so a race with the kill switch heals itself).
- Entry ticket: a gate PROMOTE for exactly this candidate vs current production plus the `candidate_approved` alias; OR `--demo` for versions tagged `demo=true` (recorded as such).
- Watch signals per ring (on the events since the ring switched): model flag rate vs production on the same period (Wilson intervals, `evaluate.rate_increase_check`), never-break canary hits (the producer's `--canaries` sends the 500 never-break files to a canary tenant in every ring), 5xx error rate, p99 latency (from the API's metrics).
- `docker/postgres/init/007_stage6.sql`: `predictions.ring`; append-only `rollout_transitions`; `008_rollout_role.sh`: `sentinel_rollout` role; `sentinel_api` may append transitions (kill switch).
- `scripts/register_demo_models.py`: two demo versions of `sentinel-win64` built from v1's own artifacts: GOOD (identical copy) and BAD (cut-off 0.5, the classic mistake: 6× false alarms, 1 never-break hit measured on week 56).
- Make: `rollout-init`, `demo-models`, `rollout CANDIDATE= [DEMO=1]`, `rollout-status`, `kill-switch`, `rollout-timeline`.
- Tests: bad model rolled back in ring0 before ring1; good model reaches COMPLETE; kill switch restores all rings in < 60 s; router/fallback/shared cache; canaries; statistic; append-only transitions (integration).
- Demo on the real stack (Win64, unreplayed weeks 56+), timelines in `docs/ROLLOUT_DEMO.md`.
- (Changed while building) Canaries are sent by the controller through the API once the ring serves the candidate, not mixed into the replay stream (a canary published before the ring switched would be scored by production). `make demo-reset` added to undo a completed DEMO rollout.

### Check results (2026-11-21/22)
- `make lint` clean; mypy strict: no issues in 44 source files.
- `make test`: 216 passed, 3 skipped (integration modules), coverage 97% (rollout/controller 97%, rollout/store 94%, serving/app 99%). Plan tests: bad model rolled back in ring0 before ring1 (`test_bad_model_is_rolled_back_in_ring0_before_ring1`); good model reaches COMPLETE and becomes production; kill switch restores every ring in < 60 s (`test_kill_switch_restores_every_ring_in_under_60_seconds`). Also: kill switch racing the controller wins; rollback still moves aliases when the DB is down; missing ring alias falls back to production; one model load shared by 3 rings; canary requests never enter the watched metrics; gate PROMOTE or demo tag required to start; demo versions are byte-identical models with only the threshold changed (real MLflow).
- `make test-integration`: 17 passed (5 new in `test_rollout_store.py`, Stage 3 model test updated for rings): `sentinel_rollout` appends/reads but cannot change; even the owner cannot UPDATE/DELETE/TRUNCATE; malformed transitions refused by CHECKs; the API may record a kill-switch rollback; traffic query runs as the rollout role. All in rolled-back transactions.
- `make migrate` (007 + 008) on the existing database; `make rollout-init`: ring aliases at production for Win64 + Win32; `make up`: API serves by ring, 322 MiB.
- **Real-stack demo** (`docs/ROLLOUT_DEMO.md`, tables in `docs/rollout/`):
  - BAD Win64 v4 (0.5 cut-off): rolled back in ring0 after 27.5 s by a never-break canary (1/500); 53 real ring0 files exposed; ring1/ring2 never saw it.
  - Kill switch during RING1 (GOOD Win64 v3): every ring on production in **0.65 s**; recorded ROLLED_BACK by `kill_switch`.
  - GOOD Win32 v3: APPROVED → RING0 → RING1 → RING2 → **COMPLETE** in 325 s; then `make demo-reset` put Win32 production back on v1.
  - Two false alarms found on the identical GOOD model and fixed: canary burst inflating candidate p99 (canaries now in separate metrics, sent 2 at a time); production flag-rate baseline over a longer, differently mixed period (now the same period; not compared in the last ring).
- Registry after the demo: both file types serve v1 in every ring; production v1; `gate-verify` 2 decisions, 0 problems.

### Issues found and fixed (Stage 6)
- MLflow 3.17 raises a generic INVALID_PARAMETER_VALUE for a missing alias → resolve from `get_registered_model().aliases` and raise LookupError (enables the production fallback).
- Demo models' artifacts must live under the registry's artifact root, or the API container cannot read them.
- Unit test of the weak model cache was wrong (the fake kept a strong reference); fixed the test, not the code.
- Status polling in the shell dropped stderr (structlog writes there); read the table instead.

### Stage 6 DONE (2026-11-22)
- Done-when met: both simulations (bad model rolled back in ring0; good model reaches COMPLETE), plus the kill switch, shown with timelines in `docs/ROLLOUT_DEMO.md`.
