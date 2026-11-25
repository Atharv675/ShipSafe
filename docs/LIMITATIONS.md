# Limitations (Stage 10 reviewer pass, 2026-11-25)

A strict review of the code, the tests and the claims. Each point names where it shows and what would close it.

**Fixed now:** the weak points that took under an hour to fix are marked **FIXED**, with the change.
**Documented:** the rest are documented here, not hidden.

## The 10 weakest points

| # | Weak point | Kind | Status |
|---|---|---|---|
| 1 | Load target not met: 306 req/s and p99 > 20 ms, against 500 req/s and p99 < 20 ms | claim / performance | documented |
| 2 | Release-policy evidence is thin: one file type, one 12-week stream, 18 candidates | claim | documented |
| 3 | The release gate and P4 block good candidates; P4 as specified shipped nothing | design | documented |
| 4 | Stage 7 "5 of 18 harmful" was still stated as a finding after Stage 7b showed it is noise | claim | **FIXED** |
| 5 | Dashboard SQL was only tested against canned frames, never in the suite against PostgreSQL | test gap | **FIXED** |
| 6 | The gate logged `docs/gate/...` as the report path even when writing elsewhere | code | **FIXED** |
| 7 | No CI: tests run only when someone runs `make` | process | documented (owner decision) |
| 8 | Early labels are simulated; signer identity does not exist in EMBER2024 | data | documented |
| 9 | Dashboard page time is close to the 3 s budget and noisy; first view after a restart is slow | performance | partly fixed |
| 10 | `predictions` / `shadow_predictions` grow without retention | operations | documented |

### 1. Load target not met
- **Measured result** ([LOAD_TEST.md](LOAD_TEST.md)):
  - the API reached 306 req/s on the 8-core development laptop, against a 500 req/s target;
  - p99 stayed above 20 ms in every 10-second bucket;
  - 0 failures and 0 lost lineage rows.
- **What was fixed in Stage 8:** 4 workers, httptools/uvloop and pydantic JSON parsing (125 → 306 req/s).
- **What is left:**
  - JSON decoding of ~2,500 numbers per request;
  - one LightGBM prediction per request.

  Binary request bodies, request batching or more hardware would be the next steps. Each needs a new measurement; none can be claimed from here.

### 2. Thin evidence behind the release-policy conclusions
- All experiment conclusions ([RESULTS.md](RESULTS.md), [BACKTEST.md](BACKTEST.md)) come from:
  - Win64 only;
  - one 12-week stream;
  - 18 candidates.
- The bootstrap resamples files within weeks. It cannot create new weeks or new drift.
- Intervals are wide, and most differences are "within noise". Nothing here is proven beyond these data.

### 3. The gate and P4 are too conservative
- **Stage 7:** the gate's disagreement check cannot tell "different" from "worse". It blocked 5 of 7 candidates that later proved better.
- **Stage 7b:** P4's zero-tolerance must-catch rule blocked 15 of 18 decisions. At a matched FPR, two models always disagree on some malware.
- **No silent fix:** fixing either means a new pre-registered policy judged on new weeks. Changing thresholds after seeing these results would be tuning on the judged weeks.
- **Recommendation on the evidence so far:** recency-weighted retraining, P1/P2 promotion, weekly capped recalibration and the ring rollout as the safety net.

### 4. FIXED: outdated Stage 7 claim
**Problem:** `docs/RESULTS.md` still read "auto-promotion shipped a worse model 5 times in 18". Under the point-in-time rules, a measured noise margin and family-clustered CIs, none of those harms holds up.

**Fix:** a dated banner at the top of the conclusions points to BACKTEST.md. The original text is kept for the record.

### 5. FIXED: dashboard SQL untested against the database
**Problem:** the Stage 8 bug ("malformed array literal" through the cache) only showed on the live stack.

**Fix:** `tests/integration/test_dashboard_sql.py` runs every dashboard query (Operations and ML), with real parameters and lists, as the read-only `sentinel_dashboard` role. It covers:
- the queries themselves;
- the cached path;
- that the dashboard role cannot write.

### 6. FIXED: misleading gate log
**Problem:** `sentinel.gate.run` logged `report: docs/gate/<id>.md` even when `--out` pointed elsewhere (the demo writes to its own data folder).

**Fix:** it now logs the real path.

### 7. No continuous integration
**Owner decision 2026-11-25:** no GitHub Actions workflow. Lint, mypy (strict), unit tests (coverage about 95%) and integration tests run through `make lint test test-integration` in the hardened sandbox, but only when someone runs them.

**Mitigation:** the pre-commit hooks (lint and gitleaks) run on every commit once installed (`pre-commit install`).

### 8. Data that does not exist
- **Early labels:** EMBER2024 has none. The P4 stage-2 check and step 3 of the backtest use final labels flipped at a stated rate, marked SIMULATED everywhere. They say nothing about real early labels.
- **Signer identity:** the Authenticode block has 8 numeric fields. "Top signers" was replaced by signature status (unsigned / self-signed / signed), owner decision 2026-11-25.
- **Untested assumption:** malware without a family (about 20%) is its own bootstrap cluster.

### 9. Dashboard speed
**Measured** (`make dashboard-check`, live stack):
- most pages load in 0.1–2.6 s on a warm cache;
- single readings of ML Overview and Head-to-head reached 3.2–3.6 s when the 10-second query cache expired mid-check;
- the first view after a server start takes 8–13 s, because `import mlflow`, the registry calls and the 7-day scan happen once.

**Partly fixed:** the registry and the 7-day aggregate are now cached for 60 s.

**Not done:** a materialized hourly rollup, which would need write access for a refresher job.

### 10. No retention on prediction lineage
- `ops_metrics` is downsampled after 1 day and deleted after 14. `predictions` and `shadow_predictions` keep every row.
- That is right for an audit trail during this project, but it grows without bound.
- A retention or partitioning policy (for example, monthly partitions and a stated keep period) is an owner decision. Lineage may be needed for investigations.

## Further known limitations
- **Head-to-head models:** the page compares the backtest models (v1 and the 18 point-in-time candidates), whose per-file scores and labels exist in `results/`. Registry versions 2–5 were scored only on replayed weeks, and their labels are not in PostgreSQL.
- **The demo is a demonstration, not a result.** `make demo` uses PDF:
  - its test weeks have about 500 benign files each, too few for 0.1% FPR, so the demo gate runs at 1%;
  - it uses 150 trees × 64 leaves instead of 1000 × 1024 (`configs/demo/`).
- **Demo duration depends on the machine.** On this laptop it was measured with the real stack also running; see PROGRESS (Stage 10).
- **Docker memory (8 GB on this laptop) does not hold both stacks plus a test run.** With the real stack, the demo stack (about 4 GB) and a 6 GB test sandbox together, the Docker engine stopped answering (HTTP 500 on every call, 2026-11-25). It recovered on its own; no data was lost. Run one stack at a time: `docker compose -p sentinel-demo stop` (or `make down`) before tests or the other stack.
- **Admin endpoint auth:** the kill switch (`POST /admin/rollback`) is protected by one API key, compared in constant time. There are no roles or audit of the caller beyond the transition record.
- **Read endpoints are open on localhost:** `/v1/models`, `/v1/rings` and `/v1/ops/stats` need no authentication. They are bound to 127.0.0.1 only.
- **Lineage can drop rows by design:** under a database outage, lineage writes go through a bounded queue. When it is full, rows are dropped and counted (`lineage_dropped`), so scoring never blocks. `make db-outage-check` shows the behaviour.
- **Windows paths:** on Windows the Makefile needs Git Bash with `MSYS_NO_PATHCONV=1`, and zip paths without spaces.
- **Host Python, twice:** everything ran in the hardened sandbox except two read-only host `python` calls during the project (one pretty-print in Stage 7, one key listing in Stage 7b). Both are disclosed in PROGRESS.
- **PDF manifest provenance:** `docs/cleaning/PDF.json` comes from the local Stage 0/1 smoke cleaning of PDF. It is byte-identical to `$SENTINEL_DATA_DIR/smoke/PDF.json`, and is used only by the demo.
