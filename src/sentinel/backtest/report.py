"""Stage 7b: docs/BACKTEST.md (auto section between markers; hand-written text is kept)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pandas as pd

AUTO_BEGIN = "<!-- backtest:pass1:auto:begin -->"
AUTO_END = "<!-- backtest:pass1:auto:end -->"
NAMES = {"P1_auto": "P1 auto-promote", "P2_val_auc": "P2 validation AUC", "P3_gate": "P3 gate"}


def _f(v: Any, nd: int = 2, pct: bool = False) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "n/a"
    return f"{100 * v:.{nd}f}%" if pct else f"{v:.{nd}f}"


def pass1_markdown(
    res: dict[str, Any], table: pd.DataFrame, redo: pd.DataFrame, prov: dict[str, Any]
) -> str:
    n = res["noise"]
    cfg = prov["backtest_config"]
    lines = [
        "## Pass 1 results (generated)",
        "",
        f"Win64, weeks {cfg['test_weeks']['first']}-{cfg['test_weeks']['last']}; labels "
        f"{cfg['label_delay_weeks']} weeks late; 18 candidates retrained on weeks <= T-6; "
        f"recall at {100 * cfg['operating_fpr']:.1f}% FPR; bootstrap clusters: "
        f"**{res['cluster_level']}** ({cfg['harm']['bootstrap_resamples']} resamples, "
        f"{int(100 * cfg['harm']['confidence'])}% CIs). Seed {prov['seed']}, data "
        f"{prov['data_fingerprint'][:12]}, commit {prov['git_commit'][:12]}.",
        "",
        "### 1b. Harm margin",
        "",
        f"v1 (unchanged) recall at 0.1% FPR on weeks {n.weeks[0]}-{n.weeks[-1]} (not fitted on, "
        f"before the test weeks): " + ", ".join(f"{v:.2f}" for v in n.weekly_recall) + " points.",
        "Week-to-week changes: "
        + ", ".join(f"{v:+.2f}" for v in n.week_to_week_changes)
        + f"; noise (std of the changes, n = {len(n.week_to_week_changes)}) = "
        f"**{n.noise_points:.2f} points**. X = {n.floor_points:g}. "
        f"**M = max(noise, X) = {n.margin_points:.2f} points.**",
        "",
        "### Main table",
        "",
        "| Policy | Recalibration | Evaluated/Decided | Promotions | Harmful (> M) | Max loss "
        "(pts) | Better blocked | Mean recall@0.1%FPR [95% CI] | FPR at shipped cut-off | "
        "Recall at shipped cut-off | Time-to-ship (weeks) | Days a harmful model was live |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in table.itertuples():
        lines.append(
            f"| {NAMES[str(r.release_policy)]} | {'on' if r.recalibration else 'off'} | "
            f"{r.evaluated}/{r.decided} | {r.promotions} | {r.harmful} | "
            f"{_f(r.max_loss_points)} | {r.better_blocked} | {_f(r.mean_recall, 2, True)} "
            f"[{_f(r.ci_low, 2, True)}, {_f(r.ci_high, 2, True)}] | "
            f"{_f(r.shipped_fpr, 3, True)} | {_f(r.shipped_recall, 2, True)} | "
            f"{_f(r.time_to_ship_weeks, 1)} | {r.days_harmful_live} |"
        )
    lines += [
        "",
        "Counts are summed over the three retraining policies; the mean recall and its CI are "
        "averaged over them. A promotion's outcome compares it with the model it replaced on "
        "the 4 weeks after it went live (same files).",
        "",
        "### 1e. Undecided weeks",
        "",
        "A candidate whose decision would fall after the last week is counted in Evaluated but "
        "not Decided; nothing changes, and every week is scored with the model that was "
        "actually serving (v1 or an earlier promotion).",
        "",
        "### 1f. Was the original P3 leaky?",
        "",
        "| T | Shadow weeks | Original decision week | Labels used before they existed "
        "| Point-in-time decision week |",
        "|---|---|---|---|---|",
    ]
    for r in res["p3_leak"]:
        lines.append(
            f"| {r['as_of_week']} | {r['shadow_weeks']} | {r['original_decision_week']} | "
            f"{r['labels_used_before_they_exist'] or 'none'} | "
            f"{r['point_in_time_decision_week']} |"
        )
    d = res["decisions"]
    rej = d[d["decided"] & ~d["promoted"] & (d["release_policy"] != "P1_auto")]
    lines += [
        "",
        "### Rejection reasons",
        "",
        "| Policy | Recalibration | Candidate | Check that blocked it | Later outcome "
        "(Δ recall pts [CI]) |",
        "|---|---|---|---|---|",
    ]
    for r in rej.itertuples():
        lines.append(
            f"| {NAMES[str(r.release_policy)]} | {'on' if r.recalibration else 'off'} | "
            f"{r.candidate} | {r.blocked_by or 'n/a'} | {r.outcome} ({_f(r.delta_points)} "
            f"[{_f(r.ci_low)}, {_f(r.ci_high)}]) |"
        )
    lines += [
        "",
        "### 1d. The original harmful promotions, re-judged",
        "",
        "Stage 7's harmful promotions (rejected candidates never shipped and are left out; "
        "a swap shipped by several policies is one row). Same models, files and 4-week "
        "windows, so the point loss is unchanged; "
        "what changes is the margin M and the family-clustered CI.",
        "",
        "| Shipped by | Candidate | Replaced | Original loss (pts) | Δ now (pts) [CI] | "
        "Outcome now |",
        "|---|---|---|---|---|---|",
    ]
    for r in redo.itertuples():
        lines.append(
            f"| {r.release_policy} | {r.candidate} | {r.production} | "
            f"{_f(r.original_loss_points)} | "
            f"{_f(r.delta_points)} [{_f(r.ci_low)}, {_f(r.ci_high)}] | {r.outcome_now} |"
        )
    survive = int((redo["outcome_now"] == "worse").sum())
    lines += [
        "",
        f"**{survive} of {len(redo)} original harms survive** the new definition.",
        "",
        "### 3. Early-label quality (SIMULATED)",
        "",
        "EMBER2024 has no early labels. Early label = final label flipped at the stated rate, "
        "so agreement is set by construction; it measures nothing about real early labels.",
        "",
        "| Flip rate | Mean agreement | Min weekly agreement |",
        "|---|---|---|",
    ]
    for rate, g in res["early_labels"].groupby("flip_rate"):
        lines.append(
            f"| {100 * float(rate):.0f}% | {_f(g['agreement'].mean(), 2, True)} | "
            f"{_f(g['agreement'].min(), 2, True)} |"
        )
    return "\n".join(lines) + "\n"


def write_pass1_md(
    path: Path, res: dict[str, Any], table: pd.DataFrame, redo: pd.DataFrame, prov: dict[str, Any]
) -> None:
    auto = f"{AUTO_BEGIN}\n{pass1_markdown(res, table, redo, prov)}{AUTO_END}"
    if path.exists():
        text = path.read_text("utf-8")
        if AUTO_BEGIN in text and AUTO_END in text:
            head, rest = text.split(AUTO_BEGIN, 1)
            tail = rest.split(AUTO_END, 1)[1]
            path.write_text(head + auto + tail, "utf-8")
            return
    path.write_text(
        "# Release-policy backtest (Stage 7b)\n\n"
        "Owner brief of 2026-11-23, answers of 2026-11-24 (docs/PROGRESS.md, Stage 7b).\n\n"
        + auto
        + "\n\n## Pass 1: plain statements\n\n(written after the results)\n",
        "utf-8",
    )


# ---------------------------------------------------------------------------- Pass 2

P2_BEGIN = "<!-- backtest:pass2:auto:begin -->"
P2_END = "<!-- backtest:pass2:auto:end -->"
P2_NAMES = {
    **NAMES,
    "P4_clean": "P4 clean-pool match (pre-registered, out-of-sample)",
    "P4_traffic": "P4 traffic match (pre-registered, out-of-sample)",
    "P4_walk_forward": "P4 walk-forward tuned (out-of-sample)",
    "P4_IN_SAMPLE": "P4 tuned on all 18 (**IN-SAMPLE**, optimistic)",
    "P5_blend": "Score averaging (blend, recalibrated)",
}


def _name(rp: str) -> str:
    if rp.startswith("P4_clean_flip"):
        pct = rp.removeprefix("P4_clean_flip")
        return f"P4 clean-pool, early-label flip {pct}% (sensitivity)"
    return P2_NAMES.get(rp, rp)


def _ci(v: Any, lo: Any, hi: Any) -> str:
    return "-" if v is None or pd.isna(v) else f"{v:+.2f} [{lo:+.2f}, {hi:+.2f}]"


def pass2_markdown(r: dict[str, Any], table: pd.DataFrame, diffs: pd.DataFrame) -> str:
    pr = r["params"]
    lines = [
        "## Pass 2 results (generated)",
        "",
        "Every threshold below was fixed in `configs/backtest.yaml` before this ran; M = "
        f"{pr['preregistered']['margin_points']:.2f} points (Pass 1). P4 and the blend set "
        "cut-offs point-in-time by design (recalibration on).",
        "",
        "### Main table",
        "",
        "| Policy | Recal. | Evaluated/Decided | Promotions | Rollbacks | Harmful (> M) | Max loss "
        "(pts) | Better blocked | Mean recall@0.1%FPR [95% CI] | FPR at shipped cut-off | "
        "Time-to-ship (weeks) | Days a harmful model was live |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in table.itertuples():
        lines.append(
            f"| {_name(str(t.release_policy))} | {'on' if t.recalibration else 'off'} | "
            f"{t.evaluated}/{t.decided} | {t.promotions} | {t.rollbacks} | {t.harmful} | "
            f"{_f(t.max_loss_points)} | {t.better_blocked} | {_f(t.mean_recall, 2, True)} "
            f"[{_f(t.ci_low, 2, True)}, {_f(t.ci_high, 2, True)}] | "
            f"{_f(t.shipped_fpr, 3, True)} | {_f(t.time_to_ship_weeks, 1)} | "
            f"{t.days_harmful_live} |"
        )
    lines += [
        "",
        "Rollbacks: P4 stages 2-3 only. P1-P3 rows are Pass 1's. Counts are summed over the "
        "three retraining policies.",
        "",
        "### Which differences are statistically real",
        "",
        "Mean over weeks 53-64 and the three retraining chains; 95% family-clustered CI, both "
        "policies scored on the same resampled files. Points = percentage points.",
        "",
        "| Comparison | Metric | Difference (pts) [95% CI] | Real? |",
        "|---|---|---|---|",
    ]
    for d in diffs.itertuples():
        lo_d, hi_d = float(cast(Any, d.ci_low)), float(cast(Any, d.ci_high))
        real = "yes" if (lo_d > 0 or hi_d < 0) else "no (within noise)"
        lines.append(
            f"| {d.comparison} | {d.metric} | {d.diff:+.3f} [{d.ci_low:+.3f}, "
            f"{d.ci_high:+.3f}] | {real} |"
        )
    dec = r["decisions"]
    p4 = dec[dec["release_policy"] == "P4_clean"]
    lines += [
        "",
        "### P4 (clean-pool, pre-registered) decisions",
        "",
        "| Candidate | Replaced | Evidence weeks | Clean files | Promoted | Blocked by | "
        "Later outcome (Δ pts [CI]) |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in p4.itertuples():
        warn = " (warn: < 50,000)" if d.clean_files < 50000 else ""
        lines.append(
            f"| {d.candidate} | {d.production} | {d.evidence_weeks or 'none'} | "
            f"{int(cast(Any, d.clean_files))}{warn} | {'yes' if d.promoted else 'no'} | "
            f"{d.blocked_by or '-'} | {d.outcome} ({_ci(d.delta_points, d.ci_low, d.ci_high)}) |"
        )
    counts: dict[str, int] = {}
    for d in dec[dec["release_policy"] == "P4_clean"].itertuples():
        for c in str(d.blocked_by).split(","):
            if c:
                counts[c] = counts.get(c, 0) + 1
    lines += [
        "",
        "How often each check blocked (P4 clean-pool, 18 decisions; a decision can fail "
        "several): " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none") + ".",
        "",
        "### P4 stage 2-3 events",
        "",
        "| Policy | Retraining | Week | Candidate | Event | Detail |",
        "|---|---|---|---|---|---|",
    ]
    ev = r["events"]
    for e in ev.itertuples() if len(ev) else []:
        lines.append(
            f"| {_name(str(e.policy))} | {e.retrain_policy} | {e.week} | {e.candidate} | "
            f"{e.event} | {e.detail} |"
        )
    lines += [
        "",
        "### The 7 candidates the Stage 7 gate rejected (their Pass 2 retrained versions)",
        "",
        "| Candidate | Compared with | Passes P4 now (clean-pool) | Passes P4 now (traffic) "
        "| Later outcome (Δ pts [CI]) |",
        "|---|---|---|---|---|",
    ]
    for d in r["rejected_redo"].itertuples():
        lines.append(
            f"| {d.candidate} | {d.production} | "
            f"{'yes' if d.passes_clean else 'no (' + str(d.blocked_by_clean) + ')'} | "
            f"{'yes' if d.passes_traffic else 'no (' + str(d.blocked_by_traffic) + ')'} | "
            f"{d.outcome} ({_ci(d.delta_points, d.ci_low, d.ci_high)}) |"
        )
    lines += [
        "",
        "### 5. Disagreement labelling (McNemar, reported only)",
        "",
        "Each candidate vs its predecessor at clean-pool matched cut-offs. Day 0: the labelled "
        "evidence weeks. Priority: the first live week's disagreements, labelled first "
        "(assumed within 1 week). b = production right / candidate wrong; c = the reverse.",
        "",
        "| Candidate | vs | Day-0 b / c (p) | Priority b / c (p) | Day-0 checks |",
        "|---|---|---|---|---|",
    ]
    for m in r["mcnemar"].itertuples():
        d0 = "n/a" if pd.isna(m.day0_b) else f"{int(m.day0_b)} / {int(m.day0_c)} ({m.day0_p:.3g})"
        pq_ = (
            "n/a"
            if pd.isna(m.priority_b)
            else f"{int(m.priority_b)} / {int(m.priority_c)} ({m.priority_p:.3g})"
        )
        lines.append(f"| {m.candidate} | {m.production} | {d0} | {pq_} | {m.day0_checks} |")
    wf = pr["walk_forward"]
    lines += [
        "",
        "### 6. Walk-forward tuning",
        "",
        f"Pre-registered: {pr['preregistered']}. A candidate may tune P4 at week t only once "
        "its 4 outcome weeks are labelled (T' + 4 <= t - 6). n = 18 candidates is small.",
        "",
        "| Decision week | Parameters used |",
        "|---|---|",
    ]
    for t, p in sorted(wf.items()):
        tag = " (= pre-registered)" if p == pr["preregistered"] else ""
        lines.append(f"| {t} | {p}{tag} |")
    lines += [
        "",
        f"In-sample optimum on all 18 (IN-SAMPLE, not a result): {pr['in_sample']}.",
        "",
        "### 7. Training window",
        "",
        "Recall@0.1%FPR on the 4 weeks after T; paired Δ vs the last_n_weeks candidate of the "
        "same T and vs v1 (points, 95% family-clustered CI).",
        "",
        "| T | Policy | Weeks | Recall | Δ vs last_n_weeks [CI] | Δ vs v1 [CI] |",
        "|---|---|---|---|---|---|",
    ]
    for t in r["training_window"].itertuples():
        lines.append(
            f"| {t.as_of_week} | {t.retrain_policy} | {t.weeks} | {_f(t.recall, 2, True)} | "
            f"{_ci(t.vs_last_n_points, t.vs_last_n_ci_low, t.vs_last_n_ci_high)} | "
            f"{_ci(t.vs_v1_points, t.vs_v1_ci_low, t.vs_v1_ci_high)} |"
        )
    dr = r["drift"]
    lines += [
        "",
        "### 8. Drift-triggered recalibration (drift never blocks a release)",
        "",
        "| Schedule | Cut-off rule | Alert weeks | Mean shipped FPR | Mean abs(FPR - 0.1%) | "
        "Mean shipped recall |",
        "|---|---|---|---|---|---|",
    ]
    for (s, rule), g in dr.groupby(["schedule", "rule"], sort=False):
        lines.append(
            f"| {s} | {rule} | {int(g['alert'].sum())} | "
            f"{_f(g['shipped_fpr'].mean(), 3, True)} | "
            f"{_f((g['shipped_fpr'] - 0.001).abs().mean(), 3, True)} | "
            f"{_f(g['shipped_recall'].mean(), 2, True)} |"
        )
    lines += [
        "",
        "Early *retraining* on an alert cannot be simulated: models exist only at the "
        "scheduled retrain weeks (each alert would need a new Kaggle run).",
    ]
    return "\n".join(lines) + "\n"


def write_pass2_md(path: Path, r: dict[str, Any], table: pd.DataFrame, diffs: pd.DataFrame) -> None:
    auto = f"{P2_BEGIN}\n{pass2_markdown(r, table, diffs)}{P2_END}"
    text = path.read_text("utf-8") if path.exists() else "# Release-policy backtest (Stage 7b)\n"
    if P2_BEGIN in text and P2_END in text:
        head, rest = text.split(P2_BEGIN, 1)
        text = head + auto + rest.split(P2_END, 1)[1]
    else:
        text = (
            text.rstrip("\n")
            + "\n\n"
            + auto
            + "\n\n## Pass 2: plain statements\n\n(written after the results)\n"
        )
    path.write_text(text, "utf-8")
