# BUILD PLAN — Sentinel Gate (Project 1: Safe deployment of malware detection models)

You are building Sentinel Gate, a safe-deployment system for malware detection models. Read docs/BUILD_PLAN.md fully, then CLAUDE.md and docs/PROGRESS.md if they exist. Follow the Execution Protocol in Part A exactly. Continue from the first stage that is not DONE and keep going through the stages until you hit a STOP point. Observability uses PostgreSQL ops_metrics, Jaeger and Streamlit only: do not add Prometheus or Grafana. Never download or run real malware binaries.

> **Owner overrides (take precedence over Part A):**
> - Do NOT perform any GitHub action (push, PR, remote, Actions run) without asking the owner first, every time.
> - After each stage: write a summary and WAIT for the owner's command before starting the next stage.
> - Never download or run real malware binaries.

## PART A — EXECUTION PROTOCOL (read fully before doing anything)

You are building the whole project from this plan, stage by stage, with as little hand-holding as possible.

### A1. Session start
Read CLAUDE.md, this file, docs/PROGRESS.md and docs/DECISIONS.md.
If CLAUDE.md does not exist, create it from PART B. If docs/PROGRESS.md does not exist, create it with one row per stage (status: TODO).
Find the first stage that is not DONE. Continue from there.

### A2. Loop for every stage
- Write a short plan for the stage into docs/PROGRESS.md (files to create, tests to write). Do not wait for approval unless the stage is marked STOP.
- Build in small steps: write a test, make it pass, run make lint test.
- Run every check in the stage's "Done when". Paste a short summary of the real command output into PROGRESS.md.
- If a check fails, fix it and re-run. After 3 failed attempts on the same check, STOP and report.
- Update docs/DECISIONS.md (one line per decision: what, why, alternative rejected).
- Commit with message stage-N: <title> and create git tag stage-N-done. Mark the stage DONE in PROGRESS.md.
- Move to the next stage without asking. *(Overridden: wait for the owner.)*

### A3. Hard rules
- Never weaken, skip or delete a test to make it pass. Never mark a stage DONE with a failing check.
- Never guess dataset URLs, column names, file layouts or paper numbers. Read official sources. If you cannot verify, STOP and ask.
- Never use test data for training, tuning, thresholds or calibration.
- Never download or run real malware binaries. Feature records only.
- Never put secrets in git.

### A4. When to STOP and wait for me
Stop, write a summary in PROGRESS.md, and tell me what you need, when:
- You finish a stage marked [STOP].
- You reach a stage marked [OPTIONAL]: ask whether to build it or skip it.
- A fact cannot be verified, a check fails 3 times, or results contradict the plan's assumptions.
- Your context is getting long (roughly after 2 stages in one session). Write a handoff note at the top of PROGRESS.md (what is done, what is next, open issues) and tell me to start a new session with the kickoff prompt.

### A5. Final message of each session
List: stages completed, checks passed, open issues, and the exact next stage.

## PART B — CLAUDE.md (create this if missing)
See /CLAUDE.md.

## PART C — STAGES

### Stage 0: Repo setup and dataset check [STOP]
Goal: set up the repo and verify the dataset before any modelling.
- Layout: src/sentinel/{data,features,models,gate,serving,replay,rollout,monitoring}/, tests/{unit,integration}/, configs/, dashboards/streamlit/, docs/, scripts/, data/{raw,clean,splits}/ (gitignored).
- uv + pyproject.toml; ruff, mypy (strict on src/), pytest, pytest-cov, pre-commit.
- Makefile targets: setup, lint, test, up, down, data, train, gate, dashboard, demo.
- scripts/download_ember.py for EMBER2024 using the OFFICIAL repo instructions. Read the official README; do not guess URLs.
- scripts/inspect_dataset.py: per file type — rows per split and per week, malicious vs benign counts, feature count, label values, missing values, date range.
- Create docs/DECISIONS.md, docs/DATASET_REPORT.md, docs/PROGRESS.md.

Done when:
- make setup && make lint && make test pass (a smoke test is fine).
- DATASET_REPORT.md has REAL measured numbers and flags any mismatch with "PDF subset ≈ 52,000 train / 12,000 test; 52 train weeks, 12 test weeks".
- It states how many BENIGN files are in the test weeks. If fewer than 50,000, recommend which file type(s) to use instead, because a 0.01% false-alarm rate cannot be measured on a small benign set.
- STOP: summarise and wait for my choice of file type(s).

### Stage 1: Data cleaning and data contracts
Goal: clean, versioned, time-split Parquet files every later stage trusts.
- pandera schema in src/sentinel/data/schema.py: names, dtypes, labels in {0,1}, no missing labels, timestamp present, feature ranges.
- Cleaning pipeline src/sentinel/data/clean.py, each step logging row counts before/after:
  - drop unknown/missing labels (count unlabeled rows)
  - remove exact duplicate feature vectors within a split; report duplicates across train/test (leakage)
  - handle missing/infinite values (rule in DECISIONS.md)
  - drop constant columns computed on TRAIN only
  - cast to float32
- Time splits: data/splits/week=NN/part.parquet. Never shuffle across time.
- feature_schema_version = hash(column names + dtypes + cleaning config). Write manifest.json with row counts and per-file hashes.
- NEVER-BREAK set: 500 benign files chosen by a documented rule from TRAIN weeks only → data/clean/never_break.parquet.
- Late labels: column label_available_at = first_seen + delay, delay from a configurable seeded distribution (default 7–28 days). Training at time T may only use rows with label_available_at <= T.

Tests: unit test per cleaning rule on tiny DataFrames; a test that fails if any test-week row hash appears in train weeks; a determinism test (re-run gives identical hashes).
Done when: make data writes manifest.json; tests pass; coverage of src/sentinel/data ≥ 85%; every cleaning rule explained in DECISIONS.md.

### Stage 2: Baseline models and evaluation harness
Goal: train v1 and build ONE evaluation module used everywhere.
- src/sentinel/models/train.py: LightGBM on weeks 1..K (K in config) + logistic regression baseline. Fixed seeds. Log params, metrics, feature_schema_version and data fingerprint to MLflow.
- src/sentinel/models/evaluate.py (pure, typed, tested): threshold_at_fpr, recall_at_fpr, wilson_interval, auc, auprc, reliability curve + expected calibration error, per_week_metrics.
- Isotonic calibration on a held-out calibration week (never test weeks).
- Default operating point: target FPR = 0.1% (config). All comparisons at FIXED FPR, never at threshold 0.5.
- Register v1 in MLflow with alias production.

Tests: threshold_at_fpr on synthetic scores; wilson_interval vs textbook values; golden file tests/golden/v1_metrics.json guarding v1 recall at 0.1% FPR on week K+1.
Done when: make train registers v1 as production; docs/BASELINE.md compares LightGBM vs logistic regression at fixed FPR with intervals.

### Stage 3: Serving service (serving side)
Goal: FastAPI scoring that loads only what the registry says, logs full lineage, applies overrides outside the model.
- POST /v1/score — request {file_id, tenant_id, features[], feature_schema_version}; response {score, verdict, model_version, threshold, override_applied, request_id}.
- Load model by alias. Hot reload: poll registry every 30 s, atomic swap, zero failed requests. Keep previous model in memory for instant rollback.
- Feature schema mismatch → HTTP 409 with a clear error. Never score silently.
- src/sentinel/serving/overrides.py: per-tenant allow/block lists in PostgreSQL; overrides win; log when an override changes a verdict.
- Lineage table predictions(request_id, file_id, tenant_id, model_version, feature_schema_version, threshold, score, verdict, override_applied, ts) with batched inserts.
- /healthz, /readyz. Record request count, latency and errors (labelled by model_version) through src/sentinel/monitoring/metrics.py (a simple version now; finished in Stage 8).
- Docker Compose: api, postgres, mlflow, jaeger. Do NOT add Prometheus or Grafana.

Tests: hot reload under 200 concurrent requests → zero 5xx; schema mismatch → 409; allow-listed file never blocked.
Done when: make up works, curl to /v1/score works, tests pass, hot-reload design in DECISIONS.md.

### Stage 4: Replay stream and shadow scoring
Goal: stream test weeks in time order; score with production AND a shadow candidate on the same events.
- Add Redpanda to Compose. Topic file-events, partitioned by file_id.
- src/sentinel/replay/producer.py: publishes week files in timestamp order at configurable speed. Assign tenant_id and ring (ring0 = 5%, ring1 = 25%, ring2 = rest) by stable hash of tenant_id.
- Consumer A (live) calls the serving API.
- Consumer B (shadow) is a SEPARATE process scoring with the candidate alias → shadow_predictions. Never affects verdicts; its crash or lag never affects live scoring.
- Idempotent processing: unique key (event_id, model_version).
- src/sentinel/models/retrain.py: trains a candidate at simulated time T using only labels available by T. Policies via config: all history, last N weeks, recency-weighted.

Tests: kill shadow mid-run → live continues with no gaps; replaying a week twice creates no duplicates; retraining never uses a label available after T.
Done when: a 2-week replay finishes with matching event counts in live and shadow tables.

### Stage 5: The release gate (core of the project)
Goal: compare candidate vs production on the SAME events → PROMOTE / REJECT / INSUFFICIENT_EVIDENCE, with a permanent evidence bundle. Explain every statistical choice in DECISIONS.md in plain words.

Rules in configs/gate_policy.yaml (versioned), e.g.:
```yaml
operating_fpr: 0.001
min_recall_gain: 0.0
max_fpr_increase_relative: 0.10
never_break_hits_allowed: 0
max_weekly_recall_drop: 0.02
min_disagreements: 30
confidence: 0.95
```
Checks (each returns pass / fail / insufficient with numbers):
a. Recall at fixed FPR for both models + Wilson intervals.
b. Disagreement test: 2x2 table of (v1 right/wrong) × (v2 right/wrong), separately for malicious and benign files; McNemar exact test on the flipped cells; list the flipped files.
c. Never-break: zero flags allowed.
d. Per-week regression within limit.
e. Calibration: ECE candidate vs production.

Decision: any hard fail → REJECT; any insufficient → INSUFFICIENT_EVIDENCE; all pass → PROMOTE. Reason in plain English.
Evidence bundle JSON: policy version, both model versions, data fingerprints, all metrics and intervals, flipped-file IDs, decision, reason. Store SHA-256 in gate_decisions; make the table append-only with a trigger blocking UPDATE/DELETE.
On PROMOTE set alias candidate_approved (do not touch production; Stage 6 rolls out).
CLI: make gate CANDIDATE=<version>.

Tests: synthetic candidates — clearly better passes; 10× false alarms fails; too few disagreements → INSUFFICIENT_EVIDENCE; never-break hit fails; editing a stored bundle is detected by hash; McNemar matches scipy/statsmodels.
Done when: the gate runs on a real candidate and writes a readable decision; DECISIONS.md explains McNemar and fixed FPR.

### Stage 6: Ring rollout and automatic rollback [OPTIONAL]
Goal: roll an approved model out ring by ring; roll back automatically on bad signals.
- src/sentinel/rollout/controller.py state machine: APPROVED → RING0 → RING1 → RING2 → COMPLETE, plus ROLLED_BACK. Transitions stored with timestamps and reasons.
- Serving uses per-ring aliases (ring0_model, ring1_model, ring2_model) by tenant ring.
- Advance only after a minimum number of events AND passing watch signals.
- Watch signals: flag rate vs production (with interval), never-break hits, error rate, p99 latency. Breach → revert ring aliases within one reload cycle.
- Kill switch POST /admin/rollback (API key) reverts all rings.

Tests: bad model rolled back in ring0 before ring1; good model reaches COMPLETE; kill switch restores all rings in < 60 s.
Done when: both simulations shown with timelines in docs/ROLLOUT_DEMO.md.

### Stage 7: The main experiment
Goal: headline result — how often each release policy ships a model that is actually worse later. Never tune on test weeks.
- Every W weeks retrain a candidate (each retraining policy) and decide with:
  - P1: auto-promote every retrain
  - P2: promote if validation AUC is higher
  - P3: Sentinel Gate
- "Harmful" = on the following weeks, recall at fixed FPR is worse than the model it replaced, OR it flags a never-break file.
- Repeat with 20+ seeds / bootstrap resamples of the weekly streams.
- Report per policy: harmful promotions, good updates blocked, recall and FPR over time, number of promotions.
- Did drift alerts (PSI on top features and on scores) predict harmful candidates? Report drift-alert precision.
- Save to MLflow and data/results/experiment.parquet.

Tests: deterministic for a given seed; assert no test-week data is used for thresholds or calibration.
Done when: docs/RESULTS.md has one table, two charts and 5 honest conclusions, including any that do not favour the gate.

### Stage 8: Observability, alerts and load testing
No Prometheus and no Grafana. Metrics live in PostgreSQL, traces in Jaeger, dashboards in Streamlit, alerts from a Python worker.
- Metrics recorder src/sentinel/monitoring/metrics.py: in-process counters and latency samples per (service, metric, labels). Every 10 s compute count, sum, p50/p95/p99 and write one row per series to ops_metrics(ts_bucket, service, metric, labels jsonb, count, sum, p50, p95, p99). Background thread with a bounded buffer; if the database is down, drop samples and count the drops. It must never slow or fail a request.
- Consumer lag: a small job reads Redpanda committed vs latest offsets for the live and shadow consumers every 10 s and writes them to ops_metrics.
- Tracing: OpenTelemetry SDK with OTLP export to a Jaeger all-in-one container (UI on port 16686). One trace per event: consume → score → log. Store trace_id in the predictions table so every prediction links to its trace.
- Alert worker src/sentinel/monitoring/alerts.py, every 30 s, rules in configs/alert_rules.yaml:
  - p99 latency > 20 ms for 5 min
  - error rate > 1% for 5 min
  - consumer lag rising for 10 min
  - any never-break hit (fires at once)
  FIRING/RESOLVED states go to an append-only alerts table, de-duplicated, with an optional POST to a webhook URL from config. The Stage 6 rollout controller reads the same signals.
- Retention: a daily job downsamples ops_metrics to 1-minute buckets after 1 day and deletes rows older than 14 days.
- Streamlit app — start it here (Stage 9 adds the ML pages). Operations section:
  - Service Health: requests/sec, error rate, p50/p95/p99 latency by model_version, consumer lag (live and shadow), model reload events as markers.
  - Model Live: flag rate by ring and model_version, override rate per tenant, never-break hits (flat zero expected), rollout state over time.
  - Alerts: firing now, history with durations, rule definitions.
  - From any latency spike, a link opens the Jaeger UI filtered to that service and time window.
- Locust load test at 500 req/s for 5 min; results in docs/LOAD_TEST.md. If p99 > 20 ms, profile and fix (batched writes, warmup) and record before/after. Confirm the metrics recorder adds under 1 ms per request.

Tests: percentile maths matches numpy on random data; each alert rule fires and resolves on synthetic metrics; with the database stopped, the API still serves requests; trace_id is stored with predictions.
Done when: make up starts api, consumers, alert worker, postgres, mlflow, redpanda and jaeger; the Operations pages show live data during a replay; LOAD_TEST.md has real numbers.

### Stage 9: ML dashboard (Streamlit)
Add these pages to the Streamlit app started in Stage 8, in an "ML" section. Reads only PostgreSQL, MLflow and data/results; reuses evaluate.py. Sidebar filters (date, model version, ring, tenant) shared across pages.
- Overview: production model per ring, last gate decision, rollout state, 7-day flag rate, status cards.
- Model Registry: all versions with training window, retrain policy, data fingerprint, gate outcome, MLflow links.
- Gate Decision Viewer: each check with numbers, reason text, recall-at-FPR interval bars for both models, McNemar 2x2 tables, flipped-files table with both scores, "verify bundle hash" button.
- Head-to-head: ROC and PR curves for any two versions zoomed to FPR 0–1%, per-week recall/FPR lines, calibration curves.
- Drift: PSI heatmap (top-20 features × week), score distributions by week, drift alerts marked.
- Rollout Timeline: state timeline per rollout, ring-by-ring flag rate, rollbacks with reasons.
- Experiment Results: harmful promotions per policy with intervals, recall over time per policy, blocked good updates, drift-alert precision.
- Prediction Lookup: by file_id or request_id → full lineage.

Every chart has a one-line caption; st.cache_data with short TTL; pages load < 3 s.
Tests: Streamlit AppTest renders each page on a seeded demo database.
Done when: make dashboard opens all 8 pages on demo data.

### Stage 10: Hardening, CI and handover [STOP]
- GitHub Actions: lint, mypy, unit + integration tests (compose), coverage ≥ 80%.
- make demo: small dataset, two models, gate run, dashboards filled in < 10 min on a laptop.
- Secrets via .env (+ .env.example), API key on admin endpoints, gitleaks in pre-commit.
- README: pitch, Mermaid architecture, quick start, headline results table, screenshots of the Operations and ML pages and one Jaeger trace, limitations.
- Strict reviewer pass: list the 10 weakest points (code, tests, claims); fix those under an hour; document the rest in docs/LIMITATIONS.md.
