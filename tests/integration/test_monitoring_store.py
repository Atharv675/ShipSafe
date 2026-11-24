"""Stage 8 against the running stack: traces, ops_metrics, alerts, retention, grants.

Writes run inside transactions that are always rolled back (alerts are append-only, and the
retention job must not touch real rows), except the API's own requests.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from sentinel.config import RetentionConfig, load_monitoring_config
from sentinel.monitoring.alerts import INSERT_ALERT, SELECT_CURRENT, SELECT_POINTS
from sentinel.monitoring.metrics import INSERT_OPS_METRICS
from sentinel.monitoring.retention import run_retention
from sentinel.rollout.store import OPS_STATS_SQL, health_from_stats, stats_from_ops_rows

pytestmark = pytest.mark.integration

API = os.environ.get("SENTINEL_API_URL", "")
ADMIN_DSN = os.environ.get("SENTINEL_IT_ADMIN_DSN", "")
API_DSN = os.environ.get("SENTINEL_IT_API_DSN", "")
ROLLOUT_DSN = os.environ.get("SENTINEL_IT_ROLLOUT_DSN", "")
MONITOR_DSN = os.environ.get("SENTINEL_IT_MONITOR_DSN", "")
DASHBOARD_DSN = os.environ.get("SENTINEL_IT_DASHBOARD_DSN", "")
JAEGER = os.environ.get("SENTINEL_IT_JAEGER_URL", "http://jaeger:16686")
SAMPLES = Path(os.environ.get("SENTINEL_DATA_DIR", "/data")) / "samples" / "Win64"
MON = load_monitoring_config()

if not (API and ADMIN_DSN and API_DSN and ROLLOUT_DSN and MONITOR_DSN and DASHBOARD_DSN):
    pytest.skip("stack not configured (run via make test-integration)", allow_module_level=True)


@pytest.fixture(scope="module")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=API, timeout=30) as c:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                if c.get("/readyz").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        assert c.get("/readyz").status_code == 200, "api never became ready"
        yield c


def _sample() -> dict[str, Any]:
    path = SAMPLES / "benign_1.json"
    if not path.exists():
        pytest.skip("no sample requests: run `make sample-request` first")
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return {**body, "tenant_id": f"it-{uuid.uuid4().hex[:12]}"}


def _poll(fn: Any, seconds: float) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        out = fn()
        if out or time.monotonic() > deadline:
            return out
        time.sleep(1)


# ------------------------------------------------------------------ traces


def test_prediction_stores_the_trace_id_and_jaeger_has_the_trace(http: httpx.Client) -> None:
    trace_id = uuid.uuid4().hex
    parent = f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01"
    out = http.post("/v1/score", json=_sample(), headers={"traceparent": parent}).json()

    def row() -> Any:
        with psycopg.connect(ADMIN_DSN) as conn:
            return conn.execute(
                "SELECT trace_id FROM predictions WHERE request_id = %s", (out["request_id"],)
            ).fetchone()

    stored = _poll(row, 20)
    assert stored is not None and stored[0] == trace_id

    def spans() -> list[dict[str, Any]]:
        r = httpx.get(f"{JAEGER}/api/traces/{trace_id}", timeout=10)
        if r.status_code != 200:
            return []
        data = r.json().get("data") or []
        return [s for t in data for s in t.get("spans", [])]

    found = _poll(spans, 30)
    names = {s["operationName"] for s in found}
    assert {"score", "log"} <= names, names


# ------------------------------------------------------------------ ops_metrics


def test_api_writes_its_metrics_to_ops_metrics(http: httpx.Client) -> None:
    # a row is labelled with the START of the 10-second window just flushed (floored), so a
    # request at t lands in a bucket as early as t - 2 windows, depending on the flush moment
    started = datetime.now(UTC) - timedelta(seconds=2 * MON.metrics.flush_seconds)
    for _ in range(3):
        assert http.post("/v1/score", json=_sample()).status_code == 200

    def rows() -> list[Any]:
        with psycopg.connect(ADMIN_DSN) as conn:
            return conn.execute(
                "SELECT metric, bucket_seconds, count, p99 FROM ops_metrics "
                "WHERE service = 'api' AND ts_bucket >= %s AND metric IN ('requests', "
                "'latency_ms')",
                (started,),
            ).fetchall()

    found = _poll(lambda: {r[0] for r in rows()} >= {"requests", "latency_ms"}, 35)
    assert found, rows()
    lat = [r for r in rows() if r[0] == "latency_ms"]
    assert all(r[1] == MON.metrics.flush_seconds and r[2] > 0 and r[3] > 0 for r in lat)


def test_roles_have_least_privilege_on_stage8_tables() -> None:
    now = datetime.now(UTC)
    ops = (now, 10, "it-svc", "it-metric", "{}", 1, 1.0, None, None, None)
    with psycopg.connect(API_DSN) as conn, conn.transaction(force_rollback=True):
        conn.execute(INSERT_OPS_METRICS, ops)  # the API writes its own metrics
        for sql in (
            "SELECT 1 FROM ops_metrics LIMIT 1",
            "SELECT 1 FROM alerts LIMIT 1",
            "DELETE FROM ops_metrics WHERE false",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(sql)
    with psycopg.connect(DASHBOARD_DSN) as conn, conn.transaction(force_rollback=True):
        for table in (
            "predictions",
            "ops_metrics",
            "alerts",
            "rollout_transitions",
            "gate_decisions",
            "tenant_overrides",
        ):
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        for sql in (
            "DELETE FROM ops_metrics WHERE false",
            "DELETE FROM tenant_overrides WHERE false",
            "UPDATE predictions SET verdict = verdict WHERE false",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(sql)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(INSERT_OPS_METRICS, ops)
    with psycopg.connect(MONITOR_DSN) as conn, conn.transaction(force_rollback=True):
        conn.execute(INSERT_OPS_METRICS, ops)
        conn.execute("DELETE FROM ops_metrics WHERE service = 'it-svc'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("SELECT 1 FROM predictions LIMIT 1")
    with psycopg.connect(ROLLOUT_DSN) as conn, conn.transaction(force_rollback=True):
        conn.execute("SELECT 1 FROM ops_metrics LIMIT 1")
        conn.execute("SELECT 1 FROM alerts LIMIT 1")


# ------------------------------------------------------------------ alerts


def _alert(key: str, state: str) -> tuple[Any, ...]:
    return ("it_rule", key, "{}", state, "page", 1.0, "integration test")


def test_alerts_are_append_only_and_deduplicated_by_the_database() -> None:
    key = f"it_rule|run={uuid.uuid4().hex[:8]}"
    with psycopg.connect(MONITOR_DSN) as conn, conn.transaction(force_rollback=True):
        with pytest.raises(psycopg.errors.RaiseException, match="first state"), conn.transaction():
            conn.execute(INSERT_ALERT, _alert(key, "RESOLVED"))
        conn.execute(INSERT_ALERT, _alert(key, "FIRING"))
        with (
            pytest.raises(psycopg.errors.RaiseException, match="already FIRING"),
            conn.transaction(),
        ):
            conn.execute(INSERT_ALERT, _alert(key, "FIRING"))
        conn.execute(INSERT_ALERT, _alert(key, "RESOLVED"))
        conn.execute(INSERT_ALERT, _alert(key, "FIRING"))
        current = {r[0]: r[4] for r in conn.execute(SELECT_CURRENT).fetchall()}
        assert current[key] == "FIRING"
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("UPDATE alerts SET state = 'RESOLVED' WHERE dedup_key = %s", (key,))
    with psycopg.connect(ADMIN_DSN) as conn, conn.transaction(force_rollback=True):
        conn.execute(INSERT_ALERT, _alert(key, "FIRING"))
        for sql, args in (
            ("UPDATE alerts SET message = 'x' WHERE dedup_key = %s", (key,)),
            ("DELETE FROM alerts WHERE dedup_key = %s", (key,)),
            ("TRUNCATE alerts", ()),
        ):
            with (
                pytest.raises(psycopg.errors.RaiseException, match="append-only"),
                conn.transaction(),
            ):
                conn.execute(sql, args)


def test_alert_point_query_reads_fine_buckets_of_rule_metrics() -> None:
    svc = f"it-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    with psycopg.connect(MONITOR_DSN) as conn, conn.transaction(force_rollback=True):
        for secs, age in ((10, 30), (60, 30), (10, 4000)):
            conn.execute(
                INSERT_OPS_METRICS,
                (
                    now - timedelta(seconds=age),
                    secs,
                    svc,
                    "latency_ms",
                    '{"model_version": "x"}',
                    5,
                    10.0,
                    1.0,
                    2.0,
                    3.0,
                ),
            )
        rows = conn.execute(
            SELECT_POINTS, (now - timedelta(seconds=660), 10, ["latency_ms"])
        ).fetchall()
        mine = [r for r in rows if r[1] == svc]
        assert len(mine) == 1  # only the recent 10-second row
        assert mine[0][3] == {"model_version": "x"} and float(mine[0][8]) == 3.0


# ------------------------------------------------------------------ retention


def test_retention_downsamples_to_minutes_and_deletes_old_rows() -> None:
    svc = f"it-{uuid.uuid4().hex[:8]}"
    cfg = RetentionConfig(
        run_every_hours=24,
        downsample_after_hours=24,
        downsample_bucket_seconds=60,
        delete_after_days=14,
    )
    with psycopg.connect(MONITOR_DSN) as conn, conn.transaction(force_rollback=True):
        base = conn.execute(
            "SELECT date_bin('60 seconds', now() - interval '2 days', TIMESTAMPTZ 'epoch')"
        ).fetchone()
        assert base is not None
        minute: datetime = base[0]
        # minute 1: six 10-second rows; minute 2: two rows
        rows = [
            (minute + timedelta(seconds=10 * i), 10 * (i + 1), float(i + 1), float(10 + i))
            for i in range(6)
        ]
        rows += [
            (minute + timedelta(seconds=60), 100, 4.0, 50.0),
            (minute + timedelta(seconds=70), 300, 8.0, 20.0),
        ]
        for ts, n, p50, p99 in rows:
            conn.execute(
                INSERT_OPS_METRICS,
                (ts, 10, svc, "latency_ms", '{"m": "1"}', n, float(n), p50, p50, p99),
            )
        conn.execute(
            INSERT_OPS_METRICS,
            (
                datetime.now(UTC) - timedelta(days=15),
                60,
                svc,
                "latency_ms",
                "{}",
                1,
                1.0,
                None,
                None,
                None,
            ),
        )
        recent = datetime.now(UTC) - timedelta(minutes=5)
        conn.execute(
            INSERT_OPS_METRICS, (recent, 10, svc, "requests", "{}", 7, 7.0, None, None, None)
        )
        run_retention(conn, cfg)
        got = conn.execute(
            "SELECT ts_bucket, bucket_seconds, metric, count, sum, p50, p99 FROM ops_metrics "
            "WHERE service = %s ORDER BY ts_bucket",
            (svc,),
        ).fetchall()
    assert [(r[1], r[2]) for r in got] == [(60, "latency_ms"), (60, "latency_ms"), (10, "requests")]
    m1, m2, keep = got
    assert m1[0] == minute and m1[3] == 210 and m1[4] == 210.0
    w = [10 * (i + 1) for i in range(6)]
    assert m1[5] == pytest.approx(sum(n * (i + 1) for i, n in enumerate(w)) / sum(w))
    assert m1[6] == 15.0  # max of the 10-second p99s
    assert m2[0] == minute + timedelta(seconds=60) and m2[3] == 400
    assert m2[5] == pytest.approx((100 * 4 + 300 * 8) / 400) and m2[6] == 50.0
    assert keep[3] == 7  # newer than a day: untouched


def test_rollout_health_query_reads_the_ring_period() -> None:
    svc = f"it-{uuid.uuid4().hex[:8]}"
    since = datetime.now(UTC) - timedelta(minutes=2)
    with psycopg.connect(ADMIN_DSN) as conn, conn.transaction(force_rollback=True):
        data = [
            (
                since - timedelta(minutes=5),
                "requests",
                {"model_version": "c", "status": "200"},
                999,
                None,
            ),  # before the ring switched: ignored
            (
                since + timedelta(seconds=10),
                "requests",
                {"model_version": "c", "status": "200"},
                90,
                None,
            ),
            (
                since + timedelta(seconds=20),
                "requests",
                {"model_version": "c", "status": "503"},
                2,
                None,
            ),
            (since + timedelta(seconds=10), "latency_ms", {"model_version": "c"}, 30, 9.0),
            (since + timedelta(seconds=20), "latency_ms", {"model_version": "c"}, 10, 5.0),
            (since + timedelta(seconds=10), "latency_ms", {"model_version": "p"}, 50, 4.0),
        ]
        for ts, metric, labels, n, p99 in data:
            conn.execute(
                INSERT_OPS_METRICS,
                (ts, 10, svc, metric, json.dumps(labels), n, float(n), p99, p99, p99),
            )
        rows = conn.execute(
            OPS_STATS_SQL, {"service": svc, "since": since, "bucket": 10}
        ).fetchall()
    h = health_from_stats(stats_from_ops_rows([tuple(r) for r in rows]), "c", "p")
    assert (h.candidate_requests, h.candidate_errors, h.candidate_latency_samples) == (92, 2, 40)
    assert h.candidate_p99_ms == pytest.approx((30 * 9.0 + 10 * 5.0) / 40)
    assert h.production_p99_ms == 4.0
