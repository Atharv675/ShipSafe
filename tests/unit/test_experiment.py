"""Stage 7: the main experiment (release policies replayed on exported scores).

Synthetic score tables check the policy logic; one end-to-end test runs the real pipeline
(retrain -> score export -> zip -> verified import -> merge -> simulate -> report) on the
synthetic Stage 1 splits.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

import sentinel.experiment.simulate as sim
from sentinel.config import (
    ExperimentConfig,
    GatePolicy,
    candidate_id,
    load_experiment_config,
    load_gate_policy,
    load_retrain_config,
)
from sentinel.experiment.export import V1_ID
from sentinel.experiment.scores import ModelInfo, ScoreTable
from sentinel.gate.checks import GateInput
from sentinel.models.evaluate import (
    population_stability_index,
    score_bin_counts,
    threshold_at_fpr,
)
from tests.unit.test_train import splits, trained  # noqa: F401  (pytest fixtures)

DAY = 86_400
WEEKS = list(range(45, 65))
PER_CLASS = 2_000
END0 = 1_700_000_000  # end of week 0 (epoch seconds); weeks are 7 days apart

# The real rules, with an operating FPR a small synthetic week can measure.
POLICY: GatePolicy = load_gate_policy().model_copy(update={"operating_fpr": 0.01})
EXP: ExperimentConfig = load_experiment_config().model_copy(
    update={"retrain_policies": ["all_history"], "bootstrap_resamples": 2}
)
# candidate quality per retrain week: separation of the two classes (v1 = 1.5)
STRENGTH = {52: 0.6, 54: 2.6, 56: 0.6, 58: 2.6, 60: 0.6, 62: 2.6}


def _week_end(w: int) -> int:
    return END0 + 7 * DAY * w


def synthetic_table(nb_hit: str | None = None) -> ScoreTable:
    rng = np.random.default_rng(7)
    week = np.repeat(WEEKS, 2 * PER_CLASS).astype(np.int64)
    y = np.tile(np.repeat([0, 1], PER_CLASS), len(WEEKS)).astype(np.int8)
    seen = np.array([_week_end(w) for w in week]) - rng.integers(0, 7 * DAY, week.size)
    label_at = (seen + rng.integers(7 * DAY, 28 * DAY + 1, week.size)).astype(np.int64)
    noise = rng.normal(size=week.size)  # shared: models disagree only through strength

    def model(mid: str, strength: float, as_of: int, fit: list[int], cal: list[int]) -> None:
        r = 1 / (1 + np.exp(-(strength * (2 * y - 1) + noise + 0.3 * rng.normal(size=y.size))))
        m = np.isin(week, cal)
        raw[mid], prob[mid] = r, r
        models[mid] = ModelInfo(
            mid, mid, as_of, threshold_at_fpr(y[m], r[m], 0.01), tuple(fit), tuple(cal)
        )
        nb[mid] = np.full(50, 1e-6)
        if mid == nb_hit:
            nb[mid][0] = 1.0

    raw: dict[str, Any] = {}
    prob: dict[str, Any] = {}
    nb: dict[str, Any] = {}
    models: dict[str, ModelInfo] = {}
    model(V1_ID, 1.5, 52, list(range(1, 48)), [48, 49, 50, 51])
    for t, s in STRENGTH.items():
        model(
            candidate_id("all_history", t), s, t, list(range(1, t - 3)), list(range(t - 3, t + 1))
        )
    return ScoreTable(
        np.array([f"f{i}" for i in range(week.size)]),
        week,
        y,
        label_at,
        raw,
        prob,
        nb,
        models,
        {w: _week_end(w) for w in range(1, 65)},
        None,
        {},
    )


@pytest.fixture(scope="module")
def table() -> ScoreTable:
    return synthetic_table()


@pytest.fixture(scope="module")
def results(table: ScoreTable) -> tuple[pd.DataFrame, pd.DataFrame]:
    return sim.run_experiment(table, EXP, POLICY)


def _rec(d: pd.DataFrame, rp: str, t: int, r: int = 0) -> pd.Series:
    s = d[(d["resample"] == r) & (d["release_policy"] == rp) & (d["as_of_week"] == t)]
    assert len(s) == 1
    return s.iloc[0]


# ------------------------------------------------------------------ config + statistics


def test_experiment_config_file_is_valid() -> None:
    cfg = load_experiment_config()
    assert cfg.file_type == "Win64" and cfg.bootstrap_resamples >= 20
    assert set(cfg.retrain_policies) == set(load_retrain_config().policies.model_dump())
    assert cfg.score_weeks.last == cfg.test_weeks.last == 64
    assert len(cfg.model_ids()) == len(cfg.retrain_weeks) * len(cfg.retrain_policies)
    bad = {**cfg.model_dump(), "retrain_weeks": [54, 52]}
    with pytest.raises(ValueError):
        ExperimentConfig.model_validate(bad)
    with pytest.raises(ValueError):  # drift windows would need weeks that were not scored
        ExperimentConfig.model_validate(
            {**cfg.model_dump(), "score_weeks": {"first": 50, "last": 64}}
        )


def test_psi_matches_a_hand_calculation() -> None:
    ref, cur = np.array([50, 30, 20]), np.array([30, 30, 40])
    p, q = ref / 100, cur / 100
    assert population_stability_index(ref, cur) == pytest.approx(
        float(np.sum((q - p) * np.log(q / p)))
    )
    assert population_stability_index(ref, ref) == 0.0
    assert population_stability_index([10, 0], [5, 5]) > 0  # empty bin floored, finite
    for a, b in (([1, 2], [1, 2, 3]), ([0, 0], [1, 1]), ([1], [1])):
        with pytest.raises(ValueError):
            population_stability_index(a, b)
    assert score_bin_counts([0.1, 0.5, 0.5, 0.9], [0.5]).tolist() == [1, 3]


def test_resampling_is_deterministic_and_stays_within_weeks(table: ScoreTable) -> None:
    assert np.array_equal(sim.resample_index(table.week, 0, 1), np.arange(table.week.size))
    a = sim.resample_index(table.week, 3, 11)
    assert np.array_equal(a, sim.resample_index(table.week, 3, 11))
    assert not np.array_equal(a, sim.resample_index(table.week, 4, 11))
    assert np.array_equal(np.bincount(table.week[a]), np.bincount(table.week))  # same week sizes


# ------------------------------------------------------------------ the policies


def test_p1_ships_the_bad_candidate_that_p2_and_the_gate_block(
    results: tuple[pd.DataFrame, pd.DataFrame],
) -> None:
    d, _ = results
    p1, p2, p3 = (_rec(d, rp, 52) for rp in sim.RELEASE_POLICIES)
    assert p1["promoted"] and p1["harmful"] and p1["deploy_week"] == 53
    assert not p2["promoted"] and p2["validation_auc_candidate"] < p2["validation_auc_production"]
    assert p3["decision_week"] == 58 and p3["deploy_week"] == 59  # 2 shadow weeks + 28 days
    assert p3["gate_decision"] == "REJECT" and not p3["promoted"]
    assert p3["harmful"]  # what it WOULD have done, judged on weeks 59-62
    assert p3["harm_weeks"] == "59,60,61,62"


def test_the_gate_promotes_a_clearly_better_candidate(
    results: tuple[pd.DataFrame, pd.DataFrame],
) -> None:
    d, _ = results
    p3 = _rec(d, "P3_gate", 54)
    assert (
        p3["gate_decision"] == "PROMOTE" and p3["promoted"] and p3["better"] and not p3["harmful"]
    )
    # decided after the last test week: never decided, never judged
    late = _rec(d, "P3_gate", 58)
    assert not late["decided"] and late["harmful"] is None
    p1_last = _rec(d, "P1_auto", 62)
    assert p1_last["harm_weeks"] == "63,64"  # capped at the last test week


def test_chains_replace_production_only_on_promotion(
    results: tuple[pd.DataFrame, pd.DataFrame],
) -> None:
    d, weekly = results
    p1 = d[(d["resample"] == 0) & (d["release_policy"] == "P1_auto")].sort_values("as_of_week")
    assert p1["production"].tolist() == [V1_ID, *p1["candidate"].tolist()[:-1]]
    w = weekly[(weekly["resample"] == 0) & (weekly["release_policy"] == "P3_gate")]
    assert w.set_index("week").loc[60, "model"] == V1_ID
    assert w.set_index("week").loc[61, "model"] == candidate_id("all_history", 54)


def test_gate_sees_only_shadow_weeks_and_labels_known_at_decision(
    table: ScoreTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[GateInput] = []
    real = sim.run_checks

    def spy(g: GateInput, policy: GatePolicy) -> Any:
        seen.append(g)
        return real(g, policy)

    monkeypatch.setattr(sim, "run_checks", spy)
    sim.run_experiment(table, EXP, POLICY, resamples=0)
    assert [set(np.unique(g.week).tolist()) for g in seen] == [{53, 54}, {55, 56}, {57, 58}]
    for g, t in zip(seen, (52, 54, 56), strict=True):
        cutoff = _week_end(t + 2) + 28 * DAY
        in_window = np.isin(table.week, [t + 1, t + 2])
        assert len(g.y) == int((in_window & (table.label_at <= cutoff)).sum())
        assert g.expected_events == g.paired_events == int(in_window.sum())


def test_never_break_hit_is_harmful_even_with_better_recall() -> None:
    st = synthetic_table(nb_hit=candidate_id("all_history", 54))
    d, _ = sim.run_experiment(st, EXP, POLICY, resamples=0)
    p1 = _rec(d, "P1_auto", 54)
    assert (
        p1["recall_gain"] > 0 and p1["never_break_hits"] == 1 and p1["harmful"] and not p1["better"]
    )
    assert _rec(d, "P3_gate", 54)["gate_decision"] == "REJECT"


def test_deterministic_for_a_seed(
    table: ScoreTable, results: tuple[pd.DataFrame, pd.DataFrame]
) -> None:
    again = sim.run_experiment(table, EXP, POLICY)
    pd.testing.assert_frame_equal(results[0], again[0])
    pd.testing.assert_frame_equal(results[1], again[1])


def test_no_test_week_data_in_any_threshold_or_calibration(table: ScoreTable) -> None:
    sim.check_no_test_week_leakage(table, EXP)
    v1 = table.models[V1_ID]
    leaky_v1 = {
        **table.models,
        V1_ID: ModelInfo(V1_ID, "v1", 52, v1.threshold, v1.fit_weeks, (50, 51, 52, 53)),
    }
    with pytest.raises(ValueError, match="v1 used test week"):
        sim.check_no_test_week_leakage(_with(table, leaky_v1), EXP)
    c = candidate_id("all_history", 56)
    m = table.models[c]
    leaky = {
        **table.models,
        c: ModelInfo(c, "all_history", 56, m.threshold, m.fit_weeks, (55, 56, 57)),
    }
    with pytest.raises(ValueError, match="after its retrain week"):
        sim.check_no_test_week_leakage(_with(table, leaky), EXP)


def _with(st: ScoreTable, models: dict[str, ModelInfo]) -> ScoreTable:
    return ScoreTable(
        st.file_ids,
        st.week,
        st.y,
        st.label_at,
        st.raw,
        st.prob,
        st.never_break,
        models,
        st.week_end,
        st.histograms,
        st.info,
    )


def test_drift_windows_and_reading(table: ScoreTable) -> None:
    assert sim.drift_windows(52, EXP) == ([47, 48, 49, 50], [51, 52])
    r = sim.drift_reading(table, V1_ID, 52, EXP)
    assert r.feature_psi == {} and r.features_alerting == 0  # no histograms in this table
    assert r.score_psi < EXP.drift.score_psi_alert and not r.alert  # stationary synthetic stream


def test_report_summary_and_markdown(
    results: tuple[pd.DataFrame, pd.DataFrame], tmp_path: Path
) -> None:
    from sentinel.experiment import report

    d, w = results
    rs = report.per_resample(d, w)
    assert set(rs["resample"]) == {0, 1, 2}
    table = report.summary_table(rs)
    row = table.set_index("release_policy")
    assert row.loc["P1_auto", "harmful_promotions"] >= 2  # T52 and T60 are clearly worse
    assert row.loc["P3_gate", "harmful_promotions"] == 0
    assert row.loc["P1_auto", "promotions"] == 6 and row.loc["P3_gate", "promotions"] >= 1
    drift = report.drift_precision(d)
    md = report.markdown(
        table, report.by_retrain_policy(d), drift, 2, {"harmful": "a.png", "recall": "b.png"}
    )
    assert md.startswith(report.AUTO_BEGIN) and "P3 Sentinel Gate" in md
    pngs = report.charts(table, w, tmp_path)
    assert all(p.stat().st_size > 1000 for p in pngs.values())
    path = tmp_path / "RESULTS.md"
    report.write_results_md(path, md)
    path.write_text(path.read_text("utf-8") + "\n## Conclusions\nhand-written\n", "utf-8")
    report.write_results_md(path, md.replace("P3 Sentinel Gate", "P3 Gate (v2)"))
    text = path.read_text("utf-8")
    assert "P3 Gate (v2)" in text and "hand-written" in text and text.count(report.AUTO_BEGIN) == 1


# ------------------------------------------------------------------ end to end (real pipeline)


def test_pipeline_end_to_end_on_synthetic_splits(
    tmp_path: Path,
    splits: Any,  # noqa: F811 - pytest fixture
    trained: Any,  # noqa: F811 - pytest fixture
) -> None:
    from sentinel.data.splits import feature_columns, load_manifest
    from sentinel.experiment.export import (
        HIST_FILE,
        export_scores,
        feature_histograms,
        import_export_zip,
        load_bundle,
        top_features,
        train_candidates,
        write_export_zip,
    )
    from sentinel.experiment.scores import load_score_table
    from tests.unit.test_retrain import _rcfg
    from tests.unit.test_train import FT, _cfg

    base = load_experiment_config().model_dump()
    exp = ExperimentConfig.model_validate(
        {
            **base,
            "retrain_weeks": [6],
            "retrain_policies": ["all_history", "last_n_weeks"],
            "score_weeks": {"first": 1, "last": 7},
            "test_weeks": {"first": 6, "last": 7},
            "bootstrap_resamples": 1,
            "drift": {
                **base["drift"],
                "recent_weeks": 1,
                "baseline_weeks": 2,
                "top_features": 3,
                "bins": 4,
            },
        }
    )
    # 120 benign files per synthetic week: an operating FPR they can measure
    small_policy = load_gate_policy().model_copy(update={"operating_fpr": 0.05})
    bundle_dir, _ = trained
    v1 = load_bundle(V1_ID, bundle_dir, as_of_week=5)
    rcfg = _rcfg(calibration_weeks=2)
    exports = []
    for part, pols in (("a", ["all_history"]), ("b", ["last_n_weeks"])):
        cands = train_candidates(
            FT,
            splits.splits_root,
            splits.never_break,
            tmp_path / "work",
            _cfg(),
            rcfg,
            exp,
            pols,
            threads=1,
        )
        out = tmp_path / f"export_{part}"
        export_scores(FT, splits.splits_root, splits.never_break, out, [v1, *cands], exp, 1)
        manifest = load_manifest(splits.splits_root, FT)
        feats = top_features(v1.booster, feature_columns(manifest), 3)
        hist = feature_histograms(splits.splits_root, FT, feats, v1.info["fit_weeks"], 4)
        assert set(hist["counts"][feats[0]]) == {str(w) for w in range(1, 8)}
        (out / HIST_FILE).write_text(json.dumps(hist), "utf-8")
        z = tmp_path / f"sentinel_stage7_{part}.zip"
        write_export_zip(out, z)
        exports.append(tmp_path / "imported" / part)
        import_export_zip(z, exports[-1])
    # merged: v1 scored identically by both runs; both candidates present; no features exported
    st = load_score_table(exports, load_manifest(splits.splits_root, FT))
    assert set(st.models) == {V1_ID, "all_history_T6", "last_n_weeks_T6"}
    assert st.models["last_n_weeks_T6"].calibration_weeks == (5, 6)
    assert not any(
        c.startswith("f_") for c in pd.read_parquet(exports[0] / "scores.parquet").columns
    )
    d, w = sim.run_experiment(st, exp, small_policy)
    assert len(d) == 2 * 3 * 2 and set(w["week"]) == {6, 7}
    assert d["candidate_recall"].notna().any()  # P1 promotions (T = 6) judged on week 7

    # tampering is refused on import
    bad = tmp_path / "bad.zip"
    with (
        zipfile.ZipFile(tmp_path / "sentinel_stage7_a.zip") as src,
        zipfile.ZipFile(bad, "w") as dst,
    ):
        for item in src.infolist():
            data = src.read(item.filename)
            dst.writestr(item.filename, data + b"x" if item.filename == "models.json" else data)
    with pytest.raises(ValueError, match="sha256"):
        import_export_zip(bad, tmp_path / "imported" / "bad")
    extra = tmp_path / "extra.zip"
    with zipfile.ZipFile(extra, "w") as dst:
        dst.writestr("../evil.py", "x")
    with pytest.raises(ValueError, match="unexpected files"):
        import_export_zip(extra, tmp_path / "imported" / "extra")
