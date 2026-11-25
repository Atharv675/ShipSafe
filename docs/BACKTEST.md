# Release-policy backtest (Stage 7b)

Owner brief of 2026-11-23, answers of 2026-11-24 (docs/PROGRESS.md, Stage 7b).

<!-- backtest:pass1:auto:begin -->
## Pass 1 results (generated)

Win64, weeks 53-64; labels 6 weeks late; 18 candidates retrained on weeks <= T-6; recall at 0.1% FPR; bootstrap clusters: **family** (200 resamples, 95% CIs). Seed 20241122, data c824866c3eb2, commit 2c423ad5f997.

### 1b. Harm margin

v1 (unchanged) recall at 0.1% FPR on weeks 48-52 (not fitted on, before the test weeks): 91.68, 91.72, 89.66, 87.60, 81.08 points.
Week-to-week changes: +0.04, -2.06, -2.06, -6.52; noise (std of the changes, n = 4) = **2.76 points**. X = 0.5. **M = max(noise, X) = 2.76 points.**

### Main table

| Policy | Recalibration | Evaluated/Decided | Promotions | Harmful (> M) | Max loss (pts) | Better blocked | Mean recall@0.1%FPR [95% CI] | FPR at shipped cut-off | Recall at shipped cut-off | Time-to-ship (weeks) | Days a harmful model was live |
|---|---|---|---|---|---|---|---|---|---|---|---|
| P1 auto-promote | off | 18/18 | 18 | 1 | 4.57 | 0 | 88.20% [84.92%, 89.75%] | 0.132% | 89.25% | 1.0 | 14 |
| P1 auto-promote | on | 18/18 | 18 | 1 | 4.57 | 0 | 88.20% [84.92%, 89.75%] | 0.130% | 89.23% | 1.0 | 14 |
| P2 validation AUC | off | 18/18 | 7 | 0 | 0.22 | 0 | 88.89% [85.63%, 90.38%] | 0.165% | 91.24% | 1.0 | 0 |
| P2 validation AUC | on | 18/18 | 7 | 0 | 0.22 | 0 | 88.89% [85.63%, 90.38%] | 0.143% | 90.54% | 1.0 | 0 |
| P3 gate | off | 18/9 | 0 | 0 | 0.00 | 0 | 88.35% [84.92%, 90.10%] | 0.197% | 92.06% | n/a | 0 |
| P3 gate | on | 18/9 | 0 | 0 | 0.00 | 0 | 88.35% [84.92%, 90.10%] | 0.137% | 90.04% | n/a | 0 |

Counts are summed over the three retraining policies; the mean recall and its CI are averaged over them. A promotion's outcome compares it with the model it replaced on the 4 weeks after it went live (same files).

### 1e. Undecided weeks

A candidate whose decision would fall after the last week is counted in Evaluated but not Decided; nothing changes, and every week is scored with the model that was actually serving (v1 or an earlier promotion).

### 1f. Was the original P3 leaky?

| T | Shadow weeks | Original decision week | Labels used before they existed | Point-in-time decision week |
|---|---|---|---|---|
| 52 | [53, 54] | 58 | [53, 54] | 60 |
| 54 | [55, 56] | 60 | [55, 56] | 62 |
| 56 | [57, 58] | 62 | [57, 58] | 64 |
| 58 | [59, 60] | 64 | [59, 60] | 66 |
| 60 | [61, 62] | 66 | [61, 62] | 68 |
| 62 | [63, 64] | 68 | [63, 64] | 70 |

### Rejection reasons

| Policy | Recalibration | Candidate | Check that blocked it | Later outcome (Δ recall pts [CI]) |
|---|---|---|---|---|
| P2 validation AUC | off | all_history_T52 | validation_auc | within noise (-2.39 [-4.82, 0.22]) |
| P2 validation AUC | off | all_history_T54 | validation_auc | within noise (-0.19 [-2.54, 1.62]) |
| P2 validation AUC | off | all_history_T56 | validation_auc | within noise (0.29 [-2.00, 1.31]) |
| P2 validation AUC | off | last_n_weeks_T52 | validation_auc | worse (-4.57 [-6.83, -0.24]) |
| P2 validation AUC | off | last_n_weeks_T54 | validation_auc | within noise (-2.21 [-5.66, -0.55]) |
| P2 validation AUC | off | last_n_weeks_T56 | validation_auc | within noise (-1.76 [-3.75, -0.28]) |
| P2 validation AUC | off | last_n_weeks_T58 | validation_auc | within noise (-0.29 [-1.94, 0.56]) |
| P2 validation AUC | off | last_n_weeks_T60 | validation_auc | within noise (-0.79 [-2.75, 1.50]) |
| P2 validation AUC | off | recency_weighted_T52 | validation_auc | within noise (-1.17 [-3.35, 1.92]) |
| P2 validation AUC | off | recency_weighted_T54 | validation_auc | within noise (0.11 [-2.65, 1.80]) |
| P2 validation AUC | off | recency_weighted_T56 | validation_auc | within noise (-0.01 [-1.14, 1.33]) |
| P2 validation AUC | on | all_history_T52 | validation_auc | within noise (-2.39 [-4.82, 0.22]) |
| P2 validation AUC | on | all_history_T54 | validation_auc | within noise (-0.19 [-2.54, 1.62]) |
| P2 validation AUC | on | all_history_T56 | validation_auc | within noise (0.29 [-2.00, 1.31]) |
| P2 validation AUC | on | last_n_weeks_T52 | validation_auc | worse (-4.57 [-6.83, -0.24]) |
| P2 validation AUC | on | last_n_weeks_T54 | validation_auc | within noise (-2.21 [-5.66, -0.55]) |
| P2 validation AUC | on | last_n_weeks_T56 | validation_auc | within noise (-1.76 [-3.75, -0.28]) |
| P2 validation AUC | on | last_n_weeks_T58 | validation_auc | within noise (-0.29 [-1.94, 0.56]) |
| P2 validation AUC | on | last_n_weeks_T60 | validation_auc | within noise (-0.79 [-2.75, 1.50]) |
| P2 validation AUC | on | recency_weighted_T52 | validation_auc | within noise (-1.17 [-3.35, 1.92]) |
| P2 validation AUC | on | recency_weighted_T54 | validation_auc | within noise (0.11 [-2.65, 1.80]) |
| P2 validation AUC | on | recency_weighted_T56 | validation_auc | within noise (-0.01 [-1.14, 1.33]) |
| P3 gate | off | all_history_T52 | recall_at_fpr,disagreement,weekly_regression | within noise (-1.05 [-3.64, 0.81]) |
| P3 gate | off | all_history_T54 | recall_at_fpr,disagreement,weekly_regression | within noise (-2.44 [-3.09, 1.83]) |
| P3 gate | off | all_history_T56 | recall_at_fpr,disagreement | not judged (n/a [n/a, n/a]) |
| P3 gate | off | last_n_weeks_T52 | recall_at_fpr,disagreement,weekly_regression | within noise (-0.17 [-3.15, 0.63]) |
| P3 gate | off | last_n_weeks_T54 | recall_at_fpr,disagreement,weekly_regression | within noise (-1.61 [-4.62, 1.28]) |
| P3 gate | off | last_n_weeks_T56 | recall_at_fpr,disagreement,weekly_regression | not judged (n/a [n/a, n/a]) |
| P3 gate | off | recency_weighted_T52 | recall_at_fpr,disagreement | within noise (-0.64 [-3.51, 1.03]) |
| P3 gate | off | recency_weighted_T54 | recall_at_fpr,disagreement | within noise (-1.33 [-2.39, 1.96]) |
| P3 gate | off | recency_weighted_T56 | disagreement | not judged (n/a [n/a, n/a]) |
| P3 gate | on | all_history_T52 | recall_at_fpr,false_alarms,weekly_regression | within noise (-1.05 [-3.64, 0.81]) |
| P3 gate | on | all_history_T54 | recall_at_fpr,disagreement,weekly_regression | within noise (-2.44 [-3.09, 1.83]) |
| P3 gate | on | all_history_T56 | recall_at_fpr,false_alarms,disagreement | not judged (n/a [n/a, n/a]) |
| P3 gate | on | last_n_weeks_T52 | recall_at_fpr,false_alarms,disagreement,weekly_regression | within noise (-0.17 [-3.15, 0.63]) |
| P3 gate | on | last_n_weeks_T54 | recall_at_fpr,disagreement,weekly_regression | within noise (-1.61 [-4.62, 1.28]) |
| P3 gate | on | last_n_weeks_T56 | recall_at_fpr,false_alarms,disagreement,weekly_regression | not judged (n/a [n/a, n/a]) |
| P3 gate | on | recency_weighted_T52 | recall_at_fpr,false_alarms | within noise (-0.64 [-3.51, 1.03]) |
| P3 gate | on | recency_weighted_T54 | recall_at_fpr,disagreement | within noise (-1.33 [-2.39, 1.96]) |
| P3 gate | on | recency_weighted_T56 | false_alarms,disagreement | not judged (n/a [n/a, n/a]) |

### 1d. The original harmful promotions, re-judged

Stage 7's harmful promotions (rejected candidates never shipped and are left out; a swap shipped by several policies is one row). Same models, files and 4-week windows, so the point loss is unchanged; what changes is the margin M and the family-clustered CI.

| Shipped by | Candidate | Replaced | Original loss (pts) | Δ now (pts) [CI] | Outcome now |
|---|---|---|---|---|---|
| P1_auto,P2_val_auc | all_history_T58 | all_history_T56 | -0.09 | -0.09 [-0.63, 1.27] | within noise |
| P1_auto | last_n_weeks_T52 | v1 | -0.58 | -0.58 [-3.65, 3.19] | within noise |
| P1_auto | last_n_weeks_T54 | last_n_weeks_T52 | -0.10 | -0.10 [-2.06, 2.71] | within noise |
| P1_auto,P2_val_auc | recency_weighted_T54 | recency_weighted_T52 | -0.13 | -0.13 [-0.83, 2.72] | within noise |
| P1_auto,P2_val_auc | recency_weighted_T56 | recency_weighted_T54 | -0.15 | -0.15 [-0.80, 2.87] | within noise |
| P2_val_auc | last_n_weeks_T54 | v1 | -1.63 | -1.63 [-4.16, 1.62] | within noise |

**0 of 6 original harms survive** the new definition.

### 3. Early-label quality (SIMULATED)

EMBER2024 has no early labels. Early label = final label flipped at the stated rate, so agreement is set by construction; it measures nothing about real early labels.

| Flip rate | Mean agreement | Min weekly agreement |
|---|---|---|
| 5% | 94.96% | 94.49% |
| 10% | 90.04% | 89.22% |
<!-- backtest:pass1:auto:end -->

## Pass 1: plain statements

Written 2026-11-25 after the run above (commit 2c423ad plus the two fixes listed under "Corrections"). Every count is summed over the three retraining policies. Intervals are 95% family-clustered bootstrap CIs (200 resamples).

**1. Were the original harms real or noise? Noise, by the new definition.**
- None of the 6 harmful swaps that Stage 7 counted survives (table 1d).
- Their losses were 0.09 to 1.63 points. Every CI includes 0, and every loss is below M = 2.76.
- The point losses are unchanged; only the margin and the clustered CI are new.
- So the Stage 7 statement "P1 shipped a worse model 5 times in 18" does not hold up once week-to-week noise and family clustering are taken into account.

**2. Under the point-in-time rules, does any policy ship real harm? Once, under P1.**
- `last_n_weeks_T52` replaced v1 and lost 4.57 points over weeks 53-56 [CI -6.83, -0.24]. It was live 14 days, until `last_n_weeks_T54` replaced it.
- That CI only just excludes 0, so this is one borderline case, not a pattern.
- The other 17 P1 promotions are "within noise". None is "better": no candidate beat its predecessor by more than M with a CI above 0.
- P2 blocked that candidate on validation AUC. P2 shipped 7 models, all within noise.
- P3 shipped nothing.

**3. Is any policy better on recall? No difference can be shown.**
- Mean recall at 0.1% FPR: P1 88.20%, P2 88.89%, P3 88.35%. The CIs overlap almost completely (about 85-90% for each).
- Recalibration does not change these numbers, because recall at a fixed FPR does not depend on the cut-off.

**4. Was the original P3 leaky? Yes, every decision was.**
- Stage 7's gate decided at T+6 using shadow weeks T+1 and T+2. Under the 6-week label rule, those weeks are only labelled at T+7 and T+8.
- All 6 original decisions used labels before they existed (table 1f).
- Decided point-in-time at T+8, the gate reaches a decision on only 9 of 18 candidates (T = 52, 54, 56) before week 64. It promoted none of them.
- Without recalibration, 8 of 9 rejections cite `recall_at_fpr` (most alongside `disagreement` and `weekly_regression`). The exception is `recency_weighted_T56`, blocked on `disagreement` alone. In Stage 7, disagreement was the sole reason for every rejection. The later outcome of every judged rejection is "within noise".

**5. Does recalibration alone close most of the P3 gap? For false alarms, yes. There is no recall gap left to close.**
- At the shipped cut-off, P3 (v1 kept live) ran at 0.197% FPR without recalibration and 0.137% with it. P1 runs at 0.132% (0.130% with recalibration).
- So recalibration removes about 92% of P3's FPR excess over P1 ((0.197 - 0.137) / (0.197 - 0.132)), at the cost of shipped recall (92.06% to 90.04%).
- This is a point estimate: no CI was computed for shipped FPR.
- P2 moves from 0.165% to 0.143%. No policy reaches the 0.1% target within the 0.05 pp-per-week cap.

**6. Early labels:** simulated only (table 3). By construction they say nothing about real early labels.

### What these results do not show
- One file type (Win64) and one 12-week stream.
- P3 decided only 9 candidates and promoted none, so these results say nothing about the harm rate of the models P3 would promote.
- No policy is shown to be better than another. Every comparison is inside the CIs.

### Assumptions (stated, not tuned on the judged weeks)
1. **M comes mostly from a trend.** M = std of v1's week-to-week recall changes over weeks 48-52 (n = 4), floored at X = 0.5. Its value, 2.76, is driven mostly by the 87.60 to 81.08 drop into week 52. That drop is drift (a trend) more than noise, so M is wide. With M = X = 0.5, two P2-blocked candidates (`last_n_weeks_T54`, `last_n_weeks_T56`, CIs entirely below 0) would count as "worse". The definition was fixed before the run and has not been changed.
2. **Noise weeks overlap calibration.** Weeks 48-51 are v1's calibration weeks (where its threshold was set). They are not training weeks, and recall at a fixed FPR does not use the threshold.
3. **Unfamilied malware.** 79.4% of malware files have a family and are resampled as whole families. The rest, and all benign files, are their own clusters.
4. **Harm window.** A promotion is judged on the 4 weeks after it goes live, truncated at week 64. Decisions at T = 62 fall outside the data and are "not judged".
5. **Recalibration pool.** The pool is the benign files of the newest 4 labelled weeks (weeks <= t-6), excluding weeks the model was fitted on. If nothing is left, the cut-off stays where it is. v1 therefore keeps its Kaggle cut-off for weeks 53-54. A candidate starts from its own Kaggle calibration cut-off.
6. **Mean CI averaging.** The policy-level CI is the average of the three retraining policies' CIs. It is not a joint interval.
7. **FPR resolution.** Each test week has about 5,000 benign files, so recall at 0.1% FPR per week rests on about 5 false alarms.

### Corrections made before these numbers (disclosed)
- **Recalibration pool contained training weeks.** In the first run, the recalibration pool for v1 at weeks 52-53 held only weeks 43-47, which v1 was trained on. Its top benign score there was about 7e-5, so the cut-off collapsed and shipped FPR read 4.5-7.5%. Fixed by leaving a model's fit weeks out of its own pool (unit test `test_recalibration_pool_never_uses_the_models_training_weeks`). No threshold, margin or tolerance was changed.
- **Table 1d included rejected candidates.** The first version listed 12 rows, because Stage 7 also marks rejected candidates as counterfactually "harmful". It now lists only promotions, merged per swap: 6 rows, covering P1's 5 and P2's 4.

### Open before Pass 2
- **"Top signers" (P4, N = 10):** EMBER2024 has no signer identity (Authenticode has 8 numeric fields only). The proposed substitute is signature status (unsigned / self-signed / signed by count of certs). This needs the owner's confirmation.

<!-- backtest:pass2:auto:begin -->
## Pass 2 results (generated)

Every threshold below was fixed in `configs/backtest.yaml` before this ran; M = 2.76 points (Pass 1). P4 and the blend set cut-offs point-in-time by design (recalibration on).

### Main table

| Policy | Recal. | Evaluated/Decided | Promotions | Rollbacks | Harmful (> M) | Max loss (pts) | Better blocked | Mean recall@0.1%FPR [95% CI] | FPR at shipped cut-off | Time-to-ship (weeks) | Days a harmful model was live |
|---|---|---|---|---|---|---|---|---|---|---|---|
| P1 auto-promote | off | 18/18 | 18 | 0 | 1 | 4.57 | 0 | 88.20% [84.92%, 89.75%] | 0.132% | 1.0 | 14 |
| P1 auto-promote | on | 18/18 | 18 | 0 | 1 | 4.57 | 0 | 88.20% [84.92%, 89.75%] | 0.130% | 1.0 | 14 |
| P2 validation AUC | off | 18/18 | 7 | 0 | 0 | 0.22 | 0 | 88.89% [85.63%, 90.38%] | 0.165% | 1.0 | 0 |
| P2 validation AUC | on | 18/18 | 7 | 0 | 0 | 0.22 | 0 | 88.89% [85.63%, 90.38%] | 0.143% | 1.0 | 0 |
| P3 gate | off | 18/9 | 0 | 0 | 0 | 0.00 | 0 | 88.35% [84.92%, 90.10%] | 0.197% | n/a | 0 |
| P3 gate | on | 18/9 | 0 | 0 | 0 | 0.00 | 0 | 88.35% [84.92%, 90.10%] | 0.137% | n/a | 0 |
| P4 tuned on all 18 (**IN-SAMPLE**, optimistic) | on | 18/18 | 1 | 0 | 0 | 0.00 | 0 | 88.52% [85.13%, 90.17%] | 0.137% | 1.0 | 0 |
| P4 clean-pool match (pre-registered, out-of-sample) | on | 18/18 | 0 | 0 | 0 | 0.00 | 0 | 88.35% [84.92%, 90.10%] | 0.137% | n/a | 0 |
| P4 clean-pool, early-label flip 10% (sensitivity) | on | 18/18 | 0 | 0 | 0 | 0.00 | 0 | 88.35% [84.92%, 90.10%] | 0.137% | n/a | 0 |
| P4 traffic match (pre-registered, out-of-sample) | on | 18/18 | 0 | 0 | 0 | 0.00 | 0 | 88.35% [84.92%, 90.10%] | 0.137% | n/a | 0 |
| P4 walk-forward tuned (out-of-sample) | on | 18/18 | 0 | 0 | 0 | 0.00 | 0 | 88.35% [84.92%, 90.10%] | 0.137% | n/a | 0 |
| Score averaging (blend, recalibrated) | on | 18/18 | 18 | 0 | 0 | 2.93 | 0 | 88.44% [85.12%, 89.75%] | 0.135% | 1.0 | 0 |

Rollbacks: P4 stages 2-3 only. P1-P3 rows are Pass 1's. Counts are summed over the three retraining policies.

### Which differences are statistically real

Mean over weeks 53-64 and the three retraining chains; 95% family-clustered CI, both policies scored on the same resampled files. Points = percentage points.

| Comparison | Metric | Difference (pts) [95% CI] | Real? |
|---|---|---|---|
| P2_val_auc (off) - P1_auto (off) | recall@0.1%FPR | +0.684 [-0.084, +1.508] | no (within noise) |
| P2_val_auc (off) - P1_auto (off) | shipped FPR | +0.033 [+0.019, +0.048] | yes |
| P3_gate (off) - P1_auto (off) | recall@0.1%FPR | +0.151 [-1.090, +1.406] | no (within noise) |
| P3_gate (off) - P1_auto (off) | shipped FPR | +0.064 [+0.046, +0.084] | yes |
| P3_gate (on) - P3_gate (off) | recall@0.1%FPR | +0.000 [+0.000, +0.000] | no (within noise) |
| P3_gate (on) - P3_gate (off) | shipped FPR | -0.060 [-0.078, -0.045] | yes |
| P4_clean (on) - P1_auto (off) | recall@0.1%FPR | +0.151 [-0.838, +1.308] | no (within noise) |
| P4_clean (on) - P1_auto (off) | shipped FPR | +0.004 [-0.016, +0.021] | no (within noise) |
| P4_traffic (on) - P1_auto (off) | recall@0.1%FPR | +0.151 [-0.910, +1.391] | no (within noise) |
| P4_traffic (on) - P1_auto (off) | shipped FPR | +0.004 [-0.015, +0.022] | no (within noise) |
| P4_walk_forward (on) - P1_auto (off) | recall@0.1%FPR | +0.151 [-0.884, +1.418] | no (within noise) |
| P4_walk_forward (on) - P1_auto (off) | shipped FPR | +0.004 [-0.012, +0.020] | no (within noise) |
| P4_clean (on) - P3_gate (on) | recall@0.1%FPR | +0.000 [+0.000, +0.000] | no (within noise) |
| P4_clean (on) - P3_gate (on) | shipped FPR | +0.000 [+0.000, +0.000] | no (within noise) |
| P5_blend (on) - P1_auto (off) | recall@0.1%FPR | +0.234 [-0.391, +0.650] | no (within noise) |
| P5_blend (on) - P1_auto (off) | shipped FPR | +0.003 [-0.007, +0.016] | no (within noise) |
| P5_blend (on) - P1_auto (on) | recall@0.1%FPR | +0.234 [-0.391, +0.650] | no (within noise) |
| P5_blend (on) - P1_auto (on) | shipped FPR | +0.005 [-0.004, +0.017] | no (within noise) |

### P4 (clean-pool, pre-registered) decisions

| Candidate | Replaced | Evidence weeks | Clean files | Promoted | Blocked by | Later outcome (Δ pts [CI]) |
|---|---|---|---|---|---|---|
| all_history_T52 | v1 | none | 0 (warn: < 50,000) | no | evidence | within noise (-2.39 [-4.82, +0.22]) |
| all_history_T54 | v1 | 48 | 5000 (warn: < 50,000) | no | must_catch,unflags,new_families,signature_self_signed,signature_signed | within noise (-0.19 [-2.54, +1.62]) |
| all_history_T56 | v1 | 48,49,50 | 15000 (warn: < 50,000) | no | must_catch,unflags | within noise (+0.29 [-2.00, +1.31]) |
| all_history_T58 | v1 | 49,50,51,52 | 20000 (warn: < 50,000) | no | must_catch | within noise (+0.61 [-0.90, +2.15]) |
| all_history_T60 | v1 | 51,52,53,54 | 19999 (warn: < 50,000) | no | must_catch | within noise (+0.37 [-1.45, +2.97]) |
| all_history_T62 | v1 | 53,54,55,56 | 19995 (warn: < 50,000) | no | must_catch | within noise (+1.24 [-0.50, +6.55]) |
| last_n_weeks_T52 | v1 | none | 0 (warn: < 50,000) | no | evidence | worse (-4.57 [-6.83, -0.24]) |
| last_n_weeks_T54 | v1 | 48 | 5000 (warn: < 50,000) | no | non_inferiority,must_catch,unflags,new_families,signature_unsigned,signature_self_signed,signature_signed | within noise (-2.21 [-5.66, -0.55]) |
| last_n_weeks_T56 | v1 | 48,49,50 | 15000 (warn: < 50,000) | no | must_catch,unflags,signature_self_signed,signature_signed | within noise (-1.76 [-3.75, -0.28]) |
| last_n_weeks_T58 | v1 | 49,50,51,52 | 20000 (warn: < 50,000) | no | must_catch | within noise (-0.29 [-1.94, +0.56]) |
| last_n_weeks_T60 | v1 | 51,52,53,54 | 19999 (warn: < 50,000) | no | must_catch | within noise (-0.79 [-2.75, +1.50]) |
| last_n_weeks_T62 | v1 | 53,54,55,56 | 19995 (warn: < 50,000) | no | must_catch | within noise (+1.23 [-2.27, +5.83]) |
| recency_weighted_T52 | v1 | none | 0 (warn: < 50,000) | no | evidence | within noise (-1.17 [-3.35, +1.92]) |
| recency_weighted_T54 | v1 | 48 | 5000 (warn: < 50,000) | no | must_catch,unflags,new_families,signature_self_signed,signature_signed | within noise (+0.11 [-2.65, +1.80]) |
| recency_weighted_T56 | v1 | 48,49,50 | 15000 (warn: < 50,000) | no | must_catch,unflags,signature_self_signed,signature_signed | within noise (-0.01 [-1.14, +1.33]) |
| recency_weighted_T58 | v1 | 49,50,51,52 | 20000 (warn: < 50,000) | no | must_catch | within noise (+1.02 [-0.59, +2.71]) |
| recency_weighted_T60 | v1 | 51,52,53,54 | 19999 (warn: < 50,000) | no | must_catch | within noise (+0.08 [-1.53, +2.49]) |
| recency_weighted_T62 | v1 | 53,54,55,56 | 19995 (warn: < 50,000) | no | must_catch | within noise (+1.82 [-0.56, +6.81]) |

How often each check blocked (P4 clean-pool, 18 decisions; a decision can fail several): evidence 3, must_catch 15, new_families 3, non_inferiority 1, signature_self_signed 5, signature_signed 5, signature_unsigned 1, unflags 6.

### P4 stage 2-3 events

| Policy | Retraining | Week | Candidate | Event | Detail |
|---|---|---|---|---|---|
| P4 tuned on all 18 (**IN-SAMPLE**, optimistic) | recency_weighted | 63 | recency_weighted_T62 | stage2 | agreement 95.04%, early delta +0.56 pts |

### The 7 candidates the Stage 7 gate rejected (their Pass 2 retrained versions)

| Candidate | Compared with | Passes P4 now (clean-pool) | Passes P4 now (traffic) | Later outcome (Δ pts [CI]) |
|---|---|---|---|---|
| all_history_T54 | all_history_T52 | no (must_catch) | no (must_catch) | within noise (+1.11 [-0.89, +3.96]) |
| all_history_T56 | all_history_T52 | no (must_catch) | no (must_catch) | within noise (+0.40 [-0.21, +3.63]) |
| last_n_weeks_T52 | v1 | no (evidence) | no (evidence) | worse (-4.57 [-6.83, -0.24]) |
| last_n_weeks_T54 | v1 | no (non_inferiority,must_catch,unflags,new_families,signature_unsigned,signature_self_signed,signature_signed) | no (non_inferiority,never_break,must_catch,new_families) | within noise (-2.21 [-5.66, -0.55]) |
| last_n_weeks_T56 | v1 | no (must_catch,unflags,signature_self_signed,signature_signed) | no (must_catch,new_flags) | within noise (-1.76 [-3.75, -0.28]) |
| recency_weighted_T54 | recency_weighted_T52 | no (must_catch) | no (must_catch) | within noise (+0.40 [-1.51, +2.16]) |
| recency_weighted_T56 | recency_weighted_T52 | no (must_catch) | no (must_catch) | within noise (+0.55 [-0.99, +2.03]) |

### 5. Disagreement labelling (McNemar, reported only)

Each candidate vs its predecessor at clean-pool matched cut-offs. Day 0: the labelled evidence weeks. Priority: the first live week's disagreements, labelled first (assumed within 1 week). b = production right / candidate wrong; c = the reverse.

| Candidate | vs | Day-0 b / c (p) | Priority b / c (p) | Day-0 checks |
|---|---|---|---|---|
| all_history_T52 | v1 | n/a | n/a | evidence |
| all_history_T54 | all_history_T52 | 71 / 374 (9.39e-51) | 31 / 86 (3.68e-07) | must_catch |
| all_history_T56 | all_history_T54 | 107 / 481 (1.48e-57) | 41 / 55 (0.184) | must_catch |
| all_history_T58 | all_history_T56 | 157 / 328 (6.16e-15) | 33 / 56 (0.0192) | must_catch |
| all_history_T60 | all_history_T58 | 124 / 463 (6.01e-47) | 21 / 43 (0.00815) | must_catch |
| all_history_T62 | all_history_T60 | 195 / 237 (0.0484) | 55 / 27 (0.00264) | must_catch |
| last_n_weeks_T52 | v1 | n/a | n/a | evidence |
| last_n_weeks_T54 | last_n_weeks_T52 | 344 / 245 (5.19e-05) | 128 / 32 (8.42e-15) | must_catch,unflags,new_families,signature_self_signed |
| last_n_weeks_T56 | last_n_weeks_T54 | 106 / 671 (3.46e-101) | 39 / 104 (5.18e-08) | must_catch |
| last_n_weeks_T58 | last_n_weeks_T56 | 134 / 483 (3.34e-47) | 20 / 74 (1.85e-08) | must_catch |
| last_n_weeks_T60 | last_n_weeks_T58 | 267 / 302 (0.154) | 45 / 21 (0.00427) | must_catch |
| last_n_weeks_T62 | last_n_weeks_T60 | 87 / 561 (9.18e-86) | 17 / 87 (1.67e-12) | must_catch |
| recency_weighted_T52 | v1 | n/a | n/a | evidence |
| recency_weighted_T54 | recency_weighted_T52 | 119 / 332 (2.41e-24) | 35 / 73 (0.000326) | must_catch |
| recency_weighted_T56 | recency_weighted_T54 | 199 / 382 (2.67e-14) | 53 / 51 (0.922) | must_catch |
| recency_weighted_T58 | recency_weighted_T56 | 187 / 340 (2.6e-11) | 39 / 60 (0.0439) | must_catch |
| recency_weighted_T60 | recency_weighted_T58 | 133 / 381 (9.78e-29) | 21 / 42 (0.0111) | must_catch |
| recency_weighted_T62 | recency_weighted_T60 | 90 / 393 (3.57e-46) | 15 / 67 (5.26e-09) | must_catch |

### 6. Walk-forward tuning

Pre-registered: {'margin_points': 2.7634036983401464, 'confidence': 0.95, 'eval_weeks': 4, 'flag_rate_tolerance': 0.1}. A candidate may tune P4 at week t only once its 4 outcome weeks are labelled (T' + 4 <= t - 6). n = 18 candidates is small.

| Decision week | Parameters used |
|---|---|
| 52 | {'margin_points': 2.7634036983401464, 'confidence': 0.95, 'eval_weeks': 4, 'flag_rate_tolerance': 0.1} (= pre-registered) |
| 54 | {'margin_points': 2.7634036983401464, 'confidence': 0.95, 'eval_weeks': 4, 'flag_rate_tolerance': 0.1} (= pre-registered) |
| 56 | {'margin_points': 2.7634036983401464, 'confidence': 0.95, 'eval_weeks': 4, 'flag_rate_tolerance': 0.1} (= pre-registered) |
| 58 | {'margin_points': 2.7634036983401464, 'confidence': 0.95, 'eval_weeks': 4, 'flag_rate_tolerance': 0.1} (= pre-registered) |
| 60 | {'margin_points': 2.7634036983401464, 'confidence': 0.95, 'eval_weeks': 4, 'flag_rate_tolerance': 0.1} (= pre-registered) |
| 62 | {'margin_points': 2.7634036983401464, 'confidence': 0.95, 'eval_weeks': 4, 'flag_rate_tolerance': 0.1} (= pre-registered) |

In-sample optimum on all 18 (IN-SAMPLE, not a result): {'margin_points': 0.5, 'confidence': 0.9, 'eval_weeks': 2, 'flag_rate_tolerance': 0.05}.

### 7. Training window

Recall@0.1%FPR on the 4 weeks after T; paired Δ vs the last_n_weeks candidate of the same T and vs v1 (points, 95% family-clustered CI).

| T | Policy | Weeks | Recall | Δ vs last_n_weeks [CI] | Δ vs v1 [CI] |
|---|---|---|---|---|---|
| 52 | all_history | 53-56 | 84.84% | +2.18 [-0.65, +4.36] | -2.39 [-4.82, +0.22] |
| 52 | last_n_weeks | 53-56 | 82.66% | - | -4.57 [-6.83, -0.24] |
| 52 | recency_weighted | 53-56 | 86.06% | +3.40 [+0.70, +5.76] | -1.17 [-3.35, +1.92] |
| 54 | all_history | 55-58 | 86.39% | +2.02 [-0.01, +3.80] | -0.19 [-2.54, +1.62] |
| 54 | last_n_weeks | 55-58 | 84.38% | - | -2.21 [-5.66, -0.55] |
| 54 | recency_weighted | 55-58 | 86.69% | +2.32 [+0.53, +3.98] | +0.11 [-2.65, +1.80] |
| 56 | all_history | 57-60 | 89.53% | +2.04 [+0.00, +3.47] | +0.29 [-2.00, +1.31] |
| 56 | last_n_weeks | 57-60 | 87.48% | - | -1.76 [-3.75, -0.28] |
| 56 | recency_weighted | 57-60 | 89.23% | +1.75 [+0.24, +3.44] | -0.01 [-1.14, +1.33] |
| 58 | all_history | 59-62 | 91.77% | +0.90 [-0.40, +2.64] | +0.61 [-0.90, +2.15] |
| 58 | last_n_weeks | 59-62 | 90.88% | - | -0.29 [-1.94, +0.56] |
| 58 | recency_weighted | 59-62 | 92.18% | +1.31 [+0.38, +3.70] | +1.02 [-0.59, +2.71] |
| 60 | all_history | 61-64 | 91.16% | +1.16 [-0.48, +3.19] | +0.37 [-1.45, +2.97] |
| 60 | last_n_weeks | 61-64 | 90.00% | - | -0.79 [-2.75, +1.50] |
| 60 | recency_weighted | 61-64 | 90.86% | +0.86 [-0.03, +3.08] | +0.08 [-1.53, +2.49] |
| 62 | all_history | 63-64 | 89.54% | +0.01 [-1.69, +4.40] | +1.24 [-0.50, +6.55] |
| 62 | last_n_weeks | 63-64 | 89.53% | - | +1.23 [-2.27, +5.83] |
| 62 | recency_weighted | 63-64 | 90.12% | +0.59 [-2.01, +4.19] | +1.82 [-0.56, +6.81] |

### 8. Drift-triggered recalibration (drift never blocks a release)

| Schedule | Cut-off rule | Alert weeks | Mean shipped FPR | Mean abs(FPR - 0.1%) | Mean shipped recall |
|---|---|---|---|---|---|
| v1 kept live | none | 1 | 0.197% | 0.103% | 92.06% |
| v1 kept live | weekly capped | 1 | 0.137% | 0.073% | 90.04% |
| v1 kept live | drift jump only | 1 | 0.153% | 0.083% | 90.24% |
| v1 kept live | weekly capped + drift jump | 1 | 0.137% | 0.073% | 90.04% |
| P1 all_history | none | 1 | 0.132% | 0.058% | 89.36% |
| P1 all_history | weekly capped | 1 | 0.132% | 0.058% | 89.30% |
| P1 all_history | drift jump only | 1 | 0.132% | 0.058% | 89.30% |
| P1 all_history | weekly capped + drift jump | 1 | 0.132% | 0.058% | 89.30% |
| P1 last_n_weeks | none | 2 | 0.135% | 0.048% | 88.66% |
| P1 last_n_weeks | weekly capped | 2 | 0.130% | 0.043% | 88.70% |
| P1 last_n_weeks | drift jump only | 2 | 0.135% | 0.048% | 88.60% |
| P1 last_n_weeks | weekly capped + drift jump | 2 | 0.130% | 0.043% | 88.70% |
| P1 recency_weighted | none | 1 | 0.130% | 0.053% | 89.73% |
| P1 recency_weighted | weekly capped | 1 | 0.128% | 0.052% | 89.68% |
| P1 recency_weighted | drift jump only | 1 | 0.130% | 0.053% | 89.67% |
| P1 recency_weighted | weekly capped + drift jump | 1 | 0.128% | 0.052% | 89.68% |

Early *retraining* on an alert cannot be simulated: models exist only at the scheduled retrain weeks (each alert would need a new Kaggle run).
<!-- backtest:pass2:auto:end -->

## Pass 2: plain statements

Written 2026-11-25 after the run above. Every threshold was fixed in `configs/backtest.yaml` before the run, and nothing was changed after seeing results.

**1. P4 as specified promoted nothing (0 of 18), so it is not better than the simpler options. It is effectively P3 with recalibration.**
- 15 of 18 decisions were blocked by the **must-catch** check (0 of 500 malware may be missed).
- The other 3 (T = 52) had **no usable evidence**: under the 6-week label rule the only labelled weeks are v1's training weeks.
- At matched 0.1% FPR, two different models always disagree on some malware. Even candidates that later turned out better (for example `recency_weighted_T62`, +1.82 points) missed some of production's caught files.
- So a zero-tolerance must-catch rule blocks every real retrain. The rule was fixed before the run and has **not** been relaxed: relaxing it now would mean tuning on the judged weeks.
- Because nothing was promoted, P4's stage 2 (early labels) and stage 3 (sequential rollback) were never exercised on the real stream. They were only exercised in the unit tests and in one in-sample event.

**2. No policy is better on recall.** Every recall difference has a CI that includes 0:
- P2 − P1: +0.68 points [−0.08, +1.51];
- P3 − P1: +0.15 [−1.09, +1.41];
- P4 − P1: +0.15 [−0.84, +1.31];
- blend − P1: +0.23 [−0.39, +0.65].

**3. The real differences are all in shipped false alarms, and recalibration closes them.**
- Keeping an older model live raises the shipped FPR:
  - P2: +0.033 pp [+0.019, +0.048];
  - P3: +0.064 pp [+0.046, +0.084].
- Weekly capped recalibration removes most of that: P3 drops by 0.060 pp [−0.078, −0.045], which is real.
- With recalibration, P3/P4 is no different from P1: +0.004 pp [−0.016, +0.021].

**4. Score averaging (the blend) works but adds nothing measurable.** Recall +0.23 [−0.39, +0.65] and FPR +0.003 pp vs P1. Its worst swap lost 2.93 points, which is within noise (the CI includes 0).

**5. The training window matters more than the release policy.**
- `recency_weighted` beats `last_n_weeks` by 1.3–3.4 points at T = 52, 54, 56 and 58, with each CI above 0.
- `all_history` is ahead at every T as well, but only T = 56 clears 0, and only just (+2.04 [+0.00, +3.47]).
- The two worse `last_n_weeks` candidates would have been **avoided at source**:
  - at T52 the other windows lose −2.39 / −1.17 points vs v1, against −4.57 for `last_n_weeks` (the only real harm in the backtest);
  - at T54 they lose −0.19 / +0.11, against −2.21.

**6. Drift-triggered recalibration does not help.**
- Alerts fired in only 1–2 of 12 weeks per schedule.
- A jump on alert added nothing on top of weekly capped recalibration.
- Used alone, it was worse than weekly recalibration for v1 (shipped FPR 0.153% vs 0.137%).
- Drift was never used to block a release.

**7. Disagreement labelling (McNemar) is a stronger signal than the Wilson checks, but it is reported only.**
- On the day-0 labelled weeks, most candidates were significantly better than their predecessor (c ≫ b, small p).
- With 1-week priority labelling, the first live week's disagreements told the same story for most swaps.
- Exceptions:
  - `last_n_weeks_T54` was worse in both views (b > c);
  - `all_history_T62` and `last_n_weeks_T60` were worse on the priority week.
- It was not a decision input, so this is a lead for future work, not a result.

**8. Walk-forward tuning was not possible in practice.**
- With 6-week labels and 4-week outcomes, a candidate can inform tuning only after 10 weeks. Only the T = 52 candidates could tune the T = 62 decision.
- All three were blocked by "evidence" at every grid point, so the pre-registered values were used throughout.
- The in-sample optimum (M 0.5, 90% Wilson, 2 weeks, 5% tolerance) promoted one candidate. It is labelled IN-SAMPLE and is not a result. n = 18 is small.

**9. The 7 candidates the Stage 7 gate rejected:** their Pass 2 retrained versions all fail P4 under both matching methods, almost all on must-catch.
- Four were later "within noise" with gains of +0.40 to +1.11 points.
- `last_n_weeks_T52` was really worse.
- The other two `last_n_weeks` candidates had losses whose CIs exclude 0 but which are smaller than M.

### Recommendation (the simpler option, as the brief asks)
On this stream, the evidence supports a policy simpler than P4:
- retrain with `recency_weighted` (or `all_history`);
- promote on schedule (P1) or with the cheap validation-AUC check (P2, which blocked the only real harm);
- keep **weekly capped recalibration** on;
- rely on the ring rollout (Stage 6) as the safety net.

The release gate (P3) and P4 add delay and block candidates without a measurable recall benefit. This is one file type and one 12-week stream; nothing here is proven beyond these data.

If the owner wants P4 kept, the must-catch rule needs a tolerance, such as "candidate misses no more than production's own miss rate on a fresh slice". That is a new pre-registration, to be judged on new weeks, not these.

### Pass 2 assumptions (stated, fixed before the run)
1. **Evidence weeks.** P4's day-0 evidence is the newest 4 labelled weeks minus every week either model was fitted on. At T = 52 that leaves nothing, so the decision is blocked ("evidence"), not deferred.
2. **Production's cut-off in traffic match.** Production's cut-off is its clean-pool cut-off in both methods; only the candidate's differs.
3. **Directional checks.** These compare the two discordant cells:
   - new flags: benign files the candidate adds vs benign files it removes;
   - unflags: malware it drops vs malware it adds.

   A check fails only if the candidate's lower Wilson bound is above production's upper bound.
4. **Slices.** Slices use malware of the evidence weeks at matched cut-offs. A family is "new" if it was first seen (in any label) within the last 4 labelled weeks. Signature status is unsigned (`num_certs` = 0), self-signed, or signed. A slice with under 30 files is n/a.
5. **Stage 2.** Early labels are SIMULATED (5% flips; 10% as sensitivity). Agreement is measured on the newest labelled week.
6. **Stage 3.** The always-valid CI uses Bonferroni over 6 looks (99.17% per look), with the family-clustered bootstrap. The candidate is compared with the model it replaced on the same files, which assumes paired scoring (Stage 7) is running.
7. **Tuning objective.** It is the total realised 4-week recall change of the candidates P4 would promote, each against its predecessor. Ties keep the pre-registered values.
8. **Blend.** B_T = 0.5 × B_prev + 0.5 × candidate_T, so older models decay geometrically. Its first cut-off comes from the clean pool, or from traffic match when every labelled week is a fit week (T = 52).
9. **Drift.** Drift uses Stage 7's unchanged rule (configs/experiment.yaml). Early retraining on an alert cannot be simulated.
10. **Priority labelling.** Labels for the first live week's disagreement files are assumed to arrive within 1 week.
11. **Paired-difference CIs.** These resample files within each week, with the chain of decisions held fixed. They do not capture uncertainty in which models were promoted.
