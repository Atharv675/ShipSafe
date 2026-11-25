"""Stage 7b Pass 1: measurement and recalibration (owner brief, answers 2026-11-24).

Point-in-time rule: a decision at week t sees labelled weeks <= t - 6 only. Candidates were
retrained on weeks <= T - 6 (Kaggle, `--label-delay-weeks 6`), so a candidate exists at T.

- P1 auto-promote: decides at T, serves from T+1.
- P2 validation AUC: candidate vs production AUC on the candidate's calibration weeks (all
  <= T-6, fully labelled); decides at T.
- P3 gate (policy 1.0.0 unchanged): shadows T+1..T+2 and decides when week T+2 is labelled
  (T+8); serves from T+9.
Undecided candidates (decision after the last week) leave production unchanged: their weeks
are scored with whatever model serves. "Evaluated" = candidates considered, "Decided" =
decisions made inside the data.

Outcome of a promotion (a measurement, never a decision input): recall at 0.1% FPR over the
4 weeks after it went live, candidate minus replaced model, on the same files, with a 95%
family-clustered bootstrap CI. Harm margin M = max(week-to-week recall noise of an unchanged
model on pre-test weeks, X). Harmful = loss > M with the CI excluding 0 (or a never-break
hit). Better = gain > M with the CI excluding 0. Otherwise: within noise.

Weekly recalibration (optional): each week the live model's cut-off moves toward 0.1% FPR on
the benign files of the newest labelled weeks (<= t-6), at most Y per week; a promoted model
starts from its own shipped cut-off. The P3 gate then compares both models at their
recalibrated cut-offs.

Statistics come from sentinel.models.evaluate; the gate is sentinel.gate.checks, unchanged.
"""

from __future__ import annotations

import itertools
import zlib
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, cast

import numpy as np
import numpy.typing as npt
import pandas as pd

from sentinel.config import BacktestConfig, GatePolicy, candidate_id
from sentinel.experiment.export import V1_ID
from sentinel.experiment.scores import ScoreTable
from sentinel.gate.checks import GateInput, ModelScores, decide, run_checks
from sentinel.models.evaluate import (
    auc,
    capped_threshold,
    cluster_bootstrap_rows,
    percentile_interval,
    rates_at_threshold,
    recall_at_fpr,
)

Policy = Literal["P1_auto", "P2_val_auc", "P3_gate"]
POLICIES: tuple[Policy, ...] = ("P1_auto", "P2_val_auc", "P3_gate")
Outcome = Literal["better", "worse", "within noise", "not judged"]
I64 = npt.NDArray[np.int64]


# ---------------------------------------------------------------------------- data


def cluster_keys(st: ScoreTable, meta: pd.DataFrame | None) -> npt.NDArray[np.str_]:
    """Bootstrap cluster per row: the malware family when known, else the file itself
    (benign files, malware without a family). Without metadata every file is its own
    cluster (file-level bootstrap; reported as such)."""
    files = pd.Series(st.file_ids.astype(str))
    if meta is None:
        return np.asarray(("file:" + files).to_numpy(), dtype=str)
    fam = meta.dropna(subset=["family"]).groupby("sha256")["family"].first()
    f = files.map(fam)
    malware_with_family = (st.y == 1) & f.notna().to_numpy()
    keys = np.where(malware_with_family, "fam:" + f.fillna("").to_numpy(), "file:" + files)
    return np.asarray(keys, dtype=str)


@dataclass
class Ctx:
    st: ScoreTable
    cfg: BacktestConfig
    gate: GatePolicy
    clusters: npt.NDArray[np.str_]
    cluster_level: str  # "family" or "file"
    rows_by_week: dict[int, I64] = field(default_factory=dict)
    _delta_cache: dict[tuple[str, str, tuple[int, ...]], Judged] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for w in np.unique(self.st.week):
            self.rows_by_week[int(w)] = np.flatnonzero(self.st.week == w)

    def rows(self, weeks: Sequence[int]) -> I64:
        parts = [self.rows_by_week[w] for w in weeks if w in self.rows_by_week]
        return np.concatenate(parts) if parts else np.empty(0, dtype=np.int64)


# ---------------------------------------------------------------------------- measurement


def recall_on(ctx: Ctx, idx: I64, model: str) -> float | None:
    y = ctx.st.y[idx]
    n_benign = int((y == 0).sum())
    if (y == 1).sum() == 0 or n_benign * ctx.cfg.operating_fpr < 1:
        return None
    return recall_at_fpr(y, ctx.st.raw[model][idx], ctx.cfg.operating_fpr).recall


@dataclass(frozen=True)
class Noise:
    weeks: list[int]
    weekly_recall: list[float]
    week_to_week_changes: list[float]
    noise_points: float  # std of the week-to-week changes, in recall points
    floor_points: float  # X
    margin_points: float  # M = max(noise, X)


def measure_noise(ctx: Ctx, model: str = V1_ID) -> Noise:
    """1b: week-to-week recall noise of an UNCHANGED model on pre-test weeks it was not
    fitted on. M = max(noise, X)."""
    h = ctx.cfg.harm
    weeks = h.noise_weeks.weeks
    fit = set(ctx.st.models[model].fit_weeks)
    if fit & set(weeks):
        raise ValueError(f"noise weeks overlap {model}'s fit weeks")
    rec = [recall_on(ctx, ctx.rows([w]), model) for w in weeks]
    vals = [100 * r for r in rec if r is not None]
    changes = [b - a for a, b in itertools.pairwise(vals)]
    noise = float(np.std(changes, ddof=1)) if len(changes) >= 2 else float("nan")
    margin = max(noise, h.recall_floor_points) if np.isfinite(noise) else h.recall_floor_points
    return Noise(weeks, vals, changes, noise, h.recall_floor_points, margin)


@dataclass(frozen=True)
class Judged:
    weeks: tuple[int, ...]
    candidate_recall: float | None
    production_recall: float | None
    delta_points: float | None
    ci_low: float | None
    ci_high: float | None
    never_break_hits: int


def _seed(*parts: object) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode())


def judge(ctx: Ctx, cand: str, prod: str, deploy_week: int) -> Judged:
    """Paired recall difference (points) over the harm window after `deploy_week`, with a
    family-clustered bootstrap CI. Cached: it does not depend on the release policy."""
    cfg = ctx.cfg
    last = cfg.test_weeks.last
    weeks = tuple(range(deploy_week, min(deploy_week + cfg.harm_weeks - 1, last) + 1))
    key = (cand, prod, weeks)
    if key in ctx._delta_cache:
        return ctx._delta_cache[key]
    hits = ctx.st.flags_never_break(cand)
    if len(weeks) < cfg.min_harm_weeks:
        out = Judged(weeks, None, None, None, None, None, hits)
    else:
        newest = max(ctx.st.models[cand].as_of_week, ctx.st.models[prod].as_of_week)
        if min(weeks) <= newest:
            raise AssertionError("judged on weeks a model learned from")
        idx = ctx.rows(weeks)
        rc, rp = recall_on(ctx, idx, cand), recall_on(ctx, idx, prod)
        if rc is None or rp is None:
            out = Judged(weeks, rc, rp, None, None, None, hits)
        else:
            rng = np.random.default_rng([ctx.cfg.seed, _seed(cand, prod, weeks)])
            deltas = []
            for _ in range(cfg.harm.bootstrap_resamples):
                sub = idx[cluster_bootstrap_rows(ctx.clusters[idx], ctx.st.y[idx], rng)]
                a, b = recall_on(ctx, sub, cand), recall_on(ctx, sub, prod)
                if a is not None and b is not None:
                    deltas.append(100 * (a - b))
            lo, hi = percentile_interval(deltas, cfg.harm.confidence)
            out = Judged(weeks, rc, rp, 100 * (rc - rp), lo, hi, hits)
    ctx._delta_cache[key] = out
    return out


def outcome(j: Judged, margin: float) -> Outcome:
    if j.delta_points is None or j.ci_low is None or j.ci_high is None:
        return "not judged"
    if j.never_break_hits > 0:
        return "worse"
    if j.delta_points < -margin and j.ci_high < 0:
        return "worse"
    if j.delta_points > margin and j.ci_low > 0:
        return "better"
    return "within noise"


# ---------------------------------------------------------------------------- cut-offs


def clean_pool(ctx: Ctx, model: str, decision_week: int) -> npt.NDArray[np.float64]:
    """Benign scores of the newest `clean_pool_weeks` labelled weeks at `decision_week`,
    leaving out weeks the model was fitted on: its scores on its own training files are far
    too low (v1 at week 52 sees only weeks 43-46, all inside its fit window 1-47, where the
    top benign score is ~7e-5) and would drag the cut-off to near zero. Empty pool -> the
    cut-off stays where it is."""
    newest = ctx.cfg.labelled_upto(decision_week)
    fitted = set(ctx.st.models[model].fit_weeks)
    weeks = [
        w
        for w in range(newest - ctx.cfg.recalibration.clean_pool_weeks + 1, newest + 1)
        if w not in fitted
    ]
    idx = ctx.rows(weeks)
    idx = idx[ctx.st.y[idx] == 0]
    return np.asarray(ctx.st.raw[model][idx], dtype=np.float64)


def next_cutoff(ctx: Ctx, model: str, current: float, decision_week: int) -> float:
    return capped_threshold(
        clean_pool(ctx, model, decision_week),
        current,
        ctx.cfg.operating_fpr,
        ctx.cfg.recalibration.max_fpr_move_pp / 100,
    )


# ---------------------------------------------------------------------------- decisions


def decision_week(cfg: BacktestConfig, rp: Policy, t: int) -> int:
    return cfg.p3_decision_week(t) if rp == "P3_gate" else t


def _decide_p2(ctx: Ctx, cand: str, prod: str, t: int) -> tuple[bool, str]:
    info = ctx.st.models[cand]
    if max(info.calibration_weeks) > ctx.cfg.labelled_upto(t):
        raise AssertionError("P2 would use weeks not yet labelled")
    idx = ctx.rows(info.calibration_weeks)
    y = ctx.st.y[idx]
    a_c, a_p = auc(y, ctx.st.raw[cand][idx]), auc(y, ctx.st.raw[prod][idx])
    return a_c > a_p, f"validation AUC {a_c:.5f} vs {a_p:.5f}"


def _decide_p3(
    ctx: Ctx, cand: str, prod: str, t: int, thresholds: dict[str, float]
) -> tuple[bool, str, str]:
    cfg, st = ctx.cfg, ctx.st
    shadow = list(range(t + 1, t + cfg.shadow_weeks + 1))
    if max(shadow) > cfg.labelled_upto(cfg.p3_decision_week(t)):
        raise AssertionError("P3 would use shadow labels that do not exist yet")
    idx = ctx.rows(shadow)

    def scores(m: str) -> ModelScores:
        return ModelScores(
            label=m,
            threshold=thresholds[m],
            scores=st.raw[m][idx],
            probabilities=st.prob[m][idx],
            never_break_scores=st.never_break[m],
        )

    g = GateInput(
        file_ids=[str(v) for v in st.file_ids[idx]],
        y=st.y[idx],
        week=st.week[idx],
        production=scores(prod),
        candidate=scores(cand),
        expected_events=int(idx.size),
        paired_events=int(idx.size),
        unlabelled_events=0,
    )
    checks = run_checks(g, ctx.gate)
    decision, reason = decide(checks)
    failed = [c.name for c in checks if c.status == "fail" and c.hard]
    return decision == "PROMOTE", f"{decision}: {reason}", ",".join(failed)


@dataclass(frozen=True)
class Decision:
    release_policy: str
    recalibration: bool
    retrain_policy: str
    as_of_week: int
    candidate: str
    production: str
    decision_week: int
    decided: bool
    promoted: bool
    deploy_week: int | None
    detail: str
    blocked_by: str
    delta_points: float | None
    ci_low: float | None
    ci_high: float | None
    outcome: str
    never_break_hits: int


def run_chain(
    ctx: Ctx, rp: Policy, retrain_policy: str, recal: bool, margin: float
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """One release policy over one retraining policy's candidates (time order)."""
    cfg, st = ctx.cfg, ctx.st
    last = cfg.test_weeks.last
    prod = V1_ID
    live: dict[int, str] = {}  # week -> model serving
    cutoff: dict[str, float] = {}  # model -> current cut-off (recalibration)
    events = sorted((decision_week(cfg, rp, t), t) for t in cfg.retrain_weeks)
    decisions: list[Decision] = []
    serving = V1_ID
    pending = {dw: t for dw, t in events}
    weekly: list[dict[str, Any]] = []
    cutoff[V1_ID] = st.models[V1_ID].threshold
    # start one week early: a decision at the end of the last train week (T = 52) serves
    # from the first test week; only test weeks are scored
    for w in range(cfg.test_weeks.first - 1, last + 1):
        if w >= cfg.test_weeks.first:
            live[w] = serving
            # the live model's cut-off for week w, set at the end of week w-1 (point-in-time)
            if recal:
                cutoff[serving] = next_cutoff(ctx, serving, cutoff[serving], w - 1)
            thr = cutoff[serving] if recal else st.models[serving].threshold
            idx = ctx.rows([w])
            shipped = rates_at_threshold(st.y[idx], st.raw[serving][idx], thr)
            weekly.append(
                {
                    "release_policy": rp,
                    "recalibration": recal,
                    "retrain_policy": retrain_policy,
                    "week": w,
                    "model": serving,
                    "cutoff": thr,
                    "recall_at_fpr": recall_on(ctx, idx, serving),
                    "shipped_fpr": shipped.fpr,
                    "shipped_recall": shipped.recall,
                }
            )
        # decisions taken at the end of week w (they serve from w+1)
        if w in pending:
            t = pending.pop(w)
            cand = candidate_id(retrain_policy, t)
            cutoff.setdefault(cand, st.models[cand].threshold)
            if recal:  # both models at cut-offs recalibrated on the same labelled pool
                thresholds = {
                    prod: next_cutoff(ctx, prod, cutoff[prod], w),
                    cand: next_cutoff(ctx, cand, cutoff[cand], w),
                }
            else:
                thresholds = {prod: st.models[prod].threshold, cand: st.models[cand].threshold}
            blocked = ""
            if rp == "P1_auto":
                promoted, detail = True, "auto-promote"
            elif rp == "P2_val_auc":
                promoted, detail = _decide_p2(ctx, cand, prod, t)
                blocked = "" if promoted else "validation_auc"
            else:
                promoted, detail, blocked = _decide_p3(ctx, cand, prod, t, thresholds)
            j = judge(ctx, cand, prod, w + 1) if w + 1 <= last else None
            decisions.append(
                Decision(
                    rp,
                    recal,
                    retrain_policy,
                    t,
                    cand,
                    prod,
                    w,
                    True,
                    promoted,
                    w + 1 if w + 1 <= last else None,
                    detail[:300],
                    blocked,
                    j.delta_points if j else None,
                    j.ci_low if j else None,
                    j.ci_high if j else None,
                    outcome(j, margin) if j else "not judged",
                    st.flags_never_break(cand),
                )
            )
            if promoted:
                prod = serving = cand
    for dw, t in sorted(pending.items()):  # decisions that fall after the data
        cand = candidate_id(retrain_policy, t)
        decisions.append(
            Decision(
                rp,
                recal,
                retrain_policy,
                t,
                cand,
                prod,
                dw,
                False,
                False,
                None,
                "no decision before the last week",
                "",
                None,
                None,
                None,
                "not judged",
                st.flags_never_break(cand),
            )
        )
    return decisions, weekly


# ---------------------------------------------------------------------------- policy CI


def mean_recall_ci(ctx: Ctx, weekly: pd.DataFrame) -> tuple[float, float, float]:
    """Mean weekly recall at 0.1% FPR of the models that served, with a family-clustered
    bootstrap CI (clusters resampled within each week; the chain of decisions is fixed)."""
    cfg = ctx.cfg
    point = float(weekly["recall_at_fpr"].mean())
    rng = np.random.default_rng([cfg.seed, _seed("mean", *weekly["model"])])
    vals = []
    for _ in range(cfg.harm.bootstrap_resamples):
        rs = []
        for w, m in zip(weekly["week"], weekly["model"], strict=True):
            idx = ctx.rows_by_week[int(w)]
            sub = idx[cluster_bootstrap_rows(ctx.clusters[idx], ctx.st.y[idx], rng)]
            r = recall_on(ctx, sub, str(m))
            if r is not None:
                rs.append(r)
        vals.append(float(np.mean(rs)))
    lo, hi = percentile_interval(vals, cfg.harm.confidence)
    return point, lo, hi


# ---------------------------------------------------------------------------- leak audit


def original_p3_leak(cfg: BacktestConfig, original_wait_weeks: int = 4) -> list[dict[str, Any]]:
    """1f: Stage 7's P3 shadowed T+1..T+2 and decided at T+6 (28-day label wait). Under the
    6-week rule week w is labelled only at w+6: list what it used before it existed."""
    rows = []
    for t in cfg.retrain_weeks:
        shadow = list(range(t + 1, t + cfg.shadow_weeks + 1))
        decided = t + cfg.shadow_weeks + original_wait_weeks
        unavailable = [w for w in shadow if w > cfg.labelled_upto(decided)]
        rows.append(
            {
                "as_of_week": t,
                "shadow_weeks": shadow,
                "original_decision_week": decided,
                "labels_used_before_they_exist": unavailable,
                "leaky": bool(unavailable),
                "point_in_time_decision_week": cfg.p3_decision_week(t),
            }
        )
    return rows


# ---------------------------------------------------------------------------- early labels


def simulated_early_agreement(ctx: Ctx, flip_rate: float) -> pd.DataFrame:
    """Step 3, SIMULATED: early label = final label flipped with probability `flip_rate`
    (EMBER2024 has no early labels). Agreement per test week."""
    rng = np.random.default_rng([ctx.cfg.seed, _seed("early", flip_rate)])
    rows = []
    for w in ctx.cfg.test_weeks.weeks:
        idx = ctx.rows_by_week[w]
        flips = rng.random(idx.size) < flip_rate
        rows.append(
            {
                "week": w,
                "flip_rate": flip_rate,
                "agreement": float(1 - flips.mean()),
                "files": int(idx.size),
                "simulated": True,
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------- the run


def run_pass1(
    st: ScoreTable, meta: pd.DataFrame | None, cfg: BacktestConfig, gate: GatePolicy
) -> dict[str, Any]:
    ctx = Ctx(st, cfg, gate, cluster_keys(st, meta), "family" if meta is not None else "file")
    for m in st.models.values():  # point-in-time: candidates only learned weeks <= T-6
        if m.model_id != V1_ID and max(*m.fit_weeks, *m.calibration_weeks) > cfg.labelled_upto(
            m.as_of_week
        ):
            raise ValueError(f"{m.model_id} used weeks after T-{cfg.label_delay_weeks}")
    noise = measure_noise(ctx)
    decisions: list[dict[str, Any]] = []
    weekly: list[dict[str, Any]] = []
    for rp in POLICIES:
        for recal in (False, True):
            for pol in cfg.retrain_policies:
                d, w = run_chain(ctx, rp, pol, recal, noise.margin_points)
                decisions += [asdict(x) for x in d]
                weekly += w
    dec, wk = pd.DataFrame(decisions), pd.DataFrame(weekly)
    cis = []
    for keys, g in wk.groupby(["release_policy", "recalibration", "retrain_policy"]):
        rp_k, recal_k, pol_k = cast(tuple[str, bool, str], keys)
        point, lo, hi = mean_recall_ci(ctx, g.sort_values("week"))
        cis.append(
            {
                "release_policy": rp_k,
                "recalibration": recal_k,
                "retrain_policy": pol_k,
                "mean_recall": point,
                "ci_low": lo,
                "ci_high": hi,
            }
        )
    early = pd.concat(
        [simulated_early_agreement(ctx, f) for f in cfg.early_labels.flip_rates],
        ignore_index=True,
    )
    return {
        "noise": noise,
        "decisions": dec,
        "weekly": wk,
        "mean_recall_ci": pd.DataFrame(cis),
        "p3_leak": original_p3_leak(cfg),
        "early_labels": early,
        "cluster_level": ctx.cluster_level,
    }


# ---------------------------------------------------------------------------- 1d + summary


def rejudge(
    st: ScoreTable,
    meta: pd.DataFrame | None,
    cfg: BacktestConfig,
    gate: GatePolicy,
    original: pd.DataFrame,
    margin: float,
) -> pd.DataFrame:
    """1d: re-judge Stage 7's original harmful promotions (its own candidates and chain)
    with the new definition (loss > M and family-clustered CI excluding 0). The same swap
    shipped by several release policies is one row, listing the policies."""
    ctx = Ctx(st, cfg, gate, cluster_keys(st, meta), "family" if meta is not None else "file")
    rows = []
    key = ["candidate", "production", "deploy_week"]
    shipped_by: dict[tuple[str, str, int], set[str]] = {}
    for c, p, w, rp in original[[*key, "release_policy"]].itertuples(index=False):
        shipped_by.setdefault((str(c), str(p), int(w)), set()).add(str(rp))
    for r in original.drop_duplicates(key).itertuples():
        j = judge(ctx, str(r.candidate), str(r.production), int(cast(Any, r.deploy_week)))
        policies = shipped_by[(str(r.candidate), str(r.production), int(cast(Any, r.deploy_week)))]
        rows.append(
            {
                "release_policy": ",".join(sorted(policies)),
                "candidate": r.candidate,
                "production": r.production,
                "deploy_week": int(cast(Any, r.deploy_week)),
                "original_loss_points": 100 * float(cast(Any, r.recall_gain)),
                "delta_points": j.delta_points,
                "ci_low": j.ci_low,
                "ci_high": j.ci_high,
                "outcome_now": outcome(j, margin),
            }
        )
    return pd.DataFrame(rows)


def summary_table(
    ctx_mean: pd.DataFrame, decisions: pd.DataFrame, weekly: pd.DataFrame
) -> pd.DataFrame:
    """One row per (release policy, recalibration), summed over retraining policies."""
    rows = []
    for (rp, recal), d in decisions.groupby(["release_policy", "recalibration"], sort=True):
        prom = d[d["promoted"]]
        harmful = prom[prom["outcome"] == "worse"]
        w = weekly[(weekly["release_policy"] == rp) & (weekly["recalibration"] == recal)]
        days = 0
        for h in harmful.itertuples():
            chain = w[w["retrain_policy"] == h.retrain_policy]
            days += 7 * int((chain["model"] == h.candidate).sum())
        m = ctx_mean[(ctx_mean["release_policy"] == rp) & (ctx_mean["recalibration"] == recal)]
        losses = prom["delta_points"].dropna()
        rows.append(
            {
                "release_policy": rp,
                "recalibration": bool(recal),
                "evaluated": len(d),
                "decided": int(d["decided"].sum()),
                "promotions": len(prom),
                "harmful": len(harmful),
                "max_loss_points": float(-losses.min())
                if len(losses) and losses.min() < 0
                else 0.0,
                "better_blocked": int(
                    ((~d["promoted"]) & d["decided"] & (d["outcome"] == "better")).sum()
                ),
                "mean_recall": float(m["mean_recall"].mean()),
                "ci_low": float(m["ci_low"].mean()),
                "ci_high": float(m["ci_high"].mean()),
                "shipped_fpr": float(w["shipped_fpr"].mean()),
                "shipped_recall": float(w["shipped_recall"].mean()),
                "time_to_ship_weeks": float((prom["deploy_week"] - prom["as_of_week"]).mean())
                if len(prom)
                else float("nan"),
                "days_harmful_live": days,
            }
        )
    return pd.DataFrame(rows)
