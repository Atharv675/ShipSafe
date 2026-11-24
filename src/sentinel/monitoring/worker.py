"""Stage 8 alert worker: one process, three schedules.

    python -m sentinel.monitoring.worker      (compose service `alert-worker`)

- every `consumer_lag.interval_seconds` (10 s): consumer lag of the live and shadow groups
  -> ops_metrics (written by the same 10-second writer as the API's metrics);
- every `evaluate_every_seconds` (30 s): alert rules -> append-only `alerts` (+ webhook);
- every `retention.run_every_hours` (daily, and once at start): ops_metrics retention.
A failing job logs and waits for its next turn; it never stops the others.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Job:
    name: str
    every_seconds: float
    run: Callable[[], object]
    next_at: float = 0.0  # 0 = run at once


def run_jobs(
    jobs: list[Job],
    stop: threading.Event,
    clock: Callable[[], float] = time.monotonic,
    on_error: Callable[[str, Exception], None] | None = None,
) -> None:
    """Run each job when due, until `stop` is set. Exceptions go to `on_error`."""
    while not stop.is_set():
        now = clock()
        for job in jobs:
            if now >= job.next_at:
                try:
                    job.run()
                except Exception as exc:
                    if on_error is not None:
                        on_error(job.name, exc)
                job.next_at = now + job.every_seconds
        wait = min(j.next_at for j in jobs) - clock()
        stop.wait(max(wait, 0.0))


def main() -> None:  # pragma: no cover - runs in the alert-worker container
    from datetime import UTC, datetime

    import psycopg

    from sentinel.config import (
        Settings,
        load_alert_rules,
        load_monitoring_config,
        load_replay_config,
    )
    from sentinel.logging import configure_logging, get_logger
    from sentinel.monitoring.alerts import AlertEngine, http_webhook
    from sentinel.monitoring.lag import KafkaLagSource, record_lag
    from sentinel.monitoring.metrics import MetricsRecorder, OpsMetricsWriter, postgres_ops_writer
    from sentinel.monitoring.retention import run_retention

    settings = Settings()
    configure_logging(settings.log_level)
    log = get_logger("sentinel.monitoring.worker")
    mon = load_monitoring_config()
    rules = load_alert_rules()
    replay = load_replay_config()
    dsn = os.environ["SENTINEL_MONITOR_DSN"]

    recorder = MetricsRecorder(
        mon.consumer_lag.service, mon.metrics.window_max_samples, mon.metrics.window_max_samples
    )
    writer = OpsMetricsWriter([recorder], postgres_ops_writer(dsn), mon.metrics.flush_seconds)
    lag_source = KafkaLagSource(
        replay.bootstrap_servers, replay.topic, mon.consumer_lag.timeout_seconds
    )
    groups = [replay.live.group_id, replay.shadow.group_id]
    engine = AlertEngine(
        dsn,
        rules,
        round(mon.metrics.flush_seconds),
        http_webhook(rules.webhook_url, rules.webhook_timeout_seconds)
        if rules.webhook_url
        else None,
    )

    def retention() -> None:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            res = run_retention(conn, mon.retention)
        log.info(
            "ops_metrics_retention",
            downsampled_rows_written=res.downsampled_rows_written,
            deleted_rows=res.deleted_rows,
        )

    jobs = [
        Job(
            "consumer-lag",
            mon.consumer_lag.interval_seconds,
            lambda: record_lag(lag_source, groups, replay.topic, recorder),
        ),
        Job("alerts", rules.evaluate_every_seconds, lambda: engine.run_once(datetime.now(UTC))),
        Job("retention", mon.retention.run_every_hours * 3600, retention),
    ]
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    writer.start()
    log.info("alert_worker_started", rules=[r.name for r in rules.rules], groups=groups)
    try:
        run_jobs(
            jobs,
            stop,
            on_error=lambda name, exc: log.error(
                "worker_job_failed", job=name, error=f"{type(exc).__name__}: {exc}"
            ),
        )
    finally:
        writer.close()
        lag_source.close()
        log.info("alert_worker_stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
