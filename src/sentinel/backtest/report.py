"""Stage 7b: docs/BACKTEST.md (auto section between markers; hand-written text is kept)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
        "### 1d. The original 5 harms, re-judged",
        "",
        "| Candidate | Replaced | Original loss (pts) | Δ now (pts) [CI] | Outcome now |",
        "|---|---|---|---|---|",
    ]
    for r in redo.itertuples():
        lines.append(
            f"| {r.candidate} | {r.production} | {_f(r.original_loss_points)} | "
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
