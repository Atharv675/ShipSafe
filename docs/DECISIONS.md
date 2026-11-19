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
