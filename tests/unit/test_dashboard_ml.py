"""Stage 9 ML pages: transforms + every page rendered with AppTest on seeded demo data."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from sentinel.dashboard import ml_data as m
from sentinel.models.evaluate import auc, pr_points, roc_points
from tests.dashboard_support import FakeRegistry, gate_bundle, ml_frames, ml_results

PAGES = [
    "ml_overview",
    "model_registry",
    "gate_viewer",
    "head_to_head_page",
    "drift_page",
    "rollout_timeline",
    "experiment_results",
    "prediction_lookup",
]

PAGE_SCRIPT = """
from tests.dashboard_support import NOW, FakeRegistry, FakeResults, FakeSource
from sentinel.config import (load_alert_rules, load_gate_policy, load_monitoring_config,
                             load_rollout_config, load_serving_config)
from sentinel.dashboard import data as d
from sentinel.dashboard import ml_data as m
from sentinel.dashboard import theme
from sentinel.dashboard.ml_pages import MLContext, {page}
from sentinel.dashboard.ops_pages import Context
theme.register()
mon = load_monitoring_config()
ro = load_rollout_config()
ops = Context(FakeSource(empty={empty}), d.Window.last(60, NOW, 10), mon, load_alert_rules(),
              load_serving_config().service_name, ro.canaries.tenant_prefix, NOW)
ctx = MLContext(ops, FakeRegistry(empty={empty}, broken={broken}), FakeResults(empty={empty}),
                mon.dashboard.ml, m.Filters(), dict(ro.ring_aliases), ro.fallback_alias,
                load_gate_policy().operating_fpr)
{page}(ctx)
"""


def _run(page: str, empty: bool = False, broken: bool = False) -> AppTest:
    at = AppTest.from_string(PAGE_SCRIPT.format(page=page, empty=empty, broken=broken))
    at.run(timeout=90)
    assert not at.exception, [e.value for e in at.exception]
    return at


def _text(at: AppTest) -> str:
    parts = [x.value for x in at.markdown] + [c.value for c in at.caption]
    parts += [h.value for h in at.header] + [s.value for s in at.subheader]
    parts += [t.value for t in at.title]
    return "\n".join(str(p) for p in parts)


# ------------------------------------------------------------------ statistics


def test_roc_points_reach_the_zoom_edge_and_agree_with_auc() -> None:
    rng = np.random.default_rng(1)
    y = np.repeat([0, 1], 5000)
    s = rng.normal(size=y.size) + 2 * y
    full = roc_points(y, s, 1.0, max_points=100_000)
    assert full.x[0] == 0 and full.y[0] == 0 and full.x[-1] == 1 and full.y[-1] == 1
    area = float(np.sum(np.diff(full.x) * full.y[1:]))  # step curve: exact without ties
    assert area == pytest.approx(auc(y, s), abs=1e-9)
    zoom = roc_points(y, s, 0.01)
    assert zoom.x[-2] < 0.01 <= zoom.x[-1] and len(zoom.x) <= 401
    assert np.all(np.diff(zoom.x) >= 0) and np.all(np.diff(zoom.y) >= 0)


def test_pr_points_precision_and_recall() -> None:
    y = np.array([1, 1, 0, 1, 0, 0])
    s = np.array([0.9, 0.8, 0.7, 0.6, 0.5, 0.4])
    c = pr_points(y, s)
    assert list(c.x) == pytest.approx([1 / 3, 2 / 3, 2 / 3, 1, 1, 1])
    assert list(c.y) == pytest.approx([1, 1, 2 / 3, 3 / 4, 3 / 5, 1 / 2])


# ------------------------------------------------------------------ transforms


def test_gate_bundle_hash_check_detects_tampering() -> None:
    f = ml_frames()["gate_bundle"].iloc[0]
    assert m.verify_bundle(f["bundle_sha256"], f["bundle_json"])
    assert not m.verify_bundle(f["bundle_sha256"], f["bundle_json"].replace("REJECT", "PROMOTE"))


def test_gate_bundle_transforms() -> None:
    b = gate_bundle()
    checks = m.gate_checks(b)
    assert list(checks["check"])[:2] == ["recall_at_fpr", "false_alarms"]
    assert not checks.set_index("check").loc["calibration", "hard"]
    bars = m.interval_bars(b)
    assert len(bars) == 4 and (bars["low"] <= bars["value"]).all()
    assert (bars["value"] <= bars["high"]).all()
    t = m.mcnemar_tables(b)
    assert t["malicious"].loc["production right", "candidate wrong"] == 400
    assert t["malicious"].loc["production wrong", "candidate right"] == 196
    assert t["benign"].attrs["mcnemar_p"] == 0.013
    assert len(m.flipped_files(b, 3)) == 3
    assert set(m.weekly_regression(b)["model"]) == {"production", "candidate"}


def test_ring_serving_follows_aliases_with_production_fallback() -> None:
    v = FakeRegistry().versions()
    rs = m.ring_serving(
        v, {"ring0": "ring0_model", "ring1": "ring1_model", "ring2": "ring2_model"}, "production"
    ).set_index(["model", "ring"])
    assert rs.loc[("sentinel-win64", "ring0"), "version"] == "sentinel-win64/4"
    assert rs.loc[("sentinel-win64", "ring1"), "version"] == "sentinel-win64/1"
    assert rs.loc[("sentinel-win32", "ring0"), "via"] == "production (fallback)"


def test_registry_table_joins_gate_outcomes_and_links_mlflow() -> None:
    t = m.registry_table(FakeRegistry().versions(), ml_frames()["gates"], "http://x:5000/")
    by = t.set_index("label")
    assert by.loc["sentinel-win64/2", "gate_outcome"] == "REJECT"
    assert by.loc["sentinel-win64/1", "gate_outcome"] == "not gated"
    assert (
        by.loc["sentinel-win64/2", "mlflow"] == "http://x:5000/#/models/sentinel-win64/versions/2"
    )


def test_head_to_head_uses_only_weeks_after_both_models() -> None:
    r = ml_results()
    scores, models = r["tables"]["ml_scores"], r["documents"]["ml_models"]["models"]
    h = m.head_to_head(scores, models, "v1", "all_history_T54", 0.01, 0.05, 10)
    assert h.weeks == list(range(55, 65))
    assert set(h.summary["model"]) == {"v1", "all_history_T54"}
    s = h.summary.set_index("model")
    assert s.loc["v1", "recall_at_fpr"] > s.loc["all_history_T54", "recall_at_fpr"]
    assert h.roc["fpr"].max() >= 0.05 and set(h.weekly["week"]) == set(h.weeks)
    same = m.head_to_head(scores, models, "v1", "v1", 0.01, 0.05, 10)
    assert len(same.summary) == 1


def test_score_distribution_shares_sum_to_one_per_week() -> None:
    scores = ml_results()["tables"]["ml_scores"]
    dist = m.score_distribution(scores, "v1", 12)
    assert dist.groupby("week")["share"].sum().round(9).eq(1).all()
    assert dist["week"].nunique() == 20


def test_rollout_summary_and_state_spans() -> None:
    tr = ml_frames()["transitions"]
    s = m.rollout_summary(tr).set_index("rollout_id")
    assert s.loc["r-1", "rolled_back"] and s.loc["r-2", "final_state"] == "COMPLETE"
    spans = m.state_spans(tr, "r-1", pd.Timestamp("2030-01-01", tz="UTC"))
    assert list(spans["state"]) == ["APPROVED", "RING0", "ROLLED_BACK"]
    assert spans["end"].iloc[-1] == spans["start"].iloc[-1]  # terminal state: no open bar


def test_filters_build_null_params_and_options() -> None:
    assert m.Filters().params() == {"model": None, "ring": None, "tenant": None}
    assert m.Filters(ring="ring0").describe() == "ring = ring0"
    opts = m.filter_options(ml_frames()["filter_values"])
    assert opts["ring"] == ["ring0", "ring1"] and "tenant-0002" in opts["tenant"]
    assert "%(model)s::text IS NULL" in m.FLAG_HOURLY_SQL


def test_experiment_transforms() -> None:
    t = ml_results()["tables"]
    iv = m.experiment_intervals(t["experiment_summary"])
    assert set(iv["measure"]) == {"harmful promotions", "good updates blocked", "promotions"}
    assert (iv["low"] <= iv["value"]).all() and (iv["value"] <= iv["high"]).all()
    assert len(m.recall_over_time(t["experiment_weekly"])) == 36
    dp = m.drift_precision(t["experiment"])
    assert dp is not None and (dp.alerts, dp.alerts_on_harmful, dp.harmful) == (1, 1, 2)
    assert m.drift_alert_weeks(t["backtest_pass2_drift"]) == [57]


def test_results_dir_reads_parquet_csv_and_json(tmp_path: object) -> None:
    from pathlib import Path

    p = Path(str(tmp_path))
    pd.DataFrame({"a": [1]}).to_parquet(p / "x.parquet")
    pd.DataFrame({"b": [2]}).to_csv(p / "y.csv", index=False)
    (p / "z.json").write_text(json.dumps({"k": 1}))
    r = m.ResultsDir(p)
    assert r.table("x")["a"].tolist() == [1] and r.table("y")["b"].tolist() == [2]
    assert r.document("z") == {"k": 1} and r.table("nope") is None and r.document("no") is None


# ------------------------------------------------------------------ pages (AppTest)


def test_ml_overview_shows_status_cards_and_ring_models() -> None:
    at = _run("ml_overview")
    text = _text(at)
    assert "Production model per ring" in text and "Flag rate per ring" in text
    assert any("REJECT" in str(x.value) for x in at.markdown)


def test_model_registry_lists_versions_with_gate_outcome() -> None:
    at = _run("model_registry")
    df = at.dataframe[0].value
    assert {"policy", "fit_weeks", "fingerprint", "gate_outcome", "mlflow"} <= set(df.columns)
    assert "REJECT" in set(df["gate_outcome"])


def test_gate_viewer_checks_mcnemar_flips_and_verify_button() -> None:
    at = _run("gate_viewer")
    text = _text(at)
    assert "McNemar 2x2 tables" in text and "Files the models disagree on" in text
    assert "recall_at_fpr" in text
    at.button[0].click().run(timeout=60)
    assert not at.exception
    assert any("matches the stored hash" in str(s.value) for s in at.success)


def test_head_to_head_page_curves_and_model_choice() -> None:
    at = _run("head_to_head_page")
    assert "Calibration" in _text(at) and len(at.metric) == 2
    at.selectbox[1].set_value("all_history_T54").run(timeout=60)
    assert not at.exception


def test_drift_page_heatmap_and_score_distribution() -> None:
    at = _run("drift_page")
    text = _text(at)
    assert "Score distribution by week" in text and "57" in text


def test_rollout_timeline_page_spans_rings_and_rollbacks() -> None:
    at = _run("rollout_timeline")
    text = _text(at)
    assert "Flag rate per ring during the rollout" in text and "Rollbacks" in text
    assert at.metric[2].value == "1"


def test_experiment_results_page_has_both_stages() -> None:
    at = _run("experiment_results")
    assert len(at.tabs) == 2 and "Experiment results" in _text(at)


def test_prediction_lookup_shows_lineage() -> None:
    at = _run("prediction_lookup")
    at.text_input[0].input("abc").run(timeout=60)
    assert not at.exception
    text = _text(at)
    assert "Live verdicts (1)" in text and "Shadow scores (1)" in text


@pytest.mark.parametrize("page", PAGES)
def test_ml_pages_render_on_an_empty_database(page: str) -> None:
    _run(page, empty=True)


def test_pages_survive_a_registry_outage() -> None:
    at = _run("model_registry", broken=True)
    assert any("registry unavailable" in str(w.value) for w in at.warning)


def test_app_has_an_ml_section_with_eight_pages_and_shared_filters() -> None:
    at = AppTest.from_string(
        "from tests.dashboard_support import NOW, FakeRegistry, FakeResults, FakeSource\n"
        "from sentinel.dashboard.app import main\n"
        "main(FakeSource(), now=NOW, registry=FakeRegistry(), results=FakeResults())\n"
    )
    at.run(timeout=90)
    assert not at.exception, [e.value for e in at.exception]
    labels = [s.label for s in at.sidebar.selectbox]
    assert labels == ["Time window", "Model version", "Ring", "Tenant"]
    assert at.sidebar.selectbox[2].options == ["all", "ring0", "ring1"]
    at.sidebar.selectbox[2].set_value("ring0").run(timeout=60)
    assert not at.exception


def test_stage_grid_spans_fill_every_row() -> None:
    from sentinel.dashboard.ui import grid_spans

    assert grid_spans(5, 3) == [4, 4, 4, 6, 6]  # no empty slot in the last row
    assert grid_spans(3, 3) == [4, 4, 4]
    assert grid_spans(4, 3) == [4, 4, 4, 12]
    assert grid_spans(2, 5) == [6, 6]
    assert grid_spans(0, 3) == []
