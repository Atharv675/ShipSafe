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
    }


class FakeSource:
    def __init__(self, empty: bool = False) -> None:
        self._frames = (
            {k: pd.DataFrame(columns=c) for k, c in COLUMNS.items()} if empty else frames()
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def query(self, name: str, sql: str, params: Mapping[str, Any]) -> pd.DataFrame:
        self.calls.append((name, dict(params)))
        return self._frames[name].copy()
