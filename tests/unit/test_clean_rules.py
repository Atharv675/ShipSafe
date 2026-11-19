"""One test per Stage 1 cleaning rule, on tiny inputs."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from sentinel.config import LateLabelConfig, load_clean_config
from sentinel.data.clean import (
    SECONDS_PER_DAY,
    StepCount,
    WeekRows,
    constant_columns,
    feature_schema_version,
    fill_non_finite,
    label_available_at,
    merge_min_max,
    never_break_quotas,
    pick_never_break,
    plan_keep_masks,
    row_hashes,
    unit_draws,
    valid_label,
)

CFG = load_clean_config()


def _week(split: str, ids: list[str], vecs: list[list[float]], labels: list[int]) -> WeekRows:
    x = np.asarray(vecs, dtype=np.float32)
    return WeekRows(split, ids, row_hashes(x, 16), np.asarray(labels, dtype=np.int8))


def test_valid_label_rejects_unknown_and_missing() -> None:
    df = pd.DataFrame({"label": [0, 1, None, -1, 2, True, 1.0, "1"]})
    kept = [valid_label(v, [0, 1]) for v in df["label"].astype(object)]
    assert kept == [True, True, False, False, False, False, False, False]


def test_fill_non_finite_counts_and_replaces() -> None:
    x = np.array([[1.0, np.nan], [np.inf, -np.inf]], dtype=np.float32)
    assert fill_non_finite(x, 0.0) == 3
    assert x.tolist() == [[1.0, 0.0], [0.0, 0.0]]
    assert fill_non_finite(x, 0.0) == 0


def test_row_hashes_identify_identical_vectors_only() -> None:
    x = np.array([[1, 2], [1, 2], [2, 1]], dtype=np.float32)
    h = row_hashes(x, 16)
    assert h[0] == h[1] != h[2] and len(h[0]) == 16


def test_duplicate_vectors_within_split_keep_earliest_and_count_conflicts() -> None:
    plan = plan_keep_masks(
        [
            _week("train", ["a", "b"], [[1, 1], [2, 2]], [0, 1]),
            _week("train", ["c", "d"], [[1, 1], [2, 2]], [1, 1]),  # c conflicts with a
        ]
    )
    assert [m.tolist() for m in plan.masks] == [[True, True], [False, False]]
    assert plan.removed["train"]["drop_duplicate_vector"] == 2
    assert plan.label_conflicts["train"] == 1


def test_cross_week_repeat_keeps_first_week() -> None:
    plan = plan_keep_masks(
        [
            _week("test", ["a"], [[1, 1]], [0]),
            _week("test", ["a"], [[1, 1]], [0]),
        ]
    )
    assert plan.masks[1].tolist() == [False]
    assert plan.removed["test"]["drop_cross_week_repeat"] == 1


def test_test_rows_seen_in_train_are_removed_as_leakage() -> None:
    plan = plan_keep_masks(
        [
            _week("train", ["a", "b"], [[1, 1], [2, 2]], [0, 1]),
            _week("test", ["a", "x", "y"], [[9, 9], [2, 2], [3, 3]], [0, 1, 1]),
        ]
    )
    assert plan.masks[1].tolist() == [False, False, True]
    assert plan.removed["test"]["drop_test_id_in_train"] == 1
    assert plan.removed["test"]["drop_test_vector_in_train"] == 1


def test_weeks_out_of_time_order_are_rejected() -> None:
    with pytest.raises(ValueError, match="time order"):
        plan_keep_masks([_week("test", ["a"], [[1]], [0]), _week("train", ["b"], [[2]], [0])])


def test_constant_columns_from_train_min_max() -> None:
    lo, hi = merge_min_max(
        [
            (np.array([0, 5, 1], np.float32), np.array([0, 5, 2], np.float32)),
            (np.array([0, 5, 0], np.float32), np.array([0, 5, 1], np.float32)),
        ]
    )
    assert constant_columns(lo, hi) == [0, 1]
    with pytest.raises(ValueError):
        constant_columns(lo, hi[:2])


def test_late_label_delay_is_seeded_and_in_range() -> None:
    cfg = LateLabelConfig(distribution="uniform", min_days=7, max_days=28)
    ids = [f"id{i}" for i in range(500)]
    t0 = np.full(len(ids), 1_700_000_000, dtype=np.int64)
    a = label_available_at(ids, t0, cfg, seed=1)
    assert np.array_equal(a, label_available_at(ids, t0, cfg, seed=1))
    assert not np.array_equal(a, label_available_at(ids, t0, cfg, seed=2))
    days = (a - t0) / SECONDS_PER_DAY
    assert days.min() >= 7 and days.max() <= 28 and days.max() - days.min() > 15
    # independent of row order
    assert label_available_at(ids[::-1], t0, cfg, seed=1)[0] == a[-1]


def test_late_label_config_rejects_inverted_range() -> None:
    with pytest.raises(ValueError):
        LateLabelConfig(distribution="uniform", min_days=10, max_days=5)


def test_unit_draws_are_in_unit_interval() -> None:
    u = unit_draws([str(i) for i in range(1000)], 7, "x")
    assert u.min() >= 0 and u.max() < 1


def test_never_break_quotas_spread_evenly() -> None:
    q = never_break_quotas(52, 500)
    assert sum(q) == 500 and max(q) - min(q) <= 1 and q[0] >= q[-1]
    assert never_break_quotas(0, 500) == []


def test_never_break_picks_only_benign_and_is_deterministic() -> None:
    ids = [f"f{i}" for i in range(20)]
    labels = np.array([i % 2 for i in range(20)], dtype=np.int8)  # even = benign
    picks = pick_never_break(ids, labels, 4, seed=3, benign_label=0)
    assert len(picks) == 4 and all(labels[i] == 0 for i in picks)
    assert picks == pick_never_break(ids, labels, 4, seed=3, benign_label=0)
    assert len(pick_never_break(ids, labels, 50, seed=3, benign_label=0)) == 10


def test_feature_schema_version_tracks_columns_and_config_only() -> None:
    cols = [("f_0000", "float32")]
    v = feature_schema_version(cols, CFG)
    assert v == feature_schema_version(cols, CFG.model_copy(update={"file_types": ["PDF"]}))
    assert v != feature_schema_version([("f_0001", "float32")], CFG)
    assert v != feature_schema_version(cols, CFG.model_copy(update={"non_finite_fill": -1.0}))


def test_step_count_reports_removed() -> None:
    s = StepCount("drop_bad_label", "train", 10, 7)
    assert s.removed == 3 and s.as_dict()["removed"] == 3
