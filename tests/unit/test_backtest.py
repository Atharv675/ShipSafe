"""Stage 7b Pass 1: point-in-time backtest (owner brief, answers 2026-11-24)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from sentinel.backtest import pass1 as p1
from sentinel.backtest.metadata import COLUMNS, metadata_row
from sentinel.config import BacktestConfig, candidate_id, load_backtest_config, load_gate_policy
from sentinel.experiment.export import V1_ID
from sentinel.experiment.scores import ModelInfo, ScoreTable
from sentinel.models.evaluate import (
    capped_threshold,
    cluster_bootstrap_rows,
    percentile_interval,
    threshold_at_fpr,
)

DAY = 86_400
WEEKS = list(range(40, 65))
PER_CLASS = 1_200
FPR = 0.01  # a small synthetic week can measure 1%, not 0.1%
GATE = load_gate_policy().model_copy(update={"operating_fpr": FPR})
CFG: BacktestConfig = load_backtest_config().model_copy(
    update={"operating_fpr": FPR, "retrain_policies": ["all_history"]}
)
CFG = CFG.model_copy(update={"harm": CFG.harm.model_copy(update={"bootstrap_resamples": 30})})
STRENGTH = {52: 0.4, 54: 2.8, 56: 0.4, 58: 2.8, 60: 0.4, 62: 2.8}
FAMILIES = 12


def synthetic(drift: float = 0.0, leaky: bool = False) -> tuple[ScoreTable, pd.DataFrame]:
    """Malware in FAMILIES families (+ some without one); benign scores drift upward by
    `drift` per week after week 52 (so a fixed cut-off's FPR creeps up)."""
    rng = np.random.default_rng(11)
    week = np.repeat(WEEKS, 2 * PER_CLASS).astype(np.int64)
    y = np.tile(np.repeat([0, 1], PER_CLASS), len(WEEKS)).astype(np.int8)
    fam_id = rng.integers(-2, FAMILIES, week.size)  # < 0: no family
    fam_effect = rng.normal(0, 0.6, FAMILIES)
    shared = rng.normal(size=week.size)
    shift = np.where(y == 0, drift * np.clip(week - 52, 0, None), 0.0)
    raw: dict[str, Any] = {}
    nb: dict[str, Any] = {}
    models: dict[str, ModelInfo] = {}

    def model(mid: str, strength: float, as_of: int, fit: list[int], cal: list[int]) -> None:
        fe = np.where((y == 1) & (fam_id >= 0), fam_effect[np.clip(fam_id, 0, None)], 0.0)
        z = strength * (2 * y - 1) + shared + fe + shift + 0.3 * rng.normal(size=y.size)
        r = 1 / (1 + np.exp(-z))
        m = np.isin(week, cal)
        raw[mid] = r
        models[mid] = ModelInfo(
            mid, mid, as_of, threshold_at_fpr(y[m], r[m], FPR), tuple(fit), tuple(cal)
        )
        nb[mid] = np.full(20, 1e-6)

    model(V1_ID, 1.5, 52, list(range(1, 48)), [48, 49, 50, 51])
    for t, s in STRENGTH.items():
        newest = t if leaky else t - CFG.label_delay_weeks
        model(
            candidate_id("all_history", t),
            s,
            t,
            list(range(1, newest - 3)),
            list(range(newest - 3, newest + 1)),
        )
    ids = np.array([f"f{i}" for i in range(week.size)])
    st = ScoreTable(
        ids,
        week,
        y,
        np.zeros(week.size, dtype=np.int64),
        raw,
        dict(raw),
        nb,
        models,
        {w: 7 * DAY * w for w in range(1, 65)},
        None,
        {},
    )
    meta = pd.DataFrame(
        {
            "sha256": ids,
            "week": week,
            "label": y,
            "family": [
                f"fam{k}" if (lab == 1 and k >= 0) else None
                for lab, k in zip(y, fam_id, strict=True)
            ],
            "family_confidence": 1.0,
            "num_certs": 0.0,
            "self_signed": 0.0,
        }
    )
    return st, meta


@pytest.fixture(scope="module")
def result() -> dict[str, Any]:
    st, meta = synthetic()
    return p1.run_pass1(st, meta, CFG, GATE)


# ------------------------------------------------------------------ config + statistics


def test_backtest_config_holds_the_owners_answers() -> None:
    c = load_backtest_config()
    assert c.label_delay_weeks == 6 and c.harm.recall_floor_points == 0.5
    assert c.recalibration.max_fpr_move_pp == 0.05
    assert (
        c.p4.flag_rate_tolerance,
        c.p4.new_family_weeks,
        c.p4.top_signers,
        c.p4.min_early_agreement,
    ) == (0.10, 4, 10, 0.90)
    assert c.p3_decision_week(52) == 60 and c.labelled_upto(60) == 54
    with pytest.raises(ValueError, match="before the test weeks"):
        BacktestConfig.model_validate(
            {
                **c.model_dump(),
                "harm": {**c.harm.model_dump(), "noise_weeks": {"first": 50, "last": 54}},
            }
        )


def test_cluster_bootstrap_keeps_families_together_and_counts_per_stratum() -> None:
    clusters = np.array(["a", "a", "a", "b", "c", "c", "x1", "x2", "x3"])
    strata = np.array([1, 1, 1, 1, 1, 1, 0, 0, 0])
    rng = np.random.default_rng(3)
    for _ in range(50):
        rows = cluster_bootstrap_rows(clusters, strata, rng)
        drawn = clusters[rows]
        for fam, size in (("a", 3), ("c", 2)):
            assert (drawn == fam).sum() % size == 0  # a family comes whole, never split
        assert np.isin(rows[strata[rows] == 0], [6, 7, 8]).all()
        assert (strata[rows] == 0).sum() == 3  # 3 benign clusters drawn, 1 row each
    again = cluster_bootstrap_rows(clusters, strata, np.random.default_rng(9))
    assert (again == cluster_bootstrap_rows(clusters, strata, np.random.default_rng(9))).all()


def test_capped_threshold_moves_at_most_the_cap() -> None:
    benign = np.linspace(0, 1, 10_001)
    far = capped_threshold(benign, current=0.5, target_fpr=0.01, max_fpr_move=0.05)
    assert (benign > far).mean() == pytest.approx(0.45, abs=1e-3)  # 50% -> 45%, not 1%
    near = capped_threshold(benign, current=0.985, target_fpr=0.01, max_fpr_move=0.05)
    assert (benign > near).mean() <= 0.01  # target reachable within the cap
    jump = capped_threshold(benign, current=0.5, target_fpr=0.01, max_fpr_move=np.inf)
    assert (benign > jump).mean() <= 0.01
    assert capped_threshold(np.array([]), 0.7, 0.01, 0.05) == 0.7
    assert percentile_interval([1.0, 2.0, 3.0, float("nan")], 0.5) == (1.5, 2.5)


def test_cluster_keys_use_family_for_malware_only() -> None:
    st, meta = synthetic()
    keys = p1.cluster_keys(st, meta)
    mal_fam = (st.y == 1) & meta["family"].notna().to_numpy()
    assert all(k.startswith("fam:") for k in keys[mal_fam])
    assert all(k.startswith("file:") for k in keys[~mal_fam])
    assert all(k.startswith("file:") for k in p1.cluster_keys(st, None))


def test_metadata_row_keeps_only_allowed_fields() -> None:
    rec = {
        "sha256": "ab",
        "label": 1,
        "family": "njrat",
        "family_confidence": "0.9",
        "authenticode": {"num_certs": 2, "self_signed": 1},
        "histogram": [1, 2],
        "caps": ["x"],
    }
    row = metadata_row(rec, 7)
    assert row is not None and list(row) == COLUMNS
    assert (row["family"], row["family_confidence"], row["num_certs"]) == ("njrat", 0.9, 2.0)
    assert metadata_row({"label": 0}, 1) is None
    assert metadata_row({"sha256": "c", "family": ""}, 1)["family"] is None  # type: ignore[index]


# ------------------------------------------------------------------ pass 1 logic


def test_noise_is_measured_before_the_test_weeks_and_sets_the_margin(
    result: dict[str, Any],
) -> None:
    n = result["noise"]
    assert n.weeks == [48, 49, 50, 51, 52] and max(n.weeks) < CFG.test_weeks.first
    assert len(n.week_to_week_changes) == 4
    assert n.margin_points == max(n.noise_points, 0.5)


def test_outcome_needs_margin_and_ci_excluding_zero() -> None:
    j = p1.Judged((60,), 0.8, 0.9, -2.0, -3.0, -1.0, 0)
    assert p1.outcome(j, 0.5) == "worse"
    assert p1.outcome(j, 2.5) == "within noise"  # loss smaller than M
    wide = p1.Judged((60,), 0.8, 0.9, -2.0, -3.0, 0.4, 0)
    assert p1.outcome(wide, 0.5) == "within noise"  # CI includes 0
    good = p1.Judged((60,), 0.9, 0.8, 2.0, 1.0, 3.0, 0)
    assert p1.outcome(good, 0.5) == "better"
    hit = p1.Judged((60,), 0.9, 0.8, 2.0, 1.0, 3.0, 1)
    assert p1.outcome(hit, 0.5) == "worse"  # a never-break hit is always harm
    assert p1.outcome(p1.Judged((), None, None, None, None, None, 0), 0.5) == "not judged"


def test_p3_decides_only_when_its_shadow_labels_exist(result: dict[str, Any]) -> None:
    d = result["decisions"]
    p3 = d[(d["release_policy"] == "P3_gate") & ~d["recalibration"]]
    assert dict(zip(p3["as_of_week"], p3["decision_week"], strict=True)) == {
        52: 60,
        54: 62,
        56: 64,
        58: 66,
        60: 68,
        62: 70,
    }
    assert set(p3[p3["decided"]]["as_of_week"]) == {52, 54, 56}
    p1d = d[(d["release_policy"] == "P1_auto") & ~d["recalibration"]]
    assert p1d["decided"].all() and p1d["promoted"].all()
    assert (p1d["decision_week"] == p1d["as_of_week"]).all()


def test_the_gate_and_p2_block_weak_candidates_p1_ships_them(result: dict[str, Any]) -> None:
    d = result["decisions"]
    first = d[(d["as_of_week"] == 52) & ~d["recalibration"]].set_index("release_policy")
    assert first.loc["P1_auto", "promoted"]
    assert not first.loc["P2_val_auc", "promoted"]
    assert not first.loc["P3_gate", "promoted"] and first.loc["P3_gate", "blocked_by"]
    assert first.loc["P1_auto", "outcome"] == "worse"  # the weak candidate really is worse


def test_original_p3_used_labels_before_they_existed(result: dict[str, Any]) -> None:
    leak = result["p3_leak"]
    assert all(r["leaky"] for r in leak)
    first = leak[0]
    assert first["original_decision_week"] == 58 and first["labels_used_before_they_exist"]
    assert first["point_in_time_decision_week"] == 60


def test_recalibration_pulls_the_shipped_fpr_back_toward_target() -> None:
    st, meta = synthetic(drift=0.12)
    out = p1.run_pass1(st, meta, CFG, GATE)
    w = out["weekly"]
    p1w = w[w["release_policy"] == "P1_auto"]
    off = p1w[~p1w["recalibration"]]["shipped_fpr"].mean()
    on = p1w[p1w["recalibration"]]["shipped_fpr"].mean()
    assert abs(on - FPR) < abs(off - FPR)
    # recall at the fixed FPR is a property of the model, not of the cut-off
    assert np.allclose(
        sorted(p1w[~p1w["recalibration"]]["recall_at_fpr"]),
        sorted(p1w[p1w["recalibration"]]["recall_at_fpr"]),
    )


def test_recalibration_pool_never_uses_the_models_training_weeks() -> None:
    st, meta = synthetic()
    ctx = p1.Ctx(st, CFG, GATE, p1.cluster_keys(st, meta), "family")
    # decision at week 52 sees weeks 43-46 only: all inside v1's fit window (1-47)
    assert p1.clean_pool(ctx, V1_ID, 52).size == 0
    assert p1.next_cutoff(ctx, V1_ID, 0.77, 52) == 0.77  # nothing to learn from: unchanged
    # week 55: newest labelled 49 -> pool 46-49 minus fit weeks = 48, 49 (benign only)
    pool = p1.clean_pool(ctx, V1_ID, 55)
    assert pool.size == 2 * PER_CLASS  # 2 weeks x PER_CLASS benign files
    benign_48_49 = ctx.rows([48, 49])[st.y[ctx.rows([48, 49])] == 0]
    assert np.array_equal(pool, st.raw[V1_ID][benign_48_49])


def test_mean_recall_has_a_family_bootstrap_interval(result: dict[str, Any]) -> None:
    ci = result["mean_recall_ci"]
    assert result["cluster_level"] == "family"
    assert ((ci["ci_low"] <= ci["mean_recall"]) & (ci["mean_recall"] <= ci["ci_high"])).all()


def test_early_labels_are_marked_simulated(result: dict[str, Any]) -> None:
    e = result["early_labels"]
    assert e["simulated"].all()
    for rate, g in e.groupby("flip_rate"):
        assert g["agreement"].mean() == pytest.approx(1 - rate, abs=0.01)


def test_refuses_candidates_that_used_labels_not_yet_available() -> None:
    st, meta = synthetic(leaky=True)
    with pytest.raises(ValueError, match="used weeks after T-6"):
        p1.run_pass1(st, meta, CFG, GATE)


def test_deterministic_for_a_seed(result: dict[str, Any]) -> None:
    st, meta = synthetic()
    again = p1.run_pass1(st, meta, CFG, GATE)
    pd.testing.assert_frame_equal(again["decisions"], result["decisions"])
    pd.testing.assert_frame_equal(again["mean_recall_ci"], result["mean_recall_ci"])


def test_summary_and_report_keep_hand_written_text(result: dict[str, Any], tmp_path: Any) -> None:
    from sentinel.backtest.report import AUTO_BEGIN, AUTO_END, write_pass1_md

    table = p1.summary_table(result["mean_recall_ci"], result["decisions"], result["weekly"])
    assert len(table) == 6  # 3 policies x recalibration on/off
    p3 = table[(table["release_policy"] == "P3_gate") & ~table["recalibration"]].iloc[0]
    assert (p3["evaluated"], p3["decided"]) == (6, 3)
    st, meta = synthetic()
    original = (
        result["decisions"][
            (result["decisions"]["release_policy"] == "P1_auto") & result["decisions"]["decided"]
        ]
        .head(2)
        .assign(recall_gain=-0.01)
    )
    redo = p1.rejudge(st, meta, CFG, GATE, original, result["noise"].margin_points)
    assert len(redo) == 2 and set(redo["outcome_now"]) <= {"worse", "within noise", "better"}
    prov = {
        "seed": 1,
        "data_fingerprint": "f" * 16,
        "git_commit": "c" * 16,
        "backtest_config": CFG.model_dump(mode="json"),
    }
    md = tmp_path / "BACKTEST.md"
    write_pass1_md(md, result, table, redo, prov)
    md.write_text(md.read_text("utf-8").replace("(written after the results)", "MY NOTES"))
    write_pass1_md(md, result, table, redo, prov)
    text = md.read_text("utf-8")
    assert "MY NOTES" in text and text.count(AUTO_BEGIN) == text.count(AUTO_END) == 1
    assert "M = max(noise, X)" in text and "SIMULATED" in text
