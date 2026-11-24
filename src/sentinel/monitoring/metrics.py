"""In-process operational metrics: counters and latency samples per (metric, labels).

Two views of the same recordings:
- `snapshot()`: since process start (counters) and a reservoir of the newest samples
  (GET /v1/ops/stats, debugging);
- `drain()`: what happened since the last drain (Stage 8). `OpsMetricsWriter` drains every
  `flush_seconds` and writes one PostgreSQL `ops_metrics` row per active series.

Recording is O(1) under a lock and never raises into a request. The per-window sample buffer
is bounded: beyond `window_max_samples` a sample is not kept for the percentiles (counted as
`metrics_samples_dropped`), while count and sum stay exact. If the database is down, the
window is dropped and counted (`ops_metrics_dropped`); nothing waits or retries in a request.
Percentiles come from sentinel.models.evaluate (the one place for statistics).
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sentinel.logging import get_logger
from sentinel.models.evaluate import latency_summary

log = get_logger(__name__)

LabelKey = tuple[tuple[str, str], ...]
SeriesKey = tuple[str, LabelKey]


def _key(labels: Mapping[str, str] | None) -> LabelKey:
    return tuple(sorted((labels or {}).items()))


@dataclass(frozen=True)
class Series:
    service: str
    metric: str
    labels: dict[str, str]
    count: int
    sum: float
    p50: float | None
    p95: float | None
    p99: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "metric": self.metric,
            "labels": self.labels,
            "count": self.count,
            "sum": self.sum,
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
        }


@dataclass
class _Window:
    counters: dict[SeriesKey, float] = field(default_factory=dict)
    samples: dict[SeriesKey, list[float]] = field(default_factory=dict)
    totals: dict[SeriesKey, tuple[int, float]] = field(default_factory=dict)
    dropped_samples: int = 0


class MetricsRecorder:
    """Thread-safe counters and latency samples, both since start and per drain window."""

    def __init__(self, service: str, max_samples: int, window_max_samples: int | None = None):
        if max_samples <= 0:
            raise ValueError("max_samples must be > 0")
        if window_max_samples is not None and window_max_samples <= 0:
            raise ValueError("window_max_samples must be > 0")
        self.service = service
        self._max = max_samples
        self._win_max = window_max_samples or max_samples
        self._lock = threading.Lock()
        self._counters: dict[SeriesKey, float] = {}
        self._samples: dict[SeriesKey, deque[float]] = {}
        self._totals: dict[SeriesKey, tuple[int, float]] = {}
        self._win = _Window()

    def inc(self, metric: str, labels: Mapping[str, str] | None = None, value: float = 1) -> None:
        k = (metric, _key(labels))
        with self._lock:
            self._counters[k] = self._counters.get(k, 0) + value
            self._win.counters[k] = self._win.counters.get(k, 0) + value

    def observe(self, metric: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        k = (metric, _key(labels))
        with self._lock:
            dq = self._samples.get(k)
            if dq is None:
                dq = self._samples[k] = deque(maxlen=self._max)
            dq.append(value)
            n, s = self._totals.get(k, (0, 0.0))
            self._totals[k] = (n + 1, s + value)
            win = self._win
            kept = win.samples.setdefault(k, [])
            if len(kept) < self._win_max:
                kept.append(value)
            else:
                win.dropped_samples += 1
            n, s = win.totals.get(k, (0, 0.0))
            win.totals[k] = (n + 1, s + value)

    def counter(self, metric: str, labels: Mapping[str, str] | None = None) -> float:
        with self._lock:
            return self._counters.get((metric, _key(labels)), 0)

    def _series(
        self,
        counters: Mapping[SeriesKey, float],
        samples: Mapping[SeriesKey, Sequence[float]],
        totals: Mapping[SeriesKey, tuple[int, float]],
    ) -> list[Series]:
        out = [
            Series(self.service, m, dict(lk), int(v), float(v), None, None, None)
            for (m, lk), v in sorted(counters.items())
        ]
        for (m, lk), vals in sorted(samples.items()):
            s = latency_summary(vals)
            n, total = totals[(m, lk)]
            out.append(Series(self.service, m, dict(lk), n, total, s.p50, s.p95, s.p99))
        return out

    def snapshot(self) -> list[Series]:
        """Counters (count = value) and latency series (count/sum since start; percentiles
        over the reservoir)."""
        with self._lock:
            counters = dict(self._counters)
            samples = {k: list(v) for k, v in self._samples.items()}
            totals = dict(self._totals)
        return self._series(counters, samples, totals)

    def drain(self) -> list[Series]:
        """Everything recorded since the previous drain, then start a new window. Counters
        are deltas; latency count/sum are exact, percentiles over the kept samples."""
        with self._lock:
            win, self._win = self._win, _Window()
        if win.dropped_samples:
            self.inc("metrics_samples_dropped", value=win.dropped_samples)  # next window
        return self._series(win.counters, win.samples, win.totals)


RowWriter = Callable[[datetime, int, Sequence[Series]], None]


def bucket_start(now: float, seconds: float) -> datetime:
    """The window's bucket: `now` floored to a multiple of `seconds` (UTC)."""
    return datetime.fromtimestamp(math.floor(now / seconds) * seconds, tz=UTC)


class OpsMetricsWriter:
    """Every `flush_seconds`: drain each recorder and write the rows in one call. A failed
    write drops that window and counts it on the first recorder (`ops_metrics_dropped`, rows),
    so the next successful window records the outage."""

    def __init__(
        self,
        recorders: Sequence[MetricsRecorder],
        write: RowWriter,
        flush_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not recorders:
            raise ValueError("at least one recorder")
        self._recorders = list(recorders)
        self._write = write
        self._flush = flush_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.written = 0
        self.dropped = 0

    def flush_once(self) -> int:
        """Drain + write one window; returns the rows written (0 on failure)."""
        now = self._clock()
        rows = [s for r in self._recorders for s in r.drain()]
        if not rows:
            return 0
        bucket = bucket_start(now - self._flush, self._flush)  # the window that just ended
        try:
            self._write(bucket, round(self._flush), rows)
        except Exception as exc:
            self.dropped += len(rows)
            self._recorders[0].inc("ops_metrics_dropped", value=len(rows))
            log.warning("ops_metrics_write_failed", rows=len(rows), error=type(exc).__name__)
            return 0
        self.written += len(rows)
        return len(rows)

    def _run(self) -> None:
        while not self._stop.wait(self._flush):
            self.flush_once()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ops-metrics-writer", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        """Stop and write the last partial window."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self.flush_once()


INSERT_OPS_METRICS = (
    "INSERT INTO ops_metrics (ts_bucket, bucket_seconds, service, metric, labels, count, sum, "
    "p50, p95, p99) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)"
)


def _finite(v: float | None) -> float | None:
    return v if v is not None and math.isfinite(v) else None


def ops_rows(bucket: datetime, seconds: int, rows: Sequence[Series]) -> list[tuple[Any, ...]]:
    return [
        (
            bucket,
            seconds,
            s.service,
            s.metric,
            json.dumps(s.labels, sort_keys=True),
            s.count,
            s.sum,
            _finite(s.p50),
            _finite(s.p95),
            _finite(s.p99),
        )
        for s in rows
    ]


def postgres_ops_writer(dsn: str, connect_timeout: int = 3) -> RowWriter:
    """One reused connection; reconnects after an error (never retries the same window)."""
    import psycopg

    state: dict[str, psycopg.Connection[tuple[object, ...]] | None] = {"conn": None}

    def write(bucket: datetime, seconds: int, rows: Sequence[Series]) -> None:
        conn = state["conn"]
        try:
            if conn is None or conn.closed:
                conn = state["conn"] = psycopg.connect(dsn, connect_timeout=connect_timeout)
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(INSERT_OPS_METRICS, ops_rows(bucket, seconds, rows))
        except Exception:
            if conn is not None:
                conn.close()
            state["conn"] = None
            raise

    return write
