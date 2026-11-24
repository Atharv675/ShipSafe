"""Stage 8: metrics windows + writer, tracing, consumer lag, alert rules, worker schedule."""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sentinel.config import (
    AlertRulesConfig,
    CountRule,
    RatioRule,
    RetentionConfig,
    RisingRule,
    ThresholdRule,
    load_alert_rules,
    load_monitoring_config,
    load_replay_config,
)
from sentinel.models.evaluate import linear_slope
from sentinel.monitoring import alerts as al
from sentinel.monitoring.lag import group_lag, record_lag
from sentinel.monitoring.metrics import (
    INSERT_OPS_METRICS,
    MetricsRecorder,
    OpsMetricsWriter,
    Series,
    bucket_start,
    ops_rows,
)
from sentinel.monitoring.worker import Job, run_jobs
from sentinel.replay import live_consumer
from sentinel.replay.events import FileEvent
from sentinel.rollout.store import health_from_stats, stats_from_ops_rows
from sentinel.serving.app import create_app
from sentinel.serving.lineage import BatchedLineageWriter, InMemorySink, PredictionRecord
from sentinel.serving.models import ModelManager
from sentinel.serving.overrides import InMemoryOverrideStore
from tests.serving_support import DIM, NAMES, SCHEMA, FakeSource, loaded, serving_cfg

RULES = load_alert_rules()
MON = load_monitoring_config()
ROW = [0.1] * DIM
TRACEPARENT_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACEPARENT_TRACE}-00f067aa0ba902b7-01"


def _rule(name: str) -> Any:
    return next(r for r in RULES.rules if r.name == name)


# ------------------------------------------------------------------ configs


def test_configs_load_and_match_the_plan() -> None:
    assert MON.metrics.flush_seconds == 10
    assert MON.consumer_lag.interval_seconds == 10
    assert RULES.evaluate_every_seconds == 30
    p99 = _rule("api_p99_latency")
    assert isinstance(p99, ThresholdRule) and (p99.field, p99.above, p99.for_seconds) == (
        "p99",
        20.0,
        300,
    )
    err = _rule("api_error_rate")
    assert isinstance(err, RatioRule) and (err.above, err.for_seconds) == (0.01, 300)
    lag = _rule("consumer_lag_rising")
    assert isinstance(lag, RisingRule) and lag.for_seconds == 600
    nb = _rule("never_break_hit")
    assert isinstance(nb, CountRule) and nb.for_seconds == 0
    assert MON.retention.downsample_after_hours == 24
    assert MON.retention.downsample_bucket_seconds == 60
    assert MON.retention.delete_after_days == 14
    with pytest.raises(ValueError, match="downsampled before"):
        RetentionConfig(
            run_every_hours=24,
            downsample_after_hours=48,
            downsample_bucket_seconds=60,
            delete_after_days=1,
        )
    assert RULES.lookback_seconds() == 600 + RULES.recent_seconds


# ------------------------------------------------------------------ metrics windows


def test_window_percentiles_match_numpy_on_random_data() -> None:
    rng = np.random.default_rng(8)
    m = MetricsRecorder("api", max_samples=50, window_max_samples=100_000)
    first = rng.lognormal(1.0, 0.6, 5000)
    for v in first:
        m.observe("latency_ms", float(v), {"model_version": "m/1"})
    [s] = m.drain()
    assert s.count == 5000 and s.sum == pytest.approx(first.sum())
    p50, p95, p99 = np.percentile(first, [50, 95, 99])
    assert (s.p50, s.p95, s.p99) == (pytest.approx(p50), pytest.approx(p95), pytest.approx(p99))
    # the next window holds only what came after the drain
    second = rng.uniform(0, 3, 777)
    for v in second:
        m.observe("latency_ms", float(v), {"model_version": "m/1"})
    [s2] = m.drain()
    assert s2.count == 777 and s2.p99 == pytest.approx(np.percentile(second, 99))
    assert m.drain() == []  # nothing happened since
    # the since-start view is unchanged by draining
    [snap] = m.snapshot()
    assert snap.count == 5777


def test_window_counters_are_deltas_and_overflow_is_counted() -> None:
    m = MetricsRecorder("api", max_samples=10, window_max_samples=3)
    m.inc("requests", {"status": "200"}, 5)
    for v in (1.0, 2.0, 3.0, 4.0, 100.0):
        m.observe("latency_ms", v)
    out = {s.metric: s for s in m.drain()}
    assert out["requests"].count == 5
    lat = out["latency_ms"]
    assert lat.count == 5 and lat.sum == 110.0  # exact even though only 3 samples were kept
    assert lat.p99 == pytest.approx(np.percentile([1.0, 2.0, 3.0], 99))
    m.inc("requests", {"status": "200"})
    nxt = {s.metric: s for s in m.drain()}
    assert nxt["requests"].count == 1  # a delta, not the running total
    assert nxt["metrics_samples_dropped"].count == 2
    assert m.counter("requests", {"status": "200"}) == 6


class Rows:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[datetime, int, list[Series]]] = []

    def __call__(self, bucket: datetime, seconds: int, rows: Sequence[Series]) -> None:
        if self.fail:
            raise ConnectionError("database down")
        self.calls.append((bucket, seconds, list(rows)))


def test_writer_writes_one_bucket_per_window_and_drops_when_the_database_is_down() -> None:
    m = MetricsRecorder("api", 100)
    rows = Rows(fail=True)
    w = OpsMetricsWriter([m], rows, flush_seconds=10, clock=lambda: 1_000_005.0)
    m.inc("requests", {"status": "200"}, 7)
    m.observe("latency_ms", 2.0)
    assert w.flush_once() == 0
    assert (w.dropped, w.written) == (2, 0)
    rows.fail = False
    m.inc("requests", {"status": "200"}, 3)
    assert w.flush_once() == 2  # requests + the drop counter; the dropped window is gone
    [(bucket, seconds, written)] = rows.calls
    assert seconds == 10 and bucket == datetime.fromtimestamp(999_990, tz=UTC)
    by = {s.metric: s for s in written}
    assert by["requests"].count == 3
    assert by["ops_metrics_dropped"].count == 2
    assert w.flush_once() == 0  # empty window: nothing to write


def test_ops_rows_match_the_insert_and_turn_nan_into_null() -> None:
    s = Series("api", "latency_ms", {"b": "2", "a": "1"}, 0, 0.0, math.nan, math.nan, math.nan)
    [row] = ops_rows(bucket_start(25.0, 10), 10, [s])
    assert len(row) == INSERT_OPS_METRICS.count("%s")
    assert row[4] == '{"a": "1", "b": "2"}'
    assert row[7:] == (None, None, None)
    assert row[0] == datetime.fromtimestamp(20, tz=UTC)


# ------------------------------------------------------------------ API: DB down, traces


def _app(
    *,
    tracer: Any = None,
    sink: Any = None,
    ops_writer: Any = None,
    threshold: float = 0.5,
    metrics: MetricsRecorder | None = None,
) -> tuple[Any, MetricsRecorder, Any]:
    src = FakeSource()
    src.add(loaded("Win64", "1", seed=1, threshold=threshold))
    src.add(loaded("Win32", "1", seed=2, threshold=threshold))
    metrics = metrics or MetricsRecorder("api", 1000)
    manager = ModelManager(src, NAMES, "production", metrics)
    manager.refresh()
    sink = sink or InMemorySink()
    app = create_app(
        manager,
        InMemoryOverrideStore(),
        sink,
        metrics,
        serving_cfg(),
        background=False,
        canary_prefix="canary-",
        ops_writer=ops_writer,
        tracer=tracer,
    )
    return app, metrics, sink


def _body(tenant: str = "tenant-a") -> dict[str, Any]:
    return {
        "file_id": "f-1",
        "tenant_id": tenant,
        "features": ROW,
        "feature_schema_version": SCHEMA["Win64"],
    }


def test_api_keeps_serving_with_the_database_down() -> None:
    """Lineage and ops_metrics writes both fail: every request still gets its verdict,
    the lost rows are counted."""

    def broken_lineage(batch: Sequence[PredictionRecord]) -> None:
        raise ConnectionError("database down")

    metrics = MetricsRecorder("api", 1000)
    sink = BatchedLineageWriter(
        broken_lineage,
        serving_cfg().lineage.model_copy(update={"flush_seconds": 0.05, "max_retries": 0}),
        metrics,
    )
    rows = Rows(fail=True)
    app, _, _ = _app(sink=sink, metrics=metrics)
    writer = OpsMetricsWriter([metrics], rows, flush_seconds=10)
    with TestClient(app) as c:
        codes = [c.post("/v1/score", json=_body()).status_code for _ in range(40)]
        assert writer.flush_once() == 0
    assert codes == [200] * 40
    assert writer.dropped > 0
    assert sink.dropped == 40
    assert metrics.counter("lineage_dropped", {"reason": "write_failed"}) == 40
    assert metrics.counter("ops_metrics_dropped") == writer.dropped


def _tracer() -> tuple[Any, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_trace_id_is_stored_with_the_prediction_and_joins_the_callers_trace() -> None:
    tracer, exporter = _tracer()
    app, _, sink = _app(tracer=tracer)
    with TestClient(app) as c:
        r1 = c.post("/v1/score", json=_body(), headers={"traceparent": TRACEPARENT})
        r2 = c.post("/v1/score", json=_body())  # no caller trace: the API starts one
    assert r1.status_code == r2.status_code == 200
    rec1, rec2 = sink.records
    assert rec1.trace_id == TRACEPARENT_TRACE
    assert rec2.trace_id is not None and len(rec2.trace_id) == 32 and rec2.trace_id != rec1.trace_id
    spans = exporter.get_finished_spans()
    score = [s for s in spans if s.name == "score"]
    log_spans = [s for s in spans if s.name == "log"]
    assert len(score) == len(log_spans) == 2
    first = next(s for s in score if f"{s.context.trace_id:032x}" == TRACEPARENT_TRACE)
    assert first.parent is not None and f"{first.parent.span_id:016x}" == "00f067aa0ba902b7"
    assert first.attributes is not None
    assert first.attributes["sentinel.request_id"] == r1.json()["request_id"]
    child = next(s for s in log_spans if s.context.trace_id == first.context.trace_id)
    assert child.parent is not None and child.parent.span_id == first.context.span_id


def test_without_tracing_the_trace_id_is_null() -> None:
    app, _, sink = _app()  # global provider is the no-op one in unit tests
    with TestClient(app) as c:
        assert c.post("/v1/score", json=_body(), headers={"traceparent": TRACEPARENT}).is_success
    assert sink.records[0].trace_id is None


def test_canary_flagged_by_the_model_counts_a_never_break_hit() -> None:
    app, metrics, _ = _app(threshold=-1.0)  # flags everything
    with TestClient(app) as c:
        assert c.post("/v1/score", json=_body("canary-ring0")).is_success
        assert c.post("/v1/score", json=_body("tenant-a")).is_success  # real traffic: no hit
    series = {(s.metric, tuple(sorted(s.labels.items()))): s.count for s in metrics.drain()}
    hits = {k: v for k, v in series.items() if k[0] == "never_break_hits"}
    assert list(hits.values()) == [1]
    [(_, labels)] = hits
    assert dict(labels)["model_version"] == "sentinel-win64/1"


def test_live_consumer_sends_traceparent_of_its_consume_span(monkeypatch: Any) -> None:
    tracer, exporter = _tracer()
    monkeypatch.setattr(live_consumer, "tracer", tracer)
    seen: list[dict[str, str]] = []

    def post(body: dict[str, Any], headers: dict[str, str]) -> int:
        seen.append(dict(headers))
        return 503 if len(seen) == 1 else 200  # one retry, same span

    ev = FileEvent(
        "e-1",
        "f-1",
        "Win64",
        "t-1",
        "ring0",
        53,
        "2024-09-22T00:00:00Z",
        SCHEMA["Win64"],
        np.asarray(ROW, np.float32),
    )
    cfg = load_replay_config().live
    out = live_consumer.score_with_retry(ev, post, cfg, threading.Event(), sleep=lambda s: False)
    assert out == "ok"
    [span] = exporter.get_finished_spans()
    assert span.name == "consume"
    tid, sid = f"{span.context.trace_id:032x}", f"{span.context.span_id:016x}"
    assert len(seen) == 2 and seen[0] == seen[1]  # retries stay in the same span
    version, trace_hex, span_hex, flags = seen[0]["traceparent"].split("-")
    assert (version, trace_hex, span_hex) == ("00", tid, sid)
    assert int(flags, 16) & 0x01  # sampled
    assert span.attributes is not None and span.attributes["sentinel.attempts"] == 2


# ------------------------------------------------------------------ consumer lag


def test_group_lag_sums_partitions_and_uses_low_watermark_without_commit() -> None:
    marks = {0: (0, 100), 1: (10, 50), 2: (0, 5)}
    assert group_lag({0: 90, 1: -1001, 2: 5}, marks) == 10 + 40 + 0
    assert group_lag({}, marks) == 100 + 40 + 5
    assert group_lag({0: 120}, {0: (0, 100)}) == 0  # never negative


def test_record_lag_skips_unreadable_groups() -> None:
    class Source:
        def read(self, group: str) -> int | None:
            if group == "broken":
                raise TimeoutError
            return None if group == "no-topic" else 42

    m = MetricsRecorder("consumers", 100)
    out = record_lag(Source(), ["live", "broken", "no-topic"], "file-events", m)
    assert out == {"live": 42, "broken": None, "no-topic": None}
    [s] = m.drain()
    assert (s.metric, s.labels, s.sum) == (
        "consumer_lag",
        {"group": "live", "topic": "file-events"},
        42.0,
    )


# ------------------------------------------------------------------ alert rules

NOW = 1_700_000_000.0


def _pts(
    metric: str,
    values: Sequence[float],
    *,
    service: str = "api",
    field: str = "p99",
    end_age: float = 10.0,
    labels: dict[str, str] | None = None,
    count: int = 100,
) -> list[al.Point]:
    """One point per 10-second bucket, the newest `end_age` seconds before NOW."""
    out = []
    n = len(values)
    for i, v in enumerate(values):
        ts = NOW - end_age - 10 * (n - 1 - i)
        kw = {field: v} if field in ("p50", "p95", "p99") else {}
        s = v * count if field == "sum" else 0.0
        out.append(
            al.Point(ts, service, metric, labels or {"model_version": "m/1"}, count, s, **kw)
        )
    return out


def _one(rule_name: str, points: Sequence[al.Point]) -> al.Evaluation:
    [e] = al.evaluate_rule(_rule(rule_name), points, NOW, RULES.recent_seconds)
    return e


def test_sustained_needs_full_coverage_every_bucket_and_recent_data() -> None:
    pts = [(NOW - 10 * i, True) for i in range(31)]  # 300 s back
    assert al.sustained(pts, NOW, 300, 60)
    assert not al.sustained(pts[:20], NOW, 300, 60)  # only 190 s of data
    assert not al.sustained([*pts[1:], (NOW, False)], NOW, 300, 60)  # newest bucket fine
    old = [(t - 120, b) for t, b in pts]
    assert not al.sustained(old, NOW, 300, 60)  # stale
    assert not al.sustained([], NOW, 300, 60)


def test_p99_latency_rule_fires_after_five_minutes_and_resolves() -> None:
    slow6 = _pts("latency_ms", [35.0] * 37)  # 6 minutes above 20 ms
    e = _one("api_p99_latency", slow6)
    assert e.firing and e.value == 35.0 and "model_version=m/1" in e.message
    assert not _one("api_p99_latency", _pts("latency_ms", [35.0] * 25)).firing  # only 4 min
    recovered = _pts("latency_ms", [35.0] * 34 + [5.0, 5.0, 5.0])
    assert not _one("api_p99_latency", recovered).firing
    assert not _one("api_p99_latency", _pts("latency_ms", [35.0] * 37, end_age=200)).firing
    edge = _pts("latency_ms", [20.0] * 37)  # "> 20", not ">="
    assert not _one("api_p99_latency", edge).firing


def test_error_rate_rule_fires_resolves_and_ignores_thin_buckets() -> None:
    def stream(err: int, req: int, n: int = 37) -> list[al.Point]:
        return _pts("requests", [0] * n, field="none", count=req) + (
            _pts("errors", [0] * n, field="none", count=err) if err else []
        )

    assert _one("api_error_rate", stream(2, 100)).firing  # 2% > 1% for 6 min
    assert not _one("api_error_rate", stream(1, 100)).firing  # exactly 1%: not above
    assert not _one("api_error_rate", stream(0, 100)).firing
    thin = _one("api_error_rate", stream(5, 10))  # 50% but < 20 requests per bucket
    assert not thin.firing and thin.value is None


def test_consumer_lag_rule_fires_when_rising_for_ten_minutes() -> None:
    lab = {"group": "sentinel-live"}

    def lag(values: Sequence[float]) -> list[al.Point]:
        return _pts("consumer_lag", values, service="consumers", field="sum", count=1, labels=lab)

    rising = lag([100 + 10 * i for i in range(67)])  # 11 min, +60/min
    e = _one("consumer_lag_rising", rising)
    assert e.firing and e.value == pytest.approx(60.0)
    assert not _one("consumer_lag_rising", lag([500.0] * 67)).firing  # stuck but flat
    assert not _one("consumer_lag_rising", lag([1000 - 10 * i for i in range(67)])).firing
    assert not _one("consumer_lag_rising", lag([100 + 10 * i for i in range(40)])).firing  # 6.5 min
    # Regression (seen on the stack 2026-11-23): 6 flat minutes then a steep 4-minute climb
    # has a positive overall slope but is not "rising for 10 minutes"
    flat_then_steep = lag([0.0] * 37 + [460.0 * i for i in range(1, 31)])
    late = _one("consumer_lag_rising", flat_then_steep)
    assert not late.firing and "weakest of 5 segments" in late.message
    # the same climb, once it has lasted the whole 10 minutes, does fire
    assert _one("consumer_lag_rising", lag([460.0 * i for i in range(1, 68)])).firing


def test_never_break_rule_fires_at_once_and_resolves_after_the_lookback() -> None:
    rule = _rule("never_break_hit")
    hit_now = [al.Point(NOW - 20, "api", "never_break_hits", {"model_version": "m/5"}, 1, 1.0)]
    [e] = al.evaluate_rule(rule, hit_now, NOW, RULES.recent_seconds)
    assert e.firing and e.value == 1.0
    old_hit = [al.Point(NOW - 700, "api", "never_break_hits", {"model_version": "m/5"}, 1, 1.0)]
    [e2] = al.evaluate_rule(rule, old_hit, NOW, RULES.recent_seconds)
    assert not e2.firing and e2.dedup_key == e.dedup_key


def test_transitions_are_deduplicated_and_resolve_without_data() -> None:
    slow = al.evaluate_rules(RULES, _pts("latency_ms", [35.0] * 37), NOW)
    events = al.transitions({}, slow)
    assert [(e.rule, e.state) for e in events] == [("api_p99_latency", al.FIRING)]
    key = events[0].dedup_key
    firing = {
        key: al.CurrentAlert("api_p99_latency", key, {"model_version": "m/1"}, "page", al.FIRING)
    }
    assert al.transitions(firing, slow) == []  # still firing: nothing new to store
    ok = al.evaluate_rules(RULES, _pts("latency_ms", [3.0] * 37), NOW)
    [res] = al.transitions(firing, ok)
    assert (res.state, res.dedup_key) == (al.RESOLVED, key)
    [gone] = al.transitions(firing, [])  # the series stopped reporting
    assert gone.state == al.RESOLVED and gone.message == "no recent data for this alert"
    resolved = {key: al.CurrentAlert("api_p99_latency", key, {}, "page", al.RESOLVED)}
    assert al.transitions(resolved, ok) == []


def test_rule_metrics_and_rows_round_trip() -> None:
    assert al.rule_metrics(RULES) == sorted(
        {"latency_ms", "errors", "requests", "consumer_lag", "never_break_hits"}
    )
    [p] = al.points_from_rows(
        [(NOW, "api", "latency_ms", '{"model_version": "m/1"}', 3, 9.0, 1.0, 2.0, 3.0)]
    )
    assert p == al.Point(NOW, "api", "latency_ms", {"model_version": "m/1"}, 3, 9.0, 1.0, 2.0, 3.0)
    with pytest.raises(ValueError, match="unique"):
        AlertRulesConfig.model_validate(
            {**RULES.model_dump(), "rules": [RULES.rules[0].model_dump()] * 2}
        )


def test_linear_slope_matches_numpy_polyfit() -> None:
    rng = np.random.default_rng(3)
    x = np.sort(rng.uniform(0, 600, 50))
    y = 3.5 * x + rng.normal(0, 20, 50)
    assert linear_slope(x, y) == pytest.approx(np.polyfit(x, y, 1)[0])
    assert math.isnan(linear_slope([1.0], [2.0]))
    assert math.isnan(linear_slope([5.0, 5.0], [1.0, 2.0]))


# ------------------------------------------------------------------ worker + rollout health


def test_run_jobs_runs_each_job_on_its_own_schedule_and_survives_errors() -> None:
    t = [0.0]
    stop = threading.Event()
    ran: list[str] = []
    errors: list[str] = []

    def boom() -> None:
        ran.append("boom")
        raise RuntimeError("x")

    def tick() -> None:
        ran.append("lag")
        if t[0] >= 60:
            stop.set()

    class Clock:
        def __call__(self) -> float:
            return t[0]

    jobs = [
        Job("lag", 10, tick),
        Job("alerts", 30, lambda: ran.append("alerts")),
        Job("boom", 1000, boom),
    ]
    real_wait = stop.wait

    def fake_wait(timeout: float | None = None) -> bool:
        t[0] += timeout or 0.0
        return real_wait(0)

    stop.wait = fake_wait  # type: ignore[method-assign]
    run_jobs(jobs, stop, clock=Clock(), on_error=lambda n, e: errors.append(n))
    assert ran.count("lag") == 7  # t = 0, 10, ..., 60
    assert ran.count("alerts") == 3  # t = 0, 30, 60
    assert ran.count("boom") == 1 and errors == ["boom"]


def test_rollout_health_from_ops_metrics_rows() -> None:
    rows: list[tuple[Any, ...]] = [
        ("requests", {"model_version": "m/7", "status": "200"}, 90, 90.0, None),
        ("requests", {"model_version": "m/7", "status": "500"}, 3, 3.0, None),
        ("requests", '{"model_version": "m/1", "status": "200"}', 900, 900.0, None),
        ("latency_ms", {"model_version": "m/7"}, 93, 400.0, 7.5),
        ("latency_ms", {"model_version": "m/1"}, 900, 2000.0, 5.0),
    ]
    h = health_from_stats(stats_from_ops_rows(rows), "m/7", "m/1")
    assert (h.candidate_requests, h.candidate_errors, h.candidate_latency_samples) == (93, 3, 93)
    assert (h.candidate_p99_ms, h.production_p99_ms) == (7.5, 5.0)
