"""Lineage batch writer (never blocks, counts drops) and the in-process metrics recorder."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime

import numpy as np
import pytest

from sentinel.config import LineageConfig
from sentinel.models.evaluate import latency_summary
from sentinel.monitoring.metrics import MetricsRecorder
from sentinel.serving.lineage import (
    COLUMNS,
    INSERT_PREDICTIONS,
    BatchedLineageWriter,
    PredictionRecord,
)


def _rec(i: int) -> PredictionRecord:
    return PredictionRecord(
        request_id=f"r{i}",
        file_id=f"f{i}",
        tenant_id="t",
        model_version="sentinel-win64/1",
        feature_schema_version="s",
        threshold=0.5,
        score=0.1,
        verdict="benign",
        override_applied=False,
        ts=datetime.now(UTC),
    )


def _wait(cond: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:  # type: ignore[operator]
        time.sleep(0.01)


def test_insert_statement_matches_record_fields() -> None:
    assert COLUMNS[0] == "request_id"
    assert len(COLUMNS) == 11
    assert COLUMNS[-1] == "event_id"
    assert INSERT_PREDICTIONS.count("%s") == len(COLUMNS)
    assert "ON CONFLICT DO NOTHING" in INSERT_PREDICTIONS


def test_batches_by_size_and_by_time() -> None:
    batches: list[int] = []
    cfg = LineageConfig(batch_size=10, flush_seconds=0.2, queue_max=1000, max_retries=0)
    w = BatchedLineageWriter(lambda b: batches.append(len(b)), cfg)
    for i in range(25):
        w.submit(_rec(i))
    _wait(lambda: sum(batches) == 25)
    assert sum(batches) == 25
    assert max(batches) <= 10
    assert batches[:2] == [10, 10]  # full batches go out without waiting for the timer
    w.close()
    assert (w.written, w.dropped) == (25, 0)


def test_full_queue_drops_and_counts_without_blocking() -> None:
    gate = threading.Event()
    metrics = MetricsRecorder("api", 10)
    cfg = LineageConfig(batch_size=1, flush_seconds=0.01, queue_max=5, max_retries=0)
    w = BatchedLineageWriter(lambda b: gate.wait(), cfg, metrics)
    t0 = time.perf_counter()
    for i in range(100):
        w.submit(_rec(i))
    assert time.perf_counter() - t0 < 0.5  # submit never waits on the database
    assert w.dropped >= 90
    assert metrics.counter("lineage_dropped", {"reason": "queue_full"}) == w.dropped
    gate.set()
    w.close()


def test_failing_database_retries_then_drops() -> None:
    calls: list[int] = []

    def flaky(batch: Sequence[PredictionRecord]) -> None:
        calls.append(len(batch))
        if len(calls) <= 2:
            raise ConnectionError("db down")

    metrics = MetricsRecorder("api", 10)
    cfg = LineageConfig(batch_size=5, flush_seconds=0.01, queue_max=100, max_retries=1)
    w = BatchedLineageWriter(flaky, cfg, metrics)
    for i in range(5):
        w.submit(_rec(i))
    _wait(lambda: w.dropped == 5)
    assert metrics.counter("lineage_dropped", {"reason": "write_failed"}) == 5
    for i in range(5, 8):
        w.submit(_rec(i))
    _wait(lambda: w.written == 3)  # the database is back: later rows are written
    w.close()
    assert (w.written, w.dropped) == (3, 5)


def test_close_flushes_queued_rows() -> None:
    got: list[str] = []
    cfg = LineageConfig(batch_size=1000, flush_seconds=30.0, queue_max=1000, max_retries=0)
    w = BatchedLineageWriter(lambda b: got.extend(r.request_id for r in b), cfg)
    for i in range(7):
        w.submit(_rec(i))
    w.close(timeout=5)
    assert sorted(got) == sorted(f"r{i}" for i in range(7))


# ------------------------------------------------------------------ metrics


def test_latency_summary_matches_numpy() -> None:
    v = np.random.default_rng(0).exponential(5.0, size=10_001)
    s = latency_summary(v)
    assert s.count == v.size
    assert s.sum == pytest.approx(v.sum())
    assert (s.p50, s.p95, s.p99) == tuple(np.percentile(v, [50, 95, 99]))
    empty = latency_summary([])
    assert empty.count == 0
    assert np.isnan(empty.p99)
    with pytest.raises(ValueError):
        latency_summary([1.0, float("inf")])
    with pytest.raises(ValueError):
        latency_summary(np.ones((2, 2)))
    assert set(s.as_dict()) == {"count", "sum", "p50", "p95", "p99"}


def test_metrics_recorder_counts_and_bounds_samples() -> None:
    m = MetricsRecorder("api", max_samples=100)
    for i in range(1000):
        m.observe("latency_ms", float(i), {"model_version": "a/1"})
    m.inc("requests", {"model_version": "a/1", "status": "200"})
    m.inc("requests", {"status": "200", "model_version": "a/1"}, 2)  # label order irrelevant
    snap = {(s.metric, tuple(sorted(s.labels.items()))): s for s in m.snapshot()}
    req = snap[("requests", (("model_version", "a/1"), ("status", "200")))]
    assert req.count == 3
    assert req.p99 is None
    lat = snap[("latency_ms", (("model_version", "a/1"),))]
    assert lat.count == 1000  # total since start
    assert lat.sum == sum(range(1000))
    assert lat.p50 == pytest.approx(np.percentile(np.arange(900, 1000), 50))  # last 100 only
    assert lat.as_dict()["service"] == "api"
    with pytest.raises(ValueError):
        MetricsRecorder("api", 0)
