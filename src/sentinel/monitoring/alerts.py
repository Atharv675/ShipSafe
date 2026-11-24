"""Stage 8 alert rules on ops_metrics (configs/alert_rules.yaml).

Pure evaluation: `evaluate_rules(rules, points, now)` -> one `Evaluation` per (rule, label
group) with firing yes/no, the value and a plain-English message. `transitions(current,
evaluations)` turns that into the state changes to store: only FIRING / RESOLVED changes are
written (append-only `alerts`, de-duplicated per dedup_key; the database refuses a repeat).

Timing ("for N seconds"): a rule fires when its condition held in every 10-second bucket of
the last N seconds that has data, the data reaches back the full N seconds, and the newest
bucket is recent (`recent_seconds`). No recent data = not firing: an alert never stays up on
stale numbers, and a quiet service (no requests) resolves its latency alert.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sentinel.config import (
    AlertRule,
    AlertRulesConfig,
    CountRule,
    RatioRule,
    RisingRule,
    ThresholdRule,
)
from sentinel.logging import get_logger
from sentinel.models.evaluate import linear_slope

log = get_logger(__name__)

FIRING = "FIRING"
RESOLVED = "RESOLVED"


@dataclass(frozen=True)
class Point:
    """One ops_metrics row (ts = bucket start, epoch seconds)."""

    ts: float
    service: str
    metric: str
    labels: Mapping[str, str]
    count: int
    sum: float
    p50: float | None = None
    p95: float | None = None
    p99: float | None = None


@dataclass(frozen=True)
class Evaluation:
    rule: str
    dedup_key: str
    labels: dict[str, str]
    severity: str
    firing: bool
    value: float | None
    message: str


@dataclass(frozen=True)
class CurrentAlert:
    rule: str
    dedup_key: str
    labels: dict[str, str]
    severity: str
    state: str


@dataclass(frozen=True)
class AlertEvent:
    rule: str
    dedup_key: str
    labels: dict[str, str]
    severity: str
    state: str
    value: float | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "dedup_key": self.dedup_key,
            "labels": self.labels,
            "severity": self.severity,
            "state": self.state,
            "value": self.value,
            "message": self.message,
        }


def dedup_key(rule: str, labels: Mapping[str, str]) -> str:
    return rule + "|" + ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def _group(labels: Mapping[str, str], group_by: Sequence[str]) -> tuple[tuple[str, str], ...]:
    return tuple((k, str(labels.get(k, ""))) for k in group_by)


def sustained(
    points: Sequence[tuple[float, bool]], now: float, for_seconds: float, recent_seconds: float
) -> bool:
    """(bucket start, breached) points: breached in every bucket of the last `for_seconds`,
    data reaching back that far, and the newest bucket within `recent_seconds` of now."""
    if not points:
        return False
    newest = max(t for t, _ in points)
    if newest <= now - recent_seconds:
        return False
    span = [(t, b) for t, b in points if t > now - for_seconds - recent_seconds]
    if not span or not all(b for _, b in span):
        return False
    return min(t for t, _ in span) <= now - for_seconds


def _fmt_labels(labels: Mapping[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(labels.items())) or "all"


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:g} min"


def _eval_threshold(
    rule: ThresholdRule, pts: Sequence[Point], now: float, recent: float
) -> list[Evaluation]:
    by_group: dict[tuple[tuple[str, str], ...], dict[float, float]] = defaultdict(dict)
    for p in pts:
        if p.service != rule.service or p.metric != rule.metric:
            continue
        v = getattr(p, rule.field)
        if v is None or not math.isfinite(v):
            continue
        g = by_group[_group(p.labels, rule.group_by)]
        g[p.ts] = max(v, g.get(p.ts, -math.inf))  # several series in one group: the worst
    out = []
    for grp, series in by_group.items():
        labels = dict(grp)
        firing = sustained(
            [(t, v > rule.above) for t, v in series.items()], now, rule.for_seconds, recent
        )
        latest = series[max(series)]
        msg = (
            f"{rule.metric} {rule.field} {latest:.1f} (limit {rule.above:g}) "
            f"{'above the limit for' if firing else 'not above the limit for all of'} "
            f"{_minutes(rule.for_seconds)} [{_fmt_labels(labels)}]"
        )
        out.append(
            Evaluation(
                rule.name,
                dedup_key(rule.name, labels),
                labels,
                rule.severity,
                firing,
                latest,
                msg,
            )
        )
    return out


def _eval_ratio(
    rule: RatioRule, pts: Sequence[Point], now: float, recent: float
) -> list[Evaluation]:
    num: dict[tuple[tuple[str, str], ...], dict[float, int]] = defaultdict(lambda: defaultdict(int))
    den: dict[tuple[tuple[str, str], ...], dict[float, int]] = defaultdict(lambda: defaultdict(int))
    for p in pts:
        if p.service != rule.service:
            continue
        g = _group(p.labels, rule.group_by)
        if p.metric == rule.numerator:
            num[g][p.ts] += p.count
        elif p.metric == rule.denominator:
            den[g][p.ts] += p.count
    out = []
    for grp, d in den.items():
        labels = dict(grp)
        rates = {t: num[grp].get(t, 0) / n for t, n in d.items() if n >= rule.min_denominator}
        firing = sustained(
            [(t, r > rule.above) for t, r in rates.items()], now, rule.for_seconds, recent
        )
        latest = rates[max(rates)] if rates else None
        shown = "no bucket with enough requests" if latest is None else f"{100 * latest:.2f}%"
        msg = (
            f"{rule.numerator}/{rule.denominator} {shown} (limit {100 * rule.above:g}%) "
            f"{'above the limit for' if firing else 'not above the limit for all of'} "
            f"{_minutes(rule.for_seconds)} [{_fmt_labels(labels)}]"
        )
        out.append(
            Evaluation(
                rule.name, dedup_key(rule.name, labels), labels, rule.severity, firing, latest, msg
            )
        )
    return out


def _eval_rising(
    rule: RisingRule, pts: Sequence[Point], now: float, recent: float
) -> list[Evaluation]:
    by_group: dict[tuple[tuple[str, str], ...], dict[float, float]] = defaultdict(dict)
    for p in pts:
        if p.service != rule.service or p.metric != rule.metric or p.count <= 0:
            continue
        by_group[_group(p.labels, rule.group_by)][p.ts] = p.sum / p.count  # gauge: mean
    out = []
    for grp, series in by_group.items():
        labels = dict(grp)
        span = {t: v for t, v in series.items() if t > now - rule.for_seconds - recent}
        ts = sorted(span)
        slope = linear_slope(ts, [span[t] for t in ts]) * 60 if len(ts) >= 2 else float("nan")
        covered = bool(ts) and ts[0] <= now - rule.for_seconds and ts[-1] > now - recent
        last = span[ts[-1]] if ts else None
        # "rising for N minutes": the lag must rise in EVERY segment of the span, not just
        # overall (a flat 6 minutes followed by a steep 4 is not "rising for 10 minutes")
        seg_len = rule.for_seconds / rule.segments
        seg_slopes = []
        for k in range(rule.segments):
            lo = now - rule.for_seconds - recent + k * seg_len
            hi = lo + seg_len if k < rule.segments - 1 else math.inf
            seg = [t for t in ts if lo < t <= hi]
            seg_slopes.append(
                linear_slope(seg, [span[t] for t in seg]) * 60 if len(seg) >= 2 else math.nan
            )
        every_segment = all(math.isfinite(s) and s > rule.min_slope_per_minute for s in seg_slopes)
        firing = bool(
            covered
            and every_segment
            and math.isfinite(slope)
            and slope > rule.min_slope_per_minute
            and last is not None
            and last > span[ts[0]]
            and last > 0
        )
        shown = "n/a" if not math.isfinite(slope) else f"{slope:+.1f}/min"
        weakest = min((s for s in seg_slopes if math.isfinite(s)), default=math.nan)
        msg = (
            f"{rule.metric} {last if last is not None else 'n/a'} "
            f"(slope over {_minutes(rule.for_seconds)} {shown}, weakest of {rule.segments} "
            f"segments {'n/a' if math.isnan(weakest) else f'{weakest:+.1f}'}/min, limit "
            f"+{rule.min_slope_per_minute:g}/min){' rising' if firing else ''} "
            f"[{_fmt_labels(labels)}]"
        )
        out.append(
            Evaluation(
                rule.name,
                dedup_key(rule.name, labels),
                labels,
                rule.severity,
                firing,
                None if not math.isfinite(slope) else slope,
                msg,
            )
        )
    return out


def _eval_count(rule: CountRule, pts: Sequence[Point], now: float) -> list[Evaluation]:
    totals: dict[tuple[tuple[str, str], ...], int] = defaultdict(int)
    for p in pts:
        if p.service == rule.service and p.metric == rule.metric:
            g = _group(p.labels, rule.group_by)
            totals[g] += p.count if p.ts > now - rule.lookback_seconds else 0
    out = []
    for grp, n in totals.items():
        labels = dict(grp)
        msg = (
            f"{n} {rule.metric} in the last {_minutes(rule.lookback_seconds)} "
            f"[{_fmt_labels(labels)}]"
        )
        out.append(
            Evaluation(
                rule.name, dedup_key(rule.name, labels), labels, rule.severity, n > 0, float(n), msg
            )
        )
    return out


def evaluate_rule(
    rule: AlertRule, points: Sequence[Point], now: float, recent_seconds: float
) -> list[Evaluation]:
    if isinstance(rule, ThresholdRule):
        return _eval_threshold(rule, points, now, recent_seconds)
    if isinstance(rule, RatioRule):
        return _eval_ratio(rule, points, now, recent_seconds)
    if isinstance(rule, RisingRule):
        return _eval_rising(rule, points, now, recent_seconds)
    return _eval_count(rule, points, now)


def evaluate_rules(cfg: AlertRulesConfig, points: Sequence[Point], now: float) -> list[Evaluation]:
    return [e for r in cfg.rules for e in evaluate_rule(r, points, now, cfg.recent_seconds)]


def transitions(
    current: Mapping[str, CurrentAlert], evaluations: Iterable[Evaluation]
) -> list[AlertEvent]:
    """FIRING for a newly firing key; RESOLVED for a firing key whose condition no longer
    holds, or that has no data any more. Nothing for unchanged states (de-duplication)."""
    events: list[AlertEvent] = []
    seen: set[str] = set()
    for e in evaluations:
        seen.add(e.dedup_key)
        was_firing = e.dedup_key in current and current[e.dedup_key].state == FIRING
        if e.firing and not was_firing:
            events.append(
                AlertEvent(e.rule, e.dedup_key, e.labels, e.severity, FIRING, e.value, e.message)
            )
        elif was_firing and not e.firing:
            events.append(
                AlertEvent(e.rule, e.dedup_key, e.labels, e.severity, RESOLVED, e.value, e.message)
            )
    for key, c in sorted(current.items()):
        if key not in seen and c.state == FIRING:
            events.append(
                AlertEvent(
                    c.rule,
                    key,
                    c.labels,
                    c.severity,
                    RESOLVED,
                    None,
                    "no recent data for this alert",
                )
            )
    return events


# ---------------------------------------------------------------------------- storage


SELECT_POINTS = (
    "SELECT extract(epoch FROM ts_bucket), service, metric, labels, count, sum, p50, p95, p99 "
    "FROM ops_metrics WHERE ts_bucket > %s AND bucket_seconds <= %s AND metric = ANY(%s)"
)
SELECT_CURRENT = (
    "SELECT DISTINCT ON (dedup_key) dedup_key, rule, labels, severity, state "
    "FROM alerts ORDER BY dedup_key, id DESC"
)
INSERT_ALERT = (
    "INSERT INTO alerts (rule, dedup_key, labels, state, severity, value, message) "
    "VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s)"
)


def rule_metrics(cfg: AlertRulesConfig) -> list[str]:
    names: set[str] = set()
    for r in cfg.rules:
        if isinstance(r, RatioRule):
            names |= {r.numerator, r.denominator}
        else:
            names.add(r.metric)
    return sorted(names)


def _labels(v: Any) -> dict[str, str]:
    d = v if isinstance(v, dict) else json.loads(v or "{}")
    return {str(k): str(x) for k, x in d.items()}


def points_from_rows(rows: Iterable[Sequence[Any]]) -> list[Point]:
    return [
        Point(
            float(r[0]),
            str(r[1]),
            str(r[2]),
            _labels(r[3]),
            int(r[4]),
            float(r[5]),
            r[6],
            r[7],
            r[8],
        )
        for r in rows
    ]


Webhook = Callable[[dict[str, Any]], None]


class AlertEngine:  # pragma: no cover - SQL + HTTP, exercised by the stack and integration tests
    """One evaluation round: read points + current states, write transitions, notify."""

    def __init__(
        self,
        dsn: str,
        cfg: AlertRulesConfig,
        fine_bucket_seconds: int,
        webhook: Webhook | None = None,
    ) -> None:
        self._dsn, self._cfg = dsn, cfg
        self._fine = fine_bucket_seconds
        self._webhook = webhook
        self._metrics = rule_metrics(cfg)

    def run_once(self, now: datetime) -> list[AlertEvent]:
        import psycopg

        since = now.timestamp() - self._cfg.lookback_seconds()
        with psycopg.connect(self._dsn, connect_timeout=5) as conn:
            rows = conn.execute(
                SELECT_POINTS,
                (datetime.fromtimestamp(since, tz=now.tzinfo), self._fine, self._metrics),
            ).fetchall()
            current = {
                r[0]: CurrentAlert(r[1], r[0], _labels(r[2]), r[3], r[4])
                for r in conn.execute(SELECT_CURRENT).fetchall()
            }
            events = transitions(
                current, evaluate_rules(self._cfg, points_from_rows(rows), now.timestamp())
            )
            written: list[AlertEvent] = []
            for ev in events:
                try:
                    with conn.transaction():
                        conn.execute(
                            INSERT_ALERT,
                            (
                                ev.rule,
                                ev.dedup_key,
                                json.dumps(ev.labels, sort_keys=True),
                                ev.state,
                                ev.severity,
                                ev.value,
                                ev.message,
                            ),
                        )
                except psycopg.errors.RaiseException as exc:  # another writer got there first
                    log.warning("alert_duplicate_refused", key=ev.dedup_key, error=str(exc))
                    continue
                written.append(ev)
                log.warning("alert_" + ev.state.lower(), **ev.as_dict())
        for ev in written:
            if self._webhook is not None:
                try:
                    self._webhook({**ev.as_dict(), "ts": now.isoformat()})
                except Exception as exc:
                    log.error("alert_webhook_failed", key=ev.dedup_key, error=type(exc).__name__)
        return written


def http_webhook(url: str, timeout: float) -> Webhook:  # pragma: no cover - network
    import httpx

    def post(body: dict[str, Any]) -> None:
        httpx.post(url, json=body, timeout=timeout).raise_for_status()

    return post
