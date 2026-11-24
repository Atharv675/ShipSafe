# Decisions

One line per decision: **what** — why — *alternative rejected*.

## Stage 0
- **All tools run in a hardened Docker sandbox** (`docker/sandbox.Dockerfile`, wrapped by the Makefile): non-root uid 10001, read-only rootfs and source mount, `cap-drop=ALL`, `no-new-privileges`, pid/mem/cpu limits, `--network=none` except `make download` — owner requirement; isolates third-party code and data parsing from the host — *running uv/pytest directly on Windows*.
- **Runtime image has no git, pip or uv; base image pinned by digest; uv pinned to 0.13.6** — smaller attack surface, reproducible builds — *single-stage image with build tools*.
- **`[tool.uv] exclude-newer = 2026-11-01`** — dependency versions must be ≥ ~2 weeks old (supply-chain hygiene) and resolution is reproducible — *unbounded latest versions*.
- **`uv.lock` resolved inside a container (`make lock`)** — resolving builds the thrember git dependency, which executes its build backend — *host-side `uv lock`*.
- **Dataset source = HuggingFace `joyce8/EMBER2024`, files `{FileType}_{split}.zip`** — taken verbatim from official `thrember/download.py` @ `0ef753e` — *guessing URLs / third-party mirrors*.
- **Never touch `joyce8/EMBER2024-capa`** (download script refuses it) — it contains raw bytes of malware functions — *n/a (hard rule)*.
- **Keep zips; stream JSONL members in place, verify SHA-256 against HF LFS hashes** — halves disk use (~24 GB vs ~100 GB extracted) and proves integrity — *thrember's extract-all behaviour*.
- **Data lives in `$SENTINEL_DATA_DIR` = `C:/Users/Sarika Puranik/sentinel-data`, outside OneDrive** — 24 GB must not be cloud-synced — *repo-local `data/`* (kept as gitignored default for other machines).
- **thrember pinned to commit `0ef753e` from GitHub** — it is not on PyPI; the official extractor guarantees our vectors match published EMBER v3 features — *re-implementing vectorisation*.
- **Feature dimension reported from `thrember.PEFeatureExtractor.dim`; non-PE file types get zero-filled PE blocks** (per thrember source), so "non-constant columns in a sample" is also reported — avoids over-stating useful features for PDF/ELF/APK — *reporting raw dim only*.
- **`signify` constrained to `>=0.8.1,<0.9`** — thrember imports `signify.authenticode.SignedPEFile`, removed in signify 0.9.0 (verified in signify's tagged sources) — *forking thrember*.
- **`oscrypto` overridden to upstream commit `76bfdcb`** — PyPI's latest (1.3.0) cannot parse OpenSSL 3.0.22's version string and fails at import; upstream master's regex handles multi-digit versions (verified in source) — *downgrading OpenSSL in the image*.
- **`libgomp1` in the runtime image** — OpenMP runtime needed by LightGBM's shared library — *conda/other base image*.
- **Downloads use HF's xet backend with `HF_HOME` on the data volume + 5 retries with backoff** — xet failed once mid-file ("error decoding response body"); a persistent chunk cache lets retries reuse chunks. Plain HTTPS (`HF_HUB_DISABLE_XET`) measured ~0.25 MB/s vs ~2.4 MB/s for xet — *HTTPS-only download*.
- **Dataset counts are over UNIQUE `sha256`** — measured: every raw JSONL record appears twice in its weekly file; repeats never differ in features or labels, only (for ELF/.NET) in Capa keys `caps`/`ttps`/`mbc`. Unique counts match the published figures exactly — *counting raw lines (would double every count and the benign test set)*.
- **Integrity checks in the inspector: files repeated across weeks, train∩test overlap, and timestamps outside their weekly file (with day offsets)** — these are exactly the time-order/leakage risks Stage 1 must handle — *checking only within one week*.
- **Git commit read from `.git/HEAD` files, not by running git** — the sandbox has no git and the assistant must not run git; result files still record the commit — *subprocess `git rev-parse`*.
- **No git operations by the assistant** — owner manages git/GitHub; stage completion suggests a commit message + tag instead of creating them — *Part A's commit/tag step*.
- **Win64/Win32 measured on Kaggle from an allow-listed code bundle (`make bundle`), merged via per-type stats JSON (`docs/dataset_stats/*.json`, no file ids)** — local download ran at ~0.4 MB/s for ~22 GB; the same locked dependencies and the same inspector run remotely, and provenance records the bundle's commit and zip SHA-256s — *waiting days for the local download*.
- **Plan assumption check + file-type recommendation rendered in DATASET_REPORT.md from `configs/data.yaml: plan_assumption`** — measured PDF matches the plan's 52,000/12,000 and 52/12 weeks but has only 6,000 benign test files (< 50,000); Win32 (180,000) and Win64 (60,000) are the only types that meet the threshold. Final choice is the owner's (Stage 0 STOP) — *silently switching file type*.

## Stage 1
Owner's choice (2026-11-19): **Win64 and Win32**, cleaned on **Google Colab (free tier)**.

Pipeline shape
- **Win64 and Win32 are separate datasets** (own splits, manifest, never-break set, later own models) — different populations; mixing them would blur what the gate measures — *one pooled dataset*.
- **Streaming, two passes, one week per worker** (`src/sentinel/data/pipeline.py`) — Win32 is ~1.9M × 2568 float32 ≈ 20 GB; Colab free has ~12.7 GB RAM. Only ids + 16-byte row hashes cross weeks — *loading a split into memory*.
- **Layout `data/splits/<type>/week=NN/part.parquet`** with global week numbers 01..64 (train 01–52, test 53–64 for both types), `manifest.json` next to them; never-break in `data/clean/<type>/never_break.parquet` — the plan's `week=NN` layout plus one level per dataset — *mixed-type directories*.
- **Heavy runs on Kaggle CPU notebooks instead of Colab free** (owner, 2026-11-19) — Colab free recycled the VM after a long run and the results were lost; Kaggle gives 4 CPUs / ~34 GB RAM and background "Save & Run All" with a kept Output folder — *Colab Pro*.
- **Cleaned data is rebuilt each session, not stored** — free Drive (15 GB) cannot hold Win32; the pipeline is deterministic and manifest hashes prove a rebuild is identical — *Google Drive storage*.
- **Vectors from the official `thrember.PEFeatureExtractor`** (same as Stage 0) — published EMBER v3 features — *own vectoriser*.

Cleaning rules (in order; each logs before/after counts into manifest.json)
1. **`drop_missing_id`**: rows without a string `sha256` are dropped — every later rule keys on it.
2. **`drop_repeated_id_within_week`**: the raw JSONL lists every file twice (Stage 0); the first copy is kept — Stage 0 measured 0 feature/label differences between copies (only Capa keys differ, which are not features).
3. **`drop_bad_label`**: labels must be int 0/1 (`label_values`); None, other ints, bools, floats and strings are dropped and counted (unlabeled) — *treating unknown as benign* would poison the FPR estimate.
4. **`drop_bad_timestamp`**: rows whose `first_submission_date` cannot be parsed are dropped — time order and late labels need it.
5. **`fill_non_finite`** (missing/infinite rule): NaN/±inf feature values are replaced with `0.0` (`non_finite_fill`) and counted; rows are kept — thrember emits 0 for absent PE structures, so 0 is the extractor's own "missing" value; the local PDF smoke run measured 0 — *dropping rows* (would bias towards well-formed files) / *mean imputation* (needs train statistics in pass 1).
6. **`keep_outside_week_timestamps`**: rows dated outside their weekly file (Stage 0: Win32 23, Win64 13, all 1 day early) are **kept in the week the dataset authors assigned**, counted — the published split defines the weeks; moving them would change the official test set, and none is dated after its week (no future information) — *re-bucketing by timestamp*.
7. **`drop_cross_week_repeat`**: the same `sha256` in a later week of the same split is dropped (earliest kept) — a file must be scored once, at its first appearance (Stage 0: Win32 6, Win64 7 in test).
8. **`drop_test_id_in_train`** / **`drop_test_vector_in_train`** (leakage): test rows whose `sha256` or exact feature vector occurs in any train week are removed from test — otherwise test recall is inflated by memorised files; a unit test fails if any test-week row hash appears in train weeks.
9. **`drop_duplicate_vector`**: identical feature vectors (blake2b-128 of the float32 bytes) within a split keep only the earliest row; copies with a different label are counted (`label conflicts`) — the plan requires exact-duplicate removal; earliest-first respects time order — *dropping all conflicting copies* (rule is simple and the count is reported).
10. **`drop_constant_columns`**: columns with min == max over all TRAIN rows are dropped from every split — computed on train only so test weeks never influence the schema. (Min/max is taken before rules 7–9; rule 8/9 drops are exact duplicates and train has no cross-week repeats, so the result is the same.)
11. **`cast_float32`**: all feature columns are float32 — thrember's native dtype; halves memory vs float64.

Contracts, versioning, late labels
- **pandera schema (`src/sentinel/data/schema.py`)**: exact ordered columns (`sha256`, `week`, `first_seen`, `label`, `label_available_at`, `f_NNNN`…), dtypes, labels in {0,1}, unique ids, UTC timestamps, late-label delay within config range, all features finite. Every file is validated before it is written.
- **Feature columns named `f_NNNN` by original thrember index** — stays traceable to the EMBER v3 layout after constant columns are dropped.
- **`feature_schema_version` = sha256(columns + dtypes + cleaning config)[:16]**, excluding `file_types` / `keep_intermediate` (they do not change rows).
- **Determinism**: per-row random draws come from blake2b(seed, purpose, sha256) rather than an RNG stream, so results do not depend on row order or worker count; Parquet written with fixed settings; a test re-runs with 1 vs 2 workers and compares file hashes.
- **Late labels**: `label_available_at = first_seen + U[7, 28] days` (seeded per file, `late_labels` in config). Training at time T must use `sentinel.data.splits.labelled_as_of(df, T)`.
- **Never-break set**: 500 benign files from TRAIN weeks only, spread evenly over the 52 weeks (10 in each of the first 32 weeks, 9 in the rest), within a week the files with the smallest seeded draw — documented, reproducible, covers the whole year; files stay in training too (it is a regression check, not a held-out set) — *top-N by a model score* (no model yet; circular).
- **Provenance**: manifest records seed, data fingerprint (sha256 over the raw zip hashes), git commit (or bundle commit), per-file sha256, row counts per step.
- **`pandera` 0.35.0** added — the plan names it; resolved under `exclude-newer`.

## Stage 2
Owner's choice (2026-11-19): train **Win64 and Win32 separately** (one model, one Kaggle run, one registered model each).

Windows and data
- **K = 51; fit on weeks 1-47, calibrate + set threshold on weeks 48-51, validate on week 52 (K+1)** (`configs/train.yaml`) — later stages replay the TEST weeks 53-64 with v1 as production, so v1 must be built from train weeks only; the plan's golden metric is "recall at 0.1% FPR on week K+1" — *K = 52 with a test week as K+1* (would tune on test data).
- **Four calibration weeks, not one** — the plan says "a held-out calibration week"; at 0.1% FPR one Win64 week (5,000 benign) puts the threshold on its 5 highest benign scores, too noisy. Four weeks give ~4x the benign files — *one week*.
- **v1 is trained "as of" the end of week 52** (the moment the replay starts) and only sees labels with `label_available_at` <= then (Stage 1 late-label rule). Measured on PDF: 0 fit rows dropped, ~39% of calibration-week rows not yet labelled — *ignoring label delay* (would leak future labels).
- **Evaluation on week 52 and the test weeks uses all rows (ground truth)** — evaluation is retrospective; nothing measured there feeds back into the model, threshold or calibration.
- **Test weeks 53-64 are scored for the report only**; hyperparameters were fixed before any test week was scored and are not changed because of it.
- **Rebuilt splits are checked by CONTENT against `docs/cleaning/<type>.json`** (row counts per step and week, dropped columns, feature_schema_version, data fingerprint), not by file sha256 — adding MLflow downgraded pyarrow 26 -> 25.0.1, and Parquet bytes embed the writer version — *pinning pyarrow 26* (MLflow 3.17 requires < 26).
- **Parquet stores `timestamp[s]` as milliseconds** (the format has no seconds unit); the instant is identical. The Stage 1 manifests still name the column `timestamp[s, UTC]` (part of `feature_schema_version`, so unchanged); readers normalise units before comparing (a unit mix-up was caught by tests).

Models
- **LightGBM params from the official thrember grid** (`num_iterations` 1000, `learning_rate` 0.05, `num_leaves` 1024, feature/bagging fraction 0.5), thrember's categorical columns (original indices 2-6, 701, 702), `deterministic`, fixed seed; no grid search — thrember ships a grid but no single tuned set; a grid search on 1.4M x 2.5k rows does not fit the Kaggle budget — *thrember's `train_model` defaults* (100 trees: plain LightGBM defaults, not a tuned choice).
- **Fixed iteration count, no early stopping** — early stopping would use the calibration weeks twice (stopping + isotonic/threshold).
- **Logistic regression: signed log1p, standardise, clip +-10, L2 (C=1), seeded subsample of 200,000 fit rows** — EMBER counts are heavy-tailed; full Win32 as float64 is ~28 GB — *SGD over all rows* (more code, slower to converge, not needed for a baseline).
- **Isotonic calibration on the calibration weeks; decisions use the RAW score threshold** — isotonic is a step function and maps many top scores to one probability, so an FPR-controlled threshold on calibrated scores would be coarse; the calibrated probability is reported (ECE) and returned as `probability` — *threshold on calibrated score*.
- **Threshold rule: flag if score > t, t = the k-th highest benign calibration score with k = floor(target_fpr x n_benign)** — never exceeds the target on the calibration set even with ties.
- **Wilson score intervals** for recall and FPR — accurate for rates near 0 or 1 and small counts (FPR at 0.1%), unlike the normal approximation.
- **Models saved as LightGBM text + npz arrays (no pickles)**; the registry model is an MLflow pyfunc returning `raw_score`, `probability`, `verdict`.

Registry
- **Training on Kaggle produces a run bundle; registration happens locally in the sandbox** (`make train BUNDLE=...`) into an MLflow SQLite registry in `$SENTINEL_DATA_DIR/mlflow` — MLflow stores absolute artifact paths, so a registry built on Kaggle would not move; the registry belongs next to the serving stack (Stage 3) — *MLflow on Kaggle*. Params, metrics, feature_schema_version, data fingerprint and git commit are logged from the bundle; LightGBM is registered as `sentinel-<type>` and aliased `production`; LR is logged as a comparison run.
- **One registered model per dataset** (`sentinel-win64`, `sentinel-win32`) — matches "train separately"; each has its own production alias.
- **`structlog` writes to the current `sys.stderr` on every call** — the stock PrintLogger kept a stream that test runners/notebooks later close (caused a crash in tests).

## Stage 3

Hot reload (design)
- **Serving trusts only the registry alias** (`production`, `configs/serving.yaml`): a poller thread asks MLflow every 30 s which version the alias points to — *loading by version number or file path*.
- **Load first, then swap; never swap a half-loaded model.** A new version is copied to private temp space, loaded, and must pass a warm-up (score one all-zero row, finite result, right file type, no schema clash) before it takes traffic. If any step fails, the current model keeps serving and the error shows in `/v1/models` — *swap then verify*.
- **Atomic swap = rebind one immutable dict.** Requests read `{feature_schema_version: model}` once and keep that model for the whole request; a reload builds a new dict and rebinds the attribute. Readers never lock, so a reload cannot fail or stall requests. Measured: 3 alias moves under 200 concurrent requests each, 600/600 HTTP 200 (unit test with a real MLflow registry) — *a lock around every request* (slower, and a slow load would block scoring).
- **The previous model stays in memory.** If the alias moves back to it, or `rollback()` is called (admin endpoint in Stage 6), the swap is instant with no load. Slow loads do not hold the swap lock, so a rollback never waits behind a load — *reload from the registry on rollback* (seconds, during an incident).
- **Load from a copy, not in place.** MLflow writes metadata next to artifacts when loading a `models:/` URI; the api mounts the registry's file store read-only, so it downloads to its tmpfs first (found when the first `make up` refused to load; regression test added).
- **Readiness**: `/readyz` is 503 until every configured model AND the override snapshot have loaded; `/healthz` is liveness only.

Request contract
- **Requests are routed by `feature_schema_version`**, which differs for Win64 (`b9521b9b…`) and Win32 (`6bad8af2…`): clients send the version their features were built with, and the server never guesses the file type — *a separate `file_type` field* (two fields that could disagree). Unknown version → **409** with the versions being served; wrong feature count or non-finite values → **422**. Nothing is ever scored silently.
- **`score` is the model's raw score and the verdict is `score > threshold`** (the fixed-FPR threshold from Stage 2, returned with every response) — *calibrated probability* (the threshold is defined on raw scores; mixing them invites mistakes).

Overrides and lineage
- **Overrides live outside the model and always win** (`tenant_overrides`, per tenant + file, allow or block, reason required). The api keeps an in-memory snapshot refreshed every 5 s, so scoring never waits on the database; it is not ready until the first snapshot loads (otherwise an allow-listed file could be blocked at start-up); during a database outage it keeps the last snapshot. A change takes up to 5 s to apply — *a database read per request* (adds latency and makes scoring depend on DB uptime). Every override that changes a verdict is logged (`override_changed_verdict`) and counted.
- **Lineage rows are written in the background in batches** (500 rows or 1 s), from a bounded queue; a database problem never fails or slows a request. Rows that cannot be written (queue full, or a batch failing 3 retries) are dropped and counted (`lineage_dropped`) — *synchronous insert per request*. `request_id` is the primary key and inserts are idempotent.
- **Least privilege in PostgreSQL**: the api logs in as `sentinel_api` with only INSERT/SELECT on `predictions` and SELECT on `tenant_overrides` (no UPDATE/DELETE: lineage is append-only for the service; an integration test checks this). Passwords are only in `.env`.

Stack
- **Compose: api, postgres, mlflow, jaeger only** (no Prometheus/Grafana, per the plan). The api and mlflow server use our locked image (target `app` of `docker/sandbox.Dockerfile`), so the server version matches the registry that Stage 2 wrote. Postgres 17.6 and Jaeger 2.11.0 are pinned by digest.
- **Hardened like the sandbox**: read-only root filesystems, `cap_drop: ALL`, `no-new-privileges`, non-root users, tmpfs for scratch; postgres gets back only the 5 capabilities its entrypoint needs. All ports are published on 127.0.0.1 only.
- **API host port 18080 (configurable `SENTINEL_API_PORT`)**: 8000 (Splunk) and 8080 (a Java server) were already in use on this machine — the container still listens on 8000.
- **Tracing**: the Jaeger container is in the stack now; the api is instrumented in Stage 8 (one trace per event), as the plan orders.
- **Metrics (simple version)**: in-process counters and latency samples by `model_version` (`/v1/ops/stats`); percentiles come from `evaluate.latency_summary`. Stage 8 adds the writer to `ops_metrics`.
- **Sample requests for curl come from real EMBER2024 feature records** (`make sample-request`): Win64/Win32 records are not kept locally, so it vectorizes Dot_Net test records (same PE extractor) onto the Win64 model's columns with the Stage 1 cleaning. They are out-of-distribution, so they exercise the API but say nothing about accuracy.

## Stage 4

### Data and candidate
- **A candidate retrained at T may learn from test weeks whose labels are known by T; threshold/calibration only from weeks <= T; judged only on weeks after T** (owner, 2026-11-20). v1 and reported baselines never see test weeks — *forbid test weeks entirely* (a model retrained in week 60 could never learn from weeks 53-59, which is unrealistic).
- **First candidate: policy `last_n_weeks` (26), T = end of week 52, 4 calibration weeks (49-52)** (`configs/retrain.yaml`) — tests "does recent data alone beat a full-year model?", and T = 52 lets it be judged on all twelve test weeks. `all_history` and `recency_weighted` are implemented and tested, not yet trained.
- **Test weeks come from a Kaggle export, not a local rebuild**: the Kaggle run rebuilds the splits, checks them by content against `docs/cleaning/Win64.json`, and zips weeks 53-64 + the never-break set with a sha256 per file. `make import-replay` accepts only allow-listed names, re-hashes every file, checks each week's hash against the shipped manifest and the manifest content against `docs/cleaning/`, and installs nothing until every check passes — *trusting the zip* (it is downloaded data).
- **`make register-candidate` refuses bundles that are not `kind: candidate`, have no `identical content` rebuild check, or target the `production` alias** — a candidate can never be promoted by registration; promotion is the gate's job (Stage 5).

### Stream
- **Redpanda v25.2.9 pinned by digest; topic `file-events`, 6 partitions, key = file_id** (plan: partitioned by file_id). Hardened like the rest; its data dir is a tmpfs owned by uid 101 (the replay is reproducible from the week files, so the log does not need to survive restarts).
- **Event format v1**: `event_id = "<file_type>:<sha256>"` (deterministic, so a replay re-sends the same ids), tenant = blake2b(file_id) mod 200, ring from a stable hash of the tenant (ring0 5%, ring1 25%, ring2 rest), features as base64 float32 little-endian. **Events carry no label** — consumers cannot see ground truth; evaluation joins labels later from the week files.
- **Producer order = (first_seen, file_id)**, optional pacing (`events_per_second`, 0 = as fast as possible); broker idempotence on; it fails loudly if any delivery fails (a replay with holes would make every comparison meaningless).
- **At-least-once + idempotent writes**: consumers commit offsets only after rows are stored; duplicates from re-delivery or re-replay hit unique `(event_id, model_version)` and `ON CONFLICT DO NOTHING` — *exactly-once transactions* (more moving parts for the same result).
- **Live consumer calls the serving API** (as the plan says), passing `event_id` (new optional request field written to lineage). 5xx/timeouts retry with capped backoff; 4xx are counted and skipped (retrying cannot fix them).
- **Shadow consumer is a separate container, consumer group and DB login** (`sentinel_shadow`: INSERT/SELECT on `shadow_predictions` only). It loads the `candidate` alias with the same ModelManager as serving, scores whole batches, and waits without consuming until a candidate exists. Measured: killing it mid-replay left live complete; it caught up from its committed offset with no duplicates.
- **Commit only when a batch contains at least one real message** — a batch of broker-only errors (e.g. topic not yet created) has no offset to commit and crashed the first version.

## Stage 5

### The statistics, in plain words
- **Why a fixed false-positive rate.** A malware model gives every file a score; where you put the cut-off decides how many malware files you catch AND how many clean files you wrongly block. Any model can "catch more malware" by blocking more clean files, so comparing two models at their own cut-offs, or at 0.5, mixes up "better model" with "more trigger-happy cut-off". The gate therefore sets, for EACH model, the cut-off that blocks exactly 0.1% of the clean files in the evaluation (`operating_fpr`, the same rule as Stage 2: the k-th highest clean score, k = floor(0.001 × clean files)), and compares how much malware each catches there. Same false-alarm budget, so the only thing that differs is how well the model separates malware from clean files — *AUC* (averages over cut-offs nobody uses) or *accuracy at 0.5* (meaningless at a 0.1% false-alarm budget).
- **Using test-week labels to set that comparison cut-off is evaluation, not tuning.** It is computed on weeks after T for both models alike, never feeds back into either model, and the model's shipped threshold is not changed by it.
- **Why also check the shipped thresholds.** Users get the verdicts of the threshold that actually ships, so `false_alarms` compares both models' real false-alarm rates (candidate may be at most 10% worse, relative) and `disagreement` compares their real verdicts.
- **Why McNemar.** Both models score the SAME files, so most files get the same answer from both and say nothing about which is better. Only the files where they disagree matter: "production right, candidate wrong" (b) and "candidate right, production wrong" (c). If the models were equally good, each disagreement would be a coin flip, so b and c should be about equal. McNemar's exact test asks how surprising the observed split is under that coin-flip assumption (two-sided binomial test of min(b, c) out of b + c, p = 0.5). A small p-value (< 0.05, from `confidence: 0.95`) means the difference is real, not luck. It is done separately for malware (missed detections) and clean files (false alarms), because one kind of error cannot pay for the other — *a two-sample test on the two recall numbers* (ignores that the files are paired and is much less sensitive).
- **Exact, not the chi-square formula.** With a handful of disagreements the usual chi-square approximation is wrong; the exact binomial is computed with whole numbers (`evaluate.mcnemar_exact`) and checked against `scipy.stats.binomtest` in tests.
- **Evidence of harm is enough to reject; proof of safety needs volume.** A candidate significantly worse on either class fails even with few disagreements. Otherwise fewer than 30 disagreements (`min_disagreements`) is INSUFFICIENT_EVIDENCE: with so few, a real difference cannot be told from noise, and "no difference seen" is not "safe".
- **Wilson intervals** on every recall/FPR (as Stage 2): honest near 0% and 100% and for small counts.
- **Per-week check** (`max_weekly_recall_drop` 2 points at the fixed FPR): a candidate that is fine on average but collapses in one week is rejected. Each week has ~5,000 clean files, so its 0.1% cut-off rests on ~5 false alarms: noisy by design, which is why it allows 2 points. A week with too few clean files for even one false alarm is "insufficient".
- **Calibration (ECE) is a SOFT check.** Verdicts use the raw score and threshold; the calibrated probability is only reported. A worse ECE is named in the reason but cannot block (configurable via `soft_checks`).
- **Labels are used only when known.** The gate decides `label_wait_days` (28 = the longest label delay) after the last evaluated week and keeps only labels available by then; a model is never evaluated on a week ≤ its `as_of_week`.

### Decision and record
- **Rule**: any hard fail → REJECT; else any hard "insufficient" → INSUFFICIENT_EVIDENCE; else PROMOTE. The reason quotes each deciding check's plain-English summary with its numbers.
- **Same events only**: live (production) and shadow (candidate) rows are joined on `event_id`; coverage below 99% of the evaluated weeks' events is "insufficient". Stored thresholds must equal the registered models' thresholds, or the gate stops.
- **Evidence bundle = canonical JSON** (sorted keys, no NaN) with policy (incl. `policy_version`), both model versions + lineage tags, data fingerprint + sha256 of every week file used, all metrics and intervals, every flipped file, decision and reason. Its SHA-256 and the exact text are stored in `gate_decisions`; `docs/gate/<id>.json` holds the same bytes and `<id>.md` a readable report.
- **Append-only in the database itself**: triggers block UPDATE/DELETE/TRUNCATE for every role (owner included), and a CHECK constraint makes PostgreSQL recompute the hash on insert. `make gate-verify` re-hashes every stored decision and compares the report files. Integration tests run in rolled-back transactions so test rows never become permanent.
- **Least privilege**: `sentinel_gate` reads predictions/shadow_predictions and can only INSERT/SELECT gate_decisions.
- **PROMOTE moves `candidate_approved` only**; `production` is never touched by the gate (Stage 6 rolls out).

### Win32 added (2026-11-21)
- **One policy, judged per file type.** `gate_policy.yaml` lists `file_types: [Win64, Win32]`; the same rules (still `policy_version` 1.0.0, no rule changed) judge each type on its own events, labels and never-break set: `make gate CANDIDATE=<v> FILE_TYPE=Win32`. Win64 and Win32 never share a decision — they are separate datasets with separate models (Stage 1 owner decision), and pooling them would let a good Win32 result hide a bad Win64 one. With several types listed the gate refuses to guess (`--file-type` required).
- **One shadow consumer, one candidate per type.** `replay.yaml` lists `file_types`; the shadow consumer loads the `candidate` alias of each and routes events by `feature_schema_version` (as serving does). It consumes nothing until EVERY listed type has a candidate, so no event is read and skipped while one is missing. The producer and `replay-counts` publish/count one type per run (`FILE_TYPE=`, default Win64).
- **Win32 statistics are sharper (Win32 note)**: ~15,000 clean files per week (Win64 ~5,000), so each week's 0.1% cut-off rests on ~15 false alarms instead of ~5. The thresholds are not changed per type: one policy keeps the two decisions comparable.

## Stage 6

### Routing and aliases
- **The API computes the ring itself** from tenant_id (the same stable blake2b hash and `replay.yaml` ring shares as the producer), so a client cannot choose its ring — *trusting a ring field in the request* (any caller could opt out of ring0).
- **One registry alias per ring** (`ring0_model`, `ring1_model`, `ring2_model`), each with `production` as fallback; one `ModelManager` per ring, so hot reload, atomic swap and in-memory previous model all work exactly as in Stage 3. A missing ring alias serves production (the safe default) — *one alias plus a percentage in the API* (state would live in the API process and be lost on restart; the registry is the single record of what serves where).
- **One shared model cache** (weak references) across the three rings: a version is loaded once and freed when no ring holds it; moving a ring back to a version another ring serves is instant. Measured: rings 1 and 2 load in ~0.1 s after ring0, API memory unchanged (322 MiB).
- **Lineage stores the ring** (`predictions.ring`), so every watch signal is a SQL query on what really happened.

### Controller
- **States** APPROVED → RING0 → RING1 → RING2 → COMPLETE, plus ROLLED_BACK, in an **append-only** `rollout_transitions` table (triggers block UPDATE/DELETE/TRUNCATE for every role; CHECKs force seq 1 = APPROVED with no from-state; unique (rollout_id, seq)). Each row has the reason and the full signals behind it.
- **Write the transition first, then reconcile the aliases to it on every tick** (`desired_aliases(state)` is a pure function). A crash between the two, or a race with the kill switch, heals on the next tick — *moving aliases first* (a crash would leave a ring on a model with no record of why). Writers take a PostgreSQL advisory lock per file type and must name the transition they extend, so a stale controller cannot overwrite the kill switch.
- **Entry ticket**: the gate's PROMOTE for exactly this candidate vs the CURRENT production, plus the `candidate_approved` alias. Demo versions tagged `demo=true` may skip the gate with `--demo`, and the APPROVED row says so in words.
- **Production moves only on COMPLETE.** A rollback points the rings back at production; production is never touched by a rollback.

### Watch signals (configs/rollout.yaml)
- **Breach rolls back at once; advancing needs enough events AND every signal passing** (`min_events` 300 / 1,500 / 3,000 for ring0/1/2). One bad canary in the first minute is enough; "looks fine so far" with 20 files is not.
- **Never-break canaries**: once the API confirms a ring serves the candidate, the controller scores the 500 never-break files through the real API as a canary tenant of that ring (tenant chosen so the stable hash lands in that ring). Any flag → rollback. Canaries are counted under separate `canary_*` API metrics and excluded from flag-rate, error and latency signals.
  - Why canaries at all: measured on Win64 week 56, a model shipped with the default 0.5 cut-off raises false alarms 6× (0.22% → 1.26%) but moves the overall flag rate only 45.4% → 49.5%, because the replay is half malware. A flag-rate signal on ring0's few hundred files cannot see that; one never-break hit can. Real traffic is mostly clean, where flag rate would be far more sensitive; this replay is not.
- **Flag rate vs production on the SAME period** (model verdict score > threshold, Wilson intervals): breach only when the candidate's lower bound clears production's upper bound × 1.10 (same relative margin as the gate's false-alarm rule). The comparison set is production-scored traffic since the current ring switched (rings still on production): the malware share of the stream changes within a week, so a longer baseline is not comparable (found in the demo, see ROLLOUT_DEMO.md). In the last ring nothing runs on production alongside it, so flag rate is not compared there (it was, on the same period, in ring0 and ring1); canaries, errors and latency still apply.
- **Errors**: 5xx share of the candidate's requests; breach when its lower Wilson bound exceeds 1%. **Latency**: candidate p99 ≤ production p99 × 2 + 25 ms with ≥ 200 samples, from the API's in-process metrics (`/v1/ops/stats`; Stage 8 moves them to ops_metrics). Both are cumulative since the API started; documented limitation.

### Kill switch
- **POST /admin/rollback** with header `X-API-Key` (secret in `.env`, compared in constant time; unset = endpoint disabled, 503). It records ROLLED_BACK for any rollout in progress first (so the controller cannot move the rings forward again), points every ring alias at production (even if recording failed — safety first), and reloads at once instead of waiting for the 30 s poll. The API's DB login may only INSERT/SELECT `rollout_transitions`.
- `make demo-reset` undoes a completed DEMO rollout (refuses unless production is a version tagged demo=true), so the demo leaves the registry as it found it.

## Stage 7

### Paired scoring in the rollout (owner request, 2026-11-22)
- **Production scores the same files in candidate rings.** In a ring serving a model other than `production`, the API also scores each file with production (a 4th `ModelManager` on `production` only, sharing the model cache, so nothing extra is loaded). It stores `reference_version`, `reference_score` and `reference_verdict` (the model verdict, before overrides) in `predictions` (migration 009). Why: comparing candidate files in one ring with production files in other rings drowns small changes in file-mix noise (Stage 6 Finding 2). On the same file at the same moment, mix and drift cancel out. Rejected: a lower flag-rate tolerance, which would bring back the false alarms. Rejected: shadow-scoring the candidate in a separate consumer, which doesn't follow ring routing and adds a process.
- **A failed reference score never fails the request.** It is logged and stored as missing, which the controller sees as low coverage ("waiting", needs 95%), never as a disagreement.
- **Signal `disagreement`**, in two directions: new flags (candidate flags, production clears) and cleared (the reverse). It breaches when the live rate's lower Wilson bound exceeds the approved rate's upper Wilson bound + 1 point (absolute; `max_disagreement_increase`).
  - The approved rate comes from the gate decision that opened the rollout (its per-class disagreement tables at shipped thresholds) and is recorded in the APPROVED row.
  - For a demo copy of production it is 0, with no uncertainty.
  - Why relative to approval: labels arrive days later, so a disagreement can't be judged right or wrong live. Only "behaves as approved" can be checked.
  - Why an absolute margin: the expected rate can be 0, where a relative margin is meaningless.
  - Statistic: `evaluate.disagreement_check` and `expected_disagreement_upper`.
- **Covers ring2.** Production still holds the `production` alias until COMPLETE, so it scores alongside the last ring as well.
- **`--no-canaries` is demo only.** It exists to show the new signal on its own. A rollout without canaries can roll back but never advance (never-break stays waiting).
- Cost: one extra model call per request, in candidate rings only, counted in the candidate's latency (p99 126 vs 103 ms in the demo, under replay load). Stage 8 measures it.

### The main experiment (configs/experiment.yaml)
All of these were fixed before any experiment result existed. Nothing is tuned on test weeks.
- **Win64 only.** 60,000 benign test files, enough for 0.1% FPR over 4-week windows (about 20 benign files at the limit). Win32 would triple the Kaggle time (~45 h of retraining). Rejected for now; the same code runs it with `file_type: Win32`.
- **18 real retrains, then cheap repeats.** One candidate per (T, retraining policy), T = 52, 54, ..., 62, trained on Kaggle with the Stage 4 code unchanged (same LightGBM settings as v1). "20+ seeds" is met with **50 bootstrap resamples of the weekly streams**: files drawn with replacement within each week, one draw shared by all models, so comparisons stay paired. Retraining per seed would need 18 × 50 retrains. Rejected. Consequence: the intervals cover sampling noise in the stream, not training randomness.
- **Scores leave Kaggle, features don't.** The export holds each model's raw score and calibrated probability for every file of weeks 45-64, never-break scores, labels, weeks, label times and file hashes. The local simulation needs nothing else and runs in seconds per resample. Rejected: running the simulation on Kaggle, which would mean re-running hours of training to change one report line.
- **Each policy runs its own production chain** per retraining policy, starting from v1 at week 53. A candidate is judged against the model it would replace in that chain.
- **When decisions take effect.**
  - P1 and P2 decide at T and serve from T+1.
  - P3 shadows weeks T+1..T+2, then waits the gate's own `label_wait_days` (28) for those labels, so it decides at T+6 and serves from T+7.
  - The gate's delay is a real cost and is kept, not hidden. Consequence: P3 decides only 3 of the 6 candidates per chain before the data ends (T = 52, 54, 56).
- **P2** compares AUC on the candidate's own calibration weeks (the newest 4 weeks <= T, labels known by T). Those are the only labelled weeks the candidate did not fit on. Production may have calibrated on some of them; AUC on raw scores is not changed by a monotone calibrator.
- **P3 uses the gate unchanged.** `run_checks` + `decide` with policy 1.0.0, offline. Every event has both scores, so coverage is 100%.
- **Harmful** = over the 4 weeks after going live (capped at week 64), recall at 0.1% FPR is below the replaced model's (margin 0), or the model flags any never-break file at its shipped threshold.
  - Recall at fixed FPR sets each model's threshold on the judged weeks themselves. This is a measurement of ranking quality after the decision, never an input to one (the gate's check (a) works the same way).
  - Margin 0 is the plan's definition ("worse"). It counts small losses as harmful, which is strict on every policy alike.
  - **Good update blocked** = a candidate not promoted that would have been better (higher recall, no never-break hit) against the same production.
- **Drift alerts** are measured at T on what arrived, with no labels.
  - Features: PSI of v1's top-20 features (by gain), decile bins fixed on v1's fit weeks, on the 2 weeks ending at T vs the 4 weeks before. An alert needs 3 or more features above 0.25.
  - Scores: production's raw scores on the same windows, bins from the baseline window. An alert needs PSI above 0.10.
  - Thresholds are textbook rules of thumb, not fitted. Precision is judged on P1's candidates, where each candidate appears once against its predecessor.
- **Leakage guard.** `check_no_test_week_leakage` refuses to run if v1 touched a week >= 53, or if a candidate's fit, threshold or calibration weeks pass its T. The gate and the harm judgement assert that they never evaluate a model on weeks it learned from.

## Stage 8

### Metrics in PostgreSQL (configs/monitoring.yaml)
- **One recorder, two views.**
  - `snapshot()` (since start) keeps serving GET /v1/ops/stats.
  - `drain()` (since the last drain) feeds `ops_metrics` every 10 s, one row per active series.
  - Rejected: a second recorder for the window, which would mean double bookkeeping on the request path.
- **Bounded window.**
  - At most 20,000 latency samples per series per window. Count and sum stay exact; extra samples are dropped and counted (`metrics_samples_dropped`).
  - Rejected: a reservoir sample, which would make percentiles random for no gain at 500 req/s (5,000 per window).
- **Database down = drop and count, never wait.**
  - A failed write discards that window and adds `ops_metrics_dropped` to the next one.
  - Rejected: retrying a window, which can only pile up behind an outage and would end up costing memory in the request process.
- **Gauges** (consumer lag) are stored as one sample per window: `sum/count` is the value.
- **Percentiles** are computed per 10-second window with numpy's method (`evaluate.latency_summary`).
  - A longer period's p99 cannot be rebuilt from window summaries. Two places therefore use an explicit approximation:
    - the rollout's health check uses the count-weighted mean of window p99s, the same estimator for candidate and production;
    - retention keeps the max of the window p95/p99 (an upper bound, so spikes stay visible) and a count-weighted mean for p50.

### Retention
- A daily job in the alert worker, also run once at start. It moves 10-second rows older than a day into 1-minute rows in ONE statement (`DELETE ... RETURNING` feeding `INSERT`), whole minutes only, then deletes rows older than 14 days.
- `ops_metrics` is operational data, not an audit table: only `sentinel_monitor` may delete from it.

### Alerts (configs/alert_rules.yaml)
- **"For N minutes"** means:
  - the condition held in every 10-second bucket with data in the last N minutes;
  - the data reaches back N minutes;
  - the newest bucket is under 60 s old.

  No recent data means not firing: an alert never stays up on stale numbers, and a quiet service resolves its latency alert.
- **Error rate** is computed only on buckets with at least 20 requests. Below that, 1 error in 50 requests would page.
- **Consumer lag "rising for 10 minutes"** requires a positive least-squares slope over the span AND in each of its five 2-minute segments.
  - Found on the stack (2026-11-23): with one slope over the whole span, 6 flat minutes followed by a 4-minute climb fired after 4 minutes. Regression test added.
- **Never-break hit** fires on any canary the model flags (before overrides). It resolves after 10 minutes without one. The API counts `never_break_hits`; the canaries' own traffic stays in `canary_*` metrics as before.
- **Storage.**
  - Only transitions are stored: append-only `alerts`, FIRING / RESOLVED.
  - The database itself refuses a duplicate state or a first RESOLVED (trigger with a per-key advisory lock), so two workers cannot both page.
  - The webhook is optional (empty URL = off); a webhook failure is logged and never blocks the record.
- **Same signals for the rollout.**
  - The Stage 6 controller now reads request, error and latency health from `ops_metrics`, counted from the moment the ring switched, instead of the API's in-memory totals since process start.
  - Those totals were wrong after an API restart and mixed in traffic from before the ring changed.

### Tracing
- OpenTelemetry SDK, OTLP over HTTP to Jaeger all-in-one (no gRPC stack).
  - The live consumer opens `consume` per event (retries included) and sends `traceparent`.
  - The API continues it in `score`, with a `log` child for the lineage hand-off, and stores the trace id in `predictions.trace_id`.
  - Without a configured endpoint the tracer is a no-op and `trace_id` is NULL.
  - Export is batched with a bounded queue: dropped spans, never a blocked request.
- The shadow consumer is not traced. Its scores never reach a caller, and its rows are already linked by `event_id`.

### Dashboard
- Streamlit with Altair (bundled), reading as `sentinel_dashboard` (SELECT only).
- Every query has a name, so pages are tested with AppTest on canned frames. `make dashboard-check` renders them against the live database.
- Short `st.cache_data` TTL (10 s), with the window end floored to it so reruns hit the cache.
- Latency-spike rows (bucket p99 above the alert limit) link to the Jaeger search for that service and time window.

### Serving under load (docs/LOAD_TEST.md)
- **4 uvicorn worker processes** (`serving.yaml` `workers`). Scoring is CPU-bound Python: one process saturated at about 125 req/s under one GIL.
  - Each worker loads its own models: 1.3 GB in total.
  - Each worker writes its own `ops_metrics` rows. Readers aggregate them: counts summed, the worst worker's p99 for the chart and the alert.
  - Rejected: one process with more threads. The GIL serialises them, which the profile showed.
- **Registry poll 30 s -> 5 s.** The kill switch reaches one worker, which reloads at once; the others follow on their next poll. The kill CLI times "restored" only when every worker (`X-Sentinel-Worker` header on GET /v1/rings) reports production.
  - Cost: about 0.4 core of MLflow load from alias polling.
- **Request body.** Decoded and validated in one pass by pydantic's JSON validator. FastAPI's default (stdlib `json.loads` + object validation) cost about 4x the CPU on a 2,500-number body. Same 422 on a bad body.
- **`httptools` + `uvloop`.** uvicorn's C HTTP parser and event loop; without them it falls back to pure-Python h11.
- **Measured, not met.** The 500 req/s / p99 < 20 ms target was not reached on the shared 8-core laptop: 306 req/s, all requests answered, 0 errors.
  - Owner decision 2026-11-24: record the measured numbers.
  - Not done: sampling root-request traces, more workers.
