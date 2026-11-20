"""In-process operational metrics: counters and latency samples per (metric, labels).

Stage 3 keeps everything in memory and exposes a snapshot. Stage 8 adds the 10-second
writer to PostgreSQL ops_metrics. Recording is O(1) under a lock and never raises into a
request. Percentiles come from sentinel.models.evaluate (the one place for statistics).
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sentinel.models.evaluate import latency_summary

LabelKey = tuple[tuple[str, str], ...]


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


class MetricsRecorder:
    """Thread-safe counters and bounded latency reservoirs (the most recent N samples)."""

    def __init__(self, service: str, max_samples: int) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be > 0")
        self.service = service
        self._max = max_samples
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, LabelKey], float] = {}
        self._samples: dict[tuple[str, LabelKey], deque[float]] = {}
        self._totals: dict[tuple[str, LabelKey], tuple[int, float]] = {}

    def inc(self, metric: str, labels: Mapping[str, str] | None = None, value: float = 1) -> None:
        k = (metric, _key(labels))
        with self._lock:
            self._counters[k] = self._counters.get(k, 0) + value

    def observe(self, metric: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        k = (metric, _key(labels))
        with self._lock:
            dq = self._samples.get(k)
            if dq is None:
                dq = self._samples[k] = deque(maxlen=self._max)
            dq.append(value)
            n, s = self._totals.get(k, (0, 0.0))
            self._totals[k] = (n + 1, s + value)

    def counter(self, metric: str, labels: Mapping[str, str] | None = None) -> float:
        with self._lock:
            return self._counters.get((metric, _key(labels)), 0)

    def snapshot(self) -> list[Series]:
        """Counters (count = value) and latency series (count/sum since start; percentiles
        over the reservoir)."""
        with self._lock:
            counters = dict(self._counters)
            samples = {k: list(v) for k, v in self._samples.items()}
            totals = dict(self._totals)
        out = [
            Series(self.service, m, dict(lk), int(v), float(v), None, None, None)
            for (m, lk), v in sorted(counters.items())
        ]
        for (m, lk), vals in sorted(samples.items()):
            s = latency_summary(vals)
            n, total = totals[(m, lk)]
            out.append(Series(self.service, m, dict(lk), n, total, s.p50, s.p95, s.p99))
        return out
