# Progress

> **HANDOFF (2026-11-19)**
> - **Done:** Stage 0 complete. All six file types measured (PDF/ELF/APK/Dot_Net locally in the sandbox; Win64/Win32 on Kaggle with the code bundle, commit `848a628`). `docs/DATASET_REPORT.md` covers all six, with a plan-assumption check and a recommendation. lint/mypy clean, 43 tests, 97% coverage.
> - **STOPPED, waiting for the owner:** (1) which file type(s) to use (recommendation: Win64 or Win32; PDF has only 6,000 benign test files); (2) where Stage 1 runs (Win32 is likely too big for the local 6 GB sandbox; Win64 may fit).
> - **Open issues for Stage 1:** raw records duplicated ×2 (dedupe by sha256; repeats differ only in caps/ttps/mbc, 0 label conflicts); files repeated across test weeks (Win32 6, Win64 7, Dot_Net 47, ELF 11); test rows dated 1 day before week start (Win32 23, Win64 13, Dot_Net 881, ELF 322).
> - **Before uploading anything to Kaggle/Colab:** ask the owner (outward-facing).

| Stage | Title | Status |
|---|---|---|
| 0 | Repo setup and dataset check [STOP] | DONE (awaiting owner choice) |
| 1 | Data cleaning and data contracts | TODO |
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

### Issues found and fixed
- `thrember` import chain broke in a clean environment: oscrypto 1.3.0 vs OpenSSL 3.0.22, signify 0.9 removed `SignedPEFile`, LightGBM needs libgomp1. All fixed (DECISIONS.md).
- xet download error mid-file → retries + persistent chunk cache.
