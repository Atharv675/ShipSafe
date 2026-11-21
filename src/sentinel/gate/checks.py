"""The release gate's checks and decision (pure: arrays in, results out).

Candidate and production are compared on the SAME labelled events. Each check returns
pass / fail / insufficient with its numbers and a plain-English summary; `decide` turns them
into PROMOTE / REJECT / INSUFFICIENT_EVIDENCE. All statistics come from
sentinel.models.evaluate (never re-implemented here). Rules: configs/gate_policy.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

from sentinel.config import GatePolicy
from sentinel.models.evaluate import (
    expected_calibration_error,
    mcnemar_exact,
    paired_table,
    rates_at_threshold,
    recall_at_fpr,
)

Status = Literal["pass", "fail", "insufficient"]
Decision = Literal["PROMOTE", "REJECT", "INSUFFICIENT_EVIDENCE"]
F64 = npt.NDArray[np.float64]


@dataclass(frozen=True)
class ModelScores:
    """One model's view of the evaluation: scores on the paired events + never-break set."""

    label: str  # "<registered model>/<version>"
    threshold: float  # the SHIPPED threshold (flag if score > threshold)
    scores: F64
    probabilities: F64
    never_break_scores: F64


@dataclass(frozen=True)
class GateInput:
    file_ids: list[str]
    y: npt.NDArray[np.int8]
    week: npt.NDArray[np.int64]
    production: ModelScores
    candidate: ModelScores
    expected_events: int  # events in the evaluated weeks (week files)
    paired_events: int  # events with BOTH a live and a shadow score
    unlabelled_events: int  # paired events whose label was not yet known at decision time

    def __post_init__(self) -> None:
        n = len(self.file_ids)
        arrays = (
            self.y,
            self.week,
            self.production.scores,
            self.candidate.scores,
            self.production.probabilities,
            self.candidate.probabilities,
        )
        if any(a.shape != (n,) for a in arrays):
            raise ValueError("every per-event array must have one entry per file id")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    hard: bool
    summary: str
    numbers: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "hard": self.hard,
            "summary": self.summary,
            "numbers": self.numbers,
        }


def _pct(x: float) -> str:
    return f"{100 * x:.2f}%"


def _enough_benign(n_benign: int, policy: GatePolicy, minimum: int) -> bool:
    return n_benign * policy.operating_fpr >= minimum


# ------------------------------------------------------------------------------- checks


def check_coverage(g: GateInput, policy: GatePolicy) -> CheckResult:
    cov = g.paired_events / g.expected_events if g.expected_events else 0.0
    nums = {
        "expected_events": g.expected_events,
        "paired_events": g.paired_events,
        "coverage": cov,
        "unlabelled_events": g.unlabelled_events,
        "labelled_events": len(g.file_ids),
        "min_coverage": policy.min_coverage,
    }
    if cov < policy.min_coverage:
        return CheckResult(
            "coverage",
            "insufficient",
            True,
            f"Only {_pct(cov)} of the evaluated events were scored by BOTH models "
            f"(need {_pct(policy.min_coverage)}); the comparison would not be on the same events.",
            nums,
        )
    return CheckResult(
        "coverage",
        "pass",
        True,
        f"{g.paired_events:,} of {g.expected_events:,} events ({_pct(cov)}) were scored by both "
        f"models; {len(g.file_ids):,} have a known label.",
        nums,
    )


def check_recall_at_fpr(g: GateInput, policy: GatePolicy) -> CheckResult:
    n_benign, n_mal = int((g.y == 0).sum()), int((g.y == 1).sum())
    nums: dict[str, Any] = {
        "operating_fpr": policy.operating_fpr,
        "benign": n_benign,
        "malicious": n_mal,
        "min_recall_gain": policy.min_recall_gain,
    }
    if n_mal == 0 or not _enough_benign(n_benign, policy, policy.min_false_alarms_pooled):
        return CheckResult(
            "recall_at_fpr",
            "insufficient",
            True,
            f"Too few labelled files for a {_pct(policy.operating_fpr)} false-alarm operating "
            f"point ({n_benign:,} benign, {n_mal:,} malicious).",
            nums,
        )
    p = recall_at_fpr(g.y, g.production.scores, policy.operating_fpr, policy.confidence)
    c = recall_at_fpr(g.y, g.candidate.scores, policy.operating_fpr, policy.confidence)
    gain = c.recall - p.recall
    nums |= {"production": p.as_dict(), "candidate": c.as_dict(), "recall_gain": gain}
    text = (
        f"At {_pct(policy.operating_fpr)} false alarms the candidate catches {_pct(c.recall)} "
        f"of malware (CI {_pct(c.recall_ci[0])}-{_pct(c.recall_ci[1])}) vs production "
        f"{_pct(p.recall)} (CI {_pct(p.recall_ci[0])}-{_pct(p.recall_ci[1])}), "
        f"a change of {100 * gain:+.2f} points"
    )
    if gain < policy.min_recall_gain:
        return CheckResult(
            "recall_at_fpr",
            "fail",
            True,
            text + f" (needs at least {100 * policy.min_recall_gain:+.2f}).",
            nums,
        )
    return CheckResult("recall_at_fpr", "pass", True, text + ".", nums)


def check_false_alarms(g: GateInput, policy: GatePolicy) -> CheckResult:
    n_benign = int((g.y == 0).sum())
    nums: dict[str, Any] = {
        "benign": n_benign,
        "max_fpr_increase_relative": policy.max_fpr_increase_relative,
    }
    if not _enough_benign(n_benign, policy, policy.min_false_alarms_pooled):
        return CheckResult(
            "false_alarms",
            "insufficient",
            True,
            f"Too few benign files ({n_benign:,}) to measure false alarms.",
            nums,
        )
    p = rates_at_threshold(g.y, g.production.scores, g.production.threshold, policy.confidence)
    c = rates_at_threshold(g.y, g.candidate.scores, g.candidate.threshold, policy.confidence)
    limit = p.fpr * (1 + policy.max_fpr_increase_relative)
    nums |= {"production": p.as_dict(), "candidate": c.as_dict(), "candidate_fpr_limit": limit}
    text = (
        f"At their shipped thresholds the candidate raises {c.fp:,} false alarms on "
        f"{n_benign:,} benign files ({_pct(c.fpr)}) vs production {p.fp:,} ({_pct(p.fpr)}); "
        f"limit {_pct(limit)}"
    )
    if c.fpr > limit:
        return CheckResult("false_alarms", "fail", True, text + " — exceeded.", nums)
    return CheckResult("false_alarms", "pass", True, text + ".", nums)


def _flips(
    g: GateInput, mask: npt.NDArray[np.bool_], cand_flag: npt.NDArray[np.bool_]
) -> list[dict[str, Any]]:
    return [
        {
            "file_id": g.file_ids[i],
            "week": int(g.week[i]),
            "label": "malicious" if g.y[i] == 1 else "benign",
            "candidate_verdict": "malicious" if cand_flag[i] else "benign",
            "production_score": float(g.production.scores[i]),
            "candidate_score": float(g.candidate.scores[i]),
        }
        for i in np.flatnonzero(mask)
    ]


def check_disagreement(g: GateInput, policy: GatePolicy) -> CheckResult:
    """McNemar exact test on the files where exactly one model is right, per class."""
    pf = g.production.scores > g.production.threshold
    cf = g.candidate.scores > g.candidate.threshold
    nums: dict[str, Any] = {"min_disagreements": policy.min_disagreements, "alpha": policy.alpha}
    worse: list[str] = []
    discordant = 0
    for cls, value in (("malicious", 1), ("benign", 0)):
        m = g.y == value
        t = paired_table(g.y[m], pf[m], cf[m])  # a = production, b = candidate
        pval = mcnemar_exact(t.only_a_right, t.only_b_right)
        discordant += t.discordant
        cand_worse = t.only_a_right > t.only_b_right and pval < policy.alpha
        nums[cls] = {
            **t.as_dict(),
            "only_production_right": t.only_a_right,
            "only_candidate_right": t.only_b_right,
            "mcnemar_p": pval,
            "candidate_significantly_worse": cand_worse,
            "candidate_significantly_better": t.only_b_right > t.only_a_right
            and pval < policy.alpha,
        }
        if cand_worse:
            what = "misses" if value == 1 else "false alarms"
            worse.append(
                f"{cls}: candidate is significantly worse ({t.only_a_right} new {what} vs "
                f"{t.only_b_right} fixed, McNemar p = {pval:.2g})"
            )
    nums["discordant_total"] = discordant
    nums["flipped_files"] = _flips(g, pf != cf, cf)
    mal, ben = nums["malicious"], nums["benign"]
    text = (
        f"Malware: candidate catches {mal['only_candidate_right']} that production misses and "
        f"misses {mal['only_production_right']} that production catches (p = "
        f"{mal['mcnemar_p']:.2g}). Benign: candidate clears {ben['only_candidate_right']} "
        f"production false alarms and adds {ben['only_production_right']} new ones (p = "
        f"{ben['mcnemar_p']:.2g})."
    )
    if worse:
        return CheckResult("disagreement", "fail", True, text + " " + "; ".join(worse) + ".", nums)
    if discordant < policy.min_disagreements:
        return CheckResult(
            "disagreement",
            "insufficient",
            True,
            text + f" Only {discordant} files differ (need {policy.min_disagreements}) — too "
            "few to tell the models apart.",
            nums,
        )
    return CheckResult("disagreement", "pass", True, text, nums)


def check_never_break(g: GateInput, policy: GatePolicy) -> CheckResult:
    nb = g.candidate.never_break_scores
    hits = int((nb > g.candidate.threshold).sum())
    prod_hits = int((g.production.never_break_scores > g.production.threshold).sum())
    nums = {
        "never_break_files": int(nb.size),
        "candidate_flags": hits,
        "production_flags": prod_hits,
        "allowed": policy.never_break_hits_allowed,
    }
    if nb.size == 0:
        return CheckResult(
            "never_break", "insufficient", True, "The never-break set is empty.", nums
        )
    text = (
        f"The candidate flags {hits} of {nb.size} never-break files "
        f"(allowed {policy.never_break_hits_allowed}; production flags {prod_hits})"
    )
    if hits > policy.never_break_hits_allowed:
        return CheckResult("never_break", "fail", True, text + ".", nums)
    return CheckResult("never_break", "pass", True, text + ".", nums)


def check_weekly_regression(g: GateInput, policy: GatePolicy) -> CheckResult:
    rows: list[dict[str, Any]] = []
    for w in np.unique(g.week):
        m = g.week == w
        y = g.y[m]
        n_benign, n_mal = int((y == 0).sum()), int((y == 1).sum())
        row: dict[str, Any] = {"week": int(w), "benign": n_benign, "malicious": n_mal}
        if n_mal == 0 or not _enough_benign(n_benign, policy, policy.min_false_alarms_weekly):
            row["status"] = "insufficient"
        else:
            p = recall_at_fpr(y, g.production.scores[m], policy.operating_fpr, policy.confidence)
            c = recall_at_fpr(y, g.candidate.scores[m], policy.operating_fpr, policy.confidence)
            drop = p.recall - c.recall
            row |= {
                "production_recall": p.recall,
                "candidate_recall": c.recall,
                "recall_drop": drop,
                "status": "fail" if drop > policy.max_weekly_recall_drop else "pass",
            }
        rows.append(row)
    nums = {"max_weekly_recall_drop": policy.max_weekly_recall_drop, "weeks": rows}
    failing = [r for r in rows if r["status"] == "fail"]
    thin = [r for r in rows if r["status"] == "insufficient"]
    if failing:
        worst = max(failing, key=lambda r: r["recall_drop"])
        return CheckResult(
            "weekly_regression",
            "fail",
            True,
            f"Recall at {_pct(policy.operating_fpr)} false alarms drops more than "
            f"{100 * policy.max_weekly_recall_drop:.1f} points in week(s) "
            f"{', '.join(str(r['week']) for r in failing)} (worst: week {worst['week']}, "
            f"{100 * worst['recall_drop']:.2f} points).",
            nums,
        )
    if thin or not rows:
        return CheckResult(
            "weekly_regression",
            "insufficient",
            True,
            f"Week(s) {', '.join(str(r['week']) for r in thin) or 'none'} have too few labelled "
            "files for a per-week comparison.",
            nums,
        )
    worst_drop = max(r["recall_drop"] for r in rows)
    return CheckResult(
        "weekly_regression",
        "pass",
        True,
        f"No week drops more than {100 * policy.max_weekly_recall_drop:.1f} points "
        f"(largest drop {100 * worst_drop:+.2f}) over {len(rows)} week(s).",
        nums,
    )


def check_calibration(g: GateInput, policy: GatePolicy) -> CheckResult:
    hard = True  # run_checks applies policy.soft_checks
    if len(g.file_ids) == 0:
        return CheckResult("calibration", "insufficient", hard, "No labelled files.", {})
    ep = expected_calibration_error(g.y, g.production.probabilities, policy.calibration_bins)
    ec = expected_calibration_error(g.y, g.candidate.probabilities, policy.calibration_bins)
    nums = {
        "production_ece": ep,
        "candidate_ece": ec,
        "max_ece_increase": policy.max_ece_increase,
        "bins": policy.calibration_bins,
    }
    text = f"Calibration error (ECE) candidate {ec:.4f} vs production {ep:.4f}"
    if ec > ep + policy.max_ece_increase:
        return CheckResult(
            "calibration",
            "fail",
            hard,
            text + f" — worse by more than {policy.max_ece_increase}.",
            nums,
        )
    return CheckResult("calibration", "pass", hard, text + ".", nums)


CHECKS = (
    check_coverage,
    check_recall_at_fpr,
    check_false_alarms,
    check_disagreement,
    check_never_break,
    check_weekly_regression,
    check_calibration,
)


def run_checks(g: GateInput, policy: GatePolicy) -> list[CheckResult]:
    """Run every check; checks named in `policy.soft_checks` are marked soft."""
    out = []
    for chk in CHECKS:
        r = chk(g, policy)
        hard = r.name not in policy.soft_checks
        out.append(CheckResult(r.name, r.status, hard, r.summary, r.numbers))
    return out


def decide(results: list[CheckResult]) -> tuple[Decision, str]:
    """Any hard fail -> REJECT; else any hard insufficient -> INSUFFICIENT_EVIDENCE; else
    PROMOTE. Soft failures are named in the reason but never block."""
    hard_fail = [r for r in results if r.hard and r.status == "fail"]
    hard_thin = [r for r in results if r.hard and r.status == "insufficient"]
    soft = [r for r in results if not r.hard and r.status != "pass"]
    note = (
        " Note (does not block): " + " ".join(f"[{r.name}] {r.summary}" for r in soft)
        if soft
        else ""
    )
    if hard_fail:
        return "REJECT", (
            "Rejected because " + " ".join(f"[{r.name}] {r.summary}" for r in hard_fail) + note
        )
    if hard_thin:
        return "INSUFFICIENT_EVIDENCE", (
            "Not enough evidence yet: "
            + " ".join(f"[{r.name}] {r.summary}" for r in hard_thin)
            + note
        )
    return "PROMOTE", (
        "Promoted: every hard check passed. "
        + " ".join(f"[{r.name}] {r.summary}" for r in results if r.hard)
        + note
    )
