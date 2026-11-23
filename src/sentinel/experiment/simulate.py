"""Stage 7: replay the release decisions offline, per release policy and retraining policy.

Every chain starts with v1 in production at the first test week. At each retrain week T a
candidate (one retraining policy) is decided by the release policy:

- P1_auto      promote every retrain; decided at T.
- P2_val_auc   promote if the candidate's AUC beats production's on the candidate's own
               calibration weeks (<= T, labels known by T); decided at T.
- P3_gate      Sentinel Gate: `gate.checks.run_checks` + `decide` (policy unchanged) on the
               `shadow_weeks` after T against the production of that moment, decided once
               `label_wait_days` have passed after the shadow window, with only labels known
               by then.

A promoted model serves from the week after its decision. It is judged on the `harm_weeks`
after that (capped at the last test week): HARMFUL if its recall at the operating FPR is below
the model it replaced by more than `harm_margin`, or it flags a never-break file at its shipped
threshold; BETTER if its recall is higher and it flags none. Candidates that were NOT promoted
are judged the same way against the production they would have replaced (a "good update
blocked" is a non-promoted BETTER candidate).

Repeats: resample 0 is the real stream; resample r >= 1 draws the files of every week with
replacement (one draw shared by all models, so comparisons stay paired). All statistics come
from sentinel.models.evaluate and the gate's own checks: nothing is re-implemented here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

from sentinel.config import ExperimentConfig, GatePolicy, candidate_id
from sentinel.experiment.export import V1_ID
from sentinel.experiment.scores import ScoreTable
from sentinel.gate.checks import GateInput, ModelScores, decide, run_checks
from sentinel.models.evaluate import (
    auc,
    population_stability_index,
    rates_at_threshold,
    recall_at_fpr,
    score_bin_counts,
)

ReleasePolicy = Literal["P1_auto", "P2_val_auc", "P3_gate"]
RELEASE_POLICIES: tuple[ReleasePolicy, ...] = ("P1_auto", "P2_val_auc", "P3_gate")
DAY = 86_400


# ---------------------------------------------------------------------------- resampling


def resample_index(week: npt.NDArray[np.int64], r: int, seed: int) -> npt.NDArray[np.int64]:
    """Row indices of resample r: 0 = every row once; else, within each week, as many rows
    as the week has, drawn with replacement (seeded by (seed, r): deterministic)."""
    if r == 0:
        return np.arange(week.size, dtype=np.int64)
    rng = np.random.default_rng([seed, r])
    parts = []
    for w in np.unique(week):
        rows = np.flatnonzero(week == w)
        parts.append(np.sort(rng.choice(rows, size=rows.size, replace=True)))
    return np.concatenate(parts).astype(np.int64)


class View:
    """The score table seen through one resample's row indices."""

    def __init__(self, st: ScoreTable, idx: npt.NDArray[np.int64]) -> None:
        self.st, self.idx = st, idx
        self.week: npt.NDArray[np.int64] = st.week[idx]
        self.y: npt.NDArray[np.int8] = st.y[idx]
        self.label_at: npt.NDArray[np.int64] = st.label_at[idx]

    def rows(self, weeks: Iterable[int]) -> npt.NDArray[np.bool_]:
        return np.isin(self.week, np.fromiter(weeks, dtype=np.int64))

    def raw(self, model: str) -> npt.NDArray[np.float64]:
        return self.st.raw[model][self.idx]

    def prob(self, model: str) -> npt.NDArray[np.float64]:
        return self.st.prob[model][self.idx]

    def file_ids(self, mask: npt.NDArray[np.bool_]) -> list[str]:
        return [str(v) for v in self.st.file_ids[self.idx[mask]]]


# ---------------------------------------------------------------------------- timing


def gate_decision_week(
    st: ScoreTable, as_of_week: int, exp: ExperimentConfig, policy: GatePolicy
) -> int | None:
    """The week in which P3 decides: labels for the shadow weeks are waited for
    `label_wait_days` after the shadow window ends. None if past the data."""
    cutoff = gate_label_cutoff(st, as_of_week, exp, policy)
    later = [w for w, end in sorted(st.week_end.items()) if end >= cutoff]
    return later[0] if later else None


def gate_label_cutoff(
    st: ScoreTable, as_of_week: int, exp: ExperimentConfig, policy: GatePolicy
) -> int:
    last_shadow = as_of_week + exp.shadow_weeks
    if last_shadow not in st.week_end:
        return int(np.iinfo(np.int64).max)
    return st.week_end[last_shadow] + int(policy.label_wait_days * DAY)


def decision_week(
    st: ScoreTable, rp: ReleasePolicy, as_of_week: int, exp: ExperimentConfig, policy: GatePolicy
) -> int | None:
    return gate_decision_week(st, as_of_week, exp, policy) if rp == "P3_gate" else as_of_week


# ---------------------------------------------------------------------------- decisions


def _decide_val_auc(
    v: View, cand: str, prod: str, as_of_week: int
) -> tuple[bool, str, dict[str, Any]]:
    info = v.st.models[cand]
    mask = v.rows(info.calibration_weeks) & (v.label_at <= v.st.week_end[as_of_week])
    y = v.y[mask]
    if y.min(initial=1) == y.max(initial=0):
        return False, "validation weeks hold one class only", {}
    a_c, a_p = auc(y, v.raw(cand)[mask]), auc(y, v.raw(prod)[mask])
    nums = {"candidate_auc": a_c, "production_auc": a_p, "validation_rows": int(mask.sum())}
    better = a_c > a_p
    return better, f"validation AUC {a_c:.5f} vs {a_p:.5f}", nums


def _decide_gate(
    v: View, cand: str, prod: str, as_of_week: int, exp: ExperimentConfig, policy: GatePolicy
) -> tuple[bool, str, dict[str, Any]]:
    st = v.st
    shadow = list(range(as_of_week + 1, as_of_week + exp.shadow_weeks + 1))
    newest_model = max(st.models[cand].as_of_week, st.models[prod].as_of_week)
    if min(shadow) <= newest_model:  # pragma: no cover - guaranteed by the timeline
        raise AssertionError("the gate would judge a model on weeks it learned from")
    in_window = v.rows(shadow)
    cutoff = gate_label_cutoff(st, as_of_week, exp, policy)
    labelled = in_window & (v.label_at <= cutoff)

    def scores(m: str) -> ModelScores:
        return ModelScores(
            label=m,
            threshold=st.models[m].threshold,
            scores=v.raw(m)[labelled],
            probabilities=v.prob(m)[labelled],
            never_break_scores=st.never_break[m],
        )

    g = GateInput(
        file_ids=v.file_ids(labelled),
        y=v.y[labelled],
        week=v.week[labelled],
        production=scores(prod),
        candidate=scores(cand),
        expected_events=int(in_window.sum()),
        paired_events=int(in_window.sum()),  # offline: every event has both scores
        unlabelled_events=int((in_window & ~labelled).sum()),
    )
    decision, reason = decide(run_checks(g, policy))
    return decision == "PROMOTE", f"{decision}: {reason}", {"gate_decision": decision}


# ---------------------------------------------------------------------------- judging


def _recall(v: View, mask: npt.NDArray[np.bool_], model: str, policy: GatePolicy) -> float | None:
    y = v.y[mask]
    n_benign = int((y == 0).sum())
    if (y == 1).sum() == 0 or n_benign * policy.operating_fpr < policy.min_false_alarms_pooled:
        return None
    return recall_at_fpr(y, v.raw(model)[mask], policy.operating_fpr, policy.confidence).recall


@dataclass(frozen=True)
class Judgement:
    harm_weeks: tuple[int, ...]
    candidate_recall: float | None
    production_recall: float | None
    recall_gain: float | None
    never_break_hits: int
    harmful: bool | None  # None: not enough following weeks to judge
    better: bool | None


def judge(
    v: View, cand: str, prod: str, deploy_week: int, exp: ExperimentConfig, policy: GatePolicy
) -> Judgement:
    last = exp.test_weeks.last
    weeks = tuple(range(deploy_week, min(deploy_week + exp.harm_weeks - 1, last) + 1))
    hits = v.st.flags_never_break(cand)
    if len(weeks) < exp.min_harm_weeks:
        return Judgement(weeks, None, None, None, hits, None, None)
    if min(weeks) <= max(v.st.models[cand].as_of_week, v.st.models[prod].as_of_week):
        raise AssertionError("harm judged on weeks a model learned from")  # pragma: no cover
    mask = v.rows(weeks)
    rc, rp = _recall(v, mask, cand, policy), _recall(v, mask, prod, policy)
    if rc is None or rp is None:
        return Judgement(weeks, rc, rp, None, hits, None, None)
    gain = rc - rp
    return Judgement(
        weeks,
        rc,
        rp,
        gain,
        hits,
        harmful=gain < -exp.harm_margin or hits > 0,
        better=gain > exp.harm_margin and hits == 0,
    )


# ---------------------------------------------------------------------------- drift


@dataclass(frozen=True)
class DriftReading:
    feature_psi: dict[str, float]
    features_alerting: int
    score_psi: float
    alert: bool


def drift_windows(as_of_week: int, exp: ExperimentConfig) -> tuple[list[int], list[int]]:
    """(baseline weeks, recent weeks): the recent window ends at T, the baseline just before."""
    d = exp.drift
    recent = list(range(as_of_week - d.recent_weeks + 1, as_of_week + 1))
    base = list(range(recent[0] - d.baseline_weeks, recent[0]))
    return base, recent


def drift_reading(
    st: ScoreTable, production: str, as_of_week: int, exp: ExperimentConfig
) -> DriftReading:
    """PSI of v1's top features (weekly histograms, fixed bins) and of production's raw scores
    (bins = deciles of the baseline window), recent window vs baseline window. Uses only
    weeks <= T, and no labels."""
    d = exp.drift
    base, recent = drift_windows(as_of_week, exp)
    fpsi: dict[str, float] = {}
    if st.histograms is not None:
        counts = st.histograms["counts"]
        for f in st.histograms["features"]:
            b = np.sum([counts[f][str(w)] for w in base], axis=0)
            r = np.sum([counts[f][str(w)] for w in recent], axis=0)
            fpsi[f] = population_stability_index(b, r)
    n_alert = sum(p > d.feature_psi_alert for p in fpsi.values())
    raw = st.raw[production]
    b_scores = raw[np.isin(st.week, base)]
    r_scores = raw[np.isin(st.week, recent)]
    edges = sorted(set(np.quantile(b_scores, np.linspace(0, 1, d.bins + 1)[1:-1]).tolist()))
    spsi = population_stability_index(
        score_bin_counts(b_scores, edges), score_bin_counts(r_scores, edges)
    )
    alert = n_alert >= d.min_features_alerting or spsi > d.score_psi_alert
    return DriftReading(fpsi, n_alert, spsi, alert)


# ---------------------------------------------------------------------------- the run


@dataclass(frozen=True)
class DecisionRecord:
    resample: int
    release_policy: str
    retrain_policy: str
    as_of_week: int
    candidate: str
    production: str
    decision_week: int | None
    deploy_week: int | None
    decided: bool
    promoted: bool
    detail: str
    validation_auc_candidate: float | None
    validation_auc_production: float | None
    gate_decision: str | None
    harm_weeks: str
    candidate_recall: float | None
    production_recall: float | None
    recall_gain: float | None
    never_break_hits: int
    harmful: bool | None
    better: bool | None
    drift_alert: bool
    drift_features_alerting: int
    drift_score_psi: float


def simulate_chain(
    v: View,
    resample: int,
    rp: ReleasePolicy,
    retrain_policy: str,
    exp: ExperimentConfig,
    policy: GatePolicy,
    drift: dict[tuple[str, int], DriftReading],
) -> tuple[list[DecisionRecord], list[tuple[int, str]]]:
    """One release policy over one retraining policy's candidates. Returns the decisions and
    the live history [(first week served, model), ...]."""
    st = v.st
    prod = V1_ID
    live: list[tuple[int, str]] = [(exp.test_weeks.first, V1_ID)]
    timeline = sorted(
        ((decision_week(st, rp, t, exp, policy), t) for t in exp.retrain_weeks),
        key=lambda e: (e[0] if e[0] is not None else 10**9, e[1]),
    )
    out: list[DecisionRecord] = []
    for dw, t in timeline:
        cand = candidate_id(retrain_policy, t)
        key = (prod, t)
        if key not in drift:
            drift[key] = drift_reading(st, prod, t, exp)
        dr = drift[key]
        base: dict[str, Any] = {
            "resample": resample,
            "release_policy": rp,
            "retrain_policy": retrain_policy,
            "as_of_week": t,
            "candidate": cand,
            "production": prod,
            "decision_week": dw,
            "drift_alert": dr.alert,
            "drift_features_alerting": dr.features_alerting,
            "drift_score_psi": dr.score_psi,
            "validation_auc_candidate": None,
            "validation_auc_production": None,
            "gate_decision": None,
        }
        if dw is None or dw >= exp.test_weeks.last:
            out.append(
                DecisionRecord(
                    **base,
                    deploy_week=None,
                    decided=False,
                    promoted=False,
                    detail="no decision before the last test week",
                    harm_weeks="",
                    candidate_recall=None,
                    production_recall=None,
                    recall_gain=None,
                    never_break_hits=st.flags_never_break(cand),
                    harmful=None,
                    better=None,
                )
            )
            continue
        nums: dict[str, Any]
        if rp == "P1_auto":
            promoted, detail, nums = True, "auto-promote", {}
        elif rp == "P2_val_auc":
            promoted, detail, nums = _decide_val_auc(v, cand, prod, t)
            base["validation_auc_candidate"] = nums.get("candidate_auc")
            base["validation_auc_production"] = nums.get("production_auc")
        else:
            promoted, detail, nums = _decide_gate(v, cand, prod, t, exp, policy)
            base["gate_decision"] = nums["gate_decision"]
        deploy = dw + 1
        j = judge(v, cand, prod, deploy, exp, policy)
        out.append(
            DecisionRecord(
                **base,
                deploy_week=deploy,
                decided=True,
                promoted=promoted,
                detail=detail[:300],
                harm_weeks=",".join(str(w) for w in j.harm_weeks),
                candidate_recall=j.candidate_recall,
                production_recall=j.production_recall,
                recall_gain=j.recall_gain,
                never_break_hits=j.never_break_hits,
                harmful=j.harmful,
                better=j.better,
            )
        )
        if promoted:
            prod = cand
            live.append((deploy, cand))
    return out, live


def weekly_series(
    v: View, live: list[tuple[int, str]], exp: ExperimentConfig, policy: GatePolicy
) -> list[dict[str, Any]]:
    """Per test week: the model live, its recall at the operating FPR (threshold set on that
    week: a measurement, never used for any decision) and its FPR at its shipped threshold."""
    rows = []
    for w in exp.test_weeks.weeks:
        model = [m for start, m in live if start <= w][-1]
        mask = v.week == w
        y, raw = v.y[mask], v.raw(model)[mask]
        rec = recall_at_fpr(y, raw, policy.operating_fpr, policy.confidence)
        shipped = rates_at_threshold(y, raw, v.st.models[model].threshold, policy.confidence)
        rows.append(
            {
                "week": w,
                "model": model,
                "recall_at_fpr": rec.recall,
                "shipped_fpr": shipped.fpr,
                "shipped_recall": shipped.recall,
            }
        )
    return rows


def check_no_test_week_leakage(st: ScoreTable, exp: ExperimentConfig) -> None:
    """Data rules (CLAUDE.md): v1 never saw a test week (fit, threshold or calibration); a
    candidate retrained at T saw only weeks <= T, and its threshold/calibration came from
    weeks <= T. Raises if any model breaks them."""
    first_test = exp.test_weeks.first
    for mid, m in st.models.items():
        used = (*m.fit_weeks, *m.calibration_weeks)
        if mid == V1_ID:
            if max(used) >= first_test or m.as_of_week >= first_test:
                raise ValueError(
                    f"v1 used test week(s) {sorted(w for w in used if w >= first_test)}"
                )
        elif max(used) > m.as_of_week:
            raise ValueError(f"{mid} used week(s) after its retrain week {m.as_of_week}")


def run_experiment(
    st: ScoreTable, exp: ExperimentConfig, policy: GatePolicy, resamples: int | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """All resamples x release policies x retraining policies. Deterministic for a seed."""
    missing = [m for m in exp.model_ids() if m not in st.models]
    if missing:
        raise ValueError(f"exports lack candidates: {missing}")
    check_no_test_week_leakage(st, exp)
    n = exp.bootstrap_resamples if resamples is None else resamples
    decisions: list[dict[str, Any]] = []
    weekly: list[dict[str, Any]] = []
    drift: dict[tuple[str, int], DriftReading] = {}  # real stream only (no labels used)
    for r in range(n + 1):
        v = View(st, resample_index(st.week, r, exp.seed))
        for rp in RELEASE_POLICIES:
            for pol in exp.retrain_policies:
                recs, live = simulate_chain(v, r, rp, pol, exp, policy, drift)
                decisions += [asdict(x) for x in recs]
                weekly += [
                    {"resample": r, "release_policy": rp, "retrain_policy": pol, **row}
                    for row in weekly_series(v, live, exp, policy)
                ]
    return pd.DataFrame(decisions), pd.DataFrame(weekly)
