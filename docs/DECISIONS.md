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
- **Cleaned data is rebuilt on Colab each session, not stored** — free Drive (15 GB) cannot hold Win32; the pipeline is deterministic and manifest hashes prove a rebuild is identical — *Google Drive storage*.
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
