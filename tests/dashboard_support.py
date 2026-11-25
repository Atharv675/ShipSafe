"""Canned query results for the Streamlit pages (no database)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

OPS_COLS = ["ts_bucket", "bucket_seconds", "metric", "labels", "count", "sum", "p50", "p95", "p99"]
COLUMNS: dict[str, list[str]] = {
    "ops_api": OPS_COLS,
    "ops_lag": OPS_COLS,
    "never_break": OPS_COLS,
    "flag_rate": ["minute", "ring", "model_version", "events", "model_flags"],
    "override_rate": ["tenant_id", "events", "overridden"],
    "rollouts": [
        "ts",
        "file_type",
        "rollout_id",
        "seq",
        "from_state",
        "to_state",
        "actor",
        "reason",
    ],
    "alerts": ["id", "ts", "rule", "dedup_key", "labels", "state", "severity", "value", "message"],
    "serving": ["ring", "model_version", "events", "last_seen"],
    "score_hist": ["ring", "bin", "files"],
}


def _t(minutes_ago: float) -> datetime:
    return NOW - timedelta(minutes=minutes_ago)


def _ops(rows: list[tuple[Any, ...]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=OPS_COLS)


def frames() -> dict[str, pd.DataFrame]:
    mv1, mv5 = {"model_version": "sentinel-win64/1"}, {"model_version": "sentinel-win64/5"}
    api: list[tuple[Any, ...]] = []
    for i in range(30):
        ts = _t(10 - i / 6)
        api.append((ts, 10, "requests", {**mv1, "status": "200"}, 950, 950.0, None, None, None))
        api.append((ts, 10, "requests", {**mv5, "status": "200"}, 50, 50.0, None, None, None))
        api.append((ts, 10, "requests", {**mv5, "status": "500"}, 1, 1.0, None, None, None))
        api.append((ts, 10, "errors", mv5, 1, 1.0, None, None, None))
        p99 = 31.0 if i == 12 else 6.0  # one latency spike above the 20 ms alert limit
        api.append((ts, 10, "latency_ms", mv1, 950, 1900.0, 1.8, 3.9, p99))
    api.append((_t(8), 10, "model_reloads", {**mv5, "how": "loaded"}, 1, 1.0, None, None, None))
    api.append(
        (_t(3), 10, "lineage_dropped", {"reason": "write_failed"}, 40, 40.0, None, None, None)
    )
    lag = [
        (
            _t(10 - i),
            10,
            "consumer_lag",
            {"group": g, "topic": "file-events"},
            1,
            float(v),
            float(v),
            float(v),
            float(v),
        )
        for i in range(10)
        for g, v in (("sentinel-live", 5 * i), ("sentinel-shadow", 3))
    ]
    nb = [
        (_t(5 - i), 10, "canary_requests", {**mv5, "status": "200"}, 100, 100.0, None, None, None)
        for i in range(5)
    ]
    flags = pd.DataFrame(
        [
            (_t(10 - i), r, v, 500, int(500 * s))
            for i in range(10)
            for r, v, s in (
                ("ring0", "sentinel-win64/5", 0.47),
                ("ring1", "sentinel-win64/1", 0.45),
                ("ring2", "sentinel-win64/1", 0.45),
            )
        ],
        columns=COLUMNS["flag_rate"],
    )
    overrides = pd.DataFrame(
        [("tenant-0007", 900, 12), ("tenant-0002", 1200, 3)], columns=COLUMNS["override_rate"]
    )
    rollouts = pd.DataFrame(
        [
            (_t(30), "Win64", "r-1", 1, None, "APPROVED", "controller", "demo"),
            (_t(29), "Win64", "r-1", 2, "APPROVED", "RING0", "controller", "start"),
            (_t(20), "Win64", "r-1", 3, "RING0", "ROLLED_BACK", "controller", "[disagreement]"),
        ],
        columns=COLUMNS["rollouts"],
    )
    alerts = pd.DataFrame(
        [
            (
                1,
                _t(50),
                "api_p99_latency",
                "api_p99_latency|model_version=m/1",
                {},
                "FIRING",
                "page",
                31.0,
                "p99 above",
            ),
            (
                2,
                _t(40),
                "api_p99_latency",
                "api_p99_latency|model_version=m/1",
                {},
                "RESOLVED",
                "page",
                6.0,
                "ok",
            ),
            (
                3,
                _t(5),
                "never_break_hit",
                "never_break_hit|model_version=m/5",
                {},
                "FIRING",
                "page",
                1.0,
                "1 never_break_hits",
            ),
        ],
        columns=COLUMNS["alerts"],
    )
    return {
        "ops_api": _ops(api),
        "ops_lag": _ops(lag),
        "never_break": _ops(nb),
        "flag_rate": flags,
        "override_rate": overrides,
        "rollouts": rollouts,
        "alerts": alerts,
        "serving": pd.DataFrame(
            [
                ("ring0", "sentinel-win64/5", 250, _t(1)),
                ("ring1", "sentinel-win64/1", 1250, _t(1)),
                ("ring2", "sentinel-win64/1", 3500, _t(1)),
            ],
            columns=COLUMNS["serving"],
        ),
        "score_hist": pd.DataFrame(
            [
                (r, b, n)
                for r in ("ring0", "ring1", "ring2")
                for b, n in ((1, 900), (2, 40), (10, 12), (19, 30), (20, 800))
            ],
            columns=COLUMNS["score_hist"],
        ),
    }


# ---------------------------------------------------------------------------- Stage 9 (ML)

ML_COLUMNS: dict[str, list[str]] = {
    "filter_values": ["kind", "value", "n"],
    "flag_hourly": ["hour", "ring", "model_version", "events", "model_flags"],
    "gates": [
        "decision_id",
        "created_at",
        "file_type",
        "policy_version",
        "production_version",
        "candidate_version",
        "decision",
        "reason",
    ],
    "gate_bundle": ["decision_id", "bundle_sha256", "bundle_json"],
    "transitions": [
        "id",
        "ts",
        "file_type",
        "rollout_id",
        "seq",
        "model_name",
        "candidate_version",
        "production_version",
        "from_state",
        "to_state",
        "actor",
        "reason",
        "signals",
    ],
    "ring_flag": ["minute", "ring", "model_version", "events", "model_flags"],
    "lookup": [
        "request_id",
        "file_id",
        "tenant_id",
        "ring",
        "model_version",
        "feature_schema_version",
        "threshold",
        "score",
        "verdict",
        "override_applied",
        "reference_version",
        "reference_score",
        "reference_verdict",
        "trace_id",
        "ts",
    ],
    "shadow_lookup": [
        "event_id",
        "model_version",
        "file_id",
        "tenant_id",
        "ring",
        "week",
        "threshold",
        "score",
        "verdict",
        "ts",
    ],
}
COLUMNS.update(ML_COLUMNS)


def gate_bundle() -> dict[str, Any]:
    def rates(recall: float, fpr: float) -> dict[str, Any]:
        return {
            "recall": recall,
            "recall_ci": [recall - 0.005, recall + 0.005],
            "fpr": fpr,
            "fpr_ci": [fpr * 0.6, fpr * 1.6],
        }

    return {
        "decision": "REJECT",
        "decision_id": "Win64-v2-vs-v1-demo",
        "reason": "Rejected because [recall_at_fpr] the candidate catches less.",
        "models": {
            "candidate": {"label": "sentinel-win64/2"},
            "production": {"label": "sentinel-win64/1"},
        },
        "policy": {"policy_version": "1.0.0"},
        "evaluation": {"weeks": [53, 54, 55]},
        "checks": [
            {
                "name": "recall_at_fpr",
                "status": "fail",
                "hard": True,
                "summary": "86.9% vs 87.5%",
                "numbers": {"production": rates(0.875, 0.0009), "candidate": rates(0.869, 0.0009)},
            },
            {
                "name": "false_alarms",
                "status": "pass",
                "hard": True,
                "summary": "0.11% vs 0.18%",
                "numbers": {"production": rates(0.908, 0.0018), "candidate": rates(0.879, 0.0011)},
            },
            {
                "name": "disagreement",
                "status": "fail",
                "hard": True,
                "summary": "624 flips",
                "numbers": {
                    "malicious": {
                        "both_right": 13000,
                        "both_wrong": 1500,
                        "only_production_right": 400,
                        "only_candidate_right": 196,
                        "mcnemar_p": 1e-9,
                    },
                    "benign": {
                        "both_right": 14967,
                        "both_wrong": 15,
                        "only_production_right": 2,
                        "only_candidate_right": 12,
                        "mcnemar_p": 0.013,
                    },
                    "flipped_files": [
                        {
                            "file_id": f"f{i:03d}",
                            "week": 53 + i % 3,
                            "label": "malicious",
                            "production_score": 0.9999,
                            "candidate_score": 0.99,
                            "candidate_verdict": "benign",
                        }
                        for i in range(5)
                    ],
                },
            },
            {
                "name": "weekly_regression",
                "status": "fail",
                "hard": True,
                "summary": "week 53",
                "numbers": {
                    "weeks": [
                        {"week": w, "production_recall": 0.88, "candidate_recall": r}
                        for w, r in ((53, 0.84), (54, 0.88), (55, 0.85))
                    ]
                },
            },
            {
                "name": "calibration",
                "status": "pass",
                "hard": False,
                "summary": "ECE ok",
                "numbers": {},
            },
        ],
    }


def ml_frames() -> dict[str, pd.DataFrame]:
    import hashlib
    import json

    text = json.dumps(gate_bundle(), sort_keys=True)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    tr = [
        (
            1,
            _t(30),
            "Win64",
            "r-1",
            1,
            "sentinel-win64",
            "4",
            "1",
            None,
            "APPROVED",
            "c",
            "demo",
            {},
        ),
        (
            2,
            _t(29),
            "Win64",
            "r-1",
            2,
            "sentinel-win64",
            "4",
            "1",
            "APPROVED",
            "RING0",
            "c",
            "start",
            {},
        ),
        (
            3,
            _t(20),
            "Win64",
            "r-1",
            3,
            "sentinel-win64",
            "4",
            "1",
            "RING0",
            "ROLLED_BACK",
            "c",
            "[never_break] 1 flagged",
            {},
        ),
        (
            4,
            _t(15),
            "Win64",
            "r-2",
            1,
            "sentinel-win64",
            "3",
            "1",
            None,
            "APPROVED",
            "c",
            "demo",
            {},
        ),
        (
            5,
            _t(14),
            "Win64",
            "r-2",
            2,
            "sentinel-win64",
            "3",
            "1",
            "APPROVED",
            "RING0",
            "c",
            "start",
            {},
        ),
        (
            6,
            _t(8),
            "Win64",
            "r-2",
            3,
            "sentinel-win64",
            "3",
            "1",
            "RING0",
            "COMPLETE",
            "c",
            "done",
            {},
        ),
    ]
    return {
        "filter_values": pd.DataFrame(
            [
                ("model", "sentinel-win64/1", 900),
                ("model", "sentinel-win64/5", 50),
                ("ring", "ring0", 50),
                ("ring", "ring1", 400),
                ("tenant", "tenant-0002", 300),
            ],
            columns=ML_COLUMNS["filter_values"],
        ),
        "flag_hourly": pd.DataFrame(
            [
                (_t(60 * h), r, "sentinel-win64/1", 1000, 450 + h)
                for h in range(24)
                for r in ("ring0", "ring1", "ring2")
            ],
            columns=ML_COLUMNS["flag_hourly"],
        ),
        "gates": pd.DataFrame(
            [
                (
                    "Win64-v2-vs-v1-demo",
                    _t(100),
                    "Win64",
                    "1.0.0",
                    "sentinel-win64/1",
                    "sentinel-win64/2",
                    "REJECT",
                    "Rejected because [recall_at_fpr] ...",
                )
            ],
            columns=ML_COLUMNS["gates"],
        ),
        "gate_bundle": pd.DataFrame(
            [("Win64-v2-vs-v1-demo", sha, text)], columns=ML_COLUMNS["gate_bundle"]
        ),
        "transitions": pd.DataFrame(tr, columns=ML_COLUMNS["transitions"]),
        "ring_flag": pd.DataFrame(
            [
                (_t(30 - i), r, v, 100, 45 + (5 if r == "ring0" else 0))
                for i in range(15)
                for r, v in (("ring0", "sentinel-win64/4"), ("ring1", "sentinel-win64/1"))
            ],
            columns=ML_COLUMNS["ring_flag"],
        ),
        "lookup": pd.DataFrame(
            [
                (
                    "req-1",
                    "abc",
                    "tenant-0002",
                    "ring0",
                    "sentinel-win64/4",
                    "b952",
                    0.5,
                    0.97,
                    "malicious",
                    False,
                    "sentinel-win64/1",
                    0.31,
                    "benign",
                    "a" * 32,
                    _t(25),
                )
            ],
            columns=ML_COLUMNS["lookup"],
        ),
        "shadow_lookup": pd.DataFrame(
            [
                (
                    "ev-1",
                    "sentinel-win64/2",
                    "abc",
                    "tenant-0002",
                    "ring0",
                    53,
                    0.99,
                    0.98,
                    "benign",
                    _t(25),
                )
            ],
            columns=ML_COLUMNS["shadow_lookup"],
        ),
    }


class FakeRegistry:
    def __init__(self, empty: bool = False, broken: bool = False) -> None:
        self.empty, self.broken = empty, broken

    def versions(self) -> pd.DataFrame:
        from sentinel.dashboard.ml_data import REGISTRY_COLUMNS

        if self.broken:
            raise ConnectionError("mlflow down")
        rows = (
            []
            if self.empty
            else [
                (
                    "sentinel-win64",
                    "1",
                    "production, ring1_model, ring2_model",
                    _t(5000),
                    "baseline",
                    "stage2_v1",
                    "52",
                    "1-47",
                    "48-51",
                    "c824866c3eb2b871",
                    "a3c6",
                    "",
                    "run1",
                ),
                (
                    "sentinel-win64",
                    "2",
                    "candidate",
                    _t(4000),
                    "candidate",
                    "last_n_weeks",
                    "52",
                    "27-47",
                    "48-51",
                    "c824866c3eb2b871",
                    "62f3",
                    "",
                    "run2",
                ),
                (
                    "sentinel-win64",
                    "4",
                    "ring0_model",
                    _t(3000),
                    "",
                    "",
                    "",
                    "",
                    "",
                    "c824866c3eb2",
                    "",
                    "true",
                    "run4",
                ),
                (
                    "sentinel-win32",
                    "1",
                    "production",
                    _t(5000),
                    "baseline",
                    "stage2_v1",
                    "52",
                    "1-47",
                    "48-51",
                    "1111",
                    "",
                    "",
                    "run5",
                ),
            ]
        )
        return pd.DataFrame(rows, columns=REGISTRY_COLUMNS)


def ml_results() -> dict[str, Any]:
    """Synthetic results folder: scores + labels for three models, PSI, Stage 7/7b tables."""
    import numpy as np

    rng = np.random.default_rng(5)
    weeks = np.repeat(np.arange(45, 65), 1000)
    y = np.tile(np.repeat([0, 1], 500), 20).astype(np.int8)
    cols: dict[str, Any] = {"file_id": [f"f{i}" for i in range(y.size)], "week": weeks, "y": y}
    models: dict[str, Any] = {}
    for mid, s, as_of in (
        ("v1", 2.0, 52),
        ("all_history_T52", 2.4, 52),
        ("all_history_T54", 1.6, 54),
    ):
        z = s * (2 * y - 1) + rng.normal(size=y.size)
        raw = 1 / (1 + np.exp(-z))
        cols[f"raw_{mid}"], cols[f"prob_{mid}"] = raw, raw.astype(np.float32)
        models[mid] = {
            "model_id": mid,
            "policy": "x",
            "as_of_week": as_of,
            "threshold": float(np.quantile(raw[y == 0], 0.99)),
            "fit_weeks": [1, as_of - 6],
            "calibration_weeks": [as_of - 9, as_of - 6],
            "never_break_flags": 0,
        }
    psi = pd.DataFrame(
        [
            {"feature": f, "week": w, "psi": 0.01 * (w - 44) * (k + 1)}
            for k, f in enumerate(("f_1", "f_2", "f_3"))
            for w in range(45, 65)
        ]
    )
    drift = pd.DataFrame(
        [
            {"schedule": "v1 kept live", "rule": "none", "week": w, "alert": w == 57}
            for w in range(53, 65)
        ]
    )
    pols = ("P1_auto", "P2_val_auc", "P3_gate")
    summary = pd.DataFrame(
        [
            {
                "release_policy": p,
                **{
                    f"{c}{s}": v
                    for c, v in (
                        ("harmful_promotions", 5 - i),
                        ("good_blocked", i),
                        ("promotions", 18 - 8 * i),
                    )
                    for s, v in (("", v), ("_lo", max(0, v - 2)), ("_hi", v + 2))
                },
            }
            for i, p in enumerate(pols)
        ]
    )
    weekly = pd.DataFrame(
        [
            {
                "resample": 0,
                "release_policy": p,
                "retrain_policy": "all_history",
                "week": w,
                "model": "m",
                "recall_at_fpr": 0.88 + 0.001 * w % 3,
                "shipped_fpr": 0.001,
                "shipped_recall": 0.9,
            }
            for p in pols
            for w in range(53, 65)
        ]
    )
    exp = pd.DataFrame(
        [
            {"resample": 0, "release_policy": "P1_auto", "drift_alert": i == 0, "harmful": i < 2}
            for i in range(6)
        ]
    )
    b2 = pd.DataFrame([{"release_policy": "P4_clean", "recalibration": True, "promotions": 0}])
    diffs = pd.DataFrame(
        [
            {
                "comparison": "P3 - P1",
                "metric": "shipped FPR",
                "diff": 0.06,
                "ci_low": 0.04,
                "ci_high": 0.08,
            },
            {
                "comparison": "P3 - P1",
                "metric": "recall@0.1%FPR",
                "diff": 0.1,
                "ci_low": -1.0,
                "ci_high": 1.2,
            },
        ]
    )
    return {
        "tables": {
            "ml_scores": pd.DataFrame(cols),
            "ml_psi": psi,
            "backtest_pass2_drift": drift,
            "experiment_summary": summary,
            "experiment_weekly": weekly,
            "experiment": exp,
            "backtest_pass2_summary": b2,
            "backtest_pass2_differences": diffs,
        },
        "documents": {"ml_models": {"models": models}},
    }


class FakeResults:
    def __init__(self, empty: bool = False) -> None:
        data = {"tables": {}, "documents": {}} if empty else ml_results()
        self.path = f"fake-{'empty' if empty else 'full'}"
        self._t, self._d = data["tables"], data["documents"]

    def table(self, name: str) -> pd.DataFrame | None:
        t = self._t.get(name)
        return None if t is None else t.copy()

    def document(self, name: str) -> dict[str, Any] | None:
        return self._d.get(name)


class FakeSource:
    def __init__(self, empty: bool = False) -> None:
        self._frames = (
            {k: pd.DataFrame(columns=c) for k, c in COLUMNS.items()}
            if empty
            else {**frames(), **ml_frames()}
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def query(self, name: str, sql: str, params: Mapping[str, Any]) -> pd.DataFrame:
        self.calls.append((name, dict(params)))
        return self._frames[name].copy()
