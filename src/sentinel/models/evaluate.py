"""All metrics and statistics: pure, typed functions. Services and dashboards import these.

Conventions
- `y` is 1 = malicious, 0 = benign. `scores` are higher = more malicious.
- A file is FLAGGED when `score > threshold` (strictly greater), so a threshold chosen with
  `threshold_at_fpr` never flags more benign files than allowed, even with tied scores.
- Every comparison between models is at a FIXED false-positive rate, never at 0.5.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from fractions import Fraction
from statistics import NormalDist
from typing import Any

import numpy as np
import numpy.typing as npt

ArrayLike = npt.NDArray[Any] | Sequence[float] | Sequence[int]


def _arrays(
    y: ArrayLike, scores: ArrayLike
) -> tuple[npt.NDArray[np.int8], npt.NDArray[np.float64]]:
    yy = np.asarray(y).astype(np.int8)
    ss = np.asarray(scores, dtype=np.float64)
    if yy.shape != ss.shape or yy.ndim != 1:
        raise ValueError("y and scores must be 1-D arrays of the same length")
    if not np.isin(yy, (0, 1)).all():
        raise ValueError("y must contain only 0 (benign) and 1 (malicious)")
    if not np.isfinite(ss).all():
        raise ValueError("scores must be finite")
    return yy, ss


# ---------------------------------------------------------------------------- intervals


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion; (0, 1) when n == 0."""
    if n < 0 or successes < 0 or successes > n:
        raise ValueError("need 0 <= successes <= n")
    if n == 0:
        return 0.0, 1.0
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


# ---------------------------------------------------------------------------- fixed-FPR metrics


def threshold_at_fpr(y: ArrayLike, scores: ArrayLike, target_fpr: float) -> float:
    """Smallest threshold whose benign flag rate (score > threshold) is <= target_fpr."""
    yy, ss = _arrays(y, scores)
    if not 0 <= target_fpr < 1:
        raise ValueError("target_fpr must be in [0, 1)")
    benign = np.sort(ss[yy == 0])[::-1]
    if benign.size == 0:
        raise ValueError("need at least one benign file to set a threshold")
    allowed = math.floor(target_fpr * benign.size + 1e-9)  # false alarms we may raise
    return (
        float(benign[allowed])
        if allowed < benign.size
        else float(np.nextafter(benign[-1], -np.inf))
    )


@dataclass(frozen=True)
class RateAtThreshold:
    """Recall and FPR at a fixed threshold, with Wilson intervals."""

    threshold: float
    tp: int
    positives: int
    fp: int
    negatives: int
    recall: float
    recall_ci: tuple[float, float]
    fpr: float
    fpr_ci: tuple[float, float]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["recall_ci"], d["fpr_ci"] = list(self.recall_ci), list(self.fpr_ci)
        return d


def rates_at_threshold(
    y: ArrayLike, scores: ArrayLike, threshold: float, confidence: float = 0.95
) -> RateAtThreshold:
    yy, ss = _arrays(y, scores)
    flagged = ss > threshold
    pos, neg = int((yy == 1).sum()), int((yy == 0).sum())
    tp, fp = int((flagged & (yy == 1)).sum()), int((flagged & (yy == 0)).sum())
    return RateAtThreshold(
        threshold=float(threshold),
        tp=tp,
        positives=pos,
        fp=fp,
        negatives=neg,
        recall=tp / pos if pos else float("nan"),
        recall_ci=wilson_interval(tp, pos, confidence),
        fpr=fp / neg if neg else float("nan"),
        fpr_ci=wilson_interval(fp, neg, confidence),
    )


def recall_at_fpr(
    y: ArrayLike, scores: ArrayLike, target_fpr: float, confidence: float = 0.95
) -> RateAtThreshold:
    """Recall at the threshold chosen on THESE scores (in-sample; use for the calibration set)."""
    return rates_at_threshold(y, scores, threshold_at_fpr(y, scores, target_fpr), confidence)


# ---------------------------------------------------------------------------- ranking metrics


def _average_ranks(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    _, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
    upper = np.cumsum(counts)
    avg = upper - (counts - 1) / 2.0  # 1-based average rank of each tie group
    ranks: npt.NDArray[np.float64] = avg[inverse].astype(np.float64)
    return ranks


def auc(y: ArrayLike, scores: ArrayLike) -> float:
    """ROC AUC via the Mann-Whitney U statistic (ties count one half)."""
    yy, ss = _arrays(y, scores)
    n1, n0 = int((yy == 1).sum()), int((yy == 0).sum())
    if n1 == 0 or n0 == 0:
        raise ValueError("AUC needs both classes")
    r = _average_ranks(ss)
    return float((r[yy == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def auprc(y: ArrayLike, scores: ArrayLike) -> float:
    """Average precision: sum over distinct thresholds of (R_n - R_{n-1}) * P_n."""
    yy, ss = _arrays(y, scores)
    n1 = int((yy == 1).sum())
    if n1 == 0:
        raise ValueError("AUPRC needs at least one malicious file")
    order = np.argsort(-ss, kind="mergesort")
    s_sorted, y_sorted = ss[order], yy[order]
    last_of_group = np.r_[np.flatnonzero(np.diff(s_sorted)), s_sorted.size - 1]
    tp = np.cumsum(y_sorted)[last_of_group]
    seen = last_of_group + 1
    precision = tp / seen
    recall = tp / n1
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


# ---------------------------------------------------------------------------- calibration


@dataclass(frozen=True)
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    fraction_malicious: float


def reliability_curve(
    y: ArrayLike, probabilities: ArrayLike, n_bins: int = 10
) -> list[ReliabilityBin]:
    """Equal-width bins on [0, 1]; empty bins are omitted."""
    yy, pp = _arrays(y, probabilities)
    if ((pp < 0) | (pp > 1)).any():
        raise ValueError("probabilities must be in [0, 1]")
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.searchsorted(edges, pp, side="right") - 1, 0, n_bins - 1)
    out = []
    for b in range(n_bins):
        m = idx == b
        if m.any():
            out.append(
                ReliabilityBin(
                    float(edges[b]),
                    float(edges[b + 1]),
                    int(m.sum()),
                    float(pp[m].mean()),
                    float(yy[m].mean()),
                )
            )
    return out


def expected_calibration_error(y: ArrayLike, probabilities: ArrayLike, n_bins: int = 10) -> float:
    """ECE = sum over bins of (bin share) * |fraction malicious - mean predicted|."""
    bins = reliability_curve(y, probabilities, n_bins)
    total = sum(b.count for b in bins)
    return float(sum(b.count / total * abs(b.fraction_malicious - b.mean_predicted) for b in bins))


# ---------------------------------------------------------------------------- per week


def per_week_metrics(
    weeks: ArrayLike,
    y: ArrayLike,
    scores: ArrayLike,
    threshold: float,
    confidence: float = 0.95,
) -> list[dict[str, Any]]:
    """Recall / FPR at a fixed threshold for each week (in week order)."""
    ww = np.asarray(weeks)
    yy, ss = _arrays(y, scores)
    if ww.shape != yy.shape:
        raise ValueError("weeks must align with y")
    out = []
    for w in np.unique(ww):
        m = ww == w
        out.append(
            {"week": int(w), **rates_at_threshold(yy[m], ss[m], threshold, confidence).as_dict()}
        )
    return out


# ---------------------------------------------------------------------------- paired comparison


@dataclass(frozen=True)
class PairedTable:
    """2x2 table of two models' correctness on the SAME files.

    `only_a_right` / `only_b_right` are the discordant ("flipped") cells McNemar tests."""

    both_right: int
    only_a_right: int
    only_b_right: int
    both_wrong: int

    @property
    def discordant(self) -> int:
        return self.only_a_right + self.only_b_right

    def as_dict(self) -> dict[str, int]:
        return {**asdict(self), "discordant": self.discordant}


def paired_table(y: ArrayLike, flagged_a: ArrayLike, flagged_b: ArrayLike) -> PairedTable:
    """A model is right on a file when it flags a malicious file or passes a benign one."""
    yy = np.asarray(y).astype(np.int8)
    fa, fb = np.asarray(flagged_a, dtype=bool), np.asarray(flagged_b, dtype=bool)
    if not (yy.shape == fa.shape == fb.shape) or yy.ndim != 1:
        raise ValueError("y and both flag arrays must be 1-D and the same length")
    if not np.isin(yy, (0, 1)).all():
        raise ValueError("y must contain only 0 (benign) and 1 (malicious)")
    truth = yy == 1
    ra, rb = fa == truth, fb == truth
    return PairedTable(
        both_right=int((ra & rb).sum()),
        only_a_right=int((ra & ~rb).sum()),
        only_b_right=int((~ra & rb).sum()),
        both_wrong=int((~ra & ~rb).sum()),
    )


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the two discordant counts.

    Under "both models are equally good", each disagreement is a fair coin flip, so
    min(b, c) ~ Binomial(b + c, 1/2); p = 2 * P(X <= min(b, c)), capped at 1. Computed with
    exact integers (no normal approximation, valid for small counts)."""
    if b < 0 or c < 0:
        raise ValueError("counts must be >= 0")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, float(Fraction(2 * tail, 2**n)))


# ---------------------------------------------------------------------------- operations


@dataclass(frozen=True)
class LatencySummary:
    count: int
    sum: float
    p50: float
    p95: float
    p99: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def latency_summary(values: ArrayLike) -> LatencySummary:
    """Count, sum and p50/p95/p99 (linear interpolation, numpy's default) of samples.

    Empty input gives count 0 and NaN percentiles (no samples is not "zero latency").
    """
    v = np.asarray(values, dtype=np.float64)
    if v.ndim != 1:
        raise ValueError("values must be 1-D")
    if v.size == 0:
        nan = float("nan")
        return LatencySummary(0, 0.0, nan, nan, nan)
    if not np.isfinite(v).all():
        raise ValueError("values must be finite")
    p50, p95, p99 = np.percentile(v, [50, 95, 99])
    return LatencySummary(int(v.size), float(v.sum()), float(p50), float(p95), float(p99))


@dataclass(frozen=True)
class RateComparison:
    """A candidate's rate (e.g. share of files flagged) against production's."""

    candidate: int
    candidate_n: int
    production: int
    production_n: int
    candidate_ci: tuple[float, float]
    production_ci: tuple[float, float]
    limit: float  # breach when candidate_ci[0] > limit
    breach: bool

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["candidate_rate"] = self.candidate / self.candidate_n if self.candidate_n else None
        d["production_rate"] = self.production / self.production_n if self.production_n else None
        return d


def rate_increase_check(
    candidate: int,
    candidate_n: int,
    production: int,
    production_n: int,
    max_relative_increase: float,
    confidence: float = 0.95,
) -> RateComparison:
    """Is the candidate's rate CLEARLY above production's, beyond the allowed increase?

    Breach only when the lower Wilson bound of the candidate's rate exceeds the upper Wilson
    bound of production's times (1 + max_relative_increase). Both intervals must be cleared,
    so a handful of events can never cause a breach by noise; with no events on either side
    there is nothing to compare and no breach.
    """
    if max_relative_increase < 0:
        raise ValueError("max_relative_increase must be >= 0")
    c_ci = wilson_interval(candidate, candidate_n, confidence)
    p_ci = wilson_interval(production, production_n, confidence)
    limit = p_ci[1] * (1 + max_relative_increase)
    breach = candidate_n > 0 and production_n > 0 and c_ci[0] > limit
    return RateComparison(
        candidate, candidate_n, production, production_n, c_ci, p_ci, limit, breach
    )


@dataclass(frozen=True)
class DisagreementCheck:
    """Live share of paired files where two models disagree in one direction, against the
    share that was expected (approved)."""

    disagreements: int
    n: int
    ci: tuple[float, float]
    expected_upper: float  # upper end of the expected rate (0 = the models must agree)
    limit: float  # breach when ci[0] > limit
    breach: bool

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["rate"] = self.disagreements / self.n if self.n else None
        return d


def disagreement_check(
    disagreements: int,
    n: int,
    expected_upper: float,
    max_increase: float,
    confidence: float = 0.95,
) -> DisagreementCheck:
    """Do the models disagree CLEARLY more often than approved?

    Both models score the same files, so file mix and drift cancel out. Breach only when the
    lower Wilson bound of the live disagreement rate exceeds `expected_upper + max_increase`
    (absolute), so noise in a few hundred files cannot cause a breach; no files, no breach.
    """
    if max_increase < 0 or not 0 <= expected_upper <= 1:
        raise ValueError("need max_increase >= 0 and 0 <= expected_upper <= 1")
    ci = wilson_interval(disagreements, n, confidence)
    limit = expected_upper + max_increase
    return DisagreementCheck(disagreements, n, ci, expected_upper, limit, n > 0 and ci[0] > limit)


def expected_disagreement_upper(k: int, n: int, confidence: float = 0.95) -> float:
    """Upper Wilson bound of an expected rate measured as k of n (e.g. by the release gate).
    n == 0 means "no disagreement expected at all" (a copy of production): 0."""
    return 0.0 if n == 0 else wilson_interval(k, n, confidence)[1]


def population_stability_index(
    reference_counts: ArrayLike, current_counts: ArrayLike, epsilon: float = 1e-4
) -> float:
    """PSI = sum (cur - ref) * ln(cur / ref) over bins, from per-bin COUNTS; empty bins are
    floored at `epsilon` (as a share) so the log is finite. 0 = identical distributions;
    rule of thumb: < 0.1 stable, 0.1-0.25 moderate shift, > 0.25 large shift."""
    ref = np.asarray(reference_counts, dtype=np.float64)
    cur = np.asarray(current_counts, dtype=np.float64)
    if ref.shape != cur.shape or ref.ndim != 1 or ref.size < 2:
        raise ValueError("need two 1-D count arrays of the same length (>= 2 bins)")
    if (ref < 0).any() or (cur < 0).any() or ref.sum() == 0 or cur.sum() == 0:
        raise ValueError("counts must be >= 0 and each side must have at least one row")
    p = np.maximum(ref / ref.sum(), epsilon)
    q = np.maximum(cur / cur.sum(), epsilon)
    return float(np.sum((q - p) * np.log(q / p)))


def score_bin_counts(scores: ArrayLike, edges: ArrayLike) -> npt.NDArray[np.int64]:
    """Counts of `scores` in the bins defined by interior `edges` (len(edges) + 1 bins)."""
    s = np.asarray(scores, dtype=np.float64)
    e = np.asarray(edges, dtype=np.float64)
    out: npt.NDArray[np.int64] = np.bincount(
        np.searchsorted(e, s, side="right"), minlength=e.size + 1
    ).astype(np.int64)
    return out
