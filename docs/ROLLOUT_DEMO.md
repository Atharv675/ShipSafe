# Stage 6 rollout demo (real stack, 2026-11-21/22)

All runs used the running compose stack (API, Redpanda, live and shadow consumers, PostgreSQL, MLflow). Test weeks that had never been replayed were streamed through it at 300 events/second (`make replay ... EPS=300`) while the controller (`make rollout CANDIDATE=<v> DEMO=1`) watched and decided. Every row below is copied from the append-only `rollout_transitions` table (`make rollout-timeline`); the full tables, with every signal, are in [docs/rollout/](rollout/).

**Demo models** (`make demo-models`), built from production's own artifacts with no retraining. They are tagged `demo=true`, which is the only way to skip the release gate, and each rollout's first row says so:

| Model | What it is | Why |
|---|---|---|
| `sentinel-win64/4` **BAD** | Win64 v1 shipped with the default 0.5 cut-off | A classic mistake. Measured on week 56: false alarms go from 0.22% to 1.26% (6×), and 1 of the 500 never-break files is flagged |
| `sentinel-win64/3`, `sentinel-win32/3` **GOOD** | Identical copies of v1 | Nothing can be wrong with them, so any rollback of these is a false alarm |

Rings: ring0 = 5% of tenants, ring1 = 25%, ring2 = the rest. A ring advances only after the candidate has served at least 300 / 1,500 / 3,000 real files there and every watch signal passes. Any breach rolls back at once.

## 1. Bad model: rolled back in ring0, before ring1

`Win64-v4-20261121T134119Z` ([full table](rollout/1-bad-model-canary.md))

| t (s) | Change | Reason |
|---|---|---|
| 0.0 | → APPROVED | demo approval (gate skipped, recorded) |
| 0.2 | APPROVED → RING0 | `ring0_model` → v4 |
| 27.5 | RING0 → **ROLLED_BACK** | [never_break] candidate flagged 1 never-break file (allowed 0) |

- The controller sent the 500 never-break canaries as soon as the API reported ring0 serving v4 (16 s after the alias moved, within the 30 s reload poll). One was flagged, and the rollback followed 11 s later.
- **Exposure:** 53 real ring0 files were scored by v4 (32 of them flagged). Ring1 and ring2 never saw it. The API stopped serving v4 12 s after the rollback (lineage: last v4 verdict at 13:42:00, rollback at 13:41:48).
- The flag rate could NOT have caught this in ring0. The replay is half malware, so the 0.5 cut-off moves the flag rate only from 45.4% to 49.5%, and a few hundred ring0 files can't tell that apart from noise (unit test `test_flag_rate_hides_a_6x_false_alarm_rise_on_balanced_traffic`). The canaries are what make the bad model visible.

## 2. Kill switch: every ring restored in 0.65 s

`Win64-v3-20261121T181708Z` ([full table](rollout/3-kill-switch.md))

| t (s) | Change | Reason |
|---|---|---|
| 0.0 | → APPROVED | demo approval |
| 0.6 | APPROVED → RING0 | `ring0_model` → v3 |
| 130.3 | RING0 → RING1 | ring0 healthy: 0/500 canaries flagged; flag rate 53.98% of 339 files (CI 48.66–59.21%) vs production 46.75%; 0 errors; p99 42.5 ms vs 50.6 ms |
| 152.8 | RING1 → **ROLLED_BACK** | **kill_switch**: "Stage 6 demo: operator kill switch during ring1" |

- `make kill-switch` called `POST /admin/rollback` with the API key, then polled `GET /v1/rings`. **Every ring served production v1 for both file types after 0.65 s** (requirement: under 60 s).
  - Ring0 and ring1 swapped back to v1, which was still in memory (`swapped_to_previous:1`), without waiting for the 30 s poll.
  - The rollout was recorded as ROLLED_BACK by `kill_switch`, and the controller stopped on its next tick.
- Ring0's flag rate for this *identical* model was 54.0% of 339 files against production's 46.8%. That's sampling noise in a small ring. The Wilson-interval rule correctly treated it as not a breach (candidate lower bound 48.7% < limit 52.6%).

## 3. Good model: reaches COMPLETE

`Win32-v3-20261122T053451Z` ([full table](rollout/5-good-model-complete.md)), Win32 weeks 56–58

| t (s) | Change | Reason (abridged) |
|---|---|---|
| 0.0 | → APPROVED | demo approval |
| 0.3 | APPROVED → RING0 | `ring0_model` → v3 |
| 167.1 | RING0 → RING1 | 0/500 canaries; flag rate 32.85% of 344 vs production 33.76% (same period); 0 errors in 351; p99 54.3 ms vs 78.0 ms |
| 261.7 | RING1 → RING2 | 0/500 canaries; flag rate 40.84% of 1,589 vs 40.67%; 0 errors in 2,295; p99 48.6 vs 58.7 ms |
| 325.4 | RING2 → **COMPLETE** | 0/500 canaries; flag rate not compared in the last ring; 0 errors in 7,148; p99 45.7 vs 57.2 ms; 3,174 files |

- At COMPLETE, `production` and all three ring aliases pointed at v3, and the API served v3 in every ring.
- `make demo-reset FILE_TYPE=Win32` then put production and the rings back on v1, because v3 is only a demo copy. The registry is as it was before Stage 6.
- This run used Win32 because only two unreplayed Win64 weeks were left (about 20,000 files), which is too few to fill all three rings' minimums. The thresholds were not lowered for the demo.

## What the demo found (and fixed)

The good model needed three attempts. The first two were rolled back although nothing was wrong with it. Both failures were in how the signals were measured, not in the model, and both are kept here.

### Finding 1: canary traffic inflated the candidate's latency

`Win64-v3-20261121T135329Z` ([table](rollout/2-good-model-latency-false-alarm.md)): RING0 → ROLLED_BACK after 47 s, "[latency] p99 107.5 ms vs production 36.4 ms (limit 97.7 ms)".

- **Cause:** the controller sent the 500 canaries 8 at a time, on top of the live replay, to the single-worker API. The canaries were counted as candidate requests, so their queueing showed up as the candidate's p99.
- **Fix:**
  - the API counts canary-tenant requests under separate `canary_requests`, `canary_errors` and `canary_latency_ms` metrics, so they never enter the watched signals (unit test `test_canary_requests_never_enter_the_watched_metrics`);
  - canaries are sent 2 at a time (`canaries.send_workers`).

### Finding 2: production's comparison window was too long

`Win64-v3-20261121T200822Z` ([table](rollout/4-good-model-flag-rate-false-alarm.md)): RING1 → ROLLED_BACK, "[flag_rate] candidate flags 59.38% of 448 files in ring1 vs production 46.91% of 9,591".

- **Cause:** ring1 switched near the end of the replay, so the candidate's 448 files came from the end of week 62. Production's comparison set was everything since the rollout began (weeks 59–62).
  - Checked in the lineage table: over the same 54 s window, production v1 *itself* flagged 55.9% of ring2's files and 51.1% of ring1's, against 46.4% over the whole rollout.
  - The share of malware in the stream changes within a week, so a short recent window can't be compared with a long baseline.
- **Fix:** production is compared over the **same period**, the traffic of rings still on production since the current ring switched. In the last ring nothing runs on production alongside it, so the flag rate isn't compared there. It was already compared on the same period in ring0 and ring1, and canaries, errors and latency still apply.

## Stage 7 addition: paired scoring (2026-11-22)

The flag rate compares different files: candidate files in one ring against production files in other rings. So small changes drown in noise (Finding 2 above). Since Stage 7, in every ring that serves the candidate, the API also scores each file with production and stores production's version, score and verdict in the lineage row. The new `disagreement` signal counts, on the **same files**, how often the candidate flags what production clears ("new flags") and the reverse ("cleared"). It compares each rate with what was approved:
- with a gate PROMOTE, the gate's own disagreement tables, recorded in the APPROVED row;
- for a demo copy of production, 0.

A breach needs the live rate's lower Wilson bound above the approved upper bound + 1 point. Production keeps scoring alongside in ring2 too, so the last ring is covered now.

### 4. Bad model caught by paired scoring alone (canaries off)

`Win64-v5-20261122T091833Z` ([full table](rollout/6-bad-model-paired-scoring.md)), Win64 week 64 at 100 events/s. v5 is a fresh copy of the BAD 0.5 cut-off model. `make rollout CANDIDATE=5 DEMO=1 NO_CANARIES=1`: no never-break canaries were sent (allowed only with `--demo`; recorded in the APPROVED row).

| t (s) | Change | Reason |
|---|---|---|
| 0.0 | → APPROVED | demo approval; never-break canaries OFF (paired-scoring signal alone) |
| 1.6 | APPROVED → RING0 | `ring0_model` → v5 |
| 59.8 | RING0 → **ROLLED_BACK** | [disagreement] flags that production clears: 4.55% of 66 (CI 1.56–12.53%, limit 1.00%), expected from demo copy of sentinel-win64/1 |

- The API picked up v5 about 35 s after the alias moved (30 s poll). The rollback came about 20 s after the first v5 verdict, after only 66 paired files. The flag rate check was still waiting for 300 files, and at this size could not have told the models apart anyway.
- **Exposure:** 113 ring0 files were scored by v5, 4 of them newly flagged against production, 0 newly cleared. The API stopped serving v5 10 s after the rollback.
- A first attempt reused the Stage 6 BAD version v4 (`Win64-v4-20261122T091445Z`). It was rolled back after 11 s by `[never_break]`, before serving a single file, because v4's flagged canary from Stage 6 is still in the lineage. Canary results are counted across rollouts on purpose: a version that ever flagged a never-break file is stopped at once. Demonstrating the new signal on its own therefore needed a fresh version.

### 5. Good model still reaches COMPLETE (no false alarm, last ring covered)

`Win32-v3-20261122T092116Z` ([full table](rollout/7-good-model-paired-scoring-complete.md)), Win32 weeks 59–61 at 300 events/s, canaries on:

| t (s) | Change | Disagreement (new flags / cleared) |
|---|---|---|
| 0.4 | APPROVED → RING0 | |
| 176.9 | RING0 → RING1 | 0 / 0 of 323 paired files |
| 288.0 | RING1 → RING2 | 0 / 0 of 1,502 |
| 365.5 | RING2 → **COMPLETE** | 0 / 0 of 3,424 (ring2, where the flag rate is not compared) |

Then `make demo-reset FILE_TYPE=Win32` put production back on v1. Every ring serves v1 for both types again.

**Cost:** the extra production score runs inside the candidate's request. So the candidate's p99 includes it: ring0 126 ms vs production 103 ms under a 300 events/s replay on the single-worker API, inside the 2× + 25 ms limit. Stage 8's load test will measure it properly.

## Honest limits

- **Not a gate-approved rollout.** The gate approved neither real candidate (Stage 5: both REJECT), so every rollout here used a demo model and skipped the gate, recorded in each APPROVED row. With a gate PROMOTE the controller needs no `--demo`; that path is covered by unit tests only.
- **The flag rate is a weak signal on this replay.** The traffic is half malware, so even a 6× rise in false alarms barely moves it. On real, mostly clean traffic it would be far more sensitive. Since Stage 7, paired scoring (section 4) is the sensitive signal, and the canaries carry the never-break protection.
- **Paired scoring can't tell new false alarms from newly caught malware.** Labels arrive days later, so the signal can only check that the candidate behaves as approved. A gate-approved candidate is allowed the disagreement the gate measured (plus 1 point). That path is unit-tested on the real Stage 5 bundle, but no gate-approved rollout has run on the stack.
- **Error rate and latency come from the API's in-process metrics.** They're cumulative since the API started, and the latency check compares the candidate's recent samples with production's most recent 10,000 under the same load. Stage 8 moves them to PostgreSQL `ops_metrics`.
- **The last ring has no flag-rate comparison** (see Finding 2). Since Stage 7 the paired disagreement signal covers ring2 instead.
- **A rolled-back model keeps serving until the API's next poll.** That was up to 12 s here, with the poll every 30 s. The kill switch skips the wait by reloading at once.
