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


# ---------------------------------------------------------------------------- redesign (2026-11-24)

SERVING_SQL = """
SELECT ring, model_version, count(*) AS events, max(ts) AS last_seen
FROM predictions
WHERE ts >= %(start)s AND ts < %(end)s AND tenant_id NOT LIKE %(canary)s
GROUP BY ring, model_version
ORDER BY ring, model_version
"""

# model score distribution per ring: 20 equal-width bins on [0, 1] (bin 21 = exactly 1.0)
SCORE_HIST_SQL = """
SELECT ring, least(width_bucket(score, 0, 1, %(bins)s), %(bins)s) AS bin, count(*) AS files
FROM predictions
WHERE ts >= %(start)s AND ts < %(end)s AND tenant_id NOT LIKE %(canary)s
GROUP BY 1, 2 ORDER BY 1, 2
"""


def per_bucket(series: pd.DataFrame, metric: str, how: str = "sum") -> pd.Series:
    """One value per ts_bucket for a metric: summed counts ('sum'), or the worst p99 ('p99')
    / mean gauge value ('gauge') across label sets and API workers."""
    r = series[series["metric"] == metric]
    if r.empty:
        return pd.Series(dtype="float64")
    if how == "p99":
        out = r.groupby("ts_bucket")["p99"].max()
    elif how == "gauge":
        g = r.groupby("ts_bucket")[["sum", "count"]].sum()
        out = g["sum"] / g["count"]
    else:
        out = r.groupby("ts_bucket")["count"].sum()
    return out.sort_index().astype("float64")


@dataclass(frozen=True)
class Kpi:
    """A headline value with its recent trend (for st.metric sparklines)."""

    value: float | None
    previous: float | None
    trend: list[float]

    @property
    def delta(self) -> float | None:
        if self.value is None or self.previous is None:
            return None
        return self.value - self.previous


def kpi(values: pd.Series, points: int = 30) -> Kpi:
    """Latest value, the value before it, and the last `points` values as the trend."""
    v = values.dropna()
    if v.empty:
        return Kpi(None, None, [])
    trend = [float(x) for x in v.iloc[-points:]]
    prev = float(v.iloc[-2]) if len(v) >= 2 else None
    return Kpi(float(v.iloc[-1]), prev, trend)


def rate_per_second(counts: pd.Series, bucket_seconds: float) -> pd.Series:
    return counts / bucket_seconds


def ratio_series(num: pd.Series, den: pd.Series, min_den: float = 1) -> pd.Series:
    d = den[den >= min_den]
    return (num.reindex(d.index).fillna(0) / d).astype("float64")


def drops(series: pd.DataFrame) -> pd.DataFrame:
    """Rows lost on purpose (never blocking a request), per bucket and kind."""
    kinds = {
        "lineage_dropped": "lineage rows",
        "ops_metrics_dropped": "metric rows",
        "metrics_samples_dropped": "latency samples",
    }
    r = series[series["metric"].isin(list(kinds))]
    if r.empty:
        return pd.DataFrame(columns=["ts_bucket", "kind", "count"])
    g = pd.DataFrame(r.groupby(["ts_bucket", "metric"], as_index=False)[["count"]].sum())
    g["kind"] = g["metric"].map(kinds)
    return g[["ts_bucket", "kind", "count"]]


def status_mix(series: pd.DataFrame) -> pd.DataFrame:
    """Answered requests per bucket by HTTP status class (2xx / 4xx / 5xx)."""
    r = with_label(series[series["metric"] == "requests"], "status")
    if r.empty:
        return pd.DataFrame(columns=["ts_bucket", "status", "count"])
    r["status"] = r["status"].str[0] + "xx"
    return pd.DataFrame(r.groupby(["ts_bucket", "status"], as_index=False)[["count"]].sum())


def ring_share(serving: pd.DataFrame) -> pd.DataFrame:
    """Share of real events per ring (part-to-whole for a segmented bar)."""
    if serving.empty:
        return pd.DataFrame(columns=["ring", "events", "share"])
    s = pd.DataFrame(
        serving.assign(ring=serving["ring"].fillna("none"))
        .groupby("ring", as_index=False)[["events"]]
        .sum()
    )
    s["share"] = s["events"] / s["events"].sum()
    return s.sort_values("ring", ignore_index=True)


def score_histogram(hist: pd.DataFrame, bins: int) -> pd.DataFrame:
    """Bin index -> score range start/end, and each ring's share of files per bin."""
    if hist.empty:
        return pd.DataFrame(columns=["ring", "bin", "score_lo", "score_hi", "files", "share"])
    h = hist.assign(ring=hist["ring"].fillna("none")).copy()
    h["score_lo"] = (h["bin"] - 1) / bins
    h["score_hi"] = h["bin"] / bins
    h["share"] = h["files"] / h.groupby("ring")["files"].transform("sum")
    return h


def rollout_segments(ro: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Rollout states as time segments [start, end) per file type (for a timeline)."""
    if ro.empty:
        return pd.DataFrame(columns=["file_type", "rollout_id", "state", "start", "end"])
    rows = []
    for ft, g in ro.sort_values("ts").groupby("file_type"):
        recs = g.to_dict("records")
        for i, r in enumerate(recs):
            end = recs[i + 1]["ts"] if i + 1 < len(recs) else now
            rows.append(
                {
                    "file_type": ft,
                    "rollout_id": r["rollout_id"],
                    "state": r["to_state"],
                    "start": r["ts"],
                    "end": end,
                }
            )
    return pd.DataFrame(rows)


@dataclass(frozen=True)
class AlertStats:
    firing: int
    episodes: int
    mean_minutes_to_resolve: float | None
    noisiest_rule: str | None
    episodes_of_noisiest: int


def alert_stats(history: pd.DataFrame, firing: pd.DataFrame) -> AlertStats:
    closed = history[history["resolved"].notna()] if not history.empty else history
    mttr = float(closed["minutes"].mean()) if len(closed) else None
    noisy = str(history["rule"].value_counts().idxmax()) if len(history) else None
    top = int(history["rule"].value_counts().max()) if len(history) else 0
    return AlertStats(len(firing), len(history), mttr, noisy, top)


def health(firing: pd.DataFrame) -> str:
    """Overall state for the banner: critical if a page-severity alert fires, warning if
    only lower severities fire, else good."""
    if firing.empty:
        return "good"
    return "critical" if (firing["severity"] == "page").any() else "warning"


def break_gaps(
    df: pd.DataFrame, x: str, by: list[str], value: str, step_seconds: float, *, zero: bool
) -> pd.DataFrame:
    """Make idle stretches visible instead of drawing a line across them.

    Wherever two consecutive points of a series are more than 1.5 steps apart:
    - zero=True (counts, rates of events): the series drops to 0 for the gap (no requests
      really means 0 requests per second);
    - zero=False (ratios, latency, lag): one empty row starts the gap, so the line breaks
      (unknown is not zero).
    """
    if df.empty:
        return df
    step = pd.Timedelta(seconds=step_seconds)
    parts = []
    groups = df.groupby(by, sort=False) if by else [((), df)]
    for key, g in groups:
        g = g.sort_values(x)
        ts = pd.to_datetime(g[x])
        gaps = ts.diff() > 1.5 * step
        extra = []
        keys = dict(zip(by, key if isinstance(key, tuple) else (key,), strict=False))
        for prev, nxt in zip(ts.shift(1)[gaps], ts[gaps], strict=False):
            if zero:
                extra += [{**keys, x: prev + step, value: 0.0}, {**keys, x: nxt - step, value: 0.0}]
            else:
                extra.append({**keys, x: prev + step, value: float("nan")})
        parts.append(g)
        if extra:
            parts.append(pd.DataFrame(extra))
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values([*by, x], ignore_index=True)


def clip_spans(df: pd.DataFrame, start_col: str, end_col: str, window: Window) -> pd.DataFrame:
    """Keep spans that overlap the window and clip them to it (timelines)."""
    if df.empty:
        return df
    s = pd.to_datetime(df[start_col], utc=True)
    e = pd.to_datetime(df[end_col], utc=True)
    lo, hi = pd.Timestamp(window.start), pd.Timestamp(window.end)
    keep = (e >= lo) & (s <= hi)
    out = df[keep].copy()
    out[start_col] = s[keep].where(s[keep] >= lo, lo)
    out[end_col] = e[keep].where(e[keep] <= hi, hi)
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------- redesign (2026-11-25)


def verdict_timeline(flags: pd.DataFrame) -> pd.DataFrame:
    """Long format per minute: files scored and files flagged (all rings and versions)."""
    cols = ["minute", "series", "files"]
    if flags.empty:
        return pd.DataFrame(columns=cols)
    g = flags.groupby("minute", as_index=False)[["events", "model_flags"]].sum()
    long = g.melt(
        id_vars="minute",
        value_vars=["events", "model_flags"],
        var_name="series",
        value_name="files",
    )
    long["series"] = long["series"].map({"events": "scored", "model_flags": "flagged"})
    return long[cols].sort_values(["series", "minute"], ignore_index=True)


def verdict_mix(flags: pd.DataFrame, tenants: pd.DataFrame) -> pd.DataFrame:
    """Files by outcome in the window: not flagged / flagged by the model / overridden by a
    tenant allow-or-block rule (overrides counted over the tenants listed)."""
    scored = int(flags["events"].sum()) if len(flags) else 0
    flagged = int(flags["model_flags"].sum()) if len(flags) else 0
    overridden = int(tenants["overridden"].sum()) if len(tenants) else 0
    rows = [
        ("not flagged", max(0, scored - flagged)),
        ("flagged", flagged),
        ("overridden", overridden),
    ]
    return pd.DataFrame(rows, columns=["verdict", "files"])


def ring_flag_rates(flags: pd.DataFrame) -> pd.DataFrame:
    cols = ["ring", "events", "model_flags", "flag_rate"]
    if flags.empty:
        return pd.DataFrame(columns=cols)
    g = (
        flags.assign(ring=flags["ring"].fillna("none"))
        .groupby("ring", as_index=False)[["events", "model_flags"]]
        .sum()
    )
    g = g[g["events"] > 0]
    g["flag_rate"] = g["model_flags"] / g["events"]
    return g[cols].sort_values("ring", ignore_index=True)


def tenant_shares(tenants: pd.DataFrame, top: int) -> pd.DataFrame:
    """The busiest tenants; `share` = events relative to the busiest (bar length)."""
    cols = ["tenant_id", "events", "overridden", "share"]
    if tenants.empty:
        return pd.DataFrame(columns=cols)
    t = tenants.sort_values("events", ascending=False).head(top).copy()
    t["share"] = t["events"] / max(1, int(t["events"].max()))
    return t[cols].reset_index(drop=True)
