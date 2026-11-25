"""Stage 7b Pass 2: P4, walk-forward, blend, drift recalibration (synthetic data)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from sentinel.backtest import pass1 as p1
from sentinel.backtest import pass2 as p2
from sentinel.config import candidate_id, load_backtest_config, load_experiment_config
from sentinel.experiment.export import V1_ID
from tests.unit.test_backtest import CFG, GATE, synthetic

DRIFT = load_experiment_config().drift


@pytest.fixture(scope="module")
def setup() -> tuple[p1.Ctx, p2.Slices, p2.Params]:
    st, meta = synthetic()
    ctx = p1.Ctx(st, CFG, GATE, p1.cluster_keys(st, meta), "family")
    return ctx, p2.build_slices(st, meta), p2.preregistered(CFG, 0.5)


def test_pass2_config_is_fixed_before_running() -> None:
    c = load_backtest_config()
    assert c.p4.signer_slices == ["unsigned", "self_signed", "signed"]
    assert (c.p4.must_catch_files, c.p4.must_catch_missed_allowed) == (500, 0)
    assert c.blend.weight_candidate == 0.5
    assert c.drift_recalibration.on_alert_max_fpr_move_pp == float("inf")
    assert len(p2.grid(c)) == 4 * 3 * 3 * 3


def test_evidence_never_uses_either_models_training_weeks(setup: Any) -> None:
    ctx, sl, pre = setup
    c52 = candidate_id("all_history", 52)
    assert p2.evidence_weeks(ctx, c52, V1_ID, 52, 4) == []  # weeks 43-46: inside v1's fit
    res = p2.day0(ctx, sl, c52, V1_ID, 52, pre, "clean")
    assert not res["promote"] and res["blocked_by"] == "evidence"
    c56 = candidate_id("all_history", 56)
    assert p2.evidence_weeks(ctx, c56, V1_ID, 56, 4) == [48, 49, 50]


def test_an_identical_model_passes_every_check_with_both_matching_methods(setup: Any) -> None:
    ctx, sl, pre = setup
    c = candidate_id("all_history", 54)
    for method in ("clean", "traffic"):
        res = p2.day0(ctx, sl, c, c, 56, pre, method)
        assert res["promote"], res["checks"]
        assert res["checks"]["must_catch"][1].startswith("0 of")
        b, c_ = res["disagree_b"], res["disagree_c"]
        if method == "clean":  # the same cut-off on the same data: no disagreement at all
            assert (b, c_) == (0, 0)
        else:  # traffic match equalises week-T flag counts; its cut-off may sit one score lower
            assert b + c_ <= 2
        assert res["clean_warning"]  # far fewer than 50,000 clean files


def test_clean_pool_match_holds_the_target_and_traffic_match_equalises_flag_rate(
    setup: Any,
) -> None:
    ctx, sl, pre = setup
    st = ctx.st
    c, t = candidate_id("all_history", 58), 58
    res = p2.day0(ctx, sl, c, V1_ID, t, pre, "clean")
    benign = ctx.rows(res["evidence_weeks"])
    benign = benign[st.y[benign] == 0]
    for m, k in ((c, "cutoff_candidate"), (V1_ID, "cutoff_production")):
        assert (st.raw[m][benign] > res[k]).mean() <= CFG.operating_fpr
    tr = p2.day0(ctx, sl, c, V1_ID, t, pre, "traffic")
    week = ctx.rows([t])
    fr_c = (st.raw[c][week] > tr["cutoff_candidate"]).mean()
    fr_p = (st.raw[V1_ID][week] > tr["cutoff_production"]).mean()
    assert fr_c <= fr_p and fr_p - fr_c < 2 / week.size
    assert tr["checks"]["flag_rate"][0] == "pass"


def test_a_weak_candidate_fails_non_inferiority(setup: Any) -> None:
    ctx, sl, pre = setup
    res = p2.day0(ctx, sl, candidate_id("all_history", 56), V1_ID, 56, pre, "clean")
    assert not res["promote"] and "non_inferiority" in res["blocked_by"]


def test_directional_check_needs_non_overlapping_wilson_bounds() -> None:
    assert p2._worse_rate(40, 5, 1000, 0.95)[0]
    assert not p2._worse_rate(8, 5, 1000, 0.95)[0]  # more, but within noise
    assert not p2._worse_rate(5, 40, 1000, 0.95)[0]


def test_stage3_rolls_back_a_harmful_promotion(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, sl, pre = setup

    def always(ctx_: Any, sl_: Any, cand: str, prod: str, t: int, p: Any, m: str) -> Any:
        return {
            "promote": True,
            "checks": {},
            "blocked_by": "",
            "clean_files": 0,
            "evidence_weeks": [],
            "cutoff_candidate": ctx_.st.models[cand].threshold,
        }

    monkeypatch.setattr(p2, "day0", always)
    _dec, weekly, ev = p2.run_p4_chain(
        ctx, sl, "all_history", "P4_test", lambda t: pre, "clean", 0.05, p2.SeqCache(), 0.5
    )
    rb = [e for e in ev if e["event"] == "rollback"]
    # T56 (weak) replaces T54 (strong) at week 57; final labels of 57 exist at week 63
    assert any(e["candidate"] == candidate_id("all_history", 56) for e in rb)
    served = {r["week"]: r["model"] for r in weekly}
    assert served[57] == candidate_id("all_history", 56)
    assert any(e["event"] in ("stage2", "stage2_skipped") for e in ev)
    for e in rb:  # after a rollback the replaced model serves the next week
        nxt = served.get(e["week"] + 1)
        assert nxt is None or nxt != e["candidate"]


def test_stage2_runs_only_when_early_labels_agree_enough(setup: Any) -> None:
    ctx, _, _ = setup
    assert p2.early_agreement(ctx, 60, 0.05) >= CFG.p4.min_early_agreement
    assert p2.early_agreement(ctx, 60, 0.20) < CFG.p4.min_early_agreement


def test_walk_forward_tunes_only_on_outcomes_labelled_in_time() -> None:
    pairs = p2.predecessor_pairs(CFG)
    assert [pr for pr in pairs if p2.known_by(CFG, 60)(pr)] == []
    usable = {pr[2] for pr in pairs if p2.known_by(CFG, 62)(pr)}
    assert usable == {52}  # 52 + 4 weeks <= 62 - 6


def test_blend_averages_scores_and_sets_its_cut_off_point_in_time(setup: Any) -> None:
    ctx, _, _ = setup
    st, ids = p2._blend_table(ctx.st, CFG, "all_history")
    c52 = candidate_id("all_history", 52)
    assert np.allclose(st.raw[ids[0]], 0.5 * st.raw[V1_ID] + 0.5 * st.raw[c52])
    bctx = p1.Ctx(st, CFG, GATE, ctx.clusters, "family")
    dec, weekly = p2.run_blend_chain(bctx, "all_history", ids, 0.5)
    assert "traffic match" in dec[0]["detail"]  # T52: every labelled week is a fit week
    assert all(d["promoted"] for d in dec[:-1])
    assert {r["model"] for r in weekly} <= {V1_ID, *ids}


def test_drift_recalibration_never_moves_the_cut_off_under_rule_none(setup: Any) -> None:
    ctx, _, _ = setup
    dr = p2.drift_recalibration(ctx, DRIFT)
    none = dr[(dr["schedule"] == "v1 kept live") & (dr["rule"] == "none")]
    assert len(none) == len(CFG.test_weeks.weeks)
    assert set(dr["rule"]) == {
        "none",
        "weekly capped",
        "drift jump only",
        "weekly capped + drift jump",
    }


def test_slices_map_signature_status() -> None:
    st, meta = synthetic()
    meta.loc[:2, "num_certs"] = 2.0
    meta.loc[1, "self_signed"] = 1.0
    sl = p2.build_slices(st, meta)
    assert list(sl.signature[:4]) == ["signed", "self_signed", "signed", "unsigned"]
    assert sl.first_week  # families have a first week


def test_run_pass2_end_to_end_and_report(tmp_path: Any) -> None:
    from sentinel.backtest.report import P2_BEGIN, write_pass2_md

    st, meta = synthetic()
    cfg = CFG.model_copy(update={"harm": CFG.harm.model_copy(update={"bootstrap_resamples": 10})})
    res = p2.run_pass2(st, meta, cfg, GATE, 0.5, DRIFT, [(candidate_id("all_history", 54), V1_ID)])
    d = res["decisions"]
    assert {"P4_clean", "P4_traffic", "P4_walk_forward", "P4_IN_SAMPLE", "P5_blend"} <= set(
        d["release_policy"]
    )
    assert len(res["rejected_redo"]) == 1 and len(res["mcnemar"]) == 6
    table = p1.summary_table(res["mean_recall_ci"], d, res["weekly"]).assign(rollbacks=0)
    diffs = pd.DataFrame(
        [{"comparison": "x", "metric": "recall", "diff": 0.1, "ci_low": -0.2, "ci_high": 0.3}]
    )
    md = tmp_path / "BACKTEST.md"
    md.write_text("# x\n\nPASS 1 TEXT\n", "utf-8")
    write_pass2_md(md, res, table, diffs)
    write_pass2_md(md, res, table, diffs)
    text = md.read_text("utf-8")
    assert "PASS 1 TEXT" in text and text.count(P2_BEGIN) == 1
    assert "IN-SAMPLE" in text and "within noise" in text
    a = res["weekly"][res["weekly"]["release_policy"] == "P4_clean"]
    point, lo, hi = p2.policy_difference(res["ctx"], a, a, "recall_at_fpr")
    assert point == 0 and lo == 0 and hi == 0
