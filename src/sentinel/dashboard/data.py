"""Data for the Streamlit operations pages (Stage 8; Stage 9 adds the ML pages).

Reads PostgreSQL only, as the read-only `sentinel_dashboard` role. Every query has a name so
tests can serve canned frames (`Source` protocol). The transforms below are pure functions
on DataFrames; statistics come from sentinel.models.evaluate.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

import pandas as pd

from sentinel.config import AlertRulesConfig, RatioRule, ThresholdRule


class Source(Protocol):
    def query(self, name: str, sql: str, params: Mapping[str, Any]) -> pd.DataFrame: ...


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    @classmethod
    def last(cls, minutes: int, now: datetime, align_seconds: float) -> Window:
        """[now - minutes, now], `now` floored to `align_seconds` so cached queries hit."""
        ts = math.floor(now.timestamp() / align_seconds) * align_seconds
        end = datetime.fromtimestamp(ts, tz=UTC)
        return cls(end - timedelta(minutes=minutes), end)

    def params(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end}


# ---------------------------------------------------------------------------- SQL

OPS_SERIES_SQL = """
SELECT ts_bucket, bucket_seconds, metric, labels, count, sum, p50, p95, p99
FROM ops_metrics
WHERE service = %(service)s AND metric = ANY(%(metrics)s)
  AND ts_bucket >= %(start)s AND ts_bucket < %(end)s
ORDER BY ts_bucket
"""

FLAG_RATE_SQL = """
SELECT date_bin('1 minute', ts, TIMESTAMPTZ 'epoch') AS minute, ring, model_version,
       count(*) AS events, count(*) FILTER (WHERE score > threshold) AS model_flags
FROM predictions
WHERE ts >= %(start)s AND ts < %(end)s AND tenant_id NOT LIKE %(canary)s
GROUP BY 1, 2, 3 ORDER BY 1
"""

OVERRIDE_RATE_SQL = """
SELECT tenant_id, count(*) AS events,
       count(*) FILTER (WHERE override_applied) AS overridden
FROM predictions
WHERE ts >= %(start)s AND ts < %(end)s AND tenant_id NOT LIKE %(canary)s
GROUP BY tenant_id
ORDER BY count(*) FILTER (WHERE override_applied) DESC, count(*) DESC, tenant_id
LIMIT %(top)s
"""

ROLLOUT_SQL = """
(SELECT ts, file_type, rollout_id, seq, from_state, to_state, actor, reason
   FROM rollout_transitions WHERE ts >= %(start)s AND ts < %(end)s)
UNION ALL
(SELECT DISTINCT ON (file_type) ts, file_type, rollout_id, seq, from_state, to_state, actor, reason
   FROM rollout_transitions WHERE ts < %(start)s ORDER BY file_type, id DESC)
ORDER BY ts
"""

ALERTS_SQL = """
SELECT id, ts, rule, dedup_key, labels, state, severity, value, message
FROM alerts
WHERE ts >= %(start)s AND ts < %(end)s
   OR dedup_key IN (SELECT DISTINCT ON (dedup_key) dedup_key FROM alerts
                    ORDER BY dedup_key, id DESC)
ORDER BY id
"""


# ---------------------------------------------------------------------------- transforms


def _label(labels: Any, key: str) -> str:
    d = labels if isinstance(labels, dict) else json.loads(labels or "{}")
    return str(d.get(key, ""))


def with_label(df: pd.DataFrame, key: str) -> pd.DataFrame:
    out = df.copy()
    out[key] = [_label(v, key) for v in out["labels"]] if len(out) else []
    return out


def requests_per_second(series: pd.DataFrame) -> pd.DataFrame:
    """Per bucket and model_version: answered requests / bucket length."""
    r = with_label(series[series["metric"] == "requests"], "model_version")
    if r.empty:
        return pd.DataFrame(columns=["ts_bucket", "model_version", "requests_per_second"])
    g = r.groupby(["ts_bucket", "model_version"], as_index=False).agg(
        n=("count", "sum"), secs=("bucket_seconds", "max")
    )
    g["requests_per_second"] = g["n"] / g["secs"]
    return g[["ts_bucket", "model_version", "requests_per_second"]]


def error_rate(series: pd.DataFrame) -> pd.DataFrame:
    """Per bucket and model_version: 5xx / answered requests (buckets with requests only)."""
    r = with_label(series[series["metric"].isin(["requests", "errors"])], "model_version")
    cols = ["ts_bucket", "model_version", "requests", "errors", "error_rate"]
    if r.empty:
        return pd.DataFrame(columns=cols)
    piv = r.pivot_table(
        index=["ts_bucket", "model_version"], columns="metric", values="count", aggfunc="sum"
    ).reset_index()
    for c in ("requests", "errors"):
        if c not in piv:
            piv[c] = 0
    piv = piv.fillna({"requests": 0, "errors": 0})
    piv = piv[piv["requests"] > 0]
    piv["error_rate"] = piv["errors"] / piv["requests"]
    return piv[cols]


def latency(series: pd.DataFrame) -> pd.DataFrame:
    """Long format: ts_bucket, model_version, percentile (p50/p95/p99), ms. Several API
    workers each write a row per bucket: the chart shows the worst worker (as the alert)."""
    r = with_label(series[series["metric"] == "latency_ms"], "model_version")
    if r.empty:
        return pd.DataFrame(columns=["ts_bucket", "model_version", "percentile", "ms"])
    r = r.groupby(["ts_bucket", "model_version"], as_index=False)[["p50", "p95", "p99"]].max()
    long = r.melt(
        id_vars=["ts_bucket", "model_version"],
        value_vars=["p50", "p95", "p99"],
        var_name="percentile",
        value_name="ms",
    )
    return long.dropna(subset=["ms"]).reset_index(drop=True)


def reload_events(series: pd.DataFrame) -> pd.DataFrame:
    r = with_label(series[series["metric"] == "model_reloads"], "model_version")
    return r[["ts_bucket", "model_version"]].reset_index(drop=True)


def consumer_lag(series: pd.DataFrame) -> pd.DataFrame:
    r = with_label(series[series["metric"] == "consumer_lag"], "group")
    if r.empty:
        return pd.DataFrame(columns=["ts_bucket", "group", "lag"])
    r = r[r["count"] > 0].copy()
    r["lag"] = r["sum"] / r["count"]
    return r[["ts_bucket", "group", "lag"]].reset_index(drop=True)


def never_break_hits(series: pd.DataFrame, canaries: pd.DataFrame) -> pd.DataFrame:
    """Per bucket: canaries scored and canaries flagged (0 expected), all buckets that had
    canary traffic or hits."""
    scored = pd.DataFrame(
        canaries.groupby("ts_bucket")["count"].sum().rename("canaries_scored")
    ).reset_index()
    nb = series[series["metric"] == "never_break_hits"]
    hits = pd.DataFrame(
        nb.groupby("ts_bucket")["count"].sum().rename("never_break_hits")
    ).reset_index()
    out = scored.merge(hits, on="ts_bucket", how="outer").fillna(0)
    return pd.DataFrame(out.sort_values("ts_bucket").reset_index(drop=True))


def flag_rate(df: pd.DataFrame) -> pd.DataFrame:
    """Model flag rate (score > threshold, before overrides) per minute, ring, version."""
    out = df.copy()
    out["flag_rate"] = out["model_flags"] / out["events"]
    out["ring"] = out["ring"].fillna("none")
    return out


def override_rate(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["override_rate"] = out["overridden"] / out["events"]
    return out


def latency_spikes(lat: pd.DataFrame, above_ms: float) -> pd.DataFrame:
    """Buckets whose p99 is above the alert threshold (worst first)."""
    p99 = lat[(lat["percentile"] == "p99") & (lat["ms"] > above_ms)]
    return p99.sort_values("ms", ascending=False).reset_index(drop=True)


def jaeger_url(
    ui: str, service: str, start: datetime, end: datetime, margin_seconds: float, limit: int = 20
) -> str:
    """Jaeger UI search for `service` in [start - margin, end + margin] (microseconds)."""
    s = int((start.timestamp() - margin_seconds) * 1_000_000)
    e = int((end.timestamp() + margin_seconds) * 1_000_000)
    q = urlencode({"service": service, "start": s, "end": e, "limit": limit, "lookback": "custom"})
    return f"{ui.rstrip('/')}/search?{q}"


def alert_history(alerts: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """One row per firing episode: rule, labels, fired, resolved (None = still firing),
    duration in minutes, message when it fired."""
    rows: list[dict[str, Any]] = []
    for key, g in alerts.sort_values("id").groupby("dedup_key", sort=False):
        fired: Any = None
        for r in g.itertuples():
            if r.state == "FIRING":
                fired = r
            elif r.state == "RESOLVED" and fired is not None:
                rows.append(_episode(str(key), fired, r.ts))
                fired = None
        if fired is not None:
            rows.append(_episode(str(key), fired, None, now))
    cols = ["rule", "dedup_key", "severity", "fired", "resolved", "minutes", "message"]
    return pd.DataFrame(rows, columns=cols).sort_values("fired", ascending=False, ignore_index=True)


def _episode(key: str, fired: Any, resolved: Any, now: datetime | None = None) -> dict[str, Any]:
    end: Any = resolved if resolved is not None else now
    return {
        "rule": fired.rule,
        "dedup_key": key,
        "severity": fired.severity,
        "fired": fired.ts,
        "resolved": resolved,
        "minutes": round((pd.Timestamp(end) - pd.Timestamp(fired.ts)).total_seconds() / 60, 1),
        "message": fired.message,
    }


def firing_now(alerts: pd.DataFrame) -> pd.DataFrame:
    if alerts.empty:
        return alerts
    latest = alerts.sort_values("id").groupby("dedup_key").tail(1)
    return latest[latest["state"] == "FIRING"].reset_index(drop=True)


def rule_table(cfg: AlertRulesConfig) -> pd.DataFrame:
    rows = []
    for r in cfg.rules:
        if isinstance(r, ThresholdRule):
            cond = f"{r.metric} {r.field} > {r.above:g}"
        elif isinstance(r, RatioRule):
            cond = (
                f"{r.numerator}/{r.denominator} > {r.above:g} (>= {r.min_denominator} req/bucket)"
            )
        elif r.kind == "rising":
            cond = f"{r.metric} slope > {r.min_slope_per_minute:g}/min"
        else:
            cond = f"any {r.metric} (resolves after {r.lookback_seconds / 60:g} min without)"
        rows.append(
            {
                "rule": r.name,
                "severity": r.severity,
                "service": r.service,
                "condition": cond,
                "for": f"{r.for_seconds / 60:g} min" if r.for_seconds else "at once",
                "per": ", ".join(r.group_by) or "-",
            }
        )
    return pd.DataFrame(rows)


class PostgresSource:  # pragma: no cover - needs the database (exercised on the stack)
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def query(self, name: str, sql: str, params: Mapping[str, Any]) -> pd.DataFrame:
        import psycopg

        with psycopg.connect(self._dsn, connect_timeout=5) as conn:
            cur = conn.execute(sql, dict(params))
            cols = [d.name for d in cur.description or []]
            return pd.DataFrame(cur.fetchall(), columns=cols)
