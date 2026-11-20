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
