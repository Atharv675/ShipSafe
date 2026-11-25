"""Data for the Streamlit ML pages (Stage 9).

Three read-only sources, as the build plan allows:
- PostgreSQL (role `sentinel_dashboard`) through the named-query `Source` of data.py;
- the MLflow model registry (`Registry`);
- $SENTINEL_DATA_DIR/results (`Results`): experiment/backtest outputs and the files
  `make ml-results` writes (per-file scores + labels, feature PSI).
Transforms are pure functions on DataFrames; statistics come from sentinel.models.evaluate.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np
import pandas as pd

from sentinel.models.evaluate import (
    auc,
    pr_points,
    rates_at_threshold,
    recall_at_fpr,
    reliability_curve,
    roc_points,
    score_bin_counts,
)

# ---------------------------------------------------------------------------- sources


class Registry(Protocol):
    def versions(self) -> pd.DataFrame: ...


REGISTRY_COLUMNS = [
    "name",
    "version",
    "aliases",
    "created",
    "kind",
    "policy",
    "as_of_week",
    "fit_weeks",
    "calibration_weeks",
    "data_fingerprint",
    "git_commit",
    "demo",
    "run_id",
]


class MlflowRegistry:  # pragma: no cover - needs the MLflow server (exercised on the stack)
    """Every version of every registered model, with aliases, tags and run windows."""

    def __init__(self, uri: str) -> None:
        self._uri = uri

    def versions(self) -> pd.DataFrame:
        from mlflow.tracking import MlflowClient

        c = MlflowClient(tracking_uri=self._uri, registry_uri=self._uri)
        rows = []
        for rm in c.search_registered_models():
            # search_model_versions does not fill `aliases`: read them from the model itself
            alias_map = dict(getattr(c.get_registered_model(rm.name), "aliases", {}) or {})
            by_version: dict[str, list[str]] = {}
            for alias, ver in alias_map.items():
                by_version.setdefault(str(ver), []).append(str(alias))
            for v in c.search_model_versions(f"name='{rm.name}'"):
                tags = dict(v.tags or {})
                params: dict[str, str] = {}
                if v.run_id:
                    try:
                        params = dict(c.get_run(v.run_id).data.params)
                    except Exception:  # a deleted run must not break the page
                        params = {}
                rows.append(
                    {
                        "name": rm.name,
                        "version": str(v.version),
                        "aliases": ", ".join(sorted(by_version.get(str(v.version), []))),
                        "created": pd.to_datetime(v.creation_timestamp, unit="ms", utc=True),
                        "kind": tags.get("kind", "baseline" if str(v.version) == "1" else ""),
                        "policy": tags.get("policy", params.get("policy", "")),
                        # v1's run has `as_of` as a timestamp; its week is the eval week
                        "as_of_week": tags.get("as_of_week", params.get("eval_week", "")),
                        "fit_weeks": params.get("fit_weeks", tags.get("fit_weeks", "")),
                        "calibration_weeks": params.get(
                            "calibration_weeks", tags.get("calibration_weeks", "")
                        ),
                        "data_fingerprint": tags.get("data_fingerprint", ""),
                        "git_commit": tags.get("git_commit", ""),
                        "demo": tags.get("demo", ""),
                        "run_id": v.run_id or "",
                    }
                )
        return pd.DataFrame(rows, columns=REGISTRY_COLUMNS)


class Results(Protocol):
    def table(self, name: str) -> pd.DataFrame | None: ...
    def document(self, name: str) -> dict[str, Any] | None: ...


class ResultsDir:
    """Parquet / CSV / JSON files in the results folder (None when a file is missing)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def table(self, name: str) -> pd.DataFrame | None:
        for ext, read in ((".parquet", pd.read_parquet), (".csv", pd.read_csv)):
            p = self.path / f"{name}{ext}"
            if p.exists():
                return read(p)
        return None

    def document(self, name: str) -> dict[str, Any] | None:
        p = self.path / f"{name}.json"
        if not p.exists():
            return None
        doc: dict[str, Any] = json.loads(p.read_text("utf-8"))
        return doc


# ---------------------------------------------------------------------------- SQL

# shared sidebar filters: NULL = all
FILTERS = """
  AND (%(model)s::text IS NULL OR model_version = %(model)s::text)
  AND (%(ring)s::text IS NULL OR ring = %(ring)s::text)
  AND (%(tenant)s::text IS NULL OR tenant_id = %(tenant)s::text)
"""

FILTER_VALUES_SQL = """
SELECT 'model' AS kind, model_version AS value, count(*) AS n FROM predictions
 WHERE ts >= %(start)s AND ts < %(end)s GROUP BY model_version
UNION ALL
SELECT 'ring', ring, count(*) FROM predictions
 WHERE ts >= %(start)s AND ts < %(end)s AND ring IS NOT NULL GROUP BY ring
UNION ALL
(SELECT 'tenant', tenant_id, count(*) FROM predictions
  WHERE ts >= %(start)s AND ts < %(end)s AND tenant_id NOT LIKE %(canary)s
  GROUP BY tenant_id ORDER BY count(*) DESC LIMIT %(top)s)
"""

FLAG_HOURLY_SQL = (
    """
SELECT date_bin('1 hour', ts, TIMESTAMPTZ 'epoch') AS hour, ring, model_version,
       count(*) AS events, count(*) FILTER (WHERE score > threshold) AS model_flags
FROM predictions
WHERE ts >= %(start)s AND ts < %(end)s AND tenant_id NOT LIKE %(canary)s
"""
    + FILTERS
    + "GROUP BY 1, 2, 3 ORDER BY 1"
)

GATES_SQL = """
SELECT decision_id, created_at, file_type, policy_version, production_version,
       candidate_version, decision, reason
FROM gate_decisions ORDER BY created_at DESC
"""

GATE_BUNDLE_SQL = """
SELECT decision_id, bundle_sha256, bundle_json FROM gate_decisions
WHERE decision_id = %(decision_id)s
"""

TRANSITIONS_SQL = """
SELECT id, ts, file_type, rollout_id, seq, model_name, candidate_version, production_version,
       from_state, to_state, actor, reason, signals
FROM rollout_transitions ORDER BY id
"""

RING_FLAG_SQL = """
SELECT date_bin('1 minute', ts, TIMESTAMPTZ 'epoch') AS minute, ring, model_version,
       count(*) AS events, count(*) FILTER (WHERE score > threshold) AS model_flags
FROM predictions
WHERE ts >= %(start)s AND ts < %(end)s AND tenant_id NOT LIKE %(canary)s
  AND (%(tenant)s::text IS NULL OR tenant_id = %(tenant)s::text)
GROUP BY 1, 2, 3 ORDER BY 1
"""

LOOKUP_SQL = """
SELECT request_id, file_id, tenant_id, ring, model_version, feature_schema_version,
       threshold, score, verdict, override_applied, reference_version, reference_score,
       reference_verdict, trace_id, ts
FROM predictions
WHERE file_id = %(q)s OR request_id = %(q)s
ORDER BY ts DESC LIMIT %(limit)s
"""

SHADOW_LOOKUP_SQL = """
SELECT event_id, model_version, file_id, tenant_id, ring, week, threshold, score, verdict, ts
FROM shadow_predictions WHERE file_id = %(q)s ORDER BY ts DESC LIMIT %(limit)s
"""


# ---------------------------------------------------------------------------- filters


@dataclass(frozen=True)
class Filters:
    model: str | None = None
    ring: str | None = None
    tenant: str | None = None

    def params(self) -> dict[str, Any]:
        return {"model": self.model, "ring": self.ring, "tenant": self.tenant}

    def describe(self) -> str:
        parts = [f"{k} = {v}" for k, v in self.params().items() if v]
        return ", ".join(parts) if parts else "all traffic"


def filter_options(values: pd.DataFrame) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"model": [], "ring": [], "tenant": []}
    for kind, g in values.groupby("kind") if len(values) else []:
        out[str(kind)] = sorted(str(v) for v in g["value"].dropna())
    return out


# ---------------------------------------------------------------------------- overview + registry


def ring_serving(
    versions: pd.DataFrame, ring_aliases: Mapping[str, str], fallback: str
) -> pd.DataFrame:
    """Which registered version each ring serves, per model name (alias, else fallback)."""
    rows = []
    for name, g in versions.groupby("name") if len(versions) else []:
        by_alias: dict[str, str] = {}
        for v, al in zip(g["version"], g["aliases"], strict=True):
            for a in str(al).split(", "):
                if a:
                    by_alias[a] = str(v)
        for ring, alias in ring_aliases.items():
            v = by_alias.get(alias, by_alias.get(fallback))
            rows.append(
                {
                    "model": name,
                    "ring": ring,
                    "version": f"{name}/{v}" if v else "none",
                    "via": alias if alias in by_alias else f"{fallback} (fallback)",
                }
            )
    return pd.DataFrame(rows, columns=["model", "ring", "version", "via"])


def registry_table(versions: pd.DataFrame, gates: pd.DataFrame, mlflow_ui: str) -> pd.DataFrame:
    """Registry versions + the latest gate decision on each (as candidate) + an MLflow link."""
    t = versions.copy()
    t["label"] = t["name"] + "/" + t["version"].astype(str)
    last = (
        gates.sort_values("created_at").groupby("candidate_version").tail(1)
        if len(gates)
        else pd.DataFrame(columns=["candidate_version", "decision", "decision_id"])
    )
    t = t.merge(
        last[["candidate_version", "decision", "decision_id"]].rename(
            columns={"candidate_version": "label", "decision": "gate_outcome"}
        ),
        on="label",
        how="left",
    )
    t["gate_outcome"] = t["gate_outcome"].fillna("not gated")
    t["mlflow"] = [
        f"{mlflow_ui.rstrip('/')}/#/models/{n}/versions/{v}"
        for n, v in zip(t["name"], t["version"], strict=True)
    ]
    t["fingerprint"] = t["data_fingerprint"].astype(str).str[:12]
    return t


def hourly_flag_rate(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["hour", "ring", "events", "model_flags", "flag_rate"])
    g = df.groupby(["hour", "ring"], as_index=False, dropna=False)[["events", "model_flags"]].sum()
    g["ring"] = g["ring"].fillna("none")
    g["flag_rate"] = g["model_flags"] / g["events"]
    return g


# ---------------------------------------------------------------------------- gate viewer


def verify_bundle(sha256: str, bundle_json: str) -> bool:
    """The stored hash must be the SHA-256 of the stored bundle text (tamper check)."""
    return hashlib.sha256(bundle_json.encode("utf-8")).hexdigest() == sha256


def gate_checks(bundle: Mapping[str, Any]) -> pd.DataFrame:
    rows = [
        {
            "check": c["name"],
            "status": c["status"],
            "hard": bool(c.get("hard", True)),
            "summary": c.get("summary", ""),
        }
        for c in bundle.get("checks", [])
    ]
    return pd.DataFrame(rows, columns=["check", "status", "hard", "summary"])


def _check(bundle: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    return next((c for c in bundle.get("checks", []) if c["name"] == name), None)


def interval_bars(bundle: Mapping[str, Any]) -> pd.DataFrame:
    """Recall at the operating FPR (check a) and FPR at each SHIPPED cut-off (check a'),
    with the Wilson intervals stored in the bundle, for both models."""
    rows = []
    for check, metric in (("recall_at_fpr", "recall"), ("false_alarms", "fpr")):
        c = _check(bundle, check)
        if not c:
            continue
        for model in ("production", "candidate"):
            m = c["numbers"].get(model)
            if not m:
                continue
            lo, hi = m[f"{metric}_ci"]
            label = (
                "recall at the operating FPR"
                if metric == "recall"
                else "FPR at the shipped cut-off"
            )
            rows.append(
                {"measure": label, "model": model, "value": m[metric], "low": lo, "high": hi}
            )
    return pd.DataFrame(rows, columns=["measure", "model", "value", "low", "high"])


def mcnemar_tables(bundle: Mapping[str, Any]) -> dict[str, pd.DataFrame]:
    """2x2 tables per class from the disagreement check (rows production, columns candidate)."""
    c = _check(bundle, "disagreement")
    out: dict[str, pd.DataFrame] = {}
    if not c:
        return out
    for cls in ("malicious", "benign"):
        n = c["numbers"].get(cls)
        if not n:
            continue
        both_r, both_w = n["both_right"], n["both_wrong"]
        p_only, c_only = n["only_production_right"], n["only_candidate_right"]
        out[cls] = pd.DataFrame(
            {
                "candidate right": [both_r, c_only],
                "candidate wrong": [p_only, both_w],
            },
            index=["production right", "production wrong"],
        )
        out[cls].attrs["mcnemar_p"] = n.get("mcnemar_p")
    return out


def flipped_files(bundle: Mapping[str, Any], limit: int) -> pd.DataFrame:
    c = _check(bundle, "disagreement")
    files = c["numbers"].get("flipped_files", []) if c else []
    cols = [
        "file_id",
        "week",
        "label",
        "production_score",
        "candidate_score",
        "candidate_verdict",
    ]
    return pd.DataFrame(files[:limit], columns=cols)


def weekly_regression(bundle: Mapping[str, Any]) -> pd.DataFrame:
    c = _check(bundle, "weekly_regression")
    rows = []
    for w in c["numbers"].get("weeks", []) if c else []:
        for model in ("production", "candidate"):
            rows.append({"week": w["week"], "model": model, "recall": w[f"{model}_recall"]})
    return pd.DataFrame(rows, columns=["week", "model", "recall"])


# ---------------------------------------------------------------------------- head-to-head


def model_ids(scores: pd.DataFrame) -> list[str]:
    return sorted(c.removeprefix("raw_") for c in scores.columns if c.startswith("raw_"))


def common_weeks(models: Mapping[str, Any], a: str, b: str, weeks: list[int]) -> list[int]:
    """Weeks after BOTH models' as-of week (neither learned from them)."""
    t = max(int(models[a]["as_of_week"]), int(models[b]["as_of_week"]))
    return [w for w in weeks if w > t]


@dataclass(frozen=True)
class HeadToHead:
    weeks: list[int]
    summary: pd.DataFrame
    roc: pd.DataFrame
    pr: pd.DataFrame
    weekly: pd.DataFrame
    calibration: pd.DataFrame


def head_to_head(
    scores: pd.DataFrame,
    models: Mapping[str, Any],
    a: str,
    b: str,
    operating_fpr: float,
    max_fpr: float,
    bins: int,
) -> HeadToHead:
    weeks = common_weeks(models, a, b, sorted(int(w) for w in scores["week"].unique()))
    s = scores[scores["week"].isin(weeks)]
    y = s["y"].to_numpy()
    summ, roc, pr, wk, cal = [], [], [], [], []
    for m in dict.fromkeys((a, b)):
        raw = s[f"raw_{m}"].to_numpy()
        prob = s[f"prob_{m}"].to_numpy(dtype=np.float64)
        if len(np.unique(y)) < 2:
            continue
        r = recall_at_fpr(y, raw, operating_fpr)
        shipped = rates_at_threshold(y, raw, float(models[m]["threshold"]))
        summ.append(
            {
                "model": m,
                "auc": auc(y, raw),
                "recall_at_fpr": r.recall,
                "recall_low": r.recall_ci[0],
                "recall_high": r.recall_ci[1],
                "shipped_fpr": shipped.fpr,
                "shipped_recall": shipped.recall,
                "files": int(y.size),
            }
        )
        c = roc_points(y, raw, max_fpr)
        roc.append(pd.DataFrame({"model": m, "fpr": c.x, "recall": c.y}))
        p = pr_points(y, raw)
        pr.append(pd.DataFrame({"model": m, "recall": p.x, "precision": p.y}))
        for b_ in reliability_curve(y, prob, bins):
            cal.append(
                {
                    "model": m,
                    "predicted": b_.mean_predicted,
                    "observed": b_.fraction_malicious,
                    "files": b_.count,
                }
            )
        for w in weeks:
            mw = s["week"].to_numpy() == w
            yw, rw = y[mw], raw[mw]
            if (yw == 0).sum() * operating_fpr < 1 or (yw == 1).sum() == 0:
                continue
            rr = recall_at_fpr(yw, rw, operating_fpr)
            sh = rates_at_threshold(yw, rw, float(models[m]["threshold"]))
            wk.append({"model": m, "week": w, "recall_at_fpr": rr.recall, "shipped_fpr": sh.fpr})
    return HeadToHead(
        weeks,
        pd.DataFrame(summ),
        pd.concat(roc, ignore_index=True)
        if roc
        else pd.DataFrame(columns=["model", "fpr", "recall"]),
        pd.concat(pr, ignore_index=True)
        if pr
        else pd.DataFrame(columns=["model", "recall", "precision"]),
        pd.DataFrame(wk, columns=["model", "week", "recall_at_fpr", "shipped_fpr"]),
        pd.DataFrame(cal, columns=["model", "predicted", "observed", "files"]),
    )


# ---------------------------------------------------------------------------- drift


def log_odds(raw: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    p = np.clip(raw.astype(np.float64), 1e-12, 1 - 1e-12)
    out: np.ndarray[Any, Any] = np.log(p / (1 - p))
    return out


def score_distribution(scores: pd.DataFrame, model: str, bins: int) -> pd.DataFrame:
    """Per week, share of files in each log-odds bin of `model`'s raw score (fixed edges over
    all weeks, so weeks are comparable)."""
    lo = log_odds(scores[f"raw_{model}"].to_numpy())
    edges = np.linspace(np.floor(lo.min()), np.ceil(lo.max()), bins + 1)[1:-1]
    rows = []
    for w, idx in scores.groupby("week").indices.items():
        counts = score_bin_counts(lo[idx], edges)
        centers = np.r_[edges[0] - 0.5, (edges[:-1] + edges[1:]) / 2, edges[-1] + 0.5]
        for c, n in zip(centers, counts, strict=True):
            rows.append(
                {"week": int(cast(Any, w)), "log_odds": float(c), "share": n / max(1, len(idx))}
            )
    return pd.DataFrame(rows, columns=["week", "log_odds", "share"])


def drift_alert_weeks(drift: pd.DataFrame | None) -> list[int]:
    """Weeks the Stage 7 drift rule alerted on the live model (v1 kept live, Pass 2 step 8)."""
    if drift is None or drift.empty:
        return []
    d = drift[(drift["schedule"] == "v1 kept live") & (drift["rule"] == "none")]
    return sorted(int(w) for w in d.loc[d["alert"].astype(bool), "week"])


# ---------------------------------------------------------------------------- rollouts


def rollout_summary(tr: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "rollout_id",
        "file_type",
        "candidate_version",
        "started",
        "ended",
        "final_state",
        "rolled_back",
        "reason",
    ]
    if tr.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for rid, g in tr.sort_values("id").groupby("rollout_id", sort=False):
        last = g.iloc[-1]
        rows.append(
            {
                "rollout_id": rid,
                "file_type": last["file_type"],
                "candidate_version": f"{last['model_name']}/{last['candidate_version']}",
                "started": g["ts"].min(),
                "ended": last["ts"],
                "final_state": last["to_state"],
                "rolled_back": last["to_state"] == "ROLLED_BACK",
                "reason": last["reason"],
            }
        )
    return pd.DataFrame(rows, columns=cols).sort_values("started", ascending=False)


def state_spans(tr: pd.DataFrame, rollout_id: str, end: Any) -> pd.DataFrame:
    """One bar per state the rollout was in: [entered, next transition)."""
    g = tr[tr["rollout_id"] == rollout_id].sort_values("seq")
    if g.empty:
        return pd.DataFrame(columns=["state", "start", "end", "reason"])
    ends = [
        *g["ts"].iloc[1:],
        end if g["to_state"].iloc[-1] not in TERMINAL else g["ts"].iloc[-1],
    ]
    return pd.DataFrame(
        {
            "state": g["to_state"].to_numpy(),
            "start": g["ts"].to_numpy(),
            "end": ends,
            "reason": g["reason"].to_numpy(),
        }
    )


TERMINAL = {"COMPLETE", "ROLLED_BACK"}


def minute_flag_rate(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["minute", "ring", "model_version", "events", "flag_rate"])
    out = df.copy()
    out["ring"] = out["ring"].fillna("none")
    out["flag_rate"] = out["model_flags"] / out["events"]
    return out


# ---------------------------------------------------------------------------- experiment


POLICY_NAMES = {
    "P1_auto": "P1 auto-promote",
    "P2_val_auc": "P2 validation AUC",
    "P3_gate": "P3 Sentinel Gate",
}


def experiment_intervals(summary: pd.DataFrame | None) -> pd.DataFrame:
    """Stage 7 (`experiment_summary.csv`): per release policy, the real-stream value and the
    bootstrap range of harmful promotions, good updates blocked and mean recall."""
    if summary is None or summary.empty:
        return pd.DataFrame()
    rows = []
    for r in summary.itertuples():
        for measure, col in (
            ("harmful promotions", "harmful_promotions"),
            ("good updates blocked", "good_blocked"),
            ("promotions", "promotions"),
        ):
            rows.append(
                {
                    "policy": POLICY_NAMES.get(str(r.release_policy), str(r.release_policy)),
                    "measure": measure,
                    "value": float(getattr(r, col)),
                    "low": float(getattr(r, f"{col}_lo")),
                    "high": float(getattr(r, f"{col}_hi")),
                }
            )
    return pd.DataFrame(rows)


def recall_over_time(weekly: pd.DataFrame | None) -> pd.DataFrame:
    """Real stream (resample 0): mean recall@FPR per release policy and week."""
    if weekly is None or weekly.empty:
        return pd.DataFrame(columns=["policy", "week", "recall_at_fpr", "shipped_fpr"])
    w = weekly[weekly["resample"] == 0]
    g = w.groupby(["release_policy", "week"], as_index=False)[
        ["recall_at_fpr", "shipped_fpr"]
    ].mean()
    g["policy"] = g["release_policy"].map(lambda p: POLICY_NAMES.get(str(p), str(p)))
    return g[["policy", "week", "recall_at_fpr", "shipped_fpr"]]


@dataclass(frozen=True)
class DriftPrecision:
    alerts: int
    alerts_on_harmful: int
    harmful: int
    candidates: int


def drift_precision(exp: pd.DataFrame | None) -> DriftPrecision | None:
    """Stage 7: drift alerts on P1 candidates (real stream) against harmful outcomes."""
    if exp is None or exp.empty:
        return None
    p = exp[(exp["resample"] == 0) & (exp["release_policy"] == "P1_auto")]
    alert = p["drift_alert"].fillna(False).astype(bool)
    harm = p["harmful"].fillna(False).astype(bool)
    return DriftPrecision(int(alert.sum()), int((alert & harm).sum()), int(harm.sum()), len(p))


# ---------------------------------------------------------------------------- redesign (2026-11-25)

_TAG = re.compile(r"\[([a-z_]+)\]")


def latest_gate(gates: pd.DataFrame, model_name: str) -> dict[str, Any] | None:
    """The newest gate decision whose candidate is a version of `model_name`."""
    if gates.empty:
        return None
    g = gates[gates["candidate_version"].astype(str).str.startswith(f"{model_name}/")]
    if g.empty:
        return None
    row: dict[str, Any] = {str(k): v for k, v in g.sort_values("created_at").iloc[-1].items()}
    return row


def bundle_rates(bundle: Mapping[str, Any]) -> dict[str, dict[str, float | None]]:
    """Per model (production / candidate): recall at the operating FPR, FPR at the shipped
    cut-off, never-break files flagged, and the gate's paired coverage."""
    out: dict[str, dict[str, float | None]] = {}
    rec, fa = _check(bundle, "recall_at_fpr"), _check(bundle, "false_alarms")
    nb, cov = _check(bundle, "never_break"), _check(bundle, "coverage")
    for model in ("production", "candidate"):
        out[model] = {
            "recall": (rec or {}).get("numbers", {}).get(model, {}).get("recall"),
            "fpr": (fa or {}).get("numbers", {}).get(model, {}).get("fpr"),
            "never_break": (nb or {}).get("numbers", {}).get(f"{model}_flags"),
            "never_break_files": (nb or {}).get("numbers", {}).get("never_break_files"),
            "coverage": (cov or {}).get("numbers", {}).get("coverage"),
        }
    return out


def blocking_checks(gates: pd.DataFrame) -> pd.DataFrame:
    """How often each check is named in a REJECT / INSUFFICIENT_EVIDENCE reason."""
    counts: dict[str, int] = {}
    for d, reason in zip(gates.get("decision", []), gates.get("reason", []), strict=False):
        if d == "PROMOTE":
            continue
        for tag in set(_TAG.findall(str(reason).split(" Note (does not block)")[0])):
            counts[tag] = counts.get(tag, 0) + 1
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return pd.DataFrame(rows, columns=["check", "decisions"])


def decision_counts(gates: pd.DataFrame) -> pd.DataFrame:
    if gates.empty:
        return pd.DataFrame(columns=["decision", "n"])
    g = gates.groupby("decision", as_index=False).size().rename(columns={"size": "n"})
    return g[["decision", "n"]]


def rollback_reasons(summary: pd.DataFrame) -> pd.DataFrame:
    """Rolled-back rollouts by the signal that stopped them ([never_break], [latency], ...;
    an operator kill switch has no tag)."""
    rb = summary[summary["rolled_back"]] if len(summary) else summary
    counts: dict[str, int] = {}
    for reason in rb.get("reason", []):
        tags = _TAG.findall(str(reason))
        key = tags[0] if tags else ("kill_switch" if "kill" in str(reason).lower() else "other")
        counts[key] = counts.get(key, 0) + 1
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return pd.DataFrame(rows, columns=["signal", "rollouts"])


def outcome_counts(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame(columns=["outcome", "rollouts"])
    lab = summary["final_state"].map(
        lambda s: {"COMPLETE": "complete", "ROLLED_BACK": "rolled back"}.get(str(s), "in progress")
    )
    g = lab.value_counts().rename_axis("outcome").reset_index(name="rollouts")
    return g


def policy_comparison(summary: pd.DataFrame | None) -> pd.DataFrame:
    """Stage 7b summary in long form for grouped bars: one row per (policy label,
    recalibration, measure)."""
    cols = ["policy", "recalibration", "measure", "value"]
    if summary is None or summary.empty:
        return pd.DataFrame(columns=cols)
    s = summary.copy()
    s["policy"] = s["release_policy"].map(lambda p: POLICY_SHORT.get(str(p), str(p)))
    s["recalibration"] = s["recalibration"].map(lambda r: "recalibration on" if r else "off")
    keep = [c for c in ("promotions", "harmful", "better_blocked", "rollbacks") if c in s]
    long = s.melt(
        id_vars=["policy", "recalibration"], value_vars=keep, var_name="measure", value_name="value"
    )
    long["measure"] = long["measure"].map(
        {
            "promotions": "promotions",
            "harmful": "harmful (> M)",
            "better_blocked": "better blocked",
            "rollbacks": "rollbacks",
        }
    )
    return long[cols]


POLICY_SHORT = {
    "P1_auto": "P1 auto",
    "P2_val_auc": "P2 val. AUC",
    "P3_gate": "P3 gate",
    "P4_clean": "P4 clean",
    "P4_traffic": "P4 traffic",
    "P4_walk_forward": "P4 walk-fwd",
    "P4_IN_SAMPLE": "P4 in-sample",
    "P5_blend": "Blend",
}
