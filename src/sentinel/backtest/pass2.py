"""Stage 7b Pass 2: P4 (fast decision + verify + rollback) and the extras (owner brief steps
4-9; answers of 2026-11-24 and 2026-11-25). Every threshold is in configs/backtest.yaml and was
fixed before this ran; the only tuning is walk-forward (step 6), point-in-time.

Point-in-time rule as in Pass 1: at week t only weeks <= t-6 are labelled.

P4, for a candidate retrained at T (decides at T, serves from T+1):
  Stage 1 (day 0) - promote only if every check passes, at MATCHED cut-offs set on the same
  pre-decision data for both models:
    * evidence weeks E = newest `eval_weeks` labelled weeks, minus every week either model was
      fitted on (a model's scores on its own training files are biased). E empty -> blocked
      ("insufficient evidence").
    * clean-pool match: both cut-offs at 0.1% FPR on E's benign files. Traffic match:
      production as clean-pool, candidate at production's flag rate on unlabelled week-T files.
    * non-inferiority: recall@0.1%FPR on E, candidate >= production - M.
    * never-break: 0 of 500 benign flagged; must-catch: 0 of 500 malware missed (malware from
      E that production flags at its matched cut-off, seeded sample - owner 2026-11-25).
    * new flags (prod 0, cand 1): the candidate's extra false alarms on E's benign files must
      not exceed production's extra false alarms (cand 0, prod 1): fail when the candidate's
      lower Wilson bound is above production's upper Wilson bound.
    * unflags (prod 1, cand 0): malware the candidate drops must not exceed malware it adds,
      same Wilson rule (malware files of E). Unflags are never counted as a benefit.
    * flag-rate stability on week-T traffic: |cand - prod| <= Z * prod.
    * slices (malware of E, matched cut-offs): families first seen in the last K labelled
      weeks, and signature status (unsigned / self-signed / signed - substitute for "top N
      signers", owner 2026-11-25): candidate recall >= production - M; < min_slice_files -> n/a.
  Stage 2 (end of the first live week): only when SIMULATED early-label agreement (measured on
  the newest labelled week) >= A. Early recall@0.1%FPR on the live week, candidate minus the
  replaced model, below -M -> roll back.
  Stage 3 (final labels, weeks 1..6 after deployment): always-valid CI by Bonferroni (each look
  at 1 - alpha/looks, family-clustered bootstrap). delta < -M and CI below 0 -> roll back;
  CI above -M -> confirmed (monitoring stops).

Statistics come from sentinel.models.evaluate; Pass 1's measurement code is reused.
"""

from __future__ import annotations

import itertools
import zlib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pandas as pd

from sentinel.backtest.pass1 import (
    Ctx,
    Judged,
    clean_pool,
    cluster_keys,
    judge,
    mean_recall_ci,
    next_cutoff,
    outcome,
    recall_on,
)
from sentinel.config import BacktestConfig, DriftConfig, GatePolicy, candidate_id
from sentinel.experiment.export import V1_ID
from sentinel.experiment.scores import ModelInfo, ScoreTable
from sentinel.models.evaluate import (
    capped_threshold,
    cluster_bootstrap_rows,
    mcnemar_exact,
    percentile_interval,
    population_stability_index,
    rates_at_threshold,
    recall_at_fpr,
    score_bin_counts,
    threshold_at_fpr,
    wilson_interval,
)

I64 = npt.NDArray[np.int64]
F64 = npt.NDArray[np.float64]


def _seed(*parts: object) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode())


# ---------------------------------------------------------------------------- parameters


@dataclass(frozen=True)
class Params:
    """The P4 thresholds step 6 may tune (everything else is fixed in the config)."""

    margin_points: float
    confidence: float
    eval_weeks: int
    flag_rate_tolerance: float


def preregistered(cfg: BacktestConfig, margin_points: float) -> Params:
    return Params(margin_points, cfg.p4.confidence, cfg.p4.eval_weeks, cfg.p4.flag_rate_tolerance)


def grid(cfg: BacktestConfig) -> list[Params]:
    g = cfg.p4.walk_forward
    return [
        Params(m, c, e, z)
        for m, c, e, z in itertools.product(
            g.margin_points, g.confidence, g.eval_weeks, g.flag_rate_tolerance
        )
    ]


# ---------------------------------------------------------------------------- slices


@dataclass(frozen=True)
class Slices:
    """Per-row family and signature status (aligned with the score table)."""

    family: npt.NDArray[np.object_]
    first_week: dict[str, int]  # family -> first week it appears (any label, metadata weeks)
    signature: npt.NDArray[np.str_]  # unsigned / self_signed / signed / unknown


def build_slices(st: ScoreTable, meta: pd.DataFrame | None) -> Slices:
    n = st.file_ids.size
    if meta is None:
        return Slices(np.full(n, None, dtype=object), {}, np.full(n, "unknown"))
    m = meta.drop_duplicates("sha256").set_index("sha256")
    ids = pd.Index(st.file_ids.astype(str))
    fam = m["family"].reindex(ids).to_numpy(dtype=object)
    certs = m["num_certs"].reindex(ids).to_numpy(dtype=np.float64)
    selfs = m["self_signed"].reindex(ids).to_numpy(dtype=np.float64)
    sig = np.where(
        np.isnan(certs),
        "unknown",
        np.where(certs <= 0, "unsigned", np.where(selfs > 0, "self_signed", "signed")),
    )
    known = meta.dropna(subset=["family"])
    first = {str(k): int(v) for k, v in known.groupby("family")["week"].min().items()}
    return Slices(fam, first, np.asarray(sig, dtype=str))


# ---------------------------------------------------------------------------- stage 1


def evidence_weeks(ctx: Ctx, cand: str, prod: str, t: int, n_weeks: int) -> list[int]:
    newest = ctx.cfg.labelled_upto(t)
    fitted = set(ctx.st.models[cand].fit_weeks) | set(ctx.st.models[prod].fit_weeks)
    return [
        w
        for w in range(newest - n_weeks + 1, newest + 1)
        if w not in fitted and w in ctx.rows_by_week
    ]


def traffic_cutoff(cand_scores: F64, target_flag_rate: float) -> float:
    """Cut-off giving `cand_scores` (unlabelled traffic) at most this flag rate."""
    return threshold_at_fpr(
        np.zeros(cand_scores.size, dtype=np.int8), cand_scores, target_flag_rate
    )


def _worse_rate(k_c: int, k_p: int, n: int, conf: float) -> tuple[bool, str]:
    """Candidate count significantly above production's (Wilson bounds do not overlap)."""
    lo_c, _ = wilson_interval(k_c, n, conf)
    _, hi_p = wilson_interval(k_p, n, conf)
    return lo_c > hi_p, f"{k_c} vs {k_p} of {n}"


def day0(
    ctx: Ctx,
    sl: Slices,
    cand: str,
    prod: str,
    t: int,
    p: Params,
    method: str,
) -> dict[str, Any]:
    """Stage 1 checks. Returns the verdict, each check's status/detail, and cut-offs."""
    cfg, st, p4 = ctx.cfg, ctx.st, ctx.cfg.p4
    out: dict[str, Any] = {"candidate": cand, "production": prod, "as_of_week": t}
    weeks = evidence_weeks(ctx, cand, prod, t, p.eval_weeks)
    idx = ctx.rows(weeks)
    y = st.y[idx]
    benign = idx[y == 0]
    out.update(evidence_weeks=weeks, clean_files=int(benign.size))
    out["clean_warning"] = benign.size < p4.min_clean_files_warn
    checks: dict[str, tuple[str, str]] = {}
    if benign.size * cfg.operating_fpr < 1 or (y == 1).sum() == 0:
        checks["evidence"] = ("fail", f"weeks {weeks}: {benign.size} clean files")
        out.update(checks=checks, promote=False, blocked_by="evidence")
        return out
    thr_p = threshold_at_fpr(
        np.zeros(benign.size, dtype=np.int8), st.raw[prod][benign], cfg.operating_fpr
    )
    traffic = ctx.rows([t])
    if method == "clean":
        thr_c = threshold_at_fpr(
            np.zeros(benign.size, dtype=np.int8), st.raw[cand][benign], cfg.operating_fpr
        )
    else:
        fr_p0 = float((st.raw[prod][traffic] > thr_p).mean()) if traffic.size else 0.0
        thr_c = traffic_cutoff(st.raw[cand][traffic], fr_p0)
    out.update(cutoff_production=thr_p, cutoff_candidate=thr_c)

    # non-inferiority at 0.1% FPR on E
    rc, rp = recall_on(ctx, idx, cand), recall_on(ctx, idx, prod)
    assert rc is not None and rp is not None
    d = 100 * (rc - rp)
    checks["non_inferiority"] = (
        "pass" if d >= -p.margin_points else "fail",
        f"recall {100 * rc:.2f} vs {100 * rp:.2f} ({d:+.2f} pts, M {p.margin_points:.2f})",
    )
    # never-break (benign) + must-catch (malware)
    nb_hits = int((st.never_break[cand] > thr_c).sum())
    checks["never_break"] = (
        "pass" if nb_hits <= p4.never_break_allowed else "fail",
        f"{nb_hits} of {st.never_break[cand].size} flagged",
    )
    mal = idx[y == 1]
    caught = mal[st.raw[prod][mal] > thr_p]
    rng = np.random.default_rng([cfg.seed, _seed("must-catch", t, prod)])
    must = (
        rng.choice(caught, p4.must_catch_files, replace=False)
        if caught.size > p4.must_catch_files
        else caught
    )
    missed = int((st.raw[cand][must] <= thr_c).sum())
    checks["must_catch"] = (
        "pass" if missed <= p4.must_catch_missed_allowed else "fail",
        f"{missed} of {must.size} missed",
    )
    out["must_catch_files"] = int(must.size)
    # directional disagreement on E at matched cut-offs
    fc, fp = st.raw[cand][idx] > thr_c, st.raw[prod][idx] > thr_p
    b0, b1 = y == 0, y == 1
    new_benign, gone_benign = int((fc & ~fp & b0).sum()), int((~fc & fp & b0).sum())
    dropped_mal, added_mal = int((~fc & fp & b1).sum()), int((fc & ~fp & b1).sum())
    bad, det = _worse_rate(new_benign, gone_benign, int(b0.sum()), p.confidence)
    checks["new_flags"] = ("fail" if bad else "pass", f"benign new flags {det}")
    bad, det = _worse_rate(dropped_mal, added_mal, int(b1.sum()), p.confidence)
    checks["unflags"] = ("fail" if bad else "pass", f"malware dropped vs added {det}")
    out.update(
        disagree_b=int((fp & ~fc & b1).sum() + (fc & ~fp & b0).sum()),  # production right
        disagree_c=int((fc & ~fp & b1).sum() + (fp & ~fc & b0).sum()),  # candidate right
    )
    # flag-rate stability on week-T traffic
    if traffic.size:
        fr_c = float((st.raw[cand][traffic] > thr_c).mean())
        fr_p = float((st.raw[prod][traffic] > thr_p).mean())
        ok = abs(fr_c - fr_p) <= p.flag_rate_tolerance * fr_p
        checks["flag_rate"] = ("pass" if ok else "fail", f"{100 * fr_c:.2f}% vs {100 * fr_p:.2f}%")
    else:
        checks["flag_rate"] = ("n/a", "no week-T traffic")
    # slices
    newest = cfg.labelled_upto(t)
    new_fam = np.array(
        [
            f is not None and sl.first_week.get(str(f), -1) > newest - p4.new_family_weeks
            for f in sl.family[mal]
        ],
        dtype=bool,
    )
    slices = {"new_families": mal[new_fam]}
    for s in p4.signer_slices:
        slices[f"signature_{s}"] = mal[sl.signature[mal] == s]
    for name, rows in slices.items():
        if rows.size < p4.min_slice_files:
            checks[name] = ("n/a", f"{rows.size} malware files")
            continue
        r_c = float((st.raw[cand][rows] > thr_c).mean())
        r_p = float((st.raw[prod][rows] > thr_p).mean())
        dd = 100 * (r_c - r_p)
        checks[name] = (
            "pass" if dd >= -p.margin_points else "fail",
            f"recall {100 * r_c:.2f} vs {100 * r_p:.2f} on {rows.size}",
        )
    failed = [k for k, (s, _) in checks.items() if s == "fail"]
    out.update(checks=checks, promote=not failed, blocked_by=",".join(failed))
    return out


# ---------------------------------------------------------------------------- stages 2-3


def early_labels(ctx: Ctx, week: int, flip_rate: float) -> tuple[I64, npt.NDArray[np.int8]]:
    """SIMULATED early labels of one week: the final label flipped with `flip_rate`."""
    idx = ctx.rows_by_week[week]
    rng = np.random.default_rng([ctx.cfg.seed, _seed("p4-early", flip_rate, week)])
    flips = rng.random(idx.size) < flip_rate
    y = ctx.st.y[idx].copy()
    y[flips] = 1 - y[flips]
    return idx, y


def early_agreement(ctx: Ctx, t: int, flip_rate: float) -> float:
    """Agreement of early with final labels on the newest week whose final labels exist."""
    w = ctx.cfg.labelled_upto(t)
    if w not in ctx.rows_by_week:
        return float("nan")
    idx, ye = early_labels(ctx, w, flip_rate)
    return float((ye == ctx.st.y[idx]).mean())


def early_delta(ctx: Ctx, cand: str, prod: str, week: int, flip_rate: float) -> float | None:
    idx, ye = early_labels(ctx, week, flip_rate)
    if (ye == 1).sum() == 0 or (ye == 0).sum() * ctx.cfg.operating_fpr < 1:
        return None
    rc = recall_at_fpr(ye, ctx.st.raw[cand][idx], ctx.cfg.operating_fpr).recall
    rp = recall_at_fpr(ye, ctx.st.raw[prod][idx], ctx.cfg.operating_fpr).recall
    return 100 * (rc - rp)


@dataclass
class SeqCache:
    store: dict[tuple[str, str, tuple[int, ...], float], Judged] = field(default_factory=dict)


def delta_ci(
    ctx: Ctx, cand: str, prod: str, weeks: Sequence[int], conf: float, cache: SeqCache
) -> Judged:
    key = (cand, prod, tuple(weeks), conf)
    if key in cache.store:
        return cache.store[key]
    idx = ctx.rows(weeks)
    rc, rp = recall_on(ctx, idx, cand), recall_on(ctx, idx, prod)
    if rc is None or rp is None:
        out = Judged(tuple(weeks), rc, rp, None, None, None, 0)
    else:
        rng = np.random.default_rng([ctx.cfg.seed, _seed("seq", cand, prod, *weeks, conf)])
        deltas = []
        for _ in range(ctx.cfg.harm.bootstrap_resamples):
            sub = idx[cluster_bootstrap_rows(ctx.clusters[idx], ctx.st.y[idx], rng)]
            a, b = recall_on(ctx, sub, cand), recall_on(ctx, sub, prod)
            if a is not None and b is not None:
                deltas.append(100 * (a - b))
        lo, hi = percentile_interval(deltas, conf)
        out = Judged(tuple(weeks), rc, rp, 100 * (rc - rp), lo, hi, 0)
    cache.store[key] = out
    return out


# ---------------------------------------------------------------------------- chains


@dataclass
class Monitor:
    candidate: str
    replaced: str
    deploy_week: int
    confirmed: bool = False


Decider = Callable[[str, str, int], tuple[bool, str, dict[str, Any]]]


def run_p4_chain(
    ctx: Ctx,
    sl: Slices,
    retrain_policy: str,
    label: str,
    params_for: Callable[[int], Params],
    method: str,
    flip_rate: float,
    cache: SeqCache,
    noise_margin: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """One P4 variant over one retraining policy. Weekly cut-off recalibration is on (P4 sets
    cut-offs point-in-time by design). Returns (decisions, weekly rows, events)."""
    cfg, st, p4 = ctx.cfg, ctx.st, ctx.cfg.p4
    last = cfg.test_weeks.last
    prod = V1_ID
    cutoff: dict[str, float] = {V1_ID: st.models[V1_ID].threshold}
    pending = set(cfg.retrain_weeks)
    decisions: list[dict[str, Any]] = []
    weekly: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    mon: Monitor | None = None
    conf_seq = 1 - p4.sequential_alpha / p4.sequential_looks

    def rollback(w: int, why: str) -> None:
        nonlocal prod, mon
        assert mon is not None
        events.append(
            {
                "policy": label,
                "retrain_policy": retrain_policy,
                "week": w,
                "candidate": mon.candidate,
                "event": "rollback",
                "detail": why,
            }
        )
        prod = mon.replaced
        mon = None

    for w in range(cfg.test_weeks.first - 1, last + 1):
        if w >= cfg.test_weeks.first:
            cutoff[prod] = next_cutoff(ctx, prod, cutoff[prod], w - 1)
            idx = ctx.rows([w])
            shipped = rates_at_threshold(st.y[idx], st.raw[prod][idx], cutoff[prod])
            weekly.append(
                {
                    "release_policy": label,
                    "recalibration": True,
                    "retrain_policy": retrain_policy,
                    "week": w,
                    "model": prod,
                    "cutoff": cutoff[prod],
                    "recall_at_fpr": recall_on(ctx, idx, prod),
                    "shipped_fpr": shipped.fpr,
                    "shipped_recall": shipped.recall,
                }
            )
        # end of week w: monitoring of the live promotion first, then any new decision
        if mon is not None:
            margin = params_for(mon.deploy_week - 1).margin_points
            if w == mon.deploy_week:  # stage 2
                agree = early_agreement(ctx, w, flip_rate)
                if agree >= p4.min_early_agreement:
                    d = early_delta(ctx, mon.candidate, mon.replaced, w, flip_rate)
                    events.append(
                        {
                            "policy": label,
                            "retrain_policy": retrain_policy,
                            "week": w,
                            "candidate": mon.candidate,
                            "event": "stage2",
                            "detail": f"agreement {100 * agree:.2f}%, early delta "
                            + ("n/a" if d is None else f"{d:+.2f} pts"),
                        }
                    )
                    if d is not None and d < -margin:
                        rollback(w, f"stage 2: early recall {d:+.2f} pts < -M")
                else:
                    events.append(
                        {
                            "policy": label,
                            "retrain_policy": retrain_policy,
                            "week": w,
                            "candidate": mon.candidate,
                            "event": "stage2_skipped",
                            "detail": f"agreement {100 * agree:.2f}% < A",
                        }
                    )
            if mon is not None and not mon.confirmed:  # stage 3
                newest = cfg.labelled_upto(w)
                k = newest - mon.deploy_week + 1
                if 1 <= k <= p4.sequential_looks:
                    weeks = list(range(mon.deploy_week, newest + 1))
                    j = delta_ci(ctx, mon.candidate, mon.replaced, weeks, conf_seq, cache)
                    if (
                        j.delta_points is not None
                        and j.ci_low is not None
                        and j.ci_high is not None
                    ):
                        if j.delta_points < -margin and j.ci_high < 0:
                            rollback(
                                w,
                                f"stage 3 look {k}: {j.delta_points:+.2f} "
                                f"[{j.ci_low:.2f}, {j.ci_high:.2f}]",
                            )
                        elif j.ci_low > -margin:
                            mon.confirmed = True
                            events.append(
                                {
                                    "policy": label,
                                    "retrain_policy": retrain_policy,
                                    "week": w,
                                    "candidate": mon.candidate,
                                    "event": "confirmed",
                                    "detail": f"look {k}: {j.delta_points:+.2f} "
                                    f"[{j.ci_low:.2f}, {j.ci_high:.2f}]",
                                }
                            )
        if w in pending:
            pending.discard(w)
            cand = candidate_id(retrain_policy, w)
            p = params_for(w)
            res = day0(ctx, sl, cand, prod, w, p, method)
            jd = judge(ctx, cand, prod, w + 1) if w + 1 <= last else None
            decisions.append(
                {
                    "release_policy": label,
                    "recalibration": True,
                    "retrain_policy": retrain_policy,
                    "as_of_week": w,
                    "candidate": cand,
                    "production": prod,
                    "decision_week": w,
                    "decided": True,
                    "promoted": bool(res["promote"]),
                    "deploy_week": w + 1 if w + 1 <= last else None,
                    "detail": "; ".join(f"{k}: {s} ({d})" for k, (s, d) in res["checks"].items())[
                        :600
                    ],
                    "blocked_by": res["blocked_by"],
                    "delta_points": jd.delta_points if jd else None,
                    "ci_low": jd.ci_low if jd else None,
                    "ci_high": jd.ci_high if jd else None,
                    "outcome": outcome(jd, noise_margin) if jd else "not judged",
                    "never_break_hits": st.flags_never_break(cand),
                    "clean_files": res["clean_files"],
                    "evidence_weeks": ",".join(str(x) for x in res["evidence_weeks"]),
                    "disagree_b": res.get("disagree_b"),
                    "disagree_c": res.get("disagree_c"),
                    "margin_points": p.margin_points,
                }
            )
            if res["promote"] and w + 1 <= last:
                cutoff[cand] = float(res["cutoff_candidate"])
                mon = Monitor(cand, prod, w + 1)
                prod = cand
    return decisions, weekly, events


def _blend_table(
    st: ScoreTable, cfg: BacktestConfig, retrain_policy: str
) -> tuple[ScoreTable, list[str]]:
    """Add the chain of blended models: B_T = (1-a) * B_prev + a * candidate_T (B_prev = v1
    first). Each is a new model id; its cut-off is set at decision time (`run_blend_chain`)."""
    a = cfg.blend.weight_candidate
    raw, prob, nb, models = dict(st.raw), dict(st.prob), dict(st.never_break), dict(st.models)
    prev = V1_ID
    ids: list[str] = []
    for t in cfg.retrain_weeks:
        c = candidate_id(retrain_policy, t)
        b = f"blend_{retrain_policy}_T{t}"
        raw[b] = (1 - a) * raw[prev] + a * raw[c]
        prob[b] = (1 - a) * prob[prev] + a * prob[c]
        nb[b] = (1 - a) * nb[prev] + a * nb[c]
        fit = tuple(sorted(set(models[prev].fit_weeks) | set(models[c].fit_weeks)))
        models[b] = ModelInfo(b, "blend", t, models[c].threshold, fit, ())
        ids.append(b)
        prev = b
    return replace(st, raw=raw, prob=prob, never_break=nb, models=models), ids


def run_blend_chain(
    ctx: Ctx, retrain_policy: str, ids: list[str], noise_margin: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Step 9: at each T the blend of production and the candidate is promoted. Its cut-off:
    0.1% FPR on labelled benign files (<= T-6, 4 weeks, minus fit weeks); if none are left,
    production's flag rate on unlabelled week-T traffic. Weekly recalibration after that."""
    cfg, st = ctx.cfg, ctx.st
    last = cfg.test_weeks.last
    prod = V1_ID
    cutoff: dict[str, float] = {V1_ID: st.models[V1_ID].threshold}
    by_t = dict(zip(cfg.retrain_weeks, ids, strict=True))
    decisions: list[dict[str, Any]] = []
    weekly: list[dict[str, Any]] = []
    label = "P5_blend"
    for w in range(cfg.test_weeks.first - 1, last + 1):
        if w >= cfg.test_weeks.first:
            cutoff[prod] = next_cutoff(ctx, prod, cutoff[prod], w - 1)
            idx = ctx.rows([w])
            shipped = rates_at_threshold(st.y[idx], st.raw[prod][idx], cutoff[prod])
            weekly.append(
                {
                    "release_policy": label,
                    "recalibration": True,
                    "retrain_policy": retrain_policy,
                    "week": w,
                    "model": prod,
                    "cutoff": cutoff[prod],
                    "recall_at_fpr": recall_on(ctx, idx, prod),
                    "shipped_fpr": shipped.fpr,
                    "shipped_recall": shipped.recall,
                }
            )
        if w in by_t:
            b = by_t[w]
            pool = clean_pool(ctx, b, w)
            if pool.size * cfg.operating_fpr >= 1:
                thr = threshold_at_fpr(np.zeros(pool.size, dtype=np.int8), pool, cfg.operating_fpr)
                how = f"clean pool ({pool.size} benign)"
            else:
                tr = ctx.rows([w])
                fr = float((st.raw[prod][tr] > cutoff[prod]).mean())
                thr = traffic_cutoff(st.raw[b][tr], fr)
                how = f"traffic match ({100 * fr:.2f}% flag rate)"
            j = judge(ctx, b, prod, w + 1) if w + 1 <= last else None
            decisions.append(
                {
                    "release_policy": label,
                    "recalibration": True,
                    "retrain_policy": retrain_policy,
                    "as_of_week": w,
                    "candidate": b,
                    "production": prod,
                    "decision_week": w,
                    "decided": True,
                    "promoted": w + 1 <= last,
                    "deploy_week": w + 1 if w + 1 <= last else None,
                    "detail": f"blend promoted; cut-off by {how}",
                    "blocked_by": "",
                    "delta_points": j.delta_points if j else None,
                    "ci_low": j.ci_low if j else None,
                    "ci_high": j.ci_high if j else None,
                    "outcome": outcome(j, noise_margin) if j else "not judged",
                    "never_break_hits": int((st.never_break[b] > thr).sum()),
                }
            )
            if w + 1 <= last:
                cutoff[b] = thr
                prod = b
    return decisions, weekly


# ---------------------------------------------------------------------------- step 6


def tuning_value(ctx: Ctx, sl: Slices, pairs: list[tuple[str, str, int]], p: Params) -> float:
    """Total realised recall change (points, 4 weeks after T) of the candidates P4 would
    promote with `p`, each against its predecessor. Blocking a loss adds value."""
    total = 0.0
    for cand, prod, t in pairs:
        j = judge(ctx, cand, prod, t + 1)
        if j.delta_points is None:
            continue
        if day0(ctx, sl, cand, prod, t, p, "clean")["promote"]:
            total += j.delta_points
    return total


def tune(ctx: Ctx, sl: Slices, pairs: list[tuple[str, str, int]], default: Params) -> Params:
    """Best grid point on `pairs`; ties keep the pre-registered value (then grid order)."""
    if not pairs:
        return default
    best, best_v = default, tuning_value(ctx, sl, pairs, default)
    for p in grid(ctx.cfg):
        v = tuning_value(ctx, sl, pairs, p)
        if v > best_v + 1e-12:
            best, best_v = p, v
    return best


def predecessor_pairs(cfg: BacktestConfig) -> list[tuple[str, str, int]]:
    """Every candidate against the one before it in its retraining policy (T=52: v1)."""
    out = []
    for pol in cfg.retrain_policies:
        prev = V1_ID
        for t in cfg.retrain_weeks:
            c = candidate_id(pol, t)
            out.append((c, prev, t))
            prev = c
    return out


def known_by(cfg: BacktestConfig, t: int) -> Callable[[tuple[str, str, int]], bool]:
    """A tuning pair is usable at decision week t only if its 4 outcome weeks are labelled."""
    return lambda pr: pr[2] + cfg.harm_weeks <= cfg.labelled_upto(t)


# ---------------------------------------------------------------------------- step 7


def training_window(ctx: Ctx) -> pd.DataFrame:
    """Each candidate's recall@0.1%FPR on the 4 weeks after T, and its paired difference
    against the last_n_weeks candidate of the same T (family-clustered CI)."""
    cfg = ctx.cfg
    rows = []
    for t in cfg.retrain_weeks:
        weeks = [w for w in range(t + 1, t + cfg.harm_weeks + 1) if w <= cfg.test_weeks.last]
        if not weeks:
            continue
        idx = ctx.rows(weeks)
        ref = "last_n_weeks" if "last_n_weeks" in cfg.retrain_policies else None
        base = candidate_id(ref, t) if ref else None
        for pol in cfg.retrain_policies:
            c = candidate_id(pol, t)
            r = recall_on(ctx, idx, c)
            j = judge(ctx, c, base, t + 1) if base and pol != ref else None
            v = judge(ctx, c, V1_ID, t + 1)
            rows.append(
                {
                    "as_of_week": t,
                    "retrain_policy": pol,
                    "candidate": c,
                    "weeks": f"{weeks[0]}-{weeks[-1]}",
                    "recall": r,
                    "vs_last_n_points": j.delta_points if j else None,
                    "vs_last_n_ci_low": j.ci_low if j else None,
                    "vs_last_n_ci_high": j.ci_high if j else None,
                    "vs_v1_points": v.delta_points,
                    "vs_v1_ci_low": v.ci_low,
                    "vs_v1_ci_high": v.ci_high,
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------- step 8


def drift_alert(ctx: Ctx, model: str, as_of: int, d: DriftConfig) -> tuple[bool, float, int]:
    """Stage 7's drift rule on weeks <= as_of only, no labels: PSI of v1's top features and of
    the live model's scores, recent window vs the baseline just before it."""
    st = ctx.st
    recent = list(range(as_of - d.recent_weeks + 1, as_of + 1))
    base = list(range(recent[0] - d.baseline_weeks, recent[0]))
    n_alert = 0
    if st.histograms is not None:
        counts = st.histograms["counts"]
        for f in st.histograms["features"]:
            if not all(str(w) in counts[f] for w in base + recent):
                continue
            b = np.sum([counts[f][str(w)] for w in base], axis=0)
            r = np.sum([counts[f][str(w)] for w in recent], axis=0)
            n_alert += population_stability_index(b, r) > d.feature_psi_alert
    raw = st.raw[model]
    b_s, r_s = raw[np.isin(st.week, base)], raw[np.isin(st.week, recent)]
    if b_s.size == 0 or r_s.size == 0:
        return False, float("nan"), n_alert
    edges = sorted(set(np.quantile(b_s, np.linspace(0, 1, d.bins + 1)[1:-1]).tolist()))
    spsi = population_stability_index(score_bin_counts(b_s, edges), score_bin_counts(r_s, edges))
    return (n_alert >= d.min_features_alerting or spsi > d.score_psi_alert), spsi, n_alert


def drift_recalibration(ctx: Ctx, d: DriftConfig) -> pd.DataFrame:
    """Step 8: never blocks a release. On two fixed serving schedules (v1 kept live; P1's
    every-retrain schedule) compare cut-off rules: none, weekly capped, drift-triggered jump
    only, weekly capped + jump on alert. Alerts at the end of week w-1 set week w's cut-off."""
    cfg, st = ctx.cfg, ctx.st
    cap = cfg.recalibration.max_fpr_move_pp / 100
    jump = cfg.drift_recalibration.on_alert_max_fpr_move_pp / 100
    rows = []
    schedules: dict[str, dict[int, str]] = {"v1 kept live": {}}
    for pol in cfg.retrain_policies:
        schedules[f"P1 {pol}"] = {}
    for w in cfg.test_weeks.weeks:
        schedules["v1 kept live"][w] = V1_ID
        for pol in cfg.retrain_policies:
            ts = [t for t in cfg.retrain_weeks if t < w]
            schedules[f"P1 {pol}"][w] = candidate_id(pol, max(ts)) if ts else V1_ID
    alerts: dict[tuple[str, int], tuple[bool, float, int]] = {}
    for sched, serving in schedules.items():
        for rule in ("none", "weekly capped", "drift jump only", "weekly capped + drift jump"):
            cutoff: dict[str, float] = {}
            for w in cfg.test_weeks.weeks:
                m = serving[w]
                cutoff.setdefault(m, st.models[m].threshold)
                key = (m, w - 1)
                if key not in alerts:
                    alerts[key] = drift_alert(ctx, m, w - 1, d)
                alert = alerts[key][0]
                move = 0.0
                if rule in ("weekly capped", "weekly capped + drift jump"):
                    move = cap
                if alert and rule in ("drift jump only", "weekly capped + drift jump"):
                    move = jump
                if move > 0:
                    cutoff[m] = capped_threshold(
                        clean_pool(ctx, m, w - 1), cutoff[m], cfg.operating_fpr, move
                    )
                idx = ctx.rows([w])
                s = rates_at_threshold(st.y[idx], st.raw[m][idx], cutoff[m])
                rows.append(
                    {
                        "schedule": sched,
                        "rule": rule,
                        "week": w,
                        "model": m,
                        "alert": alert,
                        "score_psi": alerts[key][1],
                        "features_alerting": alerts[key][2],
                        "shipped_fpr": s.fpr,
                        "shipped_recall": s.recall,
                    }
                )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------- step 5


def priority_mcnemar(ctx: Ctx, d: dict[str, Any]) -> dict[str, Any]:
    """Step 5: disagreement files of the first live week (matched cut-offs) are labelled first
    (assumed within `priority_label_weeks`); exact McNemar on them. Reported, never decides."""
    st = ctx.st
    w = int(d["as_of_week"]) + 1
    if w not in ctx.rows_by_week or d.get("cutoff_candidate") is None:
        return {"priority_b": None, "priority_c": None, "priority_p": None}
    idx = ctx.rows([w])
    y = st.y[idx]
    fc = st.raw[d["candidate"]][idx] > d["cutoff_candidate"]
    fp = st.raw[d["production"]][idx] > d["cutoff_production"]
    b = int(((fp == y.astype(bool)) & (fc != y.astype(bool))).sum())
    c = int(((fc == y.astype(bool)) & (fp != y.astype(bool))).sum())
    return {"priority_b": b, "priority_c": c, "priority_p": mcnemar_exact(b, c)}


# ---------------------------------------------------------------------------- differences


def policy_difference(
    ctx: Ctx, a: pd.DataFrame, b: pd.DataFrame, metric: str
) -> tuple[float, float, float]:
    """Mean weekly `metric` of policy a minus policy b (both: weekly rows of the three
    retraining chains), same resampled files for both (family-clustered, per week)."""
    cfg, st = ctx.cfg, ctx.st

    def value(rows: pd.DataFrame, sub_by_week: dict[int, I64]) -> float:
        vals = []
        for r in rows.itertuples():
            sub = sub_by_week[int(cast(Any, r.week))]
            if metric == "recall_at_fpr":
                v = recall_on(ctx, sub, str(r.model))
            else:
                v = rates_at_threshold(
                    st.y[sub], st.raw[str(r.model)][sub], float(cast(Any, r.cutoff))
                ).fpr
            if v is not None:
                vals.append(v)
        return float(np.mean(vals))

    weeks = sorted(set(a["week"]))
    full = {w: ctx.rows_by_week[w] for w in weeks}
    point = 100 * (value(a, full) - value(b, full))
    rng = np.random.default_rng(
        [cfg.seed, _seed("diff", metric, *a["release_policy"][:1], *b["release_policy"][:1])]
    )
    diffs = []
    for _ in range(cfg.harm.bootstrap_resamples):
        sub = {
            w: idx[cluster_bootstrap_rows(ctx.clusters[idx], st.y[idx], rng)]
            for w, idx in full.items()
        }
        diffs.append(100 * (value(a, sub) - value(b, sub)))
    lo, hi = percentile_interval(diffs, cfg.harm.confidence)
    return point, lo, hi


# ---------------------------------------------------------------------------- the run


def run_pass2(
    st: ScoreTable,
    meta: pd.DataFrame | None,
    cfg: BacktestConfig,
    gate: GatePolicy,
    noise_margin: float,
    drift: DriftConfig,
    rejected: list[tuple[str, str]],
) -> dict[str, Any]:
    """Every P4 variant, the blend policy and the extras. `noise_margin` is Pass 1's M;
    `rejected` = (candidate, production) pairs the Stage 7 gate rejected."""
    blend_ids: dict[str, list[str]] = {}
    for pol in cfg.retrain_policies:  # step 9's blended models join the table up front
        st, blend_ids[pol] = _blend_table(st, cfg, pol)
    ctx = Ctx(st, cfg, gate, cluster_keys(st, meta), "family" if meta is not None else "file")
    sl = build_slices(st, meta)
    pre = preregistered(cfg, noise_margin)
    cache = SeqCache()
    pairs = predecessor_pairs(cfg)

    # step 6: walk-forward (point-in-time) and the in-sample optimum (labelled as such)
    wf_params = {
        t: tune(ctx, sl, [pr for pr in pairs if known_by(cfg, t)(pr)], pre)
        for t in cfg.retrain_weeks
    }
    in_sample = tune(ctx, sl, pairs, pre)
    variants: list[tuple[str, Callable[[int], Params], str, float]] = [
        ("P4_clean", lambda t: pre, "clean", cfg.p4.early_flip_rate),
        ("P4_traffic", lambda t: pre, "traffic", cfg.p4.early_flip_rate),
        ("P4_walk_forward", lambda t: wf_params.get(t, pre), "clean", cfg.p4.early_flip_rate),
        ("P4_IN_SAMPLE", lambda t: in_sample, "clean", cfg.p4.early_flip_rate),
    ]
    for rate in cfg.early_labels.flip_rates:
        if rate != cfg.p4.early_flip_rate:
            variants.append((f"P4_clean_flip{int(100 * rate)}", lambda t: pre, "clean", rate))
    decisions: list[dict[str, Any]] = []
    weekly: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for label, pf, method, rate in variants:
        for pol in cfg.retrain_policies:
            d, w, e = run_p4_chain(ctx, sl, pol, label, pf, method, rate, cache, noise_margin)
            decisions += d
            weekly += w
            events += e

    # step 9: blend
    for pol in cfg.retrain_policies:
        d, w = run_blend_chain(ctx, pol, blend_ids[pol], noise_margin)
        decisions += d
        weekly += w
    dec, wk, ev = pd.DataFrame(decisions), pd.DataFrame(weekly), pd.DataFrame(events)

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

    # 7 previously rejected candidates: P4 day-0 with both matching methods
    redo = []
    for cand, prod in rejected:
        t = st.models[cand].as_of_week
        j = judge(ctx, cand, prod, t + 1)
        row: dict[str, Any] = {"candidate": cand, "production": prod}
        for method in ("clean", "traffic"):
            r = day0(ctx, sl, cand, prod, t, pre, method)
            row[f"passes_{method}"] = bool(r["promote"])
            row[f"blocked_by_{method}"] = r["blocked_by"]
        row.update(
            delta_points=j.delta_points,
            ci_low=j.ci_low,
            ci_high=j.ci_high,
            outcome=outcome(j, noise_margin),
        )
        redo.append(row)

    # step 5: day-0 McNemar (labelled slice) + priority-labelled first live week
    mc = []
    for pol in cfg.retrain_policies:
        prev = V1_ID
        for t in cfg.retrain_weeks:
            cand = candidate_id(pol, t)
            r = day0(ctx, sl, cand, prev, t, pre, "clean")
            b, c = r.get("disagree_b"), r.get("disagree_c")
            mc.append(
                {
                    "candidate": cand,
                    "production": prev,
                    "as_of_week": t,
                    "day0_b": b,
                    "day0_c": c,
                    "day0_p": mcnemar_exact(b, c) if b is not None and c is not None else None,
                    "day0_checks": r["blocked_by"] or "all pass",
                    **priority_mcnemar(ctx, r),
                }
            )
            prev = cand

    tw = training_window(ctx)
    dr = drift_recalibration(ctx, drift)
    return {
        "decisions": dec,
        "weekly": wk,
        "events": ev,
        "mean_recall_ci": pd.DataFrame(cis),
        "rejected_redo": pd.DataFrame(redo),
        "mcnemar": pd.DataFrame(mc),
        "training_window": tw,
        "drift": dr,
        "params": {
            "preregistered": asdict(pre),
            "in_sample": asdict(in_sample),
            "walk_forward": {t: asdict(p) for t, p in wf_params.items()},
        },
        "ctx": ctx,
    }
