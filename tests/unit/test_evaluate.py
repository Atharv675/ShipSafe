"""evaluate.py: fixed-FPR thresholds, Wilson intervals, AUC/AUPRC, calibration, per-week."""

from __future__ import annotations

import numpy as np
import pytest
from sklearn.metrics import average_precision_score, roc_auc_score

from sentinel.models.evaluate import (
    auc,
    auprc,
    expected_calibration_error,
    per_week_metrics,
    rates_at_threshold,
    recall_at_fpr,
    reliability_curve,
    threshold_at_fpr,
    wilson_interval,
)


def test_threshold_at_fpr_flags_exactly_the_allowed_benign_files() -> None:
    rng = np.random.default_rng(0)
    y = np.r_[np.zeros(10_000, int), np.ones(1_000, int)]
    s = np.r_[rng.uniform(0, 1, 10_000), rng.uniform(0.5, 1.5, 1_000)]
    for target in (0.0, 0.0005, 0.001, 0.01, 0.1):
        thr = threshold_at_fpr(y, s, target)
        fp = int(((s > thr) & (y == 0)).sum())
        assert fp == int(target * 10_000)  # distinct scores: exactly floor(target * n) false alarms


def test_threshold_at_fpr_never_exceeds_target_with_ties() -> None:
    y = np.zeros(1000, int)
    s = np.r_[np.full(5, 0.9), np.linspace(0, 0.5, 995)]  # 5 benign tied at the top
    thr = threshold_at_fpr(y, s, 0.003)  # allows 3, but 5 are tied: must flag 0
    assert ((s > thr) & (y == 0)).sum() <= 3
    assert thr == pytest.approx(0.9)


def test_threshold_edge_cases() -> None:
    assert threshold_at_fpr([0, 0, 1], [0.1, 0.2, 0.9], 0.99) == 0.1  # 1 of 2 benign allowed
    assert threshold_at_fpr([0, 1], [0.1, 0.9], 1 - 1e-12) < 0.1  # every benign allowed
    with pytest.raises(ValueError):
        threshold_at_fpr([1, 1], [0.1, 0.2], 0.01)  # no benign files
    with pytest.raises(ValueError):
        threshold_at_fpr([0, 1], [0.1, 0.2], 1.0)
    with pytest.raises(ValueError):
        threshold_at_fpr([0, 2], [0.1, 0.2], 0.1)
    with pytest.raises(ValueError):
        threshold_at_fpr([0, 1], [0.1, np.nan], 0.1)
    with pytest.raises(ValueError):
        threshold_at_fpr([0, 1, 1], [0.1, 0.2], 0.1)


def test_wilson_matches_textbook_values() -> None:
    lo, hi = wilson_interval(5, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.2366, 0.7634)
    lo, hi = wilson_interval(0, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.0, 0.2775)
    lo, hi = wilson_interval(10, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.7225, 1.0)
    assert wilson_interval(0, 0) == (0.0, 1.0)
    lo99, hi99 = wilson_interval(5, 10, confidence=0.99)
    assert lo99 < 0.2366 and hi99 > 0.7634
    with pytest.raises(ValueError):
        wilson_interval(11, 10)


def test_recall_at_fpr_and_rates() -> None:
    y = [0, 0, 0, 0, 1, 1, 1, 1]
    s = [0.1, 0.2, 0.3, 0.8, 0.5, 0.6, 0.9, 0.95]
    r = recall_at_fpr(y, s, 0.0)
    assert r.threshold == 0.8 and r.fp == 0 and r.tp == 2 and r.recall == 0.5
    r2 = rates_at_threshold(y, s, 0.25)
    assert (r2.tp, r2.fp, r2.positives, r2.negatives) == (4, 2, 4, 4)
    assert r2.fpr == 0.5 and r2.as_dict()["recall_ci"][1] == 1.0
    empty = rates_at_threshold([0, 0], [0.1, 0.2], 0.5)
    assert np.isnan(empty.recall)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_auc_and_auprc_match_sklearn_including_ties(seed: int) -> None:
    rng = np.random.default_rng(seed)
    y = rng.integers(0, 2, 2000)
    s = np.round(rng.normal(y * 0.7, 1.0), 1)  # rounding creates many ties
    assert auc(y, s) == pytest.approx(roc_auc_score(y, s), abs=1e-12)
    assert auprc(y, s) == pytest.approx(average_precision_score(y, s), abs=1e-12)
    with pytest.raises(ValueError):
        auc([1, 1], [0.1, 0.2])
    with pytest.raises(ValueError):
        auprc([0, 0], [0.1, 0.2])


def test_reliability_curve_and_ece() -> None:
    y = [0, 0, 1, 1, 1]
    p = [0.05, 0.15, 0.95, 0.95, 0.85]
    bins = reliability_curve(y, p, n_bins=10)
    assert [b.count for b in bins] == [1, 1, 1, 2]
    assert bins[-1].mean_predicted == pytest.approx(0.95) and bins[-1].fraction_malicious == 1.0
    expected = (1 * 0.05 + 1 * 0.15 + 1 * 0.15 + 2 * 0.05) / 5
    assert expected_calibration_error(y, p, 10) == pytest.approx(expected)
    assert expected_calibration_error([0, 1], [0.0, 1.0], 10) == 0.0
    with pytest.raises(ValueError):
        reliability_curve([0], [1.5])


def test_per_week_metrics() -> None:
    weeks = [53, 53, 54, 54, 54]
    y = [0, 1, 0, 1, 1]
    s = [0.9, 0.95, 0.1, 0.2, 0.8]
    out = per_week_metrics(weeks, y, s, threshold=0.5)
    assert [r["week"] for r in out] == [53, 54]
    assert (out[0]["fp"], out[0]["tp"]) == (1, 1)
    assert (out[1]["fp"], out[1]["tp"], out[1]["positives"]) == (0, 1, 2)
    with pytest.raises(ValueError):
        per_week_metrics([1], y, s, 0.5)
