"""Stage 7: summarise the simulated decisions (table, two charts, Markdown for RESULTS.md).

Point estimates are resample 0 (the real stream); intervals are the 2.5-97.5 percentiles over
the bootstrap resamples (r >= 1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from sentinel.experiment.simulate import RELEASE_POLICIES

POLICY_NAMES = {
    "P1_auto": "P1 auto-promote every retrain",
    "P2_val_auc": "P2 promote if validation AUC is higher",
    "P3_gate": "P3 Sentinel Gate",
}
AUTO_BEGIN = "<!-- experiment:auto:begin -->"
AUTO_END = "<!-- experiment:auto:end -->"


def per_resample(decisions: pd.DataFrame, weekly: pd.DataFrame) -> pd.DataFrame:
    """One row per (resample, release policy), summed over the retraining policies."""
    d = decisions.copy()
    d["promoted_decided"] = d["promoted"] & d["decided"]
    d["harmful_promotion"] = d["promoted_decided"] & (d["harmful"] == True)  # noqa: E712
    d["judged_promotion"] = d["promoted_decided"] & d["harmful"].notna()
    d["good_blocked"] = d["decided"] & ~d["promoted"] & (d["better"] == True)  # noqa: E712
    d["good_available"] = d["decided"] & (d["better"] == True)  # noqa: E712
    g = d.groupby(["resample", "release_policy"])
    out = pd.DataFrame(
        {
            "decisions": g["decided"].sum(),
            "promotions": g["promoted_decided"].sum(),
            "judged_promotions": g["judged_promotion"].sum(),
            "harmful_promotions": g["harmful_promotion"].sum(),
            "good_blocked": g["good_blocked"].sum(),
            "good_available": g["good_available"].sum(),
        }
    )
    out["harmful_rate"] = out["harmful_promotions"] / out["judged_promotions"].where(
        out["judged_promotions"] > 0
    )
    w = weekly.groupby(["resample", "release_policy"])
    out["mean_recall_at_fpr"] = w["recall_at_fpr"].mean()
    out["mean_shipped_fpr"] = w["shipped_fpr"].mean()
    return out.reset_index()


def _interval(s: pd.Series) -> tuple[float, float]:
    v = s.dropna().to_numpy(dtype=np.float64)
    if v.size == 0:
        return float("nan"), float("nan")
    lo, hi = np.percentile(v, [2.5, 97.5])
    return float(lo), float(hi)


def summary_table(rs: pd.DataFrame) -> pd.DataFrame:
    """Per release policy: real-stream value + bootstrap interval for every column."""
    rows = []
    metrics = [c for c in rs.columns if c not in ("resample", "release_policy")]
    for rp in RELEASE_POLICIES:
        sub = rs[rs["release_policy"] == rp]
        real = sub[sub["resample"] == 0]
        boot = sub[sub["resample"] > 0]
        row: dict[str, Any] = {"release_policy": rp}
        for m in metrics:
            row[m] = float(real[m].iloc[0]) if len(real) else float("nan")
            row[f"{m}_lo"], row[f"{m}_hi"] = _interval(boot[m])
        rows.append(row)
    return pd.DataFrame(rows)


def drift_precision(decisions: pd.DataFrame) -> pd.DataFrame:
    """Did drift alerts flag the harmful candidates? Judged on P1 (every candidate promoted
    against its predecessor, so each candidate appears once per retraining policy)."""
    d = decisions[
        (decisions["release_policy"] == "P1_auto")
        & decisions["decided"]
        & decisions["harmful"].notna()
    ]
    rows = []
    for r, sub in d.groupby("resample"):
        harmful = sub["harmful"].astype(bool)
        alert = sub["drift_alert"].astype(bool)
        rows.append(
            {
                "resample": r,
                "candidates": len(sub),
                "harmful": int(harmful.sum()),
                "alerts": int(alert.sum()),
                "true_alerts": int((harmful & alert).sum()),
                "precision": (harmful & alert).sum() / alert.sum() if alert.any() else np.nan,
                "recall": (harmful & alert).sum() / harmful.sum() if harmful.any() else np.nan,
                "base_rate": harmful.mean() if len(sub) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def _fmt(row: pd.Series, col: str, pct: bool = False, digits: int = 1) -> str:
    v, lo, hi = row[col], row.get(f"{col}_lo"), row.get(f"{col}_hi")
    if pd.isna(v):
        return "-"
    k = 100.0 if pct else 1.0
    text = f"{k * v:.{digits}f}{'%' if pct else ''}"
    if lo is not None and hi is not None and not (pd.isna(lo) or pd.isna(hi)):
        text += f" ({k * lo:.{digits}f}-{k * hi:.{digits}f})"
    return text


def markdown(
    table: pd.DataFrame,
    by_retrain: pd.DataFrame,
    drift: pd.DataFrame,
    resamples: int,
    charts: dict[str, str],
) -> str:
    lines = [
        AUTO_BEGIN,
        "",
        f"Real stream; in brackets the 95% range over {resamples} bootstrap resamples. Counts "
        "are summed over the three retraining policies.",
        "",
        "| Release policy | Promotions | Harmful promotions | Harmful share | Good updates "
        "blocked | Mean recall at 0.1% FPR | Mean FPR at shipped threshold |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, r in table.iterrows():
        lines.append(
            f"| {POLICY_NAMES[str(r['release_policy'])]} | {_fmt(r, 'promotions', digits=0)} | "
            f"{_fmt(r, 'harmful_promotions', digits=0)} | {_fmt(r, 'harmful_rate', pct=True)} | "
            f"{_fmt(r, 'good_blocked', digits=0)} of {_fmt(r, 'good_available', digits=0)} | "
            f"{_fmt(r, 'mean_recall_at_fpr', pct=True, digits=2)} | "
            f"{_fmt(r, 'mean_shipped_fpr', pct=True, digits=3)} |"
        )
    lines += [
        "",
        f"![Harmful promotions per policy]({charts['harmful']})",
        "",
        f"![Recall over time per policy]({charts['recall']})",
        "",
        "Per retraining policy (real stream): promotions / harmful / good updates blocked.",
        "",
        "| Retraining policy | " + " | ".join(POLICY_NAMES[p] for p in RELEASE_POLICIES) + " |",
        "|---|" + "---|" * len(RELEASE_POLICIES),
    ]
    for pol, sub in by_retrain.groupby("retrain_policy"):
        cells = []
        for rp in RELEASE_POLICIES:
            s = sub[sub["release_policy"] == rp]
            cells.append(
                f"{int(s['promotions'].sum())} / {int(s['harmful_promotions'].sum())} / "
                f"{int(s['good_blocked'].sum())}"
            )
        lines.append(f"| {pol} | " + " | ".join(cells) + " |")
    real = drift[drift["resample"] == 0]
    boot = drift[drift["resample"] > 0]
    if len(real):
        r0 = real.iloc[0]
        lo, hi = _interval(boot["precision"])
        lines += [
            "",
            f"Drift alerts (P1 candidates, real stream): {int(r0['alerts'])} alerts on "
            f"{int(r0['candidates'])} candidates, {int(r0['true_alerts'])} of them harmful "
            f"(precision {_pct(r0['precision'])}, bootstrap {_pct(lo)}-{_pct(hi)}); "
            f"{int(r0['harmful'])} harmful candidates in all (base rate "
            f"{_pct(r0['base_rate'])}, recall {_pct(r0['recall'])}).",
        ]
    lines += ["", AUTO_END]
    return "\n".join(lines) + "\n"


def _pct(x: float) -> str:
    return "-" if x is None or pd.isna(x) else f"{100 * x:.0f}%"


def by_retrain_policy(decisions: pd.DataFrame) -> pd.DataFrame:
    d = decisions[decisions["resample"] == 0].copy()
    d["promotions"] = d["promoted"] & d["decided"]
    d["harmful_promotions"] = d["promotions"] & (d["harmful"] == True)  # noqa: E712
    d["good_blocked"] = d["decided"] & ~d["promoted"] & (d["better"] == True)  # noqa: E712
    return (
        d.groupby(["retrain_policy", "release_policy"])[
            ["promotions", "harmful_promotions", "good_blocked"]
        ]
        .sum()
        .reset_index()
    )


def charts(table: pd.DataFrame, weekly: pd.DataFrame, out_dir: Path) -> dict[str, Path]:
    """Two PNGs: harmful promotions (+ blocked good updates) per policy; weekly recall."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    labels = [POLICY_NAMES[p].split(" ", 1)[0] for p in RELEASE_POLICIES]
    x = np.arange(len(RELEASE_POLICIES))
    fig, ax = plt.subplots(figsize=(7, 4))
    for off, col, name, color in (
        (-0.2, "harmful_promotions", "harmful promotions", "#c0392b"),
        (0.2, "good_blocked", "good updates blocked", "#7f8c8d"),
    ):
        v = table[col].to_numpy(dtype=float)
        err = np.vstack(
            [
                np.clip(v - table[f"{col}_lo"].to_numpy(dtype=float), 0, None),
                np.clip(table[f"{col}_hi"].to_numpy(dtype=float) - v, 0, None),
            ]
        )
        ax.bar(x + off, v, width=0.4, label=name, color=color, yerr=err, capsize=4)
    ax.set_xticks(x, labels)
    ax.set_ylabel("count (3 retraining policies)")
    ax.set_title("Harmful promotions and blocked good updates (95% bootstrap range)")
    ax.legend()
    fig.tight_layout()
    harmful = out_dir / "harmful_promotions.png"
    fig.savefig(harmful, dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4))
    for rp, color in zip(RELEASE_POLICIES, ("#c0392b", "#2980b9", "#27ae60"), strict=True):
        sub = weekly[weekly["release_policy"] == rp]
        per = sub.groupby(["resample", "week"])["recall_at_fpr"].mean().unstack("week")
        weeks = per.columns.to_numpy()
        ax.plot(weeks, 100 * per.loc[0].to_numpy(), label=POLICY_NAMES[rp], color=color)
        boot = per.drop(index=0, errors="ignore")
        if len(boot):
            lo, hi = np.percentile(100 * boot.to_numpy(), [2.5, 97.5], axis=0)
            ax.fill_between(weeks, lo, hi, color=color, alpha=0.15)
    ax.set_xlabel("test week")
    ax.set_ylabel("recall at 0.1% FPR (%)")
    ax.set_title("Live model's recall per week (mean of retraining policies)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    recall = out_dir / "recall_over_time.png"
    fig.savefig(recall, dpi=120)
    plt.close(fig)
    return {"harmful": harmful, "recall": recall}


def write_results_md(path: Path, auto: str) -> None:
    """Replace the auto section of RESULTS.md (hand-written text outside it is kept)."""
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if AUTO_BEGIN in text and AUTO_END in text:
            head, rest = text.split(AUTO_BEGIN, 1)
            tail = rest.split(AUTO_END, 1)[1]
            path.write_text(head + auto.rstrip("\n") + tail, encoding="utf-8")
            return
    path.write_text("# Results (Stage 7)\n\n" + auto, encoding="utf-8")
